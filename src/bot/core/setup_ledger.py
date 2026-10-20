"""The setup ledger's bucket key.

An entry is filed under the side it takes, the regime condition the gate read for that hour and
the volatility regime of its setup. The ledger's maths (per-bucket outcome statistics and the
verdict they feed) lands with this module in a later change; for now it holds only `bucket_key`,
which the orchestrator stamps on every entry so the history is already keyed when the ledger arrives.

This module imports neither `bot.strategy.pine_indicators` nor `bot.strategy.modes`; only
`bot/core/regime_gate.py` may.
"""

from __future__ import annotations


def bucket_key(side: str, kondisi: str, atr_regime: str) -> str:
    """`"SHORT|TURUN|atr_mid"`: the side upper-cased, the condition and the ATR regime as given."""
    return f"{str(side).strip().upper()}|{kondisi}|{atr_regime}"
