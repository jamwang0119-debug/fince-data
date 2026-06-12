"""核销生命周期：建议核销的确认/退回、反核销、人工核销。

所有动作只追加不删除：撤销的核销记录保留为 cancelled 状态并记录操作人/时间/原因。
"""

from __future__ import annotations

import sqlite3

from fince_recon.config import Config
from fince_recon.db import audit, now, require_open_period
from fince_recon.money import fmt
from fince_recon.quality import bank_signed, voucher_signed
from fince_recon.tickets import auto_close_resolved


def _get_match(conn, match_id: int):
    m = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    if not m:
        raise SystemExit(f"核销记录 {match_id} 不存在")
    return m


def _set_record_status(conn, match_id: int, status: str):
    for link in conn.execute(
        "SELECT side, record_id FROM match_links WHERE match_id=?", (match_id,)
    ).fetchall():
        table = "bank_txns" if link["side"] == "bank" else "vouchers"
        conn.execute(
            f"UPDATE {table} SET status=? WHERE id=?", (status, link["record_id"])
        )


def confirm_match(conn: sqlite3.Connection, match_id: int, actor: str) -> None:
    m = _get_match(conn, match_id)
    require_open_period(conn, m["period"])
    if m["status"] != "pending":
        raise SystemExit(f"核销 {match_id} 状态为 {m['status']}，仅 pending 可确认")
    conn.execute(
        "UPDATE matches SET status='confirmed', confirmed_by=?, confirmed_at=? WHERE id=?",
        (actor, now(), match_id),
    )
    _set_record_status(conn, match_id, "matched")
    audit(conn, actor, "match_confirm", "match", match_id)
    auto_close_resolved(conn, m["period"], actor)
    conn.commit()


def reject_match(conn: sqlite3.Connection, match_id: int, actor: str, reason: str) -> None:
    m = _get_match(conn, match_id)
    require_open_period(conn, m["period"])
    if m["status"] != "pending":
        raise SystemExit(f"核销 {match_id} 状态为 {m['status']}，仅 pending 可退回")
    conn.execute(
        "UPDATE matches SET status='cancelled', cancelled_by=?, cancelled_at=?,"
        " cancel_reason=? WHERE id=?",
        (actor, now(), reason or "建议核销被退回", match_id),
    )
    _set_record_status(conn, match_id, "unmatched")
    audit(conn, actor, "match_reject", "match", match_id, reason)
    conn.commit()


def unmatch(conn: sqlite3.Connection, match_id: int, actor: str, reason: str) -> None:
    """反核销：撤销自动或已确认的核销，关联记录释放回未匹配。"""
    if not reason:
        raise SystemExit("反核销必须提供 --reason 原因（审计要求）")
    m = _get_match(conn, match_id)
    require_open_period(conn, m["period"])
    if m["status"] not in ("auto", "confirmed"):
        raise SystemExit(f"核销 {match_id} 状态为 {m['status']}，仅 auto/confirmed 可反核销")
    conn.execute(
        "UPDATE matches SET status='cancelled', cancelled_by=?, cancelled_at=?,"
        " cancel_reason=? WHERE id=?",
        (actor, now(), reason, match_id),
    )
    _set_record_status(conn, match_id, "unmatched")
    audit(conn, actor, "match_unmatch", "match", match_id, reason)
    conn.commit()


def manual_match(
    conn: sqlite3.Connection,
    cfg: Config,
    period: str,
    bank_ids: list[int],
    voucher_ids: list[int],
    actor: str,
    note: str = "",
    allow_diff: bool = False,
) -> int:
    """人工核销：指定两侧记录直接核销，默认要求轧平（容差内），--allow-diff 放开。"""
    require_open_period(conn, period)
    if not bank_ids and not voucher_ids:
        raise SystemExit("人工核销至少要指定一侧记录")
    bank_rows, fin_rows = [], []
    for bid in bank_ids:
        r = conn.execute("SELECT * FROM bank_txns WHERE id=?", (bid,)).fetchone()
        if not r:
            raise SystemExit(f"银行流水 {bid} 不存在")
        if r["status"] != "unmatched":
            raise SystemExit(f"银行流水 {bid} 状态为 {r['status']}，不可重复核销")
        bank_rows.append(r)
    for vid in voucher_ids:
        r = conn.execute("SELECT * FROM vouchers WHERE id=?", (vid,)).fetchone()
        if not r:
            raise SystemExit(f"凭证行 {vid} 不存在")
        if r["status"] != "unmatched":
            raise SystemExit(f"凭证行 {vid} 状态为 {r['status']}，不可重复核销")
        fin_rows.append(r)
    bank_total = sum(bank_signed(r) for r in bank_rows)
    fin_total = sum(voucher_signed(r) for r in fin_rows)
    diff = bank_total - fin_total
    if abs(diff) > cfg.tolerance_cents and not allow_diff:
        raise SystemExit(
            f"两侧差额 {fmt(diff)} 元超出容差，如确认仍要核销请加 --allow-diff 并填写 --note"
        )
    if abs(diff) > cfg.tolerance_cents and not note:
        raise SystemExit("带差额的人工核销必须填写 --note 说明（审计要求）")
    cur = conn.execute(
        "INSERT INTO matches(batch_id, period, method, confidence, status, recon_code,"
        " bank_total, fin_total, diff, tolerance_note, explanation, created_by, created_at)"
        " VALUES(NULL,?,?,?,?,NULL,?,?,?,?,?,?,?)",
        (
            period, "manual", "high", "confirmed",
            bank_total, fin_total, diff,
            f"人工核销差额 {fmt(diff)} 元" if diff else None,
            note or None, actor, now(),
        ),
    )
    mid = cur.lastrowid
    for r in bank_rows:
        conn.execute(
            "INSERT INTO match_links(match_id, side, record_id) VALUES(?,?,?)",
            (mid, "bank", r["id"]),
        )
        conn.execute("UPDATE bank_txns SET status='matched' WHERE id=?", (r["id"],))
    for r in fin_rows:
        conn.execute(
            "INSERT INTO match_links(match_id, side, record_id) VALUES(?,?,?)",
            (mid, "voucher", r["id"]),
        )
        conn.execute("UPDATE vouchers SET status='matched' WHERE id=?", (r["id"],))
    audit(conn, actor, "match_manual", "match", mid,
          {"bank": bank_ids, "voucher": voucher_ids, "diff": diff})
    auto_close_resolved(conn, period, actor)
    conn.commit()
    return mid
