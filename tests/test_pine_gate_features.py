import random
import unittest
from array import array
from dataclasses import fields
from datetime import datetime, timezone

from bot.arena.setup_profile import build_setup_profile
from bot.strategy import indicators as live
from bot.strategy import pine_indicators as pine
from research.pine_gate.bars import (
    BarGapError,
    Bars,
    aggregate,
    bars_from_rows,
    concat,
    slice_until,
)
from research.pine_gate.features import FeatureFrame, Snapshot, htf_agreement, htf_bars
from research.pine_gate.spec import ms

MIN = 60_000
M3 = 3 * MIN
M5 = 5 * MIN
HOUR = 60 * MIN
H4 = 4 * HOUR
DAY = 24 * HOUR

# 17:00 UTC is hour-aligned but not 4h-aligned, so the first 4h bucket is partial.
START_1M = ms(datetime(2025, 7, 31, 17, 0, tzinfo=timezone.utc))
WARMUP_DAYS = 80
SYNTH_DAYS = 3


def walk_rows(rng, start_ms, interval_ms, count, price, step):
    rows = []
    for k in range(count):
        o = price
        c = o * (1.0 + rng.gauss(0.0, step))
        h = max(o, c) * (1.0 + abs(rng.gauss(0.0, step / 2)))
        low = min(o, c) * (1.0 - abs(rng.gauss(0.0, step / 2)))
        rows.append((start_ms + k * interval_ms, o, h, low, c, rng.uniform(1.0, 100.0)))
        price = c
    return rows


def make_dataset(seed=11, days=SYNTH_DAYS):
    """Native 1h/4h warm-up ending at START_1M, then ``days`` of 1m bars."""
    rng = random.Random(seed)
    hours = WARMUP_DAYS * 24
    rows_1h = walk_rows(rng, START_1M - hours * HOUR, HOUR, hours, 60_000.0, 0.004)
    warmup_1h = bars_from_rows(rows_1h, HOUR)
    rows_1m = walk_rows(rng, START_1M, MIN, days * 1440, rows_1h[-1][4], 0.0006)
    bars_1m = bars_from_rows(rows_1m, MIN)
    # Native 4h bars up to the one straddling START_1M (16:00-20:00 UTC), built
    # from the same prices so they agree with the 1m data.
    all_4h = aggregate(concat(warmup_1h, aggregate(bars_1m, HOUR)), H4)
    warmup_4h = slice_until(all_4h, START_1M + H4 - 1)
    return bars_1m, warmup_1h, warmup_4h


def replace_bars(bars, start_ms, end_ms, transform):
    """Copy of ``bars`` with every bar opening in [start_ms, end_ms) transformed."""
    cols = {name: array(getattr(bars, name).typecode) for name in
            ("open_ms", "open", "high", "low", "close", "volume")}
    for i in range(len(bars)):
        row = (bars.open_ms[i], bars.open[i], bars.high[i], bars.low[i],
               bars.close[i], bars.volume[i])
        if start_ms <= row[0] < end_ms:
            row = transform(row)
        for name, value in zip(cols, row):
            cols[name].append(value)
    return Bars(bars.interval_ms, **cols)


def extreme(row):
    t, o, h, low, c, v = row
    return (t, o * 10.0, h * 20.0, low * 0.05, c * 15.0, v * 1000.0)


def flat_rows(start_ms, prices_and_volumes):
    return [
        (start_ms + k * MIN, p, p, p, p, v) for k, (p, v) in enumerate(prices_and_volumes)
    ]


SNAPSHOT_FIELDS = tuple(f.name for f in fields(Snapshot) if f.name != "t_ms")


class SyntheticFrameTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars, cls.w1h, cls.w4h = make_dataset()
        cls.full = FeatureFrame.build("BTC-USDT", cls.bars, cls.w1h, cls.w4h)
        cls.end = START_1M + SYNTH_DAYS * DAY

    def test_properties(self):
        self.assertEqual(self.full.symbol, "BTC-USDT")
        self.assertIs(self.full.bars_1m, self.bars)

    def test_snapshot_ignores_forming_hour_bar(self):
        t = START_1M + DAY + (17 * 60 + 30) * MIN  # 10:30 UTC
        self.assertEqual(t % HOUR, 30 * MIN)
        hour_open = t - 30 * MIN
        before = self.full.snapshot(t)
        self.assertIsNotNone(before.h1_ema20)
        self.assertIsNotNone(before.h4_ema50)
        self.assertIsNotNone(before.h1_vol_pct)

        # The not-yet-closed part of the forming hour: nothing at t may change.
        future = replace_bars(self.bars, t, hour_open + HOUR, extreme)
        frame = FeatureFrame.build("BTC-USDT", future, self.w1h, self.w4h)
        self.assertEqual(frame.snapshot(t), before)
        self.assertEqual(frame.poc_24h(t), self.full.poc_24h(t))

        # The whole forming hour: 5m fields may change, 1h and 4h fields may not.
        whole = replace_bars(self.bars, hour_open, hour_open + HOUR, extreme)
        after = FeatureFrame.build("BTC-USDT", whole, self.w1h, self.w4h).snapshot(t)
        self.assertNotEqual(after.close_5m, before.close_5m)
        for name in SNAPSHOT_FIELDS:
            if name.startswith(("h1_", "h4_")):
                self.assertEqual(getattr(after, name), getattr(before, name), name)

    def test_truncation_invariance_at_random_times(self):
        rng = random.Random(2026)
        times = [rng.randrange(START_1M, self.end + HOUR) for _ in range(20)]
        for interval, k in ((M5, 300), (M3, 500), (HOUR, 40), (H4, 9), (MIN, 1700)):
            times.append(START_1M + k * interval)  # exactly at a bar close
        boundary_5m = START_1M + DAY + 7 * M5
        boundary_1h = START_1M + 2 * DAY + 5 * HOUR
        times.extend((boundary_5m - 1, boundary_5m + 1, boundary_1h - 1, boundary_1h + 1,
                      boundary_1h))
        self.assertEqual(len(times), 30)

        populated = {name: 0 for name in SNAPSHOT_FIELDS}
        pocs = 0
        for t in times:
            truncated = FeatureFrame.build(
                "BTC-USDT", slice_until(self.bars, t), self.w1h, self.w4h
            )
            expected = self.full.snapshot(t)
            self.assertEqual(truncated.snapshot(t), expected, t)
            self.assertEqual(truncated.poc_24h(t), self.full.poc_24h(t), t)
            pocs += self.full.poc_24h(t) is not None
            for name in SNAPSHOT_FIELDS:
                populated[name] += getattr(expected, name) is not None

        # Guard against a vacuous comparison of None with None.
        self.assertGreater(pocs, 5)
        for name, count in populated.items():
            if name in ("ema200_5m", "ema125_5m"):
                continue  # 1,600 / 1,000 5m bars of warm-up exceed 3 days
            self.assertGreater(count, 10, name)

    def test_warmup_bars_feed_htf_before_first_minute_bar(self):
        t = START_1M + 2 * HOUR
        with_warmup = self.full.snapshot(t)
        without = FeatureFrame.build("BTC-USDT", self.bars).snapshot(t)
        self.assertIsNone(without.h1_ema20)
        self.assertIsNone(without.h4_ema20)
        self.assertIsNone(without.h1_vol_pct)

        closes_1h = list(self.w1h.close) + list(aggregate(slice_until(self.bars, t), HOUR).close)
        self.assertEqual(with_warmup.h1_ema20, pine.ema(closes_1h, 20)[-1])
        self.assertEqual(with_warmup.h1_ema50, pine.ema(closes_1h, 50)[-1])

        # 4h: 1m data starts mid-bucket (17:00 UTC), so aggregation drops the
        # partial 16:00 bucket and the native 16:00 bar fills it, read from 20:00.
        first_agg_4h = START_1M + 3 * HOUR
        agg_4h = aggregate(self.bars, H4)
        self.assertEqual(agg_4h.open_ms[0], first_agg_4h)
        self.assertEqual(self.w4h.open_ms[-1], START_1M - HOUR)
        native_4h = list(self.w4h.close)
        for when in (first_agg_4h - 1, first_agg_4h, first_agg_4h + 2 * HOUR,
                     first_agg_4h + H4 - 1):
            expected = native_4h if when >= first_agg_4h else native_4h[:-1]
            s = self.full.snapshot(when)
            self.assertEqual(s.h4_ema20, pine.ema(expected, 20)[-1], when)
        after = self.full.snapshot(first_agg_4h + H4)
        self.assertEqual(after.h4_ema50, pine.ema(native_4h + [agg_4h.close[0]], 50)[-1])

        # Warm-up bars at or after the first aggregated open are dropped.
        overlap_1h = bars_from_rows(
            [(START_1M + k * HOUR, 1.0, 1.0, 1.0, 1.0, 1.0) for k in range(5)], HOUR
        )
        overlap_4h = bars_from_rows(
            [(first_agg_4h + k * H4, 1.0, 1.0, 1.0, 1.0, 1.0) for k in range(3)], H4
        )
        noisy = FeatureFrame.build(
            "BTC-USDT", self.bars, concat(self.w1h, overlap_1h), concat(self.w4h, overlap_4h)
        )
        for when in (t, first_agg_4h, first_agg_4h + H4, START_1M + DAY):
            self.assertEqual(noisy.snapshot(when), self.full.snapshot(when))

    def test_native_bar_fills_partial_leading_hour(self):
        late = START_1M + 30 * MIN  # 1m data starts at 17:30 UTC
        bars = _from(self.bars, late)
        first_agg_1h = START_1M + HOUR
        self.assertEqual(aggregate(bars, HOUR).open_ms[0], first_agg_1h)
        native_17 = aggregate(slice_until(self.bars, first_agg_1h), HOUR)
        w1h = concat(self.w1h, _from(native_17, START_1M))  # native bars incl. 17:00
        frame = FeatureFrame.build("BTC-USDT", bars, w1h, self.w4h)

        native = list(w1h.close)
        agg_1h = aggregate(bars, HOUR)
        for when in (first_agg_1h - 1, first_agg_1h, first_agg_1h + 30 * MIN,
                     first_agg_1h + HOUR - 1):
            expected = native if when >= first_agg_1h else native[:-1]
            self.assertEqual(frame.snapshot(when).h1_ema20, pine.ema(expected, 20)[-1], when)
        self.assertEqual(frame.snapshot(first_agg_1h + HOUR).h1_ema50,
                         pine.ema(native + [agg_1h.close[0]], 50)[-1])

        garbage = bars_from_rows(
            [(first_agg_1h + k * HOUR, 1.0, 1.0, 1.0, 1.0, 1.0) for k in range(4)], HOUR
        )
        noisy = FeatureFrame.build("BTC-USDT", bars, concat(w1h, garbage), self.w4h)
        for when in (first_agg_1h, first_agg_1h + 2 * HOUR, START_1M + DAY):
            self.assertEqual(noisy.snapshot(when), frame.snapshot(when))
            truncated = FeatureFrame.build("BTC-USDT", slice_until(bars, when), w1h, self.w4h)
            self.assertEqual(truncated.snapshot(when), frame.snapshot(when))

    def test_stretch_matches_bot_setup_profile(self):
        t = START_1M + DAY + 37 * MIN
        bars_3m = aggregate(slice_until(self.bars, t), M3)
        closes = list(bars_3m.close)[-39:]
        highs = list(bars_3m.high)[-39:]
        lows = list(bars_3m.low)[-39:]
        expected = build_setup_profile(
            {
                "last_price": closes[-1],
                "ema_21": live.ema(closes, 21),
                "atr_14": live.atr(highs, lows, closes, 14),
            },
            "LONG",
        )["stretch_atr"]
        self.assertIsNotNone(expected)
        self.assertEqual(self.full.snapshot(t).stretch_3m, expected)

        # 38 closed 3m bars are not enough; 39 are.
        self.assertIsNone(self.full.snapshot(START_1M + 38 * M3).stretch_3m)
        self.assertIsNotNone(self.full.snapshot(START_1M + 39 * M3).stretch_3m)

    def test_fields_read_last_closed_bars(self):
        t = START_1M + DAY + 2 * HOUR + 7 * MIN
        s = self.full.snapshot(t)
        b5 = aggregate(slice_until(self.bars, t), M5)
        highs, lows, closes = list(b5.high), list(b5.low), list(b5.close)
        self.assertEqual(s.t_ms, t)
        self.assertEqual(b5.open_ms[-1] + M5, t - 2 * MIN)
        self.assertEqual(s.close_5m, closes[-1])
        self.assertEqual(s.atr14_5m, pine.atr(highs, lows, closes, 14)[-1])
        self.assertEqual(s.rsi14_5m, pine.rsi(closes, 14)[-1])
        self.assertEqual(s.rsi5_5m, pine.rsi(closes, 5)[-1])
        plus, minus, adx = pine.dmi(highs, lows, closes, 14, 14)
        self.assertEqual((s.di_plus14_5m, s.di_minus14_5m, s.adx14_5m),
                         (plus[-1], minus[-1], adx[-1]))
        self.assertEqual(s.adx11_5m, pine.dmi(highs, lows, closes, 11, 11)[2][-1])
        self.assertEqual(s.high12_prev, max(highs[-13:-1]))
        self.assertEqual(s.low12_prev, min(lows[-13:-1]))
        self.assertEqual(s.high24_prev, max(highs[-25:-1]))
        self.assertEqual(s.low24_prev, min(lows[-25:-1]))

        h1 = concat(self.w1h, aggregate(slice_until(self.bars, t), HOUR))
        atr_1h = pine.atr(list(h1.high), list(h1.low), list(h1.close), 14)
        ratios = [a / c for a, c in zip(atr_1h, h1.close) if a is not None]
        current = ratios[-1]
        expected_pct = 100.0 * sum(1 for r in ratios[-721:-1] if r <= current) / 720
        self.assertEqual(s.h1_atr14, atr_1h[-1])
        self.assertEqual(s.h1_vol_pct, expected_pct)
        h4 = concat(self.w4h, aggregate(slice_until(self.bars, t), H4))
        self.assertEqual(s.h4_atr14, pine.atr(list(h4.high), list(h4.low), list(h4.close), 14)[-1])
        self.assertEqual(s.h4_ema50, pine.ema(list(h4.close), 50)[-1])

    def test_breakout_levels_exclude_last_closed_bar(self):
        b5 = aggregate(self.bars, M5)
        highs, lows = list(b5.high), list(b5.low)
        self.assertIsNone(self.full.snapshot(b5.open_ms[11] + M5).high12_prev)
        self.assertIsNotNone(self.full.snapshot(b5.open_ms[12] + M5).high12_prev)
        self.assertIsNone(self.full.snapshot(b5.open_ms[23] + M5).low24_prev)
        for last in range(24, len(b5), 7):
            s = self.full.snapshot(b5.open_ms[last] + M5)
            self.assertEqual(s.high12_prev, max(highs[last - 12:last]))
            self.assertEqual(s.low12_prev, min(lows[last - 12:last]))
            self.assertEqual(s.high24_prev, max(highs[last - 24:last]))
            self.assertEqual(s.low24_prev, min(lows[last - 24:last]))

    def test_long_emas_after_their_warmup(self):
        bars, w1h, w4h = make_dataset(seed=3, days=7)
        frame = FeatureFrame.build("ETH-USDT", bars, w1h, w4h)
        t = START_1M + 7 * DAY
        closes = list(aggregate(bars, M5).close)
        s = frame.snapshot(t)
        self.assertEqual(s.ema200_5m, pine.ema(closes, 200)[-1])
        self.assertEqual(s.ema125_5m, pine.ema(closes, 125)[-1])
        self.assertIsNotNone(s.ema200_5m)
        self.assertIsNone(frame.snapshot(START_1M + 1599 * M5).ema200_5m)
        self.assertIsNotNone(frame.snapshot(START_1M + 1600 * M5).ema200_5m)

    def test_early_times_give_empty_snapshot(self):
        for t in (0, START_1M - 1, START_1M, START_1M + MIN):
            s = self.full.snapshot(t)
            self.assertEqual(s.t_ms, t)
            self.assertIsNone(s.close_5m)
            self.assertIsNone(s.stretch_3m)
            self.assertIsNone(self.full.poc_24h(t))
        empty = FeatureFrame.build("BTC-USDT", slice_until(self.bars, START_1M))
        self.assertIsNone(empty.snapshot(self.end).h1_ema20)
        self.assertIsNone(empty.poc_24h(self.end))

    def test_build_validates_intervals(self):
        with self.assertRaises(ValueError):
            FeatureFrame.build("BTC-USDT", aggregate(self.bars, M5))
        with self.assertRaises(ValueError):
            FeatureFrame.build("BTC-USDT", self.bars, warmup_1h=self.w4h)
        with self.assertRaises(ValueError):
            FeatureFrame.build("BTC-USDT", self.bars, warmup_4h=self.w1h)

    def test_warmup_alone_feeds_htf_without_minute_bars(self):
        empty = slice_until(self.bars, START_1M)
        frame = FeatureFrame.build("BTC-USDT", empty, self.w1h, self.w4h)
        self.assertEqual(frame.snapshot(START_1M), self.full.snapshot(START_1M))
        self.assertIsNotNone(frame.snapshot(START_1M).h1_vol_pct)

    def test_snapshot_is_frozen(self):
        s = self.full.snapshot(self.end)
        with self.assertRaises(AttributeError):
            s.close_5m = 1.0

    def test_poc_requires_full_24h_window(self):
        first_full = START_1M + DAY
        self.assertIsNone(self.full.poc_24h(first_full - 1))
        self.assertIsNotNone(self.full.poc_24h(first_full))

        missing = START_1M + DAY + 3 * HOUR
        gapped = concat(slice_until(self.bars, missing), _from(self.bars, missing + MIN))
        frame = FeatureFrame.build("BTC-USDT", gapped, self.w1h, self.w4h)
        self.assertIsNotNone(frame.poc_24h(missing))
        self.assertIsNone(frame.poc_24h(missing + 10 * MIN))
        self.assertIsNone(frame.poc_24h(missing + DAY))
        self.assertIsNotNone(frame.poc_24h(missing + DAY + 5 * MIN))

    def test_poc_is_fixed_within_a_5m_bar(self):
        t = START_1M + 2 * DAY + 10 * M5
        self.assertEqual(self.full.poc_24h(t), self.full.poc_24h(t + 4 * MIN + 59_999))


def _from(bars, t_ms):
    start = list(bars.open_ms).index(t_ms)
    return Bars(bars.interval_ms, bars.open_ms[start:], bars.open[start:],
                bars.high[start:], bars.low[start:], bars.close[start:],
                bars.volume[start:])


POC_START = ms(datetime(2026, 1, 1, tzinfo=timezone.utc))
POC_T = POC_START + DAY
WIDTH = 100.0 * 5.0 / 10_000  # 5 bps of the last closed 5m close (100.0)


def poc_frame(head):
    """1,440 bars: ``head`` then flat bars at 100.0 with a little volume."""
    tail = [(100.0, 0.1)] * (1440 - len(head))
    return FeatureFrame.build("BTC-USDT", bars_from_rows(flat_rows(POC_START, head + tail), MIN))


def centre(price):
    return (int(price // WIDTH) + 0.5) * WIDTH


class PocTest(unittest.TestCase):
    def test_poc_busiest_bin_and_ties_go_to_nearest_price(self):
        busiest = poc_frame([(99.01, 1.0)] * 600 + [(101.01, 1.0)] * 800)
        self.assertEqual(busiest.snapshot(POC_T).close_5m, 100.0)
        self.assertAlmostEqual(busiest.poc_24h(POC_T), 101.025, places=9)

        # Equal volume: 99.025 is 0.975 from 100, 101.025 is 1.025 away.
        tie_low = poc_frame([(99.01, 1.0)] * 700 + [(101.01, 1.0)] * 700)
        self.assertAlmostEqual(tie_low.poc_24h(POC_T), centre(99.01), places=9)
        self.assertAlmostEqual(tie_low.poc_24h(POC_T), 99.025, places=9)
        # Mirrored: 98.975 is 1.025 away, 100.975 is 0.975 away.
        tie_high = poc_frame([(98.97, 1.0)] * 700 + [(100.99, 1.0)] * 700)
        self.assertAlmostEqual(tie_high.poc_24h(POC_T), 100.975, places=9)

    def test_volume_spreads_evenly_over_covered_bins(self):
        wide = [(POC_START + k * MIN, 99.53, 99.69, 99.51, 99.53, 4.0) for k in range(300)]
        flat = [(POC_START + (300 + k) * MIN, 101.01, 101.01, 101.01, 101.01, 1.0)
                for k in range(350)]
        rest = [(POC_START + (650 + k) * MIN, 100.0, 100.0, 100.0, 100.0, 0.1)
                for k in range(790)]
        frame = FeatureFrame.build("BTC-USDT", bars_from_rows(wide + flat + rest, MIN))
        # 99.51..99.69 covers bins 1990..1993: 300 per bin, below the 350 at 101.01.
        self.assertAlmostEqual(frame.poc_24h(POC_T), 101.025, places=9)

        # 99.51..99.64 covers 1990..1992 (both end bins): 400 each, so the tie goes
        # to 1992, the bin nearest the price.
        narrower = [(t, o, 99.64, low, c, v) for t, o, h, low, c, v in wide]
        frame = FeatureFrame.build("BTC-USDT", bars_from_rows(narrower + flat + rest, MIN))
        self.assertAlmostEqual(frame.poc_24h(POC_T), 99.625, places=9)
        # 99.51..99.56 covers 1990..1991: 600 each, 1991 is nearer.
        two_bins = [(t, o, 99.56, low, c, v) for t, o, h, low, c, v in wide]
        frame = FeatureFrame.build("BTC-USDT", bars_from_rows(two_bins + flat + rest, MIN))
        self.assertAlmostEqual(frame.poc_24h(POC_T), centre(99.56), places=9)
        self.assertAlmostEqual(centre(99.56), 99.575, places=9)

    def test_zero_volume_gives_none(self):
        frame = poc_frame([(100.0, 0.0)] * 1440)
        self.assertIsNotNone(frame.snapshot(POC_T).close_5m)
        self.assertIsNone(frame.poc_24h(POC_T))


class HtfAgreementTest(unittest.TestCase):
    def setUp(self):
        self.native = bars_from_rows(walk_rows(random.Random(1), POC_START, HOUR, 10,
                                               100.0, 0.01), HOUR)

    def test_identical_bars_agree_fully(self):
        self.assertEqual(htf_agreement(self.native, self.native), 1.0)

    def test_share_of_overlapping_bars_that_agree(self):
        changed = replace_bars(self.native, POC_START + 2 * HOUR, POC_START + 3 * HOUR,
                               lambda r: (r[0], r[1], r[2], r[3], r[4] * 1.001, r[5]))
        self.assertEqual(htf_agreement(self.native, changed), 0.9)
        tiny = replace_bars(self.native, POC_START, POC_START + HOUR,
                            lambda r: (r[0], r[1], r[2], r[3], r[4], r[5] * (1 + 1e-8)))
        self.assertEqual(htf_agreement(self.native, tiny), 1.0)
        later = _from(self.native, POC_START + 6 * HOUR)
        self.assertEqual(htf_agreement(self.native, later), 1.0)

    def test_no_overlap_raises(self):
        head = slice_until(self.native, POC_START + 3 * HOUR)
        tail = _from(self.native, POC_START + 5 * HOUR)
        with self.assertRaises(ValueError):
            htf_agreement(head, tail)
        with self.assertRaises(ValueError):
            htf_agreement(self.native, aggregate(self.native, H4))


class HtfSeamTest(unittest.TestCase):
    """The native warm-up must end exactly where the aggregated bars begin (C7)."""

    def test_a_hole_at_the_warmup_seam_raises(self):
        bars, w1h, w4h = make_dataset(days=1)
        self.assertEqual(len(htf_bars(bars, w1h, HOUR)), len(w1h) + 24)
        self.assertGreater(len(htf_bars(bars, w4h, H4)), len(w4h))
        early = slice_until(w1h, START_1M - 3 * HOUR)  # last warm-up bar opens 4 h early
        with self.assertRaises(BarGapError) as ctx:
            htf_bars(bars, early, HOUR)
        self.assertIn(str(START_1M), str(ctx.exception))

    def test_series_lists_every_bar_series_the_frame_reads(self):
        bars, w1h, w4h = make_dataset(days=1)
        frame = FeatureFrame.build("BTC-USDT", bars, w1h, w4h)
        series = frame.series()
        self.assertIs(series[0], bars)
        self.assertEqual([s.interval_ms for s in series], [MIN, M5, HOUR, H4, M3])


if __name__ == "__main__":
    unittest.main()
