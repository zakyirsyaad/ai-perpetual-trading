import random
import unittest
from datetime import date, datetime

from research.pine_gate.bars import bars_from_rows, wib_day
from research.pine_gate.outcomes import OutcomeCache
from research.pine_gate.simulate import (
    Market,
    Trade,
    in_entry_window,
    in_funding_blackout,
    simulate,
    trading_days,
)
from research.pine_gate.spec import WIB, ExecConfig, GuardConfig, ms

MIN = 60_000
HOUR = 60 * MIN
CFG = ExecConfig()
GUARDS = GuardConfig()
BTC = "BTC-USDT"
ETH = "ETH-USDT"
IDLE_STEP = (CFG.idle_tick_s + CFG.tick_s) * 1000  # 182 s
AFTER_EXIT = CFG.tick_s * 1000  # 180 s

# Tue 2026-09-15 08:00 WIB.
TUE_0800 = ms(datetime(2026, 9, 15, 8, 0, tzinfo=WIB))


def wib(year, month, day, hour=0, minute=0, second=0):
    return ms(datetime(year, month, day, hour, minute, second, tzinfo=WIB))


def flat_bar(t):
    """Nothing touches the bracket: every trade ends on the time stop."""
    return (t, 100.0, 100.0, 100.0, 100.0, 1.0)


def wave_bar(t):
    """Each bar spikes up 0.3%: a long hits TP and a short hits SL on the next bar."""
    return (t, 100.0, 100.3, 100.0, 100.0, 1.0)


def make_market(symbol, first_ms, end_ms, bar=wave_bar, funding=(), overrides=None):
    overrides = overrides or {}
    rows = [overrides.get(t, bar(t)) for t in range(first_ms, end_ms, MIN)]
    bars = bars_from_rows(rows, MIN)
    return Market(symbol, bars, OutcomeCache(bars, CFG), tuple(funding))


def always(side_by_symbol):
    return lambda symbol, t: side_by_symbol.get(symbol, 0)


def run(signal, markets, start, end, **kw):
    params = dict(
        exec_cfg=CFG,
        guard_cfg=GUARDS,
        latencies_ms=(),
        allow_zero_latency=True,
        seed=1,
        overshoot_bps=1.0,
    )
    params.update(kw)
    return simulate(signal, markets, start, end, **params)


class EntryBarTests(unittest.TestCase):
    def test_the_entry_bars_own_range_does_not_close_the_trade(self):
        start = TUE_0800
        order_bar = start + CFG.order_delay_s * 1000 // MIN * MIN  # latency 0: bar of t + 5 s
        wild = (order_bar, 100.0, 101.0, 99.0, 100.0, 1.0)  # crosses both levels
        btc = make_market(BTC, start - HOUR, start + 3 * HOUR, bar=flat_bar,
                          overrides={order_bar: wild})
        trades = run(always({BTC: 1}), [btc], start, start + 1)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].entry_ms, order_bar + MIN)
        self.assertEqual(trades[0].kind, "TIME")


class LatencySampleTests(unittest.TestCase):
    def test_an_empty_latency_sample_is_refused_unless_zero_latency_is_explicit(self):
        start = TUE_0800
        btc = make_market(BTC, start - HOUR, start + 2 * HOUR)
        with self.assertRaises(ValueError) as ctx:
            run(always({BTC: 1}), [btc], start, start + HOUR, allow_zero_latency=False)
        self.assertIn("latenc", str(ctx.exception))
        trades = run(always({BTC: 1}), [btc], start, start + HOUR)  # explicit: allowed
        self.assertTrue(trades)
        self.assertTrue(run(always({BTC: 1}), [btc], start, start + HOUR,
                            latencies_ms=(1000,), allow_zero_latency=False))


class TickClockTests(unittest.TestCase):
    def test_one_position_and_next_tick_180s_after_exit(self):
        start = TUE_0800
        btc = make_market(BTC, start - HOUR, start + 6 * HOUR, bar=flat_bar)
        eth = make_market(ETH, start - HOUR, start + 6 * HOUR, bar=flat_bar)

        def signal(symbol, t):
            if symbol == BTC:
                return 1 if t == start else 0
            return 1 if t > start else 0  # ETH wants in while BTC is open

        trades = run(signal, [btc, eth], start, start + 3 * HOUR)

        first = trades[0]
        self.assertEqual(first.symbol, BTC)
        self.assertEqual(first.signal_ms, start)
        self.assertEqual(first.entry_ms, start + MIN)  # order at +5 s fills at bar close
        self.assertEqual(first.kind, "TIME")
        self.assertEqual(first.exit_ms, start + 61 * MIN)
        second = trades[1]
        self.assertEqual(second.symbol, ETH)
        self.assertEqual(second.signal_ms, first.exit_ms + AFTER_EXIT)
        for a, b in zip(trades, trades[1:]):
            self.assertEqual(b.signal_ms, a.exit_ms + AFTER_EXIT)
            self.assertGreater(b.entry_ms, a.exit_ms)

    def test_btc_checked_before_eth_on_same_tick(self):
        start = TUE_0800
        btc = make_market(BTC, start - HOUR, start + 2 * HOUR)
        eth = make_market(ETH, start - HOUR, start + 2 * HOUR)
        signal = always({BTC: 1, ETH: -1})

        btc_first = run(signal, [btc, eth], start, start + HOUR)
        eth_first = run(signal, [eth, btc], start, start + HOUR)

        self.assertEqual((btc_first[0].symbol, btc_first[0].side), (BTC, 1))
        self.assertTrue(all(t.symbol == BTC for t in btc_first))
        self.assertEqual((eth_first[0].symbol, eth_first[0].side), (ETH, -1))
        self.assertEqual(btc_first[0].signal_ms, eth_first[0].signal_ms)

    def test_tick_without_entry_costs_two_seconds(self):
        start = TUE_0800
        btc = make_market(BTC, start - HOUR, start + 2 * HOUR)
        first_signal = start + 3 * IDLE_STEP
        signal = lambda symbol, t: 1 if t >= first_signal else 0

        trades = run(signal, [btc], start, start + HOUR)

        self.assertEqual(trades[0].signal_ms, first_signal)

    def test_offset_shifts_the_first_tick(self):
        start = TUE_0800
        btc = make_market(BTC, start - HOUR, start + 2 * HOUR)

        trades = run(always({BTC: 1}), [btc], start, start + HOUR, offset_s=45)

        self.assertEqual(trades[0].signal_ms, start + 45_000)

    def test_gapped_exit_ends_at_the_exit_bar_open(self):
        start = TUE_0800
        gap = (start + MIN, 99.0, 99.0, 99.0, 99.0, 1.0)  # opens through the long stop
        btc = make_market(
            BTC, start - HOUR, start + 2 * HOUR, bar=flat_bar, overrides={start + MIN: gap}
        )

        trades = run(always({BTC: 1}), [btc], start, start + 10 * MIN)

        self.assertEqual(trades[0].kind, "SL")
        self.assertEqual(trades[0].exit_ms, start + MIN)
        self.assertEqual(trades[1].signal_ms, start + MIN + AFTER_EXIT + IDLE_STEP)

    def test_entry_window_monday_0700_to_saturday_0000_wib(self):
        self.assertFalse(in_entry_window(wib(2026, 9, 14, 6, 59, 59)))
        self.assertTrue(in_entry_window(wib(2026, 9, 14, 7, 0)))
        self.assertTrue(in_entry_window(wib(2026, 9, 15, 3, 0)))
        self.assertTrue(in_entry_window(wib(2026, 9, 18, 23, 59, 59)))
        self.assertFalse(in_entry_window(wib(2026, 9, 19, 0, 0)))
        self.assertFalse(in_entry_window(wib(2026, 9, 20, 12, 0)))
        self.assertFalse(in_entry_window(wib(2026, 9, 21, 6, 59, 59)))

        monday_0650 = wib(2026, 9, 14, 6, 50)
        btc = make_market(BTC, monday_0650 - HOUR, monday_0650 + 2 * HOUR)
        trades = run(always({BTC: 1}), [btc], monday_0650, monday_0650 + HOUR)
        # 06:50:00, 06:53:02, 06:56:04, 06:59:06 idle; 07:02:08 enters.
        self.assertEqual(trades[0].signal_ms, monday_0650 + 4 * IDLE_STEP)

        saturday = wib(2026, 9, 19, 0, 0)
        monday_0710 = wib(2026, 9, 21, 7, 10)
        btc = make_market(BTC, saturday - HOUR, monday_0710 + 2 * HOUR)
        trades = run(always({BTC: 1}), [btc], saturday, monday_0710)
        self.assertTrue(trades)
        self.assertGreaterEqual(trades[0].signal_ms, wib(2026, 9, 21, 7, 0))
        self.assertTrue(all(in_entry_window(t.signal_ms) for t in trades))

    def test_blocked_entry_window_blocks_entries(self):
        start = TUE_0800
        blocked = (start + 10 * MIN, start + HOUR)
        btc = make_market(BTC, start - HOUR, start + 3 * HOUR)

        trades = run(
            always({BTC: 1}), [btc], start, start + 2 * HOUR,
            # Overlapping pieces that together cover ``blocked``.
            blocked_entry_windows=[
                (blocked[0] + 15 * MIN, blocked[1]),
                (blocked[0], blocked[0] + 20 * MIN),
            ],
        )

        self.assertTrue(any(t.signal_ms < blocked[0] for t in trades))
        self.assertFalse(any(blocked[0] <= t.signal_ms < blocked[1] for t in trades))
        after = [t for t in trades if t.signal_ms >= blocked[1]]
        self.assertLess(after[0].signal_ms, blocked[1] + IDLE_STEP)


class FundingBlackoutTests(unittest.TestCase):
    def test_funding_blackout_only_in_ten_minutes_before_funding(self):
        f = TUE_0800 + 8 * HOUR
        funding = (TUE_0800, f)
        self.assertTrue(in_funding_blackout(f - 600_000, funding))
        self.assertFalse(in_funding_blackout(f - 600_001, funding))
        self.assertTrue(in_funding_blackout(f, funding))
        self.assertFalse(in_funding_blackout(f + 1, funding))
        self.assertFalse(in_funding_blackout(f + 1, funding, 600))
        self.assertTrue(in_funding_blackout(f - 900_000, funding, 900))
        self.assertFalse(in_funding_blackout(f, ()))

        start = TUE_0800
        btc = make_market(BTC, start - HOUR, start + 2 * HOUR, funding=(start + 5 * MIN,))
        eth = make_market(ETH, start - HOUR, start + 2 * HOUR)
        trades = run(always({BTC: 1, ETH: 1}), [btc, eth], start, start + HOUR)

        self.assertEqual((trades[0].symbol, trades[0].signal_ms), (ETH, start))
        for t in trades:
            if t.symbol == ETH:
                self.assertTrue(in_funding_blackout(t.signal_ms, btc.funding_ms))
        self.assertTrue(any(t.symbol == BTC for t in trades))

    def test_market_in_blackout_alone_makes_an_idle_tick(self):
        start = TUE_0800
        btc = make_market(BTC, start - HOUR, start + 2 * HOUR, funding=(start + 5 * MIN,))

        trades = run(always({BTC: 1}), [btc], start, start + HOUR)

        # start and start+182 s are within 600 s of the funding; start+364 s is past it.
        self.assertEqual(trades[0].signal_ms, start + 2 * IDLE_STEP)


class CooldownTests(unittest.TestCase):
    def test_cooldown_five_minutes_after_entry(self):
        start = TUE_0800
        btc = make_market(BTC, start - HOUR, start + 2 * HOUR)

        trades = run(always({BTC: 1}), [btc], start, start + HOUR)

        first, second = trades[0], trades[1]
        self.assertEqual(first.entry_ms, start + MIN)
        self.assertEqual(first.exit_ms, start + 2 * MIN)  # TP on the next bar
        # start+300 s is before entry+5 min (start+360 s): idle, then +182 s.
        self.assertEqual(second.signal_ms, start + 300_000 + IDLE_STEP)
        for a, b in zip(trades, trades[1:]):
            self.assertGreaterEqual(b.signal_ms, a.entry_ms + CFG.cooldown_min * MIN)

        no_cooldown = ExecConfig(cooldown_min=0)
        btc = Market(BTC, btc.bars_1m, OutcomeCache(btc.bars_1m, no_cooldown), ())
        trades = run(always({BTC: 1}), [btc], start, start + HOUR, exec_cfg=no_cooldown)
        self.assertEqual(trades[1].signal_ms, start + 300_000)


class GuardTests(unittest.TestCase):
    def test_guard_day_rolls_at_wib_midnight(self):
        start = wib(2026, 9, 15, 21, 0)
        midnight = wib(2026, 9, 16, 0, 0)
        btc = make_market(BTC, start - HOUR, midnight + 3 * HOUR)

        trades = run(always({BTC: -1}), [btc], start, midnight + HOUR)

        tuesday = [t for t in trades if t.wib_day == date(2026, 9, 15)]
        wednesday = [t for t in trades if t.wib_day == date(2026, 9, 16)]
        self.assertEqual(len(tuesday), GUARDS.max_stops)
        self.assertTrue(all(t.kind == "SL" for t in trades))
        self.assertTrue(wednesday)
        self.assertGreaterEqual(wednesday[0].signal_ms, midnight)
        self.assertLess(wednesday[0].signal_ms, midnight + IDLE_STEP)

    def test_entry_filled_after_midnight_counts_toward_the_new_day(self):
        tick = wib(2026, 9, 15, 23, 59)  # order 23:59:05 fills at the 00:00 close
        btc = make_market(BTC, tick - HOUR, tick + 4 * HOUR)

        trades = run(
            always({BTC: 1}), [btc], tick, tick + 2 * HOUR,
            guard_cfg=GuardConfig(max_entries=1),
        )

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].wib_day, date(2026, 9, 16))

    def test_consecutive_losses_count_time_losses(self):
        start = TUE_0800
        btc = make_market(BTC, start - HOUR, start + 12 * HOUR, bar=flat_bar)
        guards = GuardConfig(max_consecutive_losses=3)

        trades = run(always({BTC: 1}), [btc], start, start + 10 * HOUR, guard_cfg=guards)

        self.assertEqual(len(trades), 3)
        self.assertTrue(all(t.kind == "TIME" and t.net_usdt < 0 for t in trades))

    def test_loss_streak_resets_at_the_wib_day_roll(self):
        start = wib(2026, 9, 15, 21, 0)
        midnight = wib(2026, 9, 16, 0, 0)
        btc = make_market(BTC, start - HOUR, midnight + 4 * HOUR, bar=flat_bar)
        guards = GuardConfig(max_consecutive_losses=2)

        trades = run(always({BTC: 1}), [btc], start, midnight + 3 * HOUR, guard_cfg=guards)

        tuesday = [t for t in trades if t.wib_day == date(2026, 9, 15)]
        wednesday = [t for t in trades if t.wib_day == date(2026, 9, 16)]
        self.assertEqual(len(tuesday), 2)  # the streak of two blocks the rest of Tuesday
        self.assertTrue(all(t.kind == "TIME" and t.net_usdt < 0 for t in trades))
        self.assertEqual(len(wednesday), 2)  # a fresh streak after midnight
        self.assertGreaterEqual(wednesday[0].signal_ms, midnight)
        self.assertLess(wednesday[0].signal_ms, midnight + IDLE_STEP)

    def test_drawdown_guard_stops_the_day(self):
        start = TUE_0800
        btc = make_market(BTC, start - HOUR, start + 3 * HOUR)
        guards = GuardConfig(max_drawdown_usdt=100.0)

        trades = run(always({BTC: -1}), [btc], start, start + 2 * HOUR, guard_cfg=guards)

        self.assertEqual(len(trades), 2)  # -1 stop is above -100, -2 stops are not
        self.assertLess(sum(t.net_usdt for t in trades), -100.0)

    def test_daily_caps_after_34_entries_or_1m_volume(self):
        start = TUE_0800
        end = wib(2026, 9, 15, 23, 0)
        btc = make_market(BTC, start - HOUR, end + 2 * HOUR)

        by_volume = run(always({BTC: 1}), [btc], start, end)
        by_entries = run(
            always({BTC: 1}), [btc], start, end,
            guard_cfg=GuardConfig(max_volume_usdt=1e12),
        )

        self.assertEqual(len(by_volume), 20)
        self.assertEqual(len(by_volume) * 2 * CFG.notional_usdt, GUARDS.max_volume_usdt)
        self.assertEqual(len(by_entries), GUARDS.max_entries)
        self.assertTrue(all(t.wib_day == date(2026, 9, 15) for t in by_entries))


class DeterminismAndFieldsTests(unittest.TestCase):
    LATENCIES = (1_000, 30_000, 64_000, 5_000, 47_000)

    def test_same_seed_same_trades(self):
        start = TUE_0800
        btc = make_market(BTC, start - HOUR, start + 4 * HOUR)
        eth = make_market(ETH, start - HOUR, start + 4 * HOUR)
        signal = always({BTC: 1, ETH: 1})

        a = run(signal, [btc, eth], start, start + 3 * HOUR, latencies_ms=self.LATENCIES, seed=7)
        b = run(signal, [btc, eth], start, start + 3 * HOUR, latencies_ms=self.LATENCIES, seed=7)
        c = run(signal, [btc, eth], start, start + 3 * HOUR, latencies_ms=self.LATENCIES, seed=8)

        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        rng = random.Random(7)
        for t in a:
            order = t.signal_ms + rng.choice(self.LATENCIES) + CFG.order_delay_s * 1000
            self.assertTrue(t.entry_ms - MIN <= order < t.entry_ms)

    def test_trade_fields_and_net(self):
        start = TUE_0800
        btc = make_market(BTC, start - HOUR, start + 2 * HOUR)

        t = run(always({BTC: 1}), [btc], start, start + 5 * MIN)[0]

        self.assertIsInstance(t, Trade)
        expected_gross = (1.002 * (1 - 0.25e-4) - 1) * 1e4
        self.assertAlmostEqual(t.gross_bps, expected_gross, places=6)
        self.assertAlmostEqual(t.net_usdt, 25_000 * (expected_gross - 7.0) / 1e4, places=6)
        self.assertEqual(t.wib_day, wib_day(t.entry_ms))
        self.assertFalse(t.both_touched)
        with self.assertRaises(Exception):
            t.kind = "SL"

        t5 = run(always({BTC: 1}), [btc], start, start + 5 * MIN, fee_pct=0.05)[0]
        self.assertAlmostEqual(t5.net_usdt, 25_000 * (expected_gross - 10.0) / 1e4, places=6)

    def test_both_touched_copied_and_overshoot_applied(self):
        start = TUE_0800
        both = (start + MIN, 100.0, 100.3, 99.7, 100.0, 1.0)
        btc = make_market(
            BTC, start - HOUR, start + 2 * HOUR, bar=flat_bar, overrides={start + MIN: both}
        )

        t = run(always({BTC: 1}), [btc], start, start + 2 * MIN, overshoot_bps=2.0)[0]

        self.assertEqual(t.kind, "SL")
        self.assertTrue(t.both_touched)
        self.assertAlmostEqual(t.gross_bps, (0.998 * (1 - 2e-4) - 1) * 1e4, places=6)

    def test_run_stops_when_data_ends(self):
        start = TUE_0800
        btc = make_market(BTC, start - HOUR, start + 30 * MIN, bar=flat_bar)
        self.assertEqual(run(always({BTC: 1}), [btc], start, start + HOUR), [])

        short_data = make_market(BTC, start - HOUR, start)  # order bar does not exist
        self.assertEqual(run(always({BTC: 1}), [short_data], start, start + HOUR), [])

    def test_rejects_side_other_than_plus_minus_one(self):
        start = TUE_0800
        btc = make_market(BTC, start - HOUR, start + HOUR)
        with self.assertRaises(ValueError):
            run(always({BTC: 2}), [btc], start, start + HOUR)


class TradingDaysTests(unittest.TestCase):
    def test_trading_days_are_mon_to_fri_wib_midnights_in_range(self):
        days = trading_days(wib(2026, 9, 14), wib(2026, 9, 22))
        self.assertEqual(
            days,
            [date(2026, 9, d) for d in (14, 15, 16, 17, 18, 21)],
        )
        # A start after midnight excludes that day; the end is exclusive.
        self.assertEqual(
            trading_days(wib(2026, 9, 14, 0, 0, 1), wib(2026, 9, 16)),
            [date(2026, 9, 15)],
        )
        self.assertEqual(trading_days(wib(2026, 9, 19), wib(2026, 9, 21)), [])


if __name__ == "__main__":
    unittest.main()
