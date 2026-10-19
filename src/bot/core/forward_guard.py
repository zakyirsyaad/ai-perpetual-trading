"""The LONG forward bot's own stop: a kill on the pooled result and a review stop at a position count.

Pure decision, no I/O. The engine feeds it the cycle totals read from the store on every tick, so the
latch needs no memory of its own and holds across a restart. Both limits are off at 0.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

PER_MILLION = 1_000_000.0


@dataclass(frozen=True, slots=True)
class CycleTotals:
    """Closed positions since the volume cycle started: how many, their net PnL and their traded volume."""

    count: int
    pnl_usdt: float
    volume_usdt: float

    def pnl_per_1m(self) -> Optional[float]:
        """Pooled net PnL per 1M of volume (sum of PnL over sum of volume); None with no volume."""
        if self.volume_usdt <= 0:
            return None
        return self.pnl_usdt / self.volume_usdt * PER_MILLION


def forward_guard_reason(
    totals: CycleTotals, kill_after: int, kill_below: float, review_at: int
) -> Optional[str]:
    """Why new entries must stop, or None while the forward test may go on.

    The kill needs `kill_after` positions and a pooled result strictly below `kill_below` per 1M; the
    review stop needs `review_at` positions whatever the result. The kill is checked first because it
    carries the more specific reason.
    """
    if kill_after > 0 and totals.count >= kill_after:
        per_1m = totals.pnl_per_1m()
        if per_1m is not None and per_1m < kill_below:
            return (
                f"forward_kill: {totals.count} positions at {per_1m:.0f} per 1M "
                f"(limit {kill_below:.0f})"
            )
    if review_at > 0 and totals.count >= review_at:
        return f"forward_review: {totals.count} positions reached, the manager decides"
    return None
