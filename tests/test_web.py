"""Web 后端 App：直接调方法（不起 HTTP 服务），覆盖关键接口。"""

from conftest import ACC, add_bank, add_voucher

from fince_recon.config import Config
from fince_recon.db import connect, now
from fince_recon.web import App


def setup_db(path):
    c = connect(str(path))
    c.execute("INSERT INTO accounts(account_no, account_name, ledger_account) VALUES(?,?,?)",
              (ACC, "测试户", "1002.01"))
    c.execute("INSERT INTO periods(period, status, opened_at) VALUES('2026-05','open',?)", (now(),))
    add_bank(c, 100000, code="RC1")
    add_voucher(c, 100000, code="RC1")
    add_bank(c, 5000, direction="OUT", summary="手续费")
    c.commit()
    c.close()


def test_web_run_and_summary(tmp_path):
    db = tmp_path / "w.db"
    setup_db(db)
    app = App(str(db), Config())
    st = app.state()
    assert any(p["period"] == "2026-05" for p in st["periods"])
    r = app.do("run", {"period": "2026-05"})
    assert "对账完成" in r["msg"]
    s = app.summary("2026-05")
    assert s["matched_bank"] == 1 and s["total_bank"] == 2
    assert s["open_tickets"] == 1  # 手续费 → 工单
    ms = app.matches("2026-05", None)
    assert any(m["status"] == "auto" for m in ms)


def test_web_ticket_move_and_error(tmp_path):
    db = tmp_path / "w.db"
    setup_db(db)
    app = App(str(db), Config())
    app.do("run", {"period": "2026-05"})
    tid = app.tickets("2026-05", None)[0]["id"]
    app.do("ticket_move", {"id": tid, "to": "处理中", "by": "张三"})
    assert app.tickets("2026-05", None)[0]["status"] == "处理中"
    # 非法流转抛错（SystemExit）
    import pytest
    with pytest.raises(SystemExit):
        app.do("ticket_move", {"id": tid, "to": "已解决", "by": "张三"})
    board = app.board("2026-05")
    assert any(b["status"] == "处理中" for b in board)


def test_web_confirm_flow(tmp_path):
    db = tmp_path / "w.db"
    setup_db(db)
    # 追加一组凑数（产生建议核销）
    c = connect(str(db))
    add_bank(c, 90000, cp="某公司")
    add_voucher(c, 40000, cp="某公司")
    add_voucher(c, 50000, cp="某公司")
    c.commit(); c.close()
    app = App(str(db), Config())
    app.do("run", {"period": "2026-05"})
    pend = [m for m in app.matches("2026-05", "pending")]
    assert pend, "应有建议核销"
    r = app.do("confirm", {"id": pend[0]["id"], "by": "李四"})
    assert "已确认" in r["msg"]
    assert not app.matches("2026-05", "pending")
