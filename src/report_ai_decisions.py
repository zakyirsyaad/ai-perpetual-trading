from __future__ import annotations

import argparse
import json
import os
from typing import Any

import httpx


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize Supabase trades grouped by AI/rule entry reason.")
    parser.add_argument("--supabase-url", default=os.getenv("SUPABASE_URL"), help="Supabase project URL.")
    parser.add_argument(
        "--service-role-key",
        default=os.getenv("SUPABASE_SERVICE_ROLE_KEY"),
        help="Supabase service role key. Prefer environment variable for shell history safety.",
    )
    parser.add_argument("--schema", default=os.getenv("SUPABASE_SCHEMA", "public"))
    parser.add_argument("--trades-table", default=os.getenv("SUPABASE_TRADES_TABLE", "trades"))
    parser.add_argument("--limit", type=int, default=1000, help="Maximum trades to fetch before grouping.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    rows = _fetch_trades(
        supabase_url=args.supabase_url,
        service_role_key=args.service_role_key,
        schema=args.schema,
        table=args.trades_table,
        limit=args.limit,
    )
    report = _summarize(rows)
    print(json.dumps(report, indent=2, sort_keys=True))


def _fetch_trades(
    *,
    supabase_url: str | None,
    service_role_key: str | None,
    schema: str,
    table: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not supabase_url or not service_role_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for reports.")
    response = httpx.get(
        f"{supabase_url.rstrip('/')}/rest/v1/{table}",
        params=[
            ("select", "entry_reason,pnl_usdt,closed_at"),
            ("order", "closed_at.desc"),
            ("limit", str(limit)),
        ],
        headers={
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
            "Accept-Profile": schema,
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Supabase response: {type(data).__name__}")
    return [row for row in data if isinstance(row, dict)]


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    total_pnl = 0.0
    for row in rows:
        reason = str(row.get("entry_reason") or "unknown")
        pnl = float(row.get("pnl_usdt") or 0.0)
        total_pnl += pnl
        item = grouped.setdefault(
            reason,
            {"entry_reason": reason, "trades": 0, "wins": 0, "losses": 0, "breakeven": 0, "total_pnl": 0.0},
        )
        item["trades"] += 1
        item["total_pnl"] += pnl
        if pnl > 0:
            item["wins"] += 1
        elif pnl < 0:
            item["losses"] += 1
        else:
            item["breakeven"] += 1
    reasons = []
    for item in grouped.values():
        trades = int(item["trades"])
        wins = int(item["wins"])
        item["win_rate_pct"] = (wins / trades * 100.0) if trades else 0.0
        item["avg_pnl"] = float(item["total_pnl"]) / trades if trades else 0.0
        reasons.append(item)
    return {
        "total_trades": len(rows),
        "total_pnl": total_pnl,
        "avg_pnl": total_pnl / len(rows) if rows else 0.0,
        "by_entry_reason": sorted(reasons, key=lambda item: item["trades"], reverse=True),
    }


if __name__ == "__main__":
    main()
