"""Read-only flat check for one bot directory: is this account flat?

Run it from the bot's directory. That directory's `.env`, read through `Settings` like the bot
reads it, decides which BingX account and which Supabase schema are looked at:

    cd /home/ubuntu/ai-perp-long && PYTHONPATH=src .venv/bin/python src/flat_check.py

It asks the venue for the position of every symbol in SYMBOLS and reads the persisted open
positions (`open_position_state`) from the schema that `.env` names. It prints `FLAT` and exits 0
only when both are clear. Otherwise it prints one line per finding and exits 1:

    OPEN venue BTC/USDT side=LONG size=0.01
    OPEN persisted BTC/USDT LONG status=open quantity=0.01
    UNREADABLE ExchangeAPIError (venue position of BTC/USDT)

A source that cannot be read is never flat. Only exception class names are printed, never their
text (a settings error carries the offending value, a store error the response body), and nothing
is logged: no key, URL or setting value reaches an output. The context line on stderr names the
symbols and the schema, so a run from the wrong directory shows at once. It places, cancels and
closes nothing, and it does not look at pending orders: the position read is the authority the
bot itself uses.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from dataclasses import dataclass
from typing import Any, Protocol

from bot.config import Settings
from bot.exchange.bingx_client import BingXFuturesClient
from bot.storage.supabase_trade_store import SupabaseTradeStore

_PERSISTED_PAGE = 50  # the engine reads at most this many; one row already means "not flat"


class PositionRows(Protocol):
    async def open_position_rows(self) -> list[dict[str, Any]]: ...


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


async def check_flat(settings: Any, client: Any, store: PositionRows) -> FlatReport:
    """Check the venue position of every symbol and the persisted open positions.

    `client` needs `get_symbol_position_details(symbol)`, `store` needs `open_position_rows()`; `settings`
    needs `symbols`. Every source is read even when another failed, so one run shows everything.
    """
    findings: list[str] = []
    symbols = tuple(settings.symbols)
    if not symbols:
        findings.append("UNREADABLE NoSymbols (SYMBOLS is empty, so no venue position was checked)")
    for symbol in symbols:
        try:
            details = await client.get_symbol_position_details(symbol)
        except Exception as exc:  # unreadable exposure must never look flat
            findings.append(_unreadable(exc, f"venue position of {symbol}"))
            continue
        if details.exchange_size > 0:
            findings.append(f"OPEN venue {symbol} side={details.pos_side or '?'} size={details.exchange_size}")
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


async def _run() -> int:
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
    parser.parse_args(argv)  # takes no options; a stray argument is a usage error (exit 2)
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
