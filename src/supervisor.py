"""Watchdog for the entry-block latch.

`TradeEngine` latches entries when an order's outcome is uncertain and refuses to
trade until a human reconciles. That behaviour is correct -- retrying an order
POST can duplicate a position -- but the latch is silent. On 2026-09-07 it left
the bot idle for 1h21m (28 ticks) before anyone noticed.

This supervisor performs the same read-only reconciliation a human would: read
the exchange position size, look the order up by its client id, and clear the
latch (by restarting) only when both independently say nothing happened. Every
other case is left alone for a person.

It never places, cancels, or closes an order.

Run from cron, e.g. every five minutes:

    */5 * * * * cd /home/ubuntu/ai-perpetual-trading && PYTHONPATH=src \\
        .venv/bin/python src/supervisor.py >> logs/supervisor.log 2>&1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

UNCERTAIN_RE = re.compile(r"Entries blocked: Entry order (\S+) for (\S+) could not be confirmed")
UNMATCHED_RE = re.compile(r"Unmatched exchange exposure:")

DEFAULT_STATE_PATH = Path("logs/supervisor_state.json")
DEFAULT_LOG_TAIL_LINES = 400


@dataclass(frozen=True, slots=True)
class BlockedEntry:
    kind: str  # "uncertain_order" | "unmatched_exposure"
    order_id: str
    symbol: str


@dataclass(frozen=True, slots=True)
class Decision:
    action: str  # "restart" | "hold" | "none"
    reason: str


def parse_block(log_text: str) -> BlockedEntry | None:
    """Return the most recent entry-block latch in the log, if any.

    The latest line wins: a stale uncertain-order halt must never mask a later
    unmatched-exposure halt, which is the more serious of the two.
    """
    found: BlockedEntry | None = None
    for line in log_text.splitlines():
        match = UNCERTAIN_RE.search(line)
        if match:
            found = BlockedEntry(kind="uncertain_order", order_id=match.group(1), symbol=match.group(2))
            continue
        if UNMATCHED_RE.search(line):
            found = BlockedEntry(kind="unmatched_exposure", order_id="", symbol="")
    return found


def decide(
    block: BlockedEntry | None,
    *,
    position_size: float | None,
    lookup_status: str,
    last_recovered_order_id: str = "",
) -> Decision:
    """Decide whether the latch may be cleared. Conservative by construction."""
    if block is None:
        return Decision("none", "entries are not blocked")
    if block.kind != "uncertain_order":
        return Decision(
            "hold",
            "unmatched exchange exposure is never cleared automatically: it means a real "
            "position the bot could not model",
        )
    if block.order_id and block.order_id == last_recovered_order_id:
        return Decision(
            "hold",
            f"order {block.order_id} was already recovered once; a repeat means the latch "
            "is not the underlying problem",
        )
    if position_size is None:
        return Decision("hold", "the exchange position could not be read, so exposure is unknown")
    if position_size > 0:
        return Decision("hold", f"exchange still reports {position_size} exposure on {block.symbol}")
    if lookup_status != "absent":
        return Decision(
            "hold",
            f"order lookup returned {lookup_status!r}; only a confirmed absence is safe to clear",
        )
    return Decision("restart", "no exchange exposure and the order is confirmed absent")


# ---------------------------------------------------------------------------
# side effects


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _say(message: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} | supervisor | {message}", flush=True)


async def _reconcile(symbol: str, client_order_id: str) -> tuple[float | None, str]:
    """Read-only: exchange position size and the order's lookup status."""
    from bot.config import Settings
    from bot.exchange.bingx_client import BingXFuturesClient

    settings = Settings()
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
    )
    try:
        try:
            size, _ = await client.get_symbol_position_snapshot(symbol)
        except Exception as exc:  # unreadable exposure must not look like "flat"
            _say(f"position read failed for {symbol}: {type(exc).__name__}: {exc}")
            size = None
        status = "unknown"
        if client_order_id:
            try:
                status, _ = await client._lookup_order_by_client_id(symbol, client_order_id)
            except Exception as exc:
                _say(f"order lookup failed for {client_order_id}: {type(exc).__name__}: {exc}")
        return size, str(status)
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default=str(Path.home() / ".pm2/logs/ai-perp-volume-error.log"))
    parser.add_argument("--app", default="ai-perp-volume")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--lines", type=int, default=DEFAULT_LOG_TAIL_LINES)
    parser.add_argument("--dry-run", action="store_true", help="decide and report, never restart")
    args = parser.parse_args()

    try:
        tail = Path(args.log).read_text(encoding="utf-8", errors="replace").splitlines()[-args.lines :]
    except OSError as exc:
        _say(f"cannot read {args.log}: {exc}")
        return 1

    block = parse_block("\n".join(tail))
    if block is None:
        return 0

    state = _load_state(Path(args.state))
    size, status = (None, "unknown")
    if block.kind == "uncertain_order":
        size, status = asyncio.run(_reconcile(block.symbol, block.order_id))

    decision = decide(
        block,
        position_size=size,
        lookup_status=status,
        last_recovered_order_id=str(state.get("last_recovered_order_id") or ""),
    )
    _say(
        f"block={block.kind} order={block.order_id or '-'} symbol={block.symbol or '-'} "
        f"position={size} lookup={status} -> {decision.action}: {decision.reason}"
    )
    if decision.action != "restart":
        return 0
    if args.dry_run:
        _say("dry run: not restarting")
        return 0

    result = subprocess.run(
        ["pm2", "restart", args.app, "--update-env"], capture_output=True, text=True
    )
    if result.returncode != 0:
        _say(f"pm2 restart failed rc={result.returncode}: {result.stderr.strip()[:300]}")
        return 1
    state["last_recovered_order_id"] = block.order_id
    state["last_recovered_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save_state(Path(args.state), state)
    _say(f"restarted {args.app}; latch cleared for order {block.order_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
