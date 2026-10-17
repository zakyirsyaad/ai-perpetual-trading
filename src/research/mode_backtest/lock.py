"""The lock of the mode backtest: what a training result depends on, bound before the test runs.

Research code; the bot never imports it. ``train`` writes the lock ``mode_<mode>_<pair>_lock.json``
next to its results. ``test`` refuses (``LockError``, exit code 2) unless the lock still describes
the files it would run on, and then, before it reads any day of the test period, claims two
records: the start record ``mode_<mode>_<pair>_test_started.json`` next to the lock and the run
marker ``<data_dir>/holdout_runs/mode_<mode>_<pair>_<lock sha256>.json``. Either one, once it
exists, refuses another test and another lock: the marker for that data folder, the start record
for that lock folder whatever data folder a rerun names, so a fresh copy of the data cannot buy a
second test. All three are written through ``research.pine_gate.fileio``, so none is ever
overwritten.

``check_lock`` holds four bindings against the current run, and its ``LockError`` lists every one
that differs:

* ``config_sha256``: the sha256 of the configuration bytes the numbers were parsed from;
* ``code_sha256``: ``code_sha256()``, over the files of ``code_files()``;
* ``train_manifest_sha256``: the sha256 of the training manifest (``data.manifest``), the day files
  of the training stage and both warm-ups;
* ``python``: the major.minor version that trained, because float sums can differ between versions.

The lock also records the mode, the pair, the chosen trigger timeframe, the stage bounds and the
training summary; the test stage checks those itself.

``code_files()`` covers every module of the package (``research/mode_backtest/*.py``), every module
of the rule core (``bot/strategy/modes/*.py``) and ``CODE_EXTRA``: every other module of ``bot`` and
``research`` that Python loads when the package is imported. That is each module an import statement
names, followed from module to module, and each package around one, whose ``__init__`` runs first
(``bot.arena``'s brings in the arena modules). ``tests/test_mode_backtest_structure.py`` fails when
the package imports a module this list misses, or when the list names one it does not import. Two
imports are left out: they sit inside functions the package never calls and only read the live
settings and Supabase (``bot.config`` in ``research.pine_gate.cli_common`` and
``bot.storage.supabase_trade_store`` in ``research.pine_gate.calibrate``).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple, Union

from research.pine_gate.bars import check_symbol
from research.pine_gate.fileio import write_json_exclusive

__all__ = [
    "CODE_EXTRA",
    "MARKER_DIR",
    "LockError",
    "check_lock",
    "claim_marker",
    "claim_started",
    "code_files",
    "code_sha256",
    "existing_markers",
    "lock_path",
    "marker_path",
    "python_version",
    "read_lock",
    "run_name",
    "started_path",
]

PathLike = Union[str, Path]

MARKER_DIR = "holdout_runs"  # under the data folder, as the Pine gate keeps its markers
SRC_ROOT = Path(__file__).resolve().parents[2]
CODE_DIRS = ("research/mode_backtest", "bot/strategy/modes")  # every module, imported or not
CODE_EXTRA: Tuple[str, ...] = (
    "bot/__init__.py",
    "bot/arena/__init__.py",
    "bot/arena/agents.py",
    "bot/arena/attribution.py",
    "bot/arena/expression_engine.py",
    "bot/arena/factors.py",
    "bot/arena/flow_signals.py",
    "bot/arena/program_trader.py",
    "bot/arena/setup_profile.py",
    "bot/arena/signals.py",
    "bot/core/__init__.py",
    "bot/core/entry_window.py",
    "bot/data/__init__.py",
    "bot/data/market_signals.py",
    "bot/models.py",
    "bot/strategy/__init__.py",
    "bot/strategy/indicators.py",
    "bot/strategy/pine_indicators.py",
    "bot/volume/__init__.py",
    "bot/volume/profile.py",
    "research/__init__.py",
    "research/pine_gate/__init__.py",
    "research/pine_gate/bars.py",
    "research/pine_gate/calibrate.py",
    "research/pine_gate/checks.py",
    "research/pine_gate/cli_common.py",
    "research/pine_gate/dataset.py",
    "research/pine_gate/features.py",
    "research/pine_gate/fetch.py",
    "research/pine_gate/fileio.py",
    "research/pine_gate/holdout_gate.py",
    "research/pine_gate/holdout_replay.py",
    "research/pine_gate/lock.py",
    "research/pine_gate/metalabel.py",
    "research/pine_gate/outcomes.py",
    "research/pine_gate/pipeline.py",
    "research/pine_gate/report_format.py",
    "research/pine_gate/rules.py",
    "research/pine_gate/selection.py",
    "research/pine_gate/simulate.py",
    "research/pine_gate/spec.py",
    "research/pine_gate/stats.py",
    "research/pine_gate/timeparse.py",
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_MODE = re.compile(r"[A-Z][0-9]?")  # A, B, C, D, D2


class LockError(RuntimeError):
    """The lock is missing or broken, does not describe the current run, or the test already ran.

    ``problems`` holds one line per mismatch when ``check_lock`` found several."""

    def __init__(self, message: str, problems: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.problems: Tuple[str, ...] = tuple(problems)


def code_files() -> List[Tuple[str, str]]:
    """``(path relative to src, sha256)`` of every file the code fingerprint covers, sorted."""
    paths = {path for folder in CODE_DIRS for path in (SRC_ROOT / folder).glob("*.py")}
    paths |= {SRC_ROOT / rel for rel in CODE_EXTRA}
    return sorted(
        (path.relative_to(SRC_ROOT).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in paths
    )


def code_sha256() -> str:
    """sha256 over the ``path:sha256`` lines of ``code_files``, joined by newlines."""
    text = "\n".join(f"{rel}:{sha}" for rel, sha in code_files())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def python_version() -> str:
    """The running Python as ``major.minor``, such as ``3.10``."""
    return f"{sys.version_info[0]}.{sys.version_info[1]}"


def run_name(mode: str, pair: str) -> str:
    """``mode_<mode>_<pair>``, the stem of the lock, the marker and the stage outputs.

    A mode that is not a capital letter with an optional digit, or a pair that is not a venue
    USDT symbol, raises ``ValueError``, so no name can reach outside its folder."""
    if not isinstance(mode, str) or _MODE.fullmatch(mode) is None:
        raise ValueError(f"not a mode name: {mode!r}")
    return f"mode_{mode}_{check_symbol(pair)}"


def lock_path(out_dir: PathLike, mode: str, pair: str) -> Path:
    """``<out_dir>/mode_<mode>_<pair>_lock.json``."""
    return Path(out_dir) / f"{run_name(mode, pair)}_lock.json"


def marker_path(data_dir: PathLike, mode: str, pair: str, lock_sha: str) -> Path:
    """``<data_dir>/holdout_runs/mode_<mode>_<pair>_<lock_sha>.json``; ``lock_sha`` must be a
    lowercase sha256 hex digest (``ValueError`` otherwise)."""
    if not isinstance(lock_sha, str) or _SHA256.fullmatch(lock_sha) is None:
        raise ValueError(f"lock_sha must be a lowercase sha256 hex digest, got {lock_sha!r}")
    return Path(data_dir) / MARKER_DIR / f"{run_name(mode, pair)}_{lock_sha}.json"


def started_path(out_dir: PathLike, mode: str, pair: str) -> Path:
    """``<out_dir>/mode_<mode>_<pair>_test_started.json``: the start record, next to the lock."""
    return Path(out_dir) / f"{run_name(mode, pair)}_test_started.json"


def existing_markers(data_dir: PathLike, mode: str, pair: str) -> List[Path]:
    """The run markers of ``mode`` and ``pair`` under ``data_dir``, whatever lock made them."""
    folder = Path(data_dir) / MARKER_DIR
    if not folder.is_dir():
        return []
    pattern = re.compile(re.escape(f"{run_name(mode, pair)}_") + r"[0-9a-f]{64}\.json")
    return sorted(path for path in folder.iterdir() if pattern.fullmatch(path.name))


def read_lock(path: PathLike) -> Tuple[Dict[str, Any], str]:
    """The lock document at ``path`` and the sha256 of the bytes it was read from.

    ``LockError`` when the file is missing (train first), unreadable, not JSON or not an object."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise LockError(f"{path}: no lock; run the train stage first") from None
    except OSError as exc:
        raise LockError(f"{path}: the lock cannot be read ({exc})") from exc
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError as exc:  # UnicodeDecodeError and JSONDecodeError are both ValueErrors
        raise LockError(f"{path}: the lock is not valid JSON ({exc})") from exc
    if not isinstance(doc, dict):
        raise LockError(f"{path}: the lock is not a JSON object")
    return doc, hashlib.sha256(raw).hexdigest()


def check_lock(
    doc: Mapping[str, Any],
    *,
    config_sha: str,
    code_sha: str,
    train_manifest_sha: str,
    python: str,
) -> None:
    """Refuse unless the lock binds these values; the ``LockError`` lists every mismatch.

    ``config_sha`` is the sha256 of the configuration bytes, ``code_sha`` the current
    ``code_sha256()``, ``train_manifest_sha`` the sha256 of the current training manifest and
    ``python`` the current ``python_version()``."""
    if not isinstance(doc, Mapping):
        raise LockError("the lock is not a JSON object")
    bound = (
        ("config_sha256", config_sha, "the config file"),
        ("code_sha256", code_sha, "the code"),
        ("train_manifest_sha256", train_manifest_sha, "the training files"),
        ("python", python, "this Python"),
    )
    problems = []
    for key, current, source in bound:
        if key not in doc:
            problems.append(f"{key}: missing from the lock")
        elif doc[key] != current:
            problems.append(f"{key}: locked {doc[key]}, now {current} ({source})")
    if problems:
        noun = "mismatch" if len(problems) == 1 else "mismatches"
        raise LockError(
            f"the lock does not match the current run ({len(problems)} {noun}): "
            + "; ".join(problems),
            problems,
        )


def _claim(path: PathLike, doc: Mapping[str, Any], label: str) -> None:
    """Write ``doc`` at ``path`` exclusively; an existing file means the test already ran."""
    path = Path(path)
    try:
        write_json_exclusive(path, doc, label)
    except FileExistsError:
        raise LockError(f"{path}: the test already ran ({label}); it runs once") from None


def claim_marker(path: PathLike, doc: Mapping[str, Any]) -> None:
    """Write the run marker ``doc`` at ``path`` exclusively; an existing one is a ``LockError``."""
    _claim(path, doc, "run marker")


def claim_started(path: PathLike, doc: Mapping[str, Any]) -> None:
    """Write the start record ``doc`` at ``path`` exclusively; an existing one is a
    ``LockError``."""
    _claim(path, doc, "start record")
