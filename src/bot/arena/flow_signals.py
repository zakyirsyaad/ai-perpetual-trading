from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from bot.arena.factors import FactorSnapshot
from bot.data.market_signals import TechnicalFeatures

SignalDirection = Literal["LONG", "SHORT", "MIXED"]


@dataclass(frozen=True, slots=True)
class SignalRuleConfig:
    volume_spike_pct: float = 65.0
    volume_collapse_pct: float = -70.0
    price_momentum_bps: float = 55.0
    open_interest_surge_pct: float = 7.5
    funding_extreme_abs: float = 0.001
    factor_score_trigger: float = 0.75
    factor_percentile_trigger: float = 0.90
    order_flow_imbalance_pct: float = 25.0
    max_spread_bps: float = 6.0
    min_depth_notional: float = 25_000.0


@dataclass(frozen=True, slots=True)
class FlowSignal:
    symbol: str
    name: str
    direction: SignalDirection
    strength: float
    reason: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FlowSignalEvaluation:
    symbol: str
    active: list[FlowSignal]
    new_triggers: list[FlowSignal]

    @property
    def trigger_names(self) -> list[str]:
        return [item.name for item in self.active]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "active": [item.to_dict() for item in self.active],
            "new_triggers": [item.to_dict() for item in self.new_triggers],
            "trigger_names": self.trigger_names,
        }


@dataclass(slots=True)
class FactorDistributionTracker:
    """Small in-memory rolling distribution for factor percentile triggers."""

    max_samples: int = 500
    _scores: dict[tuple[str, str], list[float]] = field(default_factory=dict, init=False, repr=False)

    def add(self, symbol: str, factor: str, score: float, *, timestamp: Any | None = None) -> None:
        del timestamp
        key = (symbol, factor)
        values = self._scores.setdefault(key, [])
        values.append(_clamp(score))
        if len(values) > self.max_samples:
            del values[: len(values) - self.max_samples]

    def values(self, symbol: str, factor: str) -> list[float]:
        return list(self._scores.get((symbol, factor), []))

    def percentile(self, symbol: str, factor: str, score: float) -> float | None:
        values = self.values(symbol, factor)
        if not values:
            return None
        bounded = _clamp(score)
        empirical = sum(1 for item in values if item <= bounded) / len(values)
        low = min(values)
        high = max(values)
        if high == low:
            scaled = 0.5
        else:
            scaled = (bounded - low) / (high - low)
        return _clamp(max(empirical, scaled), 0.0, 1.0)

    def bands(self, symbol: str, factor: str) -> dict[str, float]:
        values = self.values(symbol, factor)
        return {
            "p5": _quantile(values, 0.05),
            "p50": _quantile(values, 0.50),
            "p95": _quantile(values, 0.95),
        }


class MarketFlowSignalDetector:
    """Edge-triggered market-flow detector for volume-building decisions."""

    def __init__(
        self,
        config: SignalRuleConfig | None = None,
        *,
        distribution_tracker: FactorDistributionTracker | None = None,
    ) -> None:
        self._config = config or SignalRuleConfig()
        self._distribution_tracker = distribution_tracker
        self._active_by_symbol: dict[str, set[str]] = {}

    def evaluate_symbol(
        self,
        features: TechnicalFeatures,
        factors: list[FactorSnapshot],
    ) -> FlowSignalEvaluation:
        active = self._active_conditions(features, factors)
        current_names = {item.name for item in active}
        previous_names = self._active_by_symbol.get(features.symbol, set())
        new_triggers = [item for item in active if item.name not in previous_names]
        self._active_by_symbol[features.symbol] = current_names
        return FlowSignalEvaluation(
            symbol=features.symbol,
            active=active,
            new_triggers=new_triggers,
        )

    def evaluate_many(
        self,
        technicals: list[TechnicalFeatures],
        factors: list[FactorSnapshot],
    ) -> dict[str, Any]:
        factors_by_symbol: dict[str, list[FactorSnapshot]] = {}
        for item in factors:
            factors_by_symbol.setdefault(item.symbol, []).append(item)
        evaluations = [
            self.evaluate_symbol(item, factors_by_symbol.get(item.symbol, []))
            for item in technicals
        ]
        return {
            "by_symbol": {item.symbol: item.to_dict() for item in evaluations},
            "active": [signal.to_dict() for item in evaluations for signal in item.active],
            "new_triggers": [signal.to_dict() for item in evaluations for signal in item.new_triggers],
            "trigger_names": sorted({signal.name for item in evaluations for signal in item.active}),
        }

    def _active_conditions(
        self,
        features: TechnicalFeatures,
        factors: list[FactorSnapshot],
    ) -> list[FlowSignal]:
        signals: list[FlowSignal] = []
        if features.volume_change_pct >= self._config.volume_spike_pct:
            signals.append(
                FlowSignal(
                    symbol=features.symbol,
                    name="volume_spike",
                    direction=_direction_from_value(features.change_pct_1),
                    strength=_clamp(features.volume_change_pct / 150.0, 0.0, 1.0),
                    reason=f"Volume expanded {features.volume_change_pct:.2f}%",
                    metadata={"volume_change_pct": features.volume_change_pct},
                )
            )
        if features.volume_change_pct <= self._config.volume_collapse_pct:
            signals.append(
                FlowSignal(
                    symbol=features.symbol,
                    name="volume_collapse",
                    direction="MIXED",
                    strength=_clamp(abs(features.volume_change_pct) / 150.0, 0.0, 1.0),
                    reason=f"Volume collapsed {features.volume_change_pct:.2f}%",
                    metadata={"volume_change_pct": features.volume_change_pct},
                )
            )
        if abs(features.change_pct_5) * 100.0 >= self._config.price_momentum_bps:
            signals.append(
                FlowSignal(
                    symbol=features.symbol,
                    name="price_momentum",
                    direction=_direction_from_value(features.change_pct_5),
                    strength=_clamp(abs(features.change_pct_5) / 1.5, 0.0, 1.0),
                    reason=f"5m price momentum {features.change_pct_5:.3f}%",
                    metadata={"change_pct_5": features.change_pct_5},
                )
            )
        if features.open_interest_change_pct >= self._config.open_interest_surge_pct:
            signals.append(
                FlowSignal(
                    symbol=features.symbol,
                    name="open_interest_surge",
                    direction=_direction_from_value(features.change_pct_5),
                    strength=_clamp(features.open_interest_change_pct / 20.0, 0.0, 1.0),
                    reason=f"Open interest rose {features.open_interest_change_pct:.2f}%",
                    metadata={"open_interest_change_pct": features.open_interest_change_pct},
                )
            )
        if abs(features.funding_rate) >= self._config.funding_extreme_abs:
            direction: SignalDirection = "SHORT" if features.funding_rate > 0 else "LONG"
            signals.append(
                FlowSignal(
                    symbol=features.symbol,
                    name="funding_extreme",
                    direction=direction,
                    strength=_clamp(abs(features.funding_rate) / 0.003, 0.0, 1.0),
                    reason=f"Funding rate extreme {features.funding_rate:.6f}",
                    metadata={"funding_rate": features.funding_rate},
                )
            )
        order_flow_signal = self._order_flow_imbalance_signal(features)
        if order_flow_signal is not None:
            signals.append(order_flow_signal)
        if self._liquidity_dislocated(features):
            signals.append(
                FlowSignal(
                    symbol=features.symbol,
                    name="liquidity_dislocation",
                    direction="MIXED",
                    strength=1.0,
                    reason="Spread or book depth failed arena liquidity quality threshold",
                    metadata={
                        "spread_bps": features.spread_bps,
                        "bid_depth_notional": features.bid_depth_notional,
                        "ask_depth_notional": features.ask_depth_notional,
                    },
                )
            )
        factor = max(factors, key=lambda item: abs(item.score), default=None)
        if factor is not None and abs(factor.score) >= self._config.factor_score_trigger:
            signals.append(
                FlowSignal(
                    symbol=features.symbol,
                    name="factor_score_breakout",
                    direction=factor.direction,
                    strength=_clamp(abs(factor.score), 0.0, 1.0),
                    reason=f"{factor.name} score {factor.score:.2f}",
                    metadata={"factor": factor.to_dict()},
                )
            )
        percentile_signal = self._factor_percentile_signal(features, factors)
        if percentile_signal is not None:
            signals.append(percentile_signal)
        return signals

    def _order_flow_imbalance_signal(self, features: TechnicalFeatures) -> FlowSignal | None:
        bid_depth = features.bid_depth_notional
        ask_depth = features.ask_depth_notional
        if bid_depth is None or ask_depth is None:
            return None
        total = bid_depth + ask_depth
        if total <= 0:
            return None
        imbalance_pct = ((bid_depth - ask_depth) / total) * 100.0
        if abs(imbalance_pct) < self._config.order_flow_imbalance_pct:
            return None
        return FlowSignal(
            symbol=features.symbol,
            name="order_flow_imbalance",
            direction="LONG" if imbalance_pct > 0 else "SHORT",
            strength=_clamp(abs(imbalance_pct) / 60.0, 0.0, 1.0),
            reason=f"Order book depth imbalance {imbalance_pct:.2f}%",
            metadata={
                "imbalance_pct": imbalance_pct,
                "bid_depth_notional": bid_depth,
                "ask_depth_notional": ask_depth,
            },
        )

    def _factor_percentile_signal(
        self,
        features: TechnicalFeatures,
        factors: list[FactorSnapshot],
    ) -> FlowSignal | None:
        if self._distribution_tracker is None:
            return None
        candidates = factors or [
            FactorSnapshot(
                symbol=features.symbol,
                name="price_momentum_5m",
                category="momentum",
                score=_clamp(features.change_pct_5 / 1.5),
                direction=_direction_from_value(features.change_pct_5),
                value=features.change_pct_5,
                reason=f"Derived from 5m price change {features.change_pct_5:.3f}%",
            )
        ]
        best: tuple[FactorSnapshot, float] | None = None
        for factor in candidates:
            percentile = self._distribution_tracker.percentile(factor.symbol, factor.name, factor.score)
            if percentile is None:
                continue
            edge = max(percentile, 1.0 - percentile)
            if best is None or edge > max(best[1], 1.0 - best[1]):
                best = (factor, percentile)
        if best is None:
            return None
        factor, percentile = best
        high = percentile >= self._config.factor_percentile_trigger
        low = percentile <= (1.0 - self._config.factor_percentile_trigger)
        if not high and not low:
            return None
        direction = factor.direction if factor.direction != "MIXED" else _direction_from_value(factor.score)
        return FlowSignal(
            symbol=features.symbol,
            name="factor_percentile_breakout",
            direction=direction,
            strength=_clamp(max(percentile, 1.0 - percentile), 0.0, 1.0),
            reason=f"{factor.name} percentile {percentile:.2f}",
            metadata={
                "factor": factor.to_dict(),
                "percentile": percentile,
                "bands": self._distribution_tracker.bands(factor.symbol, factor.name),
            },
        )

    def _liquidity_dislocated(self, features: TechnicalFeatures) -> bool:
        spread = features.spread_bps
        if spread is not None and spread > self._config.max_spread_bps:
            return True
        bid_depth = features.bid_depth_notional
        ask_depth = features.ask_depth_notional
        if bid_depth is None or ask_depth is None:
            return False
        return min(bid_depth, ask_depth) < self._config.min_depth_notional


def _direction_from_value(value: float) -> SignalDirection:
    if value > 0:
        return "LONG"
    if value < 0:
        return "SHORT"
    return "MIXED"


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _quantile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0.0, min(1.0, percentile)) * (len(ordered) - 1)
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return ordered[low]
    return ordered[low] + ((ordered[high] - ordered[low]) * (index - low))
