from __future__ import annotations

import unittest
from decimal import Decimal

from bot.exchange.okx_client import OKXFuturesClient, OKXInstrument
from bot.models import PositionDirection


class OKXFuturesClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_order_fill_parses_actual_fill_details(self) -> None:
        client = OKXFuturesClient(
            api_key="key",
            secret_key="secret",
            passphrase="passphrase",
            base_url="https://www.okx.com",
        )

        async def fake_signed_request(method, path, body=None):
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/api/v5/trade/order")
            self.assertEqual(body["ordId"], "123")
            return {
                "code": "0",
                "data": [
                    {
                        "ordId": "123",
                        "avgPx": "78319.5",
                        "accFillSz": "22.99",
                        "fee": "-9.0123",
                        "pnl": "7.6812",
                    }
                ],
            }

        async def fake_get_instrument(symbol):
            return OKXInstrument(
                inst_id="BTC-USDT-SWAP",
                contract_value=Decimal("0.01"),
                lot_size=Decimal("0.01"),
                min_size=Decimal("0.01"),
                tick_size=Decimal("0.1"),
            )

        client._signed_request = fake_signed_request
        client._get_instrument = fake_get_instrument

        try:
            fill = await client.get_order_fill("BTC/USDT", "123")
        finally:
            await client.close()

        self.assertIsNotNone(fill)
        self.assertEqual(fill.avg_price, 78319.5)
        self.assertEqual(fill.filled_size, 22.99)
        self.assertAlmostEqual(fill.base_quantity, 0.2299)
        self.assertEqual(fill.fee_usdt, 9.0123)
        self.assertEqual(fill.pnl_usdt, 7.6812)

    async def test_get_trade_fee_rates_parses_pct_from_account_api(self) -> None:
        client = OKXFuturesClient(
            api_key="key",
            secret_key="secret",
            passphrase="passphrase",
            base_url="https://www.okx.com",
        )

        async def fake_signed_request(method, path, body=None):
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/api/v5/account/trade-fee")
            self.assertEqual(body["instType"], "SWAP")
            self.assertEqual(body["instFamily"], "BTC-USDT")
            return {
                "code": "0",
                "data": [
                    {
                        "maker": "-0.0002",
                        "taker": "0.0005",
                    }
                ],
            }

        client._signed_request = fake_signed_request

        try:
            rates = await client.get_trade_fee_rates("BTC/USDT")
        finally:
            await client.close()

        self.assertAlmostEqual(rates.maker_fee_pct, -0.02)
        self.assertAlmostEqual(rates.taker_fee_pct, 0.05)

    async def test_get_order_book_parses_spread_and_depth_notional(self) -> None:
        client = OKXFuturesClient(
            api_key="key",
            secret_key="secret",
            passphrase="passphrase",
            base_url="https://www.okx.com",
        )

        async def fake_public_request(method, path, params=None):
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/api/v5/market/books")
            self.assertEqual(params["instId"], "BTC-USDT-SWAP")
            return {
                "code": "0",
                "data": [
                    {
                        "bids": [["100.0", "10"], ["99.9", "20"]],
                        "asks": [["100.1", "15"], ["100.2", "25"]],
                        "ts": "1760000000000",
                    }
                ],
            }

        async def fake_get_instrument(symbol):
            return OKXInstrument(
                inst_id="BTC-USDT-SWAP",
                contract_value=Decimal("0.01"),
                lot_size=Decimal("0.01"),
                min_size=Decimal("0.01"),
                tick_size=Decimal("0.1"),
            )

        client._public_request = fake_public_request
        client._get_instrument = fake_get_instrument

        try:
            book = await client.get_order_book("BTC/USDT", depth=2)
        finally:
            await client.close()

        self.assertEqual(book.best_bid, 100.0)
        self.assertEqual(book.best_ask, 100.1)
        self.assertGreater(book.spread_bps, 0)
        self.assertAlmostEqual(book.bid_depth_notional, 29.98)
        self.assertAlmostEqual(book.ask_depth_notional, 40.065)

    async def test_get_funding_rate_info_parses_next_funding_time(self) -> None:
        client = OKXFuturesClient(
            api_key="key",
            secret_key="secret",
            passphrase="passphrase",
            base_url="https://www.okx.com",
        )

        async def fake_public_request(method, path, params=None):
            self.assertEqual(path, "/api/v5/public/funding-rate")
            return {
                "code": "0",
                "data": [
                    {
                        "fundingRate": "0.0001",
                        "fundingTime": "1760000000000",
                    }
                ],
            }

        client._public_request = fake_public_request

        try:
            info = await client.get_funding_rate_info("BTC/USDT")
        finally:
            await client.close()

        self.assertEqual(info.funding_rate, 0.0001)
        self.assertIsNotNone(info.next_funding_time)

    async def test_place_post_only_limit_order_uses_limit_payload(self) -> None:
        client = OKXFuturesClient(
            api_key="key",
            secret_key="secret",
            passphrase="passphrase",
            base_url="https://www.okx.com",
            margin_mode="isolated",
        )

        async def fake_signed_request(method, path, body=None):
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/api/v5/trade/order")
            self.assertEqual(body["ordType"], "post_only")
            self.assertEqual(body["px"], "100.1")
            self.assertEqual(body["tdMode"], "isolated")
            self.assertEqual(body["posSide"], "long")
            return {"code": "0", "data": [{"ordId": "abc"}]}

        client._signed_request = fake_signed_request

        try:
            response = await client.place_limit_order(
                "BTC/USDT",
                direction=PositionDirection.LONG,
                size=1.5,
                price=100.1,
                post_only=True,
            )
        finally:
            await client.close()

        self.assertEqual(response["data"][0]["ordId"], "abc")

    async def test_flatten_position_closes_short_with_reduce_only_buy(self) -> None:
        client = OKXFuturesClient(
            api_key="key",
            secret_key="secret",
            passphrase="passphrase",
            base_url="https://www.okx.com",
            instrument_type="SWAP",
        )
        captured: dict[str, object] = {}

        async def fake_details(symbol):
            from bot.exchange.okx_client import OKXPositionDetails

            return OKXPositionDetails(
                symbol=symbol,
                exchange_size=104.78,
                base_quantity=10.478,
                avg_price=1717.82,
                mark_price=1716.0,
                liquidation_price=1727.0,
                pos_side="short",
                raw={},
            )

        async def fake_signed_request(method, path, body=None):
            captured["path"] = path
            captured["body"] = body
            return {"code": "0", "data": [{"ordId": "close-1", "sCode": "0"}]}

        client.get_symbol_position_details = fake_details
        client._signed_request = fake_signed_request

        resp = await client.flatten_position("ETH/USDT")
        self.assertEqual(resp["data"][0]["ordId"], "close-1")
        self.assertEqual(captured["path"], "/api/v5/trade/order")
        # Closing a SHORT must be a reduce-only BUY on posSide short.
        self.assertEqual(captured["body"]["side"], "buy")
        self.assertEqual(captured["body"]["posSide"], "short")
        self.assertEqual(captured["body"]["reduceOnly"], "true")

    async def test_flatten_position_noops_when_flat(self) -> None:
        client = OKXFuturesClient(
            api_key="key",
            secret_key="secret",
            passphrase="passphrase",
            base_url="https://www.okx.com",
        )

        async def fake_details(symbol):
            from bot.exchange.okx_client import OKXPositionDetails

            return OKXPositionDetails(
                symbol=symbol,
                exchange_size=0.0,
                base_quantity=0.0,
                avg_price=None,
                mark_price=None,
                liquidation_price=None,
                pos_side=None,
                raw={},
            )

        client.get_symbol_position_details = fake_details
        self.assertIsNone(await client.flatten_position("ETH/USDT"))


if __name__ == "__main__":
    unittest.main()
