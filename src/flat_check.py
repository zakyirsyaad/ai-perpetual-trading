"""Read-only flat check for one bot directory: is this account flat?

Run it from the bot's directory. That directory's `.env`, read through `Settings` like the bot
reads it, decides which BingX account and which Supabase schema are looked at:

    cd /home/ubuntu/ai-perp-long && PYTHONPATH=src .venv/bin/python src/flat_check.py --expect-schema long_forward

It asks the venue for the position rows of every symbol in SYMBOLS and reads the persisted open
positions (`open_position_state`) from the schema that `.env` names. It prints `FLAT` and exits 0
only when both are clear. Otherwise it prints one line per finding and exits 1:

    OPEN venue BTC-USDT side=LONG size=0.01
    OPEN persisted BTC/USDT LONG status=open quantity=0.01
    UNREADABLE VenueAnswerError (venue position of BTC/USDT)

A source that cannot be read is never flat. The venue answer is read strictly: it must hold a list of
rows with a numeric `positionAmt`, and any nonzero amount counts whatever symbol the row names (the
client's own reader maps a malformed answer to "no position"). `--expect-schema NAME` refuses to run
unless `.env` names that schema, so a run from the wrong directory fails instead of reporting another
account as flat.

Only exception class names are printed, never their text (a settings error carries the offending value,
a store error the response body), and nothing is logged. The context line on stderr names the symbols,
the BingX env and the schema: no secret, and enough to see at once which directory was read. It places,
cancels and closes nothing, and it does not look at pending orders: look at the venue app for those.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from bot.config import Settings
from bot.exchange.bingx_client import BingXFuturesClient
from bot.storage.supabase_trade_store import SupabaseTradeStore

_PERSISTED_PAGE = 50  # the engine reads at most this many; one row already means "not flat"


class PositionRows(Protocol):
    async def open_position_rows(self) -> list[dict[str, Any]]: ...


class VenueAnswerError(Exception):
    """BingX answered, but not with position rows this check can read."""


@dataclass(frozen=True)
class FlatReport:
    flat: bool
    lines: tuple[str, ...]


class PersistedPositions:
    """The raw `open_position_state` rows of a SupabaseTradeStore, read so that a failure raises.

    `SupabaseTradeStore.list_open_positions()` turns every read error into an empty list. The engine can
    live with that (nothing to resume), a flat check cannot: an unreachable store would read as FLAT. So this
    goes one level down, to the store's own authenticated, schema-aware getter. It also counts a row that is
    `opening` or `closing`, not only `open`: anything but `closed` is exposure.
    """

    def __init__(self, store: SupabaseTradeStore) -> None:
        self._store = store

    async def open_position_rows(self) -> list[dict[str, Any]]:
        store = self._store
        return await store._get_rows(
            store._open_positions_table,
            [
                ("select", "symbol,direction,status,quantity"),
                ("exchange", f"eq.{store._exchange}"),
                ("status", "neq.closed"),
                ("limit", str(_PERSISTED_PAGE)),
            ],
        )


def _unreadable(exc: Exception, what: str) -> str:
    return f"UNREADABLE {type(exc).__name__} ({what})"


def _row_findings(row: Any, symbol: str) -> list[str]:
    """The OPEN line of one position row, none for a zero amount; raises for a row it cannot read."""
    if not isinstance(row, dict):
        raise VenueAnswerError("a position row is not an object")
    amount = Decimal(str(row.get("positionAmt")))  # a missing or garbled amount raises InvalidOperation
    if amount == 0:  # NaN is not equal to zero either
        return []
    size = format(abs(amount).normalize(), "f")
    return [f"OPEN venue {row.get('symbol') or symbol} side={row.get('positionSide') or '?'} size={size}"]


async def _venue_findings(client: Any, symbol: str) -> list[str]:
    """The OPEN lines for one symbol, read strictly from the raw positions answer.

    `client.get_symbol_position_details` is not used: it maps an answer without a list of rows, a row it
    cannot parse and a row naming another symbol to "no position", and a flat check must never do that.
    A row it cannot read is reported on its own line and does not hide the open position of the row beside it.
    """
    answer = await client.get_position_info(symbol)
    rows = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(rows, list):
        raise VenueAnswerError("no list of position rows")
    findings: list[str] = []
    for row in rows:
        try:
            findings.extend(_row_findings(row, symbol))
        except Exception as exc:  # unreadable exposure must never look flat, nor hide the rows that are readable
            findings.append(_unreadable(exc, f"venue position of {symbol}"))
    return findings


async def check_flat(settings: Any, client: Any, store: PositionRows) -> FlatReport:
    """Check the venue position rows of every symbol and the persisted open positions.

    `client` needs `get_position_info(symbol)`, `store` needs `open_position_rows()`; `settings` needs
    `symbols`. Every source is read even when another failed, so one run shows everything.
    """
    findings: list[str] = []
    symbols = tuple(settings.symbols)
    if not symbols:
        findings.append("UNREADABLE NoSymbols (SYMBOLS is empty, so no venue position was checked)")
    for symbol in symbols:
        try:
            findings.extend(await _venue_findings(client, symbol))
        except Exception as exc:  # unreadable exposure must never look flat
            findings.append(_unreadable(exc, f"venue position of {symbol}"))
    try:
        rows = await store.open_position_rows()
    except Exception as exc:
        findings.append(_unreadable(exc, "persisted open positions"))
    else:
        findings.extend(
            f"OPEN persisted {row.get('symbol')} {row.get('direction')} "
            f"status={row.get('status')} quantity={row.get('quantity')}"
            for row in rows
        )
    return FlatReport(flat=not findings, lines=tuple(findings) or ("FLAT",))


async def _run(expected_schema: str | None) -> int:
    try:
        settings = Settings()
    except Exception as exc:  # the text of a settings error carries the offending value
        print(_unreadable(exc, "settings"))
        return 1
    print(
        f"checking {', '.join(settings.symbols)} on BingX {settings.bingx_env}; "
        f"store schema {settings.supabase_schema}",
        file=sys.stderr,
    )
    if expected_schema is not None and settings.supabase_schema != expected_schema:
        print(f"UNREADABLE SchemaMismatch (store schema is not {expected_schema})")
        return 1
    client = store = None
    try:
        store = SupabaseTradeStore(settings, logging.getLogger("flat_check"))
        client = BingXFuturesClient(
            api_key=settings.bingx_api_key,
            secret_key=settings.bingx_secret_key,
            base_url=settings.bingx_base_url,
            env=settings.bingx_env,
            margin_mode=settings.bingx_margin_mode,
            position_mode=settings.bingx_position_mode,
            timeout_seconds=settings.request_timeout_seconds,
            recv_window_ms=settings.bingx_recv_window_ms,
            source_key=settings.bingx_source_key,
            insecure_ssl=settings.bingx_insecure_ssl,
        )
        report = await check_flat(settings, client, PersistedPositions(store))
    except Exception as exc:
        print(_unreadable(exc, "client or store setup"))
        return 1
    finally:
        for resource in (client, store):
            if resource is not None:
                with contextlib.suppress(Exception):  # a failed close must not change the verdict
                    await resource.close()
    print("\n".join(report.lines))
    return 0 if report.flat else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--expect-schema",
        metavar="NAME",
        help="refuse to run unless .env names this Supabase schema (guards against the wrong directory)",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.expect_schema))


if __name__ == "__main__":
    raise SystemExit(main())
