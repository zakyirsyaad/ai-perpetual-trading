from __future__ import annotations

import unittest

from bot.telemetry.chart import VenueChart

T0 = 1_791_661_170_000  # a whole second, in epoch milliseconds


def bars(chart: VenueChart, bar: str) -> list:
    return [p for p in chart.points() if p.bar == bar]


class LiveSecondCandleTests(unittest.TestCase):
    def test_mids_inside_one_second_fold_into_one_candle(self) -> None:
        chart = VenueChart()

        chart.add_mid(100.0, T0)
        chart.add_mid(105.0, T0 + 100)
        chart.add_mid(95.0, T0 + 200)

        [candle] = bars(chart, "1s")
        self.assertEqual((candle.open, candle.high, candle.low, candle.close), (100.0, 105.0, 95.0, 95.0))
        self.assertEqual(candle.ts, T0)

    def test_the_next_second_opens_at_the_previous_close(self) -> None:
        chart = VenueChart()
        chart.add_mid(100.0, T0)
        chart.add_mid(98.0, T0 + 1000)

        first, second = bars(chart, "1s")

        self.assertEqual(first.close, 100.0)
        self.assertEqual(second.open, 100.0)
        self.assertEqual(second.close, 98.0)

    def test_an_unchanged_mid_does_not_rebuild_the_tape(self) -> None:
        chart = VenueChart()
        chart.add_mid(100.0, T0)
        before = chart.points()

        chart.add_mid(100.0, T0 + 50)

        self.assertIs(chart.points(), before)

    def test_only_the_last_15_minutes_of_seconds_are_kept(self) -> None:
        chart = VenueChart()
        for i in range(905):
            chart.add_mid(100.0 + i, T0 + i * 1000)

        seconds = bars(chart, "1s")

        self.assertEqual(len(seconds), 900)
        self.assertEqual(seconds[-1].ts, T0 + 904 * 1000)

    def test_invalid_mids_are_ignored(self) -> None:
        chart = VenueChart()

        chart.add_mid(0.0, T0)
        chart.add_mid(100.0, 0)

        self.assertEqual(chart.points(), [])


class VenueCandleTests(unittest.TestCase):
    def test_klines_load_as_minute_and_15_minute_points(self) -> None:
        chart = VenueChart()
        rows = [{"timestamp": float(T0), "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0, "volume": 5.0}]

        chart.load_klines("1m", rows)
        chart.load_klines("15m", rows)

        [minute] = bars(chart, "1m")
        [quarter] = bars(chart, "15m")
        self.assertEqual((minute.open, minute.high, minute.low, minute.close), (10.0, 12.0, 9.0, 11.0))
        self.assertEqual(quarter.ts, T0)

    def test_a_candle_with_the_same_time_is_replaced(self) -> None:
        chart = VenueChart()
        chart.upsert_candle("1m", T0, 10.0, 12.0, 9.0, 11.0)

        chart.upsert_candle("1m", T0, 10.0, 13.0, 9.0, 12.5)

        [minute] = bars(chart, "1m")
        self.assertEqual((minute.high, minute.close), (13.0, 12.5))

    def test_re_sending_an_identical_candle_does_not_rebuild_the_tape(self) -> None:
        chart = VenueChart()
        chart.upsert_candle("1m", T0, 10.0, 12.0, 9.0, 11.0)
        before = chart.points()

        chart.upsert_candle("1m", T0, 10.0, 12.0, 9.0, 11.0)

        self.assertIs(chart.points(), before)

    def test_unusable_candles_are_ignored(self) -> None:
        chart = VenueChart()

        chart.upsert_candle("1m", 0, 10.0, 12.0, 9.0, 11.0)
        chart.upsert_candle("1m", T0, 10.0, 12.0, 9.0, 0.0)

        self.assertEqual(chart.points(), [])

    def test_missing_open_high_low_fall_back_to_the_close(self) -> None:
        chart = VenueChart()

        chart.upsert_candle("1m", T0, 0.0, 0.0, 0.0, 11.0)

        [minute] = bars(chart, "1m")
        self.assertEqual((minute.open, minute.high, minute.low, minute.close), (11.0, 11.0, 11.0, 11.0))

    def test_old_minute_candles_are_dropped_past_the_cap(self) -> None:
        chart = VenueChart(max_minute_bars=3)
        for i in range(5):
            chart.upsert_candle("1m", T0 + i * 60_000, 10.0, 11.0, 9.0, 10.0)

        self.assertEqual([p.ts for p in bars(chart, "1m")], [T0 + 2 * 60_000, T0 + 3 * 60_000, T0 + 4 * 60_000])


class FillMarkTests(unittest.TestCase):
    def test_a_fill_becomes_a_point_with_its_details(self) -> None:
        chart = VenueChart()

        added = chart.add_fill(ts=T0, side="buy", price=100.0, size=0.5, direction="open", closed_pnl=None, fee_usd=0.01)

        self.assertTrue(added)
        [point] = [p for p in chart.points() if p.fill]
        self.assertEqual((point.ts, point.mid), (T0, 100.0))
        self.assertEqual((point.fill.side, point.fill.size, point.fill.direction, point.fill.fee_usd), ("buy", 0.5, "open", 0.01))

    def test_the_same_fill_is_only_counted_once(self) -> None:
        chart = VenueChart()
        chart.add_fill(ts=T0, side="buy", price=100.0, size=0.5)

        self.assertFalse(chart.add_fill(ts=T0, side="buy", price=100.0, size=0.5))
        self.assertEqual(len([p for p in chart.points() if p.fill]), 1)

    def test_fills_with_unusable_time_or_price_are_rejected(self) -> None:
        chart = VenueChart()

        self.assertFalse(chart.add_fill(ts=0, side="buy", price=100.0, size=0.5))
        self.assertFalse(chart.add_fill(ts=T0, side="buy", price=0.0, size=0.5))


class OrderingTests(unittest.TestCase):
    def test_points_are_time_sorted_with_fills_after_candles_and_15m_first(self) -> None:
        chart = VenueChart()
        chart.upsert_candle("1m", T0, 10.0, 11.0, 9.0, 10.0)
        chart.upsert_candle("15m", T0, 10.0, 11.0, 9.0, 10.0)
        chart.add_fill(ts=T0, side="sell", price=10.0, size=1.0)
        chart.add_mid(10.0, T0 - 5000)

        order = [(p.ts, "fill" if p.fill else p.bar) for p in chart.points()]

        self.assertEqual(order, [(T0 - 5000, "1s"), (T0, "15m"), (T0, "1m"), (T0, "fill")])


if __name__ == "__main__":
    unittest.main()
