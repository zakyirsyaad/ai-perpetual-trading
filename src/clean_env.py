from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_ENV_OVERRIDES: dict[str, str] = {
    "STRATEGY_MODE": "ai_volume",
    "XAI_BASE_URL": "https://api.freemodel.dev/v1",
    "XAI_MODEL": "gpt-5.4",
    "XAI_API_STYLE": "chat",
    "AI_KLINE_INTERVAL": "3m",
    "POSITION_HOLD_MINUTES": "60",
    "VOLUME_EXECUTION_MODE": "market",
    "VOLUME_BREAKEVEN_EXIT_ENABLED": "false",
    "POSITION_USDT": "180",
    "VOLUME_MARGIN_PER_ENTRY_USDT": "180",
    "LEVERAGE": "100",
    "OKX_HARD_MAX_LEVERAGE": "100",
    "OKX_VENUE_MAX_LEVERAGE": "100",
    "TRADE_TARGET_PER_DAY": "28",
    "VOLUME_ATTEMPT_TARGET_PER_DAY": "34",
    "VOLUME_MIN_FILLED_ENTRIES_PER_DAY": "28",
    "VOLUME_DAILY_TARGET_USDT": "1000000",
    "VOLUME_TARGET_USDT": "30000000",
    "EXECUTION_ENTRY_ORDER_TYPE": "post_only",
    "EXECUTION_POST_ONLY_TIMEOUT_SECONDS": "3",
    "EXECUTION_POST_ONLY_MAX_REPRICES": "5",
    "EXECUTION_POST_ONLY_FALLBACK_TO_MARKET": "false",
    "MAKER_ENTRY_TTL_SECONDS": "3",
    "MAKER_EXIT_TTL_SECONDS": "3",
    "MAKER_MAX_REPRICES": "5",
    "MAKER_ALLOW_TAKER_EMERGENCY_EXIT": "true",
    "CONFLUENCE_ENABLED": "true",
    "CONFLUENCE_MIN_AGREEMENT": "0.55",
    "CONFLUENCE_MIN_ATR_PCT": "0.05",
    "CONFLUENCE_MIN_SCORE": "0.05",
    "CONFLUENCE_ATR_TP_MULT": "3.0",
    "CONFLUENCE_ATR_SL_MULT": "1.0",
}

PRESERVE_EXACT_KEYS = {
    "OKX_API_KEY",
    "OKX_SECRET_KEY",
    "OKX_PASSPHRASE",
    "XAI_API_KEY",
    "COINMARKETCAP_API_KEY",
    "DUNE_API_KEY",
    "TRADINGVIEW_SIGNAL_TOKEN",
    "TRADINGVIEW_WEBHOOK_URL",
}


@dataclass(frozen=True, slots=True)
class EnvCleanResult:
    env_path: Path
    backup_path: Path
    approved_key_count: int
    preserved_key_count: int
    added_key_count: int
    removed_key_count: int


def clean_env_file(
    *,
    env_path: Path = Path(".env"),
    example_path: Path = Path(".env.example"),
) -> EnvCleanResult:
    example_lines = example_path.read_text(encoding="utf-8").splitlines()
    current_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    current_values = _parse_env_values(current_text)
    approved_keys = _ordered_keys(example_lines)
    approved_set = set(approved_keys)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = env_path.with_name(f"{env_path.name}.backup.{timestamp}")
    if env_path.exists():
        shutil.copy2(env_path, backup_path)
    else:
        backup_path.write_text("", encoding="utf-8")

    output_lines: list[str] = []
    preserved = 0
    added = 0
    for line in example_lines:
        key = _key_from_line(line)
        if key is None:
            output_lines.append(line)
            continue
        default_value = line.split("=", 1)[1]
        if _should_preserve(key) and key in current_values:
            value = current_values[key]
            preserved += 1
        elif key in DEFAULT_ENV_OVERRIDES:
            value = DEFAULT_ENV_OVERRIDES[key]
        else:
            value = default_value
        if key not in current_values:
            added += 1
        output_lines.append(f"{key}={value}")

    removed = len([key for key in current_values if key not in approved_set])
    env_path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")
    return EnvCleanResult(
        env_path=env_path,
        backup_path=backup_path,
        approved_key_count=len(approved_keys),
        preserved_key_count=preserved,
        added_key_count=added,
        removed_key_count=removed,
    )


def _parse_env_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value
    return values


def _ordered_keys(lines: list[str]) -> list[str]:
    return [key for line in lines if (key := _key_from_line(line)) is not None]


def _key_from_line(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key = stripped.split("=", 1)[0].strip()
    return key or None


def _should_preserve(key: str) -> bool:
    return key in PRESERVE_EXACT_KEYS or key.startswith("SUPABASE_")


def _main() -> int:
    parser = argparse.ArgumentParser(description="Rewrite .env from .env.example without printing secret values.")
    parser.add_argument("--env", default=".env", help="Path to the local .env file.")
    parser.add_argument("--example", default=".env.example", help="Path to the public .env.example file.")
    args = parser.parse_args()
    result = clean_env_file(env_path=Path(args.env), example_path=Path(args.example))
    print(
        "cleaned_env "
        f"backup={result.backup_path.name} "
        f"approved_keys={result.approved_key_count} "
        f"preserved_keys={result.preserved_key_count} "
        f"added_keys={result.added_key_count} "
        f"removed_keys={result.removed_key_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
