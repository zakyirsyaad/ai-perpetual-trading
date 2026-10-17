# LONG Module Phase 1 — TDD Evidence

Offline backtest of the manager's LONG module for the BingX VST bot. The rule core will live in `src/bot/strategy/modes/` and the harness in `src/research/mode_backtest/`; neither is wired into the live bot. Spec: `docs/superpowers/specs/2026-10-17-long-module-phase1-design.md`. Plan: `docs/superpowers/plans/2026-10-17-long-module-phase1.md`.

## User journey

The manager's document "Modul LONG — Spesifikasi & Rencana Uji" (17 October 2026) adds rule-based modes (A and B for up-trends, C for sideways markets, D2 as a mirrored pullback SHORT) and ETH to a bot that only shorts BTC. Each mode is tested on its own and passes only if its result per 1,000,000 USDT of volume is better than −150. The document fixes the order: step 0 first (split the 16 October Pine holdout by side and trend condition, no new test), then Mode A on BTC. Phase 1 covers exactly those two. It backtests Mode A on production 1m data (training, a lock file, then a one-shot test) and reports in Indonesian. The live bot, `.env` and the VPS are not touched.

Each task below appends its RED output and GREEN result.

## Task 1 — SMA, population stdev and rolling median

`bot/strategy/pine_indicators.py` gains `sma`, `stdev` and `rolling_median`. They are the base of the hourly SMA20/SMA50 and of the 720-hour median of ATR14 that the mode selector and the volatility guard read. Each takes a list that may hold `None` and returns a list of the same length. A value is `None` until `n` values exist and while the last `n` values hold a `None` (ATR warm-up values, for instance). `stdev` is the population deviation, as TradingView's `ta.stdev`.

RED, before any implementation, with `PYTHONPATH=src .venv/bin/python -m unittest tests.test_pine_indicators -v` (paths shortened). The error names `rolling_median` because the import list is alphabetical; `sma` and `stdev` were missing as well.

```text
test_pine_indicators (unittest.loader._FailedTest) ... ERROR

======================================================================
ERROR: test_pine_indicators (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_pine_indicators
Traceback (most recent call last):
  ...
  File ".../tests/test_pine_indicators.py", line 6, in <module>
    from bot.strategy.pine_indicators import (
ImportError: cannot import name 'rolling_median' from 'bot.strategy.pine_indicators' (.../src/bot/strategy/pine_indicators.py)

----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (errors=1)
```

GREEN:

- `_rolling_complete(values, n, pick)` wraps the existing `_rolling`, so the period check and the window slicing stay in one place; a window that holds a `None` gives `None`. `sma`, `stdev` and `rolling_median` are one line each over `statistics.fmean`, `pstdev` and `median`.
- `_rolling` is annotated `Sequence[Optional[float]]`. This is an annotation only: `rolling_max` and `rolling_min` behave as before.
- The module docstring now describes the window statistics.

Nine new tests in `WindowStatisticTests`: the five from the plan (`sma` None-then-mean, a window holding a `None`, population stdev, odd and even medians, prefix stability), plus the `None` rule for all three functions on one series with warm-up and a later gap, an exactly zero deviation for a flat window, short and empty input, and a period below 1. The prefix test uses a series with a gap every 25th value, so about a third of the windows hold a `None`; its seeded run compares both `None` and numeric values for each function.

Verification:

```text
PYTHONPATH=src .venv/bin/python -m unittest tests.test_pine_indicators -v
Ran 25 tests in 0.5s — OK                     (16 existing + 9 new)
pine_indicators.py line coverage: 100% (120 statements, 0 missed; focused run under Python 3.11, because .venv has no coverage module)
Mutation check (scratch script, not committed): 8 plausible bugs patched in one at a time, all caught —
  sample deviation, None not blanked, None dropped from the window, short windows computed,
  look-ahead window, no period check, unsorted median, lower-middle median
Full suite (PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v)
Ran 1585 tests in 36.0s — OK                  (Python 3.10.20)
compileall: passed
git diff --check: passed
secret hygiene test (tests.test_secret_hygiene): passed, with this document tracked
```

Two earlier full runs were made while unrelated jobs held the machine's load average between 50 and 160. Both stalled at `test_telemetry_poller.RunLoopTests.test_run_keeps_polling_until_cancelled`, the known Python 3.10 hang (a bare `asyncio.wait_for` in the poller can swallow a cancellation, so the poller loop never stops; telemetry code this task does not touch). Each was stopped after more than 2 minutes with 0 failures among the 1,349 tests that ran before it; the 235 tests after it also passed on their own (`Ran 235 tests`, OK), and the stalled test passes alone in about 0.03 s (three runs out of three). No `bot` module imports `pine_indicators` (`test_pine_gate_structure` enforces it), so the poller cannot see this change. The third run, on a quiet machine, was clean.

Note: `pine_indicators.py` is inside the Pine gate's code fingerprint (`research.pine_gate.lock`), so the Pine selection file's `code_sha256` no longer matches current code. That was already so after PR #36; the Pine holdout ran once and its run marker refuses a second test regardless.
