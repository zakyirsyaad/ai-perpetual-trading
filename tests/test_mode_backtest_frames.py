"""Tests for research.mode_backtest.frames: the hourly frame, the condition at a time, NAIK days,
and the trigger series, the Mode D stretch and the frames of a pair.

The data is synthetic and every price is a function of time, so each expected number is computed
here from that function and never from the frame. Bars are flat. In the hourly tests a price stays
the same inside each UTC hour, or inside each UTC 4h bucket where a test reads 4h candles, so the
native warm-up bars and the bars aggregated from the day files describe one market and the numbers
run on across the seam. The trigger and stretch tests use a price that moves every minute
(``wavy``), so that the high, low and close of a 3m, 5m or 15m candle all differ.
"""

import dataclasses
import math
import statistics
import tempfile
import unittest
from array import array
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest import mock

from bot.core.entry_window import parse_weekly_window
from bot.strategy.modes.config import SelectorConfig, load_modes_config
from bot.strategy.modes.near_mean import stretch_from_window
from bot.strategy.modes.regime import NAIK, SIDEWAYS, TIDAK_ENTRY, TURUN
from bot.strategy.pine_indicators import atr, ema
from research.mode_backtest import frames
from research.mode_backtest.data import load_pair
from research.mode_backtest.frames import HourlyContext, HourlyFrame
from research.pine_gate.bars import (
    BarGapError,
    Bars,
    aggregate,
    bars_from_rows,
    slice_until,
    wib_day_start_ms,
)
from research.pine_gate.features import FeatureFrame
from research.pine_gate.spec import WIB
from tests.mode_backtest_helpers import (
    FOUR_HOUR_MS,
    HOUR_MS,
    MINUTE_MS,
    flat_price,
    minute_rows,
    write_days,
    write_warmups,
)

SYMBOL = "BTC-USDT"
SEL = SelectorConfig(sma_cepat_1h=20, sma_lambat_1h=50, ema_4h=50, sideways_jarak_kali_atr=0.2)
WINDOW = parse_weekly_window("mon 07:00-sat 00:00")

MON = date(2026, 6, 29)
TUE, WED, THU, FRI, SAT, SUN, NEXT_MON = (MON + timedelta(days=k) for k in range(1, 8))
DAY_MS = 24 * HOUR_MS

BASE_PRICE = 10_000.0
ATR_PERIOD = 14
MEDIAN_WINDOW = 720  # closed hours, from the manager's document
# Wilder ATR is first defined on the 8 x 14 = 112th candle, so 720 of them exist from index 830.
FIRST_ATR_INDEX = 8 * ATR_PERIOD - 1
FIRST_MEDIAN_INDEX = FIRST_ATR_INDEX + MEDIAN_WINDOW - 1

# Enough warm-up for every value on the first day: 831 1h candles for the median, and 401 4h
# candles (the last one is still forming at 00:00 WIB) for the 4h EMA50.
WARM_HOURS = 900
WARM_4H = 410
WILD = 50.0  # factor that turns the real price of a poisoned minute into a wild one

NO_DATA = HourlyContext(None, None, None, None, None, None, TIDAK_ENTRY)


def at(day, hour=0, minute=0):
    """UTC milliseconds of a WIB wall-clock time; ``hour`` may run past 24 into the next day."""
    return wib_day_start_ms(day) + (hour * 60 + minute) * MINUTE_MS


def rising(n):
    return float(n)


def falling(n):
    return -float(n)


def hourly_price(first_day, shape):
    """Price per minute (the helper's axis), flat inside each UTC hour.

    ``shape(n)`` is the price above BASE_PRICE ``n`` hours after the hour the first day starts
    in; ``n`` is negative in the warm-up."""
    origin_ms = wib_day_start_ms(first_day)
    origin_hour = origin_ms // HOUR_MS

    def price(minute):
        return BASE_PRICE + shape((origin_ms + minute * MINUTE_MS) // HOUR_MS - origin_hour)

    return price


def four_hour_price(first_day, shape):
    """Like ``hourly_price`` but flat inside each UTC 4h bucket; ``n`` counts buckets."""
    origin_ms = wib_day_start_ms(first_day)
    origin_bucket = origin_ms // FOUR_HOUR_MS

    def price(minute):
        return BASE_PRICE + shape((origin_ms + minute * MINUTE_MS) // FOUR_HOUR_MS - origin_bucket)

    return price


class Fixture:
    """A pair written to disk as the real data is, loaded with ``load_pair`` and framed."""

    def __init__(self, add_cleanup, first_day, days, hours_1h, bars_4h, price, sel=SEL):
        tmp = tempfile.TemporaryDirectory()
        add_cleanup(tmp.cleanup)
        root = Path(tmp.name)
        day_list = [first_day + timedelta(days=k) for k in range(days)]
        write_days(root, SYMBOL, day_list, price)
        write_warmups(root, SYMBOL, first_day, hours_1h, bars_4h, price)
        self.pair = load_pair(root, SYMBOL, first_day, day_list[-1])
        self.frame = HourlyFrame.build(self.pair, sel)


class HourlyReference:
    """The 1h closes a frame should see and the numbers the selector reads from them.

    One flat candle per hour from the first warm-up candle on, priced by the function that wrote
    the files. ``index`` is the number of a candle, given the time it opened."""

    def __init__(self, price, first_day, warm_hours, data_hours):
        origin_ms = wib_day_start_ms(first_day)
        self.first_open_ms = origin_ms - warm_hours * HOUR_MS
        self.closes = [
            price((self.first_open_ms + k * HOUR_MS - origin_ms) // MINUTE_MS)
            for k in range(warm_hours + data_hours)
        ]
        self.atr = atr(self.closes, self.closes, self.closes, ATR_PERIOD)

    def index(self, candle_open_ms):
        return (candle_open_ms - self.first_open_ms) // HOUR_MS

    def sma(self, candle_open_ms, periods):
        i = self.index(candle_open_ms)
        return statistics.fmean(self.closes[i - periods + 1 : i + 1])

    def atr_at(self, candle_open_ms):
        return self.atr[self.index(candle_open_ms)]

    def atr_median(self, candle_open_ms):
        """Median of the 720 ATR14 values ending with this candle's own; None while fewer exist."""
        i = self.index(candle_open_ms)
        if i < FIRST_MEDIAN_INDEX:
            return None
        return statistics.median(self.atr[i - MEDIAN_WINDOW + 1 : i + 1])


def poisoned(pair, start_ms, end_ms):
    """The pair with every 1m bar that opens in [start_ms, end_ms) priced WILD times higher."""
    bars = pair.bars_1m

    def scaled(column):
        return array(
            "d",
            (
                v * WILD if start_ms <= t < end_ms else v
                for t, v in zip(bars.open_ms, column, strict=True)
            ),
        )

    changed = Bars(
        bars.interval_ms,
        bars.open_ms,
        scaled(bars.open),
        scaled(bars.high),
        scaled(bars.low),
        scaled(bars.close),
        bars.volume,
    )
    return dataclasses.replace(pair, bars_1m=changed)


class WeekCase(unittest.TestCase):
    """A week that rises by 1 every hour: Monday 00:00 WIB to the next Monday 00:00 WIB.

    With that price SMA20 stays above SMA50 and the 4h close above its EMA, so every candle
    after the warm-up is NAIK. The warm-up is long enough for every value on the first day."""

    @classmethod
    def setUpClass(cls):
        cls.price = hourly_price(MON, rising)
        cls.week = Fixture(cls.addClassCleanup, MON, 7, WARM_HOURS, WARM_4H, cls.price)
        cls.reference = HourlyReference(cls.price, MON, WARM_HOURS, 7 * 24)


class HourlyContextTests(WeekCase):
    def assert_hourly_numbers(self, t_ms, candle_open_ms):
        """The context at ``t_ms`` holds the numbers of the 1h candle that opened at that time."""
        ctx = self.week.frame.context_at(t_ms)
        ref = self.reference
        self.assertEqual(ctx.sma20_1h, ref.sma(candle_open_ms, 20))
        self.assertEqual(ctx.sma50_1h, ref.sma(candle_open_ms, 50))
        self.assertEqual(ctx.atr14_1h, ref.atr_at(candle_open_ms))
        self.assertEqual(ctx.atr_median, ref.atr_median(candle_open_ms))

    def test_context_at_hour_close_uses_the_hour_just_closed_and_never_the_forming_one(self):
        cases = (
            ("09:59:59.999: 09:00-10:00 is still forming", at(WED, 10) - 1, at(WED, 8)),
            ("10:00: 09:00-10:00 has just closed", at(WED, 10), at(WED, 9)),
            ("10:00:00.001: 10:00-11:00 has begun", at(WED, 10) + 1, at(WED, 9)),
            ("10:59:59.999: 10:00-11:00 is still forming", at(WED, 11) - 1, at(WED, 9)),
            ("11:00: 10:00-11:00 has just closed", at(WED, 11), at(WED, 10)),
        )
        for label, t_ms, candle_open_ms in cases:
            with self.subTest(label):
                self.assert_hourly_numbers(t_ms, candle_open_ms)

        ctx = self.week.frame.context_at(at(WED, 10))
        forming_sma = self.reference.sma(at(WED, 10), 20)
        self.assertNotEqual(self.reference.sma(at(WED, 9), 20), forming_sma)  # the hours differ
        self.assertNotEqual(ctx.sma20_1h, forming_sma)

    def test_nothing_from_t_on_can_change_the_context_at_t(self):
        pair, frame = self.week.pair, self.week.frame
        end_ms = pair.bars_1m.open_ms[-1] + MINUTE_MS
        cases = (
            ("an hour close", at(WED, 10)),
            ("a 4h close", at(WED, 11)),
            ("inside an hour", at(WED, 10, 30)),
        )
        for label, t_ms in cases:
            with self.subTest(label):
                after = HourlyFrame.build(poisoned(pair, t_ms, end_ms), SEL).context_at(t_ms)
                self.assertEqual(after, frame.context_at(t_ms))

    def test_the_candles_that_closed_at_t_do_change_the_context_at_t(self):
        pair, frame = self.week.pair, self.week.frame
        hour_close, four_hour_close = at(WED, 10), at(WED, 11)

        hour_poisoned = HourlyFrame.build(poisoned(pair, at(WED, 9), hour_close), SEL)
        before, after = frame.context_at(hour_close), hour_poisoned.context_at(hour_close)
        self.assertNotEqual(after.sma20_1h, before.sma20_1h)
        self.assertNotEqual(after.atr14_1h, before.atr14_1h)
        self.assertEqual(after.close_4h, before.close_4h)  # 07:00-11:00 has not closed at 10:00

        four_poisoned = HourlyFrame.build(poisoned(pair, at(WED, 7), four_hour_close), SEL)
        before, after = frame.context_at(four_hour_close), four_poisoned.context_at(four_hour_close)
        self.assertNotEqual(after.close_4h, before.close_4h)

    def test_warmup_seam_makes_the_first_day_warm(self):
        origin_ms = at(MON)
        for t_ms in range(origin_ms - HOUR_MS, origin_ms + DAY_MS, 15 * MINUTE_MS):
            with self.subTest(minutes_from_start=(t_ms - origin_ms) // MINUTE_MS):
                ctx = self.week.frame.context_at(t_ms)
                values = [getattr(ctx, field.name) for field in dataclasses.fields(ctx)]
                self.assertNotIn(None, values)
                self.assertEqual(ctx.kondisi, NAIK)

        # The numbers run on across the seam: the first aggregated candle follows the last
        # warm-up candle, and half of the 20 hours of SMA20 at 10:00 are warm-up candles.
        cases = (
            ("the last warm-up candle closes as the day starts", at(MON), at(MON) - HOUR_MS),
            ("a minute before it, the candle before that", at(MON) - MINUTE_MS, at(MON, -2)),
            ("the first aggregated candle closes", at(MON, 1), at(MON)),
            ("10 aggregated and 10 warm-up candles", at(MON, 10), at(MON, 9)),
        )
        for label, t_ms, candle_open_ms in cases:
            with self.subTest(label):
                ctx = self.week.frame.context_at(t_ms)
                self.assertEqual(ctx.sma20_1h, self.reference.sma(candle_open_ms, 20))
                self.assertEqual(ctx.atr14_1h, self.reference.atr_at(candle_open_ms))

    def test_a_hole_at_the_warmup_seam_stops_the_build(self):
        pair = self.week.pair
        no_last_1h = slice_until(pair.warmup_1h, at(MON) - 1)  # drops the candle closing at 00:00
        no_straddling_4h = slice_until(pair.warmup_4h, at(MON) - 1)  # drops the one closing 03:00

        for label, damaged in (
            ("1h", dataclasses.replace(pair, warmup_1h=no_last_1h)),
            ("4h", dataclasses.replace(pair, warmup_4h=no_straddling_4h)),
        ):
            with self.subTest(label), self.assertRaises(BarGapError):
                HourlyFrame.build(damaged, SEL)

    def test_context_numbers_are_finite_or_none(self):
        start_ms = at(MON) - (WARM_HOURS + 1) * HOUR_MS
        for t_ms in range(start_ms, at(NEXT_MON) + HOUR_MS, 30 * MINUTE_MS):
            ctx = self.week.frame.context_at(t_ms)
            for field in dataclasses.fields(ctx):
                value = getattr(ctx, field.name)
                if field.name != "kondisi" and value is not None:
                    self.assertTrue(math.isfinite(value), (t_ms, field.name, value))
            self.assertIn(ctx.kondisi, (NAIK, TURUN, SIDEWAYS, TIDAK_ENTRY))

    def test_sideways_band_comes_from_the_selector_config(self):
        t_ms = at(WED, 10)
        self.assertEqual(self.week.frame.context_at(t_ms).kondisi, NAIK)  # gap 15, band 0.2 x ATR

        wide = SelectorConfig(20, 50, 50, sideways_jarak_kali_atr=1000.0)
        ctx = HourlyFrame.build(self.week.pair, wide).context_at(t_ms)

        self.assertEqual(ctx.kondisi, SIDEWAYS)

    def test_sma_periods_come_from_the_selector_config(self):
        short = SelectorConfig(5, 10, ema_4h=20, sideways_jarak_kali_atr=0.2)

        ctx = HourlyFrame.build(self.week.pair, short).context_at(at(WED, 10))

        # The fields keep the names of the document's defaults and hold the configured periods.
        self.assertEqual(ctx.sma20_1h, self.reference.sma(at(WED, 9), 5))
        self.assertEqual(ctx.sma50_1h, self.reference.sma(at(WED, 9), 10))


class FourHourBucketTests(unittest.TestCase):
    """The price is flat inside each UTC 4h bucket and 1 higher in the next, so the 4h candle a
    context holds can be read off ``close_4h``."""

    # (hour, minute, extra ms on the day) -> (day offset, hour) of the 4h close it must see
    CASES = (
        ((2, 59, 59_999), (-1, 23)),
        ((3, 0, 0), (0, 3)),
        ((6, 59, 59_999), (0, 3)),
        ((7, 0, 0), (0, 7)),
        ((10, 59, 59_999), (0, 7)),
        ((11, 0, 0), (0, 11)),
        ((14, 59, 59_999), (0, 11)),
        ((15, 0, 0), (0, 15)),
        ((18, 59, 59_999), (0, 15)),
        ((19, 0, 0), (0, 19)),
        ((22, 59, 59_999), (0, 19)),
        ((23, 0, 0), (0, 23)),
        ((23, 59, 59_999), (0, 23)),
    )

    @classmethod
    def setUpClass(cls):
        price = four_hour_price(MON, rising)
        cls.data = Fixture(cls.addClassCleanup, MON, 3, 120, WARM_4H, price)
        cls.origin_bucket = at(MON) // FOUR_HOUR_MS  # the bucket 16:00-20:00 UTC; it is number 0
        # Buckets -409..0 are the native warm-up candles, 1..17 are aggregated from the days.
        closes = [BASE_PRICE + n for n in range(-(WARM_4H - 1), 18)]
        cls.ema_by_bucket = dict(zip(range(-(WARM_4H - 1), 18), ema(closes, 50), strict=True))

    def seen(self, t_ms):
        """(WIB close time, EMA50, bucket number) of the 4h candle the context at ``t_ms`` holds."""
        ctx = self.data.frame.context_at(t_ms)
        number = round(ctx.close_4h - BASE_PRICE)
        close_ms = (self.origin_bucket + number + 1) * FOUR_HOUR_MS
        return datetime.fromtimestamp(close_ms / 1000, WIB), ctx.ema50_4h, number

    def test_four_hour_buckets_close_at_03_07_11_15_19_23_wib(self):
        for day in (MON, TUE):  # Monday's 03:00 close is the candle that straddles the seam
            for (hour, minute, extra_ms), (days_back, close_hour) in self.CASES:
                label = f"{day} {hour:02d}:{minute:02d} (+{extra_ms} ms)"
                with self.subTest(label):
                    t_ms = at(day, hour, minute) + extra_ms
                    close, ema50, number = self.seen(t_ms)

                    expected = datetime.combine(
                        day + timedelta(days=days_back), time(close_hour), tzinfo=WIB
                    )
                    self.assertEqual(close, expected)
                    self.assertEqual(ema50, self.ema_by_bucket[number])  # same candle as close_4h


class AtrMedianTests(unittest.TestCase):
    """Volatility rises every hour, so the 720-value window can only be the right one."""

    WARM = 800
    DAYS = 3

    @classmethod
    def setUpClass(cls):
        def volatility_ramp(n):
            k = n + cls.WARM  # hours since the first warm-up candle opened
            return k + k * k / 1000.0

        price = hourly_price(MON, volatility_ramp)
        cls.data = Fixture(cls.addClassCleanup, MON, cls.DAYS, cls.WARM, WARM_4H, price)
        cls.reference = HourlyReference(price, MON, cls.WARM, cls.DAYS * 24)

    def test_atr_median_needs_720_values(self):
        self.assertEqual(frames.ATR_MEDIAN_HOURS, MEDIAN_WINDOW)
        frame, ref = self.data.frame, self.reference
        origin_ms = at(MON)
        # Hour j after the start closes candle number WARM - 1 + j. The ATR exists from number
        # 111, so 719 values exist at j = 30 and 720 from j = 31.
        first_j = FIRST_MEDIAN_INDEX + 1 - self.WARM
        self.assertEqual(first_j, 31)

        for j in (0, first_j - 1):
            with self.subTest(hours_after_start=j):
                ctx = frame.context_at(origin_ms + j * HOUR_MS)
                self.assertIsNotNone(ctx.atr14_1h)
                self.assertIsNone(ctx.atr_median)

        for j in (first_j, first_j + 1, 48, self.DAYS * 24):
            with self.subTest(hours_after_start=j):
                candle_open_ms = origin_ms + (j - 1) * HOUR_MS
                ctx = frame.context_at(origin_ms + j * HOUR_MS)
                self.assertIsNotNone(ctx.atr_median)
                self.assertEqual(ctx.atr_median, ref.atr_median(candle_open_ms))

    def test_the_median_is_over_exactly_the_last_720_values_the_latest_included(self):
        ref = self.reference
        last = self.WARM + self.DAYS * 24 - 1  # the candle that closes with the data
        candle_open_ms = ref.first_open_ms + last * HOUR_MS
        window = ref.atr[last - MEDIAN_WINDOW + 1 : last + 1]
        wrong_windows = {
            "without the latest": ref.atr[last - MEDIAN_WINDOW : last],
            "719 values": ref.atr[last - MEDIAN_WINDOW + 2 : last + 1],
            "721 values": ref.atr[last - MEDIAN_WINDOW : last + 1],
        }
        for label, wrong in wrong_windows.items():  # the ramp tells the windows apart
            self.assertNotEqual(statistics.median(wrong), statistics.median(window), label)

        ctx = self.data.frame.context_at(candle_open_ms + HOUR_MS)

        self.assertEqual(ctx.atr_median, statistics.median(window))
        self.assertNotEqual(ctx.atr_median, ctx.atr14_1h)


class ColdStartTests(unittest.TestCase):
    """What the frame says while a series is too short, with a warm-up of the length given."""

    def frame(self, hours_1h, bars_4h, sel=SEL):
        price = hourly_price(MON, rising)
        return Fixture(self.addCleanup, MON, 1, hours_1h, bars_4h, price, sel).frame

    def test_condition_is_tidak_entry_while_inputs_are_none(self):
        cases = (
            ("4h EMA50: 100 4h candles", 200, 100, "ema50_4h"),
            ("ATR14: 100 1h candles", 100, WARM_4H, "atr14_1h"),
            ("SMA50: 40 1h candles", 40, WARM_4H, "sma50_1h"),
        )
        for label, hours_1h, bars_4h, missing in cases:
            with self.subTest(label):
                ctx = self.frame(hours_1h, bars_4h).context_at(at(MON))

                self.assertIsNone(getattr(ctx, missing))
                self.assertEqual(ctx.kondisi, TIDAK_ENTRY)

        # Only the missing input differs: the same rising market is NAIK once all five exist.
        warm = self.frame(200, WARM_4H).context_at(at(MON))
        self.assertEqual(warm.kondisi, NAIK)
        self.assertEqual(
            [warm.sma20_1h, warm.sma50_1h, warm.atr14_1h, warm.close_4h, warm.ema50_4h].count(None),
            0,
        )

    def test_the_median_is_not_an_input_of_the_condition(self):
        ctx = self.frame(200, WARM_4H).context_at(at(MON))  # 200 candles: no 720-value median yet

        self.assertIsNone(ctx.atr_median)
        self.assertEqual(ctx.kondisi, NAIK)

    def test_before_any_candle_has_closed_everything_is_none(self):
        frame = self.frame(200, WARM_4H)

        self.assertEqual(frame.context_at(0), NO_DATA)
        self.assertEqual(frame.context_at(-1), NO_DATA)

    def test_a_series_with_no_closed_candle_gives_none_and_not_its_last_value(self):
        # The 1h warm-up starts at 200 h before the day, the 4h warm-up ends only one hour before.
        no_hour_yet = self.frame(200, WARM_4H).context_at(at(MON) - 200 * HOUR_MS)
        no_four_hour_yet = self.frame(200, 2).context_at(at(MON) - 100 * HOUR_MS)

        self.assertEqual(dataclasses.replace(no_hour_yet, close_4h=None, ema50_4h=None), NO_DATA)
        self.assertIsNotNone(no_hour_yet.close_4h)  # it is the 1h series that has not begun
        self.assertIsNotNone(no_four_hour_yet.sma20_1h)
        self.assertIsNone(no_four_hour_yet.close_4h)
        self.assertIsNone(no_four_hour_yet.ema50_4h)
        self.assertEqual(no_four_hour_yet.kondisi, TIDAK_ENTRY)

    def test_the_4h_ema_period_comes_from_the_selector_config(self):
        short_ema = SelectorConfig(20, 50, ema_4h=20, sideways_jarak_kali_atr=0.2)

        default = self.frame(200, 200).context_at(at(MON))  # 200 candles: EMA50 needs 401
        shorter = self.frame(200, 200, short_ema).context_at(at(MON))  # EMA20 needs 161

        self.assertIsNone(default.ema50_4h)
        self.assertIsNotNone(shorter.ema50_4h)
        self.assertEqual(shorter.kondisi, NAIK)


class NaikDaysTests(WeekCase):
    WEEK_RANGE = (at(MON), at(NEXT_MON))

    def kondisi_is(self, kondisi_at):
        """Patch ``context_at`` so that the condition at t is ``kondisi_at(t)``; no numbers."""

        def context_at(_frame, t_ms):
            return HourlyContext(None, None, None, None, None, None, kondisi_at(t_ms))

        return mock.patch.object(HourlyFrame, "context_at", autospec=True, side_effect=context_at)

    def only_these_are_naik(self, *closes_ms):
        naik = set(closes_ms)
        return self.kondisi_is(lambda t_ms: NAIK if t_ms in naik else TURUN)

    def week_days(self, window=WINDOW, min_naik_hours=1):
        return self.week.frame.naik_days(*self.WEEK_RANGE, window, min_naik_hours)

    def test_naik_days_count_only_window_hours(self):
        cases = (
            ("Monday 06:00 is before the window opens", [at(MON, 6)], set()),
            ("Monday 07:00 is its first hour", [at(MON, 7)], {MON}),
            ("Friday 23:00 is its last hour", [at(FRI, 23)], {FRI}),
            ("Saturday 00:00 is the first close after it", [at(SAT, 0)], set()),
            ("Saturday and Sunday are outside it", [at(SAT, 12), at(SUN, 12)], set()),
            (
                "only the closes inside count",
                [at(MON, 6), at(MON, 7), at(SAT, 0), at(SAT, 12)],
                {MON},
            ),
        )
        for label, naik_closes, expected in cases:
            with self.subTest(label), self.only_these_are_naik(*naik_closes):
                self.assertEqual(self.week_days(), expected)

    def test_without_a_window_every_hour_counts(self):
        with self.only_these_are_naik(at(MON, 6), at(SAT, 12)):
            self.assertEqual(self.week_days(window=None), {MON, SAT})

    def test_naik_days_follow_the_wib_day_of_the_close(self):
        cases = (
            ("a close at 00:00 WIB is on the new day", [at(TUE, 0)], {TUE}),
            ("a close at 23:00 WIB is still on its day", [at(MON, 23)], {MON}),
            ("closes either side of midnight are two days", [at(MON, 23), at(TUE, 0)], {MON, TUE}),
        )
        for label, naik_closes, expected in cases:
            with self.subTest(label), self.only_these_are_naik(*naik_closes):
                self.assertEqual(self.week_days(), expected)

    def test_naik_days_need_the_naik_condition(self):
        for kondisi in (TURUN, SIDEWAYS, TIDAK_ENTRY):
            with self.subTest(kondisi), self.kondisi_is(lambda t_ms, k=kondisi: k):
                self.assertEqual(self.week_days(), set())
        with self.kondisi_is(lambda t_ms: NAIK):
            self.assertEqual(self.week_days(), {MON, TUE, WED, THU, FRI})

    def test_naik_days_range_is_half_open_on_the_close(self):
        noon, one, two = at(WED, 12), at(WED, 13), at(WED, 14)
        wed_00, thu_00 = at(WED, 0), at(THU, 0)
        cases = (  # (label, the only NAIK close, start, end, the days)
            ("a close at the start is inside", noon, noon, two, {WED}),
            ("a close 1 ms before the start is outside", noon, noon + 1, two, set()),
            ("a close 1 ms before the end is inside", one, noon, one + 1, {WED}),
            ("a close at the end is outside", one, noon, one, set()),
            ("a day's first close is inside its day", wed_00, wed_00, thu_00, {WED}),
            ("the next midnight close is not", thu_00, wed_00, thu_00, set()),
            ("it is inside the day it opens", thu_00, wed_00 + 1, thu_00 + 1, {THU}),
        )
        for label, naik_close, start_ms, end_ms, expected in cases:
            with self.subTest(label), self.only_these_are_naik(naik_close):
                self.assertEqual(self.week.frame.naik_days(start_ms, end_ms, None, 1), expected)

    def test_a_day_with_one_window_hour_is_kept_at_1_and_dropped_at_2(self):
        with self.only_these_are_naik(at(WED, 10)):
            self.assertEqual(self.week_days(min_naik_hours=1), {WED})
            self.assertEqual(self.week_days(min_naik_hours=2), set())

    def test_a_day_with_two_window_hours_is_kept_at_2(self):
        with self.only_these_are_naik(at(WED, 10), at(WED, 15)):  # any two hours, not a run
            self.assertEqual(self.week_days(min_naik_hours=2), {WED})
            self.assertEqual(self.week_days(min_naik_hours=3), set())

    def test_hours_outside_the_window_do_not_count_toward_the_minimum(self):
        with self.only_these_are_naik(at(MON, 6), at(MON, 7)):  # 06:00 is before the window opens
            self.assertEqual(self.week_days(min_naik_hours=1), {MON})
            self.assertEqual(self.week_days(min_naik_hours=2), set())
            self.assertEqual(self.week_days(window=None, min_naik_hours=2), {MON})

    def test_the_hours_are_counted_per_wib_day(self):
        cases = (
            ("23:00 and 00:00 are two days (one UTC day)", [at(MON, 23), at(TUE, 0)], set()),
            ("00:00 and 01:00 are one day", [at(TUE, 0), at(TUE, 1)], {TUE}),
            ("one hour on each of two days is not two", [at(TUE, 10), at(WED, 10)], set()),
        )
        for label, naik_closes, expected in cases:
            with self.subTest(label), self.only_these_are_naik(*naik_closes):
                self.assertEqual(self.week_days(min_naik_hours=2), expected)

    def test_hours_outside_the_range_do_not_count_toward_the_minimum(self):
        noon, one, two = at(WED, 12), at(WED, 13), at(WED, 14)
        naik_days = self.week.frame.naik_days
        with self.only_these_are_naik(noon, one):
            self.assertEqual(naik_days(noon, two, None, 2), {WED})
            self.assertEqual(naik_days(noon, one, None, 2), set())  # 13:00 is the end
            self.assertEqual(naik_days(noon + 1, two, None, 2), set())  # 12:00 is before the start

    def test_a_minimum_below_1_is_refused(self):
        for minimum in (0, -1):
            with self.subTest(minimum=minimum), self.assertRaises(ValueError):
                self.week_days(min_naik_hours=minimum)

    def test_the_minimum_has_no_default_so_no_caller_can_leave_it_out(self):
        with self.assertRaises(TypeError):
            self.week.frame.naik_days(*self.WEEK_RANGE, WINDOW)

    def test_the_condition_is_read_at_the_close_of_each_candle(self):
        seen = []

        def recording(t_ms):
            seen.append(t_ms)
            return TURUN

        with self.kondisi_is(recording):
            self.week.frame.naik_days(at(WED, 12), at(WED, 15), None, 1)

        self.assertEqual(seen, [at(WED, 12), at(WED, 13), at(WED, 14)])

    def test_naik_days_of_a_rising_week(self):
        frame = self.week.frame
        self.assertEqual(frame.context_at(at(MON, 7)).kondisi, NAIK)  # the real condition

        self.assertEqual(self.week_days(), {MON, TUE, WED, THU, FRI})
        self.assertEqual(self.week_days(window=None), {MON, TUE, WED, THU, FRI, SAT, SUN})
        self.assertEqual(frame.naik_days(at(MON, 6), at(MON, 7), WINDOW, 1), set())
        self.assertEqual(frame.naik_days(at(MON, 6), at(MON, 7) + 1, WINDOW, 1), {MON})
        self.assertEqual(frame.naik_days(at(FRI, 23), at(SAT, 1), WINDOW, 1), {FRI})
        self.assertEqual(frame.naik_days(at(SAT, 0), at(NEXT_MON, 0), WINDOW, 1), set())

    def test_naik_days_of_a_rising_week_with_a_minimum(self):
        # Monday's closes from 07:00 to 23:00 are in the window: 17 hours. Tuesday to Friday have
        # 24, and without the window every day of the week has its 24 closes.
        cases = (
            (WINDOW, 17, {MON, TUE, WED, THU, FRI}),
            (WINDOW, 18, {TUE, WED, THU, FRI}),
            (WINDOW, 24, {TUE, WED, THU, FRI}),
            (WINDOW, 25, set()),
            (None, 24, {MON, TUE, WED, THU, FRI, SAT, SUN}),
            (None, 25, set()),
        )
        for window, minimum, expected in cases:
            with self.subTest(window=window, minimum=minimum):
                self.assertEqual(self.week_days(window, minimum), expected)

    def test_naik_days_of_a_falling_week_is_empty(self):
        down = Fixture(self.addCleanup, MON, 7, 200, WARM_4H, hourly_price(MON, falling))

        self.assertEqual(down.frame.context_at(at(WED, 10)).kondisi, TURUN)
        self.assertEqual(down.frame.naik_days(*self.WEEK_RANGE, WINDOW, 1), set())
        self.assertEqual(down.frame.naik_days(*self.WEEK_RANGE, None, 1), set())

    def test_naik_days_is_a_frozenset_of_dates(self):
        days = self.week_days()

        self.assertIsInstance(days, frozenset)
        self.assertTrue(all(type(day) is date for day in days))

    def test_an_empty_range_is_empty_and_a_reversed_one_is_refused(self):
        self.assertEqual(self.week.frame.naik_days(at(WED, 12), at(WED, 12), None, 1), frozenset())
        with self.assertRaises(ValueError):
            self.week.frame.naik_days(at(WED, 13), at(WED, 12), None, 1)


class ContextTypeTests(unittest.TestCase):
    def test_the_context_is_frozen_and_lists_the_selector_inputs_then_the_condition(self):
        ctx = HourlyContext(1.0, 2.0, 3.0, 4.0, 5.0, 6.0, NAIK)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            ctx.kondisi = TURUN
        self.assertEqual(
            [field.name for field in dataclasses.fields(ctx)],
            ["sma20_1h", "sma50_1h", "atr14_1h", "atr_median", "close_4h", "ema50_4h", "kondisi"],
        )

    def test_series_of_the_wrong_interval_are_refused(self):
        hourly, four_hourly = bars_from_rows([], HOUR_MS), bars_from_rows([], FOUR_HOUR_MS)

        HourlyFrame(hourly, four_hourly, SEL)  # the right pair is accepted, empty as it is
        for label, bars_1h, bars_4h in (
            ("two 4h series", four_hourly, four_hourly),
            ("two 1h series", hourly, hourly),
            ("swapped", four_hourly, hourly),
        ):
            with self.subTest(label), self.assertRaises(ValueError):
                HourlyFrame(bars_1h, bars_4h, SEL)

    def test_the_median_covers_30_days_of_closed_hours(self):
        self.assertEqual(frames.ATR_MEDIAN_HOURS, 30 * 24)


REPO_CONFIG = Path(__file__).resolve().parents[1] / "config" / "modes.yaml"
M3, M5, M15 = 3 * MINUTE_MS, 5 * MINUTE_MS, 15 * MINUTE_MS
TRIGGER_EMA = 20
FIRST_TRIGGER_EMA_INDEX = 8 * TRIGGER_EMA - 1  # the EMA needs 8 x 20 = 160 candles
STRETCH_WINDOW, STRETCH_EMA, STRETCH_ATR = 39, 21, 14  # config/modes.yaml, mode D
BAND = 0.75  # the near-mean band of mode D


def wavy(minute):
    """A price that moves inside every 3m bucket, so a bar's high, low and close all differ."""
    return (
        100.0
        + 4.0 * math.sin(minute / 53.0)
        + 1.3 * math.sin(minute / 7.1)
        + 0.6 * math.sin(minute * 1.7)
    )


def candle_closes(minutes_per_candle, days):
    """The closes a flat-bar day file of ``wavy`` implies: the price of each candle's last minute.

    Candles of 3, 5 and 15 minutes start on the first minute of the first day, because 00:00 WIB
    is 17:00 UTC and falls on the UTC grid of all three."""
    count = days * 24 * 60 // minutes_per_candle
    return [wavy(k * minutes_per_candle + minutes_per_candle - 1) for k in range(count)]


def minute_bars(first_day, count, price, start_minute=0):
    """``count`` 1m bars from ``start_minute`` minutes after 00:00 WIB of ``first_day``.

    Bar ``i`` of the series is flat at ``price(i)``."""
    start_ms = wib_day_start_ms(first_day) + start_minute * MINUTE_MS
    return bars_from_rows(minute_rows(start_ms, count, price), MINUTE_MS)


class WavyCase(unittest.TestCase):
    """Three WIB days of the wavy price, written to disk and loaded as the real data is."""

    DAYS = 3

    @classmethod
    def setUpClass(cls):
        cls.data = Fixture(cls.addClassCleanup, MON, cls.DAYS, 12, 4, wavy)
        cls.pair = cls.data.pair
        cls.end_ms = cls.pair.bars_1m.open_ms[-1] + MINUTE_MS


class TriggerSeriesTests(WavyCase):
    def test_trigger_ema_is_aligned_to_closed_candles(self):
        ts = frames.trigger_series(self.pair.bars_1m, "15m", TRIGGER_EMA)
        closes = candle_closes(15, self.DAYS)
        self.assertEqual(len(ts.bars), len(closes))  # 288 candles, one per row of the EMA
        self.assertEqual(len(ts.ema), len(closes))

        # No value before 160 candles exist; the 160th carries the first.
        self.assertEqual(ts.ema[:FIRST_TRIGGER_EMA_INDEX], (None,) * FIRST_TRIGGER_EMA_INDEX)
        self.assertIsNotNone(ts.ema[FIRST_TRIGGER_EMA_INDEX])

        # The value at candle i is the EMA through candle i's own close: closes 0..i, not 0..i-1
        # and not 0..i+1. The price moves every candle, so a series shifted by one would differ.
        for i in (FIRST_TRIGGER_EMA_INDEX, FIRST_TRIGGER_EMA_INDEX + 1, 200, len(closes) - 1):
            with self.subTest(candle=i):
                through_close = ema(closes[: i + 1], TRIGGER_EMA)[-1]
                self.assertEqual(ts.ema[i], through_close)
                self.assertNotEqual(through_close, ema(closes[:i], TRIGGER_EMA)[-1])
                if i + 1 < len(closes):
                    self.assertNotEqual(through_close, ema(closes[: i + 2], TRIGGER_EMA)[-1])

    def test_nothing_after_a_candle_closes_can_change_its_ema(self):
        ts = frames.trigger_series(self.pair.bars_1m, "15m", TRIGGER_EMA)
        i = 200
        close_ms = ts.bars.open_ms[i] + M15

        later = poisoned(self.pair, close_ms, self.end_ms).bars_1m
        after = frames.trigger_series(later, "15m", TRIGGER_EMA)
        self.assertEqual(after.ema[: i + 1], ts.ema[: i + 1])
        self.assertNotEqual(after.ema[i + 1], ts.ema[i + 1])  # the poison is felt from the next one

        last_minute = poisoned(self.pair, close_ms - MINUTE_MS, close_ms).bars_1m
        own = frames.trigger_series(last_minute, "15m", TRIGGER_EMA)
        self.assertNotEqual(own.ema[i], ts.ema[i])  # the candle's own close does count

    def test_each_trigger_timeframe_has_its_own_candles_and_ema(self):
        for tf, interval_ms, minutes in (("15m", M15, 15), ("5m", M5, 5)):
            with self.subTest(tf):
                ts = frames.trigger_series(self.pair.bars_1m, tf, TRIGGER_EMA)
                closes = candle_closes(minutes, self.DAYS)

                self.assertEqual(ts.bars.interval_ms, interval_ms)
                self.assertEqual(list(ts.bars.close), closes)
                self.assertEqual(ts.ema, tuple(ema(closes, TRIGGER_EMA)))
                self.assertTrue(all(t % interval_ms == 0 for t in ts.bars.open_ms))  # UTC buckets
                first = next(i for i, value in enumerate(ts.ema) if value is not None)
                self.assertEqual(first, FIRST_TRIGGER_EMA_INDEX)

    def test_the_ema_period_is_an_argument(self):
        ts = frames.trigger_series(self.pair.bars_1m, "15m", 5)

        self.assertEqual(ts.ema, tuple(ema(candle_closes(15, self.DAYS), 5)))
        self.assertEqual(ts.ema[: 8 * 5 - 1], (None,) * (8 * 5 - 1))
        self.assertIsNotNone(ts.ema[8 * 5 - 1])

    def test_a_partial_candle_at_either_end_is_dropped(self):
        # 1m bars from 00:07 to 02:40 WIB: the 00:00 candle misses its first 7 minutes and the
        # 02:30 candle its last 4.
        bars = minute_bars(MON, 154, wavy, start_minute=7)

        ts = frames.trigger_series(bars, "15m", TRIGGER_EMA)

        self.assertEqual(len(ts.bars), 9)
        self.assertEqual(ts.bars.open_ms[0], at(MON, 0, 15))
        self.assertEqual(ts.bars.open_ms[-1], at(MON, 2, 15))
        self.assertEqual(ts.ema, (None,) * 9)

    def test_only_15m_and_5m_candles_made_of_1m_bars_are_accepted(self):
        for tf in ("3m", "1m", "1h", "15", "15M", ""):
            with self.subTest(tf), self.assertRaises(ValueError):
                frames.trigger_series(self.pair.bars_1m, tf, TRIGGER_EMA)

        with self.assertRaises(ValueError):  # 3m bars would aggregate to 15m without a word
            frames.trigger_series(aggregate(self.pair.bars_1m, M3), "15m", TRIGGER_EMA)

    def test_the_first_known_ema_is_known_when_its_candle_closes(self):
        for tf, interval_ms in (("15m", M15), ("5m", M5)):
            with self.subTest(tf):
                ts = frames.trigger_series(self.pair.bars_1m, tf, TRIGGER_EMA)
                first_close = ts.bars.open_ms[FIRST_TRIGGER_EMA_INDEX] + interval_ms

                self.assertEqual(ts.first_known_ms(), first_close)
                self.assertEqual(first_close, at(MON) + 8 * TRIGGER_EMA * interval_ms)
        short = minute_bars(MON, 154, wavy, start_minute=7)  # 9 candles: no EMA at all
        self.assertIsNone(frames.trigger_series(short, "15m", TRIGGER_EMA).first_known_ms())

    def test_a_trigger_series_is_frozen_and_lists_the_candles_first(self):
        ts = frames.trigger_series(self.pair.bars_1m, "15m", TRIGGER_EMA)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            ts.ema = ()
        self.assertEqual([f.name for f in dataclasses.fields(ts)], ["bars", "ema"])


class StretchSeriesTests(WavyCase):
    def stretch(
        self, bars_1m=None, window=STRETCH_WINDOW, ema_period=STRETCH_EMA, atr_period=STRETCH_ATR
    ):
        bars_1m = self.pair.bars_1m if bars_1m is None else bars_1m
        return frames.stretch_series(bars_1m, window, ema_period, atr_period)

    def test_stretch_series_matches_pine_feature_frame(self):
        bars_3m, stretch = self.stretch()
        pine = FeatureFrame.build(
            self.pair.symbol, self.pair.bars_1m, self.pair.warmup_1h, self.pair.warmup_4h
        )
        first = bars_3m.open_ms[0]
        cases = (  # (label, time, the value Pine's snapshot must hold)
            ("38 bars have closed: none yet", first + 38 * M3, None),
            ("the 39th bar has just closed", first + 39 * M3, stretch[38]),
            ("inside the 40th bar: still the 39th's", first + 39 * M3 + 2 * MINUTE_MS, stretch[38]),
            ("a bar in the middle", first + 500 * M3, stretch[499]),
            ("the data ends with the last bar", self.end_ms, stretch[-1]),
        )
        for label, t_ms, expected in cases:
            with self.subTest(label):
                self.assertEqual(pine.snapshot(t_ms).stretch_3m, expected)
        compared = [stretch[38], stretch[499], stretch[-1]]
        self.assertNotIn(None, compared)
        self.assertEqual(len(set(compared)), 3)  # three different numbers, not a constant series

        # Every bar of the series, at the time it closes.
        differ = [
            i
            for i, open_ms in enumerate(bars_3m.open_ms)
            if pine.snapshot(open_ms + M3).stretch_3m != stretch[i]
        ]
        self.assertEqual(differ, [])
        measured = [s for s in stretch if s is not None]
        self.assertEqual(len(measured), len(bars_3m) - (STRETCH_WINDOW - 1))
        # The comparison is not vacuous: values on both sides of the mean and of the band.
        self.assertTrue(any(abs(s) <= BAND for s in measured))
        self.assertTrue(any(abs(s) > BAND for s in measured))
        self.assertTrue(any(s > 0 for s in measured))
        self.assertTrue(any(s < 0 for s in measured))

    def test_the_candles_are_3m_and_the_stretch_starts_with_the_39th(self):
        bars_3m, stretch = self.stretch()

        self.assertEqual(bars_3m.interval_ms, M3)
        self.assertEqual(list(bars_3m.close), candle_closes(3, self.DAYS))
        self.assertEqual(len(stretch), len(bars_3m))
        self.assertEqual(stretch[: STRETCH_WINDOW - 1], (None,) * (STRETCH_WINDOW - 1))
        self.assertIsNotNone(stretch[STRETCH_WINDOW - 1])

    def test_the_stretch_of_a_bar_is_measured_on_the_39_bars_ending_with_it(self):
        bars_3m, stretch = self.stretch()
        highs, lows, closes = list(bars_3m.high), list(bars_3m.low), list(bars_3m.close)

        for i in (38, 39, 100, 500, len(closes) - 1):
            with self.subTest(bar=i):
                start = i + 1 - STRETCH_WINDOW
                expected = stretch_from_window(
                    highs[start : i + 1],
                    lows[start : i + 1],
                    closes[start : i + 1],
                    STRETCH_EMA,
                    STRETCH_ATR,
                )
                self.assertEqual(stretch[i], expected)

    def test_nothing_after_a_candle_closes_can_change_its_stretch(self):
        bars_3m, stretch = self.stretch()
        i = 300
        close_ms = bars_3m.open_ms[i] + M3

        later = poisoned(self.pair, close_ms, self.end_ms).bars_1m
        after = self.stretch(later)[1]
        self.assertEqual(after[: i + 1], stretch[: i + 1])
        self.assertNotEqual(after[i + 1], stretch[i + 1])

        last_minute = poisoned(self.pair, close_ms - MINUTE_MS, close_ms).bars_1m
        self.assertNotEqual(self.stretch(last_minute)[1][i], stretch[i])  # its own close counts

    def test_window_and_periods_are_arguments(self):
        _, default = self.stretch()
        bars_3m, narrow = self.stretch(window=20, ema_period=10, atr_period=5)
        highs, lows, closes = list(bars_3m.high), list(bars_3m.low), list(bars_3m.close)

        self.assertEqual(narrow[:19], (None,) * 19)
        for i in (19, 250, len(closes) - 1):
            with self.subTest(bar=i):
                window = slice(i - 19, i + 1)
                expected = stretch_from_window(highs[window], lows[window], closes[window], 10, 5)
                self.assertEqual(narrow[i], expected)
        self.assertNotEqual(narrow[250], default[250])

    def test_a_flat_market_has_no_stretch(self):
        flat = minute_bars(MON, 1440, flat_price)

        bars_3m, stretch = self.stretch(flat)

        self.assertEqual(stretch, (None,) * len(bars_3m))

    def test_a_window_the_live_maths_cannot_measure_is_refused(self):
        cases = (
            ("no candles", 0, 21, 14),
            ("a negative window", -5, 21, 14),
            ("fewer candles than the EMA period", 20, 21, 14),
            ("fewer candles than the ATR needs", 14, 5, 14),
            ("an EMA period of 0", 39, 0, 14),
            ("an ATR period of 0", 39, 21, 0),
        )
        for label, window, ema_period, atr_period in cases:
            with self.subTest(label), self.assertRaises(ValueError):
                self.stretch(window=window, ema_period=ema_period, atr_period=atr_period)

        self.stretch(window=21, ema_period=21, atr_period=14)  # exactly enough
        self.stretch(window=15, ema_period=5, atr_period=14)

    def test_only_1m_bars_are_accepted(self):
        with self.assertRaises(ValueError):
            self.stretch(aggregate(self.pair.bars_1m, M3))


class PairFramesTests(WavyCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cfg = load_modes_config(REPO_CONFIG)
        cls.built = frames.build_pair_frames(cls.pair, cls.cfg)

    def other_config(self):
        """Every period the frames read, changed: none of them can be a constant of the code."""
        cfg = self.cfg
        return dataclasses.replace(
            cfg,
            pemilih_mode=dataclasses.replace(cfg.pemilih_mode, sma_cepat_1h=5),
            mode_a=dataclasses.replace(cfg.mode_a, ema_pemicu=5),
            mode_d=dataclasses.replace(
                cfg.mode_d, candle_jendela=20, ema_periode=10, atr_periode=5
            ),
        )

    def test_the_frames_hold_the_pair_and_everything_the_event_loop_walks(self):
        built, cfg = self.built, self.cfg

        self.assertEqual(built.symbol, self.pair.symbol)
        self.assertIs(built.bars_1m, self.pair.bars_1m)
        self.assertIsInstance(built.hourly, HourlyFrame)
        self.assertEqual(set(built.triggers), {"15m", "5m"})
        self.assertEqual(set(built.triggers), set(frames.TRIGGER_TIMEFRAMES))
        for tf, trigger in built.triggers.items():
            with self.subTest(tf):
                expected = frames.trigger_series(self.pair.bars_1m, tf, cfg.mode_a.ema_pemicu)
                self.assertEqual(trigger, expected)
        stretch_bars, stretch = frames.stretch_series(
            self.pair.bars_1m,
            cfg.mode_d.candle_jendela,
            cfg.mode_d.ema_periode,
            cfg.mode_d.atr_periode,
        )
        self.assertEqual(built.stretch_bars, stretch_bars)
        self.assertEqual(built.stretch, stretch)
        self.assertEqual(built.stretch_bars.interval_ms, M3)

    def test_the_frames_record_the_numbers_they_were_built_with(self):
        other = self.other_config()
        live_only = dataclasses.replace(  # numbers the event loop reads from its own config
            self.cfg, mode_a=dataclasses.replace(self.cfg.mode_a, tp_persen=0.30))

        self.assertEqual(self.built.params, frames.FrameParams.of(self.cfg))
        self.assertEqual(frames.build_pair_frames(self.pair, other).params,
                         frames.FrameParams.of(other))
        self.assertNotEqual(frames.FrameParams.of(other), frames.FrameParams.of(self.cfg))
        self.assertEqual(frames.FrameParams.of(live_only), frames.FrameParams.of(self.cfg))

    def test_the_symbol_is_the_pairs_own(self):
        eth = dataclasses.replace(self.pair, symbol="ETH-USDT")

        self.assertEqual(frames.build_pair_frames(eth, self.cfg).symbol, "ETH-USDT")

    def test_the_hourly_frame_answers_as_the_one_built_from_the_pair_and_the_selector_config(self):
        t_ms = at(WED, 10)

        built = self.built.hourly.context_at(t_ms)

        self.assertEqual(
            built, HourlyFrame.build(self.pair, self.cfg.pemilih_mode).context_at(t_ms)
        )
        self.assertIsNotNone(built.sma20_1h)

    def test_every_period_comes_from_the_config(self):
        other = self.other_config()
        t_ms = at(WED, 10)

        built = frames.build_pair_frames(self.pair, other)

        for tf, minutes in (("15m", 15), ("5m", 5)):
            with self.subTest(tf):
                closes = candle_closes(minutes, self.DAYS)
                self.assertEqual(built.triggers[tf].ema, tuple(ema(closes, 5)))
        self.assertEqual(built.stretch, frames.stretch_series(self.pair.bars_1m, 20, 10, 5)[1])
        self.assertNotEqual(built.stretch, self.built.stretch)
        hourly = HourlyFrame.build(self.pair, other.pemilih_mode).context_at(t_ms)
        self.assertEqual(built.hourly.context_at(t_ms), hourly)
        self.assertNotEqual(hourly.sma20_1h, self.built.hourly.context_at(t_ms).sma20_1h)

    def test_the_frames_are_read_only(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.built.symbol = "ETH-USDT"
        with self.assertRaises(TypeError):
            self.built.triggers["3m"] = self.built.triggers["5m"]
        with self.assertRaises(TypeError):
            del self.built.triggers["5m"]

    def test_the_fields_come_in_the_order_the_event_loop_reads_them(self):
        names = [field.name for field in dataclasses.fields(frames.PairFrames)]

        self.assertEqual(
            names, ["symbol", "bars_1m", "hourly", "triggers", "stretch_bars", "stretch", "params"]
        )


if __name__ == "__main__":
    unittest.main()
