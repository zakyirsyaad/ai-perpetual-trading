"""A synthetic rising market and the configuration that stages the mode backtest on it.

Not a test module: the name does not match ``test*.py``, so discovery skips it. Tests import it as
``from tests.mode_backtest_stages import ...`` and run from the repository root.

The market covers one trading week, Monday 6 to Friday 10 July 2026, inside the weekly window
``mon 07:00-sat 00:00``. ``stage_config_text`` moves the ``uji`` dates of ``config/modes.yaml``
onto it: training is Monday to Wednesday and the test Thursday and Friday. Every day of the test is
a NAIK day; of training, Tuesday and Wednesday are for the 15m run, whose EMA20 is first known on
Tuesday at 16:00 WIB (160 candles after the first minute), and all three days for the 5m run.
``naik_price`` rises about 0.06 % an hour and swings 0.4 % every 50 minutes, so
the hourly frame reads NAIK, the 15m and 5m candles pull back to their EMA and Mode A trades, and
the 3m candles come near their mean, so the comparator trades too. The warm-ups are built from the
same minute prices (``write_minute_warmups``) and are just long enough for every hourly series to
be warm at 00:00 WIB of the first training day.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Sequence

from tests.mode_backtest_helpers import write_days, write_minute_warmups

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config" / "modes.yaml"
SYMBOL = "BTC-USDT"

TRAIN_DAYS = (date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8))  # Monday to Wednesday
TEST_DAYS = (date(2026, 7, 9), date(2026, 7, 10))  # Thursday and Friday
ALL_DAYS = TRAIN_DAYS + TEST_DAYS
STAGE_DATES = {
    "latihan_mulai": '"2026-07-06T00:00:00+07:00"',
    "latihan_selesai": '"2026-07-09T00:00:00+07:00"',
    "ujian_selesai": '"2026-07-11T00:00:00+07:00"',
}

# The 1h ATR14 is None for 111 hours and its 720-value median needs 719 more, so 831 closed hours
# warm it at 00:00 WIB. The 4h EMA50 needs 400 closed candles, and the last 4h warm-up bar (16:00
# to 20:00 UTC) is still forming at 00:00 WIB (17:00 UTC), so 401 bars. One fewer of either leaves
# that series None on the first training day.
WARM_HOURS_1H = 831
WARM_BARS_4H = 401

# The clock of the stage runs: 2026-10-18 10:00 WIB, so the outputs are dated 20261018.
CLOCK = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc)
STAMP = "20261018"


def naik_price(m: int) -> float:
    """The price of minute ``m`` (minutes from 00:00 WIB of the first day, negative before it)."""
    return 100.0 * math.exp(1e-5 * m) * (1.0 + 0.004 * math.sin(2.0 * math.pi * m / 50.0))


def write_naik_market(
    data_dir: Path,
    days: Sequence[date] = ALL_DAYS,
    hours_1h: int = WARM_HOURS_1H,
    bars_4h: int = WARM_BARS_4H,
    symbol: str = SYMBOL,
) -> List[Path]:
    """Write the day files of ``days`` and both warm-ups of ``naik_price``; returns the day paths.

    The warm-ups end before the earliest of ``days``, on the axis of ``write_days``."""
    paths = write_days(data_dir, symbol, days, naik_price)
    write_minute_warmups(data_dir, symbol, min(days), hours_1h, bars_4h, naik_price)
    return paths


def stage_config_text(**uji: object) -> str:
    """``config/modes.yaml`` with the ``uji`` dates of the synthetic week and the given values.

    A keyword replaces the value of that ``uji`` key as YAML text (a string is written as it is,
    so a date must carry its quotes); a key that is not written exactly once in the file raises
    ``AssertionError``, so an edit of the repo file cannot leave a test on the old values."""
    values = {**STAGE_DATES, **{key: str(value) for key, value in uji.items()}}
    head, marker, block = REPO_CONFIG.read_text(encoding="utf-8").partition("\nuji:")
    if not marker:
        raise AssertionError(f"{REPO_CONFIG.name} has no uji block")
    for key, value in values.items():
        pattern = re.compile(rf"^(  {re.escape(key)}: )\S+", re.MULTILINE)
        block, count = pattern.subn(lambda match, text=value: match.group(1) + text, block)
        if count != 1:
            raise AssertionError(f"uji.{key} is written {count} times in {REPO_CONFIG.name}")
    return head + marker + block


def write_stage_config(path: Path, **uji: object) -> Path:
    """Write ``stage_config_text(**uji)`` to ``path`` and return it."""
    path.write_text(stage_config_text(**uji), encoding="utf-8")
    return path
