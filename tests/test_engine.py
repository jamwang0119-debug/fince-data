"""分层匹配引擎：第 1 层决策树、第 2/3/4 层、重跑保护。"""

from conftest import add_bank, add_voucher, bank_status, matches, tickets, voucher_status

from fince_recon.engine import MatchEngine


def run(conn, cfg, period="2026-05"):
    return MatchEngine(conn, cfg).run(period)


def test_layer1_code_1_1(conn, cfg):
    b = add_bank(conn, 100000, code="RC1")
    v = add_voucher(conn, 100000, code="RC1")
    run(conn, cfg)
    (m,) = matches(conn, method="code_1_1")
    assert m["confidence"] == "high" and m["status"] == "auto" and m["diff"] == 0
    assert bank_status(conn, b) == "matched" and voucher_status(conn, v) == "matched"


def test_layer1_group_1_n_and_m_n(conn, cfg):
    add_bank(conn, 60000, code="RC2")
    add_voucher(conn, 10000, code="RC2")
    add_voucher(conn, 50000, code="RC2")
    add_bank(conn, 10000, code="RC3")
    add_bank(conn, 20000, code="RC3")
    add_bank(conn, 30000, code="RC3")
    add_voucher(conn, 10000, code="RC3")
    add_voucher(conn, 50000, code="RC3")
    run(conn, cfg)
    ms = matches(conn, method="code_group")
    assert len(ms) == 2
    by_code = {m["recon_code"]: m for m in ms}
    assert by_code["RC2"]["confidence"] == "high"      # 1:N
    assert by_code["RC3"]["confidence"] == "medium"    # M:N 抽样复核


def test_layer1_tolerance(conn, cfg):
    add_bank(conn, 500000, direction="OUT", code="RC4")
    add_voucher(conn, 499980, direction="OUT", code="RC4")
    run(conn, cfg)
    (m,) = matches(conn, method="code_tolerance")
    assert m["diff"] == -20 and m["status"] == "auto" and m["confidence"] == "medium"
    assert "容差" in m["tolerance_note"]


def test_layer1_partial_and_leftover_ticket(conn, cfg):
    b1 = add_bank(conn, 50000, code="RC5")
    b2 = add_bank(conn, 30000, code="RC5")
    v1 = add_voucher(conn, 50000, code="RC5")
    run(conn, cfg)
    (m,) = matches(conn, method="code_partial")
    assert m["status"] == "auto"
    assert bank_status(conn, b1) == "matched" and voucher_status(conn, v1) == "matched"
    assert bank_status(conn, b2) == "unmatched"
    (t,) = [t for t in tickets(conn) if t["reason_code"] == "BANK_ONLY"]
    assert t["diff_amount"] == 30000 and t["status"] == "待处理"


def test_layer1_amount_mismatch_whole_group(conn, cfg):
    add_bank(conn, 70000, code="RC6")
    add_voucher(conn, 50000, code="RC6")
    run(conn, cfg)
    assert not matches(conn)
    (t,) = tickets(conn)
    assert t["reason_code"] == "AMOUNT_MISMATCH" and t["diff_amount"] == 20000
    assert t["assignee"] == "财务共享"


def test_layer1_direction_error(conn, cfg):
    add_bank(conn, 10000, direction="IN", code="RC7")
    add_bank(conn, 10000, direction="OUT", code="RC7")
    add_voucher(conn, 20000, direction="IN", code="RC7")
    run(conn, cfg)
    (t,) = tickets(conn)
    assert t["reason_code"] == "DIRECTION_ERROR"


def test_layer1_reversal_offsets(conn, cfg):
    add_bank(conn, 150000, code="RC8")
    add_voucher(conn, 180000, code="RC8")
    add_voucher(conn, 180000, code="RC8", reversal=True)
    add_voucher(conn, 150000, code="RC8")
    run(conn, cfg)
    (m,) = matches(conn)
    assert m["diff"] == 0 and m["status"] == "auto"


def test_layer2_exact_with_counterparty(conn, cfg):
    b = add_bank(conn, 123456, direction="OUT", cp="北京晨光商贸有限公司", date="2026-05-12")
    v = add_voucher(conn, 123456, direction="OUT", cp="北京晨光商贸有限公司", date="2026-05-13")
    run(conn, cfg)
    (m,) = matches(conn, method="exact")
    assert m["confidence"] == "high"
    assert bank_status(conn, b) == "matched" and voucher_status(conn, v) == "matched"


def test_layer2_rescues_code_typo(conn, cfg):
    add_bank(conn, 66600, code="RC9", cp="杭州云帆网络科技有限公司")
    add_voucher(conn, 66600, code="RC09", cp="杭州云帆网络科技有限公司")
    run(conn, cfg)
    (m,) = matches(conn, method="exact")
    assert "对账码不一致" in m["explanation"]


def test_layer2_passb_requires_unique(conn, cfg):
    # 同金额两笔银行对一张凭证：歧义，不允许 Pass B 瞎配
    add_bank(conn, 5000, cp="甲公司")
    add_bank(conn, 5000, cp="乙公司")
    add_voucher(conn, 5000, cp="丙公司")
    run(conn, cfg)
    assert not matches(conn, method="exact")


def test_layer3_subset_suggestion(conn, cfg):
    b = add_bank(conn, 90000, direction="OUT", cp="深圳鹏达")
    v1 = add_voucher(conn, 40000, direction="OUT", cp="深圳鹏达")
    v2 = add_voucher(conn, 30000, direction="OUT", cp="深圳鹏达")
    v3 = add_voucher(conn, 20000, direction="OUT", cp="深圳鹏达")
    run(conn, cfg)
    (m,) = matches(conn, method="subset_sum")
    assert m["status"] == "pending" and m["confidence"] == "suggested"
    assert bank_status(conn, b) == "suggested"
    for v in (v1, v2, v3):
        assert voucher_status(conn, v) == "suggested"
    # 建议核销不开工单
    assert not tickets(conn)


def test_layer4_fuzzy_disambiguation(conn, cfg):
    add_bank(conn, 250000, cp="上海智云科技有限公司")
    v1 = add_voucher(conn, 250000, cp="上海智云科技股份有限公司")
    v2 = add_voucher(conn, 250000, cp="北方重工集团有限公司")
    run(conn, cfg)
    (m,) = matches(conn, method="fuzzy")
    assert m["status"] == "pending"
    assert voucher_status(conn, v1) == "suggested"
    assert voucher_status(conn, v2) == "unmatched"
    (t,) = [t for t in tickets(conn) if t["reason_code"] == "FIN_ONLY"]


def test_fee_ticket_classification(conn, cfg):
    add_bank(conn, 5000, direction="OUT", summary="账户手续费")
    run(conn, cfg)
    (t,) = tickets(conn)
    assert t["reason_code"] == "FEE_INTEREST" and t["assignee"] == "财务共享"


def test_rerun_is_incremental(conn, cfg):
    add_bank(conn, 100000, code="RC1")
    add_voucher(conn, 100000, code="RC1")
    add_bank(conn, 7777, direction="OUT", summary="手续费")
    run(conn, cfg)
    n_matches = len(matches(conn))
    n_tickets = len(tickets(conn))
    run(conn, cfg)  # 重跑：已核销保留，不重复开单
    assert len(matches(conn)) == n_matches
    assert len(tickets(conn)) == n_tickets
