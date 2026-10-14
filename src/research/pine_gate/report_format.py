"""Formatting helpers of the Indonesian report (``report``, ``report_checks``).

Research code; the bot never imports it. Standard library only.

Numbers: WR with one decimal and "%", two decimals within one point of a gate
threshold (criteria 3, 5, 6 and the C0 band) so a failing value never rounds onto
the threshold; shares likewise near the VST thresholds; bps with two decimals; net
per 1M as an integer; a missing or non-finite value as "–".
"""

from __future__ import annotations

import math
from typing import Any, List, Mapping, Optional, Sequence

from .checks import OUTCOME_AGREE_MIN, SIDE_AGREE_MIN
from .holdout_gate import C0_CENTRE_WR, C0_WR_TOLERANCE_PTS
from .spec import GateConfig
from .stats import net_per_1m

NA = "–"
NOT_AVAILABLE = "belum tersedia"
Lines = List[str]
_GATE = GateConfig()
NEAR_PTS = 1.0
# The WRs the gate reads a value against: criteria 3, 6 and 5, and the C0 band 50 ± 3.
WR_THRESHOLDS = (_GATE.min_wr, _GATE.robust_min_wr, _GATE.half_min_wr,
                 C0_CENTRE_WR - C0_WR_TOLERANCE_PTS, C0_CENTRE_WR + C0_WR_TOLERANCE_PTS)
SHARE_THRESHOLDS = (SIDE_AGREE_MIN, OUTCOME_AGREE_MIN)  # VST check (spec 5.12)

CONTENDER_LABELS = {
    "c1": "Kontender 1 (struktur Pine)",
    "c2": "Kontender 2 (logika bot)",
    "c3": "Kontender 3 (meta-labeling)",
}


def as_number(value: Any) -> Optional[float]:
    """``value`` as a finite float, else ``None`` (bool and non-numbers are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _digits(value: float, near: Sequence[float]) -> int:
    """Two decimals within ``NEAR_PTS`` of a threshold (so 56.96 never reads 57.0), else one."""
    return 2 if any(abs(value - t) <= NEAR_PTS for t in near) else 1


def fmt_wr(value: Any, near: Sequence[float] = WR_THRESHOLDS) -> str:
    """A WR in percent; two decimals near a gate threshold."""
    v = as_number(value)
    return NA if v is None else f"{v:.{_digits(v, near)}f}%"


def fmt_share(value: Any, near: Sequence[float] = SHARE_THRESHOLDS) -> str:
    """A share in 0..1 as a percentage; two decimals near a threshold (given as a share)."""
    v = as_number(value)
    if v is None:
        return NA
    pct = 100.0 * v
    return f"{pct:.{_digits(pct, [100.0 * t for t in near])}f}%"


def fmt_points(value: Any, near: Sequence[float] = ()) -> str:
    """Percentage points; two decimals near a threshold."""
    v = as_number(value)
    return NA if v is None else f"{v:.{_digits(v, near)}f}"


def fmt_bps(value: Any) -> str:
    v = as_number(value)
    return NA if v is None else f"{v:.2f}"


def fmt_money(value: Any) -> str:
    v = as_number(value)
    return NA if v is None else f"{round(v)}"


def fmt_p(value: Any) -> str:
    v = as_number(value)
    return NA if v is None else f"{v:.3f}"


def fmt_dec(value: Any, digits: int = 1) -> str:
    v = as_number(value)
    return NA if v is None else f"{v:.{digits}f}"


def fmt_int(value: Any) -> str:
    v = as_number(value)
    return NA if v is None else f"{round(v)}"


def net_or_none(gross_bps: Any) -> Optional[float]:
    v = as_number(gross_bps)
    return None if v is None else net_per_1m(v)


def contender_label(name: str) -> str:
    return CONTENDER_LABELS.get(name, name)


def _cell(value: Any) -> str:
    """A table cell; a pipe in a rule key would split the row."""
    return str(value).replace("|", "/").replace("\n", " ")


def md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> Lines:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    return out + [""]


def dig(doc: Any, *keys: Any) -> Any:
    """Nested lookup that yields ``None`` through anything that is not a mapping."""
    for key in keys:
        if not isinstance(doc, Mapping):
            return None
        doc = doc.get(key)
    return doc


def as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def md_section(title: str, body: Lines) -> Lines:
    return [f"## {title}", ""] + body


def unavailable(what: str = "Bagian ini") -> Lines:
    return [f"{what} {NOT_AVAILABLE}.", ""]


def yes_no(value: Any) -> str:
    return NA if value is None else ("ya" if value else "tidak")
