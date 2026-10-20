from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

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
        performance = {"total_trades": 12, "records": [{"reason": "complete record", "pnl": 1.2}]}
        memory = {"lessons": ["one", "two", "three", "four"], "patterns": [{"raw": "all fields"}]}
        objective = {"mode": "ai_volume", "primary_goal": "full objective", "nested": {"keep": True}}
        payload = self.builder.build_entry_payload(market_context=self.context, daily_state=_daily(), recent_performance=performance, trade_memory=memory, strategy_objective=objective, arena_reviews=[{"role": "MarketAnalyzer", "payload": {"all": "values"}}])
        golden = json.loads(LEGACY_ENTRY_GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(json.dumps(payload, sort_keys=True)), golden)

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
