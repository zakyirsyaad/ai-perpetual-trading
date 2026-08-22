from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from statistics import mean
from typing import Any, Literal

from bot.data.market_signals import TechnicalFeatures

FactorDirection = Literal["LONG", "SHORT", "MIXED"]


@dataclass(frozen=True, slots=True)
class FactorSnapshot:
    symbol: str
    name: str
    category: str
    score: float
    direction: FactorDirection
    value: float
    reason: str
    percentile: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FactorDefinition:
    name: str
    category: str
    source: str
    lookback: int
    description: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FactorEffectiveness:
    factor: str
    trade_count: int
    win_rate_pct: float
    average_pnl_usdt: float
    total_pnl_usdt: float
    forward_return_correlation: float
    ic: float = 0.0
    icir: float = 0.0
    decay_half_life_trades: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FactorRegistry:
    """Built-in factor library for the arena runtime."""

    def __init__(self) -> None:
        self._definitions = _build_factor_definitions()

    def definitions(self) -> list[FactorDefinition]:
        return list(self._definitions)

    def score_symbol(self, features: TechnicalFeatures) -> list[FactorSnapshot]:
        base = {
            "price_momentum_5m": self._price_momentum(features),
            "ema_macd_trend": self._ema_macd_trend(features),
            "atr_volatility_regime": self._volatility_regime(features),
            "volume_participation": self._volume_participation(features),
            "microstructure_pressure": self._microstructure_pressure(features),
        }
        snapshots = [
            base[definition.name]
            if definition.name in base
            else self._generic_factor(features, definition)
            for definition in self._definitions
        ]
        return [self._with_direction(item) for item in snapshots]

    def score_many(self, technicals: list[TechnicalFeatures]) -> list[FactorSnapshot]:
        snapshots: list[FactorSnapshot] = []
        for item in technicals:
            snapshots.extend(self.score_symbol(item))
        return self._with_percentiles(snapshots)

    def context_payload(self, technicals: list[TechnicalFeatures]) -> dict[str, Any]:
        snapshots = self.score_many(technicals)
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for item in snapshots:
            by_symbol.setdefault(item.symbol, []).append(item.to_dict())
        top = sorted(snapshots, key=lambda item: abs(item.score), reverse=True)[:8]
        return {
            "snapshots": [item.to_dict() for item in snapshots],
            "by_symbol": by_symbol,
            "top_factors": [item.name for item in top],
            "top_factor_details": [item.to_dict() for item in top],
        }

    def _price_momentum(self, features: TechnicalFeatures) -> FactorSnapshot:
        score = _clamp(features.change_pct_5 / 1.5)
        return FactorSnapshot(
            symbol=features.symbol,
            name="price_momentum_5m",
            category="momentum",
            score=score,
            direction="MIXED",
            value=features.change_pct_5,
            reason=f"5m price change {features.change_pct_5:.3f}%",
        )

    def _ema_macd_trend(self, features: TechnicalFeatures) -> FactorSnapshot:
        ema_gap_pct = 0.0
        if features.last_price > 0:
            ema_gap_pct = ((features.ema_fast - features.ema_slow) / features.last_price) * 100.0
        score = _clamp((ema_gap_pct * 18.0) + features.macd_hist)
        return FactorSnapshot(
            symbol=features.symbol,
            name="ema_macd_trend",
            category="trend",
            score=score,
            direction="MIXED",
            value=ema_gap_pct,
            reason=f"EMA gap {ema_gap_pct:.3f}% and MACD histogram {features.macd_hist:.4f}",
        )

    def _volatility_regime(self, features: TechnicalFeatures) -> FactorSnapshot:
        atr_pct = (features.atr_14 / features.last_price * 100.0) if features.last_price > 0 else 0.0
        sweet_spot_score = 1.0 - abs(atr_pct - 0.45) / 0.75
        score = _clamp(sweet_spot_score)
        return FactorSnapshot(
            symbol=features.symbol,
            name="atr_volatility_regime",
            category="volatility",
            score=score,
            direction="MIXED",
            value=atr_pct,
            reason=f"ATR volatility {atr_pct:.3f}% vs scalping sweet spot",
        )

    def _volume_participation(self, features: TechnicalFeatures) -> FactorSnapshot:
        score = _clamp(features.volume_change_pct / 120.0)
        return FactorSnapshot(
            symbol=features.symbol,
            name="volume_participation",
            category="volume",
            score=score,
            direction="MIXED",
            value=features.volume_change_pct,
            reason=f"Volume change {features.volume_change_pct:.2f}%",
        )

    def _microstructure_pressure(self, features: TechnicalFeatures) -> FactorSnapshot:
        spread_penalty = max(float(features.spread_bps or 0.0) / 12.0, 0.0)
        depth = min(float(features.bid_depth_notional or 0.0), float(features.ask_depth_notional or 0.0))
        depth_score = _clamp(depth / 200_000.0)
        oi_score = _clamp(features.open_interest_change_pct / 15.0)
        funding_penalty = _clamp(abs(features.funding_rate) / 0.0015)
        score = _clamp((depth_score * 0.35) + (oi_score * 0.45) - (funding_penalty * 0.25) - spread_penalty)
        return FactorSnapshot(
            symbol=features.symbol,
            name="microstructure_pressure",
            category="microstructure",
            score=score,
            direction="MIXED",
            value=features.open_interest_change_pct,
            reason=(
                f"OI change {features.open_interest_change_pct:.2f}%, "
                f"spread {float(features.spread_bps or 0.0):.2f}bps, depth {depth:.2f}"
            ),
        )

    def _generic_factor(self, features: TechnicalFeatures, definition: FactorDefinition) -> FactorSnapshot:
        raw = _generic_raw_value(features, definition)
        scale = _category_scale(definition.category)
        score = _clamp(raw / scale)
        return FactorSnapshot(
            symbol=features.symbol,
            name=definition.name,
            category=definition.category,
            score=score,
            direction="MIXED",
            value=raw,
            reason=f"{definition.description}; raw={raw:.6f}",
        )

    @staticmethod
    def _with_direction(snapshot: FactorSnapshot) -> FactorSnapshot:
        if snapshot.score >= 0.15:
            direction: FactorDirection = "LONG"
        elif snapshot.score <= -0.15:
            direction = "SHORT"
        else:
            direction = "MIXED"
        return FactorSnapshot(
            symbol=snapshot.symbol,
            name=snapshot.name,
            category=snapshot.category,
            score=snapshot.score,
            direction=direction,
            value=snapshot.value,
            reason=snapshot.reason,
            percentile=snapshot.percentile,
        )

    @staticmethod
    def _with_percentiles(snapshots: list[FactorSnapshot]) -> list[FactorSnapshot]:
        grouped: dict[str, list[FactorSnapshot]] = {}
        for item in snapshots:
            grouped.setdefault(item.name, []).append(item)
        percentile_by_key: dict[tuple[str, str], float] = {}
        for name, items in grouped.items():
            ordered = sorted(items, key=lambda item: item.score)
            denom = max(len(ordered) - 1, 1)
            for index, item in enumerate(ordered):
                percentile_by_key[(item.symbol, name)] = index / denom
        return [
            FactorSnapshot(
                symbol=item.symbol,
                name=item.name,
                category=item.category,
                score=item.score,
                direction=item.direction,
                value=item.value,
                reason=item.reason,
                percentile=percentile_by_key.get((item.symbol, item.name)),
            )
            for item in snapshots
        ]


class FactorEffectivenessAnalyzer:
    """Group realized trade performance by factor metadata."""

    def summarize(self, records: list[dict[str, Any]]) -> dict[str, FactorEffectiveness]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            for factor in _extract_factor_names(record.get("factor_metadata")):
                grouped.setdefault(factor, []).append(record)

        result: dict[str, FactorEffectiveness] = {}
        for factor, rows in grouped.items():
            pnls = [float(row.get("pnl_usdt") or 0.0) for row in rows]
            returns = [_forward_return(row) for row in rows]
            ordered_returns = _sort_returns_by_time(rows)
            wins = sum(1 for value in pnls if value > 0)
            count = len(rows)
            ic = _correlation(pnls, returns)
            result[factor] = FactorEffectiveness(
                factor=factor,
                trade_count=count,
                win_rate_pct=(wins / count * 100.0) if count else 0.0,
                average_pnl_usdt=mean(pnls) if pnls else 0.0,
                total_pnl_usdt=sum(pnls),
                forward_return_correlation=ic,
                ic=ic,
                icir=_icir(returns),
                decay_half_life_trades=_decay_half_life(ordered_returns),
            )
        return result


def _extract_factor_names(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    if isinstance(raw, dict):
        values = raw.get("top_factors") or raw.get("factor_names") or raw.get("factors") or []
        if isinstance(values, str):
            return [item.strip() for item in values.split(",") if item.strip()]
        if isinstance(values, list):
            return [str(item) for item in values if str(item).strip()]
    return []


def _forward_return(record: dict[str, Any]) -> float:
    entry = float(record.get("entry_price") or 0.0)
    exit_price = float(record.get("exit_price") or 0.0)
    if entry <= 0 or exit_price <= 0:
        return float(record.get("pnl_usdt") or 0.0)
    direction = str(record.get("direction") or "LONG").upper()
    raw = ((exit_price - entry) / entry) * 100.0
    return -raw if direction == "SHORT" else raw


def _sort_returns_by_time(records: list[dict[str, Any]]) -> list[float]:
    return [
        _forward_return(row)
        for row in sorted(records, key=lambda item: str(item.get("closed_at") or ""))
    ]


def _icir(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = mean(values)
    variance = sum((item - avg) ** 2 for item in values) / (len(values) - 1)
    std = math.sqrt(variance)
    if std <= 0:
        return 0.0
    return avg / std


def _decay_half_life(values: list[float]) -> float:
    if len(values) < 2:
        return 1.0 if values else 0.0
    abs_values = [abs(item) for item in values]
    first = max(abs_values[0], 1e-9)
    half = first / 2.0
    for index, value in enumerate(abs_values[1:], start=1):
        if value <= half:
            return float(index)
    return float(len(values))


def _correlation(left: list[float], right: list[float]) -> float:
    if len(left) < 2 or len(left) != len(right):
        return 0.0
    avg_left = mean(left)
    avg_right = mean(right)
    numerator = sum((x - avg_left) * (y - avg_right) for x, y in zip(left, right))
    denom_left = math.sqrt(sum((x - avg_left) ** 2 for x in left))
    denom_right = math.sqrt(sum((y - avg_right) ** 2 for y in right))
    if denom_left <= 0 or denom_right <= 0:
        return 0.0
    return numerator / (denom_left * denom_right)


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _build_factor_definitions() -> list[FactorDefinition]:
    definitions = [
        FactorDefinition("price_momentum_5m", "momentum", "exchange_candles", 5, "5m directional price momentum"),
        FactorDefinition("ema_macd_trend", "trend", "exchange_candles", 21, "EMA and MACD trend alignment"),
        FactorDefinition("atr_volatility_regime", "volatility", "exchange_candles", 14, "ATR volatility regime score"),
        FactorDefinition("volume_participation", "volume", "exchange_candles", 5, "Volume expansion or contraction"),
        FactorDefinition("microstructure_pressure", "microstructure", "exchange_orderbook", 1, "Spread, depth, funding, and OI pressure"),
    ]
    categories = {
        "momentum": [
            "rsi_impulse", "macd_impulse", "one_bar_reversal", "five_bar_reversal", "momentum_acceleration",
            "rsi_midline_distance", "trend_pullback_momentum", "breakout_velocity", "mean_reversion_pressure",
            "momentum_consistency", "short_term_squeeze", "intrabar_followthrough", "momentum_vol_adjusted",
            "directional_persistence", "fast_price_thrust", "slow_price_thrust", "momentum_exhaustion",
        ],
        "trend": [
            "ema_gap_fast", "ema_gap_slow", "ema_stack_quality", "macd_trend_confirmation", "trend_slope",
            "trend_stability", "price_vs_ema21", "trend_regime_strength", "bull_bear_bias", "trend_persistence",
            "trend_reversal_risk", "higher_low_pressure", "lower_high_pressure", "trend_vol_confirmation",
            "trend_quality_score", "moving_average_compression", "trend_expansion",
        ],
        "volatility": [
            "atr_percentile_proxy", "range_expansion", "range_compression", "volatility_breakout",
            "volatility_mean_reversion", "stop_noise_ratio", "tp_noise_ratio", "vol_adjusted_edge",
            "chaos_filter", "calm_market_score", "volatile_market_score", "liquidation_distance_proxy",
            "funding_volatility", "spread_volatility", "risk_window_quality", "noise_adjusted_momentum",
            "range_efficiency",
        ],
        "volume": [
            "volume_surge", "volume_drought", "volume_momentum_confirm", "volume_reversal_confirm",
            "volume_trend_alignment", "participation_quality", "volume_acceleration", "volume_exhaustion",
            "relative_volume_proxy", "volume_price_divergence", "volume_breakout_quality", "volume_pullback_quality",
            "volume_liquidity_score", "volume_risk_flag", "volume_followthrough", "volume_regime_shift",
            "volume_squeeze_release",
        ],
        "microstructure": [
            "spread_quality", "bid_depth_quality", "ask_depth_quality", "depth_imbalance", "funding_pressure",
            "open_interest_surge", "oi_price_confirmation", "crowding_risk", "short_squeeze_pressure",
            "long_squeeze_pressure", "book_liquidity_score", "execution_cost_pressure", "maker_fill_quality",
            "taker_cost_quality", "funding_carry_bias", "microstructure_edge", "depth_stability",
        ],
    }
    for category, names in categories.items():
        for index, name in enumerate(names, start=1):
            definitions.append(
                FactorDefinition(
                    name=name,
                    category=category,
                    source="exchange_live_features",
                    lookback=index,
                    description=f"{category} factor {name}",
                )
            )
    if len(definitions) != 90:
        raise RuntimeError(f"Unexpected generated factor count before trim: {len(definitions)}")
    return definitions[:86]


def _generic_raw_value(features: TechnicalFeatures, definition: FactorDefinition) -> float:
    category = definition.category
    index = max(definition.lookback, 1)
    if category == "momentum":
        return (features.change_pct_1 * 0.65) + (features.change_pct_5 * 0.35) + ((features.rsi_14 - 50.0) / (8.0 + index))
    if category == "trend":
        ema_gap = ((features.ema_fast - features.ema_slow) / features.last_price * 100.0) if features.last_price > 0 else 0.0
        return (ema_gap * (10.0 + index / 3.0)) + features.macd_hist
    if category == "volatility":
        atr_pct = (features.atr_14 / features.last_price * 100.0) if features.last_price > 0 else 0.0
        return 1.0 - abs(atr_pct - (0.25 + index * 0.02))
    if category == "volume":
        return features.volume_change_pct / (80.0 + index)
    depth = min(float(features.bid_depth_notional or 0.0), float(features.ask_depth_notional or 0.0))
    imbalance = _depth_imbalance(features)
    return (depth / (120_000.0 + index * 10_000.0)) + (features.open_interest_change_pct / 12.0) + imbalance - abs(features.funding_rate * 1000.0)


def _category_scale(category: str) -> float:
    return {
        "momentum": 2.5,
        "trend": 2.0,
        "volatility": 1.5,
        "volume": 1.2,
        "microstructure": 2.0,
    }.get(category, 1.0)


def _depth_imbalance(features: TechnicalFeatures) -> float:
    bid = float(features.bid_depth_notional or 0.0)
    ask = float(features.ask_depth_notional or 0.0)
    total = bid + ask
    if total <= 0:
        return 0.0
    return (bid - ask) / total
