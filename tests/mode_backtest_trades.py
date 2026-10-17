"""Builders and the spec reader for the metrics, trade-log and table tests of the mode backtest.

Not a test module: the name does not match ``test*.py``, so discovery skips it. Tests import it as
``from tests.mode_backtest_trades import ...`` and run from the repository root.

``make_trade`` builds a ``Trade`` field by field, with no simulation behind it: the metrics read
only the exit reason, the profit, the volume and the two times, and the log writes the fields as
they are, so a test sets the numbers it cares about and takes the rest from the defaults. The
default money is not worked out from the fills (the committed ``settle`` does that and has its
own tests); it is a plausible 25,000 USDT LONG that earned 10 on 50,000 of volume.

``spec_log_columns`` and ``spec_table_header`` read the two layouts of section 2.9 out of the
design document itself, so a test can hold the code to the document instead of to a copy of it
typed into the test.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

from bot.strategy.modes.position_rules import LONG
from bot.strategy.modes.regime import NAIK
from research.mode_backtest.frames import HourlyContext
from research.mode_backtest.simulate import EXIT_TP, MODE_A, Trade
from tests.mode_backtest_helpers import MINUTE_MS

SPEC_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "superpowers"
    / "specs"
    / "2026-10-17-long-module-phase1-design.md"
)

FIRST_ENTRY_MS = 29_833_334 * MINUTE_MS  # a minute boundary in October 2026


def make_context(**fields) -> HourlyContext:
    """A NAIK context with round numbers; ``fields`` replace any of its fields."""
    values = dict(
        sma20_1h=100.0,
        sma50_1h=99.0,
        atr14_1h=1.0,
        atr_median=1.0,
        close_4h=100.5,
        ema50_4h=100.0,
        kondisi=NAIK,
    )
    values.update(fields)
    return HourlyContext(**values)


def make_trade(*, entry_ms: int = FIRST_ENTRY_MS, hold_min: int = 30, **fields) -> Trade:
    """A closed Mode A LONG on BTC-USDT.

    It signals and enters at ``entry_ms`` and exits ``hold_min`` minutes later unless ``signal_ms``
    or ``exit_ms`` is given. ``fields`` replace any other field of the ``Trade``."""
    values = dict(
        mode=MODE_A,
        pair="BTC-USDT",
        side=LONG,
        kondisi=NAIK,
        context=make_context(),
        faktor_k=1.0,
        signal_ms=entry_ms,
        entry_ms=entry_ms,
        entry_fill=100.01,
        tp=100.41004,
        sl=99.759975,
        exit_ms=entry_ms + hold_min * MINUTE_MS,
        exit_fill=100.39,
        exit_reason=EXIT_TP,
        gapped=False,
        both_touched=False,
        qty=249.975,
        notional_usdt=25_000.0,
        fee_usdt=17.5,
        slippage_usdt=5.0,
        pnl_usdt=10.0,
        volume_usdt=50_000.0,
    )
    values.update(fields)
    return Trade(**values)


def spec_log_columns() -> List[str]:
    """The trade-log columns of section 2.9 of the design document, in its order."""
    text = SPEC_PATH.read_text(encoding="utf-8")
    block = re.search(r"Per-trade log columns, in this order:\s*```\s*(.*?)```", text, re.DOTALL)
    if block is None:
        raise AssertionError(f"{SPEC_PATH.name} has no trade-log column block in section 2.9")
    return "".join(block.group(1).split()).split(",")


def spec_table_header() -> List[str]:
    """The cells of the results table header of section 2.9 of the design document."""
    text = SPEC_PATH.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.startswith("| Mode | Pair |")]
    if len(lines) != 1:
        raise AssertionError(f"{SPEC_PATH.name} has {len(lines)} results table headers, not 1")
    return [cell.strip() for cell in lines[0].strip().strip("|").split("|")]
