"""数据质量校验：余额连续性勾稽（对账前置总闸门）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from fince_recon.money import fmt


def bank_signed(row) -> int:
    return row["amount"] if row["direction"] == "IN" else -row["amount"]


def voucher_signed(row) -> int:
    s = row["amount"] if row["direction"] == "IN" else -row["amount"]
    return -s if row["is_reversal"] else s


@dataclass
class BalanceCheck:
    account_no: str
    side: str  # bank | ledger
    open_balance: int
    flow_sum: int
    expected_close: int
    actual_close: int
    ok: bool

    def describe(self) -> str:
        mark = "✓" if self.ok else "✗"
        side = "银行对账单" if self.side == "bank" else "账面"
        s = (
            f"  [{mark}] {self.account_no} {side}: 期初 {fmt(self.open_balance)}"
            f" + 发生 {fmt(self.flow_sum)} = {fmt(self.expected_close)}"
            f"，期末 {fmt(self.actual_close)}"
        )
        if not self.ok:
            s += f"  ⚠ 差 {fmt(self.expected_close - self.actual_close)}（疑似流水缺漏/重复）"
        return s


def check_balances(conn: sqlite3.Connection, period: str) -> list[BalanceCheck]:
    """期初 + Σ签字金额 ?= 期末，两侧各自勾稽。未提供余额表的账户跳过。"""
    results: list[BalanceCheck] = []
    for bal in conn.execute(
        "SELECT * FROM balances WHERE period=?", (period,)
    ).fetchall():
        acct = bal["account_no"]
        bsum = sum(
            bank_signed(r)
            for r in conn.execute(
                "SELECT amount, direction FROM bank_txns WHERE period=? AND account_no=?",
                (period, acct),
            )
        )
        results.append(
            BalanceCheck(
                acct, "bank", bal["stmt_open"], bsum,
                bal["stmt_open"] + bsum, bal["stmt_close"],
                bal["stmt_open"] + bsum == bal["stmt_close"],
            )
        )
        vsum = sum(
            voucher_signed(r)
            for r in conn.execute(
                "SELECT amount, direction, is_reversal FROM vouchers"
                " WHERE period=? AND account_no=?",
                (period, acct),
            )
        )
        results.append(
            BalanceCheck(
                acct, "ledger", bal["ledger_open"], vsum,
                bal["ledger_open"] + vsum, bal["ledger_close"],
                bal["ledger_open"] + vsum == bal["ledger_close"],
            )
        )
    return results
