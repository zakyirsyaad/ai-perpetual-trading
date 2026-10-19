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

- A close that fails after the fill and is then found flat (`_manage_open_position_guarded`) does not set `_last_exit_ts`, so
  the 15-minute pause is skipped for that one exit.
- `build_regime_gate`'s refusal in `main.py` sits outside the `try/except`, so it is a raw traceback.
- `ENTRY_VOLATILITY_GUARD_MULT` alone does nothing when `ENTRY_REGIME_GATE=off`.
- `FORWARD_KILL_AFTER_POSITIONS` and `FORWARD_REVIEW_AT_POSITIONS` are not cross-validated.
- `compare-forward`'s JSON records the CSV's absolute local path; the `.md` has no win-rate column.
- `_has_value` in the builder treats `SUPABASE_URL= # note` as non-empty.

## Deployment gate

Not deployed. Task 9 starts only on the user's go-ahead and on merged `main`:

1. The account owner finishes the runbook's section 1 (key with trade only and IP whitelist, separate AI key, the
   `long_forward` schema run and exposed).
2. Merge needs a green CI (read the checks; never `--auto`). Mode L's `train` and `test` run on one code version, so either
   merge before `train`, or run both before merging, never between.
3. Build the `.env` with `--cycle-start` set to the forward start; run the names-only env check so that no forced key is
   exported to pm2; the first start in the foreground to read isolated margin, hedge mode, leverage, the fee log and the
   first regime verdict; then `pm2 start` and confirm `ai-perp-volume` is online and untouched.
4. Never restart either app with an open position (`104103` crash loop).
