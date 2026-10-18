"""The number and side checks that the rules of the LONG module and its backtest share.

They live in one place so that no copy can drift from another. A ``bool`` is never a number or
a side, although Python counts ``True`` as 1, and a side is the whole number ``LONG`` (+1) or
``SHORT`` (-1), never ``1.0``.

The module takes values only. No live module imports it.
"""

import math

__all__ = [
    "LONG",
    "SHORT",
    "check_side",
    "is_finite",
    "is_whole",
    "require_not_negative",
    "require_positive",
]

LONG = 1
SHORT = -1


def is_whole(value: object) -> bool:
    """Whether ``value`` is an ``int``. A bool is not one."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_finite(value: object) -> bool:
    """Whether ``value`` is a finite ``int`` or ``float``.

    None, text, a bool, NaN, infinity and an int too large for a float are not."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


def require_positive(name: str, value: object) -> None:
    """Raise ``ValueError`` unless ``value`` is a finite number above 0."""
    if not (is_finite(value) and value > 0):
        raise ValueError(f"{name} must be a finite number above 0, got {value!r}")


def require_not_negative(name: str, value: object) -> None:
    """Raise ``ValueError`` unless ``value`` is a finite number of at least 0."""
    if not (is_finite(value) and value >= 0):
        raise ValueError(f"{name} must be a finite number of at least 0, got {value!r}")


def check_side(side: object) -> None:
    """Raise ``ValueError`` unless ``side`` is ``LONG`` or ``SHORT``, as a whole number."""
    if not is_whole(side) or side not in (LONG, SHORT):
        raise ValueError(f"side must be LONG ({LONG}) or SHORT ({SHORT}), got {side!r}")
