from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class TechnicalFeatures:
    symbol: str
    last_price: float
    ema_fast: float
    ema_slow: float
    ema_21: float
    rsi_14: float
    atr_14: float
    macd_line: float
    macd_signal: float
    macd_hist: float
    change_pct_1: float
    change_pct_5: float
    volume: float
    volume_change_pct: float
    funding_rate: float
    open_interest: float
    open_interest_change_pct: float = 0.0
    spread_bps: float | None = None
    bid_depth_notional: float | None = None
    ask_depth_notional: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ExternalMarketSignals:
    coinmarketcap: dict[str, Any] = field(default_factory=dict)
    dune: dict[str, Any] = field(default_factory=dict)
    tradingview: dict[str, Any] = field(default_factory=dict)
    collected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "coinmarketcap": self.coinmarketcap,
            "dune": self.dune,
            "tradingview": self.tradingview,
            "collected_at": self.collected_at,
        }


@dataclass(slots=True)
class MarketContext:
    technicals: list[TechnicalFeatures]
    external_signals: ExternalMarketSignals
    intelligence: dict[str, Any]
    factor_snapshots: list[dict[str, Any]] = field(default_factory=list)
    active_flow_signals: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "technicals": [item.to_dict() for item in self.technicals],
            "external_signals": self.external_signals.to_dict(),
            "intelligence": self.intelligence,
            "factor_snapshots": self.factor_snapshots,
            "active_flow_signals": self.active_flow_signals,
        }
