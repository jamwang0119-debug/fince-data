"""分层匹配引擎：余额勾稽之后的明细核销主链路。

第 1 层 对账码分组轧差（含容差核销、组内部分核销）
第 2 层 多字段组合精确匹配（金额+方向+账号+日期，优先同对手方）
第 3 层 subset-sum 凑数匹配 → 建议核销（需人工确认）
第 4 层 户名相似度模糊匹配（AI 兜底） → 建议核销（需人工确认）

重跑安全：已 matched/suggested 的记录不进入匹配范围，人工成果永不被冲掉。
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict

from fince_recon.ai import AIProvider, RuleProvider, bank_desc, voucher_desc
from fince_recon.config import Config
from fince_recon.db import audit, now, require_open_period
from fince_recon.money import fmt
from fince_recon.normalize import days_between, similarity
from fince_recon.quality import bank_signed, voucher_signed
from fince_recon.subset import find_subset
from fince_recon.tickets import auto_close_resolved, create_ticket, open_ticket_record_ids


class MatchEngine:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        provider: AIProvider | None = None,
        actor: str = "system",
    ):
        self.conn = conn
        self.cfg = cfg
        self.provider = provider or RuleProvider()
        self.actor = actor
        self.aliases: dict[str, str] = {
            r["raw"]: r["canonical"]
            for r in conn.execute("SELECT raw, canonical FROM aliases")
        }

    # ------------------------------------------------------------------ utils

    def canon(self, norm: str) -> str:
        return self.aliases.get(norm, norm)

    def _lock(self, bank_rows, fin_rows):
        self._locked_bank.update(r["id"] for r in bank_rows)
        self._locked_fin.update(r["id"] for r in fin_rows)

    def _create_match(
        self,
        method: str,
        confidence: str,
        status: str,
        recon_code: str | None,
        bank_rows: list,
        fin_rows: list,
        tolerance_note: str | None = None,
        explanation: str | None = None,
    ) -> int:
        bank_total = sum(bank_signed(r) for r in bank_rows)
        fin_total = sum(voucher_signed(r) for r in fin_rows)
        diff = bank_total - fin_total
        cur = self.conn.execute(
            "INSERT INTO matches(batch_id, period, method, confidence, status,"
            " recon_code, bank_total, fin_total, diff, tolerance_note, explanation,"
            " created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._batch_id, self._period, method, confidence, status,
                recon_code, bank_total, fin_total, diff,
                tolerance_note, explanation, self.actor, now(),
            ),
        )
        mid = cur.lastrowid
        rec_status = "matched" if status in ("auto", "confirmed") else "suggested"
        for r in bank_rows:
            self.conn.execute(
                "INSERT INTO match_links(match_id, side, record_id) VALUES(?,?,?)",
                (mid, "bank", r["id"]),
            )
            self.conn.execute(
                "UPDATE bank_txns SET status=? WHERE id=?", (rec_status, r["id"])
            )
        for r in fin_rows:
            self.conn.execute(
                "INSERT INTO match_links(match_id, side, record_id) VALUES(?,?,?)",
                (mid, "voucher", r["id"]),
            )
            self.conn.execute(
                "UPDATE vouchers SET status=? WHERE id=?", (rec_status, r["id"])
            )
        self._lock(bank_rows, fin_rows)
        self.stats[f"match_{method}"] += 1
        self.stats["matched_bank"] += len(bank_rows) if rec_status == "matched" else 0
        self.stats["matched_fin"] += len(fin_rows) if rec_status == "matched" else 0
        self.stats["suggested"] += 1 if status == "pending" else 0
        audit(self.conn, self.actor, "match_create", "match", mid,
              {"method": method, "confidence": confidence, "diff": diff})
        return mid

    def _queue_ticket(self, reason, recon_code, diff, bank_rows, fin_rows):
        self._pending_tickets.append((reason, recon_code, diff, bank_rows, fin_rows))
        self._lock(bank_rows, fin_rows)

    def _classify_bank_single(self, row) -> str:
        text = (row["summary"] or "") + (row["counterparty_name"] or "")
        if any(kw in text for kw in self.cfg.fee_keywords):
            return "FEE_INTEREST"
        return "BANK_ONLY"

    # ------------------------------------------------------------------- run

    def run(self, period: str, note: str = "") -> dict:
        require_open_period(self.conn, period)
        self._period = period
        self.stats: Counter = Counter()
        self._pending_tickets = []
        self._locked_bank: set[int] = set()
        self._locked_fin: set[int] = set()

        cur = self.conn.execute(
            "INSERT INTO batches(period, started_at, note) VALUES(?,?,?)",
            (period, now(), note),
        )
        self._batch_id = cur.lastrowid

        bank = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM bank_txns WHERE status='unmatched'"
                " AND (period=? OR carried_to=?) ORDER BY txn_date, id",
                (period, period),
            )
        ]
        fin = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM vouchers WHERE status='unmatched'"
                " AND (period=? OR carried_to=?) ORDER BY voucher_date, id",
                (period, period),
            )
        ]
        self.stats["scope_bank"] = len(bank)
        self.stats["scope_fin"] = len(fin)

        self._layer1_code_groups(bank, fin)
        bank, fin = self._remaining(bank, fin)
        self._layer2_exact(bank, fin)
        bank, fin = self._remaining(bank, fin)
        self._layer3_subset(bank, fin)
        bank, fin = self._remaining(bank, fin)
        self._layer4_fuzzy(bank, fin)
        bank, fin = self._remaining(bank, fin)

        closed = auto_close_resolved(self.conn, period, self.actor)
        self.stats["tickets_autoclosed"] = len(closed)
        self._flush_tickets(period, bank, fin)

        self.stats["unmatched_bank"] = self.conn.execute(
            "SELECT COUNT(*) c FROM bank_txns WHERE status='unmatched'"
            " AND (period=? OR carried_to=?)", (period, period)
        ).fetchone()["c"]
        self.stats["unmatched_fin"] = self.conn.execute(
            "SELECT COUNT(*) c FROM vouchers WHERE status='unmatched'"
            " AND (period=? OR carried_to=?)", (period, period)
        ).fetchone()["c"]
        self.conn.execute(
            "UPDATE batches SET finished_at=?, stats=? WHERE id=?",
            (now(), json.dumps(dict(self.stats), ensure_ascii=False), self._batch_id),
        )
        audit(self.conn, self.actor, "batch_run", "batch", self._batch_id, dict(self.stats))
        self.conn.commit()
        return dict(self.stats)

    def _remaining(self, bank, fin):
        return (
            [r for r in bank if r["id"] not in self._locked_bank],
            [r for r in fin if r["id"] not in self._locked_fin],
        )

    # --------------------------------------------------------- 第 1 层：对账码

    def _layer1_code_groups(self, bank, fin):
        tol = self.cfg.tolerance_cents
        by_code_b: dict[str, list] = defaultdict(list)
        by_code_f: dict[str, list] = defaultdict(list)
        for r in bank:
            if r["recon_code"]:
                by_code_b[r["recon_code"]].append(r)
        for r in fin:
            if r["recon_code"]:
                by_code_f[r["recon_code"]].append(r)

        for code in sorted(set(by_code_b) & set(by_code_f)):
            b, f = by_code_b[code], by_code_f[code]
            # 方向校验：红冲行除外，组内方向必须一致
            if len({r["direction"] for r in b}) > 1 or len(
                {r["direction"] for r in f if not r["is_reversal"]}
            ) > 1:
                bsum = sum(bank_signed(r) for r in b)
                fsum = sum(voucher_signed(r) for r in f)
                self._queue_ticket("DIRECTION_ERROR", code, bsum - fsum, b, f)
                continue
            bsum = sum(bank_signed(r) for r in b)
            fsum = sum(voucher_signed(r) for r in f)
            diff = bsum - fsum
            if diff == 0:
                if len(b) == 1 and len(f) == 1:
                    self._create_match("code_1_1", "high", "auto", code, b, f)
                elif len(b) == 1 or len(f) == 1:
                    self._create_match("code_group", "high", "auto", code, b, f)
                else:
                    self._create_match(
                        "code_group", "medium", "auto", code, b, f,
                        explanation="M:N 分组轧平核销，建议抽样复核",
                    )
            elif abs(diff) <= tol:
                self._create_match(
                    "code_tolerance", "medium", "auto", code, b, f,
                    tolerance_note=f"容差核销，差额 {fmt(diff)} 元挂手续费/尾差调节项",
                )
            else:
                self._layer1_partial(code, b, f, bsum, fsum, diff)

    def _layer1_partial(self, code, b, f, bsum, fsum, diff):
        """同码轧不平：下钻子集找可轧平部分，先核销、剩余挂账；找不到整组挂账。"""
        limit = self.cfg.subset_max_items
        sub = find_subset([bank_signed(r) for r in b], fsum, limit)
        if sub is not None:
            part = [b[i] for i in sub]
            rest = [b[i] for i in range(len(b)) if i not in set(sub)]
            self._create_match(
                "code_partial", "medium", "auto", code, part, f,
                explanation="组内子集轧平（subset-sum），部分核销，剩余挂账",
            )
            reason = (
                self._classify_bank_single(rest[0]) if len(rest) == 1 else "BANK_ONLY"
            )
            self._queue_ticket(reason, code, sum(bank_signed(r) for r in rest), rest, [])
            return
        sub = find_subset([voucher_signed(r) for r in f], bsum, limit)
        if sub is not None:
            part = [f[i] for i in sub]
            rest = [f[i] for i in range(len(f)) if i not in set(sub)]
            self._create_match(
                "code_partial", "medium", "auto", code, b, part,
                explanation="组内子集轧平（subset-sum），部分核销，剩余挂账",
            )
            self._queue_ticket(
                "FIN_ONLY", code, -sum(voucher_signed(r) for r in rest), [], rest
            )
            return
        self._queue_ticket("AMOUNT_MISMATCH", code, diff, b, f)

    # --------------------------------------------------- 第 2 层：多字段精确

    def _layer2_exact(self, bank, fin):
        window = self.cfg.date_window_days

        def explain(b, f):
            if (b["recon_code"] or f["recon_code"]) and b["recon_code"] != f["recon_code"]:
                return (
                    f"对账码不一致（银行:{b['recon_code'] or '无'} /"
                    f" 凭证:{f['recon_code'] or '无'}），按强字段精确匹配救回，建议修正对账码"
                )
            return None

        # Pass A：金额+方向+账号+对手方（经别名归一）一致，时间窗内按日期就近配对
        by_key_f: dict[tuple, list] = defaultdict(list)
        for r in fin:
            if r["id"] in self._locked_fin or r["is_reversal"]:
                continue
            cp = self.canon(r["counterparty_norm"] or "")
            if cp:
                by_key_f[(r["account_no"], r["direction"], r["amount"], cp)].append(r)
        for b in bank:
            if b["id"] in self._locked_bank:
                continue
            cp = self.canon(b["counterparty_norm"] or "")
            if not cp:
                continue
            cands = [
                r
                for r in by_key_f.get((b["account_no"], b["direction"], b["amount"], cp), [])
                if r["id"] not in self._locked_fin
                and days_between(b["txn_date"], r["voucher_date"]) <= window
            ]
            if cands:
                f = min(cands, key=lambda r: days_between(b["txn_date"], r["voucher_date"]))
                self._create_match("exact", "high", "auto", None, [b], [f],
                                   explanation=explain(b, f))

        # Pass B：金额+方向+账号，仅当时间窗内两侧候选唯一时配对（防误配）
        by_key_b: dict[tuple, list] = defaultdict(list)
        by_key_f2: dict[tuple, list] = defaultdict(list)
        for r in bank:
            if r["id"] not in self._locked_bank:
                by_key_b[(r["account_no"], r["direction"], r["amount"])].append(r)
        for r in fin:
            if r["id"] not in self._locked_fin and not r["is_reversal"]:
                by_key_f2[(r["account_no"], r["direction"], r["amount"])].append(r)
        for key, blist in by_key_b.items():
            flist = by_key_f2.get(key, [])
            if len(blist) == 1 and len(flist) == 1:
                b, f = blist[0], flist[0]
                if days_between(b["txn_date"], f["voucher_date"]) <= window:
                    self._create_match("exact", "high", "auto", None, [b], [f],
                                       explanation=explain(b, f))

    # ------------------------------------------------- 第 3 层：subset-sum

    def _cp_compatible(self, b, f) -> bool:
        cb = self.canon(b["counterparty_norm"] or "")
        cf = self.canon(f["counterparty_norm"] or "")
        return not cb or not cf or cb == cf

    def _layer3_subset(self, bank, fin):
        window = self.cfg.date_window_days
        limit = self.cfg.subset_max_items
        # 一笔银行流水 ↔ 多张凭证
        for b in bank:
            if b["id"] in self._locked_bank:
                continue
            cands = [
                f
                for f in fin
                if f["id"] not in self._locked_fin
                and f["account_no"] == b["account_no"]
                and f["direction"] == b["direction"]
                and days_between(b["txn_date"], f["voucher_date"]) <= window
                and self._cp_compatible(b, f)
            ]
            if len(cands) < 2:
                continue
            sub = find_subset([voucher_signed(f) for f in cands], bank_signed(b), limit)
            if sub is not None and len(sub) >= 2:
                rows = [cands[i] for i in sub]
                expl = self.provider.judge_fuzzy(
                    bank_desc(b),
                    "；".join(voucher_desc(r) for r in rows),
                    1.0,
                )
                self._create_match(
                    "subset_sum", "suggested", "pending", None, [b], rows,
                    explanation=f"凑数匹配（1拖{len(rows)}）。{expl}",
                )
        # 一张凭证 ↔ 多笔银行流水
        for f in fin:
            if f["id"] in self._locked_fin:
                continue
            cands = [
                b
                for b in bank
                if b["id"] not in self._locked_bank
                and b["account_no"] == f["account_no"]
                and b["direction"] == f["direction"]
                and days_between(b["txn_date"], f["voucher_date"]) <= window
                and self._cp_compatible(b, f)
            ]
            if len(cands) < 2:
                continue
            sub = find_subset([bank_signed(b) for b in cands], voucher_signed(f), limit)
            if sub is not None and len(sub) >= 2:
                rows = [cands[i] for i in sub]
                expl = self.provider.judge_fuzzy(
                    "；".join(bank_desc(r) for r in rows), voucher_desc(f), 1.0
                )
                self._create_match(
                    "subset_sum", "suggested", "pending", None, rows, [f],
                    explanation=f"凑数匹配（{len(rows)}并1）。{expl}",
                )

    # ----------------------------------------------------- 第 4 层：模糊匹配

    def _layer4_fuzzy(self, bank, fin):
        window = self.cfg.date_window_days
        threshold = self.cfg.fuzzy_threshold
        by_key_f: dict[tuple, list] = defaultdict(list)
        for r in fin:
            if r["id"] not in self._locked_fin and not r["is_reversal"]:
                by_key_f[(r["account_no"], r["direction"], r["amount"])].append(r)
        pairs: list[tuple[float, dict, dict]] = []
        for b in bank:
            if b["id"] in self._locked_bank:
                continue
            for f in by_key_f.get((b["account_no"], b["direction"], b["amount"]), []):
                if days_between(b["txn_date"], f["voucher_date"]) > window:
                    continue
                score = similarity(
                    self.canon(b["counterparty_norm"] or ""),
                    self.canon(f["counterparty_norm"] or ""),
                )
                if score >= threshold:
                    pairs.append((score, b, f))
        for score, b, f in sorted(pairs, key=lambda x: -x[0]):
            if b["id"] in self._locked_bank or f["id"] in self._locked_fin:
                continue
            expl = self.provider.judge_fuzzy(bank_desc(b), voucher_desc(f), score)
            self._create_match(
                "fuzzy", "suggested", "pending", None, [b], [f],
                explanation=f"模糊匹配（相似度 {score:.2f}）。{expl}",
            )

    # ----------------------------------------------------------- 工单生成

    def _flush_tickets(self, period, bank, fin):
        already = open_ticket_record_ids(self.conn)
        created = 0

        def all_on_ticket(bank_rows, fin_rows):
            return all(("bank", r["id"]) in already for r in bank_rows) and all(
                ("voucher", r["id"]) in already for r in fin_rows
            )

        for reason, code, diff, b_rows, f_rows in self._pending_tickets:
            if all_on_ticket(b_rows, f_rows):
                continue
            create_ticket(self.conn, self.cfg, self.provider, period, self._batch_id,
                          reason, code, diff, b_rows, f_rows, self.actor)
            created += 1

        for b in bank:
            if ("bank", b["id"]) in already:
                continue
            reason = self._classify_bank_single(b)
            if reason == "BANK_ONLY" and b["recon_code"]:
                # 有码但对侧无同码，且对侧存在等额候选 → 疑似对账码错/缺
                cand = self.conn.execute(
                    "SELECT 1 FROM vouchers WHERE status='unmatched' AND account_no=?"
                    " AND direction=? AND amount=? LIMIT 1",
                    (b["account_no"], b["direction"], b["amount"]),
                ).fetchone()
                if cand:
                    reason = "CODE_ERROR"
            create_ticket(self.conn, self.cfg, self.provider, period, self._batch_id,
                          reason, b["recon_code"] or None, bank_signed(b), [b], [],
                          self.actor)
            created += 1
        for f in fin:
            if ("voucher", f["id"]) in already:
                continue
            reason = "FIN_ONLY"
            if f["recon_code"]:
                cand = self.conn.execute(
                    "SELECT 1 FROM bank_txns WHERE status='unmatched' AND account_no=?"
                    " AND direction=? AND amount=? LIMIT 1",
                    (f["account_no"], f["direction"], f["amount"]),
                ).fetchone()
                if cand:
                    reason = "CODE_ERROR"
            create_ticket(self.conn, self.cfg, self.provider, period, self._batch_id,
                          reason, f["recon_code"] or None, -voucher_signed(f), [], [f],
                          self.actor)
            created += 1
        self.stats["tickets_created"] = created
