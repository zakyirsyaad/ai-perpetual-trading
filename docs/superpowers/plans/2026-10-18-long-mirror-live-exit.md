# Mode L — LONG mirror of the live SHORT bot (offline backtest) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backtest mode L on BTC: the live bot's near-mean entry rule as LONG in NAIK, with the live exits written as formulas, in two versions, under measured costs. Use the Phase 1 training, lock and one-shot test, compare with the mirrored SHORT in TURUN, and rescore Mode A's logs under the same costs.

**Architecture:** The pure rule core (`src/bot/strategy/modes/`) gains the new config block and the exit-level formulas. The harness (`src/research/mode_backtest/`) gains:
- an ATR % column on the 3m candles;
- a live-exit walk with per-kind fills (`live_sim.py`);
- stages for mode L (`stages_live.py`) on the Phase 1 lock pattern;
- a `rescore` stage.

Mode A's code paths keep their behaviour. Its test is spent, so its lock fingerprint may change, but its trade log on the synthetic week may not.

Two refinements of spec §8:
- The live-exit walk and the L loop live in a new `live_sim.py` instead of growing `simulate.py`.
- The lock and record files use the existing `lock.run_name` convention (`mode_L_BTC-USDT_…`).

**Tech Stack:** Python ≥ 3.10 stdlib and PyYAML (`yaml.safe_load` through the existing loader), stdlib `unittest`, the Phase 1 harness.

**Spec:** `docs/superpowers/specs/2026-10-18-long-mirror-live-exit-design.md` (rules §4, costs §5, unchanged parts §6, YAML §7, harness changes §8). The forward-test spec `2026-10-18-long-ai-forward-test-design.md` gets its own plan.

## Global Constraints

- **Environment.** Local `.venv` (Python 3.10) for every stage run; CI runs 3.11. Every command uses `PYTHONPATH=src`. Tests are stdlib `unittest` in `tests/test_modes_*.py` and `tests/test_mode_backtest_*.py`. Global line coverage stays ≥ 80 %.
- **Imports.** `bot` never imports `research`. No live module imports `bot/strategy/modes` (that changes only in the forward-test plan). `bot/strategy/modes` never imports `bot.strategy.pine_indicators`.
- **Isolation.** No network and no venue code in `research/mode_backtest`. Data only from `~/replay/mode_backtest/`. Never touch the live bot, `.env`, pm2 or the VPS. Never rehearse a stage against `~/replay/mode_backtest/data`; stage tests use the synthetic week in `tests/mode_backtest_stages.py`.
- **Mode A stays as it is.** `config/modes.yaml` is unchanged. Mode A's walk, money, trade log columns and metrics values are unchanged for every Mode A trade. Its existing records (lock, start record, marker) still refuse a Mode A rerun.
- **Mode L rules (spec §4):**
  - Signal at each closed 3m candle `t` with `abs(stretch) <= 0.75` (39 bars, EMA21, ATR14, the live maths) and condition NAIK; entry at the open of the 1m bar at `t`.
  - Comparator S: the same rule, SHORT in TURUN.
  - `sl_pct = min(max(0.28, 1.5 × atr_pct), 0.40)`, with `atr_pct = ATR14(3m) / close × 100` on the signal candle's window.
  - TP: `(e(1 + r) + N/q) / (1 − r)` for LONG and `(e(1 − r) − N/q) / (1 + r)` for SHORT, with `r = 0.0005`, `N = 56`, `q = notional / e`.
  - Cut, version (ii) only: `e × (1 − side × 0.10 / 100)`, active from the bar that opens 10 minutes after entry.
  - Time exit at the open of the bar 60 minutes after entry.
- **Walk order inside a bar:** time exit → open beyond SL → open beyond the active cut → open beyond TP → touch SL → touch the active cut → touch TP. A loss level wins a bar that also touches TP.
- **Costs (spec §5):**
  - entry fill `open × (1 + side × 0.3 / 10,000)`;
  - SL, POTONG and WAKTU exits `raw × (1 − side × 0.5 / 10,000)`;
  - TP exits at the TP price exactly;
  - fee 0.035 % of each leg's notional; notional 25,000 USDT.
- **Unchanged from Phase 1:** general rules (`umum` of `config/modes.yaml`), the training and test segments, the pass rule per version, and outputs through `research.pine_gate.fileio` (exclusive). CLI exit codes: 0 ok, 1 expected failure, 2 usage error or lock refusal.
- **Metrics:**
  - `Menang` = TP / (TP + SL + POTONG).
  - `Entry per hari aktif` = positions / NAIK days for L and / TURUN days for S.
  - `Entry per hari kalender` = positions / WIB calendar days of the stage (spec §6, decided 2026-10-18).
- **Language and evidence.** Reports in Indonesian; code, tests and docs in English. Every task appends its RED output and GREEN result to `.github/tdd/long-mirror-live-exit.tdd.md`.

## Review Focus

1. **A gap past the cut at the minute-10 open.** The price fell below the cut level before minute 10 and the bar at minute 10 opens below it: the exit is POTONG at that open, not at the level, and not an SL unless the open is also beyond the SL. → Task 4 `test_gap_past_the_cut_at_minute_ten_exits_at_the_open`.
2. **The cut must not act before minute 10.** A touch of the cut level at minute 9 with no SL touch leaves the position open. → Task 4 `test_cut_touch_before_minute_ten_is_ignored`.
3. **A flat 3m window (ATR 0).** The stretch is `None`, so there is no signal. The SL formula must never run on a missing ATR. → Task 5 `test_flat_window_gives_no_signal_and_no_levels`.
4. **The live YAML's trigger numbers differing from Mode D's in `config/modes.yaml`.** The frames carry Mode D's window and periods, so a mismatch must be refused, not silently mixed. → Task 1 `test_trigger_numbers_must_match_mode_d`, Task 5 `test_live_run_refuses_frames_built_with_other_periods`.
5. **A rescored Mode A log with a gapped SL exit** (filled at a bar open, not at the level). The raw exit is recovered from the fill, so the rescore must not assume the level. → Task 9 `test_rescore_recovers_the_raw_exit_of_a_gapped_stop`.

---

### Task 1: Live-exit config file and loader

**Files:**
- Create: `config/modes_live_exit.yaml` (exactly spec §7's block)
- Modify: `src/bot/strategy/modes/config.py`
- Test: `tests/test_modes_live_exit_config.py`

**Interfaces:**
- Produces:
  - `LiveExitModeConfig(aktif, tf_pemicu, candle_jendela, ema_periode, atr_periode, maks_jarak_ke_ema_kali_atr, sl_minimal_persen, sl_kali_atr, sl_maksimal_persen, tp_net_usdt, fee_perkiraan_persen, batas_waktu_menit, potong_rugi_persen, potong_rugi_setelah_menit)`
  - `CostConfig(slippage_entry_bps, slippage_exit_market_bps, slippage_tp_bps)`
  - `LiveExitConfig(mode_l: LiveExitModeConfig, biaya: CostConfig)`
  - `parse_live_exit_config(doc) -> LiveExitConfig`
  - `load_live_exit_config_with_sha256(path: Path) -> tuple[LiveExitConfig, str]`
  - `check_live_trigger_matches(live: LiveExitConfig, cfg: ModesConfig) -> None`, which raises `ModesConfigError` when `candle_jendela`, `ema_periode`, `atr_periode`, `maks_jarak_ke_ema_kali_atr` or `tf_pemicu` differ from `cfg.mode_d`.
  - All frozen dataclasses. Top-level keys are `mode` (holding only `L_cermin_live`) and `biaya`.

- [ ] **Step 1: Write the failing tests:**
  - `test_repo_file_loads_with_the_spec_values`: every value of spec §7, e.g. `sl_minimal_persen == 0.28`, `tp_net_usdt == 56`, `slippage_exit_market_bps == 0.5`.
  - `test_unknown_missing_mistyped_and_repeated_keys_are_refused`, reusing the Phase 1 helpers' messages (path named).
  - `test_numbers_must_be_plain_decimal`: `015` and `1:30` are refused.
  - `test_sl_bounds_are_ordered`: `sl_minimal_persen <= sl_maksimal_persen`, else refused.
  - `test_cut_must_sit_inside_the_minimal_stop`: `potong_rugi_persen < sl_minimal_persen`, else refused.
  - `test_trigger_numbers_must_match_mode_d`
  - `test_sha_is_of_the_bytes_read`
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_live_exit_config -v`. Expect `ImportError`.
- [ ] **Step 3: Implement** with the existing private readers (`_check_keys`, `_read_block`, `_positive`, `_integer`, `_boolean`, `_one_of("3m")`) and `_PlainScalarLoader`. Hash and parse one read, as `load_modes_config_with_sha256` does.
- [ ] **Step 4: Run** the tests, plus `tests.test_modes_config` (unchanged). Expect them all to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: config file and loader for mode L"`.

### Task 2: Exit-level formulas and a side-neutral near-mean trigger

**Files:**
- Create: `src/bot/strategy/modes/live_exit.py`
- Modify: `src/bot/strategy/modes/near_mean.py`
- Test: `tests/test_modes_live_exit.py`, `tests/test_modes_near_mean.py` (one addition)

**Interfaces:**
- Consumes: Task 1 (`LiveExitModeConfig`); `checks.check_side`, `require_positive`.
- Produces:
  - `LiveExitLevels(sl: float, tp: float, cut: Optional[float], sl_pct: float)`
  - `stop_pct(atr_pct: float, rules: LiveExitModeConfig) -> float`
  - `net_target_price(entry_fill: float, side: int, notional: float, rules) -> float`
  - `live_exit_levels(entry_fill: float, side: int, atr_pct: float, notional: float, rules, with_cut: bool) -> LiveExitLevels`
  - `near_mean(stretch_atr, max_abs) -> bool`, with `near_mean_short = near_mean` kept for Mode D.

- [ ] **Step 1: Write the failing tests:**
  - `test_stop_pct_clamps`: atr 0.10 gives 0.28; atr 0.20 gives 0.30 (`assertAlmostEqual`); atr 0.30 gives 0.40.
  - `test_net_target_ratio_at_25000`: LONG `e × 1.0032416208`, SHORT `e × 0.9967616192` (9 decimals).
  - `test_levels_from_the_reference_entry`: entry fill 100.003, LONG, atr 0.10, 25,000 gives sl 99.722992, tp 100.327172, cut 99.902997. SHORT from 99.997 gives sl 100.276992, tp 99.673172, cut 100.096997 (6 decimals).
  - `test_no_cut_without_version_ii`: `with_cut=False` gives `cut is None`.
  - `test_bad_inputs_raise`: side 0, a non-finite atr, notional 0.
  - `test_near_mean_is_side_neutral_and_mode_d_name_still_works`
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_live_exit tests.test_modes_near_mean -v`. Expect `ImportError`.
- [ ] **Step 3: Implement** with numbers only (no bars, no I/O). `r = fee_perkiraan_persen / 100`, `q = notional / entry_fill`.
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: live exit levels for mode L"`.

### Task 3: ATR % on the 3m candles, and condition days for any condition

**Files:**
- Modify: `src/research/mode_backtest/frames.py`
- Test: `tests/test_mode_backtest_frames.py` (additions)

**Interfaces:**
- Produces:
  - `atr_pct_series(bars_3m: Bars, window: int, atr_period: int) -> Column`: `bot.strategy.indicators.atr` over the same window as the stretch, divided by the candle's close × 100. `None` where the stretch is `None`.
  - `PairFrames.atr_pct_3m: Column` (new field, index-aligned with `stretch_bars`), filled by `build_pair_frames`.
  - `HourlyFrame.condition_days(start_ms, end_ms, window, min_hours, kondisi) -> FrozenSet[date]`. `naik_days(...)` becomes `condition_days(..., NAIK)` with the same signature and results.

- [ ] **Step 1: Write the failing tests:**
  - `test_atr_pct_matches_the_live_atr_on_each_window`: compare to `atr(...)` on hand slices.
  - `test_atr_pct_is_none_where_the_stretch_is_none`
  - `test_turun_days_count_like_naik_days`, on a stub hourly frame.
  - `test_naik_days_unchanged`: the existing NAIK-days tests still pass untouched.
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_frames -v`. Expect `AttributeError`.
- [ ] **Step 3: Implement.** Build both columns in one pass over the windows in `stretch_series` (return the extra column through a new `stretch_and_atr_series`; keep `stretch_series` as a wrapper so the Pine-equality test still holds).
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: 3m ATR percent and condition days for mode L"`.

### Task 4: The live-exit walk and per-kind fills

**Files:**
- Create: `src/research/mode_backtest/live_sim.py` (walk and money part)
- Modify: `src/research/mode_backtest/simulate.py` (add `EXIT_POTONG = "POTONG"` beside the other reasons)
- Test: `tests/test_mode_backtest_live_sim.py`

**Interfaces:**
- Consumes: `ExitOutcome`, `Money`, `adverse_fill`, `INTERVAL_1M` from Phase 1; Task 2's `LiveExitLevels`; Task 1's `CostConfig`.
- Produces:
  - `resolve_live_exit(bars_1m, entry_idx, side, levels: LiveExitLevels, cut_from_min: int, limit_min: int) -> Optional[ExitOutcome]`, with reasons `TP`, `SL`, `POTONG` and `WAKTU`.
  - `settle_live(entry_open, outcome: ExitOutcome, side, notional, fee_pct, costs: CostConfig) -> Money`. TP exits use `slippage_tp_bps`, the others `slippage_exit_market_bps`.

- [ ] **Step 1: Write the failing tests:**
  - `test_cut_touch_before_minute_ten_is_ignored`
  - `test_cut_touch_from_minute_ten_exits_at_the_cut`
  - `test_gap_past_the_cut_at_minute_ten_exits_at_the_open`
  - `test_loss_level_wins_a_bar_that_touches_tp` (both SL and cut)
  - `test_sl_still_acts_inside_the_first_ten_minutes`
  - `test_time_exit_at_minute_sixty_before_levels`
  - `test_version_i_has_no_cut`: `levels.cut is None`.
  - `test_data_ending_first_returns_none`
  - `test_money_per_exit_kind`: open 100, 25,000 notional, fee 0.035, entry 0.3 bps, market exit 0.5 bps. Compare to 6 decimals:

    | Side and exit | pnl | fee | volume |
    |---|---|---|---|
    | LONG TP | 63.512156 | 17.528364 | 50081.04052 |
    | LONG SL (atr 0.10) | −88.721564 | | 49928.7535 |
    | LONG POTONG | −43.739563 | | |
    | LONG WAKTU at raw 100.1 | 5.490011 | | |
    | SHORT TP | 63.487856 | | |
    | SHORT SL | −88.778439 | | |

  - `test_tp_fill_is_the_level_exactly`
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_live_sim -v`. Expect `ImportError`.
- [ ] **Step 3: Implement.** Inside a bar the order is that of the Global Constraints. The cut is active on bars with index ≥ `entry_idx + cut_from_min`. Holes in the minutes end the walk with `None`, as in `resolve_exit`.
- [ ] **Step 4: Run** the tests, and `tests.test_mode_backtest_simulate`. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: live exit walk and per-kind fills"`.

### Task 5: The event loop for L and S

**Files:**
- Modify:
  - `src/research/mode_backtest/simulate.py`: make `_Book`, `_Gates`, `_closes`, `_NearMeanTrigger`, `_check_column` public as `Book`, `Gates`, `candle_closes`, `NearMeanTrigger`, `check_column`, and update every reference, tests included (`grep -rn` them). `Gates.__init__(umum, kondisi_gate: str, end_ms: int, limit_min: int)`.
  - `Trade`: append the defaulted fields `versi: str = ""`, `sl_pct: Optional[float] = None` and `potong: Optional[float] = None`.
- Modify: `src/research/mode_backtest/live_sim.py` (loop part)
- Test: `tests/test_mode_backtest_live_sim.py` (additions), `tests/test_mode_backtest_simulate.py` (one golden test)

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces:
  - `MODE_L = "L"`
  - `LiveRunSpec(pair: str, side: int, kondisi_gate: str, versi: str, start_ms: int, end_ms: int)`, validated so that LONG pairs with NAIK, SHORT pairs with TURUN, and `versi` is `"i"` or `"ii"`.
  - `simulate_live_run(frames: PairFrames, cfg: ModesConfig, live: LiveExitConfig, run: LiveRunSpec) -> RunResult`. Trades have `mode="L"` for LONG and `"S"` for SHORT.

- [ ] **Step 1: Write the failing tests.** Record first `MODE_A_SYNTHETIC_LOG_SHA`: the sha256 of `trade_log_text` of Mode A 5m on the synthetic week, computed on the code **before** this task's changes.
  - `test_mode_a_trade_log_unchanged_on_the_synthetic_week`: equal to that sha.
  - `test_l_enters_long_only_in_naik_at_the_signal_open`
  - `test_s_enters_short_only_in_turun`
  - `test_general_gates_apply_in_phase_1_order`: window, segment edge with a 60-minute limit, pause 15, daily cap and volatility.
  - `test_flat_window_gives_no_signal_and_no_levels`
  - `test_live_run_refuses_frames_built_with_other_periods`
  - `test_version_ii_trades_carry_the_cut_and_version_i_none`
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_live_sim tests.test_mode_backtest_simulate tests.test_mode_backtest_loop -v`. Expect failures on the new names.
- [ ] **Step 3: Implement** `simulate_live_run`:
  - `NearMeanTrigger` with `live.mode_l.maks_jarak_ke_ema_kali_atr`;
  - `Gates` with `limit_min = batas_waktu_menit`;
  - on pass, the levels from `frames.atr_pct_3m[i]`, then the walk and the money;
  - `check_live_trigger_matches` and the `FrameParams` check first.
- [ ] **Step 4: Run** the full suite `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`. Expect OK.
- [ ] **Step 5: Commit** `git commit -m "feat: event loop for mode L and comparator S"`.

### Task 6: Metrics, trade log and table for mode L

**Files:**
- Modify: `src/research/mode_backtest/metrics.py`, `tradelog.py`, `report.py`
- Test: `tests/test_mode_backtest_metrics.py`, `tests/test_mode_backtest_tradelog.py`, `tests/test_mode_backtest_report.py` (additions)

**Interfaces:**
- Produces:
  - `StageMetrics` gains the defaulted fields `potong: int = 0` and `entry_per_hari_kalender: Optional[float] = None`.
  - `stage_metrics(trades, hari_naik, drop_top, hari_kalender: Optional[int] = None)`. The parameter keeps its name. For S it receives the TURUN days, and the report labels them as active days. `menang_pct` = TP / (TP + SL + POTONG); POTONG is 0 for Mode A, so its values stay the same.
  - `LIVE_COLUMNS = COLUMNS + ("versi", "sl_persen", "harga_potong")` and `trade_log_text(trades, live: bool = False)`.
  - `live_table(rows) -> str` with the header `Mode | Versi | Pair | Tahap | Posisi | Entry per hari aktif | Entry per hari kalender | Menang | Hasil per 1 juta | Keluar TP / SL / potong / waktu | Rata-rata lama posisi`.
  - `live_status_line(versi, m, uji) -> str`, in the Phase 1 status line's words with the version named.

- [ ] **Step 1: Write the failing tests:**
  - `test_potong_counts_as_a_loss_in_menang`
  - `test_calendar_entries_per_day`: e.g. 70 positions over 70 days gives 1.0.
  - `test_mode_a_metrics_unchanged`
  - `test_live_log_has_the_extra_columns_and_mode_a_log_does_not`
  - `test_potong_reason_in_the_log`
  - `test_live_table_rows_and_rounding`, with the decimal comma.
  - `test_live_status_line_names_the_version`
- [ ] **Step 2: Run** the three test modules. Expect failures.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: metrics, trade log and table for mode L"`.

### Task 7: Lock and the train/test stages for mode L

**Files:**
- Modify:
  - `src/research/mode_backtest/lock.py`: add `check_lock(..., extra: Mapping[str, str] = MappingProxyType({}))`; every extra key must be in the lock and equal.
  - `src/research/mode_backtest/stages.py`: make public the helpers this task reuses, with no behaviour change:
    - `_Period` → `Period`, `_training` → `training_period`, `_testing` → `testing_period`
    - `_outputs` → `outputs_for`, `_stamp` → `stamp`, `_refuse_existing` → `refuse_existing`
    - `_Bindings` → `Bindings`, `_training_manifest_sha` → `training_manifest_sha`
- Create: `src/research/mode_backtest/stages_live.py`
- Test: `tests/test_mode_backtest_live_stages.py`, `tests/test_mode_backtest_lock.py` (one addition)

**Interfaces:**
- Consumes: Tasks 1–6, `lock.*`, the synthetic week.
- Produces:
  - `train_live_stage(config_path, live_config_path, data_dir, out_dir, pair, now) -> Path`
  - `test_live_stage(config_path, live_config_path, data_dir, out_dir, pair, now) -> Path`
  - Run keys `L_i_NAIK`, `L_ii_NAIK`, `S_i_TURUN` and `S_ii_TURUN`, plus no trading.
  - Lock `mode_L_BTC-USDT_lock.json`. It binds `config_sha256` (modes.yaml), `live_config_sha256` (via `extra`), `code_sha256`, `train_manifest_sha256` and `python`.
  - Start record `mode_L_BTC-USDT_test_started.json`; marker `holdout_runs/mode_L_BTC-USDT_<lock sha>.json`.
  - The result JSON and `.md` carry the four runs, both day counts, the pass status per version for L, and the two manifests as in Phase 1.

- [ ] **Step 1: Write the failing tests** (synthetic week, `stage_config_text` for `modes.yaml`, the repo's `modes_live_exit.yaml`):
  - `test_train_writes_the_lock_with_both_config_shas`
  - `test_test_refuses_an_edited_live_config_with_exit_2`
  - `test_test_claims_start_record_and_marker_before_reading_a_test_day`, modelled on the Phase 1 test.
  - `test_test_runs_once`: a second run is refused, also with only the start record present.
  - `test_outputs_are_never_overwritten`
  - `test_mode_a_records_do_not_block_mode_l_and_vice_versa`
  - `test_result_reports_calendar_and_condition_days`
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_live_stages tests.test_mode_backtest_lock -v`. Expect failures.
- [ ] **Step 3: Implement** on the Phase 1 order of checks: refusals → claims → read test days → write.
- [ ] **Step 4: Run** the full suite. Expect OK.
- [ ] **Step 5: Commit** `git commit -m "feat: train and one-shot test stages for mode L"`.

### Task 8: CLI for mode L

**Files:**
- Modify: `src/research/mode_backtest/cli.py`
- Test: `tests/test_mode_backtest_cli.py` (additions)

**Interfaces:**
- Produces:
  - `train` and `test` accept `--mode L` with a required `--live-config`.
  - `--mode A` keeps its Phase 1 path, and `--live-config` with `--mode A` is a usage error (exit 2).
  - Any other mode or pair exits 1 with one `error:` line.

- [ ] **Step 1: Write the failing tests:**
  - `test_train_mode_l_dispatches_to_the_live_stage`
  - `test_mode_l_without_live_config_is_a_usage_error`
  - `test_mode_a_unchanged`
  - `test_other_modes_exit_1`
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_cli -v`. Expect failures.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: CLI for mode L"`.

### Task 9: Rescore stage (cost sensitivity of Mode A's logs)

**Files:**
- Create: `src/research/mode_backtest/rescore.py`
- Modify: `src/research/mode_backtest/cli.py` (subcommand `rescore`)
- Test: `tests/test_mode_backtest_rescore.py`

**Interfaces:**
- Produces:
  - `rescore_rows(rows: Iterable[Mapping[str, str]], costs: CostConfig, fee_pct: float, old_slippage_pct: float) -> List[RescoredTrade]`.
    - The raw entry open is `harga_entry / (1 + side × old/100)`.
    - The raw exit is `harga_exit / (1 − side × old/100)`.
    - The new fills follow spec §5 by `alasan_exit`.
  - `rescore_stage(trade_logs: Sequence[Path], live_config_path, config_path, out_dir, now) -> Path` writes `rescore_<stamp>.json` and `.md`: per log, the old and new result per 1M and per 1M without the top 3, labelled "sensitivitas biaya, bukan uji".
  - CLI: `rescore --trade-log PATH ... --config config/modes.yaml --live-config config/modes_live_exit.yaml --out-dir DIR`.

- [ ] **Step 1: Write the failing tests:**
  - `test_rescore_recovers_the_raw_exit_of_a_gapped_stop`
  - `test_rescore_of_a_tp_uses_the_level_without_slippage`
  - `test_rescore_keeps_each_exit_event`
  - `test_rescore_matches_settle_live_on_a_round_trip`
  - `test_rescore_never_overwrites`
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_rescore -v`. Expect `ImportError`.
- [ ] **Step 3: Implement.** It reads only the CSVs given and opens no bar file.
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: rescore stage for the cost sensitivity"`.

### Task 10: Documentation and full verification

**Files:**
- Modify:
  - `CLAUDE.md`: commands for `train`/`test --mode L` and `rescore`; a `research.mode_backtest` bullet for mode L, its records, and "test runs once".
  - `.github/tdd/long-mirror-live-exit.tdd.md`: the user journey, the evidence per task and the deployment gate.

- [ ] **Step 1:** Run `PYTHONPATH=src .venv/bin/python -m compileall -q src tests`.
- [ ] **Step 2:** Run `PYTHONPATH=src .venv/bin/python -m coverage run -m unittest discover -s tests` and then `coverage report`. Expect OK and ≥ 80 %.
- [ ] **Step 3:** Run `git diff --check` and `tests.test_secret_hygiene`.
- [ ] **Step 4:** Commit `git commit -m "docs: mode L commands, notes and TDD evidence"`.
- [ ] **Step 5:** Ask the user before pushing and opening the PR.

### Task 11: Run mode L on BTC (operational, on merged `main`, only on the user's go-ahead)

- [ ] **Step 1:** Run `verify-copy`, then `train --mode L --pair BTC-USDT --config config/modes.yaml --live-config config/modes_live_exit.yaml --data-dir ~/replay/mode_backtest/data --out-dir ~/replay/mode_backtest/out`, on the 3.10 `.venv`. Show the user the training table.
- [ ] **Step 2:** On the user's go-ahead, run `verify-copy` again and then the one-shot `test` with the same arguments. Pull nothing in between.
- [ ] **Step 3:** Run `rescore` on Mode A's training and test logs and on the Phase 1 comparator logs.
- [ ] **Step 4:** Write the Indonesian report `docs/research/<date>-modul-long-mode-l.md`:
  - the §2.9 table for L (i), L (ii), S (i), S (ii) and no trading, with entries per calendar day beside the live bot's pace;
  - the pass status per version;
  - the cost sensitivity of Mode A;
  - the caveats of spec §11.
- [ ] **Step 5:** Update CLAUDE.md's status sentence with the outcome and the lock, marker and manifest sha256s.
