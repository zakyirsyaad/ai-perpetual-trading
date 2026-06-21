from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


OKX_BTC_ETH_USDT_SWAP_FALLBACK_MAX_LEVERAGE = 100


class OKXPositionTierClient(Protocol):
    async def get_position_tiers(self, symbol: str, td_mode: str = "isolated") -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True, slots=True)
class LeverageResolution:
    symbols: tuple[str, ...]
    configured_leverage: int
    effective_max_leverage: int
    resolved_leverage: int
    symbol_max_leverage: dict[str, int]
    source: str
    was_capped: bool
    reason: str


async def resolve_okx_venue_leverage(
    client: OKXPositionTierClient,
    *,
    symbols: tuple[str, ...],
    configured_leverage: int,
    margin_mode: str = "isolated",
    fallback_max: int = OKX_BTC_ETH_USDT_SWAP_FALLBACK_MAX_LEVERAGE,
) -> LeverageResolution:
    """Resolve the venue leverage cap from OKX public position tiers.

    OKX can lower maximum leverage by symbol/tier. For startup safety this uses
    the lowest symbol max across configured symbols, then caps the configured
    leverage before the strategy builds position sizes.
    """

    try:
        symbol_caps: dict[str, int] = {}
        for symbol in symbols:
            tiers = await client.get_position_tiers(symbol, td_mode=margin_mode)
            max_leverage = _max_leverage_from_tiers(tiers)
            if max_leverage is None:
                raise ValueError(f"Missing maxLever in OKX position tiers for {symbol}")
            symbol_caps[symbol] = max_leverage
        effective_max = min(symbol_caps.values()) if symbol_caps else fallback_max
        source = "okx_position_tiers"
        reason = "Resolved from OKX public position tiers."
    except Exception as exc:
        symbol_caps = {symbol: fallback_max for symbol in symbols}
        effective_max = fallback_max
        source = "fallback"
        reason = f"Using {fallback_max}x fallback because OKX position tiers were unavailable: {exc}"

    resolved = min(int(configured_leverage), int(effective_max))
    return LeverageResolution(
        symbols=tuple(symbols),
        configured_leverage=int(configured_leverage),
        effective_max_leverage=int(effective_max),
        resolved_leverage=resolved,
        symbol_max_leverage=symbol_caps,
        source=source,
        was_capped=resolved < int(configured_leverage),
        reason=reason,
    )


def _max_leverage_from_tiers(tiers: list[dict[str, Any]]) -> int | None:
    values: list[int] = []
    for row in tiers:
        if not isinstance(row, dict):
            continue
        raw = row.get("maxLever") or row.get("maxLeverLong") or row.get("maxLeverShort")
        if raw in (None, ""):
            continue
        try:
            values.append(int(float(str(raw))))
        except (TypeError, ValueError):
            continue
    return max(values) if values else None
