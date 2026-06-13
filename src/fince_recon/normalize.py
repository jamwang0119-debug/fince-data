"""字段归一化：方向映射、日期、户名标准化与别名表。"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

# 公司名中不影响主体识别的修饰词，相似度比较前剔除
_COMPANY_NOISE = re.compile(
    r"(有限责任公司|股份有限公司|有限公司|集团|股份|公司|\(|\)|（|）)"
)

DIRECTION_IN = "IN"
DIRECTION_OUT = "OUT"

DEFAULT_BANK_DIRECTION = {"收": "IN", "付": "OUT", "IN": "IN", "OUT": "OUT"}
# 银行存款科目：借方=资金流入，贷方=资金流出
DEFAULT_VOUCHER_DIRECTION = {"借": "IN", "贷": "OUT", "IN": "IN", "OUT": "OUT"}


def map_direction(raw: str, mapping: dict[str, str]) -> str:
    key = str(raw).strip()
    if key not in mapping:
        raise ValueError(f"方向无法映射: {raw!r}（映射表: {sorted(mapping)}）")
    return mapping[key]


def parse_date(raw) -> str:
    """解析日期为 ISO 格式 YYYY-MM-DD。

    支持 datetime/date 对象、2026/6/1、20260601、2026-06-01，
    以及带时分秒的 2026-06-03 11:00:14 / 2026-06-03T11:00:14。
    """
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    s = str(raw).strip()
    # 去掉时间部分（空格或 T 分隔）
    s = re.split(r"[ T]", s, maxsplit=1)[0]
    s = s.replace("/", "-").replace(".", "-")
    if re.fullmatch(r"\d{8}", s):
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if not m:
        raise ValueError(f"日期无法解析: {raw!r}")
    y, mo, d = (int(g) for g in m.groups())
    return date(y, mo, d).isoformat()


def find_code(pattern: str, *values) -> str:
    """按优先级在多个文本中用正则提取对账码，返回第一个命中。"""
    rx = re.compile(pattern)
    for v in values:
        if v is None:
            continue
        m = rx.search(str(v))
        if m:
            return m.group(0)
    return ""


def norm_name(raw: str | None) -> str:
    """户名标准化：全角→半角、去空白、统一大小写。"""
    if not raw:
        return ""
    s = unicodedata.normalize("NFKC", str(raw))
    return re.sub(r"\s+", "", s).upper()


def name_core(norm: str) -> str:
    """剔除公司修饰词后的主体名，用于相似度比较。"""
    return _COMPANY_NOISE.sub("", norm)


def similarity(a: str, b: str) -> float:
    """户名相似度（0~1），基于主体名的 SequenceMatcher。"""
    import difflib

    ca, cb = name_core(a), name_core(b)
    if not ca or not cb:
        return 0.0
    if ca == cb or ca in cb or cb in ca:
        return 1.0
    return difflib.SequenceMatcher(None, ca, cb).ratio()


def days_between(d1: str, d2: str) -> int:
    return abs((date.fromisoformat(d1) - date.fromisoformat(d2)).days)
