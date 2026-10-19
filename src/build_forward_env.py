"""Derive the LONG forward bot's `.env` from the live bot's `.env`.

The operator runs this on the VPS. It copies the source file line by line, forces the
forward bot's settings (its own Supabase schema first of all), and blanks the three
secrets that belong to the second account: the account owner types those by hand.

No value read from the source is ever printed, logged or put in an error or in the
report: only key names travel. The target is created exclusively with mode 0600.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from clean_env import _key_from_line

FORWARD_OVERRIDES: dict[str, str] = {
    "SUPABASE_SCHEMA": "long_forward",
    "ENTRY_GATE_DIRECTION": "long",
    # The direction filter runs only inside the enforcing stretch gate of an AI strategy; Settings
    # refuses ENTRY_REGIME_GATE=naik without these, so a live .env cannot leave them inherited.
    "ENTRY_STRETCH_GATE_MODE": "enforce",
    "STRATEGY_MODE": "ai_volume",
    "ENTRY_REGIME_GATE": "naik",
    "ENTRY_VOLATILITY_GUARD_MULT": "2.0",
    "COOLDOWN_MINUTES_AFTER_EXIT": "15",
    "COOLDOWN_MINUTES_AFTER_ENTRY": "0",
    "COOLDOWN_MINUTES_AFTER_STOP": "0",
    "TRADE_TARGET_PER_DAY": "6",
    "VOLUME_ATTEMPT_TARGET_PER_DAY": "6",
    "VOLUME_STOP_AT_TARGET_ENABLED": "false",
    "VOLUME_DAILY_FEE_BUDGET_USDT": "0",
    "ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR": "0",
    # python-dotenv keeps the space of an unquoted value; a test pins that Settings parses it.
    "ENTRY_WEEKLY_WINDOW": "mon 07:00-sat 00:00",
    "FORWARD_KILL_AFTER_POSITIONS": "60",
    "FORWARD_KILL_BELOW_PER_1M": "-500",
    "FORWARD_REVIEW_AT_POSITIONS": "150",
    "TELEMETRY_ENABLED": "false",
    # Spec 9.1: the forward test keeps trading through losses until its own kill rule fires.
    "MAX_STOP_LOSS_PER_DAY": "1000",
    "MAX_DAILY_DRAWDOWN_USDT": "1000000000",
    "MAX_CONSECUTIVE_LOSSES": "1000",
}
CYCLE_KEY = "VOLUME_CYCLE_STARTED_AT"
BLANKED_KEYS: tuple[str, ...] = ("BINGX_API_KEY", "BINGX_SECRET_KEY", "XAI_API_KEY")
REQUIRED_SOURCE_KEY = "SUPABASE_URL"

APPENDED_HEADER = "# --- appended by build_forward_env.py (LONG forward bot) ---"
FUTURE_TOLERANCE = timedelta(minutes=5)  # the same bound Settings applies to the cycle start

_LINE_PATTERN = re.compile(r"[^\n]*\n|[^\n]+")
_EXPORT_PATTERN = re.compile(r"^export\s+")


class ForwardEnvError(Exception):
    """An expected failure. The message never contains a value read from the source."""


@dataclass(frozen=True)
class BuildReport:
    set_keys: tuple[str, ...]
    blanked_keys: tuple[str, ...]


def _key_of(line: str) -> str | None:
    """Canonical (upper-case) key of an env line, or None.

    Handles a leading `export `, and quotes around the key (python-dotenv reads `'KEY'=v` as KEY).
    Case is folded because Settings matches names case-insensitively, so `supabase_schema=public`
    is the same setting as `SUPABASE_SCHEMA` and, coming later, would win.
    """
    key = _key_from_line(_EXPORT_PATTERN.sub("", line.strip(), count=1))
    if key is None:
        return None
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "'\"":
        key = key[1:-1].strip()
    return key.upper() or None


def _has_value(line: str) -> bool:
    body = _EXPORT_PATTERN.sub("", line.strip(), count=1)
    return bool(body.split("=", 1)[1].strip().strip("'\""))


def _validate_cycle_start(cycle_start: str) -> None:
    text = cycle_start[:-1] + "+00:00" if cycle_start.endswith(("Z", "z")) else cycle_start
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ForwardEnvError("--cycle-start must be an ISO-8601 timestamp with a timezone") from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ForwardEnvError("--cycle-start must carry a timezone (for example +07:00 or Z)")
    if moment.astimezone(timezone.utc) > datetime.now(timezone.utc) + FUTURE_TOLERANCE:
        raise ForwardEnvError("--cycle-start is more than 5 minutes in the future")


def _read_source(source: Path) -> str:
    try:
        return source.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ForwardEnvError("source is not valid UTF-8") from None
    except OSError as exc:
        raise ForwardEnvError(f"cannot read source: {exc.strerror or 'I/O error'}") from None


def _check_paths(source: Path, target: Path) -> None:
    if source.resolve() == target.resolve():
        raise ForwardEnvError("target is the same file as the source")
    if os.path.lexists(target):
        raise ForwardEnvError("target already exists; refusing to overwrite it")


def _render(source_text: str, forced: dict[str, str]) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for line in _LINE_PATTERN.findall(source_text):
        key = _key_of(line)
        if key is None or key not in forced:
            out.append(line)
            continue
        if key in seen:  # a later duplicate would win in dotenv, so it must not survive
            continue
        seen.add(key)
        out.append(f"{key}={forced[key]}" + ("\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""))
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    missing = [key for key in forced if key not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("\n")
        out.append(APPENDED_HEADER + "\n")
        out.extend(f"{key}={forced[key]}\n" for key in missing)
    return "".join(out)


def _write_exclusive(target: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError:
        raise ForwardEnvError("target already exists; refusing to overwrite it") from None
    except OSError as exc:
        raise ForwardEnvError(f"cannot create target: {exc.strerror or 'I/O error'}") from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise ForwardEnvError(f"cannot write target: {exc.strerror or 'I/O error'}") from None


def build_forward_env(source: Path, target: Path, cycle_start: str) -> BuildReport:
    source, target = Path(source), Path(target)
    _validate_cycle_start(cycle_start)
    _check_paths(source, target)
    source_text = _read_source(source)
    if not any(_key_of(line) == REQUIRED_SOURCE_KEY and _has_value(line) for line in _LINE_PATTERN.findall(source_text)):
        raise ForwardEnvError(f"source has no {REQUIRED_SOURCE_KEY}; is it the live bot's env file?")
    forced = {**FORWARD_OVERRIDES, CYCLE_KEY: cycle_start, **{key: "" for key in BLANKED_KEYS}}
    _write_exclusive(target, _render(source_text, forced))
    return BuildReport(set_keys=(*FORWARD_OVERRIDES, CYCLE_KEY), blanked_keys=BLANKED_KEYS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive the LONG forward bot's .env from the live .env without printing any value."
    )
    parser.add_argument("--source", required=True, type=Path, help="The live bot's .env (read only).")
    parser.add_argument("--target", required=True, type=Path, help="The new file to create (must not exist).")
    parser.add_argument("--cycle-start", required=True, help="Timezone-aware ISO-8601 start of the forward test.")
    args = parser.parse_args(argv)
    try:
        report = build_forward_env(args.source, args.target, args.cycle_start)
    except ForwardEnvError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"built {args.target} (mode 600)")
    print("set: " + ", ".join(report.set_keys))
    print("blanked, type these by hand: " + ", ".join(report.blanked_keys))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
