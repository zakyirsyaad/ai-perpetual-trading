# LONG Mirror with Live Exits (mode L) — TDD Evidence

Offline backtest of a LONG mirror of the live SHORT bot, with the live bot's exits, on the Phase 1
harness. Spec: `docs/superpowers/specs/2026-10-18-long-mirror-live-exit-design.md`. Each task below
records its RED output and GREEN result.

## User journey

The manager asked whether the live SHORT bot's edge has a LONG twin. Mode A (pullback LONG in an
up-trend) already failed its one-shot test, so this plan does not retune it: it mirrors the live
bot's near-mean 3m entry to LONG in NAIK days, with the live bot's exits (ATR-scaled SL, a TP at the
net +56 USDT level, a 60-minute time exit, and a cut at 0.10 % adverse from minute 10 in version ii),
and runs the SHORT original in TURUN days as comparator S. The operator runs `train --mode L`, then,
on their go-ahead, `verify-copy` and the one-shot `test --mode L`, and reads an Indonesian table with
a pass or fail line for L(i) and L(ii). `rescore` is a side tool: it reprices Mode A's Phase 1 logs
with the same costs, as a cost sensitivity and never a verdict. Nothing here touches the live bot.

## Task 1: Live-exit config file and loader

Files: `config/modes_live_exit.yaml` (spec section 7, verbatim), `src/bot/strategy/modes/config.py`
(`LiveExitModeConfig`, `CostConfig`, `LiveExitConfig`, `parse_live_exit_config`,
`load_live_exit_config_with_sha256`, `check_live_trigger_matches`), `tests/test_modes_live_exit_config.py`.
`config/modes.yaml` is unchanged (`git diff --quiet config/modes.yaml`).

### RED

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_live_exit_config -v
ERROR: test_modes_live_exit_config (unittest.loader._FailedTest)
ImportError: Failed to import test module: test_modes_live_exit_config
  File ".../tests/test_modes_live_exit_config.py", line 17, in <module>
    from bot.strategy.modes.config import (
ImportError: cannot import name 'CostConfig' from 'bot.strategy.modes.config'
Ran 1 test in 0.000s
FAILED (errors=1)
```

Expected: none of the new names exist yet.

### GREEN

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_live_exit_config tests.test_modes_config
Ran 85 tests in 2.5s
OK
```

`load_modes_config_with_sha256` and the new loader now share `_load_with_sha256` (one read, hashed
and parsed from the same bytes); `tests.test_modes_config` passes unchanged. Full suite result is in
the task report.

Full suite before commit: `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests` -> `Ran 2306 tests in 196.763s, OK`.

## Task 2: Exit-level formulas and a side-neutral near-mean trigger

Files: `src/bot/strategy/modes/live_exit.py` (`LiveExitLevels`, `stop_pct`, `net_target_price`,
`live_exit_levels`), `src/bot/strategy/modes/near_mean.py` (`near_mean`; `near_mean_short` is the same
function under Mode D's name), `tests/test_modes_live_exit.py`, `tests/test_modes_near_mean.py`.
Numbers only, no bars and no I/O; the expected prices in the tests are worked out by hand from spec section 4.

### RED

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_live_exit tests.test_modes_near_mean
  File ".../tests/test_modes_near_mean.py", line 15, in <module>
    from bot.strategy.modes.near_mean import near_mean, near_mean_short, stretch_from_window
ImportError: cannot import name 'near_mean' from 'bot.strategy.modes.near_mean'
Ran 2 tests in 0.000s
FAILED (errors=2)
```

(`tests.test_modes_live_exit` fails the same way on `bot.strategy.modes.live_exit`, which did not exist.)

### GREEN

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_live_exit tests.test_modes_near_mean
Ran 28 tests in 0.004s
OK
```

Reference values reproduced: stop pct 0.28 / 0.30 / 0.40 for ATR 0.10 / 0.20 / 0.30; TP ratio
1.0032416208 (LONG) and 0.9967616192 (SHORT) at 25,000; entry 100.003 LONG gives sl 99.722992,
tp 100.327172, cut 99.902997; entry 99.997 SHORT gives sl 100.276992, tp 99.673172, cut 100.096997.
The net-reaches-N property is also checked from gross minus both fee legs.

Full suite: `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests` -> `Ran 2322 tests, OK`;
`compileall` and `git diff --check` clean.

## Task 3: ATR % on the 3m candles, and condition days for any condition

`frames.py` gains `stretch_and_atr_series` (the stretch loop plus the live ATR over the same
window, divided by the candle's close, times 100, `None` wherever the stretch is), `atr_pct_series`
(the same column from 3m bars alone), `PairFrames.atr_pct_3m` (last field, default `()`, filled by
`build_pair_frames`) and `HourlyFrame.condition_days`. `stretch_series` is a wrapper, so the
Pine-equality test is untouched; `naik_days` keeps its signature and is `condition_days(..., NAIK)`.

### RED

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_frames
ERROR: test_atr_pct_matches_the_live_atr_on_each_window
AttributeError: module 'research.mode_backtest.frames' has no attribute 'atr_pct_series'
ERROR: test_atr_pct_is_none_where_the_stretch_is_none
AttributeError: module 'research.mode_backtest.frames' has no attribute 'stretch_and_atr_series'
ERROR: test_turun_days_count_like_naik_days
AttributeError: 'HourlyFrame' object has no attribute 'condition_days'
ERROR: test_the_frames_carry_the_atr_percent_of_each_stretch_candle
AttributeError: 'PairFrames' object has no attribute 'atr_pct_3m'
FAIL: test_the_fields_come_in_the_order_the_event_loop_reads_them
Ran 68 tests in 3.219s
FAILED (failures=1, errors=11)
```

### GREEN

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_frames
Ran 68 tests in 5.075s
OK
$ PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
Ran 2330 tests in 108.665s
OK
```

The existing NAIK-day tests and `test_stretch_series_matches_pine_feature_frame` pass untouched;
`compileall` and `git diff --check` are clean.

## Task 4: the live-exit walk and per-kind fills

New module `src/research/mode_backtest/live_sim.py` (`resolve_live_exit`, `settle_live`); `simulate.py`
gains `EXIT_POTONG = "POTONG"` and makes its three walk helpers public (`check_walk`, `reaches_sl`,
`reaches_tp`: a pure rename, because `test_no_module_imports_a_private_name_of_another` forbids
importing a private name across modules). Mode A's `resolve_exit` and `settle` are unchanged.

### RED

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_live_sim
    from research.mode_backtest.live_sim import resolve_live_exit, settle_live
ModuleNotFoundError: No module named 'research.mode_backtest.live_sim'
Ran 1 test in 0.000s
FAILED (errors=1)
```

A first GREEN attempt also failed the structure guard, which is how the rename was found:

```
FAIL: test_no_module_imports_a_private_name_of_another
AssertionError: ['live_sim.py: line 47: _check_walk', 'live_sim.py: line 47: _reaches_sl', ...]
```

### GREEN

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_live_sim
Ran 33 tests in 0.010s
OK
$ PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
Ran 2363 tests in 135.313s
OK
```

The money table of the brief (LONG TP 63.512156 / fee 17.528364 / volume 50081.04052, LONG SL
-88.721564 / 49928.7535, POTONG -43.739563, WAKTU 5.490011, SHORT TP 63.487856, SHORT SL -88.778439) is
reproduced to 6 decimals. `compileall` and `git diff --check` are clean.

## Task 5: the event loop for L and S

`live_sim.py` gains `MODE_L`, `MODE_S`, `LiveRunSpec` and `simulate_live_run`: the loop of `simulate_run`
with mode L's pieces (the near-mean trigger on the 3m candles, Phase 1's seven gates with a 60-minute
segment edge, the levels from the entry fill and `atr_pct_3m`, the live-exit walk and `settle_live`).
LONG runs in NAIK and trades as mode "L"; SHORT runs in TURUN as comparator "S". `simulate.py` makes
`_Book`, `_Gates`, `_closes`, `_NearMeanTrigger` and `_check_column` public (`Book`, `Gates`,
`candle_closes`, `NearMeanTrigger`, `check_column`) and, for the same reason (the structure test forbids
importing a private name across modules), `_moment`, `_check_inputs` (now takes the pair name) and
`_exit_ms` (now `outcome_exit_ms`) too. `Gates.__init__` is `(umum, kondisi_gate, end_ms, limit_min)`.
`Trade` gains the defaulted `versi`, `sl_pct`, `potong`. The trigger now calls `near_mean` (the same
function `near_mean_short` aliases).

Golden: `MODE_A_SYNTHETIC_LOG_SHA = 39aa210b16d9a6ccdf6bf3c78718cdb0cd9411b36dd07cef8913879ca8112431`,
computed on HEAD a4dffcc before any change: Mode A, `"5m"`, NAIK, the whole synthetic week of
`tests/mode_backtest_stages.py` as one segment (2026-07-06 00:00 WIB to 2026-07-11 00:00 WIB), 30 trades,
sha256 of `trade_log_text(result.trades)` (UTF-8). It was computed with `write_naik_market`, `load_pair`,
`build_pair_frames(..., load_modes_config(REPO_CONFIG))` and `simulate_run(frames, cfg, RunSpec(MODE_A,
"BTC-USDT", "5m", NAIK, start, end))`; the committed test is that same code.

### RED

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_live_sim
ImportError: cannot import name 'MODE_L' from 'research.mode_backtest.live_sim'
Ran 1 test in 0.000s
FAILED (errors=1)
```

### GREEN

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_live_sim tests.test_mode_backtest_simulate tests.test_mode_backtest_loop tests.test_mode_backtest_structure tests.test_mode_backtest_tradelog
Ran 256 tests in 2.737s
OK
$ PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
Ran 2400 tests in 65.578s
OK
```

The golden test passes after the refactor, so Mode A's log is byte-identical. Four hand mutants of
`simulate_live_run` (cut always on, wrong segment-edge limit, wrong trigger band, a defaulted ATR
percent) each fail the live-sim tests. `compileall` and `git diff --check` are clean.

## Task 6

Metrics, trade log and table for mode L. `StageMetrics` gains `potong` and `entry_per_hari_kalender`
(defaulted, appended after Phase 1's fields); `stage_metrics(..., hari_kalender=None)` counts POTONG
exits, makes Menang `TP / (TP + SL + POTONG)` and divides positions by the calendar days. `LIVE_COLUMNS`
= the 23 Mode A columns + `versi`, `sl_persen`, `harga_potong`, written only by `trade_log_text(...,
live=True)`. `report.live_table(rows)` (rows are `LiveRow(mode, versi, pair, tahap, metrics)`) and
`report.live_status_line(versi, m, uji, pair, mode="L")` give "Ujian mode L versi ii BTC-USDT: GAGAL.
Posisi ...". The status figures and `exact` moved from `stages.py` into `report.py` (`status_figures`,
`exact`) and Phase 1's test status line now calls them, so both lines share one text.

### RED

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_metrics tests.test_mode_backtest_tradelog tests.test_mode_backtest_report
ImportError: cannot import name 'LIVE_COLUMNS' from 'research.mode_backtest.tradelog'
ImportError: cannot import name 'LiveRow' from 'research.mode_backtest.report'
TypeError: stage_metrics() got an unexpected keyword argument 'hari_kalender'  (9 metrics tests)
Ran 74 tests in 0.029s
FAILED (errors=14)
```

### GREEN

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_metrics tests.test_mode_backtest_tradelog tests.test_mode_backtest_report tests.test_mode_backtest_simulate
Ran 221 tests ... OK   (incl. the Mode A golden log sha, unchanged)
$ PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
Ran 2416 tests in 391.905s
OK
```

`compileall` and `git diff --check` are clean. One Phase 1 test changed: the field-order test of
`StageMetrics` now expects the two new fields appended after `hari_naik`.

### Fix round 1: the lock summary shape (deliberate)

`StageMetrics` has 12 fields now (10 before). `stages._locked_metrics` requires the summary of a lock to
hold exactly the fields of `StageMetrics`, so a lock summary written by Phase 1 code (the 10-field shape,
e.g. `~/replay/mode_backtest/out/mode_A_BTC-USDT_lock.json`) is reported as "not the metrics of a training
run". This is harmless and deliberate: the lock also binds `code_sha256` over the package, which this
branch changes, so an old lock is refused on the code hash first; Mode A's one-shot test is spent; and
the real Mode A records (lock, start record, marker) still refuse any Mode A rerun. The code path is not
changed; `LockedMetricsShapeTests` in `tests/test_mode_backtest_lock.py` pins the behaviour.

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_lock.LockedMetricsShapeTests
Ran 2 tests ... OK
```

## Task 7

Lock and the train/test stages for mode L. `lock.check_lock(..., extra=...)` holds further bindings by
their key in the lock (every key must be there with that value; each mismatch is listed). The new
`research/mode_backtest/stages_live.py` has `train_live_stage(config_path, live_config_path, data_dir,
out_dir, pair, now)` and `test_live_stage(...)`, running `L_i_NAIK`, `L_ii_NAIK`, `S_i_TURUN`,
`S_ii_TURUN` and "Tidak trading" on the Phase 1 lock pattern. The lock `mode_L_BTC-USDT_lock.json` binds
`config_sha256`, `live_config_sha256` (through `extra`), `code_sha256`, `train_manifest_sha256` and
`python`; the start record is `mode_L_BTC-USDT_test_started.json` and the marker
`holdout_runs/mode_L_BTC-USDT_<lock sha>.json`, so Mode A's records and mode L's never refuse each
other. `test_live_stage` keeps Phase 1's order: configs and lock read, training files hashed, lock
checked (bindings, mode, pair, stage bounds, a summary of all four runs) → started/outputs/day-file
refusals (test days only `stat`-ed) → start record claimed → marker claimed → only then the test days
hashed and read → outputs written exclusively, with `loaded_manifest_sha256` and
`test_days_manifest_sha256`. L is measured on NAIK days and S on TURUN days, counted from the first
hour in which the first 3m stretch is known (controller ruling 2); every run carries entries per WIB
calendar day of its stage. Pass status and `live_status_line` are given for L(i) and L(ii) only
(ruling 3). `aktif: false`, a pair other than BTC-USDT and a live trigger other than Mode D's are refused
before anything is written.

Helpers of `stages.py` made public, no behaviour change: `Period`, `training_period`, `testing_period`,
`outputs_for`, `stamp`, `refuse_existing`, `Bindings`, `training_manifest_sha` (the brief's list) and,
beyond it, `Outputs`, `run_metrics` (gains an optional `hari_kalender`, so `stage_metrics` keeps its one
call site), `locked_metrics`, `refuse_after_the_test`, `refuse_before_the_marker`, `read_data`, `ReadData`
and `require_warm`. `report.live_no_trading_row(pair, tahap, hari_naik, hari_kalender)` gives the "Tidak
trading" line of the live table. `FileReads` moved from `tests/test_mode_backtest_lock.py` to
`tests/mode_backtest_stages.py`, shared by both stage test modules.

### RED

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_live_stages tests.test_mode_backtest_lock
ImportError: cannot import name 'stages_live' from 'research.mode_backtest'
ERROR: test_check_lock_holds_every_extra_binding_against_the_lock (tests.test_mode_backtest_lock.LockFileTests)
TypeError: check_lock() got an unexpected keyword argument 'extra'
Ran 46 tests in 75.753s
FAILED (errors=2)
```

### GREEN

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_lock tests.test_mode_backtest_structure
Ran 68 tests in 25.750s
OK            (after the renames, before stages_live existed)
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_live_stages tests.test_mode_backtest_report
Ran 46 tests in 4.207s
OK
```

```
$ PYTHONPATH=src .venv/bin/python -m coverage run -m unittest discover -s tests   (rcfile = the pyproject settings; Python 3.10 has no tomllib)
Ran 2435 tests in 135.417s
OK
$ PYTHONPATH=src .venv/bin/python -m coverage report
src/research/mode_backtest/lock.py           137   0  100%
src/research/mode_backtest/report.py          83   0  100%
src/research/mode_backtest/stages.py         279   0  100%
src/research/mode_backtest/stages_live.py    185   0  100%
TOTAL                                      15479 1390   91%
```

`compileall` and `git diff --check` are clean. Seven hand mutants of `stages_live.py` each fail the new tests: reading the test days before the claims,
dropping the `extra` binding, dropping the refusals before the marker, measuring S on NAIK days, not
claiming the start record, a status line for S, and ignoring the first-stretch start of the day count.

## Task 8: CLI for mode L

`train` and `test` take `--mode L` with a required `--live-config` and call
`stages_live.train_live_stage` / `test_live_stage`; `--mode A` keeps `stages.train_stage` /
`test_stage` unchanged. `--live-config` with `--mode A` and `--mode L` without it are usage errors
(exit 2, one `error:` line); any other mode, or mode L on another pair, exits 1.

### RED

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_cli
error: unrecognized arguments: --live-config ...   (argparse, exit 2)
Ran 30 tests in 0.313s
FAILED (failures=18)
  test_train_mode_l_dispatches_to_the_live_stage (x2), test_mode_l_without_live_config_is_a_usage_error (x2),
  test_live_config_with_mode_a_is_a_usage_error (x2), test_other_modes_exit_1 (x8),
  test_mode_l_on_another_pair_exits_1_and_writes_nothing (x2), test_a_lock_refusal_of_mode_l_exits_2,
  test_test_after_the_live_config_was_edited_exits_2_and_leaves_no_record
```

### GREEN

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_cli
Ran 30 tests in 0.504s
OK
$ PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
Ran 2443 tests in 58.187s
OK
```

The Task 7 carry-over is shown by `LiveLockEndToEndTests`: the real stages on the synthetic week in
a temporary folder (never `~/replay/mode_backtest`), `train --mode L` exits 0, a comment appended to
the live config, then `test --mode L` exits 2 with a `live_config_sha256` line on stdout, one
`error:` line, and no test result, start record or `holdout_runs` marker. `compileall`,
`git diff --check` and the secret-hygiene test are clean. The Phase 1 tests are unchanged and pass.

## Task 9: Rescore stage

`research.mode_backtest.rescore` reprices Mode A's (and the Phase 1 comparator's) trade logs with the
spec section 5 costs, keeping each trade's exit event, as a cost sensitivity ("sensitivitas biaya,
bukan uji"; never a pass or fail). The raw entry open is `harga_entry / (1 + side x old/100)` and the
raw exit `harga_exit / (1 - side x old/100)`, recovered from the fills for every kind of exit, so a
gapped stop is repriced from the bar's open and not from `harga_sl` (Review Focus 5). The new money
is `settle_live` on those numbers with the row's own reason and notional. The old and new results per
1M and per 1M without the top 3 use `metrics.stage_metrics`. Only the 23 Phase 1 columns are
accepted (a mode L log already carries the new costs). Reads only the CSVs given; writes
`rescore_<stamp>.json` and `.md` through `fileio` (both existence-checked before the first write).
CLI: `rescore --trade-log PATH ... --config --live-config --out-dir`.

### RED

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_rescore
ImportError: cannot import name 'rescore' from 'research.mode_backtest'
Ran 1 test in 0.000s
FAILED (errors=1)
```

### GREEN

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_rescore
Ran 19 tests in 0.109s
OK
$ PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
Ran 2462 tests in 56.728s
OK
```

Three hand mutants of `rescore.py` each fail the new tests: taking the exit from `harga_sl` for an SL
(3 failures), not undoing the old entry slippage (7), pricing every exit as an SL (3).

One existing test changed: `test_stage_metrics_has_one_call_site_and_it_passes_the_config_drop_top`
(`tests/test_mode_backtest_lock.py`) now skips `rescore.py`, which calls the same helper but gives
no verdict; the guard on the verdict path (`stages.py`, config `drop_top`) is unchanged.
`compileall` and `git diff --check` are clean; the structure test passes.

## Verification

Run on branch `feat/long-mirror-live-exit` with the local `.venv` (Python 3.10), `PYTHONPATH=src`,
before the commit of this task.

```
$ PYTHONPATH=src .venv/bin/python -m compileall -q src tests
(no output, exit 0)
```

Coverage: Python 3.10 cannot read `pyproject.toml` without tomli, so an rcfile outside the repo
carries the same settings (`source = src`, `branch = False`, omit `src/tradingview_relay.py`,
`fail_under = 80`, the same `exclude_lines`).

```
$ PYTHONPATH=src .venv/bin/python -m coverage run --rcfile=<rc> -m unittest discover -s tests
Ran 2462 tests in 136.197s
OK
$ PYTHONPATH=src .venv/bin/python -m coverage report --rcfile=<rc>
TOTAL                                      15641   1396    91%
(exit 0, fail_under 80)
```

```
$ git diff --check 1717d94..HEAD            -> no output, exit 0
$ git diff --check                          -> no output, exit 0
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_secret_hygiene
Ran 1 test in 0.023s
OK
$ git diff --quiet config/modes.yaml        -> unchanged
```

`config/modes.yaml` is unchanged, Mode A's golden log sha test passes, and no live module imports the
rule core (`tests/test_mode_backtest_structure.py`).

## Commit trail

Since `1717d94`:

```
457c5c5 feat: config file and loader for mode L
73caf23 feat: live exit levels for mode L
0f2e1d0 feat: 3m ATR percent and condition days for mode L
a4dffcc feat: live exit walk and per-kind fills
a7d8aa5 feat: event loop for mode L and comparator S
175acd7 feat: metrics, trade log and table for mode L
e34e683 test: pin that pre-mode-L lock summaries are refused
b82caa2 feat: train and one-shot test stages for mode L
0d59f25 feat: CLI for mode L
dcf5325 feat: rescore stage for the cost sensitivity
this commit: docs: mode L commands, notes and TDD evidence
```

## Deployment gate

Nothing is deployed. The live bot, `.env`, pm2 and the VPS are untouched, and nothing in `src/bot`
imports mode L. The mode L records do not exist yet: `train` and `test` have not run.

Task 11 runs `train --mode L` and then the one-shot `test --mode L`, after merge and only on the
user's go-ahead, with the Phase 1 operator notes:

- Run `verify-copy` right before `test`: `test` only `stat`s the test-period days, so their content is
  `verify-copy`'s job.
- Use the Python 3.10 `.venv` for both stages (the lock binds the Python minor version).
- Do not pull or merge `main` between `train` and `test`: the lock binds `code_sha256`, and any change
  to the package makes `test` exit 2.
- One `--out-dir`/`--data-dir` pair for both stages; never reuse a rehearsal's out-dir.
- Never rehearse against `~/replay/mode_backtest/data`: a rehearsal `test` reads the real test days
  and leaves a marker in `holdout_runs/` that refuses the real run. Rehearse on synthetic data only.
- Once `test` has claimed its start record, the shot is spent; never delete the start record or the
  marker.
