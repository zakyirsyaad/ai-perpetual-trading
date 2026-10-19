"""Tests for bot.core.regime_gate: the NAIK regime gate and the volatility guard of the LONG bot.

The venue is a fake client that serves closed candles of a synthetic market, the way
``BingXFuturesClient.get_klines`` does (oldest first, a candle only once ``open + interval <= now``).
The expected labels are never computed here from a second copy of the maths: the equality test
builds the backtest's own ``HourlyFrame`` from the candles the gate was served.
"""

import asyncio
import dataclasses
import logging
import math
import unittest
from pathlib import Path

from bot.config import Settings
from bot.core.regime_gate import (
    ATR_MEDIAN_HOURS,
    ATR_PERIOD_1H,
    EMA_PERIOD_4H,
    SMA_FAST_PERIOD_1H,
    SMA_SLOW_PERIOD_1H,
    RegimeGate,
    RegimeVerdict,
)
from bot.strategy.modes.config import SelectorConfig, load_modes_config
from research.mode_backtest import frames
from research.mode_backtest.frames import HourlyFrame
from research.pine_gate.bars import bars_from_rows

HOUR_MS = 3_600_000
FOUR_HOUR_MS = 4 * HOUR_MS
START_MS = 118_000 * FOUR_HOUR_MS  # a 4h boundary, so 4h buckets line up with the venue's
HISTORY_HOURS = 2_000  # enough 4h candles (500) for the 4h EMA50 to leave its warm-up
SEL = SelectorConfig(sma_cepat_1h=20, sma_lambat_1h=50, ema_4h=50, sideways_jarak_kali_atr=0.2)
MODES_YAML = Path(__file__).resolve().parents[1] / "config" / "modes.yaml"
SYMBOL = "BTC/USDT"

MARKETS = {
    "NAIK": lambda k: 1000.0 + 0.5 * k,
    "TURUN": lambda k: 3000.0 - 0.5 * k,
    "SIDEWAYS": lambda k: 1000.0,
    # a long fall, then a short rise: the 1h SMAs turn up while the 4h close is still under its EMA
    "TIDAK_ENTRY": lambda k: 2000.0 - 0.5 * k if k < 1960 else 2000.0 - 0.5 * 1960 + (k - 1960),
}


def hourly_rows(price, spike=0.0):
    """``HISTORY_HOURS`` hourly candles as (open_ms, open, high, low, close); the last one's high
    is raised by ``spike`` so its range, and with it the ATR, jumps."""
    rows = []
    for k in range(HISTORY_HOURS):
        close, open_ = price(k), price(k - 1) if k else price(k)
        spread = 0.5 + 0.2 * math.sin(k)
        high = max(open_, close) + spread + (spike if k == HISTORY_HOURS - 1 else 0.0)
        rows.append((START_MS + k * HOUR_MS, open_, high, min(open_, close) - spread, close))
    return rows


def hour_close(k):
    """The time at which hourly candle ``k`` has just closed."""
    return START_MS + (k + 1) * HOUR_MS


class FakeClient:
    """Serves the closed 1h and 4h candles of ``rows`` at ``now_ms``, and counts the calls."""

    def __init__(self, rows, now_ms=None, fail=False, max_1h=None):
        self.rows = rows
        self.max_1h = max_1h  # the venue answers with at most this many 1h candles
        self.now_ms = now_ms if now_ms is not None else hour_close(HISTORY_HOURS - 1)
        self.fail = fail
        self.calls = []

    def _four_hour_rows(self):
        buckets = {}
        for ts, o, h, low, c in self.rows:
            buckets.setdefault(ts // FOUR_HOUR_MS, []).append((ts, o, h, low, c))
        out = []
        for _key, hours in sorted(buckets.items()):
            start = hours[0][0]
            if len(hours) == 4 and start + FOUR_HOUR_MS <= self.now_ms:
                out.append(
                    (start, hours[0][1], max(x[2] for x in hours), min(x[3] for x in hours), hours[-1][4])
                )
        return out

    async def get_klines(self, symbol, interval="1m", limit=120):
        self.calls.append((symbol, interval, limit))
        if self.fail:
            raise RuntimeError("venue down")
        if interval == "1h":
            source, step = self.rows, HOUR_MS
        elif interval == "4h":
            source, step = self._four_hour_rows(), FOUR_HOUR_MS
        else:
            raise AssertionError(f"unexpected interval {interval}")
        closed = [r for r in source if r[0] + step <= self.now_ms]
        if interval == "1h" and self.max_1h is not None:
            closed = closed[-self.max_1h:]
        return [
            {"timestamp": ts, "open": o, "high": h, "low": low, "close": c, "volume": 1.0}
            for ts, o, h, low, c in closed[-limit:]
        ]


QUIET = logging.getLogger("test.regime_gate")
QUIET.addHandler(logging.NullHandler())
QUIET.propagate = False


def make_gate(client, mode="naik", volatility_mult=2.0, sideways_mult=0.2, logger=None):
    return RegimeGate(
        client,
        SYMBOL,
        mode,
        volatility_mult,
        sideways_mult,
        logger or QUIET,
    )


class OffModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_makes_no_call_and_allows(self):
        client = FakeClient(hourly_rows(MARKETS["TURUN"]))
        gate = make_gate(client, mode="off")

        verdict = await gate.verdict()

        self.assertEqual(client.calls, [])
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.reason, "off")
        self.assertEqual(await gate.verdict(), verdict)


class ConditionTests(unittest.IsolatedAsyncioTestCase):
    async def test_naik_allows(self):
        gate = make_gate(FakeClient(hourly_rows(MARKETS["NAIK"])))

        verdict = await gate.verdict()

        self.assertEqual((verdict.kondisi, verdict.allowed, verdict.reason), ("NAIK", True, "naik"))
        self.assertEqual(verdict.hour_close_ms, hour_close(HISTORY_HOURS - 1))
        self.assertIsNotNone(verdict.atr14_1h)
        self.assertIsNotNone(verdict.atr_median)

    async def test_other_conditions_block(self):
        for kondisi in ("SIDEWAYS", "TURUN", "TIDAK_ENTRY"):
            with self.subTest(kondisi=kondisi):
                gate = make_gate(FakeClient(hourly_rows(MARKETS[kondisi])))

                verdict = await gate.verdict()

                self.assertEqual(verdict.kondisi, kondisi)
                self.assertFalse(verdict.allowed)
                self.assertEqual(verdict.reason, f"not_naik:{kondisi}")

    async def test_fetches_1h_limit_1000_and_4h_limit_500(self):
        client = FakeClient(hourly_rows(MARKETS["NAIK"]))

        await make_gate(client).verdict()

        self.assertEqual(sorted(client.calls), [(SYMBOL, "1h", 1000), (SYMBOL, "4h", 500)])

    async def test_sideways_multiple_is_the_one_given(self):
        # the rising market's SMA gap is far under 100 x ATR, so a wide band reads it as sideways
        narrow = await make_gate(
            FakeClient(hourly_rows(MARKETS["NAIK"])), sideways_mult=100.0
        ).verdict()

        self.assertEqual(narrow.kondisi, "SIDEWAYS")
        self.assertEqual(narrow.reason, "not_naik:SIDEWAYS")


class BacktestEqualityTests(unittest.TestCase):
    """The gate and the backtest's ``HourlyFrame`` read the same candles to the same labels."""

    def test_labels_match_the_backtest_hourly_frame(self):
        rows = hourly_rows(MARKETS["NAIK"])
        for k in range(HISTORY_HOURS - 10, HISTORY_HOURS):
            now_ms = hour_close(k)
            client = FakeClient(rows, now_ms=now_ms)
            gate = make_gate(client)

            verdict = asyncio.run(gate.verdict())

            served_1h = asyncio.run(client.get_klines(SYMBOL, "1h", 1000))
            served_4h = asyncio.run(client.get_klines(SYMBOL, "4h", 500))
            frame = HourlyFrame(
                bars_from_rows([_tuple(r) for r in served_1h], HOUR_MS),
                bars_from_rows([_tuple(r) for r in served_4h], FOUR_HOUR_MS),
                SEL,
            )
            expected = frame.context_at(now_ms)
            with self.subTest(hour_close=now_ms):
                self.assertEqual(verdict.kondisi, expected.kondisi)
                self.assertEqual(verdict.atr14_1h, expected.atr14_1h)
                self.assertEqual(verdict.atr_median, expected.atr_median)
                self.assertEqual(verdict.hour_close_ms, now_ms)

    def test_labels_match_the_backtest_for_every_condition(self):
        for kondisi, price in MARKETS.items():
            client = FakeClient(hourly_rows(price))
            verdict = asyncio.run(make_gate(client).verdict())
            frame = HourlyFrame(
                bars_from_rows([_tuple(r) for r in asyncio.run(client.get_klines(SYMBOL, "1h", 1000))], HOUR_MS),
                bars_from_rows([_tuple(r) for r in asyncio.run(client.get_klines(SYMBOL, "4h", 500))], FOUR_HOUR_MS),
                SEL,
            )
            with self.subTest(kondisi=kondisi):
                self.assertEqual(verdict.kondisi, kondisi)
                self.assertEqual(verdict.kondisi, frame.context_at(client.now_ms).kondisi)


class VolatilityGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocks_above_twice_the_median_and_off_at_zero(self):
        rows = hourly_rows(MARKETS["NAIK"], spike=100.0)

        guarded = await make_gate(FakeClient(rows), volatility_mult=2.0).verdict()
        unguarded = await make_gate(FakeClient(rows), volatility_mult=0.0).verdict()

        self.assertGreater(guarded.atr14_1h, 2.0 * guarded.atr_median)
        self.assertFalse(guarded.allowed)
        self.assertEqual(guarded.reason, "volatility")
        self.assertEqual(guarded.kondisi, "NAIK")
        self.assertTrue(unguarded.allowed)
        self.assertEqual(unguarded.reason, "naik")

    async def test_calm_market_passes_the_guard(self):
        verdict = await make_gate(FakeClient(hourly_rows(MARKETS["NAIK"])), volatility_mult=2.0).verdict()

        self.assertLessEqual(verdict.atr14_1h, 2.0 * verdict.atr_median)
        self.assertTrue(verdict.allowed)

    async def test_an_atr_exactly_at_the_limit_passes(self):
        rows = hourly_rows(MARKETS["NAIK"])
        probe = await make_gate(FakeClient(rows), volatility_mult=0.0).verdict()
        mult = probe.atr14_1h / probe.atr_median

        verdict = await make_gate(FakeClient(rows), volatility_mult=mult).verdict()

        self.assertTrue(verdict.allowed)


class ShortHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_history_blocks_with_a_reason_and_warns_once(self):
        client = FakeClient(hourly_rows(MARKETS["NAIK"]), max_1h=200)
        gate = make_gate(client, volatility_mult=2.0)

        with self.assertLogs("test.regime_gate", level="WARNING") as logs:
            first = await gate.verdict()
            second = await gate.verdict()  # the clock is far past the data: a fresh evaluation

        self.assertEqual(len(client.calls), 4)
        self.assertFalse(first.allowed)
        self.assertEqual(first.reason, "volatility_unknown")
        self.assertEqual(second.reason, first.reason)
        self.assertEqual(len([r for r in logs.records if r.levelno == logging.WARNING]), 1)

    async def test_too_few_candles_for_the_selector_is_history_short(self):
        verdict = await make_gate(FakeClient(hourly_rows(MARKETS["NAIK"]), max_1h=30)).verdict()

        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "history_short")

    async def test_unknown_median_blocks_only_when_the_guard_is_on(self):
        rows = hourly_rows(MARKETS["NAIK"])

        guarded = await make_gate(FakeClient(rows, max_1h=600), volatility_mult=2.0).verdict()
        unguarded = await make_gate(FakeClient(rows, max_1h=600), volatility_mult=0.0).verdict()

        self.assertEqual((guarded.allowed, guarded.reason), (False, "volatility_unknown"))
        self.assertIsNone(guarded.atr_median)
        self.assertEqual((unguarded.allowed, unguarded.reason), (True, "naik"))

    async def test_empty_answer_is_history_short(self):
        verdict = await make_gate(FakeClient([])).verdict()

        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "history_short")


class DataErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failing_venue_blocks_and_is_retried_on_the_next_call(self):
        client = FakeClient(hourly_rows(MARKETS["NAIK"]), fail=True)
        gate = make_gate(client)

        with self.assertLogs("test.regime_gate", level="WARNING"):
            failed = await gate.verdict()
        client.fail = False
        recovered = await gate.verdict()

        self.assertEqual((failed.allowed, failed.reason), (False, "data_error"))
        self.assertEqual((recovered.allowed, recovered.reason), (True, "naik"))

    async def test_a_hole_in_the_candles_blocks(self):
        rows = hourly_rows(MARKETS["NAIK"])
        del rows[-30]

        with self.assertLogs("test.regime_gate", level="WARNING"):
            verdict = await make_gate(FakeClient(rows)).verdict()

        self.assertEqual((verdict.allowed, verdict.reason), (False, "data_error"))

    async def test_a_non_finite_price_blocks(self):
        rows = hourly_rows(MARKETS["NAIK"])
        ts, o, h, low, c = rows[-5]
        rows[-5] = (ts, o, float("nan"), low, c)

        with self.assertLogs("test.regime_gate", level="WARNING"):
            verdict = await make_gate(FakeClient(rows)).verdict()

        self.assertEqual((verdict.allowed, verdict.reason), (False, "data_error"))


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_verdict_is_cached_until_the_next_hour_close(self):
        clock = {"now": hour_close(HISTORY_HOURS - 2)}
        client = FakeClient(hourly_rows(MARKETS["NAIK"]), now_ms=clock["now"])
        gate = RegimeGate(
            client, SYMBOL, "naik", 2.0, 0.2, QUIET,
            clock_ms=lambda: clock["now"],
        )

        first = await gate.verdict()
        clock["now"] += 30 * 60_000  # half an hour later: the same hour is still open
        second = await gate.verdict()

        self.assertEqual(len(client.calls), 2)  # one fetch per interval
        self.assertEqual(second, first)

        clock["now"] = hour_close(HISTORY_HOURS - 1)  # the next hour has closed
        client.now_ms = clock["now"]
        third = await gate.verdict()

        self.assertEqual(len(client.calls), 4)
        self.assertEqual(third.hour_close_ms, hour_close(HISTORY_HOURS - 1))


class PayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_payload_is_the_last_verdict(self):
        gate = make_gate(FakeClient(hourly_rows(MARKETS["TURUN"])))
        self.assertEqual(gate.as_payload(), {})

        verdict = await gate.verdict()
        payload = gate.as_payload()

        self.assertEqual(payload, dataclasses.asdict(verdict))
        self.assertEqual(payload["reason"], "not_naik:TURUN")


class VerdictTests(unittest.TestCase):
    def test_verdict_is_frozen(self):
        verdict = RegimeVerdict("NAIK", True, "naik", 1.0, 1.0, 0)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            verdict.allowed = False  # type: ignore[misc]

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            make_gate(FakeClient([]), mode="long")


# What a gate-on Settings must carry besides the gate: the LONG-only invariant is enforced by the
# direction gate in AIOrchestrator, which only runs for an AI strategy with the stretch gate enforcing.
LONG_ONLY_COMPANIONS = {
    "ENTRY_GATE_DIRECTION": "long",
    "ENTRY_STRETCH_GATE_MODE": "enforce",
    "STRATEGY_MODE": "ai_volume",
    "XAI_API_KEY": "test-xai",
}


class ConfigTests(unittest.TestCase):
    def settings(self, **env):
        env.setdefault("STRATEGY_MODE", "volume")
        return Settings(_env_file=None, BINGX_API_KEY="test-key", BINGX_SECRET_KEY="test-secret", **env)

    def gated(self, **override):
        env = {**LONG_ONLY_COMPANIONS, "ENTRY_REGIME_GATE": "naik", **override}
        return self.settings(**env)

    def test_defaults_leave_the_gate_off(self):
        settings = self.settings()

        self.assertEqual(settings.entry_regime_gate, "off")
        self.assertEqual(settings.entry_volatility_guard_mult, 0.0)

    def test_naik_is_accepted_and_normalised(self):
        self.assertEqual(self.gated(ENTRY_REGIME_GATE=" NAIK ").entry_regime_gate, "naik")

    def test_the_gate_with_all_its_companions_boots(self):
        settings = self.gated()

        self.assertEqual(settings.entry_gate_direction, "long")
        self.assertEqual(settings.entry_stretch_gate_mode, "enforce")

    def test_the_gate_refuses_to_boot_without_a_long_only_direction(self):
        for direction in ("any", "short"):
            with self.subTest(direction=direction):
                with self.assertRaises(ValueError) as caught:
                    self.gated(ENTRY_GATE_DIRECTION=direction)
                self.assertIn("ENTRY_GATE_DIRECTION", str(caught.exception))

    def test_the_gate_refuses_to_boot_unless_the_stretch_gate_enforces(self):
        for mode in ("off", "shadow"):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError) as caught:
                    self.gated(ENTRY_STRETCH_GATE_MODE=mode)
                self.assertIn("ENTRY_STRETCH_GATE_MODE", str(caught.exception))

    def test_the_gate_refuses_to_boot_with_the_deterministic_strategy(self):
        with self.assertRaises(ValueError) as caught:
            self.gated(STRATEGY_MODE="volume")
        self.assertIn("STRATEGY_MODE", str(caught.exception))

    def test_the_gate_accepts_either_ai_strategy(self):
        self.assertEqual(self.gated(STRATEGY_MODE="grok").strategy_mode, "grok")

    def test_the_live_style_settings_with_the_gate_off_still_construct(self):
        for direction in ("any", "short", "long"):
            for mode in ("off", "shadow", "enforce"):
                for strategy in ("volume", "grok", "ai_volume"):
                    with self.subTest(direction=direction, mode=mode, strategy=strategy):
                        settings = self.settings(
                            ENTRY_GATE_DIRECTION=direction, ENTRY_STRETCH_GATE_MODE=mode,
                            STRATEGY_MODE=strategy, XAI_API_KEY="test-xai",
                        )
                        self.assertEqual(settings.entry_regime_gate, "off")

    def test_config_refuses_an_unknown_regime_gate_value(self):
        with self.assertRaises(ValueError):
            self.settings(ENTRY_REGIME_GATE="turun")

    def test_config_refuses_a_negative_guard_multiple_and_a_non_positive_sideways_multiple(self):
        with self.assertRaises(ValueError):
            self.settings(ENTRY_VOLATILITY_GUARD_MULT=-1)
        with self.assertRaises(ValueError):
            self.settings(REGIME_SIDEWAYS_ATR_MULT=0)

    def test_default_sideways_multiple_matches_modes_yaml(self):
        selector = load_modes_config(MODES_YAML).pemilih_mode

        self.assertEqual(self.settings().regime_sideways_atr_mult, selector.sideways_jarak_kali_atr)


class BacktestConstantsTests(unittest.TestCase):
    """The live module may not import research, so it keeps its own copies of the numbers."""

    def test_constants_equal_the_backtest_and_the_modes_yaml(self):
        selector = load_modes_config(MODES_YAML).pemilih_mode

        self.assertEqual(ATR_PERIOD_1H, frames.ATR_PERIOD_1H)
        self.assertEqual(ATR_MEDIAN_HOURS, frames.ATR_MEDIAN_HOURS)
        self.assertEqual(SMA_FAST_PERIOD_1H, selector.sma_cepat_1h)
        self.assertEqual(SMA_SLOW_PERIOD_1H, selector.sma_lambat_1h)
        self.assertEqual(EMA_PERIOD_4H, selector.ema_4h)


def _tuple(row):
    return (row["timestamp"], row["open"], row["high"], row["low"], row["close"], row["volume"])


if __name__ == "__main__":
    unittest.main()
