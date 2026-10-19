# LONG AI forward test on a second VST account — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run a second instance of the live bot, LONG-only and only in NAIK, on a second VST account with its own Supabase schema, under the document's general limits and with a kill/review guard. Compare it every week with mode L backtested on the same dates.

**Architecture:**
- **Live bot (`src/bot/`):** four gates, all default off, so the live SHORT bot is unchanged. They are a regime and volatility gate, a pause after any exit, and a forward kill/review guard. A new `bot/core/regime_gate.py` computes the selector inputs from closed VST 1h/4h candles with `pine_indicators` and labels them with the rule core's `pilih_mode`. It is the only live module allowed to import either.
- **Deployment:** a SQL generator gives the `long_forward` schema, and a `.env` builder derives the second bot's settings from the live file without printing a value.
- **Weekly loop:** `src/report_forward_test.py` exports the week's trades (read-only), and `research.mode_backtest compare-forward` backtests mode L on the same dates.

**Tech Stack:** Python ≥ 3.10 stdlib, the existing bot (`pydantic-settings`, `httpx`), stdlib `unittest`, the Phase 1 / mode L harness.

**Spec:** `docs/superpowers/specs/2026-10-18-long-ai-forward-test-design.md` (decisions §2, settings §3, new code §4, isolation §5, weekly comparison §6, decided points §9). Mode L: `docs/superpowers/specs/2026-10-18-long-mirror-live-exit-design.md`.

**Refinement of spec §3, recorded here:** the spec sets `VOLUME_DAILY_TARGET_USDT` to 0 to turn off volume pacing. Instead keep the live value and set `VOLUME_STOP_AT_TARGET_ENABLED=false` (the pause) and `VOLUME_ATTEMPT_TARGET_PER_DAY=6` (the cap `_daily_entry_limit` reads while a target is set). The intent is the same, no volume pacing and at most 6 entries a day. This way nothing that reads the target (volume profile, pacing snapshot) changes.

## Global Constraints

- **The live SHORT bot is unchanged.** Every new setting defaults to off (`ENTRY_REGIME_GATE=off`, `ENTRY_VOLATILITY_GUARD_MULT=0`, `COOLDOWN_MINUTES_AFTER_EXIT=0`, `FORWARD_KILL_AFTER_POSITIONS=0`, `FORWARD_REVIEW_AT_POSITIONS=0`). A defaults test pins the old tick path. The live app `ai-perp-volume`, its directory, `.env` and pm2 entry are never touched.
- **Secrets.** Never read or print a `.env` value. The builder copies values and prints key names only. The account owner types `BINGX_API_KEY`, `BINGX_SECRET_KEY` and `XAI_API_KEY` (a separate AI key, spec §9.3) into the new `.env` by hand. No key-like string goes in any tracked file.
- **Imports.**
  - `bot` never imports `research`.
  - The only live module that may import `bot.strategy.pine_indicators` or `bot/strategy/modes/` is `bot/core/regime_gate.py`. The two structure tests change to allow exactly that file.
- **Regime.** Same definitions as the backtest (`HourlyFrame`):
  - SMA20/SMA50 1h, ATR14 1h (Wilder, `pine_indicators.atr`), EMA50 4h, the median of the last 720 closed-hour ATR14 values;
  - `pilih_mode` with `sideways_jarak_kali_atr = 0.2` from `config/modes.yaml` `pemilih_mode`;
  - only closed candles (`get_klines` already drops the forming one);
  - NAIK is the only condition that lets an entry through.
- **Volatility guard.** Skip when ATR14 1h > `ENTRY_VOLATILITY_GUARD_MULT` × median, and while the median is unknown (fewer than 720 values).
- **Forward guard.**
  - Result per 1M = 1,000,000 × Σ`pnl_usdt` / Σ((entry + exit) × quantity) over the schema's closed trades since `VOLUME_CYCLE_STARTED_AT`.
  - From `FORWARD_KILL_AFTER_POSITIONS` (60) closed positions on, every tick latches entries off when the result is below `FORWARD_KILL_BELOW_PER_1M` (−500).
  - At `FORWARD_REVIEW_AT_POSITIONS` (150) it latches regardless.
  - It is recomputed from Supabase every tick (stateless across restarts), and open positions keep being managed.
- **Second bot.** Directory `/home/ubuntu/ai-perp-long`, pm2 app `ai-perp-long`, Supabase schema `long_forward`, telemetry off. The rule "never restart with an open position" applies to it too.
- **One-shot protection.** `compare-forward` never reads a day before `uji.ujian_selesai` (2026-10-14 WIB): mode L's test period must stay unread until its one-shot `test`.
- **Repo.** Every command uses `PYTHONPATH=src`. Tests are stdlib unittest. Coverage stays ≥ 80 %. TDD evidence goes to `.github/tdd/long-ai-forward-test.tdd.md`. Reports to the manager are in Indonesian; code and docs in English.

## Review Focus

1. **Too few candles from the venue** (a new account, or a venue limit below the request): the gate must block with a logged reason, never let an entry through on a `None` value. → Task 1 `test_short_history_blocks_with_a_reason`.
2. **Hour boundary:** the label at a tick must equal the backtest's `HourlyFrame.context_at` on the same closed candles. → Task 1 `test_labels_match_the_backtest_hourly_frame`.
3. **Restart:** the pause after an exit and the forward guard must hold after a pm2 restart. → Task 2 `test_pause_after_exit_is_seeded_from_the_last_closed_trade`, Task 3 `test_guard_is_recomputed_after_a_restart`.
4. **Schema isolation:** a built `.env` that still points at `public` would mix the forward trades into the live bot's guards. → Task 5 `test_target_always_has_the_forward_schema`.
5. **Holdout leak:** a weekly comparison over a date inside mode L's test period would spend its one-shot data. → Task 7 `test_refuses_any_day_before_the_end_of_the_test_period`.

---

### Task 1: Regime and volatility gate module

**Files:**
- Create: `src/bot/core/regime_gate.py`
- Modify: `src/bot/config.py`
  - `entry_regime_gate: str` (alias `ENTRY_REGIME_GATE`, `off` | `naik`, validated like `entry_gate_direction`);
  - `entry_volatility_guard_mult: float = 0.0` (`ENTRY_VOLATILITY_GUARD_MULT`, ≥ 0);
  - `regime_sideways_atr_mult: float = 0.2` (`REGIME_SIDEWAYS_ATR_MULT`, > 0).
- Modify: `tests/test_pine_gate_structure.py` and `tests/test_mode_backtest_structure.py`, to allow exactly `bot/core/regime_gate.py`.
- Test: `tests/test_regime_gate.py`

**Interfaces:**
- Produces:
  - `RegimeVerdict(kondisi: str, allowed: bool, reason: str, atr14_1h: Optional[float], atr_median: Optional[float], hour_close_ms: Optional[int])` (frozen).
  - `class RegimeGate`:
    - `__init__(self, client, symbol: str, mode: str, volatility_mult: float, sideways_mult: float, logger)`;
    - `async def verdict(self) -> RegimeVerdict`, which fetches 1h (limit 1000) and 4h (limit 500) klines once per closed hour and caches the verdict until the next 1h close;
    - `def as_payload(self) -> dict`, the last verdict for the decision payload.
  - `reason` is one of `off`, `naik`, `not_naik:<KONDISI>`, `volatility`, `volatility_unknown` or `history_short`.
  - Mode `off` returns `allowed=True, reason="off"` with no network call.

- [ ] **Step 1: Write the failing tests** (a fake client serving fixed 1h/4h rows):
  - `test_off_makes_no_call_and_allows`
  - `test_naik_allows_and_other_conditions_block`, one case each for SIDEWAYS, TURUN and TIDAK_ENTRY.
  - `test_labels_match_the_backtest_hourly_frame`: build `research.mode_backtest.frames.HourlyFrame` from the same candles (tests may import research). At ten hour closes, assert the same `kondisi`, `atr14_1h` and `atr_median`.
  - `test_short_history_blocks_with_a_reason`: 200 1h rows give `allowed=False`, reason `history_short` or `volatility_unknown`, and a WARNING logged once.
  - `test_volatility_guard_blocks_above_twice_the_median_and_off_at_zero`
  - `test_verdict_is_cached_until_the_next_hour_close`: two calls in one hour make one fetch per interval.
  - `test_config_refuses_an_unknown_regime_gate_value`
  - `test_default_sideways_multiple_matches_modes_yaml`: `Settings().regime_sideways_atr_mult == load_modes_config(config/modes.yaml).pemilih_mode.sideways_jarak_kali_atr`.
  - The structure tests: a second, made-up live module importing `pine_indicators` is still refused.
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_regime_gate tests.test_pine_gate_structure tests.test_mode_backtest_structure -v`. Expect an `ImportError`.
- [ ] **Step 3: Implement.** Reuse `pine_indicators.sma/atr/rolling_median/ema` and `modes.regime.pilih_mode` exactly as `HourlyFrame` does; copy no maths. The 1h and 4h series are the venue's closed candles, oldest first.
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: regime and volatility gate for the LONG forward bot (off by default)"`.

### Task 2: Engine wiring: regime gate, pause after exit, decision payload

**Files:**
- Modify:
  - `src/bot/core/trade_engine.py`;
  - `src/bot/config.py`: `cooldown_minutes_after_exit: int = 0` (`COOLDOWN_MINUTES_AFTER_EXIT`, ≥ 0);
  - `src/bot/strategy/grok_strategy.py`: `GrokStrategy.set_gate_context(context: dict) -> None`;
  - `src/bot/ai/orchestration.py`: merge the context into `payload_json["gate"]["regime"]` in `_record_ai_decision`;
  - `src/main.py`: build `RegimeGate` from settings for the single symbol and pass it to `TradeEngine(..., regime_gate=...)`.
- Test: `tests/test_trade_engine_forward_gates.py`

**Interfaces:**
- Consumes: Task 1 `RegimeGate`, `RegimeVerdict`.
- Produces:
  - `TradeEngine.__init__(..., regime_gate: RegimeGate | None = None)`.
  - `_tick` order: entries latch → window → forward guard (Task 3) → risk → stop cooldown → entry cooldown → **pause after exit** → volume pause → daily cap → **regime gate** → strategy.
  - Each blocking gate logs once per state change, the same way the window gate does.

- [ ] **Step 1: Write the failing tests:**
  - `test_defaults_leave_the_tick_path_unchanged`: no new setting set gives the same gate calls and strategy call as today, and the regime gate is never called.
  - `test_regime_gate_blocks_before_the_strategy_and_costs_no_ai_call`
  - `test_pause_after_exit_blocks_for_15_minutes_after_any_close`: stop_loss, neutral_exit and strategy_exit closes alike.
  - `test_pause_after_exit_is_seeded_from_the_last_closed_trade`: startup reads `recent_closed_trades(1)`.
  - `test_ai_decision_payload_carries_the_regime_verdict`
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_trade_engine_forward_gates -v`. Expect failures.
- [ ] **Step 3: Implement.** `_last_exit_ts` is set where a close is recorded (beside `_last_stop_loss_ts`). The regime gate's verdict goes through `set_gate_context` before `next_trade_plan`.
- [ ] **Step 4: Run** the full suite. Expect OK; the existing engine tests must pass unchanged.
- [ ] **Step 5: Commit** `git commit -m "feat: wire the regime gate and the pause after exit into the engine"`.

### Task 3: Forward kill and review guard

**Files:**
- Modify:
  - `src/bot/config.py`: `forward_kill_after_positions: int = 0` (`FORWARD_KILL_AFTER_POSITIONS`), `forward_kill_below_per_1m: float = -500.0` (`FORWARD_KILL_BELOW_PER_1M`), `forward_review_at_positions: int = 0` (`FORWARD_REVIEW_AT_POSITIONS`);
  - `src/bot/storage/supabase_trade_store.py`: `async def cycle_trade_totals(self) -> CycleTotals`;
  - `src/bot/core/trade_engine.py`.
- Create: `src/bot/core/forward_guard.py`
- Test: `tests/test_forward_guard.py`

**Interfaces:**
- Produces:
  - `CycleTotals(count: int, pnl_usdt: float, volume_usdt: float)`. It is read with `_all_closed_trade_rows("pnl_usdt,entry_price,exit_price,quantity")`, so it is cycle-scoped like `realized_pnl_since_cycle`, with volume as in `_sum_trade_volume`.
  - `forward_guard_reason(totals: CycleTotals, kill_after: int, kill_below: float, review_at: int) -> Optional[str]`:
    - `None` when off or fine;
    - `"forward_kill: <n> positions at <x> per 1M (limit <y>)"`;
    - `"forward_review: <n> positions reached, the manager decides"`.
  - Engine: checked right after the entries latch; logs ERROR and returns.

- [ ] **Step 1: Write the failing tests:**
  - `test_off_by_default`
  - `test_nothing_before_60_positions_even_if_bad`
  - `test_kill_at_and_after_60_when_below_minus_500`: 60 positions at −501 latch; 61 positions at −499 do not.
  - `test_review_latches_at_150_regardless_of_result`
  - `test_result_per_1m_is_pooled`: Σpnl / Σvolume, not a mean of ratios.
  - `test_guard_is_recomputed_after_a_restart`: a fresh engine against the same store blocks at once.
  - `test_open_position_is_still_managed_while_latched`
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_forward_guard -v`. Expect `ImportError`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite. Expect OK.
- [ ] **Step 5: Commit** `git commit -m "feat: forward kill and review guard (off by default)"`.

### Task 4: `long_forward` schema SQL

**Files:**
- Create:
  - `src/forward_schema.py`: `schema_sql(schema: str, base_sql: str) -> str`, plus a CLI that prints it for `--schema`;
  - `supabase/long_forward_schema.sql`, the generated output, checked in.
- Test: `tests/test_forward_schema.py`

**Interfaces:**
- Produces `schema_sql`:
  - It turns every `public.` of `supabase/schema.sql` into `<schema>.`.
  - It prepends `create schema if not exists <schema>;`.
  - It appends `grant usage on schema <schema> to service_role;`, `grant all on all tables in schema <schema> to service_role;` and `grant all on all sequences in schema <schema> to service_role;`.
  - It refuses a schema name that is not `[a-z_][a-z0-9_]*` or that is `public`.

- [ ] **Step 1: Write the failing tests:**
  - `test_checked_in_file_equals_the_generator_output`, which catches drift from `schema.sql`.
  - `test_no_public_reference_left`
  - `test_grants_and_create_schema_present`
  - `test_bad_schema_names_refused`
- [ ] **Step 2: Run** the tests. Expect `ImportError`.
- [ ] **Step 3: Implement**, then generate the file with `PYTHONPATH=src .venv/bin/python src/forward_schema.py --schema long_forward > supabase/long_forward_schema.sql`.
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: long_forward schema SQL generated from schema.sql"`.

### Task 5: `.env` builder for the second bot

**Files:**
- Create: `src/build_forward_env.py`
- Test: `tests/test_build_forward_env.py`

**Interfaces:**
- Produces `build_forward_env(source: Path, target: Path, cycle_start: str) -> BuildReport(set_keys: tuple, blanked_keys: tuple)`.
  - It copies the source line by line and replaces or appends `FORWARD_OVERRIDES`:
    - `SUPABASE_SCHEMA=long_forward`, `ENTRY_GATE_DIRECTION=long`, `ENTRY_REGIME_GATE=naik`, `ENTRY_VOLATILITY_GUARD_MULT=2.0`;
    - `COOLDOWN_MINUTES_AFTER_EXIT=15`, `COOLDOWN_MINUTES_AFTER_ENTRY=0`, `COOLDOWN_MINUTES_AFTER_STOP=0`;
    - `TRADE_TARGET_PER_DAY=6`, `VOLUME_ATTEMPT_TARGET_PER_DAY=6`, `VOLUME_STOP_AT_TARGET_ENABLED=false`, `VOLUME_DAILY_FEE_BUDGET_USDT=0`;
    - `ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR=0`, `ENTRY_WEEKLY_WINDOW=mon 07:00-sat 00:00`;
    - `FORWARD_KILL_AFTER_POSITIONS=60`, `FORWARD_KILL_BELOW_PER_1M=-500`, `FORWARD_REVIEW_AT_POSITIONS=150`;
    - `TELEMETRY_ENABLED=false`;
    - `MAX_STOP_LOSS_PER_DAY=1000`, `MAX_DAILY_DRAWDOWN_USDT=1000000000`, `MAX_CONSECUTIVE_LOSSES=1000` (spec §9.1).
  - It sets `VOLUME_CYCLE_STARTED_AT=<cycle_start>`, which must be a timezone-aware ISO time.
  - It blanks `BINGX_API_KEY`, `BINGX_SECRET_KEY` and `XAI_API_KEY`.
  - It writes the target exclusively with mode 0600.
  - It refuses an existing target, a target equal to the source, or a source without `SUPABASE_URL`.
- CLI: `--source --target --cycle-start`. It prints key names only.

- [ ] **Step 1: Write the failing tests** (temp files with fake values such as `x-test-1`):
  - `test_target_always_has_the_forward_schema`, including when the source says `public`.
  - `test_overrides_replace_and_append`
  - `test_secrets_are_blanked_and_never_printed`: stdout and the report hold no source value.
  - `test_other_lines_and_comments_are_kept`
  - `test_existing_target_and_same_path_refused`
  - `test_file_mode_is_0600`
  - `test_naive_cycle_start_refused`
- [ ] **Step 2: Run** the tests. Expect `ImportError`.
- [ ] **Step 3: Implement**, reusing the line parsing of `src/clean_env.py` (`_key_from_line`) instead of writing a new parser.
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: build the forward bot's .env without printing secrets"`.

### Task 6: Weekly forward report and trade export

**Files:**
- Create: `src/report_forward_test.py`
- Test: `tests/test_report_forward_test.py`

**Interfaces:**
- Produces `summarize(trades: Sequence[ClosedTrade], first_day: date, last_day: date) -> ForwardSummary` with these fields:
  - `posisi`, `entry_per_hari_kalender`;
  - `untung_pct` (pnl > 0);
  - `exits` (counts by `tp` = neutral_exit / take_profit, `sl` = stop_loss, `waktu` = time_stop, `ai` = strategy_exit);
  - `hasil_per_1_juta`, `hasil_tanpa_teratas` (drop the top 3).
- `export_csv(trades, path)` writes, exclusively, the columns `opened_at,closed_at,direction,entry_price,exit_price,quantity,pnl_usdt,reason,volume_usdt`.
- CLI: `--from YYYY-MM-DD --to YYYY-MM-DD` (WIB, `to` exclusive) and `--out-dir`. It reads through `SupabaseTradeStore` with GET only, using the `.env` of the working directory, so it is run from `/home/ubuntu/ai-perp-long`. It prints the summary and the guard state (count against 60 and 150).

- [ ] **Step 1: Write the failing tests** (hand-built `ClosedTrade` lists):
  - `test_summary_numbers`
  - `test_exit_mapping`
  - `test_top_three_dropped`
  - `test_csv_columns_and_exclusive_write`
  - `test_window_is_wib_and_end_exclusive`
- [ ] **Step 2: Run** the tests. Expect `ImportError`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: weekly forward-test report and trade export"`.

### Task 7: `compare-forward` stage in the mode backtest

**Files:**
- Create: `src/research/mode_backtest/forward_compare.py`
- Modify: `src/research/mode_backtest/cli.py`, adding the subcommand `compare-forward`.
- Test: `tests/test_mode_backtest_forward_compare.py`

**Interfaces:**
- Consumes:
  - `stages_live` helpers;
  - `simulate_live_run`, `LiveRunSpec`;
  - `stage_metrics(..., hari_kalender=)`;
  - `data.load_pair`;
  - the CSV from Task 6.
- Produces `compare_forward_stage(config_path, live_config_path, data_dir, forward_trades: Path, pair: str, first_day: date, end_day: date, out_dir, now) -> Path`.
  - It loads days `[first_day − 1, end_day − 1]` plus the warm-ups.
  - It runs L(i) and L(ii) over `[first_day 00:00, end_day 00:00)` WIB with no lock and no marker.
  - It writes, exclusively, `forward_compare_<first>_<end>.json` and an Indonesian `.md`: mode L (i) and (ii) beside the forward summary (positions, entries per calendar day, per 1M, without top 3, exit mix).
  - It refuses any `first_day − 1` before `uji.ujian_selesai`, a non-BTC pair, and an empty or reversed window.
- CLI exit codes as the package: 0, 1, 2.

- [ ] **Step 1: Write the failing tests** (synthetic days dated after 2026-10-14, built with the `tests/mode_backtest_helpers.py` writers):
  - `test_refuses_any_day_before_the_end_of_the_test_period`: no file is opened.
  - `test_runs_both_versions_and_writes_the_side_by_side`
  - `test_never_writes_a_marker_or_lock`
  - `test_outputs_never_overwritten`
  - `test_cli_exit_codes`
- [ ] **Step 2: Run** the tests. Expect `ImportError`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite. Expect OK.
- [ ] **Step 5: Commit** `git commit -m "feat: compare-forward stage for the weekly mode L comparison"`.

### Task 8: Runbook, docs and full verification

**Files:**
- Create: `docs/runbooks/long-forward-test.md` (English).
  - Account owner's steps:
    - the API key with trade permission only, no withdraw, and an IP whitelist on 43.129.56.85;
    - the separate AI key;
    - running `supabase/long_forward_schema.sql` in the SQL editor and adding `long_forward` to the exposed schemas;
    - typing the three keys into the new `.env`.
  - Operator's steps:
    - clone to `/home/ubuntu/ai-perp-long` at merged main, its own `.venv`, `pip install -r requirements.txt`;
    - `build_forward_env.py --source /home/ubuntu/ai-perpetual-trading/.env --target /home/ubuntu/ai-perp-long/.env --cycle-start <now>`;
    - a first start in the foreground to read the startup checks and the fee log, then `pm2 start src/main.py --name ai-perp-long --interpreter .venv/bin/python --cwd /home/ubuntu/ai-perp-long`;
    - never touch `ai-perp-volume`.
  - Weekly steps:
    - the production days through `research.pine_gate.cli fetch` and `warmup`;
    - `report_forward_test.py`;
    - `compare-forward`;
    - the Indonesian report.
  - Stop rules: the guard's 60/150 latches, and "never restart with an open position".
- Modify: `CLAUDE.md`, with a bullet for the forward bot (settings, the guard, the second app and schema, the import exception) and the new commands. Also `.github/tdd/long-ai-forward-test.tdd.md` (user journey, evidence per task, verification, commit trail, deployment gate).

- [ ] **Step 1:** Run `PYTHONPATH=src .venv/bin/python -m compileall -q src tests`.
- [ ] **Step 2:** Run coverage with an rcfile holding the `pyproject.toml` settings (Python 3.10). Expect the suite OK and ≥ 80 %.
- [ ] **Step 3:** Run `git diff --check` and `tests.test_secret_hygiene`.
- [ ] **Step 4:** Commit `git commit -m "docs: runbook and notes for the LONG AI forward test"`.
- [ ] **Step 5:** Ask the user before pushing and opening the PR. Merge only after the PR's CI is green (read the checks; never `--auto`).

### Task 9: Deploy (operational, on merged `main`, only on the user's go-ahead)

- [ ] **Step 1:** The user does the account-owner steps of the runbook. Claude never handles a key.
- [ ] **Step 2:** On the VPS, run the clone, venv and `.env` build. Show the user the printed key names. The user types the keys.
- [ ] **Step 3:** Start in the foreground once. Check the startup checks: isolated margin, hedge mode, leverage, the account's fee from the log, `SUPABASE_SCHEMA=long_forward` reachable, and the regime gate's first verdict. Stop.
- [ ] **Step 4:** Start the pm2 app `ai-perp-long`. Confirm that `ai-perp-volume` is still online and untouched.
- [ ] **Step 5:** Record the start time (the cycle start) and the first week's date in CLAUDE.md and in memory.
