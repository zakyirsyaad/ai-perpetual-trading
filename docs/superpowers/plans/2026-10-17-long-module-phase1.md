# LONG module — Phase 1 (step 0 and Mode A on BTC, offline) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run the manager's step 0 (the Pine holdout split by side and condition) and the pre-registered 1m backtest of Mode A on BTC (training, lock, one-shot test, Indonesian reports) without touching the live bot.

**Architecture:** A pure rule core in `src/bot/strategy/modes/` (config, mode selector, Mode A tracker, Mode D trigger, position rules). It takes numbers and is imported by no live module. A tested harness in `src/research/mode_backtest/` builds closed-candle series from production 1m bars and feeds them to the core. An event loop simulates entries at the next candle open and exits on a 1m walk. Metrics, the trade log and the tables follow the document. A lock file binds config, code and training data before a marker-guarded, one-shot test. Step 0 reuses `research.pine_gate`'s public API to replay the frozen Pine contenders and labels each trade with the new mode selector. Two refinements of spec §6.2: stage orchestration lives in `stages.py` (the CLI only parses arguments), and `tests/mode_backtest_helpers.py` builds synthetic day files for the tests.

**Tech Stack:** Python ≥ 3.10 stdlib (`array`, `bisect`, `statistics`, `json`, `csv`, `hashlib`, `argparse`, `datetime`), PyYAML (new, `yaml.safe_load` only), stdlib `unittest`, the existing `research.pine_gate` package.

**Spec:** `docs/superpowers/specs/2026-10-17-long-module-phase1-design.md` (source rules §2, decisions §3, defaults D1–D10 §5, YAML §6.3).

## Global Constraints

- Runs under the local `.venv` (Python 3.10, the same minor version as the Pine holdout run); CI runs 3.11. Every command uses `PYTHONPATH=src`; tests are stdlib `unittest` files named `tests/test_modes_*.py` and `tests/test_mode_backtest_*.py`; global line coverage stays ≥ 80 % (`fail_under = 80`).
- The only new dependency is PyYAML (`PyYAML>=6.0`), used through `yaml.safe_load`. No numpy.
- `bot` never imports `research`. `bot/strategy/modes` takes numbers and never imports `bot.strategy.pine_indicators`. No live module imports `bot/strategy/modes` in Phase 1.
- No network, no orders and no venue code in `research/mode_backtest`. Data comes only from `~/replay/mode_backtest/`, copied read-only from the VPS. Never touch the live bot, `.env`, pm2, or any file on the VPS.
- Timestamps are UTC milliseconds; a day is a WIB (UTC+7) calendar day; candles are UTC-epoch buckets (D1); at `t` only bars with `open + interval <= t` are visible (D2). The weekly window `mon 07:00-sat 00:00` is checked with `bot.core.entry_window`.
- Segments (WIB): training [2025-08-01 00:00, 2026-08-05 00:00); test [2026-08-05 00:00, 2026-10-14 00:00). Every value comes from the `uji` block of `config/modes.yaml`.
- Execution (spec §9.1):
  - Entry at the open of the 1m bar starting at the signal close `t`, × (1 + side × 0.0001). TP and SL are set from that fill.
  - The walk runs from the entry bar. The time exit is at the open of the bar `limit` minutes after entry. A bar opening beyond a level exits at its open. SL wins a bar that touches both. Each exit moves 0.01 % against the position.
  - Fee 0.035 % on each leg's notional. Notional 25,000 USDT.
  - A 15-minute pause per pair after an exit. At most 6 entries per pair and 10 in total per WIB day.
  - Skip a signal when ATR14 (1h) > 2 × the median of the last 720 closed-hour ATR14 values, or while fewer than 720 values exist.
- Mode A: trigger 15m (5m only through the pre-registered switch), EMA20. A touch is `low <= EMA`. A confirmation is a green candle with `close > EMA` within 3 candles, the touch included, and `close <= SMA20(1h) + 1.0 × ATR14(1h)`. TP +0.40 % / SL −0.25 % / 90 min, only in NAIK.
- Mode D (proxy): 3m bars and the live stretch maths over 39 bars (EMA21, ATR14); `|stretch| <= 0.75` → SHORT. TP −0.20 % / SL +0.20 % / 60 min, only in TURUN; the comparator runs the same in NAIK.
- Pass rule:
  - LOLOS when n ≥ 40, Hasil per 1 juta > −150 and, without the 3 best `pnl_usdt` (ties: earliest first), > −250.
  - BELUM CUKUP SAMPEL when n < 40.
  - GAGAL otherwise.
  - Switch to 5m when 15m training entries per NAIK day are < 3.
- Outputs go through `research.pine_gate.fileio` (exclusive, never overwritten). CLI exit codes: 0 ok, 1 expected failure (one `error:` line), 2 usage error or lock refusal.
- Reports are in Indonesian; code, tests and repo docs are in English. Every task appends its RED output and GREEN result to `.github/tdd/long-module-phase1.tdd.md`.

## Review Focus

1. **A forming candle leaking into a value at a boundary.** A 15m close exactly on the hour must see the hour that just closed and never the 1h or 4h bar still forming. → Task 5 `test_context_at_hour_close_uses_the_hour_just_closed_and_never_the_forming_one`, Task 9 `test_trigger_ema_is_aligned_to_closed_candles`.
2. **Test-period data reaching training.** `train` must not open a day file on or after 2026-08-05, and no training trade may exit after 2026-08-05 00:00 WIB. → Task 14 `test_train_never_opens_a_test_day_file`, Task 12 `test_segment_edge_blocks_a_signal_whose_time_limit_passes_the_end`.
3. **WIB vs UTC day rolls.** Daily caps and NAIK days roll at 00:00 WIB (17:00 UTC); the window opens Monday 07:00 WIB. → Task 12 `test_daily_cap_resets_at_wib_midnight_not_utc`, Task 5 `test_naik_days_count_only_window_hours`.
4. **A partial or altered data copy.** A file differing from the VPS, or a missing day or warm-up, stops the run instead of shrinking the sample. → Task 4 `test_verify_copy_reports_every_mismatch_and_missing_file`, `test_missing_day_raises_before_reading`.
5. **A "close enough" step 0.** Any differing `symbol_metrics` field of the Pine replay stops step 0 before it writes anything. → Task 6 `test_one_changed_field_is_reported_and_stops_step0`.

---

### Task 1: SMA, population stdev and rolling median

**Files:**
- Modify: `src/bot/strategy/pine_indicators.py`
- Create: `.github/tdd/long-module-phase1.tdd.md` (skeleton: journey, then one RED/GREEN section per task)
- Test: `tests/test_pine_indicators.py` (new class `WindowStatisticTests`)

**Interfaces:**
- Produces (lists aligned to the input; `None` until the window is full or while it holds a `None`):
  - `sma(values: Sequence[Optional[float]], n: int) -> Series`
  - `stdev(values: Sequence[Optional[float]], n: int) -> Series` (population, like TradingView `ta.stdev`)
  - `rolling_median(values: Sequence[Optional[float]], n: int) -> Series`

- [ ] **Step 1: Write the failing tests**

```python
class WindowStatisticTests(unittest.TestCase):
    def test_sma_none_until_full_then_mean(self):
        self.assertEqual(sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])
    def test_window_holding_none_is_none(self):
        self.assertEqual(sma([None, 2, 3], 2), [None, None, 2.5])
    def test_stdev_is_population(self):
        self.assertEqual(stdev([2, 4, 4, 4, 5, 5, 7, 9], 8)[-1], 2.0)
    def test_rolling_median_odd_and_even(self):
        self.assertEqual(rolling_median([5, 1, 3, 2], 3), [None, None, 3, 2])
        self.assertEqual(rolling_median([1, 2, 3, 4], 4)[-1], 2.5)
    def test_prefix_stability(self): ...  # f(x[:k])[k-1] == f(x)[k-1] for 20 seeded random k, each function
```

- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_pine_indicators -v`. Expect `ImportError: cannot import name 'sma'`.
- [ ] **Step 3: Implement** the three functions with the existing `_rolling` helper pattern and `statistics.fmean`, `statistics.pstdev` and `statistics.median`. Keep `_require_period(n)`.
- [ ] **Step 4: Run** the same command. Expect all tests to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: add SMA, population stdev and rolling median to pine_indicators"` (both files plus the TDD doc).

### Task 2: Modes configuration (YAML)

**Files:**
- Create: `config/modes.yaml` (the block of spec §6.3, verbatim), `src/bot/strategy/modes/__init__.py`, `src/bot/strategy/modes/config.py`
- Modify: `requirements.txt` and `pyproject.toml` `[project].dependencies` (add `PyYAML>=6.0`)
- Test: `tests/test_modes_config.py`

**Interfaces:**
- Produces:
  - `ModesConfigError(ValueError)`. Its message names the full key path (e.g. `umum.extra`).
  - Frozen dataclasses whose leaf field names equal the YAML keys:
    - `GeneralConfig` (block `umum`)
    - `SelectorConfig` (`pemilih_mode`)
    - `PairConfig(aktif: bool, faktor_k: float | None, harus_sama_kondisi_dengan: str | None = None)`. `faktor_k=None` means `otomatis`.
    - `PullbackConfig(aktif, tf_pemicu, ema_pemicu, konfirmasi_dalam_candle, maks_jarak_sma20_1h_kali_atr, tp_persen, sl_persen, batas_waktu_menit)`. The YAML's `maks_jarak_di_atas_…` (A) and `maks_jarak_di_bawah_…` (D2) map to `maks_jarak_sma20_1h_kali_atr`.
    - `BreakoutConfig`, `SidewaysConfig`, `NearMeanConfig`. `NearMeanConfig` holds Mode D's keys from §6.3.
    - `TestConfig(latihan_mulai, latihan_selesai, ujian_selesai: datetime (aware), lolos_hasil_per_1_juta_lebih_dari: float, lolos_minimal_posisi: int, lolos_buang_posisi_teratas: int, lolos_hasil_tanpa_teratas_lebih_dari: float, pindah_5m_jika_entry_per_hari_naik_kurang_dari: float, hari_naik_minimal_jam_naik: int)`
  - `ModesConfig(umum, pemilih_mode, pair: Mapping[str, PairConfig], mode_a: PullbackConfig, mode_b: BreakoutConfig, mode_c: SidewaysConfig, mode_d: NearMeanConfig, mode_d2: PullbackConfig, uji: TestConfig)`
  - `parse_modes_config(doc: Mapping[str, Any]) -> ModesConfig`
  - `load_modes_config(path: Path) -> ModesConfig`
  - `config_sha256(path: Path) -> str`: the file's bytes.

- [ ] **Step 1: Write the failing tests**

```python
REPO_CONFIG = Path(__file__).resolve().parents[1] / "config" / "modes.yaml"
class ModesConfigTests(unittest.TestCase):
    def test_repo_config_holds_the_document_values(self):
        c = load_modes_config(REPO_CONFIG)
        self.assertEqual((c.mode_a.tp_persen, c.mode_a.sl_persen, c.mode_a.batas_waktu_menit), (0.40, 0.25, 90))
        self.assertEqual((c.mode_a.tf_pemicu, c.mode_a.ema_pemicu, c.mode_a.konfirmasi_dalam_candle), ("15m", 20, 3))
        self.assertEqual((c.mode_d.tf_pemicu, c.mode_d.candle_jendela, c.mode_d.maks_jarak_ke_ema_kali_atr), ("3m", 39, 0.75))
        self.assertEqual((c.mode_d.tp_persen, c.mode_d.sl_persen, c.mode_d.batas_waktu_menit), (0.20, 0.20, 60))
        self.assertEqual((c.umum.fee_taker_persen, c.umum.slippage_persen, c.umum.notional_usdt), (0.035, 0.01, 25000))
        self.assertEqual(c.umum.jendela_entry_mingguan, "mon 07:00-sat 00:00")
        self.assertIsNone(c.pair["ETH-USDT"].faktor_k)
        self.assertEqual(c.uji.ujian_selesai, datetime(2026, 10, 14, tzinfo=timezone(timedelta(hours=7))))
    def test_unknown_key_refuses_and_names_its_path(self): ...      # umum.extra -> "umum.extra" in the message
    def test_missing_key_refuses(self): ...                         # drop mode.A_pullback_long.sl_persen
    def test_ai_may_not_open_or_close(self): ...                    # ai_boleh_buka_tutup: true
    def test_bool_is_not_a_number(self): ...                        # tp_persen: true
    def test_bad_weekly_window_or_naive_date_refuses(self): ...
```

- [ ] **Step 2: Run** `.venv/bin/pip install -r requirements.txt`, then `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_config -v`. Expect `ModuleNotFoundError: bot.strategy.modes`.
- [ ] **Step 3: Implement** `config.py`.
  - Walk the mapping key by key. Any extra or missing key, and any wrong type, raises with the path. `int` is accepted for float fields; `bool` never counts as a number.
  - Percents and multipliers must be > 0. `tf_pemicu` must be `15m` or `5m` for A and D2, `3m` for D. `faktor_k` must be ≥ 1.0 or `"otomatis"`. `harus_sama_kondisi_dengan` must name a listed pair.
  - The window must parse non-empty with `bot.core.entry_window.parse_weekly_window`. `uji` dates are quoted ISO strings with an offset.
  - `ai_boleh_buka_tutup` must be `false`.
- [ ] **Step 4: Run** the tests. Expect all to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: load and validate the LONG module YAML configuration"`.

### Task 3: Mode selector

**Files:**
- Create: `src/bot/strategy/modes/regime.py`
- Test: `tests/test_modes_regime.py`

**Interfaces:**
- Produces: `SIDEWAYS, NAIK, TURUN, TIDAK_ENTRY` (string constants equal to their names) and `pilih_mode(sma20_1h, sma50_1h, atr14_1h, close_4h, ema50_4h, sideways_jarak_kali_atr) -> str`. Every input is `Optional[float]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_sideways_when_gap_is_below_the_band(self):
    self.assertEqual(pilih_mode(100.1, 100.0, 1.0, 1.0, 2.0, 0.2), SIDEWAYS)  # checked before the trend
def test_gap_equal_to_the_band_is_not_sideways(self):
    self.assertEqual(pilih_mode(101.0, 100.0, 5.0, 3.0, 2.0, 0.2), NAIK)      # strict <
def test_naik_needs_the_4h_close_above_its_ema(self):
    self.assertEqual(pilih_mode(102.0, 100.0, 1.0, 2.0, 2.0, 0.2), TIDAK_ENTRY)
def test_turun_ignores_the_4h_filter(self):
    self.assertEqual(pilih_mode(98.0, 100.0, 1.0, 9.0, 1.0, 0.2), TURUN)
def test_any_missing_input_is_tidak_entry(self): ...  # each of the five inputs set to None in turn
```

- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_regime -v`. Expect an import failure.
- [ ] **Step 3: Implement** `pilih_mode`. Return `TIDAK_ENTRY` when any input is `None`; otherwise transcribe the document's function (spec §2.2) line for line.
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: the manager's mode selector as a pure function"`.

### Task 4: Backtest package, data loading and copy check

**Files:**
- Create: `src/research/mode_backtest/__init__.py`, `src/research/mode_backtest/data.py`, `tests/mode_backtest_helpers.py`
- Test: `tests/test_mode_backtest_data.py`, `tests/test_mode_backtest_structure.py`

**Interfaces:**
- Consumes: `research.pine_gate.bars` (`load_days`, `read_day`, `bars_from_rows`, `interval_to_ms`, `day_path`, `warmup_path`, `MissingDayError`, `Bars`) and `research.pine_gate.cli_common.file_sha256`.
- Produces:
  - `SOURCE = "prod"`
  - `PairData(symbol: str, bars_1m: Bars, warmup_1h: Bars, warmup_4h: Bars)`
  - `stage_files(data_dir, symbol, first_day: date, last_day: date) -> list[Path]`: the day files plus both warm-ups, sorted.
  - `manifest(data_dir, paths) -> list[str]`: `"relpath:sha256"` lines, sorted.
  - `manifest_sha256(lines) -> str`
  - `load_pair(data_dir, symbol, first_day, last_day) -> PairData`. A missing day raises `MissingDayError` before anything is read; a missing warm-up raises `FileNotFoundError`.
  - `verify_copy(data_dir, sha256sum_text: str) -> list[str]`: one problem per mismatching or missing file. The input lines are `<hex>  prod/<symbol>/<interval>/<file>`.
- Helpers (tests only): `minute_rows(start_ms, n, price=lambda i: 100.0)` and `write_days(data_dir, symbol, days, price)`, which write valid 1,440-bar WIB day files, plus `write_warmups(...)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_load_pair_reads_only_the_requested_days`: a corrupt file after `last_day` must not matter.
  - `test_missing_day_raises_before_reading`
  - `test_missing_warmup_raises`
  - `test_manifest_is_relative_sorted_and_changes_with_one_byte`
  - `test_verify_copy_reports_every_mismatch_and_missing_file`

  Structure tests, run on the package's AST:
  - `test_no_module_imports_a_private_name_of_another`
  - `test_no_network_or_venue_code`: none of `httpx`, `socket`, `urllib`, `requests`, `bingx_client`, `_signed_request`, `X-BX-APIKEY` or `listenKey` may appear.
  - `test_files_are_written_only_through_fileio`: no `write_text`, `write_bytes` or `open(..., "w"/"a"/"x")`.
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_data tests.test_mode_backtest_structure -v`. Expect an import failure.
- [ ] **Step 3: Implement** `data.py`. Read warm-ups with `bars_from_rows(read_day(warmup_path(...)), interval_to_ms(iv))`.
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: data loading and copy verification for the mode backtest"`.

### Task 5: Hourly frame, condition at a time, NAIK days

**Files:**
- Create: `src/research/mode_backtest/frames.py`
- Modify: `tests/test_pine_gate_structure.py`. `_bot_sources_imported_by_research` walks `PACKAGE` (`research/pine_gate`), not every research package; update its docstring.
- Test: `tests/test_mode_backtest_frames.py`

**Interfaces:**
- Consumes: Task 1 (`sma`, `rolling_median`) plus `pine_indicators.ema` and `atr`; Task 3 `pilih_mode`; Task 4 `PairData`; `research.pine_gate.bars.last_closed_index` and `research.pine_gate.features.htf_bars`.
- Produces:
  - `ATR_MEDIAN_HOURS = 720` (30 days, document).
  - `HourlyContext(sma20_1h, sma50_1h, atr14_1h, atr_median, close_4h, ema50_4h: Optional[float], kondisi: str)`, frozen.
  - `HourlyFrame.build(pair: PairData, sel: SelectorConfig) -> HourlyFrame`
  - `HourlyFrame.context_at(t_ms) -> HourlyContext`: the last closed 1h and 4h bars at `t`.
  - `HourlyFrame.naik_days(start_ms, end_ms, window: WeeklyWindow | None) -> frozenset[date]`: the WIB day of every closed 1h candle whose close lies in [start, end) and in the window, and whose `context_at(close).kondisi == NAIK`.

- [ ] **Step 1: Write the failing tests**
  - `test_context_at_hour_close_uses_the_hour_just_closed_and_never_the_forming_one`
  - `test_four_hour_buckets_close_at_03_07_11_15_19_23_wib`
  - `test_warmup_seam_makes_the_first_day_warm`
  - `test_atr_median_needs_720_values`
  - `test_condition_is_tidak_entry_while_inputs_are_none`
  - `test_naik_days_count_only_window_hours`: a Monday whose only NAIK close is 06:00 WIB is excluded, 07:00 is included, and Saturday is excluded.
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_frames tests.test_pine_gate_structure -v`. Expect an import failure, and the Pine fingerprint test failing on `bot/strategy/modes/regime.py`.
- [ ] **Step 3: Implement** `HourlyFrame`. Compute each series once at build time; `context_at` only looks values up with `last_closed_index`. Then make the one-line scope change in the Pine structure test.
- [ ] **Step 4: Run** the same command, then the whole suite. Expect everything to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: hourly frame and mode selector context for the mode backtest"`.

### Task 6: Step 0 — replay the Pine holdout, check, split

**Files:**
- Create: `src/research/mode_backtest/step0.py`, `src/research/mode_backtest/report.py` (step-0 table now), `src/research/mode_backtest/cli.py`, `src/research/mode_backtest/__main__.py`
- Test: `tests/test_mode_backtest_step0.py`, `tests/test_mode_backtest_cli.py`

**Interfaces:**
- Consumes, all public names of `research.pine_gate`:
  - `dataset.load_dataset`
  - `holdout_replay.{Holdout, HoldoutPeriod, holdout_markets, build_contenders, trades_of, symbol_metrics}`
  - `pipeline.purge_tick_minutes`
  - `spec.{ExecConfig, GuardConfig, GateConfig}`
  - `cli_common.{load_calibration_file, read_json, file_sha256}`
  - `simulate.Trade`, which has `symbol`, `side`, `signal_ms` and `net_usdt`.

  Also Task 2 `load_modes_config`, Task 4 `load_pair` and Task 5 `HourlyFrame`.
- Produces:
  - `PINE_FILES = {"selection": "pine_gate_selection_20261016.json", "calibration": "pine_gate_calibration_20261016.json", "holdout": "pine_gate_holdout_20261016.json"}`
  - `ReproductionError(RuntimeError)`
  - `check_inputs(pine_dir, holdout_doc, dataset) -> None`. It checks:
    - the selection file's sha256 against `selection_sha256`;
    - `configs.exec`, `configs.guard` and `configs.gate` against `asdict` of the spec defaults;
    - `dataset.data_sha256` against `data_sha256`.
  - `replay_main_runs(data_dir, pine_dir) -> tuple[dict[str, list[Trade]], Holdout]`. It runs `h.run(c.signal)` for every contender of `build_contenders` with seed 0, on a `Holdout` assembled as the Pine test stage does.
  - `reproduction_mismatches(replayed: Mapping[str, Mapping[str, Mapping[str, Any]]], holdout_doc) -> list[str]`: `"c1 BTC-USDT wr: 48.1 != 48.2"` style lines, over every `symbol_metrics` key.
  - `Step0Row(contender, symbol, sisi, kondisi, posisi, menang_pct, gross_bps, hasil_per_1_juta)`
  - `split_rows(trades_by_contender, kondisi_at: Callable[[str, int], str]) -> list[Step0Row]`. Hasil per 1 juta = 1e6 × Σ`net_usdt` / (n × 2 × 25,000).
  - `run_step0(data_dir, pine_dir, config_path, out_dir, now) -> Path`. It writes `step0_<YYYYMMDD>.json` and `.md`, exclusively.
  - `report.step0_table(rows) -> str`, with Indonesian headers: Kontender | Koin | Arah | Kondisi | Posisi | Menang | Rata-rata bps | Hasil per 1 juta.
  - CLI: `python -m research.mode_backtest step0 --data-dir --pine-dir --config --out-dir` and `verify-copy --data-dir --sha256sum`.

- [ ] **Step 1: Write the failing tests**
  - `test_identical_metrics_have_no_mismatch`
  - `test_one_changed_field_is_reported_and_stops_step0`: patch `replay_main_runs`. Expect `ReproductionError`, and no file in `out_dir`.
  - `test_split_rows_groups_by_side_and_condition`: synthetic `Trade`s and a fake `kondisi_at`.
  - `test_check_inputs_refuses_a_different_selection_file`
  - `test_cli_unknown_command_exits_2`
  - `test_cli_missing_pine_file_exits_1_with_one_error_line`
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_step0 tests.test_mode_backtest_cli -v`. Expect an import failure.
- [ ] **Step 3: Implement** `step0.py`, the step-0 table and the CLI.
  - Build the `Holdout` from `load_dataset(data_dir, None)`, `holdout_markets(dataset, exec_cfg)`, `HoldoutPeriod.spec()`, `load_calibration_file`, seed 0, `days=tuple(period.days())`, `last20_days=tuple(period.last20_days())` and `purge_tick_minutes(calib, exec_cfg)`, with `exec_cfg`, `guard_cfg` and `gate_cfg` the spec defaults (`ExecConfig()`, `GuardConfig()`, `GateConfig()`), the same ones `check_inputs` compares with the holdout file.
  - `kondisi_at(symbol, t)` uses `HourlyFrame.context_at(t).kondisi` on frames from `load_pair` over 2025-08-01 → 2026-10-13.
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: step 0 replays the Pine holdout and splits it by side and condition"`.

### Task 7: Run step 0 and write its report (operational)

- [ ] **Step 1: Copy the data, read-only on the VPS:**

```bash
mkdir -p ~/replay/mode_backtest/{data,pine_phase1,out}
ssh 0xAlvary2 'cd ~/replay/pine_gate/data && find prod -type f | sort | xargs sha256sum' > ~/replay/mode_backtest/vps_sha256sum.txt
scp -r 0xAlvary2:replay/pine_gate/data/prod ~/replay/mode_backtest/data/
scp 0xAlvary2:replay/out/pine_gate_selection_20261016.json 0xAlvary2:replay/out/pine_gate_calibration_20261016.json 0xAlvary2:replay/out/pine_gate_holdout_20261016.json ~/replay/mode_backtest/pine_phase1/
```

- [ ] **Step 2: Verify:** `PYTHONPATH=src .venv/bin/python -m research.mode_backtest verify-copy --data-dir ~/replay/mode_backtest/data --sha256sum ~/replay/mode_backtest/vps_sha256sum.txt` should print 0 problems.
- [ ] **Step 3: Run:** `PYTHONPATH=src .venv/bin/python -m research.mode_backtest step0 --data-dir ~/replay/mode_backtest/data --pine-dir ~/replay/mode_backtest/pine_phase1 --config config/modes.yaml --out-dir ~/replay/mode_backtest/out`. Expect exit 0. If the reproduction fails, rerun with `PYTHONPATH` pointing at the frozen Phase 1 code (`~/replay/pine_gate_repo/src.phase1-20261016-212600`, copied the same way) and record that in the report.
- [ ] **Step 4: Write** `docs/research/<run date>-modul-long-langkah0.md` in Indonesian: the table, which data and code produced it, and the spec §8 caveat (Pine exits and clock). Commit `git commit -m "docs: step 0 split of the Pine holdout by side and condition"`.
- [ ] **Step 5: Checkpoint.** Show the user the step 0 table before Task 8.

### Task 8: Mode A tracker

**Files:**
- Create: `src/bot/strategy/modes/pullback.py`
- Test: `tests/test_modes_pullback.py`

**Interfaces:**
- Produces:
  - `Candle(close_ms: int, open: float, high: float, low: float, close: float)`, frozen.
  - `PullbackSignal(close_ms: int)`
  - `PullbackTracker(confirm_within: int, max_distance_atr: float)` with:
    - `on_candle(candle, ema: Optional[float], sma20_1h: Optional[float], atr14_1h: Optional[float]) -> Optional[PullbackSignal]`
    - `consume() -> None`, which drops every touch whose window holds the last signal's candle (D5).
  - A touch at candle `i` covers candles `i … i + confirm_within − 1`. Touches are tracked even when no signal can follow.

- [ ] **Step 1: Write the failing tests**
  - `test_touch_and_confirmation_in_one_candle`
  - `test_confirmation_on_the_third_candle_but_not_the_fourth`
  - `test_low_equal_to_ema_is_a_touch_and_close_equal_to_ema_is_not_a_confirmation`
  - `test_red_candle_never_confirms`
  - `test_distance_cap_is_inclusive`: a close equal to `sma20 + 1.0 × atr` signals; a close just above it does not.
  - `test_one_entry_per_touch`: after `consume()`, a later green close in the same window gives no signal.
  - `test_blocked_signal_leaves_the_touch_available`: without `consume()`, the next confirming candle in the window signals.
  - `test_missing_inputs_never_signal`
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_pullback -v`. Expect an import failure.
- [ ] **Step 3: Implement** the tracker. Keep the open touch indices in a small list, and expire them when the current index passes `i + confirm_within − 1`.
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: Mode A pullback tracker with one entry per touch"`.

### Task 9: Trigger series and the Mode D trigger

**Files:**
- Create: `src/bot/strategy/modes/near_mean.py`
- Modify: `src/research/mode_backtest/frames.py`
- Test: `tests/test_modes_near_mean.py`, and new tests in `tests/test_mode_backtest_frames.py`

**Interfaces:**
- Consumes: `bot.strategy.indicators.ema` and `atr`, `bot.arena.setup_profile.build_setup_profile`, `research.pine_gate.bars.aggregate`, `pine_indicators.ema`, Task 5 `HourlyFrame`.
- Produces:
  - `near_mean.stretch_from_window(highs, lows, closes, ema_period, atr_period) -> Optional[float]`. It returns `build_setup_profile({"last_price": closes[-1], "ema_21": ema(closes, ema_period), "atr_14": atr(highs, lows, closes, atr_period)}, "LONG")["stretch_atr"]`, the live gate's maths.
  - `near_mean.near_mean_short(stretch_atr: Optional[float], max_abs: float) -> bool`
  - `frames.TriggerSeries(bars: Bars, ema: Series)` and `frames.trigger_series(bars_1m, tf: str, ema_period: int) -> TriggerSeries`
  - `frames.stretch_series(bars_1m, window: int, ema_period: int, atr_period: int) -> tuple[Bars, Series]`, over 3m bars; `None` for the first `window − 1` bars.
  - `frames.PairFrames(symbol, bars_1m, hourly: HourlyFrame, triggers: Mapping[str, TriggerSeries], stretch_bars: Bars, stretch: Series)` and `frames.build_pair_frames(pair: PairData, cfg: ModesConfig) -> PairFrames`. It builds the `15m` and `5m` triggers with `ema_pemicu`, and the stretch with the `mode_d` keys.

- [ ] **Step 1: Write the failing tests**
  - `test_near_mean_bounds_are_inclusive`: 0.75 and −0.75 are True; 0.7500001 and `None` are False.
  - `test_stretch_matches_the_live_maths_on_39_bars`
  - `test_stretch_series_matches_pine_feature_frame`: compare with `FeatureFrame.build(sym, bars_1m, w1h, w4h).snapshot(t).stretch_3m` at five times.
  - `test_trigger_ema_is_aligned_to_closed_candles`: no value before 160 15m bars, and the value at bar `i` uses closes up to `i`.
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_near_mean tests.test_mode_backtest_frames -v`. Expect an import failure.
- [ ] **Step 3: Implement** these, reusing the 39-bar window loop shape of the Pine stretch column.
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: trigger series and the near-mean SHORT trigger"`.

### Task 10: Position rules

**Files:**
- Create: `src/bot/strategy/modes/position_rules.py`
- Test: `tests/test_modes_position_rules.py`

**Interfaces:**
- Produces:
  - `Bracket(tp: float, sl: float)` and `bracket_prices(entry_fill, side, tp_pct, sl_pct) -> Bracket`, where tp = entry × (1 + side·tp/100) and sl = entry × (1 − side·sl/100).
  - `adverse_fill(price, side, slippage_pct, closing: bool) -> float`
  - `entry_window_open(window: WeeklyWindow | None, t_ms) -> bool`: the WIB datetime through `window_is_open`.
  - `pause_over(t_ms, last_exit_ms: Optional[int], pause_min) -> bool`
  - `under_daily_caps(pair_today, total_today, max_pair, max_total) -> bool`
  - `volatility_ok(atr14_1h: Optional[float], atr_median: Optional[float], mult) -> bool`: False when either is `None` or `atr > mult × median`.

- [ ] **Step 1: Write the failing tests**
  - `test_brackets`: LONG from 100 gives tp 100.4 and sl 99.75; SHORT from 100 at 0.20/0.20 gives tp 99.8 and sl 100.2, with `assertAlmostEqual`.
  - `test_adverse_fills`: a LONG open is × 1.0001 and a LONG close × 0.9999; SHORT is mirrored.
  - `test_window_edges`: Mon 06:59 WIB is closed and 07:00 open; Fri 23:59 is open and Sat 00:00 closed.
  - `test_pause_of_exactly_15_minutes_is_over`
  - `test_daily_caps`
  - `test_volatility_guard_equal_to_twice_the_median_passes`
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_position_rules -v`. Expect an import failure.
- [ ] **Step 3: Implement** these. Convert to WIB with `datetime.fromtimestamp(t_ms / 1000, ZoneInfo("Asia/Jakarta"))`.
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: position rules for the LONG module"`.

### Task 11: Exit walk and trade money

**Files:**
- Create: `src/research/mode_backtest/simulate.py` (exit part)
- Test: `tests/test_mode_backtest_simulate.py`

**Interfaces:**
- Consumes: Task 10 (`adverse_fill`).
- Produces:
  - `ExitOutcome(reason: str, exit_idx: int, raw_price: float, gapped: bool, both_touched: bool)`, where `reason` is `TP`, `SL` or `WAKTU`.
  - `resolve_exit(bars_1m, entry_idx, side, tp, sl, limit_min) -> Optional[ExitOutcome]`. It returns `None` when the data ends first.
  - `Money(entry_fill, exit_fill, qty, fee_usdt, slippage_usdt, pnl_usdt, volume_usdt)` and `settle(entry_open, raw_exit, side, notional, fee_pct, slippage_pct) -> Money`:
    - `qty = notional / entry_fill`
    - fee = fee_pct/100 × (qty·entry_fill + qty·exit_fill)
    - pnl = side·qty·(exit_fill − entry_fill) − fee
    - slippage = the cost of both adjustments
    - volume = qty·entry_fill + qty·exit_fill

- [ ] **Step 1: Write the failing tests**
  - `test_sl_wins_a_bar_that_touches_both`, with `both_touched` True.
  - `test_bar_opening_beyond_sl_exits_at_its_open`, and the same for TP. `gapped` is True.
  - `test_time_exit_at_the_open_of_minute_90_before_checking_levels`
  - `test_the_entry_bar_range_counts`
  - `test_data_ending_first_returns_none`
  - `test_money_on_the_reference_trades`: from an open of 100 with 25,000 notional, 0.035 % fee and 0.01 % slippage, compare to 6 decimals:
    - TP +0.40: entry_fill 100.01, gross 97.490000, fee 17.534122, pnl 79.955879, slippage 5.009750, volume 50097.490000.
    - SL −0.25: pnl −82.471002, fee 17.477252, volume 49935.006250.
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_simulate -v`. Expect an import failure.
- [ ] **Step 3: Implement** `resolve_exit` and `settle`. The order inside a bar is: time → open gap at SL → open gap at TP → SL touch → TP touch.
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: 1m exit walk and trade money for the mode backtest"`.

### Task 12: The simulation loop for Mode A and Mode D

**Files:**
- Modify: `src/research/mode_backtest/simulate.py`
- Test: `tests/test_mode_backtest_simulate.py` (additions)

**Interfaces:**
- Consumes: Tasks 2, 5, 8, 9, 10 and 11, and `research.pine_gate.bars.entry_index_for`.
- Produces:
  - `RunSpec(mode: str, pair: str, tf: str, kondisi_gate: str, start_ms: int, end_ms: int)`, where `mode` is `"A"` or `"D"`.
  - `Trade`: every §2.9 log field. It holds `mode`, `pair`, `side`, `kondisi`, `context: HourlyContext`, `faktor_k`, `signal_ms`, `entry_ms`, `entry_fill`, `tp`, `sl`, `exit_ms`, `exit_fill`, `exit_reason`, `gapped`, `both_touched`, `qty`, `notional_usdt`, `fee_usdt`, `slippage_usdt`, `pnl_usdt` and `volume_usdt`.
  - `RunResult(trades: tuple[Trade, ...], blocked: Mapping[str, int])`
  - `simulate_run(frames: PairFrames, cfg: ModesConfig, run: RunSpec) -> RunResult`. The loop reads conditions only through `frames.hourly.context_at(t)`, so tests may pass a stub.
  - The gate order and the block-reason keys are `window`, `segment_edge`, `kondisi`, `position_open`, `pause`, `daily_cap` and `volatility`.

- [ ] **Step 1: Write the failing tests** (a stub hourly frame with a fixed context; synthetic 1m bars from the Task 4 helpers):
  - `test_mode_a_enters_at_the_next_open_after_confirmation`
  - `test_no_entry_outside_the_condition_gate`
  - `test_open_position_blocks_and_the_touch_survives_until_after_the_pause` (D5)
  - `test_daily_cap_resets_at_wib_midnight_not_utc`
  - `test_weekend_signals_are_blocked`
  - `test_segment_edge_blocks_a_signal_whose_time_limit_passes_the_end`: for A, `end − 89 min` is blocked and `end − 90 min` is allowed.
  - `test_volatility_guard_blocks`
  - `test_mode_d_comparator_shorts_only_in_naik`
  - `test_blocked_reasons_are_counted`
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_simulate -v`. Expect `ImportError: cannot import name 'simulate_run'`.
- [ ] **Step 3: Implement** `simulate_run`.
  - Iterate the trigger bars whose close falls in [start, end): `triggers[tf]` for A, `stretch_bars` for D.
  - Feed the tracker on every candle.
  - On a signal, apply the gates in §9.1 order. On pass, enter on the 1m bar whose `open_ms == t`, resolve, settle, record, and call `consume()` for A.
  - Count entries per WIB day of `t`.
  - `faktor_k` comes from `cfg.pair[pair].faktor_k`.
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: event loop for Mode A and the Mode D comparator"`.

### Task 13: Metrics, trade log and the Mode A table

**Files:**
- Create: `src/research/mode_backtest/metrics.py`, `src/research/mode_backtest/tradelog.py`
- Modify: `src/research/mode_backtest/report.py`
- Test: `tests/test_mode_backtest_metrics.py`, `tests/test_mode_backtest_tradelog.py`

**Interfaces:**
- Produces:
  - `StageMetrics(posisi, entry_per_hari_aktif, menang_pct, hasil_per_1_juta, tp, sl, waktu, rata_lama_menit, hasil_tanpa_teratas, hari_naik)`
  - `stage_metrics(trades, hari_naik: int, drop_top: int) -> StageMetrics`
  - `pass_status(m, uji: TestConfig) -> str`, returning `"LOLOS"`, `"GAGAL"` or `"BELUM CUKUP SAMPEL"`
  - `wants_5m(m15: StageMetrics, uji: TestConfig) -> bool`
  - `tradelog.COLUMNS`: the 23 names of spec §2.9, in order.
  - `wib_iso(t_ms) -> str` (`2026-08-05T07:15:00+07:00`)
  - `trade_log_text(trades) -> str`: CSV with header and `\n`, `kondisi`, `sisi` LONG/SHORT, `alasan_exit` TP/SL/WAKTU.
  - `write_trade_log(path, trades)`, via `write_exclusive(..., "trade log")`.
  - `report.ReportRow(mode, pair, tahap, metrics)` and `report.mode_table(rows) -> str`: the §2.9 header, Indonesian decimal comma. After the comparator rows of each tahap, the table also renders a "Tidak trading" row (0 positions, Hasil per 1 juta 0).

- [ ] **Step 1: Write the failing tests**
  - `test_hasil_per_1_juta`: pnl 10 and −40 on 50,000 volume each gives −300.
  - `test_menang_excludes_time_exits`
  - `test_without_top_three_drops_the_best_and_breaks_ties_by_time`
  - `test_statuses`:
    - n = 39 with good numbers → BELUM CUKUP SAMPEL;
    - n = 40, −149 / −249 → LOLOS;
    - Hasil per 1 juta exactly −150 → GAGAL;
    - without top 3 exactly −250 → GAGAL.
  - `test_wants_5m_below_three_only`: 2.99 → True, 3.0 → False.
  - `test_log_header_equals_the_document`, `test_log_times_are_wib`, `test_table_header_equals_the_document`
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_metrics tests.test_mode_backtest_tradelog -v`. Expect an import failure.
- [ ] **Step 3: Implement** these.
- [ ] **Step 4: Run** the tests. Expect them to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: metrics, pass rule, trade log and the mode table"`.

### Task 14: Lock and the train/test stages

**Files:**
- Create: `src/research/mode_backtest/lock.py`, `src/research/mode_backtest/stages.py`
- Modify: `src/research/mode_backtest/cli.py`
- Test: `tests/test_mode_backtest_lock.py`, plus additions to `tests/test_mode_backtest_cli.py` and `tests/test_mode_backtest_structure.py`

**Interfaces:**
- Produces:
  - `lock.LockError(RuntimeError)`
  - `lock.code_files() -> list[tuple[str, str]]`: every `research/mode_backtest/*.py`, every `bot/strategy/modes/*.py`, and `CODE_EXTRA` (each other `bot.*` and `research.pine_gate.*` module the package imports).
  - `lock.code_sha256() -> str`
  - `lock.lock_path(out_dir, mode, pair)`: `mode_A_BTC-USDT_lock.json`
  - `lock.check_lock(doc, *, config_sha, code_sha, train_manifest_sha, python) -> None`. Its `LockError` lists every mismatch.
  - `lock.marker_path(data_dir, mode, pair, lock_sha)`: `holdout_runs/mode_A_BTC-USDT_<sha>.json`
  - `lock.claim_marker(path, doc) -> None`. It writes exclusively; an existing marker is `LockError("already ran")`.
  - `stages.train_stage(config_path, data_dir, out_dir, mode, pair, now) -> Path` and `stages.test_stage(config_path, data_dir, out_dir, mode, pair, now) -> Path`
  - CLI: `train` and `test`, each with `--mode A --pair BTC-USDT --config --data-dir --out-dir`. A Phase 1 refusal for any other mode or pair exits 1.

- [ ] **Step 1: Write the failing tests** (with a temporary YAML whose `uji` dates span a few synthetic days):
  - `test_train_never_opens_a_test_day_file`: a corrupt test-period file must not break `train`.
  - `test_train_writes_results_logs_and_an_exclusive_lock`
  - `test_five_minute_switch_runs_once_and_is_locked`: patch `wants_5m` to True.
  - `test_test_refuses_on_a_changed_config_code_or_training_byte`, exit 2 from the CLI.
  - `test_marker_is_written_before_any_test_day_is_read`
  - `test_second_test_run_refuses`
  - `test_outputs_are_never_overwritten`
  - Structure: `test_fingerprint_covers_every_bot_and_pine_gate_module_the_package_imports`
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_lock tests.test_mode_backtest_cli tests.test_mode_backtest_structure -v`. Expect an import failure.
- [ ] **Step 3: Implement** `lock.py` and `stages.py`.
  - **train:** load days `latihan_mulai … latihan_selesai − 1 day`. Run A at `tf_pemicu`, then the Mode D comparator with `kondisi_gate=NAIK`. If `wants_5m`, run A once at `5m`. Write:
    - `mode_A_BTC-USDT_train_<YYYYMMDD>.json`: metrics, NAIK days, blocked counts and both timeframes when run;
    - one CSV per run;
    - the lock, which binds the config sha, code sha, training manifest sha, Python major.minor, the chosen tf and the summary.
  - **test:** in order:
    1. read the lock;
    2. hash the training files only;
    3. `check_lock`;
    4. `claim_marker`;
    5. load the days through `ujian_selesai − 1 day`;
    6. run A (locked tf) and the comparator over [latihan_selesai, ujian_selesai);
    7. write `..._test_<YYYYMMDD>.json`, the CSVs and the `.md` table with `pass_status`.
- [ ] **Step 4: Run** the tests, then the whole suite. Expect everything to pass.
- [ ] **Step 5: Commit** `git commit -m "feat: lock and one-shot train/test stages for the mode backtest"`.

### Task 15: Documentation and full verification

**Files:**
- Modify: `CLAUDE.md`. Under "Offline research", add a `research.mode_backtest` entry: what it is, the core/harness split, the stages and the one-shot marker. Add the commands to the Commands block.
- Modify: `.github/tdd/long-module-phase1.tdd.md`, with the verification numbers.

- [ ] **Step 1: Run**
  - `PYTHONPATH=src .venv/bin/python -m compileall src tests`
  - `PYTHONPATH=src .venv/bin/python -m coverage run -m unittest discover -s tests`
  - `PYTHONPATH=src .venv/bin/python -m coverage report` (expect ≥ 80 %)
  - `git diff --check`
  - `.venv/bin/pip check`

  Record each result in the TDD doc.
- [ ] **Step 2: Commit** `git commit -m "docs: LONG module phase 1 in CLAUDE.md and TDD evidence"`.
- [ ] **Step 3: Ask the user** before pushing the branch and opening the PR. Merge only after CI is green and the review is done.

### Task 16: Run Mode A on BTC (operational, on merged `main`)

- [ ] **Step 1:** Re-run `verify-copy` on `~/replay/mode_backtest/data`. Expect 0 problems.
- [ ] **Step 2:** `PYTHONPATH=src .venv/bin/python -m research.mode_backtest train --mode A --pair BTC-USDT --config config/modes.yaml --data-dir ~/replay/mode_backtest/data --out-dir ~/replay/mode_backtest/out`. Expect exit 0 and a lock file.
- [ ] **Step 3: Checkpoint.** Show the user the training table, the 15m/5m decision and the comparator. Get their go-ahead for the one-shot test.
- [ ] **Step 4:** Run the same command with `test`. Expect exit 0 and a status line (LOLOS, GAGAL or BELUM CUKUP SAMPEL).
- [ ] **Step 5:** Write `docs/research/<run date>-modul-long-mode-a-btc.md` in Indonesian: the §2.9 table (Latihan and Ujian), the comparators, the status with its three numbers, the lock and marker shas, and the spec §11 caveats. Ask the user before pushing it as a docs PR.
