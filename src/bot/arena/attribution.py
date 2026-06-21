from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class MetricBucket:
    key: str
    trade_count: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl_usdt: float = 0.0

    @property
    def win_rate_pct(self) -> float:
        return (self.wins / self.trade_count * 100.0) if self.trade_count else 0.0

    @property
    def average_pnl_usdt(self) -> float:
        return self.total_pnl_usdt / self.trade_count if self.trade_count else 0.0

    def add(self, pnl: float) -> None:
        self.trade_count += 1
        self.total_pnl_usdt += pnl
        if pnl > 0:
            self.wins += 1
        elif pnl < 0:
            self.losses += 1

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["win_rate_pct"] = self.win_rate_pct
        data["average_pnl_usdt"] = self.average_pnl_usdt
        return data


@dataclass(frozen=True, slots=True)
class AttributionSummary:
    total_trades: int
    total_pnl_usdt: float
    by_symbol: dict[str, MetricBucket] = field(default_factory=dict)
    by_direction: dict[str, MetricBucket] = field(default_factory=dict)
    by_factor: dict[str, MetricBucket] = field(default_factory=dict)
    by_trigger: dict[str, MetricBucket] = field(default_factory=dict)
    by_hour: dict[str, MetricBucket] = field(default_factory=dict)
    by_exit_reason: dict[str, MetricBucket] = field(default_factory=dict)
    by_confidence_band: dict[str, MetricBucket] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "total_pnl_usdt": self.total_pnl_usdt,
            "by_symbol": _bucket_dict(self.by_symbol),
            "by_direction": _bucket_dict(self.by_direction),
            "by_factor": _bucket_dict(self.by_factor),
            "by_trigger": _bucket_dict(self.by_trigger),
            "by_hour": _bucket_dict(self.by_hour),
            "by_exit_reason": _bucket_dict(self.by_exit_reason),
            "by_confidence_band": _bucket_dict(self.by_confidence_band),
        }


@dataclass(frozen=True, slots=True)
class DiagnosisCard:
    type: str
    title: str
    severity: str
    metrics: dict[str, Any]
    description: str
    suggestion: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class VolumeProgress:
    daily_volume_usdt: float
    target_volume_usdt: float
    remaining_volume_usdt: float
    estimated_fee_spent_usdt: float
    projected_days_remaining: float
    neutral_exit_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AttributionAnalyzer:
    def summarize(self, records: list[dict[str, Any]]) -> AttributionSummary:
        groups = {
            "by_symbol": {},
            "by_direction": {},
            "by_factor": {},
            "by_trigger": {},
            "by_hour": {},
            "by_exit_reason": {},
            "by_confidence_band": {},
        }
        total_pnl = 0.0
        for record in records:
            pnl = float(record.get("pnl_usdt") or 0.0)
            total_pnl += pnl
            _add(groups["by_symbol"], str(record.get("symbol") or "unknown"), pnl)
            _add(groups["by_direction"], str(record.get("direction") or "unknown"), pnl)
            _add(groups["by_exit_reason"], str(record.get("reason") or "unknown"), pnl)
            _add(groups["by_hour"], _closed_hour(record.get("closed_at")), pnl)
            _add(groups["by_confidence_band"], _confidence_band(record.get("confidence")), pnl)
            for factor in _metadata_values(record.get("factor_metadata"), "top_factors"):
                _add(groups["by_factor"], factor, pnl)
            for trigger in _metadata_values(record.get("trigger_metadata"), "trigger_names"):
                _add(groups["by_trigger"], trigger, pnl)
        return AttributionSummary(
            total_trades=len(records),
            total_pnl_usdt=total_pnl,
            by_symbol=groups["by_symbol"],
            by_direction=groups["by_direction"],
            by_factor=groups["by_factor"],
            by_trigger=groups["by_trigger"],
            by_hour=groups["by_hour"],
            by_exit_reason=groups["by_exit_reason"],
            by_confidence_band=groups["by_confidence_band"],
        )

    def volume_progress(
        self,
        records: list[dict[str, Any]],
        *,
        target_volume_usdt: float,
        taker_fee_pct: float,
        target_daily_volume_usdt: float,
    ) -> VolumeProgress:
        volume = sum(
            (float(row.get("entry_price") or 0.0) * float(row.get("quantity") or 0.0))
            + (float(row.get("exit_price") or 0.0) * float(row.get("quantity") or 0.0))
            for row in records
        )
        remaining = max(0.0, target_volume_usdt - volume)
        projected_days = remaining / target_daily_volume_usdt if target_daily_volume_usdt > 0 else 0.0
        return VolumeProgress(
            daily_volume_usdt=volume,
            target_volume_usdt=target_volume_usdt,
            remaining_volume_usdt=remaining,
            estimated_fee_spent_usdt=volume * (taker_fee_pct / 100.0),
            projected_days_remaining=projected_days,
            neutral_exit_count=sum(1 for row in records if row.get("reason") == "neutral_exit"),
        )


class DiagnosisGenerator:
    def generate(self, summary: AttributionSummary) -> list[DiagnosisCard]:
        cards: list[DiagnosisCard] = []
        for symbol, bucket in sorted(summary.by_symbol.items(), key=lambda item: item[1].total_pnl_usdt):
            if bucket.trade_count and (bucket.total_pnl_usdt < 0 or bucket.win_rate_pct < 35.0):
                cards.append(
                    DiagnosisCard(
                        type="problem",
                        title=f"Weak performance on {symbol}",
                        severity="high" if bucket.total_pnl_usdt < 0 else "medium",
                        metrics=bucket.to_dict(),
                        description=(
                            f"{symbol} produced {bucket.total_pnl_usdt:.2f} USDT PnL "
                            f"with {bucket.win_rate_pct:.1f}% win rate across {bucket.trade_count} trades."
                        ),
                        suggestion=f"Reduce or block {symbol} entries until factor and trigger attribution improves.",
                    )
                )
        for trigger, bucket in sorted(summary.by_trigger.items(), key=lambda item: item[1].total_pnl_usdt):
            if bucket.trade_count >= 2 and bucket.total_pnl_usdt < 0:
                cards.append(
                    DiagnosisCard(
                        type="problem",
                        title=f"Trigger underperforming: {trigger}",
                        severity="medium",
                        metrics=bucket.to_dict(),
                        description=(
                            f"Trigger {trigger} has negative realized PnL "
                            f"({bucket.total_pnl_usdt:.2f} USDT)."
                        ),
                        suggestion=f"Raise the threshold or require a confirming factor before trading {trigger}.",
                    )
                )
        if not cards and summary.total_trades:
            cards.append(
                DiagnosisCard(
                    type="observation",
                    title="No major attribution problem detected",
                    severity="low",
                    metrics={"total_trades": summary.total_trades, "total_pnl_usdt": summary.total_pnl_usdt},
                    description="The current sample does not show a clearly bad symbol or trigger bucket.",
                    suggestion="Keep collecting trades before changing thresholds.",
                )
            )
        return cards


def _bucket_dict(values: dict[str, MetricBucket]) -> dict[str, dict[str, Any]]:
    return {key: bucket.to_dict() for key, bucket in values.items()}


def _add(target: dict[str, MetricBucket], key: str, pnl: float) -> None:
    target.setdefault(key, MetricBucket(key=key)).add(pnl)


def _metadata_values(raw: Any, key: str) -> list[str]:
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
        values = raw.get(key) or raw.get("names") or []
        if isinstance(values, str):
            return [item.strip() for item in values.split(",") if item.strip()]
        if isinstance(values, list):
            return [str(item) for item in values if str(item).strip()]
    return []


def _closed_hour(raw: Any) -> str:
    if not raw:
        return "unknown"
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).strftime("%H")
    except ValueError:
        return "unknown"


def _confidence_band(raw: Any) -> str:
    try:
        value = max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        value = 0.0
    if value < 0.4:
        return "0.00-0.40"
    if value < 0.6:
        return "0.40-0.60"
    if value < 0.8:
        return "0.60-0.80"
    return "0.80-1.00"
