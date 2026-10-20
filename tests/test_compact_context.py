from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot.ai import compact_context
from bot.ai.compact_context import (
    COMPACT_PROMPT_VERSION,
    ENTRY_LEARNING_NOTE_MAX_CHARS,
    ENTRY_REASON_MAX_CHARS,
    CompactDecisionContextBuilder,
    compact_json,
    payload_hash,
)

LEGACY_ENTRY_GOLDEN = Path(__file__).parent / "fixtures" / "legacy_entry_payload.json"


def _settings() -> SimpleNamespace:
    return SimpleNamespace(strategy_mode="ai_volume", symbols=("BTC/USDT", "ETH/USDT"), exchange_name="bingx", margin_mode="isolated", position_mode="hedge", taker_fee_pct=0.05, leverage=100, position_usdt=180.0, trade_target_per_day=28, max_stop_loss_per_day=2, max_consecutive_losses=3, max_daily_drawdown_usdt=100.0, ai_min_fee_adjusted_rr=0.1, ai_min_confidence=0.75, position_hold_minutes=60)


def _context() -> dict:
    factors = [{"symbol": symbol, "name": f"factor-{symbol}", "score": 0.9, "direction": "LONG", "reason": "full reason"} for symbol in ("BTC/USDT", "ETH/USDT")]
    flow = [{"symbol": "BTC/USDT", "name": "volume_spike", "direction": "LONG", "strength": 0.8}]
    new_flow = [{"symbol": "BTC/USDT", "name": "fresh_btc_trigger", "direction": "LONG", "strength": 0.9}]
    return {"technicals": [{"symbol": "BTC/USDT", "last_price": 60000.0, "ema_21": 59900.0, "volume": 1234.56}, {"symbol": "ETH/USDT", "last_price": 3000.0, "ema_21": 2990.0, "volume": 999.0}], "external_signals": {"coinmarketcap": {"BTC": {"symbol": "BTC", "bias": "bullish"}, "ETH": {"symbol": "ETH", "bias": "bearish"}, "eth": {"bias": "bearish"}, "market_regime": "risk_on"}, "dune": {"rows": [{"symbol": "BTC", "value": 1}, {"symbol": "ETH", "value": 2}, {"ticker": "ETHUSDT", "value": 3}, {"pair": "eth/usdt", "value": 4}, {"symbol": "MARKET", "state": "global"}], "macro": "global", "GLOBAL": {"USD": 1, "RSI": 55}}, "tradingview": {"summary": "BUY", "all_rows": [1, 2, 3]}}, "intelligence": {"regime": "normal", "symbol_scores": {"BTC/USDT": {"score": 0.8}, "ETH/USDT": {"score": -0.4}}, "factor_intelligence": {"snapshots": factors, "by_symbol": {"BTC/USDT": factors}, "top_factors": ["factor-BTC/USDT", "factor-ETH/USDT"]}, "market_flow_signals": {"active": flow, "new_triggers": new_flow, "trigger_names": ["volume_spike", "eth_only_trigger"]}}, "factor_snapshots": factors, "active_flow_signals": flow, "new_flow_signals": new_flow, "liquidation_constraints": {"BTC/USDT": {"status": "available", "max_long_stop_loss_pct": 0.9}}}


def _daily() -> SimpleNamespace:
    return SimpleNamespace(closed_trades=3, stop_losses=1, consecutive_losses=1, total_pnl_usdt=-2.0)


def serialized_payload(payload: dict) -> str:
    """The text form the golden was captured in. Comparing text, not parsed objects, keeps
    an int that turned into a float (100 vs 100.0) from passing: parsed equality treats
    them as equal, but the bytes sent to the model, and payload_hash, change."""
    return json.dumps(payload, sort_keys=True, indent=2)


def legacy_golden_text() -> str:
    """The fixture's exact text, less the single newline the capture script appended."""
    text = LEGACY_ENTRY_GOLDEN.read_bytes().decode("utf-8")
    return text[:-1] if text.endswith("\n") else text


def legacy_entry_payload(builder: CompactDecisionContextBuilder, context: dict) -> dict:
    """The entry payload the golden fixture was captured from, for the given context."""
    performance = {"total_trades": 12, "records": [{"reason": "complete record", "pnl": 1.2}]}
    memory = {"lessons": ["one", "two", "three", "four"], "patterns": [{"raw": "all fields"}]}
    objective = {"mode": "ai_volume", "primary_goal": "full objective", "nested": {"keep": True}}
    return builder.build_entry_payload(market_context=context, daily_state=_daily(), recent_performance=performance, trade_memory=memory, strategy_objective=objective, arena_reviews=[{"role": "MarketAnalyzer", "payload": {"all": "values"}}])


class ParityPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = CompactDecisionContextBuilder(_settings())
        self.context = _context()

    def test_entry_payload_preserves_full_decision_context_and_removes_only_mirrors(self) -> None:
        performance = {"total_trades": 12, "records": [{"reason": "complete record", "pnl": 1.2}]}
        memory = {"lessons": ["one", "two", "three", "four"], "patterns": [{"raw": "all fields"}]}
        objective = {"mode": "ai_volume", "primary_goal": "full objective", "nested": {"keep": True}}
        payload = self.builder.build_entry_payload(market_context=self.context, daily_state=_daily(), recent_performance=performance, trade_memory=memory, strategy_objective=objective, arena_reviews=[{"role": "MarketAnalyzer", "payload": {"all": "values"}}])
        self.assertEqual(payload["task"], "ai_volume_entry_decision")
        self.assertEqual(payload["strategy_objective"], objective)
        self.assertEqual(payload["recent_executed_trade_performance"], performance)
        self.assertEqual(payload["learned_trade_memory"], memory)
        self.assertEqual(payload["market_context"]["technicals"], self.context["technicals"])
        self.assertEqual(payload["market_context"]["external_signals"], self.context["external_signals"])
        self.assertIn("liquidation_constraints", payload["market_context"])
        self.assertEqual(payload["market_context"]["factor_snapshots"], self.context["factor_snapshots"])
        self.assertNotIn("factor_intelligence", payload["market_context"]["intelligence"])
        self.assertNotIn("market_flow_signals", payload["market_context"]["intelligence"])
        self.assertTrue(payload["output_rules"]["open_requires_final_stop_strictly_before_estimated_liquidation_or_hold"])

    def test_legacy_payload_matches_the_golden(self) -> None:
        # The legacy entry payload is pinned byte for byte: a change here is a change
        # to what account 1 sends the model. Regenerate the fixture only on purpose.
        payload = legacy_entry_payload(self.builder, self.context)
        self.assertEqual(serialized_payload(payload), legacy_golden_text())

    def test_exit_payload_keeps_full_position_constraints_and_one_symbol_context(self) -> None:
        payload = self.builder.build_exit_payload(position={"symbol": "BTC/USDT", "direction": "LONG", "exchange_size": 0.012345678, "entry_price": 60000.0}, market_context=self.context)
        self.assertEqual(payload["position"]["exchange_size"], 0.012345678)
        self.assertEqual(payload["constraints"]["min_confidence"], 0.75)
        self.assertEqual(payload["constraints"]["position_hold_minutes"], 60)
        self.assertEqual(len(payload["market_context"]["technicals"]), 1)
        self.assertEqual(payload["market_context"]["technicals"][0]["symbol"], "BTC/USDT")
        self.assertEqual(payload["market_context"]["factor_snapshots"], [self.context["factor_snapshots"][0]])
        self.assertEqual(payload["market_context"]["active_flow_signals"], self.context["active_flow_signals"])
        self.assertEqual(payload["market_context"]["intelligence"]["symbol_scores"], {"BTC/USDT": {"score": 0.8}})
        text = compact_json(payload["market_context"])
        self.assertNotIn("ETH/USDT", text)
        self.assertNotIn("factor-ETH/USDT", text)
        self.assertNotIn("eth_only_trigger", text)
        self.assertIn("factor-BTC/USDT", text)
        self.assertIn("volume_spike", text)
        self.assertIn("fresh_btc_trigger", text)
        self.assertEqual(text.count("fresh_btc_trigger"), 1)
        self.assertEqual(payload["market_context"]["external_signals"], self.context["external_signals"])
        self.assertIn('"ETH"', text)
        self.assertIn("ETHUSDT", text)
        self.assertIn('"eth"', text)
        self.assertIn("risk_on", text)
        self.assertIn("global", text)
        self.assertIn('"MARKET"', text)
        self.assertIn('"GLOBAL"', text)
        self.assertIn('"USD"', text)
        self.assertIn('"RSI"', text)

    def test_entry_payload_exposes_the_hard_minimum_confidence(self) -> None:
        payload = self.builder.build_entry_payload(
            market_context=self.context,
            daily_state=_daily(),
            recent_performance={},
            trade_memory={},
            strategy_objective={},
        )
        self.assertEqual(payload["risk_constraints"]["min_confidence"], 0.75)

    def test_prompt_version_and_hash_are_stable(self) -> None:
        self.assertEqual(COMPACT_PROMPT_VERSION, "okx-context-parity-v3")
        self.assertEqual(payload_hash({"b": 2, "a": 1}), payload_hash({"a": 1, "b": 2}))
        self.assertEqual(compact_json({"b": 2, "a": 1}), '{"a":1,"b":2}')


if __name__ == "__main__":
    unittest.main()


class EmptyExternalProviderTests(unittest.TestCase):
    """A provider that returned no data must not reach the model at all.

    `DuneProvider.fetch()` returns its skeleton `{"saved_queries": {},
    "builtin_signals": {}}` even when every sub-request failed, and
    `TradingViewSignalProvider.fetch()` returns `{}` when the relay is
    unreachable. The parity payload forwards `external_signals` verbatim, so the
    model is shown a provider that is present but carries nothing, and the entry
    prompt then reduces confidence for a provider that is simply not configured.

    Providers that did return data keep full parity.
    """

    def setUp(self) -> None:
        self.builder = CompactDecisionContextBuilder(_settings())

    def _external(self, **providers) -> dict:
        context = _context()
        context["external_signals"] = dict(providers)
        return context

    def _payload_external(self, context: dict) -> dict:
        payload = self.builder.build_entry_payload(
            market_context=context,
            daily_state=_daily(),
            recent_performance={},
            trade_memory={},
            strategy_objective={},
            arena_reviews=[],
        )
        return payload["market_context"]["external_signals"]

    def test_dune_skeleton_with_no_results_is_dropped(self) -> None:
        external = self._payload_external(
            self._external(dune={"saved_queries": {}, "builtin_signals": {}})
        )
        self.assertNotIn("dune", external)

    def test_unreachable_tradingview_empty_dict_is_dropped(self) -> None:
        external = self._payload_external(self._external(tradingview={}))
        self.assertNotIn("tradingview", external)

    def test_provider_with_data_keeps_full_parity(self) -> None:
        payload = {"BTC": {"symbol": "BTC", "bias": "bullish"}, "market_regime": "risk_on"}
        external = self._payload_external(self._external(coinmarketcap=payload))
        self.assertEqual(external["coinmarketcap"], payload)

    def test_partially_populated_provider_is_kept(self) -> None:
        payload = {"saved_queries": {}, "builtin_signals": {"eth_dex_volume_regime": {"rows": [1]}}}
        external = self._payload_external(self._external(dune=payload))
        self.assertEqual(external["dune"], payload)

    def test_zero_and_false_are_data_not_emptiness(self) -> None:
        payload = {"funding_rate": 0, "is_bullish": False}
        external = self._payload_external(self._external(coinmarketcap=payload))
        self.assertEqual(external["coinmarketcap"], payload)

    def test_deeply_nested_empty_containers_are_dropped(self) -> None:
        external = self._payload_external(self._external(dune={"a": {"b": {"c": []}}}))
        self.assertNotIn("dune", external)

    def test_non_provider_metadata_is_never_pruned(self) -> None:
        context = self._external(tradingview={}, collected_at="2026-09-02T14:00:00Z")
        external = self._payload_external(context)
        self.assertNotIn("tradingview", external)
        self.assertEqual(external["collected_at"], "2026-09-02T14:00:00Z")

    def test_source_context_is_not_mutated(self) -> None:
        context = self._external(tradingview={})
        self._payload_external(context)
        self.assertIn("tradingview", context["external_signals"])


class OutputRulesDeclareTextLimitsTests(unittest.TestCase):
    """The model cannot respect a length limit it is never told about."""

    def test_entry_output_rules_state_the_free_text_limits(self) -> None:
        builder = CompactDecisionContextBuilder(_settings())
        payload = builder.build_entry_payload(
            market_context=_context(),
            daily_state=_daily(),
            recent_performance={},
            trade_memory={},
            strategy_objective={},
        )
        rules = payload["output_rules"]
        self.assertEqual(rules["reason_max_characters"], ENTRY_REASON_MAX_CHARS)
        self.assertEqual(rules["learning_note_max_characters"], ENTRY_LEARNING_NOTE_MAX_CHARS)


# The lean objective, verbatim from the spec (section 4). A literal here, not the module's
# constant, so a drift in either one fails the test.
LEAN_GOAL = (
    "Open a position only when the setup shows a directional edge larger than the round-trip fee; "
    "HOLD is the expected answer most ticks."
)
STRETCH_RULE = "an entry beyond this limit is rejected in code"


def _lean_context() -> dict:
    context = _context()
    context["lean_features"] = {
        "BTC/USDT": {
            "bar_interval": "3m",
            "stretch_atr": 0.123456789,
            "ema21_slope_atr_5_bars": -0.041234567,
            "rsi_14": 48.123456,
            "change_pct_5_bars": -0.056789123,
            "change_pct_20_bars": 0.2,
            "atr_pct": 0.091234567,
        },
        "ETH/USDT": {
            "bar_interval": "3m",
            "stretch_atr": None,
            "ema21_slope_atr_5_bars": None,
            "rsi_14": 51.0,
            "change_pct_5_bars": None,
            "change_pct_20_bars": None,
            "atr_pct": None,
        },
    }
    return context


def _gate_context() -> dict:
    """A regime verdict as `RegimeGate.as_payload()` hands it to the orchestrator."""
    return {
        "kondisi": "TURUN",
        "allowed": True,
        "reason": "turun",
        "allowed_direction": "short",
        "atr14_1h": 500.0,
        "atr_median": 450.0,
        "hour_close_ms": 1_792_000_000_000,
        "sma20_1h": 61_000.0,
        "sma50_1h": 62_000.0,
        "close_4h": 60_500.0,
        "ema50_4h": 61_500.0,
    }


class LeanPayloadTests(unittest.TestCase):
    """`AI_ENTRY_PAYLOAD=lean`: the arena's factor and flow records, its reviews and the trade memory
    leave the request; the 3m features, the stretch rule, the regime and the ledger lines join it.
    `market_context` keeps the technicals, the pruned external signals and the liquidation limits."""

    def setUp(self) -> None:
        self.builder = CompactDecisionContextBuilder(_settings())
        self.context = _lean_context()

    def _lean(self, **overrides) -> dict:
        arguments = dict(
            market_context=self.context,
            daily_state=_daily(),
            strategy_objective={"mode": "ai_volume", "primary_goal": LEAN_GOAL, "volume_profile": {"margin_per_entry_usdt": 180.0}},
            allowed_sides=["short"],
            entry_rules={"stretch_atr": 0.1235, "max_stretch_atr": 0.75, "rule": STRETCH_RULE},
            market_regime={},
            ledger_lines=[],
        )
        arguments.update(overrides)
        return self.builder.build_lean_entry_payload(**arguments)

    def test_lean_drops_the_factor_flow_arena_and_memory_blocks(self) -> None:
        payload = self._lean()

        for key in ("arena_reviews", "learned_trade_memory", "recent_executed_trade_performance"):
            self.assertNotIn(key, payload)
        market = payload["market_context"]
        for key in ("factor_snapshots", "active_flow_signals", "new_flow_signals", "lean_features"):
            self.assertNotIn(key, market)
        for key in ("symbol_scores", "factor_intelligence", "market_flow_signals"):
            self.assertNotIn(key, market["intelligence"])
        text = compact_json(payload)
        for name in ("factor-BTC/USDT", "volume_spike", "fresh_btc_trigger", "eth_only_trigger"):
            self.assertNotIn(name, text)

    def test_lean_keeps_technicals_external_and_liquidation(self) -> None:
        payload = self._lean()
        legacy = legacy_entry_payload(self.builder, self.context)

        market = payload["market_context"]
        self.assertEqual(market["technicals"], self.context["technicals"])
        self.assertEqual(market["external_signals"], legacy["market_context"]["external_signals"])
        self.assertEqual(market["liquidation_constraints"], self.context["liquidation_constraints"])
        self.assertEqual(market["intelligence"], {"regime": "normal"})
        self.assertEqual(payload["task"], legacy["task"])
        self.assertEqual(payload["execution_environment"], legacy["execution_environment"])
        self.assertEqual(payload["strategy_objective"]["volume_profile"], {"margin_per_entry_usdt": 180.0})
        self.assertEqual(payload["entry_rules"], {"stretch_atr": 0.1235, "max_stretch_atr": 0.75, "rule": STRETCH_RULE})

    def test_lean_features_are_the_3m_features_per_symbol_rounded(self) -> None:
        features = self._lean()["features"]

        self.assertEqual(
            features["BTC/USDT"],
            {
                "bar_interval": "3m",
                "stretch_atr": 0.1235,
                "ema21_slope_atr_5_bars": -0.0412,
                "rsi_14": 48.1235,
                "change_pct_5_bars": -0.0568,
                "change_pct_20_bars": 0.2,
                "atr_pct": 0.0912,
            },
        )
        # a value the candles could not give stays None, never a placeholder
        self.assertEqual(features["ETH/USDT"], self.context["lean_features"]["ETH/USDT"])

    def test_lean_allowed_actions_follow_the_sides(self) -> None:
        legacy_rules = legacy_entry_payload(self.builder, self.context)["output_rules"]
        for sides, actions in (
            (["short"], ["OPEN_SHORT", "HOLD"]),
            (["long"], ["OPEN_LONG", "HOLD"]),
            (["long", "short"], ["OPEN_LONG", "OPEN_SHORT", "HOLD"]),
        ):
            with self.subTest(sides=sides):
                rules = self._lean(allowed_sides=sides)["output_rules"]
                self.assertEqual(rules["allowed_actions"], actions)
                # every other output rule is today's
                self.assertEqual({**rules, "allowed_actions": None}, {**legacy_rules, "allowed_actions": None})

    def test_lean_payload_refuses_an_empty_side_list(self) -> None:
        for sides in ([], (), ["any"], ["short", "short"]):
            with self.subTest(sides=sides):
                with self.assertRaises(ValueError):
                    self._lean(allowed_sides=sides)

    def test_lean_has_no_min_confidence(self) -> None:
        payload = self._lean()
        legacy = legacy_entry_payload(self.builder, self.context)["risk_constraints"]

        self.assertNotIn("min_confidence", payload["risk_constraints"])
        self.assertNotIn("min_confidence", compact_json(payload))
        self.assertEqual(payload["risk_constraints"], {k: v for k, v in legacy.items() if k != "min_confidence"})

    def test_lean_objective_text_is_exact(self) -> None:
        self.assertEqual(compact_context.LEAN_PRIMARY_GOAL, LEAN_GOAL)
        self.assertEqual(compact_context.STRETCH_RULE_TEXT, STRETCH_RULE)
        objective = {"mode": "ai_volume", "primary_goal": compact_context.LEAN_PRIMARY_GOAL}
        payload = self._lean(strategy_objective=objective)
        self.assertEqual(payload["strategy_objective"], {"mode": "ai_volume", "primary_goal": LEAN_GOAL})
        text = compact_json(payload)
        self.assertNotIn("every tick", text)
        self.assertNotIn("sit out", text)

    def test_lean_regime_block_adds_price_vs_sma20(self) -> None:
        gate = _gate_context()

        regime = compact_context.lean_market_regime(gate, 60_000.0)

        expected = {key: value for key, value in gate.items() if key != "allowed"}
        expected["price_vs_sma20_1h_atr"] = -2.0   # (60,000 - 61,000) / 500
        self.assertEqual(regime, expected)
        self.assertEqual(self._lean(market_regime=regime)["market_regime"], expected)
        self.assertEqual(gate["allowed"], True)   # the caller's verdict is not mutated

    def test_lean_regime_distance_is_none_without_its_inputs(self) -> None:
        for name, gate, last in (
            ("no sma20", {**_gate_context(), "sma20_1h": None}, 60_000.0),
            ("no atr", {**_gate_context(), "atr14_1h": None}, 60_000.0),
            ("zero atr", {**_gate_context(), "atr14_1h": 0.0}, 60_000.0),
            ("no price", _gate_context(), None),
            ("zero price", _gate_context(), 0.0),
            ("nan price", _gate_context(), float("nan")),
        ):
            with self.subTest(name):
                self.assertIsNone(compact_context.lean_market_regime(gate, last)["price_vs_sma20_1h_atr"])

    def test_lean_regime_block_is_absent_without_a_gate_context(self) -> None:
        self.assertEqual(compact_context.lean_market_regime(None, 60_000.0), {})
        self.assertEqual(compact_context.lean_market_regime({}, 60_000.0), {})
        self.assertNotIn("market_regime", self._lean(market_regime={}))
        self.assertNotIn("market_regime", self._lean(market_regime=None))

    def test_lean_setup_ledger_only_when_lines_exist(self) -> None:
        line = {"side": "short", "bucket": "SHORT|TURUN|atr_mid", "n": 3, "mean_bps": -15.0, "status": "blocked"}

        self.assertNotIn("setup_ledger", self._lean(ledger_lines=[]))
        self.assertEqual(self._lean(ledger_lines=[line])["setup_ledger"], [line])

    def test_lean_does_not_mutate_the_context(self) -> None:
        before = json.loads(json.dumps(self.context))
        self._lean(market_regime=compact_context.lean_market_regime(_gate_context(), 60_000.0))
        self.assertEqual(self.context, before)

    def test_lean_payload_is_deterministic(self) -> None:
        self.assertEqual(compact_json(self._lean()), compact_json(self._lean()))
