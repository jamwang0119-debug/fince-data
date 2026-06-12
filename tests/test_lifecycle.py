"""核销生命周期：确认/退回/反核销、期间管理、人工核销。"""

import pytest
from conftest import add_bank, add_voucher, bank_status, matches, voucher_status

from fince_recon.db import now
from fince_recon.engine import MatchEngine
from fince_recon.writeoff import confirm_match, manual_match, reject_match, unmatch


def run(conn, cfg, period="2026-05"):
    return MatchEngine(conn, cfg).run(period)


def _suggest(conn, cfg):
    b = add_bank(conn, 90000, cp="某公司")
    v1 = add_voucher(conn, 40000, cp="某公司")
    v2 = add_voucher(conn, 50000, cp="某公司")
    run(conn, cfg)
    (m,) = matches(conn, status="pending")
    return m["id"], b, (v1, v2)


def test_confirm_suggestion(conn, cfg):
    mid, b, vs = _suggest(conn, cfg)
    confirm_match(conn, mid, "张三")
    (m,) = matches(conn, id=mid)
    assert m["status"] == "confirmed" and m["confirmed_by"] == "张三"
    assert bank_status(conn, b) == "matched"


def test_reject_suggestion_releases_records(conn, cfg):
    mid, b, vs = _suggest(conn, cfg)
    reject_match(conn, mid, "张三", "不是同一笔业务")
    (m,) = matches(conn, id=mid)
    assert m["status"] == "cancelled"
    assert bank_status(conn, b) == "unmatched"
    for v in vs:
        assert voucher_status(conn, v) == "unmatched"


def test_unmatch_then_rematch(conn, cfg):
    b = add_bank(conn, 100000, code="RC1")
    v = add_voucher(conn, 100000, code="RC1")
    run(conn, cfg)
    (m,) = matches(conn)
    unmatch(conn, m["id"], "李四", "核销错误")
    (m,) = matches(conn, id=m["id"])
    assert m["status"] == "cancelled" and m["cancel_reason"] == "核销错误"
    assert bank_status(conn, b) == "unmatched"
    run(conn, cfg)  # 释放后可重新核销
    assert len(matches(conn, status="auto")) == 1
    assert bank_status(conn, b) == "matched"


def test_unmatch_requires_reason(conn, cfg):
    b = add_bank(conn, 100000, code="RC1")
    add_voucher(conn, 100000, code="RC1")
    run(conn, cfg)
    (m,) = matches(conn)
    with pytest.raises(SystemExit):
        unmatch(conn, m["id"], "李四", "")
    assert bank_status(conn, b) == "matched"


def test_closed_period_blocks_writes(conn, cfg):
    add_bank(conn, 100000, code="RC1")
    add_voucher(conn, 100000, code="RC1")
    run(conn, cfg)
    (m,) = matches(conn)
    conn.execute(
        "UPDATE periods SET status='closed', closed_at=? WHERE period='2026-05'", (now(),)
    )
    with pytest.raises(SystemExit):
        unmatch(conn, m["id"], "李四", "关账后尝试")
    with pytest.raises(SystemExit):
        MatchEngine(conn, cfg).run("2026-05")


def test_manual_match(conn, cfg):
    b = add_bank(conn, 88800)
    v = add_voucher(conn, 88800)
    mid = manual_match(conn, cfg, "2026-05", [b], [v], "王五", "线下核实同一笔")
    (m,) = matches(conn, id=mid)
    assert m["method"] == "manual" and m["status"] == "confirmed"
    # 已核销记录不可重复人工核销
    with pytest.raises(SystemExit):
        manual_match(conn, cfg, "2026-05", [b], [v], "王五")


def test_manual_match_diff_guard(conn, cfg):
    b = add_bank(conn, 88800)
    v = add_voucher(conn, 70000)
    with pytest.raises(SystemExit):
        manual_match(conn, cfg, "2026-05", [b], [v], "王五", "差太多")
    mid = manual_match(conn, cfg, "2026-05", [b], [v], "王五", "确认差额挂账", allow_diff=True)
    (m,) = matches(conn, id=mid)
    assert m["diff"] == 18800
