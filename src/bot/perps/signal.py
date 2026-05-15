from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


SignalSide = Literal["long", "short", "flat"]
DecisionTimeframe = Literal["1m", "3m", "5m"]

_RATIONALE_CODE = re.compile(r"^[a-z0-9_:-]{2,48}$")


class NormalizedSignal(BaseModel):
    """Strict execution-facing signal contract."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    symbol: str
    timeframe: DecisionTimeframe
    side: SignalSide
    confidence: float = Field(ge=0.0, le=1.0)
    expected_move_bps: float = Field(ge=0.0)
    stop_bps: float = Field(ge=0.0)
    take_bps: float = Field(ge=0.0)
    max_hold_minutes: int = Field(gt=0, le=240)
    rationale_codes: list[str] = Field(default_factory=list, min_length=1, max_length=12)
    invalidation_reason: str = Field(min_length=1, max_length=300)
    model_latency_ms: int = Field(ge=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        out = value.upper().replace("-", "/")
        if out not in {"BTC/USDT", "ETH/USDT"}:
            raise ValueError("symbol must be BTC/USDT or ETH/USDT")
        return out

    @field_validator("rationale_codes")
    @classmethod
    def validate_codes(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            code = value.strip().lower().replace(" ", "_")
            if not _RATIONALE_CODE.match(code):
                raise ValueError(f"Invalid rationale code: {value!r}")
            normalized.append(code)
        return normalized

    @model_validator(mode="after")
    def validate_directional_fields(self) -> "NormalizedSignal":
        if self.side == "flat":
            return self
        missing = []
        if self.expected_move_bps <= 0:
            missing.append("expected_move_bps")
        if self.stop_bps <= 0:
            missing.append("stop_bps")
        if self.take_bps <= 0:
            missing.append("take_bps")
        if missing:
            raise ValueError(f"Directional signals require positive {', '.join(missing)}")
        return self


class NormalizedSignalBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signals: list[NormalizedSignal] = Field(min_length=1)


def validate_signal_payload(payload: dict[str, Any]) -> NormalizedSignal:
    try:
        return NormalizedSignal.model_validate(payload)
    except ValidationError:
        raise


def normalize_legacy_entry_decision(
    decision: Any,
    *,
    timeframe: str,
    model_latency_ms: int,
    default_max_hold_minutes: int,
) -> NormalizedSignal:
    """Convert the existing EntryDecision model into the execution contract.

    OPEN signals must include explicit stop/take fields. Missing directional
    economics are rejected instead of being silently defaulted.
    """

    data = decision.model_dump() if hasattr(decision, "model_dump") else dict(decision)
    action = str(data.get("action", "HOLD")).upper()
    if action == "OPEN_LONG":
        side: SignalSide = "long"
    elif action == "OPEN_SHORT":
        side = "short"
    else:
        side = "flat"

    stop_pct = data.get("stop_loss_pct")
    take_pct = data.get("take_profit_pct")
    expected_move_bps = data.get("expected_move_bps")
    if side != "flat":
        if stop_pct in (None, "") or take_pct in (None, ""):
            raise ValueError("OPEN signals require stop_loss_pct and take_profit_pct")
        if expected_move_bps in (None, ""):
            expected_move_bps = float(take_pct) * 100.0

    trend = str(data.get("market_trend") or "unknown").lower()
    action_code = side if side != "flat" else "hold"
    rationale_codes = data.get("rationale_codes") or [f"legacy:{action_code}", f"trend:{trend}"]

    return NormalizedSignal.model_validate(
        {
            "symbol": data.get("symbol"),
            "timeframe": timeframe,
            "side": side,
            "confidence": data.get("confidence", 0.0),
            "expected_move_bps": expected_move_bps or 0.0,
            "stop_bps": float(stop_pct or 0.0) * 100.0,
            "take_bps": float(take_pct or 0.0) * 100.0,
            "max_hold_minutes": int(data.get("max_hold_minutes") or default_max_hold_minutes),
            "rationale_codes": rationale_codes,
            "invalidation_reason": data.get("invalidation_reason") or data.get("reason") or "no setup",
            "model_latency_ms": model_latency_ms,
        }
    )
