"""The per-trade CSV of the mode backtest (spec section 2.9).

Research code; the bot never imports it. ``trade_log_text`` writes one row per ``Trade`` under the
23 columns of the document, in its order, and ``write_trade_log`` puts the text on disk through
``research.pine_gate.fileio``, so a log is never overwritten.

* ``trade_id`` is the 1-based row number inside this CSV, so every log starts again at 1.
* ``sisi`` is ``LONG`` or ``SHORT`` (from the trade's ``side``), ``kondisi`` is the trade's own
  condition and ``alasan_exit`` is ``TP``, ``SL``, ``WAKTU`` or (mode L version ii) ``POTONG``.
  ``sma20_1h``, ``sma50_1h``,
  ``ema50_4h`` and ``atr14_1h`` come from the hourly context the signal was read from.
* ``harga_entry`` and ``harga_exit`` are the fills, slippage included; ``harga_tp`` and
  ``harga_sl`` are the levels set from the entry fill.
* Times are ISO 8601 in WIB with a ``+07:00`` offset, to the second (every time of a trade is on
  a minute). Numbers are machine text: a decimal point, no thousands separator, and the
  shortest text that reads back as the same float (``repr``). A missing indicator is an empty
  cell and never a lost trade. The report tables use the decimal comma; this file does not.
* Rows keep the order of the trades, and lines end with ``\\n``.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple, Union

from bot.strategy.modes.checks import check_side, is_whole
from bot.strategy.modes.position_rules import LONG, SHORT
from research.pine_gate.fileio import write_exclusive
from research.pine_gate.spec import WIB

from .simulate import Trade

__all__ = ["COLUMNS", "LIVE_COLUMNS", "trade_log_text", "wib_iso", "write_trade_log"]

# The columns of spec section 2.9, in the document's order.
COLUMNS: Tuple[str, ...] = (
    "trade_id",
    "pair",
    "mode",
    "sisi",
    "kondisi",
    "sma20_1h",
    "sma50_1h",
    "ema50_4h",
    "atr14_1h",
    "faktor_k",
    "waktu_sinyal",
    "waktu_entry",
    "harga_entry",
    "harga_tp",
    "harga_sl",
    "waktu_exit",
    "harga_exit",
    "alasan_exit",
    "fee_usdt",
    "slippage_usdt",
    "pnl_usdt",
    "notional_usdt",
    "volume_usdt",
)

# Mode L and comparator S add the exit version, the stop distance and the cut price.
LIVE_COLUMNS: Tuple[str, ...] = COLUMNS + ("versi", "sl_persen", "harga_potong")

_SIDES = {LONG: "LONG", SHORT: "SHORT"}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MS_PER_SECOND = 1000


def wib_iso(t_ms: int) -> str:
    """``t_ms`` (UTC epoch milliseconds) as ISO 8601 in WIB, e.g. ``2026-08-05T07:15:00+07:00``.

    Raises ``ValueError`` for a time that is not a whole number of milliseconds (a bool, a float
    and None are not) and for one that is not a whole second, which the format would cut."""
    if not is_whole(t_ms):
        raise ValueError(f"a time must be whole milliseconds, got {t_ms!r}")
    if t_ms % _MS_PER_SECOND:
        raise ValueError(f"a time must be a whole second, got {t_ms} ms")
    return (_EPOCH + timedelta(milliseconds=t_ms)).astimezone(WIB).isoformat()


def _sisi(side: object) -> str:
    check_side(side)
    return _SIDES[side]


def _number(value: Optional[float]) -> str:
    """The shortest text that reads back as the same float; an empty cell for None."""
    return "" if value is None else repr(float(value))


def _cells(trade_id: int, t: Trade) -> Dict[str, str]:
    """The cells of one row, by column name."""
    ctx = t.context
    return {
        "trade_id": str(trade_id),
        "pair": t.pair,
        "mode": t.mode,
        "sisi": _sisi(t.side),
        "kondisi": t.kondisi,
        "sma20_1h": _number(ctx.sma20_1h),
        "sma50_1h": _number(ctx.sma50_1h),
        "ema50_4h": _number(ctx.ema50_4h),
        "atr14_1h": _number(ctx.atr14_1h),
        "faktor_k": _number(t.faktor_k),
        "waktu_sinyal": wib_iso(t.signal_ms),
        "waktu_entry": wib_iso(t.entry_ms),
        "harga_entry": _number(t.entry_fill),
        "harga_tp": _number(t.tp),
        "harga_sl": _number(t.sl),
        "waktu_exit": wib_iso(t.exit_ms),
        "harga_exit": _number(t.exit_fill),
        "alasan_exit": t.exit_reason,
        "fee_usdt": _number(t.fee_usdt),
        "slippage_usdt": _number(t.slippage_usdt),
        "pnl_usdt": _number(t.pnl_usdt),
        "notional_usdt": _number(t.notional_usdt),
        "volume_usdt": _number(t.volume_usdt),
        "versi": t.versi,
        "sl_persen": _number(t.sl_pct),
        "harga_potong": _number(t.potong),
    }


def trade_log_text(trades: Iterable[Trade], live: bool = False) -> str:
    """The CSV of ``trades``: the header, then one row per trade in the order given.

    ``live=False`` writes the 23 columns of Mode A; ``live=True`` writes ``LIVE_COLUMNS`` (the
    same 23 plus ``versi``, ``sl_persen`` and ``harga_potong``, empty for a trade without them).

    Raises ``ValueError`` for a trade whose ``side`` is not ``LONG`` or ``SHORT`` or whose time is
    not a whole second (see ``wib_iso``)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    columns = LIVE_COLUMNS if live else COLUMNS
    writer.writerow(columns)
    for trade_id, t in enumerate(trades, start=1):
        cells = _cells(trade_id, t)
        writer.writerow([cells[name] for name in columns])
    return buffer.getvalue()


def write_trade_log(path: Union[str, Path], trades: Iterable[Trade], live: bool = False) -> None:
    """Write the log of ``trades`` to ``path`` (``live`` as in ``trade_log_text``).

    ``FileExistsError`` if the file exists.

    The text is built before the file is touched, so a trade that cannot be written leaves no
    file behind."""
    write_exclusive(Path(path), trade_log_text(trades, live), "trade log")
