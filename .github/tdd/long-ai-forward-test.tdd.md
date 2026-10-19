# LONG AI Forward Test (second instance) — TDD Evidence

A forward test of the live AI pipeline restricted to LONG entries while the hourly regime reads NAIK, on a second BingX VST
account. Spec: `docs/superpowers/specs/2026-10-18-long-ai-forward-test-design.md`. Plan:
`docs/superpowers/plans/2026-10-19-long-ai-forward-test.md`. Runbook: `docs/runbooks/long-forward-test.md`. Every task below
records its RED output and GREEN result; the RED outputs of tasks 2 and 7 are also saved next to the SDD ledger.

## User journey

The manager wants to know whether the AI entry, taken only long and only in an up-trend, has an edge going forward, since the
offline modes A and L are backtests and the AI path cannot be backtested. The account owner creates a second BingX account
and API key (trade only, IP whitelisted), a separate AI key, and the `long_forward` Supabase schema. The operator builds the
new `.env` from the live one without ever printing a value, starts `ai-perp-long` beside the untouched `ai-perp-volume`, and
every Monday runs the weekly report plus `compare-forward`, which backtests mode L over the same week next to the forward
result. The bot stops its own entries at 60 positions below -500 per 1M, and at 150 positions whatever the result, for the
manager to decide. Every new setting defaults to off, so the live SHORT bot is unchanged.

## Task 1: Regime and volatility gate

Files: `src/bot/core/regime_gate.py`, `src/bot/config.py`, `tests/test_regime_gate.py` (27 tests), the two structure tests.

### RED

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_regime_gate
ImportError: Failed to import test module: test_regime_gate
  File "tests/test_regime_gate.py", line 16, in <module>
    from bot.core.regime_gate import (
ModuleNotFoundError: No module named 'bot.core.regime_gate'
```

### GREEN

`tests.test_regime_gate tests.test_pine_gate_structure tests.test_mode_backtest_structure`: Ran 67 tests, OK. The gate equals
`HourlyFrame` (`kondisi`, `atr14_1h`, `atr_median`) at ten consecutive hour closes; the structure tests allow exactly
`bot/core/regime_gate.py` and still report a made-up `bot/core/other_gate.py`. Full suite at the commit: 2503 tests, OK.

## Task 2: Engine wiring (regime gate, pause after exit, decision payload)

Files: `src/bot/core/trade_engine.py`, `src/bot/core/regime_gate.py` (`build_regime_gate`), `src/main.py`,
`src/bot/ai/orchestration.py`, `src/bot/strategy/grok_strategy.py`, `src/bot/config.py`,
`tests/test_trade_engine_forward_gates.py` (26 tests). `main.py` refuses the gate with more than one symbol.

### RED

First run `ImportError: cannot import name 'build_regime_gate'`; after adding only that helper (saved in `task-2-red.txt`):

```
Ran 26 tests ... FAILED (errors=29)
  TradeEngine.__init__() got an unexpected keyword argument 'regime_gate'     x21
  AIOrchestrator has no attribute set_gate_context                            x4
  cooldown_minutes_after_exit validation                                      x4
```

### GREEN

`tests.test_trade_engine_forward_gates`: Ran 26 tests, OK. Full suite: 2529 tests, OK. The defaults test pins the old tick
path; with no regime gate the decision payload has no `regime` key.

## Task 3: Forward kill and review guard

Files: `src/bot/core/forward_guard.py`, `SupabaseTradeStore.cycle_trade_totals()`, `src/bot/core/trade_engine.py`,
`src/bot/config.py`, `tests/test_forward_guard.py` (19 tests).

### RED

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_forward_guard
ModuleNotFoundError: No module named 'bot.core.forward_guard'
```

### GREEN

Ran 19 tests, OK. Full suite: 2548 tests, OK. The guard runs right after the entries latch, logs ERROR on every blocked tick,
never sets `_entries_blocked_reason`, fails closed on a store error, and makes no store call with both limits at 0.

## Task 4: `long_forward` schema SQL

Files: `src/forward_schema.py`, `supabase/long_forward_schema.sql`, `tests/test_forward_schema.py` (12 tests).

### RED

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_forward_schema
ModuleNotFoundError: No module named 'forward_schema'      (FAILED errors=1)
```

### GREEN

Ran 12 tests, OK. The checked-in SQL equals the generator's output (drift test), no `public` is left, and the generator
fails closed on a base without `public.` references. The SQL was not executed against a real PostgreSQL; it is a pure
schema-name substitution of the file that already runs.

## Task 5: `.env` builder

Files: `src/build_forward_env.py`, `tests/test_build_forward_env.py` (26 tests after the fix).

### RED

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_build_forward_env
    import build_forward_env
ModuleNotFoundError: No module named 'build_forward_env'
Ran 1 test in 0.000s   FAILED (errors=1)
```

Review found that key matching was case-sensitive (Settings is not, and the later line wins), so `supabase_schema=public` or
`bingx_api_key=...` would survive. Three tests were added first:

```
Ran 26 tests ... FAILED (failures=2, errors=1)
```

### GREEN

Case-insensitive and quoted keys count as occurrences (first replaced, the rest dropped). Ran 26 tests, OK; with
`tests.test_env_cleaner tests.test_secret_hygiene`, 28 tests, OK. The target is created with mode 600 and `O_EXCL`, the
report holds key names only, and `Settings(_env_file=<built file>)` parses to the forward values.

## Task 6: Weekly report and trade export

Files: `src/report_forward_test.py`, `SupabaseTradeStore.closed_trades_since_cycle()`, `tests/test_report_forward_test.py`
(13 tests), one store test.

### RED

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_report_forward_test
ModuleNotFoundError: No module named 'report_forward_test'     (FAILED errors=1)
```

### GREEN

Ran 13 tests, OK. Full suite: 2600 tests, OK. The window is WIB `[from, to)` by `closed_at`; AI exits are the
`strategy_exit:<reason>` prefix.

## Task 7: `compare-forward` stage

Files: `src/research/mode_backtest/forward_compare.py`, `src/bot/core/forward_summary.py` (pure maths shared with the report),
`src/research/mode_backtest/cli.py`, `stages_live.py` (`check_mode_l`), `tests/test_mode_backtest_forward_compare.py`
(14 tests), `tests/test_forward_summary.py` (9 tests).

### RED (saved in `task-7-red.txt`)

```
ImportError: Failed to import test module: test_mode_backtest_forward_compare
ImportError: cannot import name 'forward_summary' from 'bot.core' (.../src/bot/core/__init__.py)
ImportError: Failed to import test module: test_forward_summary
Ran 2 tests in 0.000s   FAILED (errors=2)
```

### GREEN

Both modules: Ran 23 tests, OK; `discover -p 'test_mode*.py'`: 899 tests, OK. The one-shot protection is pinned: every refusal
opens nothing but `config/modes.yaml`, with the day files booby-trapped; no lock, start record or marker is read or written;
outputs are never overwritten.

Decisions recorded in the SDD ledger: R1 (Task 2 builds every gate except the guard, Task 3 inserts it), R2 (the gate needs
exactly one symbol), R3 (the native 1h/4h warm-up bars before 2026-10-14 are indicator history only), R4 (a `--data-dir` with
`holdout_runs/` is refused).

## Task 8: Runbook, docs, verification

Files: `docs/runbooks/long-forward-test.md`, `CLAUDE.md` (the commands, a "LONG AI forward test" section, the second-instance
exception, the import exception), `.env.example` (the seven keys, commented, off defaults), the stale texts: spec 4.2 "735" ->
about 831 closed 1h bars (112 ATR warm-up + 720), and the `bot/strategy/modes/__init__.py` docstring now names
`bot/core/regime_gate.py`. The docstring is part of the rule core, so it moves mode L's code sha; mode L's `train` has not run, so
no lock is affected.

pm2 command: the plan's `pm2 start src/main.py --name ai-perp-long --interpreter .venv/bin/python --cwd ...` is kept with
three changes. `PYTHONPATH=src` is added (the plan's form has none, `src/main.py` imports `bot` and has no `sys.path` fix, and
`ecosystem.config.js` sets it for `ai-perp-volume`); the interpreter is an absolute path; and `--restart-delay 10000` mirrors
that file's `restart_delay`.

## Final review fix wave

The whole-branch review (`638bd24..8943951`) found one invariant resting on an inherited setting and a handful of cheap
gaps. Findings, in the review's numbering:

1. **LONG-only depended on an inherited `ENTRY_STRETCH_GATE_MODE=enforce`.** `ENTRY_GATE_DIRECTION` is read only inside
   `_stretch_gate_verdict`, which returns `allow` when the mode is `off`, and a direction block is enforced only when the mode
   is `enforce`; `STRATEGY_MODE=volume` never reaches the orchestrator at all. Fixed in three places: (a)
   `Settings.validate_cross_field_settings` refuses `ENTRY_REGIME_GATE` on unless `ENTRY_GATE_DIRECTION=long`,
   `ENTRY_STRETCH_GATE_MODE=enforce` and `STRATEGY_MODE` is `grok`/`ai_volume` (the gate-off live Settings is untouched, a test
   builds 27 live-style combinations); (b) the builder forces `ENTRY_STRETCH_GATE_MODE=enforce` and `STRATEGY_MODE=ai_volume`;
   (c) `report_forward_test` and `compare-forward` count rows that are not LONG (`ForwardSummary.non_long`, shared in
   `bot/core/forward_summary.py`) and print or write `PERINGATAN: N posisi bukan LONG ...`.
2. **The runbook's names-only check could print a value** (a continuation line of a multi-line quoted value has no `=`).
   Now `grep -E` keeps only `NAME=` lines before the `sed`; checked on a temp file with fake values (a multi-line quoted
   value, a bare token, an `export` line, spaces around `=`): the old pipeline printed `bare-continuation-token-...` and
   `final line"`, the new one printed the five key names only.
3. The compare `.md` states the edge asymmetry itself (no entry in the window's last `batas_waktu_menit` = 60 minutes; the
   forward side counts by `closed_at`).
4. `report_forward_test` prints `VOLUME_CYCLE_STARTED_AT` (or `tidak diatur`) and warns when `--from` precedes it.
5. Runbook: the forward bot inherits `COINMARKETCAP_API_KEY`/`DUNE_API_KEY`; an explicit keep/separate/blank decision.
6. Runbook and CLAUDE.md: the code is pinned by a tag, `long-forward-v1`, which the operator creates on merged `main` (the
   runbook gives the command; no tag was created or pushed here); the clone checks it out.
7. CLAUDE.md "The tick loop and its gates" lists the real order (ten gates) so it agrees with the forward section.
8. `test_every_new_setting_defaults_off` also asserts `forward_kill_after_positions == 0`, `forward_review_at_positions == 0`
   and `entry_volatility_guard_mult == 0`.
9. The tests that parse the built file into `Settings` run under `mock.patch.dict(os.environ, {}, clear=True)`.

### RED

Tests for 1a, 1b, 1c, 3 and 4 were written first (`.superpowers/sdd/2026-10-19-long-ai-forward-test/final-fix-red.txt`):

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_regime_gate.ConfigTests tests.test_build_forward_env \
    tests.test_trade_engine_forward_gates tests.test_forward_summary tests.test_report_forward_test \
    tests.test_mode_backtest_forward_compare
ERROR: test_the_overrides_carry_what_the_long_only_invariant_needs   (KeyError ENTRY_STRETCH_GATE_MODE)
ERROR: test_counts_rows_of_either_shape_inside_the_window / test_all_long_has_none / test_the_warning_names_the_count (AttributeError: non_long / non_long_warning)
FAIL: test_the_gate_refuses_to_boot_without_a_long_only_direction (direction='any'), (direction='short')
FAIL: test_the_gate_refuses_to_boot_unless_the_stretch_gate_enforces (mode='off'), (mode='shadow')
FAIL: test_the_gate_refuses_to_boot_with_the_deterministic_strategy
FAIL: test_a_live_env_with_the_wrong_mode_is_forced_to_the_long_only_one, test_built_file_parses_into_the_forward_settings
FAIL: test_a_short_row_is_flagged_in_the_summary_and_still_exported, test_prints_the_cycle_start_and_warns_..., test_no_cycle_warning_..., test_a_settings_without_a_cycle_start_prints_tidak_diatur
FAIL: test_the_report_states_the_edge_asymmetry, test_a_short_forward_row_in_the_window_is_flagged_in_the_report_and_the_result
Ran 110 tests ... FAILED (failures=13, errors=4)
```

Item 8 only strengthens assertions that already hold (defaults are 0), so it has no RED. Item 9's flakiness was shown
instead: with `SUPABASE_SCHEMA=public` exported and the new tests stashed, `tests.test_build_forward_env` fails 2 tests; with
the fix it passes under `SUPABASE_SCHEMA=public ENTRY_GATE_DIRECTION=short STRATEGY_MODE=volume`.

### GREEN

Same command: Ran 132 tests, OK. Existing fixtures that turned the gate on (`tests/test_regime_gate.py`,
`tests/test_trade_engine_forward_gates.py`, the parsed-Settings test) now carry the three companions; the rule was not bent.

### Verification

Full suite under coverage (rcfile with pyproject's settings; Python 3.10 cannot read pyproject): 2640 tests, OK, 91 % line
coverage (gate 80 %); `compileall -q src tests` clean; `git diff --check` clean; `tests.test_secret_hygiene` passes.
Mode L's code sha changed again (`bot/core/forward_summary.py`, `forward_compare.py`); mode L's `train` has not run, so
nothing is invalidated.

## Verification

```text
compileall -q src tests                                   : exit 0
Full suite under coverage (Python 3.10, rcfile in the SDD folder with the pyproject.toml settings):
  Ran 2622 tests in 133.506s                              : OK
coverage report --rcfile ... (fail_under 80)              : TOTAL 16417 statements, 1417 missed, 91%, exit 0
git diff --check                                          : clean
tests.test_secret_hygiene                                 : Ran 1 test, OK
```

Python 3.10 cannot read `pyproject.toml` without tomli, so the rcfile carries the same `source`, `omit`, `exclude_lines` and
`fail_under = 80`. `pip check` was not run (`.venv` has no pip module); no dependency was added. No key-like string was added
to any tracked file, and no `.env` was read.

## Commit trail

- `9570e7f` — `feat: regime and volatility gate for the LONG forward bot (off by default)`
- `1d51ebd` — `feat: wire the regime gate and the pause after exit into the engine`
- `ead46aa` — `feat: forward kill and review guard (off by default)`
- `8b60c91` — `feat: long_forward schema SQL generated from schema.sql`
- `9350220` — `feat: build the forward bot's .env without printing secrets`
- `3de046c` — `fix: match forward .env keys case-insensitively`
- `3182c4e` — `feat: weekly forward-test report and trade export`
- `ed407a5` — `feat: compare-forward stage for the weekly mode L comparison`
- Task 8 — `docs: runbook and notes for the LONG AI forward test`

## Deferred notes (from the task reviews, none blocking)

- `build_regime_gate`'s refusal in `main.py` sits outside the `try/except`, so it is a raw traceback.
- `ENTRY_VOLATILITY_GUARD_MULT` alone does nothing when `ENTRY_REGIME_GATE=off`.
- `FORWARD_KILL_AFTER_POSITIONS` and `FORWARD_REVIEW_AT_POSITIONS` are not cross-validated.
- `compare-forward`'s JSON records the CSV's absolute local path.
- `_has_value` in the builder treats `SUPABASE_URL= # note` as non-empty.

## PR #42 review fixes

Ten review findings, each with a test written first (items 3 and 4 are docs). RED output is the failing run before the
fix; GREEN is the same command after. The live SHORT bot (all new settings off) is unchanged: the only change on its
path is that `AIOrchestrator` sets `_gate_context = None` in `__init__` (it was a class default) and the new side check
in `_build_entry_candidate` is behind `entry_regime_gate != "off"`.

| # | Change | RED | GREEN |
|---|--------|-----|-------|
| 1 | `RegimeGate._evaluate`: newest 1h candle older than one hour plus `STALE_SLACK_MS` (5 min) blocks as `data_error` ("stale candles: last 1h close <iso>") | `python -m unittest tests.test_regime_gate`: `test_candles_older_than_an_hour_plus_slack_block_as_stale` (no WARNING logged), `test_candles_within_the_hour_and_slack_are_not_stale` (`'naik' != 'data_error'` at one ms past the slack) | 36 tests OK |
| 2 | `_manage_open_position_guarded`, flat branch, sets `_last_exit_ts` | `tests.test_trade_engine_forward_gates.PauseAfterFailedManagementTests`: `AssertionError: unexpectedly None` | 27 tests OK |
| 3 | Runbook section 2 step 9: supervisor cron for `ai-perp-long` (cwd, `--app`, `--log`, five-minute cadence, a `--dry-run` first); CLAUDE.md one clause | docs | docs |
| 4 | Runbook section 2 step 4: names-only check via `dotenv_values` | docs; checked on a temp file with fake values (multi-line quoted value ending in `=`, a continuation line with `==`, bare token, `export`, spaces around `=`, comment line): printed names only, the bare token and every value were absent | same |
| 5 | `_build_entry_candidate`: with `entry_regime_gate != "off"` any side but `entry_gate_direction` is rejected before the stretch gate, with its own log line | `tests.test_ai_orchestration.RegimeGateSideDefenceTests`: OPEN_SHORT returned an `EntryCandidate` with gate on, direction long and stretch mode off | 3 tests OK (gate off: a SHORT still passes) |
| 6 | `data_error` verdict kept `DATA_ERROR_RETRY_MS` (60 s) then refetched | `test_a_data_error_is_cached_for_the_retry_window_then_refetched`: `AssertionError: 2 != 1` | 36 tests OK |
| 7 | Forward guard moved right after the window gate (entries latch, window, forward guard, risk, ...); order updated in CLAUDE.md (both lists), the runbook and the plan | `tests.test_forward_guard.EngineGuardTests.test_the_guard_runs_after_the_window_gate`: guard logged ERROR with the window closed | 19 tests OK |
| 8 | `_gate_context` set in `__init__`, class default removed; `object.__new__` test helpers set it | `test_the_gate_context_is_set_by_init_and_not_by_a_class_default`: `'_gate_context' unexpectedly found` in the class; removing the default alone broke 14 `test_orchestration_pipeline` tests (`AttributeError`) until the helpers set it | OK |
| 9 | `RegimeGate._detail` removed: `_evaluate` returns `(verdict, detail)`, `_note` takes the detail | covered by the item 1 and 6 tests (log text unchanged) | OK |
| 10 | `compare-forward` table and JSON: `untung (pnl > 0)` (share of positions with pnl > 0) for both sides; mode L's `menang_pct` is not in the table | `tests.test_mode_backtest_forward_compare`: `KeyError: 'untung_pct'` | 16 tests OK |

Notes. Item 7's old test (`test_the_guard_runs_before_the_window_gate`) used a window string the parser rejects and passed
only because the guard returned first; the new test stubs the window as closed. Item 1 and 6 needed the gate's clock to
follow the fake venue in `tests/test_regime_gate.py` (`make_gate` passes `clock_ms=lambda: client.now_ms`); the old tests
ran on the real clock against 2023 candles, which the staleness check would rightly block. Item 10 changes mode L's
code sha, which is fine because its `train` has not run.

Verification after the ten fixes: full suite 2649 tests OK (was 2641; +8 new), coverage 91 % total with the temp-rcfile
approach (`regime_gate.py` 99 %, `forward_compare.py` 100 %, `orchestration.py` 95 %, `trade_engine.py` 87 %), `compileall`
clean, `git diff --check` clean, `tests.test_secret_hygiene` OK.

## PR #42 second review fixes

Six code items, each with a test written first (RED = the failing run before the fix). With every new setting off the live
SHORT bot is unchanged: the only path it touches is `_stretch_gate_verdict`, which for `entry_regime_gate == "off"` returns
exactly what it did (an added test pins gate off, stretch mode off, OPEN_SHORT: verdict `allow`, mode `off`).

| # | Change | RED | GREEN |
|---|--------|-----|-------|
| 1 | `FORWARD_OVERRIDES["SYMBOLS"] = "BTC/USDT"` (Settings stores the tuple `("BTC/USDT",)`) | `tests.test_build_forward_env` with the source listing `SYMBOLS=BTC/USDT,ETH/USDT`: `Tuples differ: ('BTC/USDT', 'ETH/USDT') != ('BTC/USDT',)` | 28 tests OK |
| 2 | `ForwardSummary.untung_pct` and mode L's `untung_pct` are `None` without positions; every renderer prints a dash, JSON carries null, a number keeps its `%` | `0.0 is not None` (forward summary, report and compare, both sides), `'0.0%' != '-'` | 37 tests OK |
| 3 | `main.py`: `build_regime_gate`'s `ValueError` logs one error line, closes the client and exits 1 (no traceback) | `tests.test_main_composition.RegimeGateRefusalTests`: raw `ValueError: ENTRY_REGIME_GATE=naik needs exactly one symbol in SYMBOLS, got 2` out of `run()` | 12 tests OK |
| 4 | The LONG-only defence moved into `_stretch_gate_verdict`: gate on and a decision on the other side returns the `direction` block with mode `enforce`, whatever the stretch mode; the separate block in `_build_entry_candidate` is gone | `tests.test_ai_orchestration.RegimeGateSideDefenceTests`: `'allow' != 'block'` (verdict) and `KeyError: 'reason'` (the stored `gate` of the decision) | OK (4 tests; the enforce path drops the candidate) |
| 5 | `entry_regime_gate` / `entry_gate_direction` are read by plain attribute access in `orchestration.py` and `regime_gate.py`; the fake settings in `test_ai_orchestration`, `test_orchestration_pipeline` and `test_main_composition` carry both fields | the first full run after the plain access: 28 `AttributeError: ... no attribute 'entry_gate_direction'` errors until the fakes were updated | full suite OK |
| 6 | The engine no longer logs the regime gate (`RegimeGate._note` already logs each change with the numbers); the pause-after-exit note stays | `test_the_engine_leaves_the_regime_log_to_the_gate`: two engine lines (`Regime gate blocks entries ...`, `Regime gate allows entries ...`) where none are expected | 28 tests OK |

Notes. Item 4 keeps one behaviour on purpose: gate on, the configured side, stretch mode off still skips the stretch check
(only the other side is promoted to `enforce`), so a misconfigured stretch mode cannot start rejecting LONGs either.
Item 7 needs no change: `_publish_volume` returns early when telemetry is off and the forward bot forces
`TELEMETRY_ENABLED=false`, so it already reads the cycle rows once per tick.

Verification: full suite 2654 tests OK (was 2649), coverage 91 % total with the temp-rcfile approach (`orchestration.py`
95 %, `main.py` 100 %, `forward_summary.py` 100 %, `forward_compare.py` 100 %, `regime_gate.py` 99 %, `trade_engine.py` 87 %),
`compileall` clean, `git diff --check` clean, `tests.test_secret_hygiene` OK.

## Deployment gate

Not deployed. Task 9 starts only on the user's go-ahead and on merged `main`:

1. The account owner finishes the runbook's section 1 (key with trade only and IP whitelist, separate AI key, the
   `long_forward` schema run and exposed).
2. Merge needs a green CI (read the checks; never `--auto`). Mode L's `train` and `test` run on one code version, so either
   merge before `train`, or run both before merging, never between.
3. Build the `.env` with `--cycle-start` set to the forward start; run the names-only env check so that no forced key is
   exported to pm2; the first start in the foreground, only between Saturday 00:00 and Monday 07:00 WIB (the entry window is
   closed, so no order can happen), to read isolated margin, hedge mode, leverage, the fee log and the `Entry window closed`
   line (the regime gate sits after the window gate, so its first verdict is read in the pm2 log on Monday after 07:00);
   confirm flat on the venue, stop it, then `pm2 start` in the same closed window and confirm `ai-perp-volume` is online
   and untouched.
4. Never restart either app with an open position (`104103` crash loop).

## PR #42 third review fixes

1. `CLAUDE.md` no longer asserts what the live `.env` lists (nobody read it); `SYMBOLS` is forced because the live file *may* list several.
2. With the regime gate on, `_stretch_gate_verdict` keeps `mode` as configured and records `forced_by: regime_gate` on the direction block; `_build_entry_candidate` enforces a block that is either `mode == enforce` or forced by the regime gate. RED: `RegimeGateSideDefenceTests` failed (`'enforce' != 'off'`, `KeyError: 'forced_by'`); GREEN: `tests.test_ai_orchestration` 42 OK.
3. `report_forward_test` prints every figure with `bot.core.forward_summary.format_number`/`format_percent`, a copy of the compare `.md` notation pinned to `research.mode_backtest.report` by a test (a script may not import `research`) (Indonesian decimals, `–` when missing). RED: `test_figures_are_printed_as_the_compare_report_prints_them` and the dash test failed; GREEN: `tests.test_report_forward_test` 20 OK.

Note: the first push of these fixes (`a39608b`) imported `research` from `src/report_forward_test.py` and failed `test_pine_gate_structure.IsolationTests.test_no_bot_module_or_main_imports_research` (1 failure in 2655); the push went out because the commit command read the exit status of `tail`, not of the suite. The follow-up moves the formatter into `bot/core/forward_summary.py`, pinned to the research one by `tests/test_forward_summary.FormatTests`.
