"""AI usage report: calls and tokens by purpose, retries, errors, dedupe savings.

Observability only. This command never influences trading — reaching any daily
token or cost amount is reported, never enforced.

    PYTHONPATH=src python src/report_ai_usage.py --days 1
    PYTHONPATH=src python src/report_ai_usage.py --days 7 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from bot.ai.usage import summarize_metrics
from bot.config import Settings
from bot.logger import setup_logging
from bot.storage.supabase_trade_store import SupabaseTradeStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize AI provider usage recorded in ai_call_metrics.")
    parser.add_argument("--days", type=int, default=1, help="Look back this many days (default 1).")
    parser.add_argument("--limit", type=int, default=5000, help="Maximum records to read (default 5000).")
    parser.add_argument("--json", action="store_true", help="Emit the raw JSON summary instead of a table.")
    return parser


def group_by_day(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        created = str(row.get("created_at") or "")
        by_day.setdefault(created[:10] or "unknown", []).append(row)
    return by_day


def render_report(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No AI call metrics found for the selected window."
    lines: list[str] = []
    overall = summarize_metrics(rows)
    totals = overall["totals"]
    lines.append("=== AI usage summary ===")
    lines.append(
        f"calls={totals['calls']} success={totals['successes']} failures={totals['failures']} "
        f"success_rate={totals['success_rate_pct']:.1f}%"
    )
    lines.append(
        f"tokens: input={totals['input_tokens']} output={totals['output_tokens']} "
        f"reasoning={totals['reasoning_tokens']} cached={totals['cached_tokens']} total={totals['total_tokens']}"
    )
    lines.append(
        f"retried_calls={totals['retried_calls']} repaired_calls={totals['repaired_calls']} "
        f"deduplicated_calls={totals['deduplicated_calls']} estimated_token_calls={totals['estimated_token_calls']}"
    )
    if totals["estimated_cost_usd"]:
        lines.append(f"estimated_cost_usd={totals['estimated_cost_usd']:.6f}")

    lines.append("")
    lines.append("--- by purpose ---")
    for purpose, bucket in sorted(overall["by_purpose"].items()):
        lines.append(
            f"{purpose:<14} calls={bucket['calls']:<5} ok={bucket['successes']:<5} "
            f"fail={bucket['failures']:<5} tokens={bucket['total_tokens']}"
        )

    if overall["errors"]:
        lines.append("")
        lines.append("--- errors by category ---")
        for category, count in sorted(overall["errors"].items(), key=lambda item: -item[1]):
            lines.append(f"{category:<20} {count}")

    lines.append("")
    lines.append("--- by day ---")
    for day, day_rows in sorted(group_by_day(rows).items()):
        day_totals = summarize_metrics(day_rows)["totals"]
        lines.append(
            f"{day}  calls={day_totals['calls']:<5} total_tokens={day_totals['total_tokens']:<9} "
            f"failures={day_totals['failures']:<4} deduped={day_totals['deduplicated_calls']}"
        )
    lines.append("")
    lines.append("Reporting is observability-only; it never pauses or throttles trading.")
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    logger = setup_logging(settings.log_level)
    store = SupabaseTradeStore(settings, logger)
    since = (datetime.now(timezone.utc) - timedelta(days=max(args.days, 0))).isoformat()
    try:
        rows = await store.recent_ai_call_metrics(limit=args.limit, since=since)
    finally:
        await store.close()
    if args.json:
        print(json.dumps({"summary": summarize_metrics(rows), "record_count": len(rows)}, indent=2, default=str))
    else:
        print(render_report(rows))
    return 0


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
