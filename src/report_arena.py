from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from bot.arena.agents import AttributionAI, HyperAI, ProgramAI, PromptAI, SignalAI
from bot.arena.attribution import AttributionAnalyzer, DiagnosisGenerator
from bot.arena.factors import FactorEffectivenessAnalyzer
from bot.arena.program_trader import ProgramStrategyRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backend-only arena reports and dry-runs.")
    parser.add_argument("--supabase-url", default=os.getenv("SUPABASE_URL"), help="Supabase project URL.")
    parser.add_argument(
        "--service-role-key",
        default=os.getenv("SUPABASE_SERVICE_ROLE_KEY"),
        help="Supabase service role key. Prefer environment variable for shell history safety.",
    )
    parser.add_argument("--schema", default=os.getenv("SUPABASE_SCHEMA", "public"))
    parser.add_argument("--trades-table", default=os.getenv("SUPABASE_TRADES_TABLE", "trades"))
    subcommands = parser.add_subparsers(dest="command", required=True)

    attribution = subcommands.add_parser("attribution", help="Group performance by arena dimensions.")
    attribution.add_argument("--limit", type=int, default=100)
    attribution.add_argument("--diagnose", action="store_true")

    factors = subcommands.add_parser("factors", help="Report factor effectiveness from closed trades.")
    factors.add_argument("--limit", type=int, default=100)

    health = subcommands.add_parser("health", help="Print arena runtime health from Supabase trades.")
    health.add_argument("--limit", type=int, default=1000)
    health.add_argument("--target-volume", type=float, default=0.0)
    health.add_argument("--taker-fee-pct", type=float, default=0.05)

    program = subcommands.add_parser("program-dry-run", help="Validate and run a local Program Trader file.")
    program.add_argument("strategy_file", help="Python file defining build_signal(context).")
    program.add_argument("--context-json", default='{"symbol":"BTC/USDT"}')
    program.add_argument("--timeout-seconds", type=float, default=1.0)

    config = subcommands.add_parser("config-assist", help="Generate backend-only arena config from a goal.")
    config.add_argument("goal", help="Operator goal, for example daily volume and breakeven constraints.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "program-dry-run":
        _run_program(args)
        return
    if args.command == "config-assist":
        _run_config_assist(args)
        return

    records = _fetch_supabase_trades(
        supabase_url=args.supabase_url,
        service_role_key=args.service_role_key,
        schema=args.schema,
        table=args.trades_table,
        limit=getattr(args, "limit", 100),
    )
    if args.command == "attribution":
        _print_attribution(records, diagnose=bool(args.diagnose))
    elif args.command == "factors":
        _print_factors(records)
    elif args.command == "health":
        _print_health(records, target_volume=float(args.target_volume), taker_fee_pct=float(args.taker_fee_pct))


def _fetch_supabase_trades(
    *,
    supabase_url: str | None,
    service_role_key: str | None,
    schema: str,
    table: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not supabase_url or not service_role_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for arena reports.")
    response = httpx.get(
        f"{supabase_url.rstrip('/')}/rest/v1/{table}",
        params=[
            (
                "select",
                "symbol,direction,entry_price,exit_price,quantity,pnl_usdt,reason,entry_reason,"
                "opened_at,closed_at,exchange_entry_order_id,exchange_exit_order_id,confidence,"
                "factor_metadata,trigger_metadata",
            ),
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


def _print_attribution(records: list[dict[str, Any]], *, diagnose: bool) -> None:
    summary = AttributionAnalyzer().summarize(records)
    print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
    if diagnose:
        cards = [card.to_dict() for card in DiagnosisGenerator().generate(summary)]
        print(json.dumps({"diagnosis_cards": cards}, indent=2, sort_keys=True))


def _print_factors(records: list[dict[str, Any]]) -> None:
    summary = FactorEffectivenessAnalyzer().summarize(records)
    payload = {name: item.to_dict() for name, item in summary.items()}
    print(json.dumps(payload, indent=2, sort_keys=True))


def _print_health(records: list[dict[str, Any]], *, target_volume: float, taker_fee_pct: float) -> None:
    analyzer = AttributionAnalyzer()
    daily_volume = analyzer.volume_progress(
        records,
        target_volume_usdt=target_volume,
        taker_fee_pct=taker_fee_pct,
        target_daily_volume_usdt=target_volume if target_volume > 0 else 0.0,
    )
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "records_loaded": len(records),
                "volume_progress": daily_volume.to_dict(),
            },
            indent=2,
            sort_keys=True,
        )
    )


def _run_program(args: argparse.Namespace) -> None:
    strategy_path = Path(args.strategy_file)
    code = strategy_path.read_text(encoding="utf-8")
    context = json.loads(args.context_json)
    result = ProgramStrategyRunner(timeout_seconds=args.timeout_seconds).run(code, context)
    payload: dict[str, Any] = {
        "success": result.success,
        "error": result.error,
        "execution_time_ms": result.execution_time_ms,
        "signal": result.signal.model_dump() if result.signal is not None else None,
        "trade_plan": asdict(result.trade_plan) if result.trade_plan is not None else None,
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _run_config_assist(args: argparse.Namespace) -> None:
    goal = str(args.goal)
    payload = {
        "route": HyperAI().route(goal).to_dict(),
        "signal_conditions": SignalAI().design_conditions(goal).to_dict(),
        "prompt": PromptAI().build_prompt(goal),
        "program_strategy_template": ProgramAI().strategy_template(goal),
        "diagnosis_checklist": AttributionAI().diagnosis_checklist(goal),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
