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
of the rule core (``bot/strategy/modes/*.py``) and every other module of ``bot`` and ``research``
they import, found by ``import_closure``. That is each module an import statement names, followed
from module to module, and each package around one, whose ``__init__`` runs first (``bot.arena``'s
brings in the arena modules). The walk runs each time, so a new import on main is covered with no
list to edit; ``tests/test_mode_backtest_structure.py`` holds it against the modules a fresh Python
process loads. Two imports are left out (``IO_ONLY_IMPORTS``): they sit inside functions the
package never calls and only read the live settings and Supabase (``bot.config`` in
``research.pine_gate.cli_common`` and ``bot.storage.supabase_trade_store`` in
``research.pine_gate.calibrate``). A file whose name starts with a dot is no module.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from research.pine_gate.bars import check_symbol
from research.pine_gate.fileio import write_json_exclusive

__all__ = [
    "CODE_DIRS",
    "IO_ONLY_IMPORTS",
    "MARKER_DIR",
    "LockError",
    "check_lock",
    "claim_marker",
    "claim_started",
    "code_files",
    "code_sha256",
    "existing_markers",
    "import_closure",
    "imported_names",
    "lock_path",
    "marker_path",
    "module_name",
    "module_source",
    "project_modules",
    "python_version",
    "read_lock",
    "run_name",
    "started_path",
]

PathLike = Union[str, Path]

MARKER_DIR = "holdout_runs"  # under the data folder, as the Pine gate keeps its markers
SRC_ROOT = Path(__file__).resolve().parents[2]
CODE_DIRS = ("research/mode_backtest", "bot/strategy/modes")  # every module, imported or not
PROJECT_ROOTS = ("bot", "research")  # the packages under src that the walk follows
# Imported inside functions the package never calls, to read the live settings and Supabase
# (``research.pine_gate.cli_common.make_settings`` and ``research.pine_gate.calibrate``'s
# ``make_get_rows``): neither shapes a computed result, so the walk neither keeps nor follows
# them, as ``tests/test_pine_gate_structure.py`` leaves them out of the Pine fingerprint.
IO_ONLY_IMPORTS = frozenset({"bot.config", "bot.storage.supabase_trade_store"})

_SHA256 = re.compile(r"[0-9a-f]{64}")
_MODE = re.compile(r"[A-Z][0-9]?")  # A, B, C, D, D2


class LockError(RuntimeError):
    """The lock is missing or broken, does not describe the current run, or the test already ran.

    ``problems`` holds one line per mismatch when ``check_lock`` found several."""

    def __init__(self, message: str, problems: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.problems: Tuple[str, ...] = tuple(problems)


def module_name(path: Path) -> str:
    """The dotted name of a source file under ``SRC_ROOT``; a package is named by its folder."""
    parts = list(path.relative_to(SRC_ROOT).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def module_source(name: str) -> Optional[Path]:
    """The source file of module ``name`` under ``SRC_ROOT``, or None when it is no project
    module (another library, or a name inside a module such as a class)."""
    if name.split(".")[0] not in PROJECT_ROOTS:
        return None
    base = SRC_ROOT.joinpath(*name.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path
    return None


def imported_names(tree: ast.AST, module: str, is_package: bool) -> Set[str]:
    """Every dotted name an import statement anywhere in ``module`` may load, relative ones
    resolved: ``import a.b`` names ``a.b``, and ``from a import b`` names both ``a`` and
    ``a.b``, because ``b`` may be a submodule. Imports inside functions count too."""
    package = module if is_package else module.rpartition(".")[0]
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1])
                source = f"{base}.{node.module}" if node.module else base
            else:
                source = node.module
            names.add(source)
            names.update(f"{source}.{alias.name}" for alias in node.names)
    return names


def project_modules(names: Iterable[str]) -> Set[str]:
    """The names that are project modules, each with the packages around it, whose
    ``__init__`` Python runs first; ``IO_ONLY_IMPORTS`` are left out."""
    found = set()
    for name in names:
        if name in IO_ONLY_IMPORTS or module_source(name) is None:
            continue
        parts = name.split(".")
        found.update(".".join(parts[:k]) for k in range(1, len(parts) + 1))
    return found


@functools.lru_cache(maxsize=None)
def _names_in_source(source: bytes, module: str, is_package: bool) -> FrozenSet[str]:
    """``imported_names`` of a module's source. Parsing is the slow part of the walk, and the
    same bytes always name the same modules, so it is done once per process."""
    return frozenset(imported_names(ast.parse(source), module, is_package))


def import_closure(paths: Iterable[Path]) -> Dict[str, Tuple[Path, bytes]]:
    """Every project module that the modules at ``paths`` import, followed transitively, by
    name, with its file and the bytes it was parsed from (each file is read once); the modules
    at ``paths`` are included."""
    todo = [module_name(path) for path in paths]
    seen: Dict[str, Tuple[Path, bytes]] = {}
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        path = module_source(name)
        source = path.read_bytes()
        seen[name] = (path, source)
        todo += project_modules(_names_in_source(source, name, path.name == "__init__.py"))
    return seen


def code_files() -> List[Tuple[str, str]]:
    """``(path relative to src, sha256)`` of every file the code fingerprint covers, sorted: the
    modules of ``CODE_DIRS`` and everything they import (``import_closure``), each hashed from
    the bytes the walk parsed. A file whose name is no module name (an editor's lock file, a
    macOS AppleDouble file, a ``stages.v2.py`` backup) cannot be imported and is left out."""
    starts = [
        path
        for folder in CODE_DIRS
        for path in sorted((SRC_ROOT / folder).glob("*.py"))
        if path.stem.isidentifier()
    ]
    return sorted(
        (path.relative_to(SRC_ROOT).as_posix(), hashlib.sha256(source).hexdigest())
        for path, source in import_closure(starts).values()
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
