"""The numbers of one stage, the pass rule of the document and the 5m switch.

Research code; the bot never imports it. ``stage_metrics`` turns the trades of one run into the
columns of the results table (spec sections 2.9 and 5), ``pass_status`` applies the pass rule of
section 2.8 to them and ``wants_5m`` applies the one allowed adjustment. Nothing here reads a
clock, a file or a setting: the limits come in as the ``uji`` block of the configuration.

The definitions (spec section 5):

* Hasil per 1 juta is ``1,000,000 x sum(pnl_usdt) / sum(volume_usdt)``, pooled over the trades and
  not a mean of per-trade ratios. It is ``None`` without trades. Sums are exact (``math.fsum``),
  so the same set of trades always gives the same number, in whatever order it is listed.
* Menang is ``100 x TP / (TP + SL)``. A time exit counts in Posisi and in the result and not in
  Menang; it is ``None`` when no trade ended on a TP or an SL.
* Entry per hari aktif is the positions over the NAIK days of the stage, ``None`` with no NAIK day
  (never a ``ZeroDivisionError``).
* Rata-rata lama posisi is the mean of ``exit_ms - entry_ms`` in minutes.
* Without the top ``drop_top`` drops the ``drop_top`` trades with the highest ``pnl_usdt`` (ties:
  the earliest entry first) and recomputes Hasil per 1 juta; ``None`` when nothing remains.

The pass rule (spec section 2.8) is strict. Fewer than ``lolos_minimal_posisi`` positions is
``BELUM CUKUP SAMPEL`` whatever the numbers: not a failure, the mode goes on to the forward test.
Otherwise the mode is ``LOLOS`` when Hasil per 1 juta is greater than
``lolos_hasil_per_1_juta_lebih_dari`` and the result without the top positions is greater than
``lolos_hasil_tanpa_teratas_lebih_dari``, and ``GAGAL`` when it is not (equal is not greater).
``stage_metrics`` must be given the config's ``lolos_buang_posisi_teratas`` as ``drop_top``, because
``StageMetrics`` records the trimmed result and not how many positions were trimmed.

A number that cannot give a verdict raises ``ValueError`` and is never turned into a quiet answer:
a trade with an unknown exit reason, a profit that is not finite or a volume that is not above 0,
a count that is not a whole number, a result missing where the rule needs it, and a 5m switch with
no NAIK day to measure (the pre-registered rule cannot be evaluated).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from bot.strategy.modes.config import TestConfig

from .simulate import EXIT_SL, EXIT_TP, EXIT_WAKTU, Trade

__all__ = [
    "BELUM_CUKUP_SAMPEL",
    "GAGAL",
    "LOLOS",
    "StageMetrics",
    "pass_status",
    "stage_metrics",
    "wants_5m",
]

LOLOS = "LOLOS"
GAGAL = "GAGAL"
BELUM_CUKUP_SAMPEL = "BELUM CUKUP SAMPEL"

_EXIT_REASONS = (EXIT_TP, EXIT_SL, EXIT_WAKTU)
_PER_MILLION = 1_000_000
_MS_PER_MINUTE = 60_000


@dataclass(frozen=True)
class StageMetrics:
    """The results of one run over one stage, in the columns of the table of spec section 2.9.

    ``posisi`` counts every trade and ``tp``, ``sl`` and ``waktu`` count the exits by reason, so
    they add up to it. ``entry_per_hari_aktif`` is ``posisi / hari_naik`` and ``menang_pct`` the
    share of TP among the TP and SL exits; ``hasil_per_1_juta`` is the net result per 1M of volume
    and ``hasil_tanpa_teratas`` the same without the best positions. ``rata_lama_menit`` is the
    mean hold. Each of these is ``None`` where it is not defined (see the module docstring)."""

    posisi: int
    entry_per_hari_aktif: Optional[float]
    menang_pct: Optional[float]
    hasil_per_1_juta: Optional[float]
    tp: int
    sl: int
    waktu: int
    rata_lama_menit: Optional[float]
    hasil_tanpa_teratas: Optional[float]
    hari_naik: int


def _is_whole(value: object) -> bool:
    """Whether ``value`` is an ``int``; a bool is not one for this purpose."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite(value: object) -> bool:
    """Whether ``value`` is a finite number; None, text, a bool, NaN and infinity are not."""
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (TypeError, OverflowError):  # not a number at all; an int too large for a float
        return False


def _require_count(name: str, value: object) -> None:
    if not _is_whole(value) or value < 0:
        raise ValueError(f"{name} must be a whole number of at least 0, got {value!r}")


def _check_trades(trades: Sequence[Trade]) -> None:
    """Every trade must have a reason, a profit and a volume the metrics can use."""
    for number, t in enumerate(trades, start=1):
        if t.exit_reason not in _EXIT_REASONS:
            raise ValueError(
                f"trade {number}: exit_reason must be one of {', '.join(_EXIT_REASONS)}, "
                f"got {t.exit_reason!r}"
            )
        if not _is_finite(t.pnl_usdt):
            raise ValueError(
                f"trade {number}: pnl_usdt must be a finite number, got {t.pnl_usdt!r}"
            )
        if not (_is_finite(t.volume_usdt) and t.volume_usdt > 0):
            raise ValueError(
                f"trade {number}: volume_usdt must be finite and above 0, got {t.volume_usdt!r}"
            )


def _result_per_million(trades: Sequence[Trade]) -> Optional[float]:
    """Net result per 1M of volume over ``trades``; None without trades."""
    if not trades:
        return None
    profit = math.fsum(t.pnl_usdt for t in trades)
    volume = math.fsum(t.volume_usdt for t in trades)
    return _PER_MILLION * profit / volume


def stage_metrics(trades: Iterable[Trade], hari_naik: int, drop_top: int) -> StageMetrics:
    """The metrics of ``trades``, the positions of one run over one stage.

    ``hari_naik`` is the number of NAIK days of the stage and ``drop_top`` the number of best
    positions to leave out of ``hasil_tanpa_teratas`` (``uji.lolos_buang_posisi_teratas``). The
    trades may be listed in any order and any iterable is read once; they are not changed.

    Raises ``ValueError`` for a ``hari_naik`` or a ``drop_top`` that is not a whole number of at
    least 0, and for a trade whose ``exit_reason`` is not TP, SL or WAKTU, whose ``pnl_usdt`` is
    not finite or whose ``volume_usdt`` is not a finite number above 0."""
    _require_count("hari_naik", hari_naik)
    _require_count("drop_top", drop_top)
    book = tuple(trades)
    _check_trades(book)
    exits = Counter(t.exit_reason for t in book)
    tp, sl, waktu = exits[EXIT_TP], exits[EXIT_SL], exits[EXIT_WAKTU]
    decided = tp + sl
    best_first = sorted(book, key=lambda t: (-t.pnl_usdt, t.entry_ms))
    held_ms = sum(t.exit_ms - t.entry_ms for t in book)
    return StageMetrics(
        posisi=len(book),
        entry_per_hari_aktif=len(book) / hari_naik if hari_naik else None,
        menang_pct=100.0 * tp / decided if decided else None,
        hasil_per_1_juta=_result_per_million(book),
        tp=tp,
        sl=sl,
        waktu=waktu,
        rata_lama_menit=held_ms / (len(book) * _MS_PER_MINUTE) if book else None,
        hasil_tanpa_teratas=_result_per_million(best_first[drop_top:]),
        hari_naik=hari_naik,
    )


def _require_result(name: str, value: Optional[float]) -> float:
    """A result the pass rule needs: present and finite, or the rule cannot be evaluated."""
    if not _is_finite(value):
        raise ValueError(f"{name} is {value!r}: the pass rule cannot be evaluated")
    return value


def pass_status(m: StageMetrics, uji: TestConfig) -> str:
    """``LOLOS``, ``GAGAL`` or ``BELUM CUKUP SAMPEL`` for the metrics of a test stage.

    Fewer than ``uji.lolos_minimal_posisi`` positions is ``BELUM CUKUP SAMPEL`` whatever the
    numbers. Otherwise both ``hasil_per_1_juta`` and ``hasil_tanpa_teratas`` must be strictly
    greater than their limits (``uji.lolos_hasil_per_1_juta_lebih_dari`` and
    ``uji.lolos_hasil_tanpa_teratas_lebih_dari``) for ``LOLOS``; a result exactly on its limit
    fails. Raises ``ValueError`` when the positions are enough but one of the two results is
    missing or not finite, because then the rule has nothing to compare."""
    if m.posisi < uji.lolos_minimal_posisi:
        return BELUM_CUKUP_SAMPEL
    hasil = _require_result("hasil_per_1_juta", m.hasil_per_1_juta)
    tanpa = _require_result("hasil_tanpa_teratas", m.hasil_tanpa_teratas)
    passes = (
        hasil > uji.lolos_hasil_per_1_juta_lebih_dari
        and tanpa > uji.lolos_hasil_tanpa_teratas_lebih_dari
    )
    return LOLOS if passes else GAGAL


def wants_5m(m15: StageMetrics, uji: TestConfig) -> bool:
    """Whether Mode A moves its trigger from 15m to 5m candles (spec section 2.8).

    True exactly when the 15m training run made fewer entries per NAIK day than
    ``uji.pindah_5m_jika_entry_per_hari_naik_kurang_dari``; equal stays on 15m. Raises
    ``ValueError`` when the run has no entries-per-NAIK-day figure (no NAIK day in training): the
    pre-registered rule cannot be evaluated, and a silent answer would decide the test."""
    rate = m15.entry_per_hari_aktif
    if not _is_finite(rate):
        raise ValueError(
            f"entries per NAIK day is {rate!r} (the training run has no NAIK day): "
            "the 5m switch cannot be evaluated"
        )
    return rate < uji.pindah_5m_jika_entry_per_hari_naik_kurang_dari
