"""有上限的 subset-sum：在候选笔数受限的前提下找出和为目标值的子集。

用于第 1 层组内下钻（部分核销）与第 3 层凑数匹配。
候选笔数超过上限直接放弃（防组合爆炸，转人工），返回 None。
"""

from __future__ import annotations

from itertools import combinations
from typing import Sequence


def find_subset(
    values: Sequence[int], target: int, max_items: int
) -> tuple[int, ...] | None:
    """在 values 中找和等于 target 的子集，返回索引元组；优先笔数最少的解。

    候选超过 max_items 笔时不搜索（审计口径：超限转人工），返回 None。
    """
    n = len(values)
    if n == 0 or n > max_items or target == 0:
        return None
    indices = range(n)
    for size in range(1, n + 1):
        for combo in combinations(indices, size):
            if sum(values[i] for i in combo) == target:
                return combo
    return None
