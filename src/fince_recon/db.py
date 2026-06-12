"""SQLite 库表与审计留痕。所有写操作只追加、不物理删除。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS periods(
  period TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'open',
  opened_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS accounts(
  account_no TEXT PRIMARY KEY,
  account_name TEXT, bank_name TEXT,
  ledger_account TEXT, currency TEXT DEFAULT 'CNY'
);
CREATE TABLE IF NOT EXISTS bank_txns(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period TEXT NOT NULL, account_no TEXT NOT NULL,
  txn_date TEXT NOT NULL, book_date TEXT,
  amount INTEGER NOT NULL, direction TEXT NOT NULL,
  counterparty_name TEXT, counterparty_norm TEXT, counterparty_account TEXT,
  summary TEXT, bank_txn_no TEXT NOT NULL, recon_code TEXT,
  is_internal INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'unmatched',
  carried_to TEXT,
  source_file TEXT, imported_at TEXT,
  UNIQUE(account_no, bank_txn_no)
);
CREATE TABLE IF NOT EXISTS vouchers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period TEXT NOT NULL,
  voucher_no TEXT NOT NULL, line_no INTEGER NOT NULL DEFAULT 1,
  voucher_date TEXT, biz_date TEXT,
  ledger_account TEXT, account_no TEXT NOT NULL,
  amount INTEGER NOT NULL, direction TEXT NOT NULL,
  counterparty TEXT, counterparty_norm TEXT,
  summary TEXT, biz_no TEXT, recon_code TEXT,
  is_reversal INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'unmatched',
  carried_to TEXT,
  source_file TEXT, imported_at TEXT,
  UNIQUE(voucher_no, line_no)
);
CREATE TABLE IF NOT EXISTS balances(
  period TEXT NOT NULL, account_no TEXT NOT NULL,
  stmt_open INTEGER, stmt_close INTEGER,
  ledger_open INTEGER, ledger_close INTEGER,
  PRIMARY KEY(period, account_no)
);
CREATE TABLE IF NOT EXISTS batches(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period TEXT NOT NULL, started_at TEXT, finished_at TEXT,
  note TEXT, stats TEXT
);
CREATE TABLE IF NOT EXISTS matches(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER, period TEXT NOT NULL,
  method TEXT NOT NULL, confidence TEXT NOT NULL, status TEXT NOT NULL,
  recon_code TEXT,
  bank_total INTEGER NOT NULL, fin_total INTEGER NOT NULL, diff INTEGER NOT NULL,
  tolerance_note TEXT, explanation TEXT,
  created_by TEXT, created_at TEXT,
  confirmed_by TEXT, confirmed_at TEXT,
  cancelled_by TEXT, cancelled_at TEXT, cancel_reason TEXT
);
CREATE TABLE IF NOT EXISTS match_links(
  match_id INTEGER NOT NULL, side TEXT NOT NULL, record_id INTEGER NOT NULL,
  PRIMARY KEY(match_id, side, record_id)
);
CREATE TABLE IF NOT EXISTS tickets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period TEXT NOT NULL, batch_id INTEGER, recon_code TEXT,
  diff_amount INTEGER,
  reason_code TEXT, reason_label TEXT,
  assignee TEXT, suggested_action TEXT, ai_note TEXT,
  status TEXT NOT NULL DEFAULT '待派单',
  carried_from TEXT, carried_to TEXT, resolved_note TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS ticket_links(
  ticket_id INTEGER NOT NULL, side TEXT NOT NULL, record_id INTEGER NOT NULL,
  PRIMARY KEY(ticket_id, side, record_id)
);
CREATE TABLE IF NOT EXISTS ticket_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL, from_status TEXT, to_status TEXT,
  actor TEXT, note TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  object_type TEXT, object_id TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS aliases(
  raw TEXT PRIMARY KEY, canonical TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bank_status ON bank_txns(status, period);
CREATE INDEX IF NOT EXISTS idx_voucher_status ON vouchers(status, period);
CREATE INDEX IF NOT EXISTS idx_bank_code ON bank_txns(recon_code);
CREATE INDEX IF NOT EXISTS idx_voucher_code ON vouchers(recon_code);
"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def audit(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    object_type: str = "",
    object_id: str = "",
    detail: dict | str | None = None,
) -> None:
    if isinstance(detail, dict):
        detail = json.dumps(detail, ensure_ascii=False)
    conn.execute(
        "INSERT INTO audit_log(at, actor, action, object_type, object_id, detail)"
        " VALUES(?,?,?,?,?,?)",
        (now(), actor, action, object_type, str(object_id), detail),
    )


def period_status(conn: sqlite3.Connection, period: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM periods WHERE period=?", (period,)
    ).fetchone()
    return row["status"] if row else None


def require_open_period(conn: sqlite3.Connection, period: str) -> None:
    st = period_status(conn, period)
    if st is None:
        raise SystemExit(f"期间 {period} 不存在，请先执行: fince-recon period open {period}")
    if st != "open":
        raise SystemExit(f"期间 {period} 已关账，禁止写操作；如需调整请先重开期间")
