import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fince_recon.config import Config  # noqa: E402
from fince_recon.db import connect, now  # noqa: E402

ACC = "110060001234567"


@pytest.fixture
def cfg():
    return Config()


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    c.execute(
        "INSERT INTO accounts(account_no, account_name, ledger_account) VALUES(?,?,?)",
        (ACC, "测试户", "1002.01"),
    )
    c.execute(
        "INSERT INTO periods(period, status, opened_at) VALUES('2026-05','open',?)",
        (now(),),
    )
    c.commit()
    return c


_seq = {"n": 0}


def add_bank(conn, amount_cents, direction="IN", date="2026-05-10", code="",
             cp="", summary="", acc=ACC, period="2026-05"):
    _seq["n"] += 1
    from fince_recon.normalize import norm_name
    cur = conn.execute(
        "INSERT INTO bank_txns(period, account_no, txn_date, book_date, amount,"
        " direction, counterparty_name, counterparty_norm, summary, bank_txn_no,"
        " recon_code, imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (period, acc, date, date, amount_cents, direction, cp, norm_name(cp),
         summary, f"BK{_seq['n']:06d}", code, now()),
    )
    return cur.lastrowid


def add_voucher(conn, amount_cents, direction="IN", date="2026-05-10", code="",
                cp="", summary="", reversal=False, acc=ACC, period="2026-05"):
    _seq["n"] += 1
    from fince_recon.normalize import norm_name
    cur = conn.execute(
        "INSERT INTO vouchers(period, voucher_no, line_no, voucher_date, biz_date,"
        " ledger_account, account_no, amount, direction, counterparty,"
        " counterparty_norm, summary, recon_code, is_reversal, imported_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (period, f"记{_seq['n']:06d}", 1, date, date, "1002.01", acc, amount_cents,
         direction, cp, norm_name(cp), summary, code, 1 if reversal else 0, now()),
    )
    return cur.lastrowid


def bank_status(conn, rid):
    return conn.execute("SELECT status FROM bank_txns WHERE id=?", (rid,)).fetchone()[0]


def voucher_status(conn, rid):
    return conn.execute("SELECT status FROM vouchers WHERE id=?", (rid,)).fetchone()[0]


def matches(conn, **kw):
    q = "SELECT * FROM matches WHERE 1=1"
    params = []
    for k, val in kw.items():
        q += f" AND {k}=?"
        params.append(val)
    return conn.execute(q, params).fetchall()


def tickets(conn):
    return conn.execute("SELECT * FROM tickets ORDER BY id").fetchall()
