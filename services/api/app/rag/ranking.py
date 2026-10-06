"""召回共用的正分 top-k：零匹配不占名额，边界同分按语料顺序取。"""

from __future__ import annotations

import numpy as np

RANKING_VERSION = "positive-stable-v1"


def positive_top_k(scores: np.ndarray, k: int) -> np.ndarray:
    """返回有限且严格正分的索引，不修改原分数。

    argpartition 本身不保证同分顺序，直接截断会任意选中边界同分项。
    先找第 k 大分数，再按原索引补齐同分项，最后只排序所选候选。
    保留 O(n) 的候选选择与 O(k log k) 的结果排序。
    """
    if k <= 0:
        return np.empty(0, dtype=np.intp)
    eligible = np.flatnonzero(np.isfinite(scores) & (scores > 0))
    if len(eligible) > k:
        values = scores[eligible]
        boundary = np.partition(values, len(values) - k)[len(values) - k]
        above = eligible[values > boundary]
        tied = eligible[values == boundary]
        eligible = np.concatenate((above, tied[: k - len(above)]))
    order = np.lexsort((eligible, -scores[eligible]))
    return eligible[order]
