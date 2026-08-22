from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


DEFAULT_FALLBACK_MAX_LEVERAGE = 100
# Legacy alias kept so existing OKX call sites/tests keep importing one constant.
OKX_BTC_ETH_USDT_SWAP_FALLBACK_MAX_LEVERAGE = DEFAULT_FALLBACK_MAX_LEVERAGE


class PositionTierClient(Protocol):
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


OKXPositionTierClient = PositionTierClient


async def resolve_venue_leverage(
    client: PositionTierClient,
    *,
    symbols: tuple[str, ...],
    configured_leverage: int,
    margin_mode: str = "isolated",
    fallback_max: int = DEFAULT_FALLBACK_MAX_LEVERAGE,
    venue: str = "exchange",
) -> LeverageResolution:
    """Resolve the venue leverage cap from the exchange's position tiers.

    Venues can lower maximum leverage by symbol/tier. For startup safety this
    uses the lowest symbol max across configured symbols, then caps the
    configured leverage before the strategy builds position sizes.
    """

    try:
        symbol_caps: dict[str, int] = {}
        for symbol in symbols:
            tiers = await client.get_position_tiers(symbol, td_mode=margin_mode)
            max_leverage = _max_leverage_from_tiers(tiers)
            if max_leverage is None:
                raise ValueError(f"Missing maxLever in {venue} position tiers for {symbol}")
            symbol_caps[symbol] = max_leverage
        effective_max = min(symbol_caps.values()) if symbol_caps else fallback_max
        source = f"{venue}_position_tiers"
        reason = f"Resolved from {venue} public position tiers."
    except Exception as exc:
        symbol_caps = {symbol: fallback_max for symbol in symbols}
        effective_max = fallback_max
        source = "fallback"
        reason = f"Using {fallback_max}x fallback because {venue} position tiers were unavailable: {exc}"

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


async def resolve_okx_venue_leverage(
    client: PositionTierClient,
    *,
    symbols: tuple[str, ...],
    configured_leverage: int,
    margin_mode: str = "isolated",
    fallback_max: int = DEFAULT_FALLBACK_MAX_LEVERAGE,
) -> LeverageResolution:
    """Legacy OKX-named wrapper around `resolve_venue_leverage`."""
    return await resolve_venue_leverage(
        client,
        symbols=symbols,
        configured_leverage=configured_leverage,
        margin_mode=margin_mode,
        fallback_max=fallback_max,
        venue="okx",
    )
