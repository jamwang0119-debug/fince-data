"""配置加载：config/recon.toml，缺省时使用内置默认值。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from fince_recon.normalize import DEFAULT_BANK_DIRECTION, DEFAULT_VOUCHER_DIRECTION


@dataclass
class RoutingRule:
    reason: str
    label: str
    assignee: str
    action: str


DEFAULT_ROUTING = [
    RoutingRule("FEE_INTEREST", "手续费/利息未入账", "财务共享", "补记凭证"),
    RoutingRule("BANK_ONLY", "银行已收/付、财务未记", "财务共享", "补凭证"),
    RoutingRule("FIN_ONLY", "财务已记、银行未到", "资金/业务", "核实在途，跨月结转"),
    RoutingRule("MISSING_DOC", "漏记/缺单", "业务部门", "补提单据"),
    RoutingRule("AMOUNT_MISMATCH", "金额错记", "财务共享", "红冲调整"),
    RoutingRule("CODE_ERROR", "对账码错/缺", "业务部门", "修正对账码"),
    RoutingRule("DIRECTION_ERROR", "方向异常", "财务共享", "核实方向映射"),
]

FEE_KEYWORDS = ["手续费", "利息", "账户管理费", "工本费", "汇划费", "结息"]


@dataclass
class Config:
    db_path: str = "recon.db"
    tolerance_cents: int = 50
    subset_max_items: int = 8
    date_window_days: int = 5
    fuzzy_threshold: float = 0.85
    fee_keywords: list[str] = field(default_factory=lambda: list(FEE_KEYWORDS))
    bank_direction: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_BANK_DIRECTION)
    )
    voucher_direction: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_VOUCHER_DIRECTION)
    )
    routing: list[RoutingRule] = field(default_factory=lambda: list(DEFAULT_ROUTING))
    ai_enabled: bool = True
    ai_fuzzy_model: str = "claude-haiku-4-5-20251001"
    ai_attribution_model: str = "claude-sonnet-4-6"

    def routing_for(self, reason: str) -> RoutingRule:
        for r in self.routing:
            if r.reason == reason:
                return r
        return RoutingRule(reason, reason, "待定", "人工判断")


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    p = Path(path) if path else Path("config/recon.toml")
    if not p.is_file():
        return cfg
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    m = data.get("matching", {})
    cfg.db_path = data.get("db_path", cfg.db_path)
    cfg.tolerance_cents = int(m.get("tolerance_cents", cfg.tolerance_cents))
    cfg.subset_max_items = int(m.get("subset_max_items", cfg.subset_max_items))
    cfg.date_window_days = int(m.get("date_window_days", cfg.date_window_days))
    cfg.fuzzy_threshold = float(m.get("fuzzy_threshold", cfg.fuzzy_threshold))
    cfg.fee_keywords = m.get("fee_keywords", cfg.fee_keywords)
    dirs = data.get("directions", {})
    if "bank" in dirs:
        cfg.bank_direction = dict(dirs["bank"])
    if "voucher" in dirs:
        cfg.voucher_direction = dict(dirs["voucher"])
    if "routing" in data:
        cfg.routing = [
            RoutingRule(r["reason"], r["label"], r["assignee"], r["action"])
            for r in data["routing"]
        ]
    ai = data.get("ai", {})
    cfg.ai_enabled = bool(ai.get("enabled", cfg.ai_enabled))
    cfg.ai_fuzzy_model = ai.get("fuzzy_model", cfg.ai_fuzzy_model)
    cfg.ai_attribution_model = ai.get("attribution_model", cfg.ai_attribution_model)
    return cfg
