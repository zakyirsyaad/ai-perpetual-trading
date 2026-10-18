"""Mode A of the LONG module: the pullback LONG rule of spec section 2.3, as a small state machine.

The rule is checked at every closed trigger candle (15m):

1. Touch: the candle's low reaches or goes below the EMA20 (``low <= ema``).
2. Confirmation: within ``confirm_within`` candles of the touch, the touch candle included, a green
   candle (``close > open``) closes above the EMA20 (``close > ema``, strict).
3. Not too high: the confirmation close is at most ``max_distance_atr`` ATRs (1h) above the SMA20
   (1h), ``close <= sma20_1h + max_distance_atr * atr14_1h`` (inclusive).

A touch at candle ``i`` covers candles ``i`` to ``i + confirm_within - 1``. Two decisions of the
spec bind the tracker:

* D4: a candle is compared with the EMA20 passed with it, the one computed through its own close,
  whether it is the touch or the confirmation. Touches are tracked whatever the market condition;
  the caller checks the condition (NAIK) when a signal comes back.
* D5: a touch is used up only by an entry. A signal that the caller blocks (open position, pause,
  daily cap, volatility guard, weekly window) leaves the touch available, so a later confirmation
  inside the same window may still signal. A candle that is no signal (red, not above the EMA, or
  too high) leaves the touches alone as well. ``consume()`` drops every touch whose window holds
  the signal candle.

The caller sends every closed trigger candle in time order, one ``interval_ms`` apart, also while a
position is open and also those that have no EMA yet, because a window counts the candles
received: a candle that is missing would stretch it, so the tracker refuses the next one. When
``on_candle`` returns a signal and every gate of the caller passes, it opens the position and calls
``consume()``; when a gate blocks, it does not.

A ``None`` among ``ema``, ``sma20_1h`` and ``atr14_1h`` means that check cannot pass: without
``ema`` the candle is neither a touch nor a confirmation, and without ``sma20_1h`` or ``atr14_1h``
no signal comes back, though a touch is still recorded. A NaN fails the same way, because every
comparison with it is false.

Misuse raises instead of passing silently:

* ``ValueError`` for a constructor argument the rule cannot work with (a window under one candle,
  a cap that is not a finite number above 0, an interval that is not whole milliseconds above 0)
  and for a candle whose ``close_ms`` is not one interval after the previous one's: an older or
  repeated candle, or one after a missing candle. A rejected candle changes nothing.
* ``RuntimeError`` for ``consume()`` when the latest candle holds no unconsumed signal.

The module takes numbers only: no I/O, no bars and no indicator code, and no live module imports
it. Mode A is LONG-only here; the mirrored SHORT variant (D2) comes in a later phase.
"""

from dataclasses import dataclass
from typing import Optional

from bot.strategy.modes.checks import is_finite, is_whole

__all__ = ["Candle", "PullbackSignal", "PullbackTracker"]


@dataclass(frozen=True)
class Candle:
    """One closed trigger candle; ``close_ms`` is its close time in milliseconds.

    The rule reads ``close_ms``, ``open``, ``low`` and ``close``; ``high`` completes the candle."""

    close_ms: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class PullbackSignal:
    """The rule holds at the candle that closed at ``close_ms``: the signal time."""

    close_ms: int


class PullbackTracker:
    """Touch and confirmation state of Mode A over a stream of closed candles."""

    def __init__(self, confirm_within: int, max_distance_atr: float, interval_ms: int) -> None:
        if not is_whole(confirm_within):
            raise ValueError(f"confirm_within must be a whole number, got {confirm_within!r}")
        if confirm_within < 1:
            raise ValueError(f"confirm_within must be at least 1, got {confirm_within!r}")
        if not (is_finite(max_distance_atr) and max_distance_atr > 0):
            raise ValueError(
                f"max_distance_atr must be a finite number above 0, got {max_distance_atr!r}"
            )
        if not (is_whole(interval_ms) and interval_ms > 0):
            raise ValueError(
                f"interval_ms must be a whole number of milliseconds above 0, got {interval_ms!r}"
            )
        self._confirm_within = confirm_within
        self._max_distance_atr = max_distance_atr
        self._interval_ms = interval_ms
        self._candles_seen = 0
        self._last_close_ms: Optional[int] = None
        self._touches: list[int] = []  # indices of the touches whose window is still open
        self._unconsumed_signal = False  # the latest candle signalled and was not consumed

    def on_candle(
        self,
        candle: Candle,
        ema: Optional[float],
        sma20_1h: Optional[float],
        atr14_1h: Optional[float],
    ) -> Optional[PullbackSignal]:
        """Take the next closed candle with the indicator values at its close.

        Returns the signal when the candle confirms an open touch within the cap, else None."""
        if self._last_close_ms is not None and candle.close_ms <= self._last_close_ms:
            raise ValueError(
                f"candles must come in increasing close_ms, got {candle.close_ms} "
                f"after {self._last_close_ms}"
            )
        if self._last_close_ms is not None and (
            candle.close_ms != self._last_close_ms + self._interval_ms
        ):
            raise ValueError(
                f"a candle is missing: close_ms {candle.close_ms} is not one interval "
                f"({self._interval_ms} ms) after {self._last_close_ms}, and the window counts "
                "candles"
            )
        index = self._candles_seen
        self._candles_seen = index + 1
        self._last_close_ms = candle.close_ms
        self._unconsumed_signal = False

        open_touches = [i for i in self._touches if index <= i + self._confirm_within - 1]
        if ema is not None and candle.low <= ema:
            open_touches = [*open_touches, index]
        self._touches = open_touches

        if ema is None or sma20_1h is None or atr14_1h is None or not self._touches:
            return None
        green_above_ema = candle.close > candle.open and candle.close > ema
        within_cap = candle.close <= sma20_1h + self._max_distance_atr * atr14_1h
        if not (green_above_ema and within_cap):
            return None
        self._unconsumed_signal = True
        return PullbackSignal(close_ms=candle.close_ms)

    def consume(self) -> None:
        """The caller took the signal of the latest candle: drop every touch whose window holds it.

        Touches that ran out were already dropped when the candle arrived, and none can have been
        recorded after it, so every touch left is one whose window holds the signal candle."""
        if not self._unconsumed_signal:
            raise RuntimeError("consume() needs an unconsumed signal on the latest candle")
        self._touches = []
        self._unconsumed_signal = False
