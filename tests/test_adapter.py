"""字段映射适配层：真实导出形态（双列金额/文本内对账码/datetime/哈希唯一键）。"""

from conftest import ACC, bank_status, matches, voucher_status

from fince_recon.adapter import Mapping, adapt
from fince_recon.engine import MatchEngine
from fince_recon.normalize import find_code, parse_date


def write_csv(path, header, rows):
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return str(path)


def test_parse_date_datetime_and_time():
    from datetime import datetime, date
    assert parse_date(datetime(2026, 6, 3, 11, 0, 14)) == "2026-06-03"
    assert parse_date(date(2026, 6, 3)) == "2026-06-03"
    assert parse_date("2026-06-03 11:00:14") == "2026-06-03"
    assert parse_date("2026/6/3T09:00") == "2026-06-03"


def test_find_code():
    assert find_code(r"CC[0-9A-Z]{6,10}", "资金池归集+CC0006XZZK") == "CC0006XZZK"
    assert find_code(r"CC[0-9A-Z]{6,10}", "×", "FSSC单-CC0006YL81") == "CC0006YL81"
    assert find_code(r"CC[0-9A-Z]{6,10}", "无关文本") == ""


def test_adapt_bank_in_out_and_code_fallback(tmp_path):
    # 银行侧：收入/支出双列；对账码列为 × 时回退到备注正则提取
    f = write_csv(
        tmp_path / "bank.csv",
        ["账户", "交易日期时间", "交易日期", "收入", "支出", "当前余额",
         "对方户名", "对方账户", "用途", "对账码", "备注"],
        [
            [ACC, "2026-06-03 11:00:14", "2026-06-03", "1000.00", "", "5000",
             "甲公司", "62200", "资金池归集", "CC0006AAAA", ""],
            [ACC, "2026-06-04 09:00:00", "2026-06-04", "", "200.00", "4800",
             "乙公司", "62201", "付款", "×", "付款备注+CC0006BBBB"],
        ],
    )
    m = Mapping(
        name="bank", side="bank", account_col="账户", date_col="交易日期",
        summary_col="用途", counterparty_name_col="对方户名",
        counterparty_account_col="对方账户",
        amount_mode="in_out", amount_in_col="收入", amount_out_col="支出",
        code_col="对账码", code_from=["对账码", "用途", "备注"],
        uid_hash=["账户", "交易日期时间", "收入", "支出", "当前余额"],
    )
    kind, rows, skipped = adapt(f, m)
    assert kind == "bank" and skipped == 0 and len(rows) == 2
    assert rows[0]["方向"] == "IN" and rows[0]["金额"] == "1000.00"
    assert rows[0]["对账码"] == "CC0006AAAA"
    assert rows[1]["方向"] == "OUT" and rows[1]["金额"] == "200.00"
    assert rows[1]["对账码"] == "CC0006BBBB"  # × 回退到备注提取
    assert rows[0]["银行流水号"] != rows[1]["银行流水号"]  # 哈希唯一键


def test_adapt_hash_uid_stable(tmp_path):
    header = ["账户", "交易日期时间", "交易日期", "收入", "支出", "当前余额", "对账码"]
    row = [ACC, "2026-06-03 11:00:14", "2026-06-03", "1000.00", "", "5000", "CC0006AAAA"]
    f1 = write_csv(tmp_path / "a.csv", header, [row])
    f2 = write_csv(tmp_path / "b.csv", header, [list(row)])
    m = Mapping(name="b", side="bank", account_col="账户", date_col="交易日期",
                amount_mode="in_out", amount_in_col="收入", amount_out_col="支出",
                code_col="对账码", uid_hash=["账户", "交易日期时间", "当前余额"])
    _, r1, _ = adapt(f1, m)
    _, r2, _ = adapt(f2, m)
    assert r1[0]["银行流水号"] == r2[0]["银行流水号"]  # 重跑稳定


def test_adapt_voucher_single_skip_empty(tmp_path):
    f = write_csv(
        tmp_path / "fund.csv",
        ["交易明细编号", "凭证号", "银行账号", "交易时间", "付款金额", "对方户名", "摘要"],
        [
            ["MX-001", "记-1", ACC, "2026-06-03 11:00:14", "300.00", "丙公司", "付款CC0006CCCC"],
            ["MX-002", "记-2", ACC, "2026-06-03 12:00:00", "", "丁公司", "空金额行应跳过"],
        ],
    )
    m = Mapping(
        name="fund", side="voucher", account_col="银行账号", date_col="交易时间",
        summary_col="摘要", counterparty_name_col="对方户名",
        amount_mode="single", amount_col="付款金额", direction_fixed="OUT",
        code_from=["摘要"], uid_col="交易明细编号", voucher_no_col="凭证号",
    )
    kind, rows, skipped = adapt(f, m)
    assert kind == "vouchers" and skipped == 1 and len(rows) == 1
    assert rows[0]["凭证号"] == "MX-001" and rows[0]["关联业务单号"] == "记-1"
    assert rows[0]["借贷方向"] == "OUT" and rows[0]["对账码"] == "CC0006CCCC"


def test_adapt_end_to_end_code_match(conn, cfg, tmp_path):
    """适配 → 导入 → 匹配：同一 CC 码两侧 1:1 精确核销。"""
    from fince_recon import importer

    bank_f = write_csv(
        tmp_path / "bank.csv",
        ["账户", "交易日期时间", "交易日期", "收入", "支出", "当前余额", "对账码", "对方户名"],
        [[ACC, "2026-05-10 10:00:00", "2026-05-10", "888.00", "", "9888", "CC0006ZZZZ", "客户A"]],
    )
    fund_f = write_csv(
        tmp_path / "fund.csv",
        ["交易明细编号", "凭证号", "银行账号", "交易时间", "收款金额", "对方户名", "摘要"],
        [["MX-9", "记-9", ACC, "2026-05-10 09:30:00", "888.00", "客户A", "收款 CC0006ZZZZ"]],
    )
    mb = Mapping(name="b", side="bank", account_col="账户", date_col="交易日期",
                 counterparty_name_col="对方户名", amount_mode="in_out",
                 amount_in_col="收入", amount_out_col="支出", code_col="对账码",
                 uid_hash=["账户", "交易日期时间", "当前余额"])
    mf = Mapping(name="f", side="voucher", account_col="银行账号", date_col="交易时间",
                 summary_col="摘要", counterparty_name_col="对方户名",
                 amount_mode="single", amount_col="收款金额", direction_fixed="IN",
                 code_from=["摘要"], uid_col="交易明细编号", voucher_no_col="凭证号")
    _, brows, _ = adapt(bank_f, mb)
    _, frows, _ = adapt(fund_f, mf)
    importer.import_bank(conn, bank_f, "2026-05", cfg, rows=brows)
    importer.import_vouchers(conn, fund_f, "2026-05", cfg, rows=frows)
    MatchEngine(conn, cfg).run("2026-05")
    (m,) = matches(conn, method="code_1_1")
    assert m["diff"] == 0 and m["status"] == "auto"
    b = conn.execute("SELECT id FROM bank_txns").fetchone()["id"]
    v = conn.execute("SELECT id FROM vouchers").fetchone()["id"]
    assert bank_status(conn, b) == "matched" and voucher_status(conn, v) == "matched"
