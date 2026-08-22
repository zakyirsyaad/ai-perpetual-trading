"""Deduplicated historical replay of stored AI entry decisions.

Reads `ai_decisions.payload_json.request` records, removes duplicate stored
batch requests (the same compact request hash), and reports how the compact
payload compares with the stored one. Used before exchange cutover to spot
material behavior changes without spending provider tokens on repeats.

    PYTHONPATH=src python src/replay_ai_decisions.py --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Iterable

from bot.ai.compact_context import estimate_payload_tokens, payload_hash
from bot.config import Settings
from bot.logger import setup_logging
from bot.storage.supabase_trade_store import SupabaseTradeStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay stored AI decisions with duplicate requests removed.")
    parser.add_argument("--limit", type=int, default=500, help="Maximum stored decisions to read.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return parser


def deduplicate_requests(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Collapse stored decision rows to unique requests by compact request hash."""
    unique: dict[str, dict[str, Any]] = {}
    total = 0
    skipped = 0
    for row in rows:
        payload = row.get("payload_json")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                skipped += 1
                continue
        if not isinstance(payload, dict):
            skipped += 1
            continue
        request = payload.get("request")
        if not isinstance(request, dict):
            skipped += 1
            continue
        total += 1
        digest = payload_hash(request)
        entry = unique.setdefault(
            digest,
            {
                "request_hash": digest,
                "occurrences": 0,
                "estimated_tokens": estimate_payload_tokens(request),
                "symbols": sorted(
                    {str(item.get("symbol")) for item in _symbol_blocks(request) if item.get("symbol")}
                ),
            },
        )
        entry["occurrences"] += 1

    duplicate_requests = total - len(unique)
    duplicate_tokens = sum(
        item["estimated_tokens"] * (item["occurrences"] - 1) for item in unique.values()
    )
    return {
        "records_read": total + skipped,
        "records_skipped": skipped,
        "requests": total,
        "unique_requests": len(unique),
        "duplicate_requests": duplicate_requests,
        "estimated_duplicate_tokens": duplicate_tokens,
        "unique_request_details": sorted(
            unique.values(), key=lambda item: -item["occurrences"]
        )[:20],
    }


def _symbol_blocks(request: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = request.get("symbols")
    if isinstance(blocks, list):
        return [item for item in blocks if isinstance(item, dict)]
    # Legacy (pre-compaction) requests stored the full market context.
    context = request.get("market_context")
    if isinstance(context, dict) and isinstance(context.get("technicals"), list):
        return [item for item in context["technicals"] if isinstance(item, dict)]
    return []


def render_report(summary: dict[str, Any]) -> str:
    lines = [
        "=== Deduplicated AI decision replay ===",
        f"records_read={summary['records_read']} skipped={summary['records_skipped']}",
        f"requests={summary['requests']} unique={summary['unique_requests']} "
        f"duplicates={summary['duplicate_requests']}",
        f"estimated_tokens_wasted_on_duplicates={summary['estimated_duplicate_tokens']}",
        "",
        "--- most repeated requests ---",
    ]
    for item in summary["unique_request_details"]:
        lines.append(
            f"{item['request_hash'][:12]}  x{item['occurrences']:<4} "
            f"~{item['estimated_tokens']} tokens  symbols={','.join(item['symbols']) or '-'}"
        )
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    logger = setup_logging(settings.log_level)
    store = SupabaseTradeStore(settings, logger)
    try:
        rows = await store._get_rows(
            settings.supabase_ai_decisions_table,
            [("select", "payload_json,created_at"), ("order", "created_at.desc"), ("limit", str(args.limit))],
        )
    finally:
        await store.close()
    summary = deduplicate_requests(rows)
    print(json.dumps(summary, indent=2, default=str) if args.json else render_report(summary))
    return 0


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
