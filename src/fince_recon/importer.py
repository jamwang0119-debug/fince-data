"""数据导入：账户主数据、银行流水、财务凭证、余额表、别名表。

导入即校验：防重（文件内+库内）、作废凭证剔除、必填与格式校验、
科目/账号映射校验、内部互转识别。所有跳过与拒绝逐行计入导入报告。
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from fince_recon.config import Config
from fince_recon.db import audit, now, require_open_period
from fince_recon.money import parse_cents
from fince_recon.normalize import map_direction, norm_name, parse_date


@dataclass
class ImportReport:
    file: str
    kind: str
    inserted: int = 0
    skipped_dup: int = 0
    skipped_void: int = 0
    rejected: list[tuple[int, str]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"导入 {self.kind}: {self.file}",
            f"  新增 {self.inserted} 条，重复跳过 {self.skipped_dup} 条，"
            f"作废剔除 {self.skipped_void} 条，拒绝 {len(self.rejected)} 条",
        ]
        for line_no, reason in self.rejected[:20]:
            lines.append(f"  - 第 {line_no} 行拒绝: {reason}")
        if len(self.rejected) > 20:
            lines.append(f"  ...另有 {len(self.rejected) - 20} 行")
        return "\n".join(lines)


def read_rows(path: str) -> list[dict[str, str]]:
    """读取 CSV（UTF-8/UTF-8-BOM/GBK）或 XLSX，返回按表头取值的字典列表。"""
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"文件不存在: {path}")
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise SystemExit("读取 Excel 需要安装 openpyxl: pip install openpyxl")
        ws = openpyxl.load_workbook(p, read_only=True, data_only=True).active
        rows_iter = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(rows_iter)]
        return [
            {h: ("" if v is None else str(v)) for h, v in zip(header, row)}
            for row in rows_iter
            if any(v not in (None, "") for v in row)
        ]
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SystemExit(f"无法识别文件编码: {path}")
    reader = csv.DictReader(text.splitlines())
    return [
        {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        for r in reader
        if any((v or "").strip() for v in r.values())
    ]


def _get(row: dict[str, str], *names: str, required: bool = False) -> str:
    for n in names:
        if n in row and str(row[n]).strip():
            return str(row[n]).strip()
    if required:
        raise ValueError(f"缺少必填字段: {names[0]}")
    return ""


def import_accounts(
    conn: sqlite3.Connection, path: str, rows: list[dict] | None = None
) -> ImportReport:
    rep = ImportReport(path, "账户主数据")
    for i, row in enumerate(rows if rows is not None else read_rows(path), start=2):
        try:
            no = _get(row, "账号", required=True)
            conn.execute(
                "INSERT INTO accounts(account_no, account_name, bank_name, ledger_account, currency)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(account_no) DO UPDATE SET account_name=excluded.account_name,"
                " bank_name=excluded.bank_name, ledger_account=excluded.ledger_account,"
                " currency=excluded.currency",
                (
                    no,
                    _get(row, "户名"),
                    _get(row, "开户行"),
                    _get(row, "科目编码"),
                    _get(row, "币种") or "CNY",
                ),
            )
            rep.inserted += 1
        except ValueError as e:
            rep.rejected.append((i, str(e)))
    audit(conn, "system", "import_accounts", "file", path, {"inserted": rep.inserted})
    conn.commit()
    return rep


def import_aliases(conn: sqlite3.Connection, path: str) -> ImportReport:
    rep = ImportReport(path, "别名表")
    for i, row in enumerate(read_rows(path), start=2):
        try:
            raw = norm_name(_get(row, "原始名称", required=True))
            canonical = norm_name(_get(row, "标准名称", required=True))
            conn.execute(
                "INSERT INTO aliases(raw, canonical) VALUES(?,?)"
                " ON CONFLICT(raw) DO UPDATE SET canonical=excluded.canonical",
                (raw, canonical),
            )
            rep.inserted += 1
        except ValueError as e:
            rep.rejected.append((i, str(e)))
    conn.commit()
    return rep


def _own_accounts(conn: sqlite3.Connection) -> dict[str, str]:
    """账号 → 科目编码；同时给出科目 → 账号反查。"""
    return {
        r["account_no"]: r["ledger_account"] or ""
        for r in conn.execute("SELECT account_no, ledger_account FROM accounts")
    }


def import_bank(
    conn: sqlite3.Connection,
    path: str,
    period: str,
    cfg: Config,
    rows: list[dict] | None = None,
) -> ImportReport:
    require_open_period(conn, period)
    rep = ImportReport(path, "银行流水")
    own = _own_accounts(conn)
    seen: set[tuple[str, str]] = set()
    for i, row in enumerate(rows if rows is not None else read_rows(path), start=2):
        try:
            account_no = _get(row, "账号", "本方账号", required=True)
            if account_no not in own:
                raise ValueError(f"账号 {account_no} 不在账户主数据中")
            txn_no = _get(row, "银行流水号", required=True)
            key = (account_no, txn_no)
            if key in seen:
                rep.skipped_dup += 1
                continue
            seen.add(key)
            amount = parse_cents(_get(row, "金额", required=True))
            if amount <= 0:
                raise ValueError("银行流水金额必须为正数，方向用「方向」列表达")
            direction = map_direction(
                _get(row, "方向", "收付方向", required=True), cfg.bank_direction
            )
            cp_account = _get(row, "对手方账号")
            cp_name = _get(row, "对手方户名")
            cur = conn.execute(
                "INSERT INTO bank_txns(period, account_no, txn_date, book_date, amount,"
                " direction, counterparty_name, counterparty_norm, counterparty_account,"
                " summary, bank_txn_no, recon_code, is_internal, source_file, imported_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(account_no, bank_txn_no) DO NOTHING",
                (
                    period,
                    account_no,
                    parse_date(_get(row, "交易日期", required=True)),
                    parse_date(_get(row, "入账日期") or _get(row, "交易日期", required=True)),
                    amount,
                    direction,
                    cp_name,
                    norm_name(cp_name),
                    cp_account,
                    _get(row, "摘要", "摘要/附言", "附言"),
                    txn_no,
                    _get(row, "对账码"),
                    1 if cp_account and cp_account in own else 0,
                    path,
                    now(),
                ),
            )
            if cur.rowcount == 0:
                rep.skipped_dup += 1
            else:
                rep.inserted += 1
        except ValueError as e:
            rep.rejected.append((i, str(e)))
    audit(conn, "system", "import_bank", "file", path,
          {"period": period, "inserted": rep.inserted, "dup": rep.skipped_dup})
    conn.commit()
    return rep


def import_vouchers(
    conn: sqlite3.Connection,
    path: str,
    period: str,
    cfg: Config,
    rows: list[dict] | None = None,
) -> ImportReport:
    require_open_period(conn, period)
    rep = ImportReport(path, "财务凭证")
    own = _own_accounts(conn)
    ledger_to_account = {v: k for k, v in own.items() if v}
    seen: set[tuple[str, int]] = set()
    for i, row in enumerate(rows if rows is not None else read_rows(path), start=2):
        try:
            status_raw = _get(row, "凭证状态") or "正常"
            if status_raw == "作废":
                rep.skipped_void += 1
                continue
            voucher_no = _get(row, "凭证号", required=True)
            line_no = int(_get(row, "分录行号") or "1")
            key = (voucher_no, line_no)
            if key in seen:
                rep.skipped_dup += 1
                continue
            seen.add(key)
            ledger = _get(row, "科目编码")
            account_no = _get(row, "银行账号") or ledger_to_account.get(ledger, "")
            if not account_no or account_no not in own:
                raise ValueError(
                    f"无法映射到银行账户（科目 {ledger!r}，账号 {account_no!r}）"
                )
            amount = parse_cents(_get(row, "金额", required=True))
            if amount <= 0:
                raise ValueError("凭证金额必须为正数，红冲用「凭证状态=红冲」表达")
            direction = map_direction(
                _get(row, "借贷方向", "方向", required=True), cfg.voucher_direction
            )
            cp = _get(row, "往来对象", "对手方")
            cur = conn.execute(
                "INSERT INTO vouchers(period, voucher_no, line_no, voucher_date, biz_date,"
                " ledger_account, account_no, amount, direction, counterparty,"
                " counterparty_norm, summary, biz_no, recon_code, is_reversal,"
                " source_file, imported_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(voucher_no, line_no) DO NOTHING",
                (
                    period,
                    voucher_no,
                    line_no,
                    parse_date(_get(row, "凭证日期", required=True)),
                    parse_date(_get(row, "业务日期") or _get(row, "凭证日期", required=True)),
                    ledger,
                    account_no,
                    amount,
                    direction,
                    cp,
                    norm_name(cp),
                    _get(row, "摘要"),
                    _get(row, "关联业务单号", "业务单号"),
                    _get(row, "对账码"),
                    1 if status_raw == "红冲" else 0,
                    path,
                    now(),
                ),
            )
            if cur.rowcount == 0:
                rep.skipped_dup += 1
            else:
                rep.inserted += 1
        except ValueError as e:
            rep.rejected.append((i, str(e)))
    audit(conn, "system", "import_vouchers", "file", path,
          {"period": period, "inserted": rep.inserted, "dup": rep.skipped_dup})
    conn.commit()
    return rep


def import_balances(conn: sqlite3.Connection, path: str, period: str) -> ImportReport:
    require_open_period(conn, period)
    rep = ImportReport(path, "余额表")
    for i, row in enumerate(read_rows(path), start=2):
        try:
            conn.execute(
                "INSERT INTO balances(period, account_no, stmt_open, stmt_close,"
                " ledger_open, ledger_close) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(period, account_no) DO UPDATE SET"
                " stmt_open=excluded.stmt_open, stmt_close=excluded.stmt_close,"
                " ledger_open=excluded.ledger_open, ledger_close=excluded.ledger_close",
                (
                    period,
                    _get(row, "账号", required=True),
                    parse_cents(_get(row, "对账单期初余额", "银行对账单期初余额", required=True)),
                    parse_cents(_get(row, "对账单期末余额", "银行对账单期末余额", required=True)),
                    parse_cents(_get(row, "账面期初余额", required=True)),
                    parse_cents(_get(row, "账面期末余额", required=True)),
                ),
            )
            rep.inserted += 1
        except ValueError as e:
            rep.rejected.append((i, str(e)))
    audit(conn, "system", "import_balances", "file", path, {"period": period})
    conn.commit()
    return rep
