"""The holdout lock and the run settings it binds (spec 5.1).

Research code; the bot never imports it. Standard library only.

A selection file is usable by the test stage only when it was written by a
spec run: the current spec sha256, the spec's own configs and segments and the
real ``train_meta``. ``load_selection_checked`` enforces that. The file also
binds the calibration (``latencies_sha256``) and the code (``code_sha256``,
the research package plus ``pine_indicators.py``) it was made with.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .fileio import write_exclusive, write_json_exclusive
from .metalabel import train_meta
from .spec import (
    BLOCKS,
    CSCV_BLOCKS,
    TRAIN_END,
    TRAIN_START,
    ExecConfig,
    GuardConfig,
    ModelConfig,
    SelectionConfig,
    ms,
    spec_sha256,
)

__all__ = [
    "HoldoutLockError", "MetaTrainer", "RunSettings", "Segments", "TimeRange",
    "check_spec_run", "check_spec_sha", "code_files", "code_sha256", "equal_ranges",
    "is_spec_sha", "latencies_sha256", "load_selection_checked", "run_settings",
    "write_exclusive", "write_json_exclusive",
]

SHA256_HEX_LEN = 64
TimeRange = Tuple[int, int]
MetaTrainer = Callable[..., Tuple[Any, Dict[str, Any]]]

_SRC_ROOT = Path(__file__).resolve().parents[2]
_CODE_EXTRA = ("bot/strategy/pine_indicators.py",)


class HoldoutLockError(RuntimeError):
    """The selection file is missing, unreadable or was made under another spec."""


# --------------------------------------------------------------------------- segments


def equal_ranges(start_ms: int, end_ms: int, n: int) -> Tuple[TimeRange, ...]:
    """``n`` half-open ranges of equal length tiling [start, end); the last absorbs rounding."""
    if n < 1 or end_ms <= start_ms:
        raise ValueError(f"cannot cut {start_ms}..{end_ms} into {n} ranges")
    step = (end_ms - start_ms) // n
    edges = [start_ms + k * step for k in range(n)] + [end_ms]
    return tuple(zip(edges, edges[1:]))


def _check_tiling(name: str, ranges: Sequence[TimeRange], start_ms: int, end_ms: int) -> None:
    if not ranges or ranges[0][0] != start_ms or ranges[-1][1] != end_ms:
        raise ValueError(f"{name} must start at {start_ms} and end at {end_ms}")
    for (a, b), (c, _) in zip(ranges, ranges[1:]):
        if b != c:
            raise ValueError(f"{name} leaves a gap or overlap at {b}..{c}")
    if any(b <= a for a, b in ranges):
        raise ValueError(f"{name} has an empty range")


@dataclass(frozen=True)
class Segments:
    """Training range, its B1-B4 selection blocks and the CSCV blocks, in UTC ms."""

    train_start_ms: int
    train_end_ms: int
    block_bounds_ms: Tuple[TimeRange, ...]
    cscv_bounds_ms: Tuple[TimeRange, ...]

    def __post_init__(self) -> None:
        if len(self.block_bounds_ms) != len(BLOCKS):
            raise ValueError(
                f"need {len(BLOCKS)} selection blocks, got {len(self.block_bounds_ms)}"
            )
        if len(self.cscv_bounds_ms) < 2 or len(self.cscv_bounds_ms) % 2:
            raise ValueError("the number of CSCV blocks must be even and >= 2")
        start, end = self.train_start_ms, self.train_end_ms
        _check_tiling("block_bounds_ms", self.block_bounds_ms, start, end)
        _check_tiling("cscv_bounds_ms", self.cscv_bounds_ms, start, end)

    @classmethod
    def spec(cls) -> "Segments":
        start, end = ms(TRAIN_START), ms(TRAIN_END)
        return cls(
            train_start_ms=start,
            train_end_ms=end,
            block_bounds_ms=tuple((ms(a), ms(b)) for a, b in BLOCKS),
            cscv_bounds_ms=equal_ranges(start, end, CSCV_BLOCKS),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "train_start_ms": self.train_start_ms,
            "train_end_ms": self.train_end_ms,
            "block_bounds_ms": [list(r) for r in self.block_bounds_ms],
            "cscv_bounds_ms": [list(r) for r in self.cscv_bounds_ms],
        }


# --------------------------------------------------------------------------- run settings


@dataclass(frozen=True)
class RunSettings:
    """Everything a caller may inject into the train stage; a spec run uses the defaults."""

    segments: Segments
    exec_cfg: ExecConfig
    guard_cfg: GuardConfig
    sel_cfg: SelectionConfig
    model_cfg: ModelConfig
    meta_trainer: MetaTrainer

    @classmethod
    def spec(cls) -> "RunSettings":
        return cls(Segments.spec(), ExecConfig(), GuardConfig(), SelectionConfig(),
                   ModelConfig(), train_meta)

    def record(self) -> Dict[str, Any]:
        """``configs``, ``segments`` and ``meta_trainer`` as JSON-native values."""
        record = {
            "configs": {
                "exec": asdict(self.exec_cfg),
                "guard": asdict(self.guard_cfg),
                "selection": asdict(self.sel_cfg),
                "model": asdict(self.model_cfg),
            },
            "segments": self.segments.to_dict(),
            "meta_trainer": "train_meta" if self.meta_trainer is train_meta else "<injected>",
        }
        return json.loads(json.dumps(record, allow_nan=False))

    def non_spec_fields(self) -> List[str]:
        mine, spec = self.record(), RunSettings.spec().record()
        fields = [name for name in spec["configs"]
                  if mine["configs"][name] != spec["configs"][name]]
        fields += [name for name in ("segments", "meta_trainer") if mine[name] != spec[name]]
        return fields


def run_settings(
    segments: Optional[Segments] = None,
    exec_cfg: Optional[ExecConfig] = None,
    guard_cfg: Optional[GuardConfig] = None,
    sel_cfg: Optional[SelectionConfig] = None,
    model_cfg: Optional[ModelConfig] = None,
    meta_trainer: MetaTrainer = train_meta,
) -> RunSettings:
    """Defaults filled in; refuses injected segments with the real ``train_meta``,
    whose folds always use the dates of ``spec.BLOCKS``."""
    spec = RunSettings.spec()
    settings = RunSettings(
        segments if segments is not None else spec.segments,
        exec_cfg if exec_cfg is not None else spec.exec_cfg,
        guard_cfg if guard_cfg is not None else spec.guard_cfg,
        sel_cfg if sel_cfg is not None else spec.sel_cfg,
        model_cfg if model_cfg is not None else spec.model_cfg,
        meta_trainer,
    )
    if meta_trainer is train_meta and settings.segments != spec.segments:
        raise ValueError(
            "train_meta validates on spec.BLOCKS; injected segments need a meta_trainer"
        )
    return settings


def check_spec_sha(spec_sha: str) -> None:
    if (not isinstance(spec_sha, str) or len(spec_sha) != SHA256_HEX_LEN
            or any(ch not in "0123456789abcdef" for ch in spec_sha)):
        raise ValueError(f"spec_sha must be a lowercase sha256 hex digest, got {spec_sha!r}")


def is_spec_sha(spec_sha: str) -> bool:
    """Whether ``spec_sha`` is the current spec's sha256 (a real, locked run)."""
    return spec_sha == spec_sha256()


def check_spec_run(spec_sha: str, settings: RunSettings) -> None:
    """The real spec sha256 may only label a run with every spec default."""
    if not is_spec_sha(spec_sha):
        return
    fields = settings.non_spec_fields()
    if fields:
        raise ValueError(
            "the current spec sha256 may only label a spec run; non-spec settings: "
            + ", ".join(fields)
        )


def latencies_sha256(latencies_ms: Sequence[float]) -> str:
    """sha256 of the latency sample as a JSON list of floats; binds it to the selection."""
    payload = json.dumps([float(x) for x in latencies_ms], allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- code hash


def code_files() -> List[Tuple[str, str]]:
    """(path relative to ``src``, sha256) of every research .py plus pine_indicators.py."""
    paths = sorted((_SRC_ROOT / "research").rglob("*.py"))
    paths += [_SRC_ROOT / rel for rel in _CODE_EXTRA]
    files = [(p.relative_to(_SRC_ROOT).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest())
             for p in paths]
    return sorted(files)


def code_sha256() -> str:
    """sha256 over the sorted "path:sha256" lines of ``code_files``."""
    text = "\n".join(f"{rel}:{sha}" for rel, sha in code_files())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- the lock


def load_selection_checked(path: Path, spec_sha: str) -> Dict[str, Any]:
    """The holdout lock: the selection document, only if it was made under ``spec_sha``
    by a spec run (spec configs and segments, the real ``train_meta``)."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise HoldoutLockError(f"{path}: no selection file; run the train stage first") from exc
    except OSError as exc:
        raise HoldoutLockError(f"{path}: selection file unreadable ({exc})") from exc
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError as exc:  # includes UnicodeDecodeError
        raise HoldoutLockError(f"{path}: selection file is not valid JSON ({exc})") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("spec_sha256"), str):
        raise HoldoutLockError(f"{path}: selection file records no spec_sha256")
    if doc["spec_sha256"] != spec_sha:
        raise HoldoutLockError(
            f"{path}: spec_sha256 {doc['spec_sha256']} does not match the current spec {spec_sha}"
        )
    _check_spec_record(path, doc)
    return doc


def _check_spec_record(path: Path, doc: Mapping[str, Any]) -> None:
    """The file must record the spec's own configs, segments and ``train_meta``."""
    spec = RunSettings.spec().record()
    configs = doc.get("configs")
    if not isinstance(configs, dict) or sorted(configs) != sorted(spec["configs"]):
        raise HoldoutLockError(f"{path}: configs missing or incomplete; not a spec run")
    for name, expected in spec["configs"].items():
        if configs[name] != expected:
            raise HoldoutLockError(f"{path}: configs.{name} differ from the spec; not a spec run")
    for name in ("segments", "meta_trainer"):
        if doc.get(name) != spec[name]:
            raise HoldoutLockError(f"{path}: {name} differ from the spec; not a spec run")
