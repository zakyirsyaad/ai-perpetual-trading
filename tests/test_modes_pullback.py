"""Mode A of the LONG module: the pullback tracker, with one entry per touch.

Every candle below is shaped against an EMA20 of 100, an SMA20 (1h) of 100 and an ATR14 (1h) of 2,
so with a cap of one ATR the highest close that may still enter is 102.
"""

import dataclasses
import math
import random
import unittest

from bot.strategy.modes.pullback import Candle, PullbackSignal, PullbackTracker

STEP_MS = 15 * 60 * 1000
EMA, SMA, ATR = 100.0, 100.0, 2.0
CEILING = SMA + 1.0 * ATR  # 102.0: the highest close that is not too high


def candle(n, open_, close, low):
    """The n-th 15m candle; its high is the larger of open and close."""
    return Candle(
        close_ms=(n + 1) * STEP_MS, open=open_, high=max(open_, close), low=low, close=close
    )


def touch_green(n):
    """Reaches the EMA and closes green above it: a touch and a confirmation in one candle."""
    return candle(n, 99.5, 101.0, 99.0)


def touch_red(n):
    """Reaches the EMA and closes above it, but red: a touch that cannot confirm."""
    return candle(n, 101.0, 100.5, 99.0)


def red_above(n):
    """Stays above the EMA and closes red: neither a touch nor a confirmation."""
    return candle(n, 101.5, 101.0, 100.5)


def green_above(n):
    """Stays above the EMA and closes green below the ceiling: it confirms an open touch."""
    return candle(n, 100.5, 101.5, 100.5)


def doji_above(n):
    """Stays above the EMA and closes where it opened: not green."""
    return candle(n, 101.0, 101.0, 100.5)


def wick_above(n):
    """Reaches the EMA and closes green below it, with a high above it: open < close < EMA < high.
    Only its wick clears the EMA."""
    return Candle(close_ms=(n + 1) * STEP_MS, open=99.0, high=101.5, low=98.8, close=99.6)


def red_wick_above(n):
    """Stays above the EMA and closes red, with a high above its open: red despite the wick."""
    return Candle(close_ms=(n + 1) * STEP_MS, open=101.0, high=101.8, low=100.5, close=100.8)


def tall_at_cap(n):
    """Stays above the EMA and closes green exactly on the ceiling, with a high far above it."""
    return Candle(close_ms=(n + 1) * STEP_MS, open=100.5, high=CEILING + 10 * ATR, low=100.5,
                  close=CEILING)


def new_tracker(confirm_within=3, max_distance_atr=1.0):
    return PullbackTracker(
        confirm_within=confirm_within, max_distance_atr=max_distance_atr, interval_ms=STEP_MS
    )


def feed(tracker, candles):
    """Send each candle with the reference indicator values; one result per candle."""
    return [tracker.on_candle(c, EMA, SMA, ATR) for c in candles]


def fired(results):
    """The positions of ``results`` that hold a signal."""
    return [i for i, result in enumerate(results) if result is not None]


class RuleTests(unittest.TestCase):
    def test_touch_and_confirmation_in_one_candle(self):
        first = touch_green(0)
        result = new_tracker().on_candle(first, EMA, SMA, ATR)
        self.assertEqual(result, PullbackSignal(close_ms=first.close_ms))

    def test_confirmation_on_the_third_candle_but_not_the_fourth(self):
        third = feed(new_tracker(), [touch_red(0), red_above(1), green_above(2)])
        self.assertEqual(fired(third), [2])
        fourth = feed(new_tracker(), [touch_red(0), red_above(1), red_above(2), green_above(3)])
        self.assertEqual(fired(fourth), [])

    def test_low_equal_to_ema_is_a_touch_and_close_equal_to_ema_is_not_a_confirmation(self):
        # a low exactly on the EMA touches it, a low one tick above does not
        on_ema = candle(0, 101.0, 100.5, EMA)
        one_tick_above = candle(0, 101.0, 100.5, math.nextafter(EMA, math.inf))
        self.assertEqual(fired(feed(new_tracker(), [on_ema, green_above(1)])), [1])
        self.assertEqual(fired(feed(new_tracker(), [one_tick_above, green_above(1)])), [])
        # a green close exactly on the EMA confirms nothing, and the touch stays for the next one
        closes_on_ema = candle(0, 99.5, EMA, 99.0)
        self.assertEqual(fired(feed(new_tracker(), [closes_on_ema])), [])
        self.assertEqual(fired(feed(new_tracker(), [closes_on_ema, green_above(1)])), [1])

    def test_red_candle_never_confirms(self):
        # touch_red closes above the EMA, and so do the others: only the colour holds them back
        self.assertEqual(fired(feed(new_tracker(), [touch_red(0)])), [])
        self.assertEqual(fired(feed(new_tracker(), [touch_red(0), red_above(1)])), [])
        # open == close is not green either
        doji = [touch_red(0), red_above(1), doji_above(2)]
        self.assertEqual(fired(feed(new_tracker(), doji)), [])
        # the same window does confirm on a green candle, so the touch was open all along
        green = [touch_red(0), red_above(1), green_above(2)]
        self.assertEqual(fired(feed(new_tracker(), green)), [2])

    def test_distance_cap_is_inclusive(self):
        at_cap = candle(0, 99.5, CEILING, 99.0)
        just_above = candle(0, 99.5, math.nextafter(CEILING, math.inf), 99.0)
        self.assertEqual(fired(feed(new_tracker(), [at_cap])), [0])
        self.assertEqual(fired(feed(new_tracker(), [just_above])), [])

    def test_the_close_and_not_the_high_must_clear_the_ema(self):
        # after an open touch: a green candle above the EMA by its wick alone confirms nothing,
        # and the window stays open for a close above the EMA
        tracker = new_tracker()
        feed(tracker, [touch_red(0)])
        self.assertIsNone(tracker.on_candle(wick_above(1), EMA, SMA, ATR))
        self.assertIsNotNone(tracker.on_candle(green_above(2), EMA, SMA, ATR))
        # as its own touch: no signal either, though it is a touch the next candle confirms
        tracker = new_tracker()
        self.assertIsNone(tracker.on_candle(wick_above(0), EMA, SMA, ATR))
        self.assertIsNotNone(tracker.on_candle(green_above(1), EMA, SMA, ATR))
        # and the colour is the close against the open: a high above the open is no green candle
        self.assertEqual(fired(feed(new_tracker(), [touch_red(0), red_wick_above(1)])), [])

    def test_the_distance_cap_holds_the_close_and_not_the_high(self):
        # a close exactly on the cap still enters, however far above the cap the high went
        tracker = new_tracker()
        feed(tracker, [touch_red(0)])
        self.assertIsNotNone(tracker.on_candle(tall_at_cap(1), EMA, SMA, ATR))

    def test_one_entry_per_touch(self):
        tracker = new_tracker()
        self.assertEqual(fired(feed(tracker, [touch_green(0)])), [0])
        tracker.consume()
        # the touch is used up: green closes above the EMA inside its window give nothing
        self.assertEqual(fired(feed(tracker, [green_above(1), green_above(2)])), [])
        # a new touch is a new chance
        self.assertEqual(fired(feed(tracker, [touch_green(3)])), [0])

    def test_blocked_signal_leaves_the_touch_available(self):
        # nobody calls consume(): a gate of the caller blocked every signal
        candles = [touch_green(0), green_above(1), green_above(2), green_above(3)]
        self.assertEqual(fired(feed(new_tracker(), candles)), [0, 1, 2])  # window ends at the 3rd

    def test_missing_inputs_never_signal(self):
        full = {"ema": EMA, "sma20_1h": SMA, "atr14_1h": ATR}
        self.assertIsNotNone(new_tracker().on_candle(touch_green(0), **full))  # the base signals
        for name in full:
            with self.subTest(missing=name):
                self.assertIsNone(new_tracker().on_candle(touch_green(0), **{**full, name: None}))
        # also on a confirming candle that follows a recorded touch
        for name in full:
            with self.subTest(missing_on_later_candle=name):
                tracker = new_tracker()
                feed(tracker, [touch_red(0)])
                inputs = {**full, name: None}
                self.assertIsNone(tracker.on_candle(green_above(1), **inputs))
        # with a cap far above any close, a missing input must not be read as zero either
        lenient = {"ema": EMA, "sma20_1h": 1000.0, "atr14_1h": 1000.0}
        self.assertIsNotNone(new_tracker().on_candle(touch_green(0), **lenient))
        for name in lenient:
            with self.subTest(lenient_missing=name):
                inputs = {**lenient, name: None}
                self.assertIsNone(new_tracker().on_candle(touch_green(0), **inputs))


class WindowTests(unittest.TestCase):
    def test_the_window_is_confirm_within_candles_counting_the_touch_candle(self):
        # one candle: only the touch candle itself can confirm
        self.assertEqual(fired(feed(new_tracker(confirm_within=1), [touch_green(0)])), [0])
        late = [touch_red(0), green_above(1)]
        self.assertEqual(fired(feed(new_tracker(confirm_within=1), late)), [])
        for width in (2, 3, 5):
            with self.subTest(confirm_within=width):
                reds = [red_above(n) for n in range(1, width - 1)]
                last_inside = [touch_red(0)] + reds + [green_above(width - 1)]
                first_outside = [touch_red(0)] + reds + [red_above(width - 1), green_above(width)]
                self.assertEqual(fired(feed(new_tracker(width), last_inside)), [width - 1])
                self.assertEqual(fired(feed(new_tracker(width), first_outside)), [])

    def test_a_new_touch_after_an_unconfirmed_window_opens_a_new_window(self):
        # the window of the touch at 0 ends after candle 2; the touch at 3 opens its own
        candles = [touch_red(0), red_above(1), red_above(2), touch_red(3), green_above(4)]
        self.assertEqual(fired(feed(new_tracker(), candles)), [4])

    def test_each_candle_is_compared_with_the_ema_passed_with_it(self):
        # D4: the touch is at an EMA of 100, but a later close is judged against its own EMA, and
        # both closes are under the cap (102), so the EMA alone decides
        rising = new_tracker()
        rising.on_candle(touch_red(0), EMA, SMA, ATR)
        above_the_old_ema = candle(1, 101.0, 101.8, 100.8)
        self.assertIsNone(rising.on_candle(above_the_old_ema, 103.0, SMA, ATR))
        falling = new_tracker()
        falling.on_candle(touch_red(0), EMA, SMA, ATR)
        below_the_old_ema = candle(1, 99.0, 99.5, 98.5)
        self.assertIsNotNone(falling.on_candle(below_the_old_ema, 98.0, SMA, ATR))

    def test_a_touch_is_judged_on_the_ema_passed_with_its_own_candle(self):
        # D4 on the touch side, with no earlier touch open: candle 0 stays above an EMA of 100
        rising = new_tracker()
        self.assertIsNone(rising.on_candle(red_above(0), EMA, SMA, ATR))
        # the EMA rose to 101: a low of 100.5, above the old EMA and below the new one, touches
        # it, and the green close at 101.5 above the new EMA confirms in the same candle
        dipped_to_the_new_ema = candle(1, 100.8, 101.5, 100.5)
        self.assertIsNotNone(rising.on_candle(dipped_to_the_new_ema, 101.0, SMA, ATR))
        # the EMA fell to 98: a low of 99, below the old EMA and above the new one, is no touch
        above_the_new_ema = candle(1, 99.5, 100.5, 99.0)
        falling = new_tracker()
        self.assertIsNone(falling.on_candle(red_above(0), EMA, SMA, ATR))
        self.assertIsNone(falling.on_candle(above_the_new_ema, 98.0, SMA, ATR))
        # the same candle confirms a touch that is open: only the touch was missing
        confirmed = new_tracker()
        confirmed.on_candle(touch_red(0), EMA, SMA, ATR)
        self.assertIsNotNone(confirmed.on_candle(above_the_new_ema, 98.0, SMA, ATR))

    def test_a_too_high_close_is_no_entry_and_leaves_the_touch_available(self):
        too_high = candle(0, 99.5, math.nextafter(CEILING, math.inf), 99.0)
        self.assertEqual(fired(feed(new_tracker(), [too_high, green_above(1)])), [1])

    def test_the_cap_is_the_given_multiple_of_the_hourly_atr_above_the_hourly_sma(self):
        cases = (  # sma20_1h, atr14_1h, multiple, highest close that enters
            (100.0, 2.0, 0.5, 101.0),
            (100.0, 2.0, 2.0, 104.0),
            (100.0, 4.0, 1.0, 104.0),
            (105.0, 2.0, 1.0, 107.0),
        )
        for sma, atr, multiple, ceiling in cases:
            with self.subTest(sma=sma, atr=atr, multiple=multiple):
                at_cap = candle(0, 99.5, ceiling, 99.0)
                above = candle(0, 99.5, math.nextafter(ceiling, math.inf), 99.0)
                tracker = new_tracker(max_distance_atr=multiple)
                self.assertIsNotNone(tracker.on_candle(at_cap, EMA, sma, atr))
                tracker = new_tracker(max_distance_atr=multiple)
                self.assertIsNone(tracker.on_candle(above, EMA, sma, atr))


class ConsumptionTests(unittest.TestCase):
    def test_an_entry_consumes_every_touch_whose_window_holds_the_signal_candle(self):
        # touches at 0 and 1; the signal is candle 1, inside the window of the touch at 0 as well
        candles = [touch_red(0), touch_green(1)]
        tracker = new_tracker()
        self.assertEqual(fired(feed(tracker, candles)), [1])
        tracker.consume()
        self.assertEqual(fired(feed(tracker, [green_above(2), green_above(3)])), [])
        # not consumed, both touches stand: candle 2 sees both, candle 3 only the one at 1
        blocked = feed(new_tracker(), candles + [green_above(2), green_above(3), green_above(4)])
        self.assertEqual(fired(blocked), [1, 2, 3])

    def test_a_blocked_touch_expires_on_its_own_and_a_later_touch_enters_separately(self):
        # the signal at 0 is blocked and never consumed; the touch at 0 runs out after candle 2,
        # and the touch at 2 gives its own entry at 3, which the caller takes
        tracker = new_tracker()
        self.assertEqual(fired(feed(tracker, [touch_green(0), red_above(1), touch_red(2)])), [0])
        self.assertIsNotNone(tracker.on_candle(green_above(3), EMA, SMA, ATR))
        tracker.consume()
        self.assertEqual(fired(feed(tracker, [green_above(4)])), [])


class MissingInputTests(unittest.TestCase):
    def test_a_candle_without_ema_is_never_a_touch(self):
        tracker = new_tracker()
        self.assertIsNone(tracker.on_candle(touch_green(0), None, SMA, ATR))
        # had it been recorded as a touch, this green close above the EMA would confirm it
        self.assertIsNone(tracker.on_candle(green_above(1), EMA, SMA, ATR))

    def test_a_touch_is_recorded_while_the_distance_inputs_are_missing(self):
        for name in ("sma20_1h", "atr14_1h"):
            with self.subTest(missing=name):
                tracker = new_tracker()
                inputs = {"ema": EMA, "sma20_1h": SMA, "atr14_1h": ATR, name: None}
                self.assertIsNone(tracker.on_candle(touch_green(0), **inputs))
                # the touch stands, so the next green close above the EMA is a confirmation
                self.assertIsNotNone(tracker.on_candle(green_above(1), EMA, SMA, ATR))

    def test_a_candle_with_missing_inputs_still_counts_in_the_window(self):
        full = (EMA, SMA, ATR)
        tracker = new_tracker()
        feed(tracker, [touch_red(0)])
        self.assertIsNone(tracker.on_candle(red_above(1), None, SMA, ATR))
        self.assertIsNotNone(tracker.on_candle(green_above(2), *full))  # the third candle
        tracker = new_tracker()
        feed(tracker, [touch_red(0)])
        self.assertIsNone(tracker.on_candle(red_above(1), None, SMA, ATR))
        self.assertIsNone(tracker.on_candle(red_above(2), EMA, None, ATR))
        self.assertIsNone(tracker.on_candle(green_above(3), *full))  # the fourth: expired

    def test_nan_inputs_fail_closed_like_missing_ones(self):
        nan = float("nan")
        for name in ("ema", "sma20_1h", "atr14_1h"):
            with self.subTest(nan=name):
                inputs = {"ema": EMA, "sma20_1h": SMA, "atr14_1h": ATR, name: nan}
                self.assertIsNone(new_tracker().on_candle(touch_green(0), **inputs))
        # a NaN EMA records no touch
        tracker = new_tracker()
        tracker.on_candle(touch_green(0), nan, SMA, ATR)
        self.assertIsNone(tracker.on_candle(green_above(1), EMA, SMA, ATR))


class MisuseTests(unittest.TestCase):
    def test_consume_before_any_candle_raises(self):
        with self.assertRaisesRegex(RuntimeError, "consume"):
            new_tracker().consume()

    def test_consume_after_a_candle_without_signal_raises_and_changes_nothing(self):
        tracker = new_tracker()
        feed(tracker, [touch_red(0)])
        with self.assertRaisesRegex(RuntimeError, "consume"):
            tracker.consume()
        self.assertEqual(fired(feed(tracker, [green_above(1)])), [0])  # the touch is still open

    def test_consume_twice_raises(self):
        tracker = new_tracker()
        feed(tracker, [touch_green(0)])
        tracker.consume()
        with self.assertRaisesRegex(RuntimeError, "consume"):
            tracker.consume()

    def test_a_signal_of_an_earlier_candle_cannot_be_consumed_any_more(self):
        tracker = new_tracker()
        self.assertEqual(fired(feed(tracker, [touch_green(0), red_above(1)])), [0])  # blocked
        with self.assertRaisesRegex(RuntimeError, "consume"):
            tracker.consume()
        self.assertEqual(fired(feed(tracker, [green_above(2)])), [0])  # the touch survived

    def test_candles_must_come_in_increasing_time(self):
        first = touch_red(5)
        for label, later in (("the same candle again", first), ("an older candle", touch_red(4))):
            with self.subTest(label):
                tracker = new_tracker()
                tracker.on_candle(first, EMA, SMA, ATR)
                with self.assertRaisesRegex(ValueError, "close_ms"):
                    tracker.on_candle(later, EMA, SMA, ATR)

    def test_a_missing_candle_is_refused_instead_of_widening_the_window(self):
        # The window counts candles, so a hole would stretch three candles past 45 minutes.
        tracker = new_tracker()
        feed(tracker, [touch_red(0), red_above(1)])
        with self.assertRaisesRegex(ValueError, "missing"):
            tracker.on_candle(green_above(3), EMA, SMA, ATR)  # candle 2 never came
        # the rejected candle changed nothing: candle 2 still confirms the touch
        self.assertIsNotNone(tracker.on_candle(green_above(2), EMA, SMA, ATR))

    def test_the_interval_must_be_a_whole_number_of_milliseconds_above_zero(self):
        for bad in (0, -STEP_MS, float(STEP_MS), True, str(STEP_MS), None):
            with self.subTest(interval_ms=bad), self.assertRaisesRegex(ValueError, "interval_ms"):
                PullbackTracker(confirm_within=3, max_distance_atr=1.0, interval_ms=bad)

    def test_the_first_candle_may_have_any_close_time(self):
        zero = Candle(close_ms=0, open=99.5, high=101.0, low=99.0, close=101.0)
        self.assertIsNotNone(new_tracker().on_candle(zero, EMA, SMA, ATR))

    def test_a_rejected_candle_changes_nothing(self):
        # it does not use up a place in the window ...
        tracker = new_tracker()
        feed(tracker, [touch_red(0)])
        with self.assertRaises(ValueError):
            tracker.on_candle(touch_red(0), EMA, SMA, ATR)
        feed(tracker, [red_above(1)])
        self.assertIsNotNone(tracker.on_candle(green_above(2), EMA, SMA, ATR))  # still the third
        # ... and it does not take the pending signal away
        tracker = new_tracker()
        feed(tracker, [touch_green(0)])
        with self.assertRaises(ValueError):
            tracker.on_candle(touch_green(0), EMA, SMA, ATR)
        tracker.consume()  # still allowed: candle 0 is the latest accepted one
        # ... and it does not move the clock back: a candle between the two stays refused
        tracker = new_tracker()
        feed(tracker, [red_above(5)])
        for older in (3, 4):
            with self.subTest(older=older):
                with self.assertRaises(ValueError):
                    tracker.on_candle(red_above(older), EMA, SMA, ATR)

    def test_the_window_must_be_a_whole_number_of_at_least_one_candle(self):
        for bad in (0, -1, 2.5, True, "3", None):
            with self.subTest(confirm_within=bad):
                with self.assertRaisesRegex(ValueError, "confirm_within"):
                    PullbackTracker(confirm_within=bad, max_distance_atr=1.0, interval_ms=STEP_MS)

    def test_the_cap_must_be_a_finite_number_above_zero(self):
        for bad in (0, 0.0, -1.0, math.nan, math.inf, 10**400, True, "1", None):
            with self.subTest(max_distance_atr=repr(bad)[:20]):
                with self.assertRaisesRegex(ValueError, "max_distance_atr"):
                    PullbackTracker(confirm_within=3, max_distance_atr=bad, interval_ms=STEP_MS)

    def test_a_whole_number_cap_is_accepted(self):
        tracker = new_tracker(max_distance_atr=1)
        self.assertIsNotNone(tracker.on_candle(touch_green(0), EMA, SMA, ATR))


class ValueObjectTests(unittest.TestCase):
    def test_candle_and_signal_are_immutable_values(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            touch_green(0).close = 1.0
        with self.assertRaises(dataclasses.FrozenInstanceError):
            PullbackSignal(close_ms=1).close_ms = 2
        self.assertEqual(PullbackSignal(close_ms=7), PullbackSignal(close_ms=7))
        self.assertEqual(touch_green(0), touch_green(0))


def reference_signal(history, k, width, multiple, last_consumed):
    """The rule as a scan back over the window, with no state kept between candles.

    ``history[j]`` is ``(candle, ema, sma20_1h, atr14_1h)``; ``last_consumed`` is the position of
    the last candle whose signal the caller took (-1 for none), which used up every touch up to it.
    """
    c, ema, sma, atr = history[k]
    if ema is None or sma is None or atr is None:
        return False
    if not (c.close > c.open and c.close > ema and c.close <= sma + multiple * atr):
        return False
    first = max(k - width + 1, last_consumed + 1, 0)
    return any(
        history[j][1] is not None and history[j][0].low <= history[j][1]
        for j in range(first, k + 1)
    )


def random_history(rng, count):
    """Candles in regimes that sit below, around or well above the EMA, with gaps in the inputs."""
    history = []
    bias = 0.0
    for n in range(count):
        if n % 25 == 0:
            bias = rng.choice((-1.0, 0.0, 0.5, 2.0))
        ema = 100.0 + rng.uniform(-1.0, 1.0)
        open_ = ema + bias + rng.uniform(-1.5, 1.5)
        close = ema + bias + rng.uniform(-1.5, 1.5)
        low = min(open_, close) - rng.uniform(0.0, 1.0)
        sma = 100.0 + rng.uniform(-1.0, 1.0)
        atr = rng.uniform(0.5, 2.0)
        inputs = [None if rng.random() < 0.08 else value for value in (ema, sma, atr)]
        history.append((candle(n, open_, close, low), *inputs))
    return history


class DifferentialTests(unittest.TestCase):
    def test_matches_a_window_scan_on_random_candles_whatever_the_caller_consumes(self):
        for width in (1, 2, 3, 5):
            for multiple in (0.5, 1.0):
                rng = random.Random(width * 100 + int(multiple * 10))
                history = random_history(rng, 1500)
                tracker = new_tracker(confirm_within=width, max_distance_atr=multiple)
                last_consumed, taken, blocked = -1, 0, 0
                with self.subTest(confirm_within=width, max_distance_atr=multiple):
                    for k, (c, ema, sma, atr) in enumerate(history):
                        expected = reference_signal(history, k, width, multiple, last_consumed)
                        result = tracker.on_candle(c, ema, sma, atr)
                        self.assertEqual(result is not None, expected, f"candle {k}")
                        if result is None:
                            continue
                        self.assertEqual(result.close_ms, c.close_ms)
                        if rng.random() < 0.5:
                            tracker.consume()
                            last_consumed = k
                            taken += 1
                        else:
                            blocked += 1
                    # the comparison is not vacuous: signals were taken and blocked, many times
                    self.assertGreater(taken, 30)
                    self.assertGreater(blocked, 30)


if __name__ == "__main__":
    unittest.main()
