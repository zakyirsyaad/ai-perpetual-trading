"""Shared pieces of the Pine gate CLI: injectable factories, errors and file output.

Research code; the bot never imports it. Standard library only (plus the bot's
own settings and Supabase store, reached only through ``make_settings`` and
``make_get_rows``).

Every outside dependency is a module-level name that the commands look up at
call time, so tests replace it with ``mock.patch.object``: ``http_get`` (public
BingX market data), ``sleep``, ``now`` (the clock that dates output files),
``make_settings`` (the bot's ``Settings``, which reads ``.env``) and
``make_get_rows`` (read-only Supabase pages).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from . import calibrate
from .bars import SYMBOL_PATTERN, check_symbol
from .calibrate import Calibration
from .fetch import httpx_get
from .fileio import write_exclusive, write_json_exclusive
from .lock import latencies_sha256
from .spec import WIB

http_get = httpx_get
sleep = time.sleep


def now() -> datetime:
    return datetime.now(timezone.utc)


def make_settings() -> Any:  # pragma: no cover - reads the live .env
    from bot.config import Settings

    return Settings()


make_get_rows: Callable[[Any], Any] = calibrate.make_get_rows

# SYMBOL_PATTERN and check_symbol (bars), write_exclusive and write_json_exclusive
# (fileio) reach the CLI modules through this one.
_MAX_MESSAGE_CHARS = 400


class CliError(Exception):
    """An expected failure reported as one line (exit code 1)."""


def one_line(message: Any) -> str:
    text = " ".join(str(message).split())
    if len(text) > _MAX_MESSAGE_CHARS:
        text = text[: _MAX_MESSAGE_CHARS - 3] + "..."
    return text


def positive_int(text: str) -> int:
    """argparse type for counts: 0 or less is a usage error, never a vacuous pass."""
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {text!r}")
    return value


def wib_today() -> date:
    return now().astimezone(WIB).date()


def stamped_path(out_dir: Path, prefix: str, suffix: str = ".json") -> Path:
    """``<out_dir>/<prefix><YYYYMMDD><suffix>`` dated by today's WIB day."""
    return Path(out_dir) / f"{prefix}{wib_today():%Y%m%d}{suffix}"


def read_json(path: Path) -> Any:
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{path}: not valid JSON ({exc})") from exc


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def newest(out_dir: Path, pattern: str) -> Optional[Path]:
    """The file matching ``pattern`` with the greatest name (names carry the date)."""
    found = sorted(Path(out_dir).glob(pattern))
    return found[-1] if found else None


def _finite_number(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def load_calibration_file(path: Path) -> Calibration:
    """The ``calibrate`` output as a ``Calibration``; the latency hash must match."""
    doc = read_json(path)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: calibration file is not a JSON object")
    s_star, n_stops = doc.get("s_star_bps"), doc.get("n_stops")
    latencies = doc.get("latencies_ms")
    if not _finite_number(s_star):
        raise ValueError(f"{path}: s_star_bps is missing or not a finite number")
    if isinstance(n_stops, bool) or not isinstance(n_stops, int) or n_stops < 0:
        raise ValueError(f"{path}: n_stops is missing or not a non-negative integer")
    if not isinstance(latencies, list) or not all(_finite_number(x) for x in latencies):
        raise ValueError(f"{path}: latencies_ms is missing or not a list of finite numbers")
    values = tuple(float(x) for x in latencies)
    if doc.get("latencies_sha256") != latencies_sha256(values):
        raise ValueError(f"{path}: latencies_sha256 does not match latencies_ms")
    return Calibration(s_star_bps=float(s_star), n_stops=n_stops, latencies_ms=values)


def call_live(what: str, action: Callable[[], Any]) -> Any:
    """Run a step that touches the bot's settings or Supabase.

    Any failure becomes a ``CliError`` naming only the exception type: settings
    and HTTP errors can carry environment values (the Supabase URL, a field's
    input) that must never be printed."""
    try:
        return action()
    except Exception as exc:  # noqa: BLE001 - the message may hold secrets
        raise CliError(f"{what} failed ({type(exc).__name__}; details withheld)") from None


def as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def json_doc(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    """A JSON object file, or ``None`` when ``path`` is ``None``."""
    if path is None:
        return None
    doc = read_json(path)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return doc
