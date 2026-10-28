"""Offline study of alternative exit rules (a profit lock, a lower floor, a trailing stop) against the live bot's.

Research code that only reads: it simulates exits from 1m candles and never places, changes or advises an
order. The bot never imports it; the one script allowed to is ``src/report_exit_variants.py``.
"""
