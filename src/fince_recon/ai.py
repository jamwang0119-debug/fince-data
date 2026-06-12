"""AI Provider：可插拔接口 + 规则兜底。

配置 ANTHROPIC_API_KEY 且安装 anthropic 时真实调用 Claude；
否则（或单次调用失败时）自动降级到规则 Provider，任何环境都能跑通。
AI 输出仅作为说明与建议，不参与金额判定。
"""

from __future__ import annotations

import os
from typing import Protocol

from fince_recon.config import Config
from fince_recon.money import fmt


class AIProvider(Protocol):
    name: str

    def judge_fuzzy(self, bank_desc: str, voucher_desc: str, score: float) -> str:
        """第 4 层：对一组模糊匹配候选给出一句话归属判断说明。"""
        ...

    def attribute_diff(self, context: str, reason_label: str) -> str:
        """差异工单：生成一句话归因建议。"""
        ...


class RuleProvider:
    """规则兜底：模板化说明，无外部依赖。"""

    name = "rule"

    def judge_fuzzy(self, bank_desc: str, voucher_desc: str, score: float) -> str:
        return (
            f"户名相似度 {score:.2f}，金额方向一致，建议核销（规则判定，需人工确认）。"
            f"银行侧[{bank_desc}] ↔ 凭证侧[{voucher_desc}]"
        )

    def attribute_diff(self, context: str, reason_label: str) -> str:
        return f"系统初判为「{reason_label}」，请按建议动作处理。{context}"


class ClaudeProvider:
    """真实调用 Claude API；任何异常自动降级到规则 Provider。"""

    name = "claude"

    def __init__(self, cfg: Config):
        import anthropic

        self._client = anthropic.Anthropic()
        self._cfg = cfg
        self._fallback = RuleProvider()

    def _ask(self, model: str, prompt: str) -> str | None:
        try:
            resp = self._client.messages.create(
                model=model,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception:
            return None

    def judge_fuzzy(self, bank_desc: str, voucher_desc: str, score: float) -> str:
        prompt = (
            "你是资金对账助手。以下银行流水与财务凭证金额、方向已精确一致，"
            "仅户名/摘要存在差异。请用一句中文判断二者是否同一笔业务并说明依据，"
            "不超过50字。\n"
            f"银行侧: {bank_desc}\n凭证侧: {voucher_desc}\n字符串相似度: {score:.2f}"
        )
        return (
            self._ask(self._cfg.ai_fuzzy_model, prompt)
            or self._fallback.judge_fuzzy(bank_desc, voucher_desc, score)
        )

    def attribute_diff(self, context: str, reason_label: str) -> str:
        prompt = (
            "你是资金对账助手。针对以下对账差异，用一句中文给出归因建议"
            "（如『该差额疑似X月手续费未入账，建议财务补凭证』），不超过50字。\n"
            f"系统初判原因: {reason_label}\n差异上下文: {context}"
        )
        return (
            self._ask(self._cfg.ai_attribution_model, prompt)
            or self._fallback.attribute_diff(context, reason_label)
        )


def get_provider(cfg: Config) -> AIProvider:
    if cfg.ai_enabled and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return ClaudeProvider(cfg)
        except Exception:
            pass
    return RuleProvider()


def bank_desc(row) -> str:
    return (
        f"流水号{row['bank_txn_no']} {row['txn_date']} {fmt(row['amount'])}元"
        f" {row['direction']} 对手方:{row['counterparty_name'] or '-'}"
        f" 摘要:{row['summary'] or '-'}"
    )


def voucher_desc(row) -> str:
    return (
        f"凭证{row['voucher_no']}-{row['line_no']} {row['voucher_date']}"
        f" {fmt(row['amount'])}元 {row['direction']}"
        f" 往来:{row['counterparty'] or '-'} 摘要:{row['summary'] or '-'}"
    )
