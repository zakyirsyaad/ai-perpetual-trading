"""`src/flat_check.py`: FLAT only when the venue and the store both say so. Unreadable is never flat.

Fakes only, no network. `CANARY` stands for any secret or response body that must never reach an output.
"""
from __future__ import annotations

import contextlib
import io
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

import flat_check
from bot.config import Settings
from bot.exchange.contracts import ExchangeAPIError, PositionDetails
from bot.storage.supabase_trade_store import SupabaseTradeStore

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "src" / "flat_check.py"
CANARY = "x-test-canary-value-7"


def position(symbol: str, size: float = 0.0, side: str | None = None) -> PositionDetails:
    return PositionDetails(
        symbol=symbol, exchange_size=size, base_quantity=size, avg_price=None, mark_price=None,
        liquidation_price=None, pos_side=side, raw={},
    )


class FakeClient:
    def __init__(self, sizes: dict | None = None, errors: dict | None = None, close_error: Exception | None = None) -> None:
        self.sizes = sizes or {}  # symbol -> (size, side)
        self.errors = errors or {}  # symbol -> exception to raise
        self.close_error = close_error
        self.calls: list[str] = []
        self.closed = False

    async def get_symbol_position_details(self, symbol: str) -> PositionDetails:
        self.calls.append(symbol)
        if symbol in self.errors:
            raise self.errors[symbol]
        size, side = self.sizes.get(symbol, (0.0, None))
        return position(symbol, size, side)

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakePositions:
    def __init__(self, rows: list | None = None, error: Exception | None = None) -> None:
        self.rows = rows or []
        self.error = error
        self.reads = 0

    async def open_position_rows(self) -> list:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return list(self.rows)


class FakeHTTP:
    """Stands in for httpx.AsyncClient inside SupabaseTradeStore: records every GET."""

    def __init__(self, payload=None, status: int = 200) -> None:
        self.payload = [] if payload is None else payload
        self.status = status
        self.gets: list[dict] = []
        self.closed = False

    async def get(self, url, params=None, headers=None):
        self.gets.append({"url": url, "params": dict(params or []), "headers": headers})
        return httpx.Response(self.status, request=httpx.Request("GET", url), json=self.payload)

    async def aclose(self) -> None:
        self.closed = True


def bot_settings(**overrides) -> SimpleNamespace:
    """What flat_check reads: the client's connection settings, the store's, and the symbols."""
    base = dict(
        symbols=("BTC/USDT",),
        bingx_api_key="x-test-key-1", bingx_secret_key="x-test-secret-2",
        bingx_base_url="https://open-api-vst.bingx.com", bingx_env="vst",
        bingx_margin_mode="isolated", bingx_position_mode="hedge",
        request_timeout_seconds=5, bingx_recv_window_ms=5000, bingx_source_key="BX-AI-SKILL",
        bingx_insecure_ssl=False,
        supabase_url="https://x-test-project.example.test", supabase_service_role_key="x-test-service-role-3",
        supabase_schema="long_forward", supabase_trades_table="trades",
        supabase_ai_decisions_table="ai_decisions", supabase_ai_exit_verdicts_table="ai_exit_verdicts",
        supabase_ai_call_metrics_table="ai_call_metrics", supabase_open_positions_table="open_position_state",
        exchange_name="bingx", volume_cycle_started_at=None, trading_timezone="Asia/Jakarta",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def persisted_row(**overrides) -> dict:
    row = {"symbol": "BTC/USDT", "direction": "LONG", "status": "open", "quantity": 0.01}
    row.update(overrides)
    return row


class CheckFlatTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, *, symbols=("BTC/USDT",), sizes=None, errors=None, rows=None, store_error=None):
        client = FakeClient(sizes, errors)
        positions = FakePositions(rows, store_error)
        report = await flat_check.check_flat(SimpleNamespace(symbols=symbols), client, positions)
        return report, client, positions

    async def test_flat_when_every_symbol_is_flat_and_nothing_is_persisted(self) -> None:
        report, client, positions = await self.check(symbols=("BTC/USDT", "ETH/USDT"))
        self.assertTrue(report.flat)
        self.assertEqual(report.lines, ("FLAT",))
        self.assertEqual(client.calls, ["BTC/USDT", "ETH/USDT"])
        self.assertEqual(positions.reads, 1)

    async def test_an_open_venue_position_is_reported_with_side_and_size(self) -> None:
        report, _, _ = await self.check(sizes={"BTC/USDT": (0.01, "LONG")})
        self.assertFalse(report.flat)
        self.assertEqual(report.lines, ("OPEN venue BTC/USDT side=LONG size=0.01",))

    async def test_every_configured_symbol_is_checked(self) -> None:
        report, client, _ = await self.check(symbols=("BTC/USDT", "ETH/USDT"), sizes={"ETH/USDT": (0.5, "SHORT")})
        self.assertFalse(report.flat)
        self.assertEqual(report.lines, ("OPEN venue ETH/USDT side=SHORT size=0.5",))
        self.assertEqual(client.calls, ["BTC/USDT", "ETH/USDT"])

    async def test_a_venue_position_without_a_side_still_counts(self) -> None:
        report, _, _ = await self.check(sizes={"BTC/USDT": (0.01, None)})
        self.assertEqual(report.lines, ("OPEN venue BTC/USDT side=? size=0.01",))

    async def test_a_persisted_position_counts_even_when_the_venue_is_flat(self) -> None:
        report, _, _ = await self.check(rows=[persisted_row()])
        self.assertFalse(report.flat)
        self.assertEqual(report.lines, ("OPEN persisted BTC/USDT LONG status=open quantity=0.01",))

    async def test_both_sources_are_reported_when_both_are_open(self) -> None:
        report, _, _ = await self.check(
            sizes={"BTC/USDT": (0.01, "LONG")}, rows=[persisted_row(), persisted_row(symbol="ETH/USDT", status="closing")],
        )
        self.assertEqual(
            report.lines,
            (
                "OPEN venue BTC/USDT side=LONG size=0.01",
                "OPEN persisted BTC/USDT LONG status=open quantity=0.01",
                "OPEN persisted ETH/USDT LONG status=closing quantity=0.01",
            ),
        )

    async def test_an_unreadable_venue_is_never_flat(self) -> None:
        report, _, positions = await self.check(errors={"BTC/USDT": ExchangeAPIError("boom", venue="bingx")})
        self.assertFalse(report.flat)
        self.assertEqual(report.lines, ("UNREADABLE ExchangeAPIError (venue position of BTC/USDT)",))
        self.assertEqual(positions.reads, 1)  # one broken source does not stop the other from being read

    async def test_an_unreadable_store_is_never_flat(self) -> None:
        report, client, _ = await self.check(store_error=RuntimeError("down"))
        self.assertFalse(report.flat)
        self.assertEqual(report.lines, ("UNREADABLE RuntimeError (persisted open positions)",))
        self.assertEqual(client.calls, ["BTC/USDT"])

    async def test_unreadable_does_not_hide_an_open_position(self) -> None:
        report, _, _ = await self.check(
            symbols=("BTC/USDT", "ETH/USDT"),
            errors={"BTC/USDT": TimeoutError("slow")},
            sizes={"ETH/USDT": (0.5, "SHORT")},
            store_error=ValueError("bad"),
        )
        self.assertEqual(
            report.lines,
            (
                "UNREADABLE TimeoutError (venue position of BTC/USDT)",
                "OPEN venue ETH/USDT side=SHORT size=0.5",
                "UNREADABLE ValueError (persisted open positions)",
            ),
        )

    async def test_exception_messages_never_reach_the_report(self) -> None:
        report, _, _ = await self.check(
            errors={"BTC/USDT": ExchangeAPIError(f"signed url {CANARY}", payload={"k": CANARY}, venue="bingx")},
            store_error=RuntimeError(f"body={CANARY}"),
        )
        self.assertNotIn(CANARY, "\n".join(report.lines))
        self.assertFalse(report.flat)

    async def test_no_symbols_means_nothing_was_checked_so_it_is_not_flat(self) -> None:
        report, client, _ = await self.check(symbols=())
        self.assertFalse(report.flat)
        self.assertEqual(len(report.lines), 1)
        self.assertTrue(report.lines[0].startswith("UNREADABLE NoSymbols"))
        self.assertEqual(client.calls, [])


class PersistedPositionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.http = FakeHTTP()
        patcher = mock.patch.object(httpx, "AsyncClient", return_value=self.http)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = SupabaseTradeStore(bot_settings(), logging.getLogger("test.flat_check"))
        self.reader = flat_check.PersistedPositions(self.store)

    async def test_it_reads_the_schema_the_settings_name_and_every_unclosed_row_of_this_exchange(self) -> None:
        self.http.payload = [persisted_row()]
        rows = await self.reader.open_position_rows()
        self.assertEqual(rows, [persisted_row()])
        self.assertEqual(len(self.http.gets), 1)
        request = self.http.gets[0]
        self.assertEqual(request["url"], "https://x-test-project.example.test/rest/v1/open_position_state")
        self.assertEqual(request["headers"]["Accept-Profile"], "long_forward")
        self.assertEqual(request["params"]["exchange"], "eq.bingx")
        self.assertEqual(request["params"]["status"], "neq.closed")

    async def test_another_schema_is_read_when_the_settings_say_so(self) -> None:
        store = SupabaseTradeStore(bot_settings(supabase_schema="public"), logging.getLogger("test.flat_check"))
        await flat_check.PersistedPositions(store).open_position_rows()
        self.assertEqual(self.http.gets[-1]["headers"]["Accept-Profile"], "public")

    async def test_a_read_failure_raises_where_the_stores_own_reader_returns_an_empty_list(self) -> None:
        self.http.status = 500
        self.http.payload = {"message": CANARY}
        with self.assertLogs("test.flat_check", level="WARNING"):  # it logs the body and returns nothing
            self.assertEqual(await self.store.list_open_positions(), [])  # the trap: an unreachable store looks flat
        with self.assertRaises(RuntimeError):
            await self.reader.open_position_rows()

    async def test_an_answer_that_is_not_a_list_raises(self) -> None:
        self.http.payload = {"unexpected": "shape"}
        with self.assertRaises(RuntimeError):
            await self.reader.open_position_rows()


class MainTests(unittest.TestCase):
    def run_main(self, *, client=None, http=None, settings=None, settings_error=None, client_error=None, argv=()):
        client = client or FakeClient()
        http = http or FakeHTTP()
        built: dict = {}

        def make_client(**kwargs):
            if client_error is not None:
                raise client_error
            built["client_kwargs"] = kwargs
            return client

        def make_settings():
            if settings_error is not None:
                raise settings_error
            return settings or bot_settings()

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(flat_check, "Settings", side_effect=make_settings), \
                mock.patch.object(flat_check, "BingXFuturesClient", side_effect=make_client), \
                mock.patch.object(httpx, "AsyncClient", return_value=http), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = flat_check.main(list(argv))
        return code, out.getvalue(), err.getvalue(), client, http, built

    def test_flat_prints_exactly_flat_and_exits_0(self) -> None:
        code, out, err, client, http, _ = self.run_main()
        self.assertEqual((code, out), (0, "FLAT\n"))
        self.assertIn("long_forward", err)  # which schema was read, so a wrong directory shows at once
        self.assertIn("BTC/USDT", err)
        self.assertTrue(client.closed)
        self.assertTrue(http.closed)

    def test_the_store_is_read_with_the_schema_of_the_directorys_settings(self) -> None:
        _, _, _, _, http, _ = self.run_main(settings=bot_settings(supabase_schema="long_forward"))
        self.assertEqual(http.gets[0]["headers"]["Accept-Profile"], "long_forward")

    def test_an_open_venue_position_exits_1(self) -> None:
        code, out, _, _, _, _ = self.run_main(client=FakeClient(sizes={"BTC/USDT": (0.01, "LONG")}))
        self.assertEqual((code, out), (1, "OPEN venue BTC/USDT side=LONG size=0.01\n"))

    def test_a_persisted_position_exits_1(self) -> None:
        code, out, _, _, _, _ = self.run_main(http=FakeHTTP(payload=[persisted_row()]))
        self.assertEqual((code, out), (1, "OPEN persisted BTC/USDT LONG status=open quantity=0.01\n"))

    def test_a_store_that_answers_500_is_unreadable_and_its_body_stays_out_of_the_output(self) -> None:
        code, out, err, _, _, _ = self.run_main(http=FakeHTTP(payload={"message": CANARY}, status=500))
        self.assertEqual((code, out), (1, "UNREADABLE RuntimeError (persisted open positions)\n"))
        self.assertNotIn(CANARY, out + err)

    def test_a_venue_error_is_unreadable_and_its_message_stays_out_of_the_output(self) -> None:
        client = FakeClient(errors={"BTC/USDT": ExchangeAPIError(f"url {CANARY}", venue="bingx")})
        code, out, err, _, _, _ = self.run_main(client=client)
        self.assertEqual((code, out), (1, "UNREADABLE ExchangeAPIError (venue position of BTC/USDT)\n"))
        self.assertNotIn(CANARY, out + err)

    def test_settings_that_do_not_load_are_unreadable_and_say_nothing_else(self) -> None:
        code, out, err, client, _, built = self.run_main(settings_error=ValueError(f"input_value={CANARY}"))
        self.assertEqual((code, out), (1, "UNREADABLE ValueError (settings)\n"))
        self.assertNotIn(CANARY, out + err)
        self.assertNotIn("client_kwargs", built)
        self.assertFalse(client.closed)

    def test_a_client_that_cannot_be_built_is_unreadable_and_the_store_is_still_closed(self) -> None:
        http = FakeHTTP()
        code, out, err, _, _, _ = self.run_main(http=http, client_error=ValueError(CANARY))
        self.assertEqual((code, out), (1, "UNREADABLE ValueError (client or store setup)\n"))
        self.assertNotIn(CANARY, out + err)
        self.assertTrue(http.closed)

    def test_the_client_is_built_from_the_settings_without_logging_or_debug_options(self) -> None:
        _, _, _, _, _, built = self.run_main()
        kwargs = built["client_kwargs"]
        self.assertEqual(kwargs["api_key"], "x-test-key-1")
        self.assertEqual(kwargs["secret_key"], "x-test-secret-2")
        self.assertEqual(kwargs["base_url"], "https://open-api-vst.bingx.com")
        self.assertEqual(kwargs["env"], "vst")
        self.assertEqual(kwargs["source_key"], "BX-AI-SKILL")
        self.assertNotIn("debug_http", kwargs)
        self.assertNotIn("logger", kwargs)

    def test_a_failing_close_does_not_change_the_verdict(self) -> None:
        code, out, _, _, _, _ = self.run_main(client=FakeClient(close_error=RuntimeError("late")))
        self.assertEqual((code, out), (0, "FLAT\n"))

    def test_stray_arguments_are_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.run_main(argv=("--bogus",))
        self.assertEqual(caught.exception.code, 2)


class ScriptTests(unittest.TestCase):
    def test_a_bad_env_gives_unreadable_exit_1_and_leaks_nothing(self) -> None:
        # control: the settings error does carry the value, so the absence below means something
        with mock.patch.dict(os.environ, {"DECISION_TIMEFRAMES": CANARY}, clear=True):
            with self.assertRaises(Exception) as raised:
                Settings(_env_file=None)
        self.assertIn(CANARY, str(raised.exception))
        with tempfile.TemporaryDirectory() as tmp:  # a bot directory with no .env in it
            result = subprocess.run(
                [sys.executable, str(SCRIPT)],
                cwd=tmp, capture_output=True, text=True, check=False, timeout=60,
                env={"PYTHONPATH": str(REPO / "src"), "PATH": os.environ.get("PATH", ""), "DECISION_TIMEFRAMES": CANARY},
            )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "UNREADABLE ValidationError (settings)\n")
        self.assertNotIn(CANARY, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
