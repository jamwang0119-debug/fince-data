"""银行存款余额调节表：未达账项四分类 + 平衡总闸门校验。

调节后对账单余额 = 对账单期末余额 + 企业已收银行未收 − 企业已付银行未付
调节后账面余额   = 账面期末余额   + 银行已收企业未收 − 银行已付企业未付
平衡校验：两者之差 ?= 本期容差核销累计差额（残差≠0 即存在未解释差异）
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class ReconStatement:
    period: str
    account_no: str
    stmt_close: int | None
    ledger_close: int | None
    bank_in_unbooked: int   # 银行已收、企业未收（未匹配银行流水 IN）
    bank_out_unbooked: int  # 银行已付、企业未付（未匹配银行流水 OUT）
    fin_in_pending: int     # 企业已收、银行未收（未匹配凭证 IN）
    fin_out_pending: int    # 企业已付、银行未付（未匹配凭证 OUT）
    tolerance_total: int    # 本期容差核销累计差额（bank − fin）
    pending_suggestions: int  # 待确认的建议核销笔数（按未达列示）

    @property
    def adjusted_stmt(self) -> int | None:
        if self.stmt_close is None:
            return None
        return self.stmt_close + self.fin_in_pending - self.fin_out_pending

    @property
    def adjusted_ledger(self) -> int | None:
        if self.ledger_close is None:
            return None
        return self.ledger_close + self.bank_in_unbooked - self.bank_out_unbooked

    @property
    def residual(self) -> int | None:
        """未解释差异 = 调节后对账单余额 − 调节后账面余额 − 容差核销累计差额。"""
        if self.adjusted_stmt is None or self.adjusted_ledger is None:
            return None
        return self.adjusted_stmt - self.adjusted_ledger - self.tolerance_total

    @property
    def balanced(self) -> bool | None:
        r = self.residual
        return None if r is None else r == 0


def build_statements(conn: sqlite3.Connection, period: str) -> list[ReconStatement]:
    accounts = [r["account_no"] for r in conn.execute(
        "SELECT account_no FROM accounts ORDER BY account_no"
    )]
    out: list[ReconStatement] = []
    for acct in accounts:
        bal = conn.execute(
            "SELECT * FROM balances WHERE period=? AND account_no=?", (period, acct)
        ).fetchone()
        # 未达：本期范围内（含结转带入）仍未正式核销的记录；建议核销按未达列示
        bank_rows = conn.execute(
            "SELECT amount, direction, status FROM bank_txns"
            " WHERE account_no=? AND status IN ('unmatched','suggested')"
            " AND (period=? OR carried_to=?)",
            (acct, period, period),
        ).fetchall()
        fin_rows = conn.execute(
            "SELECT amount, direction, is_reversal, status FROM vouchers"
            " WHERE account_no=? AND status IN ('unmatched','suggested')"
            " AND (period=? OR carried_to=?)",
            (acct, period, period),
        ).fetchall()
        bu = sum(r["amount"] for r in bank_rows if r["direction"] == "IN")
        bd = sum(r["amount"] for r in bank_rows if r["direction"] == "OUT")
        # 凭证按签字金额归类：红冲行抵减原方向
        fi = fo = 0
        for r in fin_rows:
            signed = r["amount"] if r["direction"] == "IN" else -r["amount"]
            if r["is_reversal"]:
                signed = -signed
            if signed >= 0:
                fi += signed
            else:
                fo += -signed
        pending = sum(
            1 for r in list(bank_rows) + list(fin_rows) if r["status"] == "suggested"
        )
        # 容差差额跨期累计：历史容差核销的残差会一直留在两侧余额之差中，
        # 直至财务补调整凭证，因此取所有 ≤ 本期的生效核销 diff 合计
        tol = 0
        for m in conn.execute(
            "SELECT m.id, m.diff FROM matches m WHERE m.period<=?"
            " AND m.status IN ('auto','confirmed') AND m.diff != 0",
            (period,),
        ).fetchall():
            link = conn.execute(
                "SELECT b.account_no FROM match_links l JOIN bank_txns b"
                " ON b.id = l.record_id WHERE l.match_id=? AND l.side='bank' LIMIT 1",
                (m["id"],),
            ).fetchone()
            acct_of_match = link["account_no"] if link else None
            if acct_of_match is None:
                link = conn.execute(
                    "SELECT v.account_no FROM match_links l JOIN vouchers v"
                    " ON v.id = l.record_id WHERE l.match_id=? AND l.side='voucher' LIMIT 1",
                    (m["id"],),
                ).fetchone()
                acct_of_match = link["account_no"] if link else None
            if acct_of_match == acct:
                tol += m["diff"]
        out.append(
            ReconStatement(
                period=period,
                account_no=acct,
                stmt_close=bal["stmt_close"] if bal else None,
                ledger_close=bal["ledger_close"] if bal else None,
                bank_in_unbooked=bu,
                bank_out_unbooked=bd,
                fin_in_pending=fi,
                fin_out_pending=fo,
                tolerance_total=tol,
                pending_suggestions=pending,
            )
        )
    return out
