"""金额处理：一律以「分」为单位的整数存储，避免浮点误差产生假差异。"""

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation


def parse_cents(raw: str | int | float | Decimal) -> int:
    """解析金额文本为分（整数）。支持千分位逗号、货币符号、全角字符。"""
    if isinstance(raw, int):
        return raw * 100
    if isinstance(raw, Decimal):
        d = raw
    else:
        s = str(raw).strip()
        if not s:
            raise ValueError("金额为空")
        # 全角转半角，去货币符号与千分位
        s = s.translate(str.maketrans("０１２３４５６７８９．，－", "0123456789.,-"))
        s = s.replace(",", "").replace("¥", "").replace("￥", "").replace(" ", "")
        try:
            d = Decimal(s)
        except InvalidOperation as e:
            raise ValueError(f"金额无法解析: {raw!r}") from e
    return int(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def fmt(cents: int | None) -> str:
    """分 → 显示金额字符串。"""
    if cents is None:
        return "-"
    sign = "-" if cents < 0 else ""
    c = abs(cents)
    return f"{sign}{c // 100}.{c % 100:02d}"
