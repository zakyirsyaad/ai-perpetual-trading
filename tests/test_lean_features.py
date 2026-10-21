"""Lean features computed from the closed 3m candles.

They are the numbers the lean entry payload (AI_ENTRY_PAYLOAD=lean) is built from, and they
ride in ``MarketContext.lean_features``. The legacy entry payload and the exit payload must
never see them.
"""

from __future__ import annotations

import json
import unittest

from bot.ai.compact_context import CompactDecisionContextBuilder, compact_json
from bot.data.market_signals import ExternalMarketSignals, MarketContext, TechnicalFeatures
from bot.intelligence.market_intelligence import lean_features_from_candles
from tests.test_compact_context import _context, _daily, legacy_entry_payload, legacy_golden_text, serialized_payload
from tests.test_compact_context import _settings as _builder_settings
from tests.test_market_intelligence import _FakeClient, _intelligence, _settings

LEAN_KEYS = {
    "bar_interval",
    "stretch_atr",
    "ema21_slope_atr_5_bars",
    "rsi_14",
    "change_pct_5_bars",
    "change_pct_20_bars",
    "atr_pct",
}
ATR_SCALED_KEYS = ("stretch_atr", "ema21_slope_atr_5_bars", "atr_pct")
RAMP_BARS = 400  # long enough for the EMA21 start-up transient to fall below 1e-15
RAMP_START = 1000.0
RAMP_TR = 2.0  # high = close + 1, low = close - 1, so every true range is 2


def _ramp(step: float, bars: int = RAMP_BARS) -> tuple[list[float], list[float], list[float]]:
    closes = [RAMP_START + step * index for index in range(bars)]
    return closes, [c + 1.0 for c in closes], [c - 1.0 for c in closes]


def _compute(closes: list[float], highs: list[float], lows: list[float], interval: str = "3m") -> dict:
    return lean_features_from_candles(bar_interval=interval, closes=closes, highs=highs, lows=lows)


class LeanFeatureValueTests(unittest.TestCase):
    def test_values_on_a_known_series(self) -> None:
        closes, highs, lows = _ramp(1.0)
        last = closes[-1]

        lean = _compute(closes, highs, lows)

        self.assertEqual(set(lean), LEAN_KEYS)
        self.assertEqual(lean["bar_interval"], "3m")
        # An EMA21 on a ramp of step 1 trails the last close by (21 - 1) / 2 = 10 bars.
        self.assertAlmostEqual(lean["stretch_atr"], 10.0 / RAMP_TR, delta=1e-9)
        # Five bars later the EMA21 has moved five steps.
        self.assertAlmostEqual(lean["ema21_slope_atr_5_bars"], 5.0 / RAMP_TR, delta=1e-9)
        self.assertAlmostEqual(lean["rsi_14"], 100.0, delta=1e-9)
        self.assertAlmostEqual(lean["change_pct_5_bars"], 5.0 / (last - 5.0) * 100.0, delta=1e-9)
        self.assertAlmostEqual(lean["change_pct_20_bars"], 20.0 / (last - 20.0) * 100.0, delta=1e-9)
        self.assertAlmostEqual(lean["atr_pct"], 100.0 * RAMP_TR / last, delta=1e-9)

    def test_a_falling_series_flips_the_signs(self) -> None:
        closes, highs, lows = _ramp(-1.0)
        last = closes[-1]

        lean = _compute(closes, highs, lows)

        self.assertAlmostEqual(lean["stretch_atr"], -10.0 / RAMP_TR, delta=1e-9)
        self.assertAlmostEqual(lean["ema21_slope_atr_5_bars"], -5.0 / RAMP_TR, delta=1e-9)
        self.assertAlmostEqual(lean["rsi_14"], 0.0, delta=1e-9)
        self.assertAlmostEqual(lean["change_pct_5_bars"], -5.0 / (last + 5.0) * 100.0, delta=1e-9)
        self.assertAlmostEqual(lean["change_pct_20_bars"], -20.0 / (last + 20.0) * 100.0, delta=1e-9)

    def test_the_bar_interval_is_the_configured_one(self) -> None:
        closes, highs, lows = _ramp(1.0)

        self.assertEqual(_compute(closes, highs, lows, interval="5m")["bar_interval"], "5m")

    def test_short_series_gives_none(self) -> None:
        # Five closes: not even the 5-bar change has its six closes.
        lean = _compute(*_ramp(1.0, bars=5))
        self.assertEqual(set(lean), LEAN_KEYS)
        self.assertEqual(lean["bar_interval"], "3m")
        self.assertEqual({key: lean[key] for key in LEAN_KEYS - {"bar_interval"}}, dict.fromkeys(LEAN_KEYS - {"bar_interval"}))

    def test_atr_scaled_values_wait_for_26_closes(self) -> None:
        # 26 = the 21 closes of an EMA21 plus the 5 bars of slope look-back.
        for bars in (20, 25):
            lean = _compute(*_ramp(1.0, bars=bars))
            for key in ATR_SCALED_KEYS:
                self.assertIsNone(lean[key], f"{key} with {bars} closes")
        lean = _compute(*_ramp(1.0, bars=26))
        for key in ATR_SCALED_KEYS:
            self.assertIsNotNone(lean[key], f"{key} with 26 closes")

    def test_rsi_and_changes_need_only_their_own_history(self) -> None:
        lean = _compute(*_ramp(1.0, bars=20))

        self.assertIsNotNone(lean["rsi_14"])  # 15 closes are enough
        self.assertIsNotNone(lean["change_pct_5_bars"])  # 6 closes are enough
        self.assertIsNone(lean["change_pct_20_bars"])  # 21 closes are needed
        self.assertIsNotNone(_compute(*_ramp(1.0, bars=21))["change_pct_20_bars"])
        self.assertIsNone(_compute(*_ramp(1.0, bars=14))["rsi_14"])
        self.assertIsNone(_compute(*_ramp(1.0, bars=5))["change_pct_5_bars"])

    def test_zero_atr_gives_none_for_atr_scaled_values(self) -> None:
        flat = [1000.0] * 60

        lean = _compute(flat, list(flat), list(flat))

        for key in ATR_SCALED_KEYS:
            self.assertIsNone(lean[key], key)
        # The shared indicator reads a window with no losses as fully overbought.
        self.assertEqual(lean["rsi_14"], 100.0)
        self.assertAlmostEqual(lean["change_pct_5_bars"], 0.0, delta=1e-12)
        self.assertAlmostEqual(lean["change_pct_20_bars"], 0.0, delta=1e-12)

    def test_a_zero_reference_price_gives_none_not_a_division_error(self) -> None:
        closes, highs, lows = _ramp(1.0, bars=40)
        closes[-1 - 5] = 0.0

        lean = _compute(closes, highs, lows)

        self.assertIsNone(lean["change_pct_5_bars"])

    def test_a_zero_last_close_gives_none_for_atr_scaled_values(self) -> None:
        closes, highs, lows = _ramp(1.0, bars=40)
        closes[-1] = 0.0

        lean = _compute(closes, highs, lows)

        for key in ATR_SCALED_KEYS:
            self.assertIsNone(lean[key], key)

    def test_no_candles_gives_none_everywhere(self) -> None:
        lean = _compute([], [], [])

        self.assertEqual(lean["bar_interval"], "3m")
        self.assertTrue(all(lean[key] is None for key in LEAN_KEYS - {"bar_interval"}))


class LeanFeaturesInTheContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_context_carries_lean_features_per_symbol(self) -> None:
        closes, highs, lows = _ramp(1.0)
        klines = [{"timestamp": float(i * 180_000), "open": c, "high": h, "low": low, "close": c, "volume": 10.0} for i, (c, h, low) in enumerate(zip(closes, highs, lows))]
        intelligence = _intelligence(_FakeClient(klines=klines), settings=_settings(ai_kline_limit=RAMP_BARS))

        context = await intelligence.build_context()

        self.assertEqual(set(context.lean_features), {"BTC/USDT"})
        lean = context.lean_features["BTC/USDT"]
        self.assertEqual(lean["bar_interval"], "3m")
        self.assertAlmostEqual(lean["stretch_atr"], 10.0 / RAMP_TR, delta=1e-9)
        self.assertEqual(context.to_dict()["lean_features"], context.lean_features)

    async def test_the_technicals_are_unchanged_by_the_lean_features(self) -> None:
        context = await _intelligence().build_context()

        technical = context.to_dict()["technicals"][0]

        self.assertEqual(set(technical), set(TechnicalFeatures.__dataclass_fields__))

    async def test_a_symbol_without_candles_has_no_lean_features(self) -> None:
        context = await _intelligence(_FakeClient(fail={"klines"})).build_context()

        self.assertEqual(context.lean_features, {})
        self.assertEqual(context.to_dict()["lean_features"], {})

    async def test_the_default_context_has_empty_lean_features(self) -> None:
        context = MarketContext(technicals=[], external_signals=ExternalMarketSignals(), intelligence={})

        self.assertEqual(context.lean_features, {})
        self.assertEqual(context.to_dict()["lean_features"], {})


class LegacyAndExitPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = CompactDecisionContextBuilder(_builder_settings())
        self.plain = _context()
        self.with_lean = {**_context(), "lean_features": {"BTC/USDT": {"bar_interval": "3m", "stretch_atr": 0.4}, "ETH/USDT": {"bar_interval": "3m", "stretch_atr": -1.2}}}

    def _entry(self, context: dict) -> dict:
        return self.builder.build_entry_payload(market_context=context, daily_state=_daily(), recent_performance={}, trade_memory={}, strategy_objective={})

    def test_legacy_and_exit_payloads_never_contain_lean_features(self) -> None:
        position = {"symbol": "BTC/USDT", "direction": "LONG", "exchange_size": 0.01, "entry_price": 60000.0}

        entry = self._entry(self.with_lean)
        exit_all = self.builder.build_exit_payload(position=position, market_context=self.with_lean)

        self.assertNotIn("lean_features", entry["market_context"])
        self.assertNotIn("lean_features", exit_all["market_context"])
        self.assertNotIn("lean_features", compact_json(entry))
        self.assertNotIn("lean_features", compact_json(exit_all))
        self.assertNotIn("stretch_atr", compact_json(entry))
        self.assertNotIn("stretch_atr", compact_json(exit_all))

    def test_payloads_are_identical_with_and_without_lean_features(self) -> None:
        position = {"symbol": "BTC/USDT", "direction": "LONG", "exchange_size": 0.01, "entry_price": 60000.0}

        self.assertEqual(json.dumps(self._entry(self.with_lean), sort_keys=True), json.dumps(self._entry(self.plain), sort_keys=True))
        self.assertEqual(
            json.dumps(self.builder.build_exit_payload(position=position, market_context=self.with_lean), sort_keys=True),
            json.dumps(self.builder.build_exit_payload(position=position, market_context=self.plain), sort_keys=True),
        )

    def test_the_golden_holds_with_lean_features_in_the_context(self) -> None:
        payload = legacy_entry_payload(self.builder, self.with_lean)

        self.assertEqual(serialized_payload(payload), legacy_golden_text())

    def test_the_dedupe_does_not_mutate_the_callers_context(self) -> None:
        self._entry(self.with_lean)

        self.assertIn("lean_features", self.with_lean)


if __name__ == "__main__":
    unittest.main()
