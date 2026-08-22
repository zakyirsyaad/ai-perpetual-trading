from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from bot.ai.compact_context import (
    CompactDecisionContextBuilder,
    compact_json,
    estimate_payload_tokens,
    payload_hash,
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        strategy_mode="ai_volume",
        symbols=("BTC/USDT", "ETH/USDT"),
        exchange_name="bingx",
        margin_mode="isolated",
        position_mode="hedge",
        taker_fee_pct=0.05,
        maker_fee_pct=-0.02,
        leverage=100,
        position_usdt=180.0,
        trade_target_per_day=28,
        max_stop_loss_per_day=2,
        max_consecutive_losses=3,
        max_daily_drawdown_usdt=100.0,
        ai_min_fee_adjusted_rr=0.1,
        ai_compact_max_factors_per_symbol=6,
        ai_compact_max_flow_signals=5,
        ai_compact_max_memory_lessons=3,
        position_hold_minutes=60,
        external_signal_cache_seconds=300,
    )


def _factor(symbol: str, name: str, score: float, category: str = "trend") -> dict:
    return {
        "symbol": symbol,
        "name": name,
        "category": category,
        "score": score,
        "direction": "LONG" if score > 0 else "SHORT",
        "value": score * 10,
        "percentile": 0.9,
        "reason": "a very long human readable explanation that the model does not need " * 3,
    }


def _technical(symbol: str, price: float) -> dict:
    return {
        "symbol": symbol,
        "last_price": price,
        "ema_fast": price * 1.001,
        "ema_slow": price * 0.999,
        "ema_21": price * 0.999,
        "rsi_14": 55.123456,
        "atr_14": price * 0.002,
        "macd_line": 1.23456789,
        "macd_signal": 1.1,
        "macd_hist": 0.13456789,
        "change_pct_1": 0.0123456,
        "change_pct_5": 0.234567,
        "volume": 1234.56789,
        "volume_change_pct": 12.3456,
        "funding_rate": 0.00012345,
        "open_interest": 98765.4321,
        "open_interest_change_pct": 1.23456,
        "spread_bps": 1.23456,
        "bid_depth_notional": 500000.123,
        "ask_depth_notional": 400000.456,
    }


def _full_context(symbols=("BTC/USDT", "ETH/USDT"), factors_per_symbol: int = 86) -> dict:
    snapshots = [
        _factor(symbol, f"factor_{index}", (index % 7 - 3) / 3.0)
        for symbol in symbols
        for index in range(factors_per_symbol)
    ]
    flow = [
        {
            "symbol": symbols[0],
            "name": f"signal_{index}",
            "direction": "LONG",
            "strength": 0.5,
            "reason": "long reason " * 20,
            "metadata": {"a": 1, "b": 2, "c": 3},
        }
        for index in range(12)
    ]
    return {
        "technicals": [_technical(symbol, 60000.0 + index) for index, symbol in enumerate(symbols)],
        "external_signals": {
            "coinmarketcap": {"payload": ["x"] * 200},
            "dune": {},
            "tradingview": {"summary": "BUY", "extra": ["y"] * 200},
            "collected_at": "2026-08-22T00:00:00+00:00",
        },
        "intelligence": {
            "regime": "normal",
            "risk_flags": ["dune_unavailable"],
            "symbol_scores": {
                symbol: {
                    "composite_score": 0.4321,
                    "trend_bias": "bullish",
                    "rsi": 55.1,
                    "funding_rate": 0.0001,
                    "open_interest": 1.0,
                }
                for symbol in symbols
            },
            "factor_intelligence": {
                "snapshots": [dict(item) for item in snapshots],
                "by_symbol": {symbol: [dict(item) for item in snapshots] for symbol in symbols},
                "top_factors": ["factor_1"],
                "top_factor_details": [dict(item) for item in snapshots[:8]],
            },
            "market_flow_signals": {
                "by_symbol": {symbols[0]: {"active": flow, "new_triggers": flow}},
                "active": flow,
                "new_triggers": flow,
                "trigger_names": [item["name"] for item in flow],
            },
        },
        "factor_snapshots": [dict(item) for item in snapshots],
        "active_flow_signals": flow,
    }


def _daily_state() -> SimpleNamespace:
    return SimpleNamespace(
        stop_losses=0,
        closed_trades=3,
        total_pnl_usdt=1.5,
        consecutive_losses=0,
    )


class SerializationTests(unittest.TestCase):
    def test_compact_json_has_no_spaces_and_sorted_keys(self) -> None:
        text = compact_json({"b": 1, "a": {"d": 2, "c": 3}})
        self.assertEqual(text, '{"a":{"c":3,"d":2},"b":1}')

    def test_payload_hash_is_stable_under_key_order(self) -> None:
        self.assertEqual(payload_hash({"a": 1, "b": 2}), payload_hash({"b": 2, "a": 1}))

    def test_payload_hash_changes_with_content(self) -> None:
        self.assertNotEqual(payload_hash({"a": 1}), payload_hash({"a": 2}))

    def test_estimate_payload_tokens(self) -> None:
        self.assertGreater(estimate_payload_tokens({"a": "x" * 400}), 90)


class EntryPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = CompactDecisionContextBuilder(_settings())
        self.context = _full_context()
        self.payload = self.builder.build_entry_payload(
            market_context=self.context,
            daily_state=_daily_state(),
            recent_performance={"total_trades": 12, "total_pnl_usdt": -0.5, "by_entry_reason_sample": ["x"] * 50},
            trade_memory={
                "sample_size": 40,
                "net_pnl_usdt": -1.0,
                "gross_pnl_usdt": 3.0,
                "fee_impact_usdt": 4.0,
                "lessons": [f"lesson {index}" for index in range(10)],
                "winning_patterns": [{"pattern": f"w{i}", "trades": 3, "net": 1.0, "examples": ["e"] * 5} for i in range(6)],
                "losing_patterns": [{"pattern": f"l{i}", "trades": 3, "net": -1.0, "examples": ["e"] * 5} for i in range(6)],
                "direction_performance": {"LONG": {"trades": 5}},
                "confidence_performance": {"high": {"trades": 5}},
            },
            strategy_objective={"mode": "ai_volume", "primary_goal": "build volume"},
        )

    def test_contains_one_technical_summary_per_symbol(self) -> None:
        symbols = [item["symbol"] for item in self.payload["symbols"]]
        self.assertEqual(symbols, ["BTC/USDT", "ETH/USDT"])

    def test_technical_values_are_rounded(self) -> None:
        tech = self.payload["symbols"][0]["technicals"]
        self.assertEqual(tech["rsi_14"], 55.12)
        self.assertEqual(tech["macd_hist"], 0.1346)

    def test_factor_aggregates_present(self) -> None:
        aggregates = self.payload["symbols"][0]["factor_aggregates"]
        self.assertIn("category_scores", aggregates)
        self.assertIn("directional_agreement", aggregates)
        self.assertIn("strongest_positive", aggregates)
        self.assertIn("strongest_negative", aggregates)
        self.assertEqual(aggregates["factor_count"], 86)

    def test_ranked_factor_list_is_small_and_field_limited(self) -> None:
        factors = self.payload["symbols"][0]["top_factors"]
        self.assertLessEqual(len(factors), 6)
        for item in factors:
            self.assertEqual(set(item), {"name", "category", "score", "direction", "percentile"})

    def test_flow_signals_capped_at_five(self) -> None:
        self.assertLessEqual(len(self.payload["flow_signals"]), 5)

    def test_regime_risk_flags_and_composite_scores_present(self) -> None:
        self.assertEqual(self.payload["market"]["regime"], "normal")
        self.assertIn("dune_unavailable", self.payload["market"]["risk_flags"])
        self.assertIn("BTC/USDT", self.payload["market"]["composite_scores"])

    def test_only_fresh_available_external_signals_are_included(self) -> None:
        external = self.payload["external_signals"]
        self.assertIn("tradingview", external)
        self.assertNotIn("dune", external)
        # Semantics are preserved, not reduced to a count.
        self.assertEqual(external["tradingview"]["summary"], "BUY")

    def test_no_duplicate_factor_collections(self) -> None:
        text = compact_json(self.payload)
        # The raw 86-factor snapshot arrays must never reach the model.
        self.assertNotIn("factor_snapshots", text)
        self.assertNotIn("by_symbol", text)
        self.assertNotIn("top_factor_details", text)
        # A given factor may appear at most twice per symbol: once in the ranked
        # list and once as a strongest_positive/negative label. Two symbols -> 4.
        self.assertLessEqual(text.count('"factor_0"'), 4)

    def test_verbose_factor_reasons_are_dropped(self) -> None:
        self.assertNotIn("human readable explanation", compact_json(self.payload))

    def test_risk_state_and_fees_and_allowed_symbols(self) -> None:
        self.assertEqual(self.payload["risk_state"]["closed_trades_today"], 3)
        self.assertEqual(self.payload["fees"]["round_trip_taker_fee_pct"], 0.1)
        self.assertEqual(self.payload["allowed_symbols"], ["BTC/USDT", "ETH/USDT"])
        self.assertEqual(self.payload["strategy_objective"]["mode"], "ai_volume")

    def test_memory_lessons_are_limited(self) -> None:
        memory = self.payload["trade_memory"]
        self.assertLessEqual(len(memory["lessons"]), 3)
        self.assertLessEqual(len(memory["winning_patterns"]), 3)
        self.assertLessEqual(len(memory["losing_patterns"]), 3)

    def test_two_symbol_entry_payload_is_within_token_budget(self) -> None:
        tokens = estimate_payload_tokens(self.payload)
        self.assertLessEqual(tokens, 4000, f"entry payload used {tokens} estimated tokens")

    def test_compaction_is_a_large_reduction_versus_raw_context(self) -> None:
        raw = estimate_payload_tokens(self.context)
        compact = estimate_payload_tokens(self.payload)
        self.assertLess(compact * 4, raw)

    def test_payload_hash_is_deterministic_for_same_context(self) -> None:
        again = self.builder.build_entry_payload(
            market_context=_full_context(),
            daily_state=_daily_state(),
            recent_performance={"total_trades": 12, "total_pnl_usdt": -0.5, "by_entry_reason_sample": []},
            trade_memory={"sample_size": 0, "lessons": [], "winning_patterns": [], "losing_patterns": []},
            strategy_objective={"mode": "ai_volume", "primary_goal": "build volume"},
        )
        self.assertEqual(
            payload_hash(self.payload["symbols"]),
            payload_hash(again["symbols"]),
        )


class ExternalSignalProjectionTests(unittest.TestCase):
    """Compaction must keep decision-relevant meaning, not just element counts."""

    def setUp(self) -> None:
        self.builder = CompactDecisionContextBuilder(_settings())

    def _external(self, payload: dict) -> dict:
        context = _full_context()
        context["external_signals"] = payload
        result = self.builder.build_entry_payload(
            market_context=context,
            daily_state=_daily_state(),
            recent_performance={},
            trade_memory={},
            strategy_objective={},
        )
        return result["external_signals"]

    def test_nested_coinmarketcap_quotes_keep_direction_and_change(self) -> None:
        external = self._external(
            {
                "coinmarketcap": {
                    "BTC": {
                        "price": 60000.0,
                        "percent_change_24h": 2.5,
                        "volume_change_24h": -12.0,
                        "market_cap_dominance": 54.2,
                        "cmc_rank": 1,
                        "last_updated": "2026-08-22T00:00:00Z",
                    }
                }
            }
        )
        btc = external["coinmarketcap"]["BTC"]
        self.assertEqual(btc["percent_change_24h"], 2.5)
        self.assertEqual(btc["price"], 60000.0)
        self.assertEqual(btc["volume_change_24h"], -12.0)

    def test_dune_bias_and_confidence_survive(self) -> None:
        external = self._external(
            {
                "dune": {
                    "builtin_signals": {
                        "eth_dex_volume_regime": {
                            "rows": [{"bias": "bullish_liquidity", "confidence": 0.7, "value": 1.2e8}],
                        }
                    }
                }
            }
        )
        text = compact_json(external)
        self.assertIn("bullish_liquidity", text)
        self.assertIn("0.7", text)

    def test_tradingview_recommendation_survives(self) -> None:
        external = self._external({"tradingview": {"summary": "STRONG_BUY", "confidence": 0.8}})
        self.assertEqual(external["tradingview"]["summary"], "STRONG_BUY")
        self.assertEqual(external["tradingview"]["confidence"], 0.8)

    def test_semantic_keys_win_over_noise_when_capped(self) -> None:
        payload = {f"noise_{index}": index for index in range(20)}
        payload["bias"] = "risk_on"
        payload["confidence"] = 0.65
        external = self._external({"tradingview": payload})
        projected = external["tradingview"]
        self.assertEqual(projected["bias"], "risk_on")
        self.assertEqual(projected["confidence"], 0.65)
        self.assertIn("_omitted", projected)

    def test_projection_stays_bounded_on_huge_payloads(self) -> None:
        huge = {
            "coinmarketcap": {f"SYM{i}": {"price": i, "percent_change_24h": i} for i in range(200)},
            "dune": {"rows": [{"v": i} for i in range(500)]},
            "tradingview": {"signals": ["x"] * 500},
        }
        external = self._external(huge)
        self.assertLessEqual(estimate_payload_tokens(external), 400)

    def test_entry_payload_still_within_budget_with_semantic_projection(self) -> None:
        context = _full_context()
        context["external_signals"] = {
            "coinmarketcap": {f"SYM{i}": {"price": i, "percent_change_24h": i} for i in range(50)},
            "tradingview": {"summary": "BUY", "confidence": 0.7},
            "collected_at": "2026-08-22T00:00:00+00:00",
        }
        payload = self.builder.build_entry_payload(
            market_context=context,
            daily_state=_daily_state(),
            recent_performance={},
            trade_memory={},
            strategy_objective={},
        )
        self.assertLessEqual(estimate_payload_tokens(payload), 4000)


class ExitPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = CompactDecisionContextBuilder(_settings())
        self.context = _full_context(symbols=("BTC/USDT",))
        self.payload = self.builder.build_exit_payload(
            position={
                "symbol": "BTC/USDT",
                "direction": "LONG",
                "entry_price": 60000.0,
                "current_price": 60100.0,
                "quantity_base": 0.1,
                "exchange_size": 0.1,
                "elapsed_seconds": 300.0,
                "unrealized_net_usdt": 3.21,
            },
            market_context=self.context,
        )

    def test_position_block_present(self) -> None:
        self.assertEqual(self.payload["position"]["symbol"], "BTC/USDT")
        self.assertEqual(self.payload["position"]["direction"], "LONG")

    def test_one_symbol_exit_payload_is_within_token_budget(self) -> None:
        tokens = estimate_payload_tokens(self.payload)
        self.assertLessEqual(tokens, 1500, f"exit payload used {tokens} estimated tokens")

    def test_exit_payload_has_no_duplicate_factor_collections(self) -> None:
        text = compact_json(self.payload)
        self.assertNotIn("factor_snapshots", text)
        self.assertNotIn("top_factor_details", text)

    def test_exit_payload_includes_constraints(self) -> None:
        self.assertIn("constraints", self.payload)
        self.assertEqual(self.payload["constraints"]["position_hold_minutes"], 60)


class EmptyContextTests(unittest.TestCase):
    def test_empty_context_does_not_crash(self) -> None:
        builder = CompactDecisionContextBuilder(_settings())
        payload = builder.build_entry_payload(
            market_context={},
            daily_state=_daily_state(),
            recent_performance={},
            trade_memory={},
            strategy_objective={},
        )
        self.assertEqual(payload["symbols"], [])
        self.assertIsInstance(json.loads(compact_json(payload)), dict)


if __name__ == "__main__":
    unittest.main()
