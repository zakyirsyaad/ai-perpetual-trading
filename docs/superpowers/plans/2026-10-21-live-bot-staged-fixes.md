# Live bot staged fixes (lean AI payload, two-way regime gate, setup ledger) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give account 1 a two-way regime gate (SHORT in TURUN, LONG in NAIK, a side that can run in shadow), a binding setup ledger fed by real and shadow outcomes, and an honest lean AI entry payload. Each piece sits behind a setting whose default keeps today's behaviour.

**Architecture:**
- `bot/core/regime_gate.py` gains mode `map` and richer verdicts.
- `AIOrchestrator` maps the decision side, marks shadow decisions, records the bucket, applies the ledger and builds the lean payload.
- Two new pure modules hold the maths: `bot/core/setup_ledger.py` (bucket statistics and status) and `bot/core/shadow_eval.py` (walking 1m candles with the live exit rules).
- A new `shadow_entries` table holds shadow outcomes. The engine evaluates at most 3 pending rows per tick.
- `src/report_live_stages.py` compares rollout stages.

**Tech Stack:** Python ≥ 3.10 stdlib, the existing bot (`pydantic-settings`, `httpx`), stdlib `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-21-live-bot-staged-fixes-design.md` (Part A §4, Part B §5, Part C §6, report §7, rollout §8, tests §9). The user approved D1–D4 as proposed (2026-10-21).

**Refinement of spec §6, recorded here:** the spec reads real outcomes "once per closed trade, cached in memory between closes". This plan reads them once per tick, only while `SETUP_LEDGER_MODE` is not `off`. That is one small GET of trades since `SETUP_LEDGER_SINCE` plus one of shadow outcomes. It is stateless across restarts, like the forward guard, and the intent is the same.

**Refinement of spec §4, recorded here:** the spec's "change over 15 and 60 minutes" is computed in bars, as `change_pct_5_bars` and `change_pct_20_bars` with a `bar_interval` field. On the live 3m candles that is the same 15 and 60 minutes, and the names stay true if `AI_KLINE_INTERVAL` changes.

**Refinement of spec §6, recorded here:** the ledger requires the regime gate (`naik` or `map`), because the bucket needs a condition. `Settings` refuses `SETUP_LEDGER_MODE` ≠ `off` with `ENTRY_REGIME_GATE=off`.

## Global Constraints

- **Defaults change nothing:**
  - `ENTRY_REGIME_GATE=off`, `AI_ENTRY_PAYLOAD=legacy`, `SETUP_LEDGER_MODE=off`.
  - The legacy entry payload stays byte-for-byte identical; a golden test pins it.
  - Account 1 is not changed by merging. Each rollout step is a settings change plus a restart while flat, and only on the user's go-ahead.
- **Never touch the live app beyond read-only checks** without the user's explicit go-ahead for that step. Never restart a bot with an open position: check flat on the venue and in `open_position_state`.
- **Secrets:** never read or print a `.env` value, and no key-like string in any tracked file.
- **Imports:**
  - `bot` never imports `research`.
  - Only `bot/core/regime_gate.py` may import `bot.strategy.pine_indicators` or `bot/strategy/modes/`.
  - The new modules `setup_ledger.py` and `shadow_eval.py` import neither, and the structure tests stay unchanged.
- **Spec numbers, verbatim:**
  - map: NAIK → `long`, TURUN → `short`, SIDEWAYS and TIDAK_ENTRY → none;
  - reasons `naik`, `turun`, `not_mapped:<CONDITION>`;
  - shadow evaluation: at most 3 rows per tick, rows older than hold + 1 minutes, stop first when a candle touches both levels, stop filled 1 bp worse, time exit at the last close;
  - ledger: `SETUP_LEDGER_MIN_SAMPLES` default 60, `SETUP_LEDGER_HURDLE_BPS` default 4.0, bound = mean + 1.28 × SE;
  - lean features: stretch in ATR, EMA21 slope over the last 5 bars in ATR, RSI14, change over 5 and 20 bars in %, ATR14 in %.
- **The exit AI stays off and untouched.** The exit payload does not change.
- **The forward bot (account 2) runs a pinned tag.** It is not redeployed.
- **Repo:** `PYTHONPATH=src`, stdlib unittest, coverage ≥ 80 %, `compileall` clean. TDD evidence goes in `.github/tdd/live-staged-fixes.tdd.md`. Code and docs are in English; the report's headings are in Indonesian.

## Review Focus

1. **The hour changes between the gate read and the decision.** The bucket, the mapped side and the payload must all come from the one gate context set before the strategy ran. → Task 3 `test_bucket_and_side_use_the_same_gate_context`.
2. **Pending shadow rows after downtime or a 60-minute hold.** The venue serves at most 1000 1m candles, so a row too old to walk must be closed as `expired`, never crash or stay pending forever. → Task 5 `test_a_row_older_than_the_candle_window_expires`.
3. **A data_error or history_short hour under `map`.** No side means no AI call, and the lean `allowed_actions` must never be built empty. → Task 1 `test_map_data_error_maps_no_side`, Task 9 `test_lean_payload_refuses_an_empty_side_list`.
4. **Degenerate ledger samples** (n = 0 or 1, every value identical). Status `learning` below the minimum, and a zero SE must not divide by zero. → Task 6 `test_identical_values_have_zero_se_and_still_decide`.
5. **A shadow decision must never trade**, even when it is the best-scored candidate. → Task 7 `test_a_shadow_side_candidate_never_becomes_a_plan`.

---

### Task 1: Regime gate mode `map` and a richer verdict

**Files:**
- Modify: `src/bot/core/regime_gate.py`
- Test: `tests/test_regime_gate.py` (helpers `FakeClient`, `hourly_rows`, `MARKETS`, `make_gate` already exist)

**Interfaces:**
- Produces:
  - `MODE_MAP = "map"`, `MODES = {off, naik, map}`, `REASON_TURUN = "turun"`, prefix `"not_mapped:"`.
  - `RegimeVerdict` gains these fields **appended with defaults**, so positional constructions keep working: `allowed_direction: str = ""`, `sma20_1h: Optional[float] = None`, `sma50_1h: Optional[float] = None`, `close_4h: Optional[float] = None`, `ema50_4h: Optional[float] = None`.
  - `as_payload()` includes the new fields.

- [ ] **Step 1: Write the failing tests** in a new `class MapModeTests(unittest.IsolatedAsyncioTestCase)`:
  - `test_map_naik_allows_long`: `make_gate(FakeClient(hourly_rows(MARKETS["NAIK"])), mode="map")` → `allowed is True`, `reason == "naik"`, `allowed_direction == "long"`.
  - `test_map_turun_allows_short`: `reason == "turun"`, `allowed_direction == "short"`.
  - `test_map_sideways_and_tidak_entry_map_no_side`: for each, `allowed is False`, `reason == f"not_mapped:{kondisi}"`, `allowed_direction == ""`.
  - `test_map_volatility_guard_blocks_both_sides`: with the guard's existing high-ATR rows built for TURUN, reason `volatility`, no side.
  - `test_map_data_error_maps_no_side`: a failing client → `data_error`, `allowed_direction == ""`.
  - `test_verdict_carries_the_selector_numbers`: `sma20_1h`, `sma50_1h`, `close_4h`, `ema50_4h` equal the values the existing backtest-equality test computes for the same rows.
  - `test_naik_mode_reports_long_when_allowed`: `naik` mode keeps every old field and reason, plus `allowed_direction == "long"` when allowed.

- [ ] **Step 2: Run, expect FAIL** (`ValueError: regime gate mode must be one of ...`).
  `PYTHONPATH=src python -m unittest tests.test_regime_gate`

- [ ] **Step 3: Implement** in `_classify`: compute as today. For `map`, the order is history check → condition → volatility guard (when the multiple > 0), so a guard trip blocks either side. Every verdict built after the selector ran carries the four numbers.

- [ ] **Step 4: Run, expect PASS** (whole module, old tests included).

- [ ] **Step 5: Commit** `feat: regime gate map mode (NAIK long, TURUN short)`

### Task 2: Settings for map, the lean payload and the ledger

**Files:**
- Modify: `src/bot/config.py`, `.env.example` (commented, with the defaults)
- Test: `tests/test_live_staged_settings.py` (new)

**Interfaces:**
- Produces these `Settings` fields:
  - `entry_regime_gate` accepts `map`;
  - `ai_entry_payload: str = "legacy"` (`AI_ENTRY_PAYLOAD`, `legacy` | `lean`);
  - `setup_ledger_mode: str = "off"` (`SETUP_LEDGER_MODE`, `off` | `shadow` | `enforce`);
  - `setup_ledger_since: datetime | None = None` (`SETUP_LEDGER_SINCE`);
  - `setup_ledger_min_samples: int = 60` (`SETUP_LEDGER_MIN_SAMPLES`, ≥ 2);
  - `setup_ledger_hurdle_bps: float = 4.0` (`SETUP_LEDGER_HURDLE_BPS`).
- `_require_long_only_companions` becomes `_require_regime_gate_companions`:
  - `naik` keeps its three demands unchanged, with the same message;
  - `map` demands `ENTRY_STRETCH_GATE_MODE=enforce` and an AI strategy, and accepts any `ENTRY_GATE_DIRECTION`;
  - a ledger mode other than `off` demands a regime gate and a timezone-aware `SETUP_LEDGER_SINCE`.

- [ ] **Step 1: Write the failing tests:**
  - `test_defaults_are_legacy_and_off`;
  - `test_map_boots_with_each_direction` (`any`, `short`, `long`, each with enforce and `ai_volume`);
  - `test_map_refuses_without_enforce`;
  - `test_map_refuses_the_volume_strategy`;
  - `test_naik_still_demands_long` (the old message text is unchanged);
  - `test_payload_and_ledger_values_are_normalised_and_unknown_refused` (`" LEAN "` → `lean`; `foo` → `ValueError`);
  - `test_ledger_on_needs_a_regime_gate`;
  - `test_ledger_on_needs_an_aware_since` (naive time refused, missing time refused);
  - `test_min_samples_below_two_refused`.
  Construct `Settings` the way `tests/test_regime_gate.py` `ConfigTests` does.

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement.** Validators follow `normalize_entry_regime_gate`. `setup_ledger_since` parses ISO-8601 and is refused when naive, as `VOLUME_CYCLE_STARTED_AT` is.

- [ ] **Step 4: Run, expect PASS**, plus `tests.test_regime_gate` and `tests.test_build_forward_env` (the old validator name may be referenced there).

- [ ] **Step 5: Commit** `feat: settings for the map gate, the lean payload and the setup ledger`

### Task 3: Side mapping, shadow marking and the bucket on the trade

**Files:**
- Modify: `src/bot/ai/orchestration.py`
  - `_stretch_gate_verdict` (lines 538-603);
  - `_factor_metadata_for_symbol` (760-778) and its call in `_candidate_to_trade_plan` (269).
- Create: `src/bot/core/setup_ledger.py` with `bucket_key` only (Task 6 adds the rest).
- Test: `tests/test_ai_orchestration.py`, new `class MapSideTests(EntryStretchGateTests)` reusing its `_settings`/`_orchestrator` helpers; `tests/test_setup_ledger.py` (new) with `test_bucket_key_format`.

**Interfaces:**
- Consumes `RegimeVerdict.as_payload()` keys `kondisi` and `allowed_direction` from Task 1, through `set_gate_context`.
- Produces:
  - `bot.core.setup_ledger.bucket_key(side: str, kondisi: str, atr_regime: str) -> str`, e.g. `"SHORT|TURUN|atr_mid"` (side upper-cased).
  - `AIOrchestrator._mapped_side() -> str` returns `"long"`, `"short"` or `""`. Under `map` it is the gate context's `allowed_direction`; otherwise `""`.
  - `AIOrchestrator._live_sides() -> frozenset[str]`: under `map`, the mapped side if `ENTRY_GATE_DIRECTION` allows it (`any` or that side), else empty. Otherwise `{"long", "short"}` for `any`, or the one side.
  - A gate verdict under `map` carries `"mapped_side"` and `"shadow_side": bool`.
  - `factor_metadata["setup"]` gains `"kondisi"` (gate context `kondisi`, `""` without a gate) and `"bucket_key"` (`bucket_key(...)` above, or `""` without a gate).

- [ ] **Step 1: Write the failing tests:**
  - `test_map_mapped_side_live_passes_to_the_stretch_check`: context TURUN/short, direction `short`, decision `OPEN_SHORT` near the mean → `verdict == "allow"`, `shadow_side is False`.
  - `test_map_mapped_side_not_live_is_shadow`: context NAIK/long, direction `short`, `OPEN_LONG` near the mean → `verdict == "allow"`, `shadow_side is True`, `mapped_side == "long"`.
  - `test_map_shadow_side_still_gets_the_stretch_check`: the same as above but stretched → `verdict == "block"`, `reason == "stretch"`, `shadow_side is True`.
  - `test_map_wrong_side_is_a_direction_block`: context TURUN/short, `OPEN_LONG` → `verdict == "block"`, `reason == "direction"`, `forced_by == "regime_gate"`, `allowed_direction == "short"`.
  - `test_map_without_context_blocks_every_side`: no gate context → `verdict == "block"`, `reason == "direction"`, `allowed_direction == ""`.
  - `test_naik_and_off_verdicts_are_unchanged`: the existing `RegimeGateSideDefenceTests` and `EntryDirectionFilterTests` still pass untouched.
  - `test_bucket_and_side_use_the_same_gate_context`: after `set_gate_context(TURUN payload)`, the plan's `factor_metadata["setup"]["kondisi"] == "TURUN"` and `bucket_key == "SHORT|TURUN|<atr_regime>"`.

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement.** Under `map`:
  - the side filter uses `_mapped_side()` in place of `ENTRY_GATE_DIRECTION`;
  - a side equal to the mapped side continues to the stretch check, with `shadow_side = mapped side not in _live_sides()`;
  - any other side, or no mapped side, is the direction block with `forced_by = "regime_gate"`.
  `naik` and `off` keep today's branch exactly. The gate context is read once per call from `self._gate_context`.

- [ ] **Step 4: Run, expect PASS** (`tests.test_ai_orchestration`, `tests.test_orchestration_pipeline`).

- [ ] **Step 5: Commit** `feat: map the decision side to the regime and mark shadow sides`

### Task 4: `shadow_entries` table and store methods

**Files:**
- Modify: `supabase/schema.sql` (new table, index, RLS like `ai_exit_verdicts`), `supabase/long_forward_schema.sql` (regenerated with `src/forward_schema.py`), `src/bot/storage/supabase_trade_store.py`, `src/bot/storage/interfaces.py`
- Test: `tests/test_supabase_shadow_entries.py` (new; fake `httpx` transport as the existing store tests do), `tests/test_forward_schema.py` (drift test, unchanged, must pass)

**Interfaces:**
- Table `public.shadow_entries` has these columns:
  - `id bigint generated always as identity primary key`, `created_at timestamptz not null default now()`, `exchange text not null default 'bingx'`;
  - `decided_at timestamptz not null`, `symbol text not null`, `side text not null`, `bucket_key text not null`, `source text not null` (`shadow_side` | `ledger_block`);
  - `ref_price double precision not null`, `stop_loss_pct double precision not null`, `floor_pct double precision not null`, `hold_minutes integer not null`;
  - `gross_bps double precision`, `exit_reason text`, `evaluated_at timestamptz`.
  Index on `(exchange, evaluated_at, decided_at)`.
- Store, added to `TradeStoreProtocol` with no-op or empty defaults in every test fake that implements it:
  - `save_shadow_entry(row: dict[str, Any]) -> None`;
  - `pending_shadow_entries(decided_before: datetime, limit: int) -> list[dict[str, Any]]` (`evaluated_at` is null, oldest first);
  - `complete_shadow_entry(entry_id: int, gross_bps: float | None, exit_reason: str) -> None` (PATCH through a new `_patch(table, params, payload)` with `Content-Profile`);
  - `shadow_outcomes_since(since: datetime) -> list[dict[str, Any]]` (evaluated rows, paged);
  - `closed_trade_rows_since(since: datetime) -> list[dict[str, Any]]` (`_CLOSED_TRADE_COLUMNS`, paged past 1000, exchange filter, oldest first).

- [ ] **Step 1: Write the failing tests:**
  - `test_save_posts_to_the_configured_schema`;
  - `test_pending_filters_unevaluated_rows_before_the_cutoff_oldest_first` (asserts the query params `evaluated_at=is.null`, `decided_at=lt.<iso>`, `order=decided_at.asc`, `limit=3`);
  - `test_complete_patches_one_row_by_id`;
  - `test_outcomes_and_trades_since_page_past_1000`;
  - `test_schema_sql_defines_shadow_entries`;
  - the drift test after regeneration.

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**, then regenerate the long_forward SQL:
  `PYTHONPATH=src python src/forward_schema.py --schema long_forward > supabase/long_forward_schema.sql`

- [ ] **Step 4: Run, expect PASS** (new module, `tests.test_forward_schema`, all store and engine tests).

- [ ] **Step 5: Commit** `feat: shadow_entries table and store methods`

### Task 5: Shadow evaluation (pure walk plus engine wiring)

**Files:**
- Create: `src/bot/core/shadow_eval.py`
- Modify: `src/bot/core/trade_engine.py` (`_tick`, right after the `_entries_blocked_reason` check at line 224)
- Test: `tests/test_shadow_eval.py` (new), `tests/test_trade_engine_forward_gates.py` (wiring)

**Interfaces:**
- Produces:
  - `STOP_SLIP_BPS = 1.0`, `MAX_ROWS_PER_TICK = 3`, `CANDLE_WINDOW = 1000`.
  - `walk_exit(candles: Sequence[Mapping[str, float]], side: str, ref_price: float, stop_loss_pct: float, floor_pct: float, hold_minutes: int) -> tuple[float, str]`. It returns gross bps and one of `stop`, `floor`, `time`.
  - `async evaluate_pending(store, client, *, now: datetime, logger) -> int`. It returns the number of rows completed.
  - `TradeEngine._evaluate_shadow_entries() -> None`. It runs only when `entry_regime_gate == "map"` or `setup_ledger_mode != "off"`, catches every exception and logs it at WARNING.

- [ ] **Step 1: Write the failing tests** (candles are dicts with `timestamp`, `high`, `low`, `close` in ms, as `get_klines` returns):
  - `test_long_hits_the_floor` → (`floor_pct × 100`, `floor`).
  - `test_short_hits_the_stop_one_bp_worse` → (`-stop_loss_pct × 100 - 1.0`, `stop`).
  - `test_a_candle_touching_both_levels_is_a_stop`.
  - `test_time_exit_at_the_last_close_of_the_hold` (exactly `hold_minutes` candles walked).
  - `test_evaluates_at_most_three_rows_per_call`.
  - `test_rows_younger_than_hold_plus_one_minute_are_not_read` (the cutoff passed to `pending_shadow_entries` is `now - (hold + 1) min`, using the largest `hold_minutes` setting, `POSITION_HOLD_MINUTES`).
  - `test_candles_start_after_the_decision_minute` (the decision minute's own candle is skipped).
  - `test_a_venue_failure_leaves_the_row_pending_and_warns`.
  - `test_a_row_older_than_the_candle_window_expires` (`complete_shadow_entry(id, None, "expired")` when the oldest returned candle is after the decision minute + 1).
  - `test_too_few_candles_yet_leaves_the_row_pending`.
  - Engine:
    - `test_engine_evaluates_after_the_entries_latch_and_before_the_window` (a closed window still evaluates; a latched bot does not);
    - `test_engine_skips_evaluation_with_map_and_ledger_off`;
    - `test_an_evaluator_exception_never_stops_the_tick`.

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement.** For `walk_exit`, a candle's adverse extreme is its low for LONG and its high for SHORT. The stop is checked before the floor. The candle request is `get_klines(symbol, "1m", limit=min(CANDLE_WINDOW, minutes_since_decision + 2))`.

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit** `feat: evaluate shadow entries with the live exit rules`

### Task 6: Setup ledger maths

**Files:**
- Modify: `src/bot/core/setup_ledger.py` (created with `bucket_key` in Task 3)
- Test: `tests/test_setup_ledger.py`

**Interfaces:**
- Produces:
  - `Z_ONE_SIDED_90 = 1.28`.
  - `gross_bps(direction: str, entry_price: float, exit_price: float) -> float`.
  - `@dataclass(frozen=True) BucketStats(n: int, mean_bps: float | None, se_bps: float | None, upper_bps: float | None)`.
  - `bucket_stats(values: Sequence[float]) -> BucketStats`. It uses the sample stdev; SE is `None` below n = 2.
  - `ledger_status(real: BucketStats, shadow: BucketStats, min_samples: int, hurdle_bps: float) -> str`, one of `learning`, `open`, `blocked`, `reopened`:
    - `learning` when `real.n < min_samples`;
    - `blocked` when `real.upper < hurdle` and not reopened;
    - `reopened` when blocked by the real rule, and `shadow.n ≥ min_samples` with `shadow.upper ≥ hurdle`;
    - `open` otherwise.
  - `ledger_table(trade_rows, shadow_rows) -> dict[str, tuple[BucketStats, BucketStats]]`:
    - a trade row is bucketed by `factor_metadata.setup.bucket_key`; rows without one are skipped;
    - a shadow row counts only with `source == "ledger_block"` and a non-null `gross_bps`.

- [ ] **Step 1: Write the failing tests:**
  - `test_gross_bps_signs_by_direction`;
  - `test_stats_below_two_have_no_se`;
  - `test_identical_values_have_zero_se_and_still_decide` (60 × 1.0 bps → `blocked`; 60 × 4.0 → `open`);
  - `test_learning_below_min_samples`;
  - `test_blocked_when_the_upper_bound_is_below_the_hurdle` (a computed example: mean 1.0, SE 1.5 → upper 2.92 < 4 → `blocked`);
  - `test_open_when_the_upper_bound_reaches_the_hurdle`;
  - `test_reopened_needs_min_samples_of_shadow_at_or_above_the_hurdle`;
  - `test_table_skips_rows_without_a_bucket_and_shadow_side_rows`.

- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit** `feat: setup ledger statistics and status`

### Task 7: Ledger verdicts and shadow rows in the orchestrator

**Files:**
- Modify: `src/bot/ai/orchestration.py` (`next_trade_plan`, `_record_ai_decision` via the `gate` dict)
- Test: `tests/test_ai_orchestration.py`, new `class LedgerAndShadowTests(unittest.IsolatedAsyncioTestCase)` with a fake store exposing Task 4's methods

**Interfaces:**
- Consumes Tasks 3, 4 and 6.
- Produces:
  - `AIOrchestrator._ledger_snapshot() -> dict[str, tuple[BucketStats, BucketStats]]`. It is read once per `next_trade_plan` when `setup_ledger_mode != "off"`, else `{}`.
  - `gate["ledger"] = {"mode", "bucket", "n", "mean_bps", "upper_bps", "status"}` on every OPEN decision while the ledger is on.
  - `AIOrchestrator._shadow_row(decision, candidate, source) -> dict` with Task 4's columns:
    - `ref_price` = the 3m `last_price`;
    - `stop_loss_pct` = the candidate's;
    - `floor_pct` = `min(take_profit_pct, 100 × VOLUME_NEUTRAL_EXIT_MIN_NET_USDT / (margin_per_entry × leverage) + round_trip_fee_pct)`;
    - `hold_minutes` = `POSITION_HOLD_MINUTES`.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_shadow_side_candidate_never_becomes_a_plan` (it is the only candidate; `next_trade_plan` returns `None`, and one `save_shadow_entry` with `source == "shadow_side"`);
  - `test_a_stretched_shadow_side_writes_no_row`;
  - `test_ledger_off_reads_nothing_and_records_nothing`;
  - `test_ledger_shadow_records_the_status_and_trades`;
  - `test_ledger_enforce_turns_a_blocked_bucket_into_hold_and_a_ledger_block_row`;
  - `test_ledger_enforce_lets_a_reopened_bucket_trade`;
  - `test_a_ledger_read_failure_is_logged_and_treated_as_learning` (fail open to `learning`: the ledger only adds blocks, and the regime and stretch gates still apply).

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement.** In `next_trade_plan`, after `_build_entry_candidate`:
  - a candidate whose gate verdict has `shadow_side` is turned into a shadow row and dropped;
  - under `enforce`, a candidate in a `blocked` bucket is turned into a `ledger_block` row and dropped.
  The ledger fields join the `gate` dict before `_record_ai_decision`.

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit** `feat: setup ledger verdicts and shadow rows in the entry path`

### Task 8: Lean features from the 3m candles

**Files:**
- Modify: `src/bot/data/market_signals.py` (`MarketContext.lean_features: dict[str, dict[str, Any]]`, default empty, included in `to_dict`), `src/bot/intelligence/market_intelligence.py` (`_build_symbol_features` fills it), `src/bot/ai/compact_context.py` (`_deduplicated_market_context` pops `lean_features`, so legacy and exit payloads are unchanged)
- Test: `tests/test_lean_features.py` (new)

**Interfaces:**
- Produces `lean_features[symbol]` with these keys:
  - `bar_interval` (the `AI_KLINE_INTERVAL` string);
  - `stretch_atr`: (last − EMA21) / ATR14;
  - `ema21_slope_atr_5_bars`: (EMA21(closes) − EMA21(closes[:-5])) / ATR14;
  - `rsi_14`;
  - `change_pct_5_bars` and `change_pct_20_bars` (`pct_change`);
  - `atr_pct`: 100 × ATR14 / last.
  Each value is `None` when its input is missing (fewer than 26 closes, or ATR = 0).

- [ ] **Step 1: Write the failing tests:**
  - `test_values_on_a_known_series` (a linear ramp: slope and changes asserted to 1e-9);
  - `test_short_series_gives_none`;
  - `test_zero_atr_gives_none_for_atr_scaled_values`;
  - `test_legacy_and_exit_payloads_never_contain_lean_features`.

- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement**, reusing `bot.strategy.indicators` `ema`, `rsi`, `atr` and `pct_change`. **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit** `feat: lean 3m features beside the technicals`

### Task 9: Lean entry payload

**Files:**
- Modify: `src/bot/ai/compact_context.py` (new `build_lean_entry_payload`), `src/bot/ai/orchestration.py` (`next_trade_plan` picks the builder; `_strategy_objective_payload` gets a lean variant)
- Create: `tests/fixtures/legacy_entry_payload.json` (golden, captured **before** any change in this task)
- Test: `tests/test_compact_context.py`, new `class LeanPayloadTests` plus `test_legacy_payload_matches_the_golden`

**Interfaces:**
- Produces `CompactDecisionContextBuilder.build_lean_entry_payload(*, market_context: dict, daily_state, strategy_objective: dict, allowed_sides: Sequence[str], entry_rules: dict, market_regime: dict, ledger_lines: Sequence[dict]) -> dict`.
  - It raises `ValueError` on an empty `allowed_sides`. The orchestrator returns `None` (HOLD) without an AI call in that case.
  - Keys:
    - `task`, `execution_environment`;
    - `strategy_objective`: `primary_goal` is exactly "Open a position only when the setup shows a directional edge larger than the round-trip fee; HOLD is the expected answer most ticks.", and the volume profile is kept;
    - `risk_constraints`: today's dict without `min_confidence`;
    - `market_context`: the legacy dedupe, then without `factor_snapshots`, `active_flow_signals`, `new_flow_signals`, `intelligence.symbol_scores`, `intelligence.factor_intelligence` and `intelligence.market_flow_signals`;
    - `features` (Task 8's per symbol), `entry_rules`, `market_regime`, `setup_ledger` (only when lines exist);
    - `output_rules`: as today, with `allowed_actions = [f"OPEN_{side.upper()}" for side in allowed_sides] + ["HOLD"]`.
  - No `arena_reviews` and no `learned_trade_memory`.
- `entry_rules` = `{"stretch_atr": <signed, from build_setup_profile>, "max_stretch_atr": ENTRY_STRETCH_GATE_MAX_ATR, "rule": "an entry beyond this limit is rejected in code"}`.
- `market_regime` = the gate context minus `allowed`, plus `price_vs_sma20_1h_atr` = (last − sma20_1h) / atr14_1h.
- Under lean, the orchestrator skips `build_trade_decision_memory` and the arena reviews in the payload; the arena still runs.
- Every decision records `payload_json["payload_profile"]` as `legacy` or `lean`.
- `AIOrchestrator._ledger_lines(sides: Sequence[str], features: dict | None) -> list[dict]`: one line per side, `{"side", "bucket", "n", "mean_bps", "status"}`. The bucket comes from `bucket_key(side, gate kondisi, build_setup_profile(features, side)["atr_regime"])` and the numbers from Task 7's `_ledger_snapshot()`. Empty while the ledger is off.
- `allowed_sides` under `map` is `[_mapped_side()]` (the shadow side included); otherwise `_live_sides()`, sorted.

- [ ] **Step 1: Capture the golden** with the current code, running the existing `test_compact_context` fixture through `build_entry_payload`, and save it as `tests/fixtures/legacy_entry_payload.json` (sorted keys, indent 2).

- [ ] **Step 2: Write the failing tests:**
  - `test_legacy_payload_matches_the_golden`;
  - `test_lean_drops_the_factor_flow_arena_and_memory_blocks`;
  - `test_lean_keeps_technicals_external_and_liquidation`;
  - `test_lean_allowed_actions_follow_the_sides`;
  - `test_lean_payload_refuses_an_empty_side_list`;
  - `test_lean_has_no_min_confidence`;
  - `test_lean_objective_text_is_exact`;
  - `test_lean_regime_block_adds_price_vs_sma20`;
  - orchestrator: `test_lean_profile_is_recorded_and_legacy_is_default`, `test_no_mapped_side_under_lean_makes_no_ai_call`.

- [ ] **Step 3: Run, expect FAIL** (the golden test passes already).

- [ ] **Step 4: Implement.** **Step 5: Run, expect PASS** (`tests.test_compact_context`, `tests.test_ai_orchestration`, `tests.test_orchestration_pipeline`, `tests.test_dashboard_contract`).

- [ ] **Step 6: Commit** `feat: lean AI entry payload behind AI_ENTRY_PAYLOAD`

### Task 10: Stage report

**Files:**
- Create: `src/report_live_stages.py`
- Test: `tests/test_report_live_stages.py` (new; fake store)

**Interfaces:**
- CLI: `--stage-start <ISO>` (repeatable, at least one; each must be timezone-aware) and `--until <ISO>` (optional, default now).
- Pure helpers:
  - `stage_rows(trades, starts, until) -> list[StageSummary]`, where `StageSummary(start, end, n, win_rate, mean_gross_bps, se_gross_bps, net_per_1m, volume_per_day)`. A stage ends where the next one starts.
  - `shadow_summary(rows) -> list[tuple[side, bucket, n, mean_bps]]`.
- Output headings are Indonesian: "Ringkasan per tahap", "Shadow per sisi dan kelompok", "Ledger". Read-only: it uses only `closed_trade_rows_since`, `shadow_outcomes_since` and Task 6's `ledger_table`.

- [ ] **Step 1: Write the failing tests:**
  - `test_stages_split_at_each_start`;
  - `test_net_per_1m_uses_both_legs`: two trades with known prices and PnL give exactly `1e6 × Σpnl / Σ((entry + exit) × qty)`;
  - `test_naive_stage_start_is_refused_exit_2`;
  - `test_output_has_the_three_headings`.

- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit** `feat: live stage report`

### Task 11: Docs, evidence and full verification

**Files:**
- Modify: `CLAUDE.md`:
  - commands: the report;
  - "The tick loop and its gates": shadow evaluation after gate 1, the regime gate `map`;
  - "LONG AI forward test": the import exception is unchanged;
  - a new short section "Live staged fixes" with the three settings, the rollout table and the `shadow_entries` table.
- Modify: `README.md` only where the gates are listed.
- Create: `.github/tdd/live-staged-fixes.tdd.md` (user journey, RED/GREEN per task, verification numbers, commit trail, deployment gate).

- [ ] **Step 1: Run the full gate:**
  ```bash
  set -o pipefail
  PYTHONPATH=src python -m compileall -q src tests
  PYTHONPATH=src python -m coverage run -m unittest discover -s tests 2>&1 | tail -5
  PYTHONPATH=src python -m coverage report | tail -3
  git diff --check origin/main...HEAD
  ```
  Expected: all tests OK, total ≥ 80 %, no whitespace errors.

- [ ] **Step 2: Write the docs and the evidence file** with the numbers from Step 1.

- [ ] **Step 3: Commit** `docs: live staged fixes in CLAUDE.md and TDD evidence`

### Task 12: Rollout step 1 (operational, on merged `main`, only on the user's go-ahead)

- [ ] **Step 1:** The user applies the `shadow_entries` block of `supabase/schema.sql` in the Supabase SQL editor (`public` schema). Verify with a read-only GET that returns `[]`.
- [ ] **Step 2:** On the VPS, in `/home/ubuntu/ai-perpetual-trading`, pull the merged `main`. Verify the checkout's commit.
- [ ] **Step 3:** Back up `.env` with a timestamp. Set **only** these keys, with names-only verification:
  - `ENTRY_REGIME_GATE=map`
  - `ENTRY_GATE_DIRECTION=short` (unchanged)
  - `ENTRY_VOLATILITY_GUARD_MULT` (keep 0 unless the user says otherwise)
  - `SETUP_LEDGER_MODE=shadow`
  - `SETUP_LEDGER_SINCE=<restart time, ISO with tz>`
  Read the effective values back through `Settings()` with an allowlist.
- [ ] **Step 4:** Run `~/replay/flat_check.py`. Restart `ai-perp-volume` only on `FLAT`. Check the startup log, the first regime gate line, and 0 ERROR besides the pm2 stop traceback.
- [ ] **Step 5:** Record the step start time in project memory and in `CLAUDE.md` (a separate small docs PR). The next step waits for 60 SHORT trades (spec §8).

Rollout steps 2 (`AI_ENTRY_PAYLOAD=lean`) and 3 (`SETUP_LEDGER_MODE=enforce`) are settings-only. They follow Steps 3–5 above, each on its own go-ahead and after the previous step's 60 trades, judged with `src/report_live_stages.py`.
