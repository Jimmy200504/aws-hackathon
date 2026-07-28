from __future__ import annotations

from collections import defaultdict
from typing import Iterable


def estimate_rank_propensity(
    impressions: Iterable[tuple[int, int]],
    max_rank: int = 100,
    alpha: float = 2.0,
    beta: float = 20.0,
) -> dict[int, float]:
    """Estimate smoothed examination propensity relative to rank 1.

    Each item is (exposure_rank, clicked). This is a pragmatic baseline; the
    production notebook compares it with a position-based click model.
    """
    totals: dict[int, int] = defaultdict(int)
    clicks: dict[int, int] = defaultdict(int)
    for rank, clicked in impressions:
        if 1 <= rank <= max_rank:
            totals[rank] += 1
            clicks[rank] += int(bool(clicked))
    ctr = {
        rank: (clicks[rank] + alpha) / (totals[rank] + alpha + beta)
        for rank in range(1, max_rank + 1)
    }
    anchor = max(ctr.get(1, 0.0), 1e-6)
    propensity: dict[int, float] = {}
    previous = 1.0
    for rank in range(1, max_rank + 1):
        value = min(previous, max(0.02, min(1.0, ctr[rank] / anchor)))
        propensity[rank] = value
        previous = value
    return propensity


def ips_weight(rank: int, propensity: dict[int, float], clip: float = 10.0) -> float:
    return min(clip, 1.0 / max(propensity.get(rank, 0.02), 0.02))
