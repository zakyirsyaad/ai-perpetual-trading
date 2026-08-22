"""Event-driven AI exit policy.

Deterministic price, SL, TP, fee, liquidation, and time-stop evaluation still
runs on every five-second monitor iteration. This policy only decides whether
the *AI* exit evaluator is worth calling, which is what turns a one-hour
position from up to 720 large AI calls into at most a handful.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bot.models import PositionDirection

_INTERVAL_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


@dataclass(frozen=True, slots=True)
class ExitCallGate:
    allowed: bool
    reason: str


class AIExitPolicy:
    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._entry_price = 0.0
        self._direction = PositionDirection.LONG
        self._calls_made = 0
        self._deduplicated_calls = 0
        self._last_call_ts: float | None = None
        self._last_call_price: float | None = None
        self._last_candle_ms: int | None = None
        self._last_flow_signature: str | None = None
        self._seen_request_hashes: set[str] = set()

    # ------------------------------------------------------------------
    # per-trade lifecycle
    # ------------------------------------------------------------------
    def start_trade(self, *, entry_price: float, direction: PositionDirection, now: float) -> None:
        self._entry_price = float(entry_price)
        self._direction = direction
        self._calls_made = 0
        self._deduplicated_calls = 0
        self._last_call_ts = None
        self._last_call_price = None
        self._last_candle_ms = None
        self._last_flow_signature = None
        self._seen_request_hashes.clear()
        del now  # reserved for future time-based budgets

    @property
    def calls_made(self) -> int:
        return self._calls_made

    @property
    def deduplicated_calls(self) -> int:
        return self._deduplicated_calls

    def state(self) -> dict[str, Any]:
        return {
            "calls_made": self._calls_made,
            "max_calls": self._max_calls,
            "deduplicated_calls": self._deduplicated_calls,
            "budget_exhausted": self._calls_made >= self._max_calls,
        }

    # ------------------------------------------------------------------
    # gating
    # ------------------------------------------------------------------
    def evaluate(
        self,
        *,
        now: float,
        price: float,
        last_closed_candle_ms: int | None = None,
        flow_signature: str | None = None,
    ) -> ExitCallGate:
        if self._calls_made >= self._max_calls:
            # Budget exhausted: deterministic SL/TP/time protection stays active.
            return ExitCallGate(False, "exit_call_budget_exhausted")

        if self._last_call_ts is not None:
            elapsed = now - self._last_call_ts
            if elapsed < self._min_interval_seconds:
                return ExitCallGate(False, "min_interval")

        if last_closed_candle_ms is not None and last_closed_candle_ms != self._last_candle_ms:
            return ExitCallGate(True, "new_candle")

        if self._adverse_move_triggered(price):
            return ExitCallGate(True, "adverse_move")

        if flow_signature is not None and self._last_flow_signature is not None:
            if flow_signature != self._last_flow_signature:
                return ExitCallGate(True, "regime_or_flow_change")

        return ExitCallGate(False, "no_exit_event")

    def mark_called(
        self,
        *,
        now: float,
        price: float,
        last_closed_candle_ms: int | None = None,
        flow_signature: str | None = None,
        request_hash: str | None = None,
    ) -> None:
        self._calls_made += 1
        self._last_call_ts = now
        self._last_call_price = float(price)
        self._last_candle_ms = last_closed_candle_ms
        self._last_flow_signature = flow_signature
        if request_hash:
            self._seen_request_hashes.add(request_hash)

    def note_deduplicated(self) -> None:
        """An identical compact payload was skipped: no provider call, no budget spent."""
        self._deduplicated_calls += 1

    def is_duplicate_request(self, request_hash: str) -> bool:
        return request_hash in self._seen_request_hashes

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def flow_signature(market_context: dict[str, Any], symbol: str) -> str:
        intelligence = market_context.get("intelligence")
        regime = str((intelligence or {}).get("regime") or "unknown") if isinstance(intelligence, dict) else "unknown"
        signals = market_context.get("active_flow_signals")
        names: set[str] = set()
        if isinstance(signals, list):
            for item in signals:
                if isinstance(item, dict) and str(item.get("symbol") or "") == symbol:
                    names.add(str(item.get("name") or ""))
        return f"{regime}|{'|'.join(sorted(names))}"

    @staticmethod
    def last_closed_candle_ms(now_ms: int, interval: str) -> int:
        seconds = _INTERVAL_SECONDS.get(str(interval).lower(), 180)
        span = seconds * 1000
        return (int(now_ms) // span) * span

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _adverse_move_triggered(self, price: float) -> bool:
        threshold = float(getattr(self._settings, "ai_exit_price_move_threshold_pct", 0.0) or 0.0)
        if threshold <= 0:
            return False
        reference = self._last_call_price if self._last_call_price is not None else self._entry_price
        if not reference:
            return False
        change_pct = ((float(price) - reference) / reference) * 100.0
        adverse_pct = -change_pct if self._direction == PositionDirection.LONG else change_pct
        return adverse_pct >= threshold

    @property
    def _min_interval_seconds(self) -> float:
        value = getattr(self._settings, "ai_exit_check_interval_seconds", None)
        if value in (None, ""):
            return 30.0
        return max(float(value), 0.0)

    @property
    def _max_calls(self) -> int:
        # Explicit 0 must mean "no AI exit calls", not fall through to the default.
        value = getattr(self._settings, "ai_exit_max_calls_per_trade", None)
        if value in (None, ""):
            return 12
        return max(int(value), 0)
