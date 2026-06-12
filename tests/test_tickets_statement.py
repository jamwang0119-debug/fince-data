"""工单状态机、跨月结转自动闭环、余额勾稽与调节表。"""

import pytest
from conftest import ACC, add_bank, add_voucher, tickets

from fince_recon.db import now
from fince_recon.engine import MatchEngine
from fince_recon.quality import check_balances
from fince_recon.statement import build_statements
from fince_recon.tickets import carryover, move_ticket


def run(conn, cfg, period="2026-05"):
    return MatchEngine(conn, cfg).run(period)


def open_period(conn, period):
    conn.execute(
        "INSERT INTO periods(period, status, opened_at) VALUES(?, 'open', ?)",
        (period, now()),
    )


def set_balances(conn, period, stmt_open, stmt_close, ledger_open, ledger_close):
    conn.execute(
        "INSERT INTO balances(period, account_no, stmt_open, stmt_close, ledger_open,"
        " ledger_close) VALUES(?,?,?,?,?,?)",
        (period, ACC, stmt_open, stmt_close, ledger_open, ledger_close),
    )


def test_state_machine(conn, cfg):
    add_bank(conn, 5000, direction="OUT", summary="手续费")
    run(conn, cfg)
    (t,) = tickets(conn)
    assert t["status"] == "待处理"
    move_ticket(conn, t["id"], "处理中", "张三")
    with pytest.raises(SystemExit):  # 非法跳转
        move_ticket(conn, t["id"], "已解决", "张三")
    move_ticket(conn, t["id"], "待复核", "张三")
    with pytest.raises(SystemExit):  # 退回必填原因
        move_ticket(conn, t["id"], "处理中", "复核人")
    move_ticket(conn, t["id"], "处理中", "复核人", "凭证号写错，退回")
    move_ticket(conn, t["id"], "待复核", "张三")
    move_ticket(conn, t["id"], "已解决", "复核人")
    events = conn.execute(
        "SELECT COUNT(*) c FROM ticket_events WHERE ticket_id=?", (t["id"],)
    ).fetchone()["c"]
    assert events >= 7  # 全程留痕


def test_carryover_and_autoclose(conn, cfg):
    # 5 月：财务已记、银行未到 → FIN_ONLY 工单
    v = add_voucher(conn, 2000000, direction="OUT", date="2026-05-31", code="RC10")
    run(conn, cfg)
    (t,) = tickets(conn)
    assert t["reason_code"] == "FIN_ONLY"
    # 结转至 6 月
    carried = carryover(conn, "2026-05", "2026-06", "张三")
    assert carried == [t["id"]]
    (t,) = tickets(conn)
    assert t["status"] == "已结转" and t["carried_to"] == "2026-06"
    # 6 月银行到账，复对成功 → 工单自动闭环并回溯标注
    open_period(conn, "2026-06")
    add_bank(conn, 2000000, direction="OUT", date="2026-06-02", code="RC10",
             period="2026-06")
    run(conn, cfg, "2026-06")
    (t,) = tickets(conn)
    assert t["status"] == "已解决"
    assert "2026-05" in t["resolved_note"] and "2026-06" in t["resolved_note"]
    # 6 月重跑不为已结转记录重复开单
    run(conn, cfg, "2026-06")
    assert len(tickets(conn)) == 1


def test_balance_check(conn, cfg):
    add_bank(conn, 100000, direction="IN")
    set_balances(conn, "2026-05", 0, 100000, 0, 0)
    results = {(r.account_no, r.side): r.ok for r in check_balances(conn, "2026-05")}
    assert results[(ACC, "bank")] is True
    assert results[(ACC, "ledger")] is True
    # 漏一笔流水 → 银行侧勾稽不平
    set_balances(conn, "2026-06", 100000, 250000, 0, 0)
    open_period(conn, "2026-06")
    results = {(r.account_no, r.side): r.ok for r in check_balances(conn, "2026-06")}
    assert results[(ACC, "bank")] is False


def test_statement_balances_with_tolerance(conn, cfg):
    # 容差核销 0.20：银行付 5000.00，凭证只记 4999.80
    add_bank(conn, 500000, direction="OUT", code="RC4")
    add_voucher(conn, 499980, direction="OUT", code="RC4")
    # 未达：银行手续费 50 元；财务在途付款 200 元
    add_bank(conn, 5000, direction="OUT", summary="手续费")
    add_voucher(conn, 20000, direction="OUT", date="2026-05-31")
    # 期初均 10000.00；期末按各自流水累计
    stmt_close = 1000000 - 500000 - 5000
    ledger_close = 1000000 - 499980 - 20000
    set_balances(conn, "2026-05", 1000000, stmt_close, 1000000, ledger_close)
    run(conn, cfg)
    (s,) = [s for s in build_statements(conn, "2026-05") if s.account_no == ACC]
    assert s.bank_out_unbooked == 5000      # 银行已付企业未付
    assert s.fin_out_pending == 20000       # 企业已付银行未付
    assert s.tolerance_total == -20         # 容差核销累计差额
    assert s.residual == 0 and s.balanced   # 调节表总闸门平衡
