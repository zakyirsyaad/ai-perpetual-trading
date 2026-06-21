from __future__ import annotations

import unittest

from bot.exchange.leverage import resolve_okx_venue_leverage


class _TierClient:
    def __init__(self, tiers_by_symbol: dict[str, list[dict[str, str]]], fail: bool = False) -> None:
        self.tiers_by_symbol = tiers_by_symbol
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def get_position_tiers(self, symbol: str, td_mode: str = "isolated") -> list[dict[str, str]]:
        self.calls.append((symbol, td_mode))
        if self.fail:
            raise RuntimeError("okx unavailable")
        return self.tiers_by_symbol[symbol]


class OKXLeverageResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_position_tiers_cap_configured_leverage_to_lowest_symbol_max(self) -> None:
        client = _TierClient(
            {
                "BTC/USDT": [{"tier": "1", "maxLever": "100"}, {"tier": "2", "maxLever": "50"}],
                "ETH/USDT": [{"tier": "1", "maxLever": "125"}, {"tier": "2", "maxLever": "75"}],
            }
        )

        resolution = await resolve_okx_venue_leverage(
            client,
            symbols=("BTC/USDT", "ETH/USDT"),
            configured_leverage=150,
            margin_mode="isolated",
        )

        self.assertEqual(resolution.effective_max_leverage, 100)
        self.assertEqual(resolution.resolved_leverage, 100)
        self.assertTrue(resolution.was_capped)
        self.assertEqual(resolution.symbol_max_leverage["BTC/USDT"], 100)
        self.assertEqual(resolution.symbol_max_leverage["ETH/USDT"], 125)
        self.assertEqual(client.calls, [("BTC/USDT", "isolated"), ("ETH/USDT", "isolated")])

    async def test_unreachable_okx_falls_back_to_100x_for_btc_eth_usdt_swaps(self) -> None:
        client = _TierClient({}, fail=True)

        resolution = await resolve_okx_venue_leverage(
            client,
            symbols=("BTC/USDT", "ETH/USDT"),
            configured_leverage=150,
            margin_mode="isolated",
        )

        self.assertEqual(resolution.source, "fallback")
        self.assertEqual(resolution.effective_max_leverage, 100)
        self.assertEqual(resolution.resolved_leverage, 100)
        self.assertTrue(resolution.was_capped)


if __name__ == "__main__":
    unittest.main()
