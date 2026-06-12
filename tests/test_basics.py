import pytest

from fince_recon.money import fmt, parse_cents
from fince_recon.normalize import norm_name, parse_date, similarity
from fince_recon.subset import find_subset


def test_parse_cents():
    assert parse_cents("1,234.56") == 123456
    assert parse_cents("¥100") == 10000
    assert parse_cents("0.005") == 1  # 四舍五入到分
    assert parse_cents("１２３.４５") == 12345  # 全角
    assert parse_cents("-3.10") == -310
    with pytest.raises(ValueError):
        parse_cents("abc")


def test_fmt():
    assert fmt(123456) == "1234.56"
    assert fmt(-310) == "-3.10"
    assert fmt(5) == "0.05"


def test_parse_date():
    assert parse_date("2026/6/1") == "2026-06-01"
    assert parse_date("20260601") == "2026-06-01"
    assert parse_date("2026-06-01") == "2026-06-01"
    with pytest.raises(ValueError):
        parse_date("06-01")


def test_norm_name():
    assert norm_name("上海 智云科技（上海）") == "上海智云科技(上海)"
    assert norm_name(None) == ""


def test_similarity():
    assert similarity("上海智云科技有限公司", "上海智云科技股份有限公司") == 1.0
    assert similarity("上海智云科技有限公司", "北方重工集团有限公司") < 0.5


def test_find_subset():
    assert find_subset([40000, 30000, 20000], 90000, 8) == (0, 1, 2)
    assert find_subset([40000, 30000, 20000], 50000, 8) == (1, 2)
    assert find_subset([40000, 30000], 99999, 8) is None
    # 超过上限不搜索（防组合爆炸，转人工）
    assert find_subset(list(range(1, 10)), 45, 8) is None
