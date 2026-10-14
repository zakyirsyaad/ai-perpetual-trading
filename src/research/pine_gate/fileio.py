"""Exclusive file output for the Pine gate research.

Research code; the bot never imports it. Standard library only.

``write_exclusive`` writes the complete file under a temp name in the target
directory, then hard-links it into place. ``os.link`` fails when the target
exists, so an existing file is never replaced, not even by a concurrent run.
Every output file (selection, holdout, report, CLI outputs, day files) goes
through it.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Union


def exists_message(path: Path, label: str) -> str:
    return f"{path}: {label} file already exists; remove it deliberately to write again"


def write_exclusive(path: Path, data: Union[str, bytes], label: str = "output") -> None:
    """Write ``data`` (text as UTF-8) to ``path``; ``FileExistsError`` if it exists.

    ``label`` names the file kind in the refusal ("selection", "holdout", ...)."""
    path = Path(path)
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if path.exists():
        raise FileExistsError(exists_message(path, label))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise FileExistsError(exists_message(path, label)) from None
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def json_text(doc: Any) -> str:
    """Strict JSON (NaN refused), sorted keys, one trailing newline."""
    return json.dumps(doc, sort_keys=True, indent=1, allow_nan=False) + "\n"


def write_json_exclusive(path: Path, doc: Any, label: str = "output") -> None:
    write_exclusive(path, json_text(doc), label)
