"""Tests for research.mode_backtest.simulate (loop part): ``simulate_run``, the event loop of Mode A
and Mode D (spec section 9.1, decisions D2, D4, D5 and D10).

The frames of a test are built by hand. Mode A's trigger candles are written out against an EMA20
of 100, and the stub hourly frame answers with a context whose SMA20 (1h) is 100 and ATR14 (1h) is
1, so the highest close that may still confirm is 101. Mode D's 3m candles carry a stretch written
out per candle. The 1m bars are a separate series, flat at 100.0 except the bars a test edits: the
loop reads the trigger candles (or the stretch) for a signal and the 1m bars only for the entry and
the walk, so the two need not agree. A LONG filled at 100.01 has its take-profit at 100.41004 and
its stop at 99.759975; a SHORT filled at 99.99 has them at 99.79002 and 100.18998. A flat bar at
100.0 reaches neither, so a position that meets no edited bar ends on its time limit.

Times are WIB wall-clock readings in the week of Tuesday 30 June 2026, inside and around the weekly
window ``mon 07:00-sat 00:00``. Every number comes from ``config/modes.yaml``; a test that needs
another one replaces it with ``dataclasses.replace``. Expected trades are built with the committed
fill, bracket and money functions from arguments the test chooses, so a test checks what the loop
passes to them, never the arithmetic itself (``tests.test_mode_backtest_simulate`` does that).
"""

import dataclasses
import unittest
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType

from bot.strategy.modes.config import load_modes_config
from bot.strategy.modes.position_rules import LONG, SHORT, adverse_fill, bracket_prices
from bot.strategy.modes.regime import NAIK, SIDEWAYS, TIDAK_ENTRY, TURUN, pilih_mode
from research.mode_backtest.frames import FrameParams, HourlyContext, PairFrames, TriggerSeries
from research.mode_backtest.simulate import (
    BLOCK_REASONS,
    EXIT_SL,
    EXIT_TP,
    EXIT_WAKTU,
    MODE_A,
    MODE_D,
    RunResult,
    RunSpec,
    Trade,
    settle,
    simulate_run,
)
from research.pine_gate.bars import bars_from_rows, concat, wib_day_start_ms
from tests.mode_backtest_helpers import MINUTE_MS

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config" / "modes.yaml"
CFG = load_modes_config(REPO_CONFIG)
PAIR = "BTC-USDT"

M3, M5, M15 = 3 * MINUTE_MS, 5 * MINUTE_MS, 15 * MINUTE_MS
LIMIT_A, LIMIT_D = 90 * MINUTE_MS, 60 * MINUTE_MS  # the time limits of config/modes.yaml

TUE = date(2026, 6, 30)  # a Tuesday
WED, THU, FRI, SAT, SUN, MON = (TUE + timedelta(days=k) for k in range(1, 7))


def at(day, hour=0, minute=0):
    """UTC milliseconds of a WIB wall-clock reading."""
    return wib_day_start_ms(day) + (hour * 60 + minute) * MINUTE_MS


def minutes_after(t_ms, n):
    return t_ms + n * MINUTE_MS


# --- the hourly condition -------------------------------------------------------------------------

SMA50_OFFSET = {NAIK: -1.0, TURUN: 1.0, SIDEWAYS: 0.1, TIDAK_ENTRY: None}


def context(kondisi=NAIK, atr14=1.0, median=1.0, sma20=100.0):
    """A context the selector gives for ``kondisi``: the SMA50 (1h) is placed for it."""
    offset = SMA50_OFFSET[kondisi]
    sma50 = None if offset is None else sma20 + offset
    return HourlyContext(sma20, sma50, atr14, median, 100.5, 100.0, kondisi)


def fixed(ctx):
    return lambda t_ms: ctx


class StubHourly:
    """The hourly frame of a test: it has ``context_at`` and nothing else, so the loop can read the
    condition no other way. ``asked`` lists every time it was asked, in order."""

    def __init__(self, answer=None):
        self._answer = answer or fixed(context())
        self.asked = []

    def context_at(self, t_ms):
        self.asked.append(t_ms)
        return self._answer(t_ms)


# --- Mode A's trigger candles: (open, high, low, close) against an EMA of 100 ---------------------

EMA = 100.0
QUIET = (100.6, 100.7, 100.5, 100.55)  # red and above the EMA: neither a touch nor a confirmation
TOUCH = (100.3, 100.4, 99.9, 100.2)  # red and reaching the EMA: a touch only
CONFIRM = (100.2, 100.6, 100.1, 100.5)  # green above the EMA, no touch: confirms an open touch
SIGNAL = (99.95, 100.6, 99.9, 100.5)  # a touch and a confirmation in one candle
TOO_HIGH = (101.0, 101.6, 100.9, 101.5)  # green above the EMA, but above SMA20 + 1 ATR (101)


def with_ema(bar, ema):
    """A candle judged against its own EMA instead of 100 (None: no EMA yet)."""
    return (*bar, ema)


def candles(first_close, last_close, kinds, step=M15):
    """Trigger candles closing every ``step`` from ``first_close`` to ``last_close``, both included.

    A candle is QUIET with an EMA of 100 unless ``kinds`` maps its close to a bar, or to a bar
    followed by its own EMA."""
    closes = range(first_close, last_close + 1, step)
    stray = sorted(set(kinds) - set(closes))
    if stray:
        raise AssertionError(f"candles named outside the series: {stray}")
    rows, emas = [], []
    for close in closes:
        kind = kinds.get(close, QUIET)
        rows.append((close - step, *kind[:4], 1.0))
        emas.append(kind[4] if len(kind) == 5 else EMA)
    return TriggerSeries(bars_from_rows(rows, step), tuple(emas))


# --- Mode D's 3m candles and their stretch --------------------------------------------------------

FAR = 5.0  # a stretch far from the mean: no Mode D signal


def stretch(first_close, last_close, values):
    """3m candles closing every 3 minutes, both ends included, with a stretch of FAR unless
    ``values`` maps the close to another one."""
    closes = range(first_close, last_close + 1, M3)
    stray = sorted(set(values) - set(closes))
    if stray:
        raise AssertionError(f"stretch named outside the series: {stray}")
    rows = [(close - M3, 100.0, 100.0, 100.0, 100.0, 1.0) for close in closes]
    return bars_from_rows(rows, M3), tuple(values.get(close, FAR) for close in closes)


# --- the 1m bars ----------------------------------------------------------------------------------


def flat(price):
    return (price, price, price, price)


LONG_TP_TOUCH = (100.0, 100.5, 100.0, 100.0)  # high through 100.41004
LONG_SL_TOUCH = (100.0, 100.0, 99.7, 100.0)  # low through 99.759975
LONG_TP_GAP = flat(100.5)
LONG_SL_GAP = flat(99.7)
SHORT_TP_TOUCH = (100.0, 100.0, 99.7, 100.0)  # low through 99.79002
SHORT_TP_GAP = flat(99.7)


def minutes(first_open, end, edits=None):
    """Flat 1m bars at 100.0 opening every minute in [first_open, end); ``edits`` maps an open time
    to an (open, high, low, close) bar."""
    edits = dict(edits or {})
    stray = sorted(t for t in edits if not first_open <= t < end or (t - first_open) % MINUTE_MS)
    if stray:
        raise AssertionError(f"edits outside the bars: {stray}")
    rows = [(t, *edits.get(t, flat(100.0)), 1.0) for t in range(first_open, end, MINUTE_MS)]
    return bars_from_rows(rows, MINUTE_MS)


# --- frames and runs ------------------------------------------------------------------------------


def make_frames(bars_1m, hourly, m15=None, m5=None, stretch_of=None, symbol=PAIR, cfg=CFG):
    """Frames written by hand; they record the numbers of ``cfg`` as those they were built with."""
    triggers = {
        "15m": m15 or TriggerSeries(bars_from_rows([], M15), ()),
        "5m": m5 or TriggerSeries(bars_from_rows([], M5), ()),
    }
    stretch_bars, values = stretch_of or (bars_from_rows([], M3), ())
    return PairFrames(
        symbol, bars_1m, hourly, MappingProxyType(triggers), stretch_bars, values,
        FrameParams.of(cfg),
    )


def run_a(kinds, start, end, *, hourly=None, edits=None, cfg=CFG, tf="15m", series=None):
    """Mode A over [start, end): candles at every close in it, 1m bars until ``end``."""
    step = M15 if tf == "15m" else M5
    series = series or candles(start, end - step, kinds, step)
    bars = minutes(start - step, end, edits)
    frames = make_frames(
        bars, hourly or StubHourly(), cfg=cfg, **{"m15" if tf == "15m" else "m5": series}
    )
    return simulate_run(frames, cfg, RunSpec(MODE_A, PAIR, tf, NAIK, start, end))


def run_d(values, start, end, *, hourly=None, edits=None, cfg=CFG, gate=TURUN):
    """Mode D over [start, end) with the condition gate ``gate`` (NAIK is the comparator)."""
    frames = make_frames(
        minutes(start - M3, end, edits),
        hourly or StubHourly(fixed(context(gate))),
        stretch_of=stretch(start, end - M3, values),
        cfg=cfg,
    )
    return simulate_run(frames, cfg, RunSpec(MODE_D, PAIR, "3m", gate, start, end))


def counts(result):
    """The reasons that blocked at least one signal, with their counts."""
    return {reason: n for reason, n in result.blocked.items() if n}


def entries(result):
    return [trade.entry_ms for trade in result.trades]


def variant(cfg=CFG, **blocks):
    """``cfg`` with fields of its blocks replaced, e.g. ``variant(umum={"jeda_menit": 5})``."""
    changes = {
        name: dataclasses.replace(getattr(cfg, name), **fields) for name, fields in blocks.items()
    }
    return dataclasses.replace(cfg, **changes)


def with_faktor_k(value, pair=PAIR, cfg=CFG):
    pairs = dict(cfg.pair)
    pairs[pair] = dataclasses.replace(pairs[pair], faktor_k=value)
    return dataclasses.replace(cfg, pair=MappingProxyType(pairs))


class FixtureTests(unittest.TestCase):
    def test_the_reference_contexts_are_what_the_selector_says(self):
        for kondisi in (NAIK, TURUN, SIDEWAYS, TIDAK_ENTRY):
            for atr14 in (1.0, 2.0, 2.0000001, 2.5):
                with self.subTest(kondisi=kondisi, atr14=atr14):
                    c = context(kondisi, atr14=atr14)
                    args = (c.sma20_1h, c.sma50_1h, c.atr14_1h, c.close_4h, c.ema50_4h)
                    sideways = CFG.pemilih_mode.sideways_jarak_kali_atr
                    self.assertEqual(pilih_mode(*args, sideways), kondisi)

    def test_the_config_holds_the_numbers_the_tests_are_written_for(self):
        u, a, d = CFG.umum, CFG.mode_a, CFG.mode_d
        self.assertEqual(
            (u.fee_taker_persen, u.slippage_persen, u.notional_usdt), (0.035, 0.01, 25000)
        )
        self.assertEqual(
            (u.jeda_menit, u.maks_entry_per_hari_per_pair, u.maks_entry_per_hari_total), (15, 6, 10)
        )
        self.assertEqual(
            (u.penjaga_volatilitas_kali_median_atr, u.jendela_entry_mingguan),
            (2.0, "mon 07:00-sat 00:00"),
        )
        self.assertEqual((a.konfirmasi_dalam_candle, a.maks_jarak_sma20_1h_kali_atr), (3, 1.0))
        self.assertEqual((a.tp_persen, a.sl_persen, a.batas_waktu_menit), (0.40, 0.25, 90))
        self.assertEqual(
            (d.maks_jarak_ke_ema_kali_atr, d.tp_persen, d.sl_persen, d.batas_waktu_menit),
            (0.75, 0.20, 0.20, 60),
        )
        self.assertEqual((CFG.pair[PAIR].faktor_k, CFG.umum.maks_posisi_per_pair), (1.0, 1))


class RecordTests(unittest.TestCase):
    def test_the_trade_holds_every_log_field_in_the_documented_order(self):
        names = [field.name for field in dataclasses.fields(Trade)]

        self.assertEqual(
            names,
            [
                "mode",
                "pair",
                "side",
                "kondisi",
                "context",
                "faktor_k",
                "signal_ms",
                "entry_ms",
                "entry_fill",
                "tp",
                "sl",
                "exit_ms",
                "exit_fill",
                "exit_reason",
                "gapped",
                "both_touched",
                "qty",
                "notional_usdt",
                "fee_usdt",
                "slippage_usdt",
                "pnl_usdt",
                "volume_usdt",
            ],
        )

    def test_the_run_spec_and_the_result_hold_their_documented_fields(self):
        self.assertEqual(
            [field.name for field in dataclasses.fields(RunSpec)],
            ["mode", "pair", "tf", "kondisi_gate", "start_ms", "end_ms"],
        )
        self.assertEqual(
            [field.name for field in dataclasses.fields(RunResult)], ["trades", "blocked"]
        )
        self.assertEqual((MODE_A, MODE_D), ("A", "D"))

    def test_the_block_reasons_are_the_seven_gates_in_their_order(self):
        self.assertEqual(
            BLOCK_REASONS,
            (
                "window",
                "segment_edge",
                "kondisi",
                "position_open",
                "pause",
                "daily_cap",
                "volatility",
            ),
        )

    def test_a_result_lists_every_reason_in_gate_order_and_cannot_be_edited(self):
        result = run_a({at(TUE, 10, 15): SIGNAL}, at(TUE, 9), at(TUE, 13))

        self.assertIsInstance(result.trades, tuple)
        self.assertEqual(list(result.blocked), list(BLOCK_REASONS))
        self.assertTrue(all(type(n) is int for n in result.blocked.values()))
        with self.assertRaises(TypeError):
            result.blocked["window"] = 1
        for record in (result, result.trades[0], RunSpec(MODE_A, PAIR, "15m", NAIK, 0, 1)):
            with (
                self.subTest(record=type(record).__name__),
                self.assertRaises(dataclasses.FrozenInstanceError),
            ):
                record.mode = "D"


class ModeAEntryTests(unittest.TestCase):
    def test_mode_a_enters_at_the_next_open_after_confirmation(self):
        touch, signal = at(TUE, 10, 0), at(TUE, 10, 15)
        edits = {
            signal - MINUTE_MS: flat(100.11),  # the minute before the close
            signal: flat(100.13),  # the bar opening at the close: the entry
            signal + MINUTE_MS: flat(100.17),
            signal + LIMIT_A: flat(100.07),  # the time exit
        }

        result = run_a({touch: TOUCH, signal: CONFIRM}, at(TUE, 9), at(TUE, 13), edits=edits)

        fill = adverse_fill(100.13, LONG, 0.01, False)
        bracket = bracket_prices(fill, LONG, 0.40, 0.25)
        money = settle(100.13, 100.07, LONG, 25_000.0, 0.035, 0.01)
        expected = Trade(
            mode="A",
            pair=PAIR,
            side=LONG,
            kondisi=NAIK,
            context=context(),
            faktor_k=1.0,
            signal_ms=signal,
            entry_ms=signal,
            entry_fill=fill,
            tp=bracket.tp,
            sl=bracket.sl,
            exit_ms=signal + LIMIT_A,
            exit_fill=money.exit_fill,
            exit_reason=EXIT_WAKTU,
            gapped=False,
            both_touched=False,
            qty=money.qty,
            notional_usdt=25_000.0,
            fee_usdt=money.fee_usdt,
            slippage_usdt=money.slippage_usdt,
            pnl_usdt=money.pnl_usdt,
            volume_usdt=money.volume_usdt,
        )
        self.assertEqual(result.trades, (expected,))
        self.assertEqual(counts(result), {})

    def test_each_candle_is_judged_against_its_own_ema(self):
        signal = at(TUE, 10, 15)
        # The neighbours' EMA of 99 is below the signal candle's low of 99.9: judged against
        # either neighbour's EMA, the candle would be no touch.
        kinds = {
            signal - M15: with_ema(QUIET, 99.0),
            signal: SIGNAL,
            signal + M15: with_ema(QUIET, 99.0),
        }

        self.assertEqual(entries(run_a(kinds, at(TUE, 9), at(TUE, 13))), [signal])

    def test_every_candle_counts_toward_the_window_also_without_an_ema(self):
        touch = at(TUE, 10)
        no_ema = with_ema(QUIET, None)
        late = {
            touch: TOUCH,
            touch + M15: no_ema,
            touch + 2 * M15: no_ema,
            touch + 3 * M15: CONFIRM,
        }
        in_time = {touch: TOUCH, touch + M15: no_ema, touch + 2 * M15: CONFIRM}

        late_result = run_a(late, at(TUE, 9), at(TUE, 13))
        self.assertEqual((late_result.trades, counts(late_result)), ((), {}))
        self.assertEqual(entries(run_a(in_time, at(TUE, 9), at(TUE, 13))), [touch + 2 * M15])

    def test_the_confirmation_cap_uses_the_hourly_sma_and_atr_at_the_close(self):
        signal = at(TUE, 10, 15)
        low_cap = context(sma20=99.0)  # 99 + 1 ATR: a close of 100.5 is too high
        cases = {
            "always low": (fixed(low_cap), []),
            "low before the close": (lambda t: low_cap if t < signal else context(), [signal]),
            "low from the close": (lambda t: low_cap if t >= signal else context(), []),
        }
        for name, (answer, expected) in cases.items():
            with self.subTest(case=name):
                result = run_a({signal: SIGNAL}, at(TUE, 9), at(TUE, 13), hourly=StubHourly(answer))

                self.assertEqual(entries(result), expected)
                self.assertEqual(counts(result), {})

    def test_a_confirmation_too_far_above_the_hourly_sma_is_no_signal(self):
        touch = at(TUE, 10)

        result = run_a({touch: TOUCH, touch + M15: TOO_HIGH}, at(TUE, 9), at(TUE, 13))

        self.assertEqual((result.trades, counts(result)), ((), {}))

    def test_mode_a_reads_the_candles_of_its_timeframe(self):
        start, end = at(TUE, 9), at(TUE, 14)
        on_15m, on_5m = at(TUE, 10, 15), at(TUE, 11, 5)
        bars = minutes(start - M15, end)
        for tf, expected in (("15m", on_15m), ("5m", on_5m)):
            with self.subTest(tf=tf):
                frames = make_frames(
                    bars,
                    StubHourly(),
                    m15=candles(start, end - M15, {on_15m: SIGNAL}),
                    m5=candles(start, end - M5, {on_5m: SIGNAL}, M5),
                )

                result = simulate_run(frames, CFG, RunSpec(MODE_A, PAIR, tf, NAIK, start, end))

                self.assertEqual(entries(result), [expected])


class TouchSurvivalTests(unittest.TestCase):
    """D5: only an entry uses a touch up; a blocked signal leaves it for its window."""

    FIRST, DURING, PAUSED, AFTER = at(TUE, 10, 0), at(TUE, 10, 15), at(TUE, 10, 30), at(TUE, 10, 45)
    # The first position takes profit in its 21st minute (the bar opening 10:20), so it is out at
    # 10:21 and paused until 10:36.
    EDITS = {at(TUE, 10, 20): LONG_TP_TOUCH}

    def run_kinds(self, kinds):
        return run_a(kinds, at(TUE, 9), at(TUE, 14), edits=self.EDITS)

    def test_open_position_blocks_and_the_touch_survives_until_after_the_pause(self):
        result = self.run_kinds(
            {self.FIRST: SIGNAL, self.DURING: SIGNAL, self.PAUSED: CONFIRM, self.AFTER: CONFIRM}
        )

        self.assertEqual(entries(result), [self.FIRST, self.AFTER])
        first = result.trades[0]
        self.assertEqual((first.exit_reason, first.exit_ms), (EXIT_TP, at(TUE, 10, 21)))
        self.assertEqual(counts(result), {"position_open": 1, "pause": 1})

    def test_an_entry_uses_its_touch_up(self):
        result = self.run_kinds(
            {self.FIRST: SIGNAL, self.DURING: CONFIRM, self.PAUSED: CONFIRM, self.AFTER: CONFIRM}
        )

        self.assertEqual(entries(result), [self.FIRST])
        self.assertEqual(counts(result), {})

    def test_a_surviving_touch_still_lapses_after_its_three_candles(self):
        result = self.run_kinds(
            {self.FIRST: SIGNAL, self.DURING: SIGNAL, self.PAUSED: CONFIRM, at(TUE, 11): CONFIRM}
        )

        self.assertEqual(entries(result), [self.FIRST])
        self.assertEqual(counts(result), {"position_open": 1, "pause": 1})

    def test_a_signal_blocked_by_the_condition_leaves_the_touch(self):
        touch, blocked, entered = at(TUE, 10, 30), at(TUE, 10, 45), at(TUE, 11, 0)
        hourly = StubHourly(lambda t: context(NAIK if t >= at(TUE, 11) else TURUN))

        result = run_a(
            {touch: TOUCH, blocked: CONFIRM, entered: CONFIRM},
            at(TUE, 9),
            at(TUE, 14),
            hourly=hourly,
        )

        self.assertEqual(entries(result), [entered])
        self.assertEqual(result.trades[0].kondisi, NAIK)
        self.assertEqual(counts(result), {"kondisi": 1})


class ConditionGateTests(unittest.TestCase):
    def test_no_entry_outside_the_condition_gate(self):
        signal = at(TUE, 10, 15)
        for kondisi in (TURUN, SIDEWAYS, TIDAK_ENTRY):
            with self.subTest(mode="A", kondisi=kondisi):
                hourly = StubHourly(fixed(context(kondisi)))

                result = run_a({signal: SIGNAL}, at(TUE, 9), at(TUE, 13), hourly=hourly)

                self.assertEqual((result.trades, counts(result)), ((), {"kondisi": 1}))
        for kondisi in (NAIK, SIDEWAYS, TIDAK_ENTRY):
            with self.subTest(mode="D", kondisi=kondisi):
                hourly = StubHourly(fixed(context(kondisi)))

                result = run_d({signal: 0.0}, at(TUE, 9), at(TUE, 13), hourly=hourly)

                self.assertEqual((result.trades, counts(result)), ((), {"kondisi": 1}))

    def test_mode_d_comparator_shorts_only_in_naik(self):
        naik_signal, turun_signal = at(TUE, 10, 3), at(TUE, 12, 0)
        values = {naik_signal: 0.0, turun_signal: 0.0}

        def hourly():
            return StubHourly(lambda t: context(NAIK if t < at(TUE, 11) else TURUN))

        comparator = run_d(values, at(TUE, 9), at(TUE, 14), hourly=hourly(), gate=NAIK)
        proper = run_d(values, at(TUE, 9), at(TUE, 14), hourly=hourly(), gate=TURUN)

        fill = adverse_fill(100.0, SHORT, 0.01, False)
        bracket = bracket_prices(fill, SHORT, 0.20, 0.20)
        money = settle(100.0, 100.0, SHORT, 25_000.0, 0.035, 0.01)
        expected = Trade(
            mode="D",
            pair=PAIR,
            side=SHORT,
            kondisi=NAIK,
            context=context(NAIK),
            faktor_k=1.0,
            signal_ms=naik_signal,
            entry_ms=naik_signal,
            entry_fill=fill,
            tp=bracket.tp,
            sl=bracket.sl,
            exit_ms=naik_signal + LIMIT_D,
            exit_fill=money.exit_fill,
            exit_reason=EXIT_WAKTU,
            gapped=False,
            both_touched=False,
            qty=money.qty,
            notional_usdt=25_000.0,
            fee_usdt=money.fee_usdt,
            slippage_usdt=money.slippage_usdt,
            pnl_usdt=money.pnl_usdt,
            volume_usdt=money.volume_usdt,
        )
        self.assertEqual(comparator.trades, (expected,))
        self.assertLess(expected.tp, expected.entry_fill)
        self.assertLess(expected.entry_fill, expected.sl)
        self.assertEqual(counts(comparator), {"kondisi": 1})
        self.assertEqual([(t.signal_ms, t.kondisi) for t in proper.trades], [(turun_signal, TURUN)])
        self.assertEqual(counts(proper), {"kondisi": 1})

    def test_mode_d_signals_inside_the_near_mean_band_only(self):
        values = {
            at(TUE, 10, 0): 0.75,
            at(TUE, 11, 30): -0.75,
            at(TUE, 13, 0): 0.7500001,
            at(TUE, 14, 30): -0.7500001,
            at(TUE, 16, 0): None,
        }

        result = run_d(values, at(TUE, 9), at(TUE, 18))

        self.assertEqual(entries(result), [at(TUE, 10, 0), at(TUE, 11, 30)])
        self.assertEqual(counts(result), {})


class WindowAndCapTests(unittest.TestCase):
    def test_weekend_signals_are_blocked(self):
        friday_last, monday_open = at(FRI, 23, 45), at(MON, 7, 0)
        closed = [at(SAT, 0, 0), at(SAT, 12, 0), at(SUN, 12, 0), at(MON, 6, 45)]
        kinds = {t: SIGNAL for t in [friday_last, *closed, monday_open]}

        result = run_a(kinds, at(FRI, 23), at(MON, 9))

        self.assertEqual(entries(result), [friday_last, monday_open])
        self.assertEqual(result.trades[0].exit_ms, at(SAT, 1, 15))  # the window only gates opening
        self.assertEqual(counts(result), {"window": 4})

    def test_daily_cap_resets_at_wib_midnight_not_utc(self):
        # Six entries close out Tuesday (WIB), six open Wednesday; each takes profit in its first
        # minute, so neither the position nor the pause is in the way 30 minutes later.
        tuesday = [at(TUE, 20, 30) + k * 30 * MINUTE_MS for k in range(6)]  # 20:30 .. 23:00
        wednesday = [at(WED, 0, 0) + k * 30 * MINUTE_MS for k in range(6)]  # 00:00 .. 02:30
        tuesday_full = at(TUE, 23, 30)  # the 7th of Tuesday (WIB); Tuesday 16:30 UTC
        # 07:15 WIB is 00:15 UTC: a new UTC day, but still the WIB Wednesday that has six.
        wednesday_full = at(WED, 7, 15)
        kinds = {t: SIGNAL for t in [*tuesday, tuesday_full, *wednesday, wednesday_full]}
        edits = {t: LONG_TP_TOUCH for t in tuesday + wednesday}

        result = run_a(kinds, at(TUE, 20), at(WED, 9), edits=edits)

        # Counted per UTC day, Wednesday 00:00 WIB (17:00 UTC Tuesday) would be the 7th of a
        # UTC Tuesday, and 07:15 WIB the first of a UTC Wednesday.
        self.assertEqual(entries(result), tuesday + wednesday)
        self.assertEqual(counts(result), {"daily_cap": 2})

    def test_the_smaller_daily_cap_binds(self):
        signals = [at(TUE, 20, 30), at(TUE, 21, 0), at(TUE, 21, 30)]
        kinds = {t: SIGNAL for t in signals}
        edits = {t: LONG_TP_TOUCH for t in signals}
        cases = {
            "document": (CFG, signals, {}),
            "pair cap 2": (
                variant(umum={"maks_entry_per_hari_per_pair": 2}),
                signals[:2],
                {"daily_cap": 1},
            ),
            "total cap 2": (
                variant(umum={"maks_entry_per_hari_total": 2}),
                signals[:2],
                {"daily_cap": 1},
            ),
        }
        for name, (cfg, expected, blocked) in cases.items():
            with self.subTest(case=name):
                result = run_a(kinds, at(TUE, 20), at(WED, 0), edits=edits, cfg=cfg)

                self.assertEqual((entries(result), counts(result)), (expected, blocked))

    def test_an_entry_counts_for_the_wib_day_it_opens_on(self):
        # One entry a day. Tuesday 23:00 runs to its time limit, Wednesday 00:30; Wednesday's
        # first entry at 00:45 takes profit at once, so 01:30 meets only the cap.
        tuesday, wednesday, capped = at(TUE, 23, 0), at(WED, 0, 45), at(WED, 1, 30)
        cfg = variant(umum={"maks_entry_per_hari_per_pair": 1})

        result = run_a(
            {t: SIGNAL for t in (tuesday, wednesday, capped)},
            at(TUE, 22),
            at(WED, 4),
            edits={wednesday: LONG_TP_TOUCH},
            cfg=cfg,
        )

        self.assertEqual(entries(result), [tuesday, wednesday])
        self.assertEqual(result.trades[0].exit_ms, at(WED, 0, 30))
        self.assertEqual(counts(result), {"daily_cap": 1})

    def test_volatility_guard_blocks(self):
        signal = at(TUE, 10, 15)
        cases = [
            ("above twice the median", 2.0000001, 1.0, False),
            ("exactly twice the median", 2.0, 1.0, True),
            ("no median yet", 1.0, None, False),
        ]
        for name, atr14, median, enters in cases:
            for mode in ("A", "D"):
                with self.subTest(case=name, mode=mode):
                    gate = NAIK if mode == "A" else TURUN
                    hourly = StubHourly(fixed(context(gate, atr14=atr14, median=median)))
                    if mode == "A":
                        result = run_a({signal: SIGNAL}, at(TUE, 9), at(TUE, 13), hourly=hourly)
                    else:
                        result = run_d({signal: 0.0}, at(TUE, 9), at(TUE, 13), hourly=hourly)

                    self.assertEqual(entries(result), [signal] if enters else [])
                    self.assertEqual(counts(result), {} if enters else {"volatility": 1})


class SegmentEdgeTests(unittest.TestCase):
    """D10: no position may straddle the end of its segment. The time exit opens ``limit``
    minutes after the entry, and a bar opening exactly at the end belongs to the next segment."""

    END = at(WED, 0)  # a WIB midnight, where segments end; the 1m bars stop at 23:59

    def test_segment_edge_blocks_a_signal_whose_time_limit_passes_the_end(self):
        end = self.END
        for tf, step in (("15m", M15), ("5m", M5)):
            with self.subTest(mode="A", tf=tf, signal="limit before the end"):
                result = run_a({end - LIMIT_A: SIGNAL}, at(TUE, 20), end, tf=tf)

                self.assertEqual((result.trades, counts(result)), ((), {"segment_edge": 1}))
            with self.subTest(mode="A", tf=tf, signal="one candle earlier"):
                result = run_a({end - LIMIT_A - step: SIGNAL}, at(TUE, 20), end, tf=tf)

                (trade,) = result.trades
                self.assertEqual((trade.exit_reason, trade.exit_ms), (EXIT_WAKTU, end - step))
                self.assertEqual(counts(result), {})
        with self.subTest(mode="D", signal="limit before the end"):
            result = run_d({end - LIMIT_D: 0.0}, at(TUE, 20), end)

            self.assertEqual((result.trades, counts(result)), ((), {"segment_edge": 1}))
        with self.subTest(mode="D", signal="one candle earlier"):
            result = run_d({end - LIMIT_D - M3: 0.0}, at(TUE, 20), end)

            (trade,) = result.trades
            self.assertEqual((trade.exit_reason, trade.exit_ms), (EXIT_WAKTU, end - M3))

    def test_the_edge_is_exact_to_the_minute(self):
        # With the segment ending at 00:01, the time exit of the 22:30 signal opens at 00:00,
        # inside it; the 22:45 one would open at 00:15, past it.
        end = minutes_after(self.END, 1)
        allowed, past = self.END - LIMIT_A, self.END - LIMIT_A + M15

        result = run_a({allowed: SIGNAL, past: SIGNAL}, at(TUE, 20), end)

        self.assertEqual([(t.entry_ms, t.exit_ms) for t in result.trades], [(allowed, self.END)])
        self.assertEqual(counts(result), {"segment_edge": 1})

    def test_every_signal_inside_the_last_time_limit_is_blocked_and_no_trade_reaches_the_end(self):
        end = self.END
        last_hours = {t: SIGNAL for t in range(end - LIMIT_A - 2 * M15, end, M15)}
        edits = {end - LIMIT_A - 2 * M15: LONG_TP_TOUCH}  # the first is out at once

        result = run_a(last_hours, at(TUE, 20), end, edits=edits)

        # The first enters and is out in a minute, the second waits for the pause, the six from
        # end - 90 min on are past the edge.
        self.assertEqual(entries(result), [end - LIMIT_A - 2 * M15])
        self.assertEqual(counts(result), {"pause": 1, "segment_edge": 6})
        self.assertTrue(all(trade.exit_ms < end for trade in result.trades))


class ExitTimeTests(unittest.TestCase):
    """The exit time: the bar's open for a time exit and a gap, the end of the minute for a touch,
    because a touch is only known to the minute. The pause runs from it."""

    def test_exit_time_is_the_open_for_time_and_gap_exits_and_the_minute_end_for_touches(self):
        signal = at(TUE, 10, 15)
        cases = {
            "time limit": ({}, EXIT_WAKTU, False, signal + LIMIT_A),
            "take-profit touched": (
                {minutes_after(signal, 5): LONG_TP_TOUCH},
                EXIT_TP,
                False,
                minutes_after(signal, 6),
            ),
            "stop touched": (
                {minutes_after(signal, 3): LONG_SL_TOUCH},
                EXIT_SL,
                False,
                minutes_after(signal, 4),
            ),
            "touched in the entry minute": (
                {signal: LONG_TP_TOUCH},
                EXIT_TP,
                False,
                minutes_after(signal, 1),
            ),
            "gap through the stop": (
                {minutes_after(signal, 7): LONG_SL_GAP},
                EXIT_SL,
                True,
                minutes_after(signal, 7),
            ),
            "gap through the target": (
                {minutes_after(signal, 9): LONG_TP_GAP},
                EXIT_TP,
                True,
                minutes_after(signal, 9),
            ),
        }
        for name, (edits, reason, gapped, exit_ms) in cases.items():
            with self.subTest(case=name):
                result = run_a({signal: SIGNAL}, at(TUE, 9), at(TUE, 13), edits=edits)

                (trade,) = result.trades
                self.assertEqual(
                    (trade.exit_reason, trade.gapped, trade.exit_ms), (reason, gapped, exit_ms)
                )

    def test_the_pause_runs_from_the_exit_time(self):
        t0 = at(TUE, 10, 0)
        cases = {
            # Touched in the bar opening at +15: out at +16, paused until +31.
            "touch": (
                {minutes_after(t0, 15): SHORT_TP_TOUCH},
                [30, 33],
                [t0, minutes_after(t0, 33)],
                {"pause": 1},
            ),
            # A gap at the open of +15: out at +15, paused until +30.
            "gap": ({minutes_after(t0, 15): SHORT_TP_GAP}, [30], [t0, minutes_after(t0, 30)], {}),
            # The time exit at the open of +60: closed at +60 itself, paused until +75.
            "time": ({}, [60, 72, 75], [t0, minutes_after(t0, 75)], {"pause": 2}),
        }
        for name, (edits, later, expected, blocked) in cases.items():
            with self.subTest(case=name):
                values = {t0: 0.0, **{minutes_after(t0, m): 0.0 for m in later}}

                result = run_d(values, at(TUE, 9), at(TUE, 14), edits=edits)

                self.assertEqual((entries(result), counts(result)), (expected, blocked))


class CountingTests(unittest.TestCase):
    def test_blocked_reasons_are_counted(self):
        """Every signal is entered or counted once, under the first gate it fails."""
        hot = context(NAIK, atr14=2.5)  # too volatile: 2.5 > 2 x 1.0
        hot_turun = context(TURUN, atr14=2.5)
        # close: (context at the close, what happens; and what else fails)
        plan = {
            at(FRI, 18, 0): (None, "enter"),  # #1, takes profit at once: out 18:01
            at(FRI, 18, 3): (None, "pause"),
            at(FRI, 18, 15): (None, "pause"),  # 18:15 < 18:01 + 15
            at(FRI, 18, 18): (None, "enter"),  # #2, flat: out at its time limit, 19:18
            at(FRI, 18, 30): (None, "position_open"),  # paused too
            at(FRI, 19, 0): (hot_turun, "kondisi"),  # open, paused, too volatile too
            at(FRI, 19, 18): (None, "pause"),  # the exit minute: closed, paused
            at(FRI, 19, 33): (None, "enter"),  # #3: 19:18 + 15 exactly; takes profit: out 19:34
            at(FRI, 19, 48): (None, "pause"),  # 19:48 < 19:34 + 15
            at(FRI, 19, 51): (hot, "volatility"),
            at(FRI, 19, 54): (None, "enter"),  # #4, out 19:55
            at(FRI, 20, 12): (None, "enter"),  # #5, out 20:13
            at(FRI, 20, 30): (None, "enter"),  # #6, the sixth of Friday, flat: out 21:30
            at(FRI, 20, 33): (hot, "position_open"),  # paused, capped, too volatile too
            at(FRI, 21, 30): (hot, "pause"),  # capped, too volatile too
            at(FRI, 21, 39): (hot_turun, "kondisi"),  # paused, capped, too volatile too
            at(FRI, 21, 48): (hot, "daily_cap"),  # too volatile too
            at(SAT, 10, 0): (hot_turun, "window"),
            at(SUN, 10, 0): (hot_turun, "window"),
            at(MON, 6, 57): (None, "window"),
            at(MON, 7, 0): (None, "enter"),  # #7, a new WIB day, flat: out 08:00
            at(MON, 8, 0): (None, "segment_edge"),  # paused too
            at(MON, 8, 57): (hot_turun, "segment_edge"),
        }
        quick = [at(FRI, 18, 0), at(FRI, 19, 33), at(FRI, 19, 54), at(FRI, 20, 12)]
        hourly = StubHourly(lambda t: (plan[t][0] if t in plan else None) or context(NAIK))

        result = run_d(
            {t: 0.0 for t in plan},
            at(FRI, 18),
            at(MON, 9),
            hourly=hourly,
            edits={t: SHORT_TP_TOUCH for t in quick},
            gate=NAIK,
        )

        expected_trades = [
            (at(FRI, 18, 0), at(FRI, 18, 1), EXIT_TP),
            (at(FRI, 18, 18), at(FRI, 19, 18), EXIT_WAKTU),
            (at(FRI, 19, 33), at(FRI, 19, 34), EXIT_TP),
            (at(FRI, 19, 54), at(FRI, 19, 55), EXIT_TP),
            (at(FRI, 20, 12), at(FRI, 20, 13), EXIT_TP),
            (at(FRI, 20, 30), at(FRI, 21, 30), EXIT_WAKTU),
            (at(MON, 7, 0), at(MON, 8, 0), EXIT_WAKTU),
        ]
        self.assertEqual(
            [(t.entry_ms, t.exit_ms, t.exit_reason) for t in result.trades], expected_trades
        )
        expected_counts = Counter(what for _, what in plan.values() if what != "enter")
        self.assertEqual(
            dict(result.blocked), {reason: expected_counts[reason] for reason in BLOCK_REASONS}
        )
        self.assertEqual(len(result.trades) + sum(result.blocked.values()), len(plan))

    def test_the_window_is_checked_before_the_segment_edge(self):
        start, end = at(FRI, 23, 0), at(SAT, 0, 30)
        entered, at_edge, weekend = at(FRI, 23, 27), at(FRI, 23, 30), at(SAT, 0, 0)

        result = run_d({entered: 0.0, at_edge: 0.0, weekend: 0.0}, start, end)

        # 23:30 + 60 min and 00:00 + 60 min both reach the end, and both meet an open position.
        self.assertEqual(entries(result), [entered])
        self.assertEqual(counts(result), {"segment_edge": 1, "window": 1})


class RunRangeTests(unittest.TestCase):
    def test_only_candles_closing_inside_the_run_reach_the_rule(self):
        start, end = at(TUE, 10), at(TUE, 14)
        kinds = {start - M15: TOUCH, start: CONFIRM, end: SIGNAL}
        cases = {
            # A touch before the start is never seen, and a candle closing at the end is outside.
            "touch before the start": (kinds, []),
            "signal at the start": ({start: SIGNAL}, [start]),
        }
        for name, (case_kinds, expected) in cases.items():
            with self.subTest(case=name):
                hourly = StubHourly()
                frames = make_frames(
                    minutes(start - 2 * M15, end + 2 * M15),
                    hourly,
                    m15=candles(start - 2 * M15, end + 2 * M15, case_kinds),
                )

                result = simulate_run(frames, CFG, RunSpec(MODE_A, PAIR, "15m", NAIK, start, end))

                self.assertEqual((entries(result), counts(result)), (expected, {}))
                # The condition is read at every close inside the run, in order, and never else.
                self.assertEqual(hourly.asked, list(range(start, end, M15)))

    def test_mode_d_reads_the_condition_only_at_closes_inside_the_run(self):
        start, end = at(TUE, 10), at(TUE, 13)
        second = at(TUE, 11, 15)  # the first is out at 11:00 and paused until 11:15
        values = {start - M3: 0.0, start: 0.0, second: 0.0, end: 0.0}
        hourly = StubHourly(fixed(context(TURUN)))
        frames = make_frames(
            minutes(start - 2 * M3, end + 2 * M3),
            hourly,
            stretch_of=stretch(start - M3, end + M3, values),
        )

        result = simulate_run(frames, CFG, RunSpec(MODE_D, PAIR, "3m", TURUN, start, end))

        self.assertEqual((entries(result), counts(result)), ([start, second], {}))
        self.assertTrue(set(hourly.asked) <= set(range(start, end, M3)))
        self.assertEqual(hourly.asked, sorted(hourly.asked))
        self.assertTrue({start, second} <= set(hourly.asked))

    def test_an_empty_run_has_no_trades_and_counts_nothing(self):
        hourly = StubHourly()
        series = candles(at(TUE, 9), at(TUE, 11), {at(TUE, 10): SIGNAL})

        result = run_a({}, at(TUE, 10), at(TUE, 10), hourly=hourly, series=series)

        self.assertEqual((result.trades, counts(result), hourly.asked), ((), {}, []))

    def test_a_run_starts_from_a_clean_state(self):
        start, end = at(TUE, 9), at(TUE, 15)
        touch, confirm = at(TUE, 12, 15), at(TUE, 12, 30)
        series = candles(start, end - M15, {touch: TOUCH, confirm: CONFIRM})
        frames = make_frames(minutes(start - M15, end), StubHourly(), m15=series)

        def run(first, last):
            return simulate_run(frames, CFG, RunSpec(MODE_A, PAIR, "15m", NAIK, first, last))

        # One run sees the touch only, the next the confirmation only: no touch carries over.
        self.assertEqual((run(start, confirm).trades, run(confirm, end).trades), ((), ()))
        whole = run(start, end)
        self.assertEqual(entries(whole), [confirm])
        self.assertEqual(run(start, end), whole)


class FaktorKTests(unittest.TestCase):
    def test_take_profit_and_stop_scale_with_faktor_k(self):
        signal = at(TUE, 10, 15)
        # The stop at 1.5 x 0.25 % is 99.6349625: the first bar reaches the unscaled stop
        # (99.759975) and not this one, the second reaches it.
        edits = {
            minutes_after(signal, 2): LONG_SL_TOUCH,
            minutes_after(signal, 4): (100.0, 100.0, 99.6, 100.0),
        }

        result = run_a(
            {signal: SIGNAL}, at(TUE, 9), at(TUE, 13), edits=edits, cfg=with_faktor_k(1.5)
        )

        (trade,) = result.trades
        fill = adverse_fill(100.0, LONG, 0.01, False)
        bracket = bracket_prices(fill, LONG, 0.40 * 1.5, 0.25 * 1.5)
        self.assertEqual((trade.tp, trade.sl, trade.faktor_k), (bracket.tp, bracket.sl, 1.5))
        self.assertEqual((trade.exit_reason, trade.exit_ms), (EXIT_SL, minutes_after(signal, 5)))

    def test_mode_d_scales_with_faktor_k_too(self):
        signal = at(TUE, 10, 3)

        result = run_d({signal: 0.0}, at(TUE, 9), at(TUE, 13), cfg=with_faktor_k(2.0))

        (trade,) = result.trades
        bracket = bracket_prices(
            adverse_fill(100.0, SHORT, 0.01, False), SHORT, 0.20 * 2.0, 0.20 * 2.0
        )
        self.assertEqual((trade.tp, trade.sl, trade.faktor_k), (bracket.tp, bracket.sl, 2.0))

    def test_an_automatic_faktor_k_is_refused(self):
        start, end = at(TUE, 9), at(TUE, 13)
        frames = make_frames(
            minutes(start, end), StubHourly(), m15=candles(start, end - M15, {}), symbol="ETH-USDT"
        )

        self.assertIsNone(CFG.pair["ETH-USDT"].faktor_k)
        with self.assertRaisesRegex(ValueError, "faktor_k"):
            simulate_run(frames, CFG, RunSpec(MODE_A, "ETH-USDT", "15m", NAIK, start, end))


class ConfigNumberTests(unittest.TestCase):
    """Every number of a run comes from the config: a variant moves the result."""

    SIGNAL_AT = at(TUE, 10, 15)

    def test_costs_size_levels_and_limit(self):
        cfg = variant(
            umum={"fee_taker_persen": 0.05, "slippage_persen": 0.02, "notional_usdt": 10_000.0},
            mode_a={"tp_persen": 0.5, "sl_persen": 0.3, "batas_waktu_menit": 30},
        )
        signal = self.SIGNAL_AT

        (trade,) = run_a({signal: SIGNAL}, at(TUE, 9), at(TUE, 13), cfg=cfg).trades

        fill = adverse_fill(100.0, LONG, 0.02, False)
        bracket = bracket_prices(fill, LONG, 0.5, 0.3)
        money = settle(100.0, 100.0, LONG, 10_000.0, 0.05, 0.02)
        self.assertEqual((trade.entry_fill, trade.tp, trade.sl), (fill, bracket.tp, bracket.sl))
        self.assertEqual(
            (trade.exit_ms, trade.exit_fill, trade.qty),
            (minutes_after(signal, 30), money.exit_fill, money.qty),
        )
        self.assertEqual(
            (trade.fee_usdt, trade.pnl_usdt, trade.notional_usdt),
            (money.fee_usdt, money.pnl_usdt, 10_000.0),
        )

    def test_mode_d_levels_limit_and_band(self):
        cfg = variant(
            mode_d={
                "tp_persen": 0.3,
                "sl_persen": 0.1,
                "batas_waktu_menit": 20,
                "maks_jarak_ke_ema_kali_atr": 0.5,
            }
        )
        signal, outside = at(TUE, 10, 3), at(TUE, 11, 0)

        result = run_d({signal: 0.5, outside: 0.6}, at(TUE, 9), at(TUE, 13), cfg=cfg)

        (trade,) = result.trades
        bracket = bracket_prices(adverse_fill(100.0, SHORT, 0.01, False), SHORT, 0.3, 0.1)
        self.assertEqual((trade.signal_ms, trade.tp, trade.sl), (signal, bracket.tp, bracket.sl))
        self.assertEqual(trade.exit_ms, minutes_after(signal, 20))

    def test_pause_and_volatility_multiple(self):
        signals = [at(TUE, 10, 0), at(TUE, 10, 15)]
        hourly = StubHourly(fixed(context(atr14=2.5)))
        cfg = variant(umum={"jeda_menit": 14, "penjaga_volatilitas_kali_median_atr": 2.5})
        edits = {signals[0]: LONG_TP_TOUCH}  # out at 10:01: a pause of 14 is over at 10:15

        result = run_a(
            {t: SIGNAL for t in signals},
            at(TUE, 9),
            at(TUE, 13),
            hourly=hourly,
            edits=edits,
            cfg=cfg,
        )

        self.assertEqual(entries(result), signals)

    def test_weekly_window(self):
        cfg = variant(umum={"jendela_entry_mingguan": "tue 10:00-tue 10:30"})
        signals = [at(TUE, 9, 45), at(TUE, 10, 0), at(TUE, 10, 30)]

        result = run_a({t: SIGNAL for t in signals}, at(TUE, 9), at(TUE, 13), cfg=cfg)

        self.assertEqual((entries(result), counts(result)), ([at(TUE, 10, 0)], {"window": 2}))

    def test_confirmation_window_and_cap(self):
        touch = at(TUE, 10, 0)
        cases = {
            # Two candles: the touch and the next one.
            "two candles": (
                variant(mode_a={"konfirmasi_dalam_candle": 2}),
                {touch: TOUCH, touch + 2 * M15: CONFIRM},
                [],
            ),
            # A cap of 0.4 ATR: a close of 100.5 is above 100.4.
            "cap 0.4": (variant(mode_a={"maks_jarak_sma20_1h_kali_atr": 0.4}), {touch: SIGNAL}, []),
            "cap 0.5": (
                variant(mode_a={"maks_jarak_sma20_1h_kali_atr": 0.5}),
                {touch: SIGNAL},
                [touch],
            ),
        }
        for name, (cfg, kinds, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(entries(run_a(kinds, at(TUE, 9), at(TUE, 13), cfg=cfg)), expected)


class DataDefectTests(unittest.TestCase):
    """A signal that passes every gate always becomes a trade, or the run stops."""

    START, END, SIGNAL_AT = at(TUE, 9), at(TUE, 14), at(TUE, 10, 15)

    def run_on(self, bars_1m):
        frames = make_frames(
            bars_1m, StubHourly(), m15=candles(self.START, self.END - M15, {self.SIGNAL_AT: SIGNAL})
        )
        return simulate_run(frames, CFG, RunSpec(MODE_A, PAIR, "15m", NAIK, self.START, self.END))

    def assert_names_the_signal(self, caught):
        message = str(caught.exception)
        self.assertIn("2026-06-30T10:15:00+07:00", message)
        self.assertIn(str(self.SIGNAL_AT), message)

    def test_a_position_that_does_not_resolve_raises_naming_the_signal_time(self):
        hole = minutes_after(self.SIGNAL_AT, 30)
        cases = {
            "a missing minute inside the walk": concat(
                minutes(self.START, hole), minutes(hole + MINUTE_MS, self.END)
            ),
            "data ending before the exit": minutes(self.START, minutes_after(self.SIGNAL_AT, 60)),
        }
        for name, bars in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(RuntimeError) as caught:
                    self.run_on(bars)
                self.assert_names_the_signal(caught)

    def test_a_signal_without_a_1m_bar_opening_at_its_close_raises(self):
        signal = self.SIGNAL_AT
        off_grid = [
            (t + 30_000, 100.0, 100.0, 100.0, 100.0, 1.0)
            for t in range(self.START, self.END, MINUTE_MS)
        ]
        cases = {
            "a hole at the close": concat(
                minutes(self.START, signal), minutes(signal + MINUTE_MS, self.END)
            ),
            "data ending before the close": minutes(self.START, signal),
            "data starting after the close": minutes(signal + MINUTE_MS, self.END),
            "bars off the minute grid": bars_from_rows(off_grid, MINUTE_MS),
        }
        for name, bars in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(RuntimeError) as caught:
                    self.run_on(bars)
                self.assert_names_the_signal(caught)

    def test_a_missing_trigger_candle_inside_the_run_raises(self):
        start, end, missing = self.START, self.END, at(TUE, 11, 0)
        before = candles(start, missing - M15, {})
        after = candles(missing + M15, end - M15, {})
        series = TriggerSeries(concat(before.bars, after.bars), before.ema + after.ema)

        with self.assertRaisesRegex(RuntimeError, "2026-06-30T11:00:00\\+07:00"):
            run_a({}, start, end, series=series)


class ArgumentTests(unittest.TestCase):
    GOOD = dict(mode="A", pair=PAIR, tf="15m", kondisi_gate=NAIK, start_ms=0, end_ms=1)

    def test_run_spec_refuses_a_run_phase_1_does_not_have(self):
        bad = [
            {"mode": "B"},
            {"mode": "a"},
            {"mode": None},
            {"tf": "3m"},
            {"tf": "1h"},
            {"tf": "15M"},
            {"mode": "D", "tf": "15m", "kondisi_gate": TURUN},
            {"mode": "D", "tf": "5m", "kondisi_gate": TURUN},
            {"kondisi_gate": TURUN},
            {"kondisi_gate": SIDEWAYS},
            {"kondisi_gate": TIDAK_ENTRY},
            {"kondisi_gate": "naik"},
            {"mode": "D", "tf": "3m", "kondisi_gate": SIDEWAYS},
            {"mode": "D", "tf": "3m", "kondisi_gate": TIDAK_ENTRY},
            {"start_ms": 2, "end_ms": 1},
            {"start_ms": 0.0},
            {"end_ms": True},
            {"start_ms": "0"},
            {"end_ms": None},
        ]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                RunSpec(**{**self.GOOD, **change})

    def test_run_spec_takes_the_runs_of_phase_1(self):
        for change in (
            {},
            {"tf": "5m"},
            {"start_ms": 1},  # an empty run
            {"mode": "D", "tf": "3m", "kondisi_gate": TURUN},
            {"mode": "D", "tf": "3m", "kondisi_gate": NAIK},
        ):
            with self.subTest(change=change):
                spec = RunSpec(**{**self.GOOD, **change})
                self.assertEqual(spec.mode, change.get("mode", "A"))

    def test_frames_and_config_that_do_not_fit_the_run_are_refused(self):
        start, end = at(TUE, 9), at(TUE, 13)
        bars = minutes(start - M15, end)
        series = candles(start, end - M15, {})
        wrong_interval = candles(start, end - M5, {}, M5)
        three_minute = stretch(start, end - M3, {})
        spec_a = RunSpec(MODE_A, PAIR, "15m", NAIK, start, end)
        spec_d = RunSpec(MODE_D, PAIR, "3m", TURUN, start, end)
        no_5m = PairFrames(
            PAIR,
            bars,
            StubHourly(),
            MappingProxyType({"15m": series}),
            three_minute[0],
            three_minute[1],
            FrameParams.of(CFG),
        )
        cases = {
            "frames of another pair": (
                make_frames(bars, StubHourly(), m15=series, symbol="ETH-USDT"),
                CFG,
                spec_a,
                "frames are of 'ETH-USDT'",
            ),
            "a pair the config does not have": (
                make_frames(bars, StubHourly(), m15=series, symbol="SOL-USDT"),
                CFG,
                dataclasses.replace(spec_a, pair="SOL-USDT"),
                "not a pair of the config",
            ),
            "no candles of the timeframe": (
                no_5m,
                CFG,
                dataclasses.replace(spec_a, tf="5m"),
                "no 5m trigger candles",
            ),
            "5m candles under 15m": (
                make_frames(bars, StubHourly(), m15=wrong_interval),
                CFG,
                spec_a,
                "15m trigger candles are 300000 ms candles",
            ),
            "an EMA column of another length": (
                make_frames(bars, StubHourly(), m15=TriggerSeries(series.bars, series.ema[:-1])),
                CFG,
                spec_a,
                "16 candles but 15 EMA values",
            ),
            "a stretch column of another length": (
                make_frames(bars, StubHourly(), stretch_of=(three_minute[0], three_minute[1][:-1])),
                CFG,
                spec_d,
                "80 candles but 79 stretch values",
            ),
            "stretch candles that are not 3m": (
                make_frames(bars, StubHourly(), stretch_of=(series.bars, series.ema)),
                CFG,
                spec_d,
                "3m candles are 900000 ms candles",
            ),
            "1m bars that are not 1m": (
                make_frames(series.bars, StubHourly(), m15=series),
                CFG,
                spec_a,
                "expected 1m bars",
            ),
            "more than one position per pair": (
                make_frames(bars, StubHourly(), m15=series),
                variant(umum={"maks_posisi_per_pair": 2}),
                spec_a,
                "maks_posisi_per_pair is 2",
            ),
            # The EMA period is in the frames' series and the take-profit is read from the
            # config: a run of the two would trade a configuration nobody wrote.
            "frames built under another config": (
                make_frames(bars, StubHourly(), m15=series),
                variant(mode_a={"ema_pemicu": 10, "tp_persen": 0.30}),
                spec_a,
                "built under another config",
            ),
        }
        for name, (frames, cfg, spec, message) in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(ValueError, message):
                simulate_run(frames, cfg, spec)

    def test_frames_run_under_a_config_that_differs_only_in_numbers_the_loop_reads(self):
        # The pause, the take-profit and the confirmation window are read from the run's own
        # config, so the frames need not be built again for them.
        start, end = at(TUE, 9), at(TUE, 13)
        frames = make_frames(minutes(start - M15, end), StubHourly(),
                             m15=candles(start, end - M15, {at(TUE, 10, 15): SIGNAL}))
        cfg = variant(umum={"jeda_menit": 5},
                      mode_a={"tp_persen": 0.30, "konfirmasi_dalam_candle": 2})

        result = simulate_run(frames, cfg, RunSpec(MODE_A, PAIR, "15m", NAIK, start, end))

        (trade,) = result.trades
        fill = adverse_fill(100.0, LONG, 0.01, False)
        self.assertEqual(trade.tp, bracket_prices(fill, LONG, 0.30, 0.25).tp)


if __name__ == "__main__":
    unittest.main()
