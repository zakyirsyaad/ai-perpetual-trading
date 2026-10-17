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

## Task 2 — Modes configuration (YAML)

`config/modes.yaml` is the block of spec §6.3 copied byte for byte (a `diff` against spec lines 256-340 prints nothing; 85 lines, UTF-8, ends in a newline). `bot.strategy.modes.config` loads it into frozen dataclasses (`GeneralConfig`, `SelectorConfig`, `PairConfig`, `PullbackConfig`, `BreakoutConfig`, `SidewaysConfig`, `NearMeanConfig` and `TestConfig`, held by `ModesConfig`) and is the one source of every number the later tasks read. The lock file will bind `config_sha256(path)`, the sha256 of the file's bytes, so the loader refuses instead of guessing. Every refusal is a `ModesConfigError` whose message starts with the full YAML key path (`umum.extra`, `mode.A_pullback_long.sl_persen`), and all unknown and missing keys of a block are named in one message. PyYAML (`PyYAML>=6.0`, used through `yaml.safe_load` only) is added to `requirements.txt` and `pyproject.toml`.

What the loader refuses:

- an unknown or missing key at any level, a block that is not a mapping, a key written twice in one mapping (`safe_load` would silently keep the last; the check walks the composed nodes, so objects are still built by `safe_load` alone, and an alias loop cannot hang it);
- the wrong type: a `bool` is never a number, a float is never a whole number, `null` is never a value; a whole number is accepted for a float field and comes back as a float; `NaN`, infinities and ints beyond the float range are refused;
- percents and multipliers that are not greater than 0 (the two `lolos_hasil_*` thresholds may be negative); counts and periods below 1 (`jeda_menit` and `lolos_buang_posisi_teratas` may be 0);
- `tf_pemicu` other than `15m`/`5m` (A, D2) or `3m` (D); `faktor_k` below 1.0 or any text but `otomatis` (returned as `None`); `harus_sama_kondisi_dengan` other than a listed pair; `zona_waktu` other than `Asia/Jakarta` (controller ruling: the harness hard-codes WIB); `ai_boleh_buka_tutup: true`; a weekly window that is not a string or does not parse non-empty with `bot.core.entry_window.parse_weekly_window`;
- `uji` dates that are not quoted strings of the exact shape `YYYY-MM-DDTHH:MM:SS+HH:MM`, that are not midnight in Asia/Jakarta, or that do not satisfy `latihan_mulai < latihan_selesai < ujian_selesai`. They come back as aware datetimes in the +07:00 zone, whatever offset the file used. The shape is pinned because `datetime.fromisoformat` accepts more on 3.11 (CI) than on 3.10 (the `.venv`): a trailing `Z` or `+0700` would validate in one and not in the other.

Choices the brief left open: the pair names are fixed (`BTC-USDT` and `ETH-USDT`, both required); `pair` is a `MappingProxyType`, so it is read-only (`dataclasses.asdict` and `copy.deepcopy` cannot copy it; the docstring says so); mode B keeps its YAML key `maks_jarak_di_atas_sma20_1h_kali_atr` as its field name, since only modes A and D2 (which share `PullbackConfig`) map their two keys onto `maks_jarak_sma20_1h_kali_atr`.

RED, before any implementation, with `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_config -v` (paths shortened):

```text
test_modes_config (unittest.loader._FailedTest) ... ERROR

======================================================================
ERROR: test_modes_config (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_modes_config
Traceback (most recent call last):
  ...
  File ".../tests/test_modes_config.py", line 19, in <module>
    from bot.strategy.modes.config import (
ModuleNotFoundError: No module named 'bot.strategy.modes'

----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (errors=1)
exit code: 1
```

GREEN, the same command after implementing: the first run passed 52 of 53 tests. The failing test was mine (it used a YAML alias before its anchor, which YAML rightly calls undefined) and was fixed in the test, not in the loader. After two more tests, one more case in an existing test and a faster fixture the module has 55 tests:

```text
Ran 55 tests in 1.1s

OK
```

The tests include the brief's six named cases (the first with its assertions verbatim) and are data-driven over a rule table that must cover every leaf of the repo file (`test_the_rule_tables_cover_every_leaf_of_the_repo_file`), so adding a key to the YAML without a rule fails a test.

Verification:

- `.venv` was built by `uv` and has no `pip`, so `uv pip install --python .venv/bin/python -r requirements.txt` stood in for `.venv/bin/pip install -r requirements.txt`. It installed PyYAML 6.0.3 and `coverage` 7.16.2 (listed in `requirements.txt` but missing from the venv).
- Focused: 55 tests OK on Python 3.10.20 (`.venv`) and on 3.11.15 (a scratch venv, because CI runs 3.11).
- Line coverage of `bot/strategy/modes/`: 100 % (254 statements, 0 missed). Under 3.10 `coverage` cannot read `pyproject.toml` without `tomli`, so this focused run used a scratch rcfile; CI's 3.11 reads the real one.
- Mutation check (scratch script, not committed): 55 plausible bugs planted one at a time in a scratch copy of the final `src`, all caught (bool taken as a number, unknown or missing keys ignored, `ai_boleh_buka_tutup` allowed, naive or non-midnight dates, a repeated key allowed, the alias guard dropped, mode A reading D2's key, a writable `pair`, a hash of stripped text, and so on). One mutant survived an earlier pass: without the `isinstance(str)` check on the weekly window, an object whose `str()` looks like a window was accepted, because `parse_weekly_window` calls `str()` on its argument. A test for it was added.
- The existing guards cover the new package: planting `from bot.strategy import pine_indicators` and `import research.pine_gate` into a scratch copy of `config.py` made `test_no_live_module_imports_pine_indicators` and `test_no_bot_module_or_main_imports_research` fail.
- Full suite, exact command `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`: `Ran 1643 tests in 308.039s` / `OK` (1,588 existing + 55 new), exit 0, with the machine's load average at 40-107 and no stall.
- `compileall -q src tests` OK; `git diff --check` clean (new files marked intent-to-add); `tests.test_secret_hygiene` OK with the new files and this document tracked.
