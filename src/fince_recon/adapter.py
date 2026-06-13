"""字段映射适配层：把各种真实银企/资金系统导出转成标准中间表行。

设计目标：不改动已验证的匹配引擎，仅在导入前把真实文件按「映射配置」
（config/mappings.toml）翻译成工具的标准模板字段，再交给现有 importer。

映射配置支持真实数据里的几类形态：
  - 金额：单列+固定方向（付款/收款侧），或 收入/支出 双列；
  - 对账码：独立列（可识别 × 等空值标记），或正则从摘要/备注里提取（CC0006...）；
  - 唯一键：取某列，或对若干列做哈希（银行流水无单据号时）；
  - 日期：兼容 datetime 对象与带时分秒的字符串（由 normalize.parse_date 处理）。
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from fince_recon.normalize import find_code, parse_date

DEFAULT_CODE_REGEX = r"CC[0-9A-Z]{6,10}"
DEFAULT_EMPTY_MARKERS = ("×", "x", "X", "-", "无", "")


@dataclass
class Mapping:
    name: str
    side: str  # bank | voucher
    account_col: str
    date_col: str
    summary_col: str = ""
    memo_col: str = ""
    counterparty_name_col: str = ""
    counterparty_account_col: str = ""
    # 金额
    amount_mode: str = "single"  # single | in_out
    amount_col: str = ""
    direction_fixed: str = ""  # IN | OUT（single 模式）
    amount_in_col: str = ""
    amount_out_col: str = ""
    # 对账码
    code_col: str = ""
    code_from: list[str] = field(default_factory=list)
    code_regex: str = DEFAULT_CODE_REGEX
    empty_markers: list[str] = field(default_factory=lambda: list(DEFAULT_EMPTY_MARKERS))
    # 唯一键
    uid_col: str = ""
    uid_hash: list[str] = field(default_factory=list)
    # 凭证侧专用
    voucher_no_col: str = ""
    biz_no_col: str = ""
    ledger_col: str = ""
    # 账户主数据扫描用
    account_name_col: str = ""
    bank_name_col: str = ""
    ledger_default: str = "1002.01"


def load_mappings(path: str | None = None) -> dict[str, Mapping]:
    p = Path(path) if path else Path("config/mappings.toml")
    if not p.is_file():
        raise SystemExit(f"映射配置不存在: {p}（请用 --mapping-file 指定或创建 config/mappings.toml）")
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    out: dict[str, Mapping] = {}
    for name, spec in data.get("mappings", {}).items():
        out[name] = Mapping(name=name, **spec)
    return out


def _raw_rows(path: str) -> list[dict]:
    """读取真实文件为「列名→原始值」（保留 datetime/数字，不强制转字符串）。"""
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"文件不存在: {path}")
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise SystemExit("读取 Excel 需要安装 openpyxl: pip install openpyxl")
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(it)]
        rows = []
        for r in it:
            if not any(v not in (None, "") for v in r):
                continue
            rows.append({header[i]: (r[i] if i < len(r) else None) for i in range(len(header))})
        return rows
    # CSV
    import csv

    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SystemExit(f"无法识别文件编码: {path}")
    reader = csv.DictReader(text.splitlines())
    return [
        {(k or "").strip(): v for k, v in r.items()}
        for r in reader
        if any((v or "").strip() for v in r.values())
    ]


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _num(v) -> str:
    """金额原始值转可解析文本；空返回 ''。"""
    s = _s(v)
    return s if s and s not in ("0", "0.0") else ("" if not s else s)


def _resolve_code(m: Mapping, row: dict) -> str:
    if m.code_col:
        val = _s(row.get(m.code_col))
        if val and val not in m.empty_markers:
            return val
    if m.code_from:
        return find_code(m.code_regex, *[row.get(c) for c in m.code_from])
    return ""


def _resolve_uid(m: Mapping, row: dict) -> str:
    if m.uid_col:
        return _s(row.get(m.uid_col))
    if m.uid_hash:
        joined = "|".join(_s(row.get(c)) for c in m.uid_hash)
        return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]
    raise SystemExit(f"映射 {m.name} 未配置 uid_col 或 uid_hash，无法生成唯一键")


def _amount_direction(m: Mapping, row: dict) -> tuple[str, str] | None:
    """返回 (金额文本, 方向IN/OUT)；金额为空（无效行）返回 None。"""
    if m.amount_mode == "in_out":
        vin, vout = _num(row.get(m.amount_in_col)), _num(row.get(m.amount_out_col))
        if vin and vin not in ("0", "0.0"):
            return vin, "IN"
        if vout and vout not in ("0", "0.0"):
            return vout, "OUT"
        return None
    val = _num(row.get(m.amount_col))
    if not val or val in ("0", "0.0"):
        return None
    return val, m.direction_fixed


def adapt(path: str, m: Mapping) -> tuple[str, list[dict], int]:
    """把真实文件按映射转为 (kind, 标准模板行, 跳过行数)。kind ∈ {bank, vouchers}。"""
    out: list[dict] = []
    skipped = 0
    for row in _raw_rows(path):
        ad = _amount_direction(m, row)
        if ad is None:
            skipped += 1
            continue
        amount, direction = ad
        date_iso = parse_date(row.get(m.date_col))
        code = _resolve_code(m, row)
        uid = _resolve_uid(m, row)
        cp_name = _s(row.get(m.counterparty_name_col))
        summary = _s(row.get(m.summary_col))
        if m.side == "bank":
            out.append({
                "账号": _s(row.get(m.account_col)),
                "交易日期": date_iso,
                "入账日期": date_iso,
                "金额": amount,
                "方向": direction,
                "对手方户名": cp_name,
                "对手方账号": _s(row.get(m.counterparty_account_col)),
                "摘要": summary,
                "银行流水号": uid,
                "对账码": code,
            })
        else:
            out.append({
                "凭证号": uid,
                "分录行号": "1",
                "凭证日期": date_iso,
                "业务日期": date_iso,
                "科目编码": _s(row.get(m.ledger_col)),
                "银行账号": _s(row.get(m.account_col)),
                "金额": amount,
                "借贷方向": direction,
                "往来对象": cp_name,
                "摘要": summary,
                "关联业务单号": _s(row.get(m.voucher_no_col or m.biz_no_col)),
                "对账码": code,
                "凭证状态": "正常",
            })
    kind = "bank" if m.side == "bank" else "vouchers"
    return kind, out, skipped


def distinct_accounts(path: str, m: Mapping) -> list[dict]:
    """从真实文件扫描出现过的账户，生成账户主数据行（科目编码用默认值，可后续修正）。"""
    seen: dict[str, dict] = {}
    for row in _raw_rows(path):
        a = _s(row.get(m.account_col))
        if not a or a in seen:
            continue
        seen[a] = {
            "账号": a,
            "户名": _s(row.get(m.account_name_col)) or a,
            "开户行": _s(row.get(m.bank_name_col)),
            "科目编码": m.ledger_default,
            "币种": "CNY",
        }
    return list(seen.values())
