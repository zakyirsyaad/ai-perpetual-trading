from __future__ import annotations

import logging
import unittest
from decimal import Decimal

from bot.perps.config import PerpsConfig
from bot.perps.exchange_adapter import HealthStatus
from bot.perps.execution import ExecutionOutcome, PerpExecutionManager
from bot.perps.mock_exchange import MockExchangeAdapter


class PerpsExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_maker_entry_and_reduce_only_exits(self) -> None:
        adapter = MockExchangeAdapter(fill_after_status_checks=1)
        manager = PerpExecutionManager(
            config=PerpsConfig(entry_timeout_seconds=0.01, poll_interval_seconds=0),
            adapter=adapter,
            logger=logging.getLogger("test_perps_execution"),
        )

        result = await manager.process_signal(self._payload())

        self.assertEqual(result.outcome, ExecutionOutcome.ENTERED)
        self.assertEqual(adapter.margin_modes["BTC/USDT"], "isolated")
        self.assertEqual(adapter.leverage["BTC/USDT"], 3)
        self.assertGreater(result.filled_qty, Decimal("0"))
        reduce_only_orders = [order for order in adapter.orders.values() if order.request.reduce_only]
        self.assertEqual(len(reduce_only_orders), 2)
        self.assertTrue(all(order.request.reduce_only for order in reduce_only_orders))

    async def test_health_failure_blocks_new_entry(self) -> None:
        adapter = MockExchangeAdapter(health=HealthStatus(websocket_healthy=False, reason="stale ws"))
        manager = PerpExecutionManager(
            config=PerpsConfig(entry_timeout_seconds=0.01, poll_interval_seconds=0),
            adapter=adapter,
            logger=logging.getLogger("test_perps_execution"),
        )

        result = await manager.process_signal(self._payload())

        self.assertEqual(result.outcome, ExecutionOutcome.REJECTED)
        self.assertIn("websocket_unhealthy", result.reason)
        self.assertEqual(len(adapter.orders), 0)

    @staticmethod
    def _payload() -> dict:
        return {
            "symbol": "BTC/USDT",
            "timeframe": "1m",
            "side": "long",
            "confidence": 0.77,
            "expected_move_bps": 90,
            "stop_bps": 50,
            "take_bps": 100,
            "max_hold_minutes": 10,
            "rationale_codes": ["trend:bullish", "spread_ok"],
            "invalidation_reason": "breaks support",
            "model_latency_ms": 700,
        }


if __name__ == "__main__":
    unittest.main()
