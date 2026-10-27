"""The header shows realized and unrealized before the first block of a run exists.

The header read both figures from the newest block's totals, so after a restart it stayed a skeleton until the
first AI decision or close, which can be hours when the entry window is closed or the daily cap is reached. The
observer already holds both figures (the realized one is seeded from the store at startup), so it publishes them
on a small `/pnl` route, like `/volume` and `/usage`, and the page uses them until a block arrives.
"""
from __future__ import annotations

import json
import unittest

from bot.telemetry.bus import TelemetryBus
from bot.telemetry.hub import TelemetryHub
from bot.telemetry.observer import EngineTelemetry
from bot.telemetry.server import TelemetryServer
from bot.telemetry.wire import Meta, PnlSummary, to_wire

try:  # `python -m unittest tests.x` (package) and `discover -s tests` (top-level) both work
    from tests.test_telemetry_observer import position, trade
    from tests.test_telemetry_server import ORIGIN, SAMPLES, get, request
except ImportError:  # pragma: no cover - depends on how unittest was launched
    from test_telemetry_observer import position, trade
    from test_telemetry_server import ORIGIN, SAMPLES, get, request

META = Meta.model_validate(SAMPLES["meta"])


def _observer() -> tuple[EngineTelemetry, TelemetryHub]:
    hub = TelemetryHub(META, TelemetryBus())
    return EngineTelemetry(hub, ("BTC/USDT",), account_balance_usdt=1500.0, mark_interval_seconds=0.0), hub


class PnlWireTests(unittest.TestCase):
    def test_summary_dumps_the_camel_case_keys_the_dashboard_reads(self) -> None:
        summary = PnlSummary(realized_usd=-190.67, unrealized_usd=12.5)
        self.assertEqual(to_wire(summary), {"realizedUsd": -190.67, "unrealizedUsd": 12.5})

    def test_unknown_keys_are_refused_so_nothing_else_can_ride_along(self) -> None:
        with self.assertRaises(ValueError):
            PnlSummary.model_validate({"realizedUsd": 1.0, "unrealizedUsd": 0.0, "balance": 1500.0})


class ObserverPnlTests(unittest.TestCase):
    def test_the_stored_realized_figure_is_served_before_any_block_exists(self) -> None:
        observer, hub = _observer()
        observer.history_loaded([], realized_usd=-190.67)
        self.assertEqual(hub.pnl_body(), {"realizedUsd": -190.67, "unrealizedUsd": 0.0})
        self.assertEqual(hub.history_body()["BTC"], [])  # the gap this route fills: the header has no block to read

    def test_a_close_moves_realized_by_its_net_pnl(self) -> None:
        observer, hub = _observer()
        observer.history_loaded([], realized_usd=-190.67)
        observer.position_closed(trade(pnl=54.45), fee_usdt=25.0)
        self.assertAlmostEqual(hub.pnl_body()["realizedUsd"], -136.22)

    def test_unrealized_follows_the_open_position_and_returns_to_zero_on_close(self) -> None:
        observer, hub = _observer()
        held = position()
        observer.history_loaded([], realized_usd=0.0)
        observer.entry_filled(held, leverage=100, fee_usdt=0.04)
        self.assertEqual(hub.pnl_body(), {"realizedUsd": 0.0, "unrealizedUsd": 0.0})
        observer.position_marked(held, net_usdt=12.5)
        self.assertEqual(hub.pnl_body(), {"realizedUsd": 0.0, "unrealizedUsd": 12.5})
        observer.position_closed(trade(held, pnl=3.0), fee_usdt=0.04)
        self.assertEqual(hub.pnl_body(), {"realizedUsd": 3.0, "unrealizedUsd": 0.0})

    def test_an_entry_fill_publishes_the_figures_too(self) -> None:
        observer, hub = _observer()
        self.assertEqual(hub.pnl_body(), {})
        observer.entry_filled(position(), leverage=100, fee_usdt=0.04)
        self.assertEqual(hub.pnl_body(), {"realizedUsd": 0.0, "unrealizedUsd": 0.0})

    def test_a_resumed_position_publishes_the_figures_too(self) -> None:
        observer, hub = _observer()
        self.assertEqual(hub.pnl_body(), {})
        observer.position_resumed(position(), leverage=100)
        self.assertEqual(hub.pnl_body(), {"realizedUsd": 0.0, "unrealizedUsd": 0.0})

    def test_bad_input_never_raises_and_keeps_the_previous_figures(self) -> None:
        observer, hub = _observer()
        observer.history_loaded([], realized_usd=5.0)
        observer.history_loaded([], realized_usd=None)  # type: ignore[arg-type]
        self.assertEqual(hub.pnl_body()["realizedUsd"], 5.0)


class HubAndServerPnlTests(unittest.IsolatedAsyncioTestCase):
    def test_nothing_to_show_before_the_first_update(self) -> None:
        self.assertEqual(TelemetryHub(META, TelemetryBus()).pnl_body(), {})

    async def test_the_pnl_route_serves_the_hub_body(self) -> None:
        observer, hub = _observer()
        server = TelemetryServer(hub, host="127.0.0.1", port=0, allowed_origin=ORIGIN, request_timeout=0.5)
        await server.start()
        try:
            status, _, body = await request(server.bound_port, get("/pnl"))
            self.assertEqual((status, json.loads(body)), (200, {}))
            observer.history_loaded([], realized_usd=-190.67)
            status, _, body = await request(server.bound_port, get("/pnl"))
            self.assertEqual((status, json.loads(body)), (200, {"realizedUsd": -190.67, "unrealizedUsd": 0.0}))
        finally:
            await server.stop()


if __name__ == "__main__":
    unittest.main()
