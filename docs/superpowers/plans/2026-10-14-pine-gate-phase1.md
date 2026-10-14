# Pine-adapted gate — Phase 1 (offline evaluation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run the pre-registered offline evaluation of spec §5 — data, indicators, three contenders, a locked holdout, controls and an Indonesian report — without changing the live bot.

**Architecture:** A tested research package `src/research/pine_gate/` (never imported by `bot`) plus the unwired `src/bot/strategy/pine_indicators.py`. Columnar 1m bars from BingX production public klines feed per-symbol feature frames; rules turn snapshots into a side; a tick-clock simulator replays the bot's execution over a memoised bracket-outcome cache; selection, meta-labeling and statistics are pure Python. The CLI's `train` stage writes a hashed selection file that the `test` stage requires before it reads a single holdout bar.

**Tech Stack:** Python ≥ 3.10 stdlib (`array`, `bisect`, `math`, `random`, `statistics`, `json`, `gzip`, `hashlib`, `argparse`), `httpx` (already a dependency), stdlib `unittest`. No new dependencies (no numpy).

**Spec:** `docs/superpowers/specs/2026-10-14-pine-adapted-gate-design.md` (evidence: `docs/research/2026-10-14-indicator-method-evidence.md`). This plan also applies four small spec alignments, committed with it: research code lives in the repo; the collector stores VST ETH 1m only (3m is aggregated); native BingX 1h/4h klines from 2025-02-01 warm up the 1h/4h indicators; the 8 × period warm-up applies to recursive smoothers, window functions need only a full window.

## Global Constraints

- Python 3.10-compatible (VPS) and 3.11 (CI); no new packages; `PYTHONPATH=src`; tests are stdlib `unittest` files `tests/test_pine_*.py`; global line coverage stays ≥ 80% (`fail_under = 80`).
- `bot` never imports `research`; no `bot` module imports `pine_indicators` in Phase 1; `src/bot/strategy/indicators.py` is not modified.
- No orders and no private endpoints. Hosts: production `https://open-api.bingx.com`, VST `https://open-api-vst.bingx.com`; paths `/openApi/swap/v3/quote/klines` and `/openApi/swap/v2/quote/fundingRate`; header `X-SOURCE-KEY: BX-AI-SKILL`; ≥ 1 second between requests; fixed `startTime` + `endTime` windows; never paginate on `startTime` alone.
- Timestamps are UTC milliseconds; a day is a WIB (UTC+7) calendar day; trading days are Mon–Fri WIB days; the entry window is Mon 07:00 – Sat 00:00 WIB.
- Segments (WIB): training 2025-08-01 00:00 → 2026-08-05 00:00; blocks B1 → 2025-11-01, B2 → 2026-02-01, B3 → 2026-05-04, B4 → 2026-08-05; CSCV = 8 equal training blocks; holdout 2026-08-05 00:00 → 2026-10-14 00:00; last 20 trading days 2026-09-16 → 2026-10-13. Purge: no entry in the 60 minutes before any block or segment boundary; model validation embargo: 60 minutes after a boundary.
- Execution: next tick 180 s after the previous tick finishes (a tick without an entry takes 2 s); an entry tick adds a sampled AI latency; order time = tick + latency + 5 s; entry = close of the 1m bar containing the order time, 0.25 bps adverse; bracket ±0.20% from the entry fill; the walk starts on the next 1m bar; a bar touching both levels is a stop; gaps fill at the bar open; stop fill − s\*; take-profit − 0.25 bps; time stop at the close of the 60th bar − 0.25 bps; fee 0.035% per side (sensitivity 0.05%); cooldown 5 min after each entry; one position across `BTC-USDT` and `ETH-USDT`, BTC first; per WIB day: 12 stop-losses, 15 consecutive losses, −1,500 USDT net, 34 entries, 1,000,000 volume, at 25,000 notional; funding blackout = 0–600 s before a funding time.
- Indicators: Wilder RMA (alpha 1/n, seeded with the mean of the first n values); EMA alpha 2/(n+1), same seed; recursive smoothers return `None` before 8 × period bars; window functions return `None` until the window is full.
- Holdout lock: `test` refuses to run unless a selection file exists whose `spec_sha256` equals the current spec document's sha256.
- VPS runs: CWD `~/ai-perpetual-trading` (for `.env`), `PYTHONPATH=$HOME/replay/pine_gate_repo/src`, `nice -n 19 .venv/bin/python -m research.pine_gate.cli …`; data in `~/replay/pine_gate/data/`, outputs in `~/replay/out/`. Never restart pm2 `ai-perp-volume`, never edit `.env`, never modify `~/replay/kline_keeper.py` or its cron line.
- Reports are in Indonesian; code, tests and repo docs in English.

## Review Focus

1. **Incomplete or disordered exchange days** (< 1,440 bars, duplicates, gaps, unsorted rows) are refetched or fail loudly — never stored as valid. → Task 4 `test_incomplete_day_is_retried_then_rejected`.
2. **Higher-timeframe look-ahead:** the 1h/4h bar still forming at t never feeds the snapshot at t. → Task 5 `test_snapshot_ignores_forming_hour_bar`.
3. **WIB/UTC day mix-ups** in guards, trading-day counts and the Monday 07:00 window (17:00 UTC is WIB midnight). → Task 8 `test_guard_day_rolls_at_wib_midnight`.
4. **Holdout leakage through straddling trades:** during `train`, no bar after 2026-08-05 00:00 WIB + 60 min is loaded and no entry falls in the purge. → Task 13 `test_train_stage_never_reads_bars_after_boundary_plus_hour`.
5. **Thin rules winning by luck:** a rule with < 1,000 TP+SL trades, or a block with no trades, neither crashes ranking nor ranks first. → Task 10 `test_ineligible_rule_never_selected_even_with_perfect_wr`.

---

### Task 1: Pine-compatible indicator series

**Files:**
- Create: `src/bot/strategy/pine_indicators.py`
- Test: `tests/test_pine_indicators.py`

**Interfaces:**
- Produces (all return lists aligned to the input, `None` where not yet valid; `min_bars=None` means 8 × period for smoothers):
  `rma(values, n, min_bars=None)`, `ema(values, n, min_bars=None)`, `rsi(closes, n, min_bars=None)`, `true_range(highs, lows, closes) -> list[float]`, `atr(highs, lows, closes, n, min_bars=None)`, `dmi(highs, lows, closes, di_len, adx_len, min_bars=None) -> tuple[list, list, list]` (+DI, −DI, ADX; warm-up 8 × max(di_len, adx_len)), `rolling_max(values, n)`, `rolling_min(values, n)`, `percent_rank(values, n)` (Pine `ta.percentrank`: % of the previous n values ≤ the current one).

- [ ] **Step 1: Write the failing tests**

```python
class PineIndicatorTests(unittest.TestCase):
    def test_rma_seeds_with_mean_then_wilder(self):
        self.assertEqual(rma([1, 2, 3, 4], 2, min_bars=2), [None, 1.5, 2.25, 3.125])
    def test_ema_seeds_with_mean_then_alpha(self):
        self.assertEqual(ema([1, 2, 3], 2, min_bars=2), [None, 1.5, 1.5 + (2 / 3) * (3 - 1.5)])
    def test_rsi_edges(self):
        self.assertEqual(rsi([1, 2, 3, 4, 5], 2, min_bars=2)[-1], 100.0)
        self.assertEqual(rsi([5, 4, 3, 2, 1], 2, min_bars=2)[-1], 0.0)
    def test_true_range_uses_previous_close(self):
        self.assertEqual(true_range([10, 12], [9, 11], [9.5, 11.5]), [1, 2.5])
    def test_dmi_zero_sum_gives_zero_dx_not_error(self): ...   # flat bars → ADX 0.0 after warm-up
    def test_dmi_uptrend_has_plus_above_minus(self): ...
    def test_percent_rank(self):
        self.assertEqual(percent_rank([1, 2, 3, 4, 5], 4)[4], 100.0)
        self.assertEqual(percent_rank([5, 4, 3, 2, 1], 4)[4], 0.0)
        self.assertIsNone(percent_rank([1, 2, 3], 4)[2])
    def test_rolling_max_min_none_until_full(self): ...
    def test_default_warmup_is_eight_periods(self):
        out = ema(list(range(100)), 10)
        self.assertIsNone(out[78]); self.assertIsNotNone(out[79])
    def test_prefix_stability(self):   # f(x[:k])[k-1] == f(x)[k-1] for 20 seeded random k
        ...
```

- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_indicators -v` — expect `ModuleNotFoundError: bot.strategy.pine_indicators`.
- [ ] **Step 3: Implement** the functions above in `src/bot/strategy/pine_indicators.py` (stdlib only). DMI follows Pine v5: up = high − prev high, down = prev low − low; +DM = up if up > down and up > 0 else 0 (−DM mirrored); +DI = 100 · rma(+DM)/rma(TR); DX = 100 · |+DI − −DI| / (+DI + −DI or 1); ADX = rma(DX, adx_len).
- [ ] **Step 4: Run** the same command — expect all PASS.
- [ ] **Step 5: Commit** `git add src/bot/strategy/pine_indicators.py tests/test_pine_indicators.py && git commit -m "feat: Pine-compatible indicator series (unwired)"`

### Task 2: Pre-registered spec module

**Files:**
- Create: `src/research/__init__.py`, `src/research/pine_gate/__init__.py`, `src/research/pine_gate/spec.py`
- Test: `tests/test_pine_gate_spec.py`

**Interfaces:**
- Produces: `WIB`; `TRAIN_START`, `TRAIN_END`, `BLOCKS: tuple[tuple[datetime, datetime], ...]` (4), `CSCV_BLOCKS = 8`, `HOLDOUT_START`, `HOLDOUT_END`, `LAST20_START`, `LAST20_END` (WIB datetimes); `ms(dt) -> int`.
- Frozen dataclasses with spec defaults: `ExecConfig(bracket_pct=0.20, entry_slip_bps=0.25, tp_slip_bps=0.25, time_stop_min=60, fee_pct=0.035, tick_s=180, idle_tick_s=2, order_delay_s=5, cooldown_min=5, notional_usdt=25_000.0, funding_blackout_s=600)`, `GuardConfig(max_stops=12, max_consecutive_losses=15, max_drawdown_usdt=1500.0, max_entries=34, max_volume_usdt=1_000_000.0)`, `SelectionConfig(min_tp_sl=1000, min_supply_per_day=10.0, z=2.0, blocks_above_half=3, top_n=5, neighbour_min=4, neighbour_tolerance_pts=3.0, pbo_max=0.5, vol_high_pct=67.0, vol_lookback_1h=720, vp_bin_bps=5.0, trend_band_atr=0.25)`, `ModelConfig(c_grid=(0.01, 0.1, 1.0), threshold_hi=0.70, threshold_lo=0.50, threshold_step=0.01, fallback_threshold=0.5)`, `GateConfig(min_tp_sl=60, alpha=0.05, min_wr=57.0, half_min_wr=50.0, robust_overshoot_bps=2.0, robust_min_wr=55.0, offsets=20, offsets_min_pass=15, min_supply_per_day=10.0, bootstrap_n=10_000, permutation_shifts=1000, c1_seeds=200)`.
- `RuleSpec(trend, entry, strength, vol="OFF", vp="OFF")` frozen + ordered, `key() -> str` like `"H1|P35|ADX_DI|OFF|OFF"`; `TRENDS = ("E200", "H1", "H4")`, `ENTRIES = ("P35", "P40", "B12", "B24")`, `STRENGTHS = ("OFF", "ADX", "ADX_DI")`, `VOLS = ("OFF", "HIGH", "NOT_HIGH")`, `VPS = ("OFF", "ON")`; `stage1_rules() -> tuple[RuleSpec, ...]`, `stage2_variants(base) -> tuple[RuleSpec, ...]`, `stage1_neighbours(base) -> tuple[RuleSpec, ...]`.
- `spec_document() -> dict` (every constant above plus contender 2, reference, features list, segment boundaries as ISO strings); `spec_sha256(doc=None) -> str` = sha256 of `json.dumps(doc, sort_keys=True, separators=(",", ":"))`.

- [ ] **Step 1: Write the failing tests**

```python
def test_boundaries_are_wib_midnights_and_blocks_tile_training(self):
    self.assertEqual(TRAIN_START, datetime(2025, 8, 1, tzinfo=WIB))
    self.assertEqual(BLOCKS[0][0], TRAIN_START); self.assertEqual(BLOCKS[-1][1], TRAIN_END)
    self.assertTrue(all(a[1] == b[0] for a, b in zip(BLOCKS, BLOCKS[1:])))
    self.assertEqual(HOLDOUT_END, datetime(2026, 10, 14, tzinfo=WIB))
def test_stage1_has_36_unique_rules_with_filters_off(self): ...
def test_stage2_variants_are_six_and_include_base(self): ...
def test_every_stage1_rule_has_seven_neighbours(self): ...
def test_spec_sha_is_deterministic_and_sensitive(self):
    doc = spec_document(); self.assertEqual(spec_sha256(doc), spec_sha256(spec_document()))
    doc["gate"]["min_wr"] = 56.0; self.assertNotEqual(spec_sha256(doc), spec_sha256())
```

- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_gate_spec -v` — expect import failure.
- [ ] **Step 3: Implement** `spec.py` with the names above.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: pre-registered spec constants for the Pine gate research"` (with the three files and the test).

### Task 3: Columnar bars, aggregation and day files

**Files:**
- Create: `src/research/pine_gate/bars.py`
- Test: `tests/test_pine_gate_bars.py`

**Interfaces:**
- Produces: `Row = tuple[int, float, float, float, float, float]` (open_ms, open, high, low, close, volume); frozen `Bars(interval_ms, open_ms: array('q'), open, high, low, close, volume: array('d'))` with `__len__`; `BarGapError`, `MissingDayError`; `bars_from_rows(rows, interval_ms) -> Bars` (rejects duplicates or spacing ≠ interval); `aggregate(bars, interval_ms) -> Bars` (UTC-aligned buckets, complete buckets only); `concat(*parts) -> Bars`; `last_closed_index(bars, t_ms) -> int` (max i with open_ms[i] + interval ≤ t, else −1; `bisect`); `slice_until(bars, t_ms) -> Bars`; `wib_day(t_ms) -> date`; `wib_day_start_ms(day) -> int`; `day_path(data_dir, source, symbol, interval, day) -> Path` (`{source}/{symbol}/{interval}/{YYYY-MM-DD}.csv.gz`); `write_day(path, rows)`; `read_day(path) -> list[Row]`; `load_days(data_dir, source, symbol, interval, start_day, end_day) -> Bars` (inclusive range; raises `MissingDayError` listing every missing day).

- [ ] **Step 1: Write the failing tests** — `test_aggregate_5m_first_open_max_high_min_low_last_close_sum_volume`, `test_aggregate_drops_incomplete_trailing_bucket`, `test_last_closed_index_excludes_forming_bar` (1h bar opened 10:00, t = 10:30 → index of the 09:00 bar), `test_bars_from_rows_rejects_gap_and_duplicate`, `test_day_file_roundtrip_and_missing_days_listed`, `test_wib_day_rolls_at_1700_utc` (`wib_day(ms(2026-01-01 16:59 UTC)) == date(2026, 1, 1)`, at 17:00 → `date(2026, 1, 2)`).
- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_gate_bars -v` — expect failures.
- [ ] **Step 3: Implement** `bars.py`.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: columnar bars, aggregation and WIB day files"`.

### Task 4: Public kline/funding fetcher and daily collector

**Files:**
- Create: `src/research/pine_gate/fetch.py`
- Test: `tests/test_pine_gate_fetch.py`

**Interfaces:**
- Consumes: `Row`, `bars_from_rows`, `day_path`, `write_day`, `wib_day_start_ms` (Task 3).
- Produces: `HttpGet = Callable[[str, Mapping[str, str], Mapping[str, str]], Any]`; `PROD_HOST`, `VST_HOST`, `KLINES_PATH`, `FUNDING_PATH`, `HEADERS = {"X-SOURCE-KEY": "BX-AI-SKILL"}`; `ExchangeError`, `IncompleteDayError`; `httpx_get(url, params, headers)` (`# pragma: no cover`); `fetch_window(http_get, host, symbol, interval, start_ms, end_ms) -> list[Row]` (sorted ascending; duplicates raise); `fetch_day(http_get, host, symbol, day, retries=3, sleep=time.sleep) -> list[Row]` (1m, exactly 1,440 consecutive bars, else retry then `IncompleteDayError`); `SyncResult(fetched, skipped, failed: tuple[date, ...])`; `sync_days(http_get, host, source, symbol, start_day, end_day, data_dir, sleep=time.sleep) -> SyncResult` (skips existing files, sleeps ≥ 1.0 s between requests); `sync_warmup(http_get, host, symbol, interval, start_ms, end_ms, path, sleep)` (native 1h/4h in windows of ≤ 1,440 bars, one gz file); `fetch_funding_times(http_get, host, symbol, start_ms, end_ms, sleep) -> list[int]` (≤ 1,000 rows per window, both bounds set); `collect(http_get, data_dir, today: date, sleep) -> list[str]` (yesterday for `vst/ETH-USDT/1m`, `prod/BTC-USDT/1m`, `prod/ETH-USDT/1m`; one log line per job).
- BingX payload: `{"code": 0, "data": [{"open","close","high","low","volume" (strings),"time" (ms)}]}`; `code != 0` raises `ExchangeError`.

- [ ] **Step 1: Write the failing tests** (fake `http_get` recording calls, fake `sleep`):

```python
def test_fetch_day_sends_fixed_window_and_source_header(self):
    fetch_day(fake, PROD_HOST, "BTC-USDT", date(2026, 7, 1), sleep=noop)
    url, params, headers = fake.calls[0]
    self.assertEqual(params["startTime"], str(wib_day_start_ms(date(2026, 7, 1))))
    self.assertEqual(params["endTime"], str(wib_day_start_ms(date(2026, 7, 1)) + 86_400_000 - 1))
    self.assertEqual(params["limit"], "1440"); self.assertEqual(headers["X-SOURCE-KEY"], "BX-AI-SKILL")
def test_incomplete_day_is_retried_then_rejected(self): ...   # 1,439 bars three times → IncompleteDayError, 3 calls
def test_unsorted_rows_are_sorted_and_duplicates_rejected(self): ...
def test_sync_days_skips_existing_and_records_failures(self): ...
def test_sync_days_waits_at_least_one_second_between_requests(self): ...
def test_funding_times_paginate_with_start_and_end(self): ...
def test_collect_fetches_yesterday_for_each_job(self): ...
```

- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_gate_fetch -v` — expect failures.
- [ ] **Step 3: Implement** `fetch.py`.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: BingX public kline and funding fetcher with daily collector"`.

### Task 5: Feature frame (snapshots and 24-hour POC)

**Files:**
- Create: `src/research/pine_gate/features.py`
- Test: `tests/test_pine_gate_features.py`

**Interfaces:**
- Consumes: Task 1 series; Task 3 `Bars`, `aggregate`, `concat`, `last_closed_index`; `bot.strategy.indicators.ema/atr`; `bot.arena.setup_profile.build_setup_profile`.
- Produces: frozen `Snapshot(t_ms, close_5m, ema200_5m, ema125_5m, atr14_5m, rsi14_5m, rsi5_5m, adx14_5m, di_plus14_5m, di_minus14_5m, adx11_5m, high12_prev, low12_prev, high24_prev, low24_prev, h1_ema20, h1_ema50, h1_atr14, h1_vol_pct, h4_ema20, h4_ema50, h4_atr14, stretch_3m)` — every field `float | None` except `t_ms`; class `FeatureFrame` with `build(symbol, bars_1m, warmup_1h=None, warmup_4h=None) -> FeatureFrame`, `snapshot(t_ms) -> Snapshot`, `poc_24h(t_ms) -> float | None`, properties `symbol`, `bars_1m`; `htf_agreement(native: Bars, aggregated: Bars) -> float`.
- Rules: 5m and 3m are aggregated from 1m. 1h/4h = native warm-up bars strictly before the first 1m bar, then aggregated bars. `h1_vol_pct` = `percent_rank(atr14_1h / close_1h, 720)`. `high12_prev`/`low12_prev` = max high / min low of the 12 closed 5m bars before the last closed one (24 likewise). `stretch_3m` = `build_setup_profile({"last_price": close, "ema_21": ema(closes, 21), "atr_14": atr(highs, lows, closes, 14)}, "LONG")["stretch_atr"]` over the last 39 closed 3m bars (bot functions, identical to live). POC: 1m bars with open + 60 s ≤ t and open ≥ t − 24 h (requires a full 1,440 bars, else `None`); bins of 5 bps of the last closed 5m close; each bar's volume spread evenly over the bins its [low, high] covers; argmax, ties → nearest bin centre to that price; memoised per 5m bar.

- [ ] **Step 1: Write the failing tests** — on 3 seeded synthetic random-walk days:
  `test_snapshot_ignores_forming_hour_bar` (replace the forming hour's 1m bars with extreme prices; `snapshot(10:30)` unchanged), `test_truncation_invariance_at_random_times` (30 seeded times: `FeatureFrame.build(slice_until(bars, t)).snapshot(t) == full.snapshot(t)` and same `poc_24h`), `test_warmup_bars_feed_htf_before_first_minute_bar`, `test_stretch_matches_bot_setup_profile`, `test_poc_busiest_bin_and_ties_go_to_nearest_price`, `test_poc_requires_full_24h_window`.
- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_gate_features -v` — expect failures.
- [ ] **Step 3: Implement** `features.py` (precompute every series once per timeframe in `build`; `snapshot` does `last_closed_index` lookups only).
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: feature frame with look-ahead-free snapshots and 24h POC"`.

### Task 6: Rules (contender 1, contender 2, reference)

**Files:**
- Create: `src/research/pine_gate/rules.py`
- Test: `tests/test_pine_gate_rules.py`

**Interfaces:**
- Consumes: `Snapshot` (Task 5), `RuleSpec`, `SelectionConfig` (Task 2).
- Produces: `trend_label(s, trend) -> int`; `rule_signal(rule, s, poc: Callable[[], float | None], trend_override: int | None = None) -> int` (+1 long, −1 short, 0 none); `contender2_signal(s, trend_override=None) -> int`; `reference_signal(s) -> int`.
- Truth table (spec §5.5, §5.7, §5.9): E200 = sign(close − ema200), 0 on ties or `None`; H1/H4 = +1 if ema20 − ema50 > 0.25 × atr14, −1 if < −0.25 × atr14, else 0. P35/P40: long when RSI14 < 35/40 in an uptrend, short when > 65/60 in a downtrend. B12/B24: long when close > high12/24_prev in an uptrend, short when close < low12/24_prev in a downtrend. ADX: adx14 > 20; ADX_DI additionally DI+ > DI− for a long (mirrored). HIGH: h1_vol_pct ≥ 67; NOT_HIGH: < 67; `None` fails both. VP: pullback long ≤ POC / short ≥ POC, breakout long ≥ POC / short ≤ POC, `None` fails; `poc()` is called only after every other condition passed. Contender 2: H1 label, and |stretch_3m| ≤ 0.75 gives that side. Reference: close vs ema125, RSI5 < 40 long / > 55 short, adx11 > 20.

- [ ] **Step 1: Write the failing tests** — `test_trend_labels_respect_neutral_band`, `test_pullback_and_breakout_truth_table`, `test_adx_di_requires_agreeing_di`, `test_vol_filter_none_blocks`, `test_vp_rule_flips_with_entry_type`, `test_poc_not_called_when_earlier_conditions_fail` (counting callable stays 0), `test_contender2_near_mean_follows_h1_trend`, `test_reference_uses_pine_numbers`, `test_trend_override_replaces_label`.
- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_gate_rules -v` — expect failures.
- [ ] **Step 3: Implement** `rules.py`.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: rule signals for the three contenders and the reference"`.

### Task 7: Bracket outcomes

**Files:**
- Create: `src/research/pine_gate/outcomes.py`
- Test: `tests/test_pine_gate_outcomes.py`

**Interfaces:**
- Consumes: `Bars` (Task 3), `ExecConfig` (Task 2).
- Produces: frozen `Outcome(kind: str, entry_idx: int, exit_idx: int, entry_price: float, raw_exit_price: float, gapped: bool)` with kind `"TP" | "SL" | "TIME"`; `bracket_outcome(bars_1m, entry_idx, side, cfg) -> Outcome | None` (entry = close[entry_idx] × (1 + side·0.25e-4); TP/SL = entry × (1 ± side·0.0020); walk bars entry_idx+1 … entry_idx+60; first touch decides, both in one bar → SL; open beyond a level → raw exit = open, `gapped=True`; otherwise TIME at close[entry_idx+60]; `None` when bars run out first); `gross_bps(o, side, overshoot_bps, cfg) -> float` (SL exit = raw × (1 − side·overshoot/1e4); TP and TIME exits = raw × (1 − side·0.25e-4); returns side · (exit/entry − 1) · 1e4); `OutcomeCache(bars_1m, cfg)` with memoised `get(entry_idx, side) -> Outcome | None`.

- [ ] **Step 1: Write the failing tests** — `test_long_tp_first`, `test_same_bar_touch_is_stop`, `test_gap_through_stop_fills_at_open_minus_overshoot`, `test_time_stop_exits_at_bar_60_close`, `test_none_when_data_ends_first`, `test_short_mirrors_long`, `test_gross_bps_applies_both_slips` (a clean TP is `20 − 0.25 − 0.25 ± rounding` ≈ +19.5 bps).
- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_gate_outcomes -v` — expect failures.
- [ ] **Step 3: Implement** `outcomes.py`.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: first-touch bracket outcomes with gap and overshoot handling"`.

### Task 8: Tick-clock simulator with live guards

**Files:**
- Create: `src/research/pine_gate/simulate.py`
- Test: `tests/test_pine_gate_simulate.py`

**Interfaces:**
- Consumes: `OutcomeCache`, `gross_bps` (Task 7); `wib_day` (Task 3); `ExecConfig`, `GuardConfig` (Task 2).
- Produces: frozen `Market(symbol, bars_1m, outcomes, funding_ms: tuple[int, ...])`; frozen `Trade(symbol, side, signal_ms, entry_ms, exit_ms, kind, gross_bps, net_usdt, wib_day)`; `SignalFn = Callable[[str, int], int]`; `simulate(signal, markets, start_ms, end_ms, *, exec_cfg, guard_cfg, latencies_ms, seed, overshoot_bps, offset_s=0, blocked_entry_windows=(), fee_pct=None) -> list[Trade]`; `in_entry_window(t_ms) -> bool`; `in_funding_blackout(t_ms, funding_ms, blackout_s=600) -> bool` (0 ≤ next funding − t ≤ 600 s; `bisect`); `trading_days(start_ms, end_ms) -> list[date]` (Mon–Fri WIB days).
- Loop: `t = start + offset_s·1000`; at each tick, if in the window, outside every blocked window, past the cooldown and the WIB day's guards allow, evaluate markets in order (BTC first); the first non-zero side not in a funding blackout enters with order time `t + latency + 5 s` (latency drawn with `random.Random(seed)` from `latencies_ms`), entry index = 1m bar containing the order time, outcome from the cache (`None` ends the run); then `t = exit time + 180 s` and cooldown until entry + 5 min. A tick without an entry costs 2 s, so the next one starts at `t + 182 s`. `net_usdt = notional · (gross_bps − 2·fee·100)/1e4`; volume per trade = 2 × notional; loss = `net_usdt < 0` (the streak resets otherwise).

- [ ] **Step 1: Write the failing tests** (two tiny synthetic markets with scripted signals and outcomes): `test_one_position_and_next_tick_180s_after_exit`, `test_btc_checked_before_eth_on_same_tick`, `test_entry_window_monday_0700_to_saturday_0000_wib`, `test_guard_day_rolls_at_wib_midnight`, `test_funding_blackout_only_in_ten_minutes_before_funding`, `test_cooldown_five_minutes_after_entry`, `test_daily_caps_after_34_entries_or_1m_volume`, `test_same_seed_same_trades`, `test_blocked_entry_window_blocks_entries`.
- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_gate_simulate -v` — expect failures.
- [ ] **Step 3: Implement** `simulate.py`.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: tick-clock simulator with the bot's guards and timing"`.

### Task 9: Statistics

**Files:**
- Create: `src/research/pine_gate/stats.py`
- Test: `tests/test_pine_gate_stats.py`

**Interfaces:**
- Consumes: `Trade` (Task 8).
- Produces: `win_rate(trades) -> float | None` (TP/(TP+SL), percent); `mean_gross_bps(trades) -> float | None`; `net_per_1m(gross_bps, fee_pct=0.035) -> float` (= 50 · (g − 200·fee_pct)); `trades_per_trading_day(trades, days) -> float`; frozen `DayAgg(day, tp, sl, n, gross_sum)`; `day_aggregates(trades, days) -> list[DayAgg]`; `wr_se_clustered(aggs) -> float | None` (ratio estimator: e_d = tp_d − WR·(tp_d + sl_d); var = D/(D−1) · Σe_d² / (Σ(tp + sl))²); `wr_lower_bound(aggs, z=2.0) -> float | None`; `bootstrap_p_above(aggs, baseline_wr, n=10_000, seed=0) -> float` (one-sided p that WR ≤ baseline, resampling days); `bootstrap_paired_p(a_aggs, b_aggs, n, seed) -> float` (p that WR_a ≤ WR_b, same days resampled); `holm(p: Mapping[str, float]) -> dict[str, float]`; `permutation_p(observed, stats) -> float` ((1 + #{s ≥ observed}) / (1 + len)); `pbo_cscv(block_counts: Mapping[str, Sequence[tuple[int, int]]]) -> float`; `spa_pvalue(daily_diff: Mapping[str, Sequence[float]], n=2000, mean_block=5.0, seed=0) -> float`.
- PBO: over all C(8, 4) splits, among the rules passed in (selection passes only rules with ≥ 1,000 TP+SL), pick the rule with the best in-sample pooled WR, take its out-of-sample rank ω/(N + 1), λ = ln(ω/(1 − ω)); PBO = share of splits with λ ≤ 0. SPA (Hansen 2005, consistent): statistic max_k √T·mean(d_k)/ω_k; stationary bootstrap (Politis–Romano, mean block 5 days); recentring g_k = mean_k · 1{mean_k ≥ −ω_k·√(2 ln ln T / T)}; p = share of bootstrap statistics ≥ observed.

- [ ] **Step 1: Write the failing tests** — `test_win_rate_ignores_time_stops`, `test_net_per_1m_formula` (`net_per_1m(4.0) == -150`, `net_per_1m(7.0) == 0`), `test_clustered_se_grows_with_day_correlation`, `test_bootstrap_p_small_for_edge_large_for_none` (seeded), `test_holm_textbook` (`{a: 0.01, b: 0.03, c: 0.04}` → `{a: 0.03, b: 0.06, c: 0.06}`), `test_pbo_zero_for_dominant_rule`, `test_pbo_mid_for_noise` (seeded noise counts → between 0.3 and 0.7), `test_spa_small_for_clear_winner_large_for_noise`, `test_permutation_p_smoothing`.
- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_gate_stats -v` — expect failures.
- [ ] **Step 3: Implement** `stats.py`.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: win-rate statistics, Holm, PBO and SPA"`.

### Task 10: Two-stage selection

**Files:**
- Create: `src/research/pine_gate/selection.py`
- Test: `tests/test_pine_gate_selection.py`

**Interfaces:**
- Consumes: `RuleSpec`, `stage1_rules`, `stage2_variants`, `stage1_neighbours`, `SelectionConfig` (Task 2); `Trade` (Task 8); stats (Task 9).
- Produces: frozen `RuleResult(rule, trades, block_counts: tuple[tuple[int, int], ...], block_exit_days: tuple[int, ...], cscv_counts: tuple[tuple[int, int], ...], n_tp_sl, supply_per_day, wr, lower_bound, block_wr, block_lb)`; `summarize(rule, trades, block_days, cscv_days, trading_days) -> RuleResult`; `eligible(r, cfg) -> bool`; `rank_value(r, cfg) -> float` (median over B1–B4 of WR − z·SE; `-inf` unless at least 3 blocks have WR > 50%; a block without trades, with < 60 TP+SL or with < 10 exit days counts as failing: no vote, lower bound −inf); frozen `Selection(choice: RuleSpec | None, top: tuple[RuleSpec, ...], stable: bool, pbo: float | None, supply_limited: bool, results: Mapping[str, RuleResult])`; `select(evaluate: Callable[[RuleSpec], RuleResult], cfg) -> Selection`.
- Algorithm (spec §5.6): evaluate the 36 Stage-1 rules; keep the eligible ones (if none, mark `supply_limited` and rank them all); take the top 5 by `rank_value`; evaluate their Stage-2 variants; the best eligible result among the top 5 and their variants wins; its base must pass the stable-area check (at least 4 of 7 Stage-1 neighbours with WR ≥ base WR − 3) or the next in rank is taken; `pbo = pbo_cscv` over every evaluated result with ≥ 1,000 TP+SL (`None` with fewer than two; the selection file then records `pbo_pass = false` and `pbo_reason`).

- [ ] **Step 1: Write the failing tests** (a fake `evaluate` returning scripted `RuleResult`s): `test_ineligible_rule_never_selected_even_with_perfect_wr`, `test_rank_requires_three_of_four_blocks_above_half`, `test_block_without_trades_counts_as_failing`, `test_stage2_only_for_top5`, `test_stable_area_moves_to_next_rule`, `test_supply_limited_fallback`.
- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_gate_selection -v` — expect failures.
- [ ] **Step 3: Implement** `selection.py`.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: two-stage rule selection with block consistency and PBO"`.

### Task 11: Meta-labeling model (contender 3)

**Files:**
- Create: `src/research/pine_gate/metalabel.py`
- Test: `tests/test_pine_gate_metalabel.py`

**Interfaces:**
- Consumes: `Snapshot` (Task 5), `ModelConfig` (Task 2), `wib_day` hour helpers (Task 3).
- Produces: `FEATURES = ("trend_strength", "rsi_side", "adx", "di_diff_side", "vol_pct", "poc_dist_side", "hour_sin", "hour_cos")`; `candidate_features(s, side, trend, poc, t_ms) -> tuple[float, ...] | None` (spec §5.8: trend strength in ATR units of the chosen trend source × side; RSI14, or 100 − RSI14 for a short; ADX14; (DI+ − DI−) × side; h1_vol_pct; (close − poc)/atr14_5m × side; sin and cos of 2π·WIB hour/24; `None` if any input is `None`); frozen `Candidate(symbol, t_ms, side, x, label, start_ms, end_ms, block)` (label = 1 when the candidate's bracket trade has `gross_bps(…, s*) > 0`, else 0; `start_ms`/`end_ms` = entry and exit times); `uniqueness_weights(intervals) -> list[float]` (mean over each interval's minutes of 1/concurrency); `fit_logistic(X, y, w, C, max_iter=50, tol=1e-8) -> tuple[tuple[float, ...], float]` (IRLS minimising Σ w·logloss + ‖β‖²/(2C), intercept unpenalised, Gaussian elimination on the 9 × 9 system); frozen `MetaModel(coef, intercept, means, stds, threshold, C)` with `prob(x) -> float`, `to_dict()`, `from_dict(d)`; `choose_C(candidates, c_grid) -> tuple[float, dict[float, list[float]]]` (expanding windows: fit on B1…Bk, validate on Bk+1 for k = 1..3, dropping validation candidates in the 60 min after the boundary; returns the C with the lowest weighted mean log-loss — ties → smaller C — and the per-fold log-loss for every C); `reliability(probs, labels, bins=10) -> list[tuple[float, float, int]]` (mean predicted, observed rate, n per decile); `choose_threshold(supply_at: Callable[[float], float], cfg) -> float` (scan 0.70 → 0.50 by 0.01, first value whose supply ≥ 10/day, else 0.5); `train_meta(candidates, supply_at, cfg) -> MetaModel` (standardise with training means/stds, choose C, refit on all training candidates).

- [ ] **Step 1: Write the failing tests** — `test_uniqueness_halves_for_identical_intervals_and_is_one_when_disjoint`, `test_logistic_recovers_sign_on_separable_data`, `test_stronger_penalty_shrinks_coefficients`, `test_choose_C_only_fits_on_past_blocks` (spy on the fit inputs), `test_threshold_scan_highest_meeting_supply_else_half`, `test_features_none_when_any_input_missing`, `test_model_dict_roundtrip`.
- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_gate_metalabel -v` — expect failures.
- [ ] **Step 3: Implement** `metalabel.py`.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: meta-labeling logistic model with purged expanding-window CV"`.

### Task 12: Live calibration readers (latency and stop overshoot)

**Files:**
- Create: `src/research/pine_gate/calibrate.py`
- Test: `tests/test_pine_gate_calibrate.py`

**Interfaces:**
- Produces: `GetRows = Callable[[str, list[tuple[str, str]]], Awaitable[list[dict]]]`; `async load_entry_latencies_ms(get_rows, table, since_iso="2026-09-25T00:00:00+00:00") -> list[float]` (`purpose=eq.entry`, `success=eq.true`); `async stop_overshoot_samples(get_rows, trades_table, decisions_table, since_iso="2026-09-12T00:00:00+00:00") -> list[float]`; `s_star(samples, min_samples=15, fallback=1.0) -> float` (median rounded **up** to 0.5 bp); frozen `Calibration(s_star_bps, n_stops, latencies_ms)`; `make_get_rows(settings)` (`# pragma: no cover`, wraps `SupabaseTradeStore._get_rows`).
- Stop reconstruction per `reason=stop_loss` trade: the latest decision with `action` `OPEN_LONG`/`OPEN_SHORT` matching symbol and direction, `created_at` within [opened_at − 180 s, opened_at]; AI stop = `payload_json.decision.stop_loss_pct`; `atr_pct = atr_14 / last_price × 100` from the matching symbol entry in `payload_json.request.market_context.technicals` (a list of dicts keyed by `symbol`, e.g. `"BTC/USDT"`); stop % = min(max(AI stop, atr_pct × 1.5), 0.4); overshoot bps = (adverse move % − stop %) × 100.

- [ ] **Step 1: Write the failing tests** (async fake `get_rows`): `test_latencies_filter_entry_success_since`, `test_stop_reconstruction_uses_ai_stop_atr_and_cap`, `test_unmatched_trades_are_skipped`, `test_s_star_rounds_up_to_half_bp`, `test_s_star_falls_back_below_15_samples`.
- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_gate_calibrate -v` — expect failures.
- [ ] **Step 3: Implement** `calibrate.py`.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: calibrate latency and stop overshoot from live records"`.

### Task 13: Train stage and the holdout lock

**Files:**
- Create: `src/research/pine_gate/pipeline.py`
- Test: `tests/test_pine_gate_pipeline.py`

**Interfaces:**
- Consumes: Tasks 2–12.
- Produces: frozen `Dataset(frames: Mapping[str, FeatureFrame], markets: Mapping[str, Market])`; `load_dataset(data_dir, until_ms: int | None) -> Dataset` (loads prod 1m days, warm-up 1h/4h files and funding times; slices everything at `until_ms`); `TRAIN_DATA_UNTIL_MS = ms(TRAIN_END) + 3_600_000`; `rule_signal_fn(rule, frames) -> SignalFn`; `contender2_fn(frames) -> SignalFn`; `regime_counts(frames, start_ms, end_ms) -> dict` (step 0: share of up/down/neutral per trend source per month, n per cell); `HoldoutLockError`; `train_stage(dataset, calib, out_dir, spec_sha, seed=0) -> Path`; `load_selection_checked(path, spec_sha) -> dict`.
- `train_stage` simulates every rule over training with `blocked_entry_windows` = the 60 minutes before each block boundary, runs `select`, builds contender 3's candidates from the primary rule (chosen trend and entry, strength OFF, no filters; without contender 1 the best-ranked Stage-1 rule, and when every Stage-1 rule ranks −inf the best by median block lower bound without the vote, then pooled WR, then Stage-1 order), runs `train_meta`, and writes `pine_gate_selection_<YYYYMMDD>.json` with: `spec_sha256`, `data_sha256` (sha256 over the sorted day-file hashes), `created_at`, `contender1` (rule key or null), `top`, `stable`, `pbo`, `spa_p`, `supply_limited`, `primary_rule`, `meta_model`, `cv_logloss`, `reliability` (out-of-fold), `s_star_bps`, `n_latencies`, `regime_counts`, per-rule training metrics. SPA benchmark: a random side (seeded) at every tick on which the always-long control would enter, same bracket and clock; the input is each rule's daily gross bps minus the benchmark's.

- [ ] **Step 1: Write the failing tests** (tiny synthetic data dir across the boundary):

```python
def test_train_stage_never_reads_bars_after_boundary_plus_hour(self):
    ds = load_dataset(self.data_dir, until_ms=TRAIN_DATA_UNTIL_MS)
    for m in ds.markets.values():
        self.assertLessEqual(m.bars_1m.open_ms[-1] + 60_000, TRAIN_DATA_UNTIL_MS)
def test_selection_file_records_spec_sha_and_choice(self): ...
def test_load_selection_refuses_missing_file(self):
    with self.assertRaises(HoldoutLockError): load_selection_checked(self.tmp / "none.json", "abc")
def test_load_selection_refuses_sha_mismatch(self): ...
```

- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_gate_pipeline -v` — expect failures.
- [ ] **Step 3: Implement** the names above in `pipeline.py`.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: train stage with purge and a sha-checked holdout lock"`.

### Task 14: Test stage, controls and the gate

**Files:**
- Modify: `src/research/pine_gate/pipeline.py`
- Test: `tests/test_pine_gate_pipeline.py` (new class `GateTests`)

**Interfaces:**
- Produces: frozen `GateVerdict(contender, symbol, passed, failed_criteria: tuple[int, ...], wr, lower_bound, gross_bps, p_holm)`; `gate_symbol(evidence: Mapping[str, Any], cfg: GateConfig) -> GateVerdict` (criteria 1–9 of spec §5.11, numbered as there); `choose_contender(verdicts, lower_bounds) -> str | None` (most symbols passed → higher pooled lower bound → simpler order `"c2" < "c1" < "c3"`; c1/c3 also need to beat c2's holdout WR); `test_stage(dataset, selection, calib, out_dir, spec_sha, seed=0) -> Path` (writes `pine_gate_holdout_<YYYYMMDD>.json`).
- `test_stage` runs, per symbol, on the holdout with entries blocked in the final 60 minutes: contenders c1 (frozen rule), c2, c3 (primary rule filtered by `meta_model.prob ≥ threshold`); C0 always-long and always-short; C1 random side at each contender's entry times (200 seeds); day-shift permutation of the trend label for c1/c2 (1,000 shifts, k ∈ [7, D − 7]); c3 vs its primary (`bootstrap_paired_p`); halves; last-20-days supply; 2 bps overshoot; 20 tick offsets; fee 0.05% sensitivity; ETH-first ordering and the co-signal frequency; cadence cost (entry at the first 1m bar after the 5m close, no latency); supply with guards off; the reference rule (Pine numbers), reported only. Holm is applied per symbol across c1–c3.

- [ ] **Step 1: Write the failing tests** — `test_gate_fails_on_each_single_criterion` (a passing evidence dict, then one criterion broken at a time → that number appears in `failed_criteria`), `test_choice_prefers_more_symbols_then_lower_bound_then_simpler`, `test_c1_or_c3_must_beat_c2`, `test_failing_symbol_is_dropped`.
- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_gate_pipeline -v` — expect the new tests to fail.
- [ ] **Step 3: Implement** `gate_symbol`, `choose_contender`, `test_stage`.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: holdout stage with controls, permutation and the staged gate"`.

### Task 15: VST-vs-production check and the AI decision-level view

**Files:**
- Create: `src/research/pine_gate/checks.py`
- Test: `tests/test_pine_gate_checks.py`

**Interfaces:**
- Produces: `load_edge_klines_vst(path) -> Bars` (`{"k1": {ts: [o, h, l, c]}}`, volume 0); frozen `Agreement(n, n_either, n_both, n_prod, n_vst, side_agree, outcome_agree, wr_prod, wr_vst, passed)`; `vst_agreement(prod, vst, signal_for: Callable[[FeatureFrame, FeatureFrame, int], int], times, cfg) -> Agreement` (VP rules use the production POC on both sides; side agreement over the ticks where either side signals, outcome agreement where both signal the same side, each WR at its own side's signal ticks; pass = side ≥ 97%, outcome ≥ 95%, |ΔWR| ≤ 2, and fail when no tick signals); frozen `DecisionRow(symbol, created_ms, side, confidence, approved, ai_call_id)`; `parse_decision(row) -> DecisionRow | None` (HOLD rows kept with `side = 0`); `decision_buckets(rows, tick_start_ms: Callable[[DecisionRow], int], signal: SignalFn, outcome_for: Callable[[str, int, int], float | None]) -> dict[str, dict]` (`outcome_for(symbol, t_ms, side)` = gross bps of the bracket trade an order at that tick would get, `None` when unresolved) (A: side ≠ 0, confidence ≥ 0.75, approved; B: A and same-side signal; E: signal present and side = 0; F: every signal at the stored tick times — each with n, WR, gross, trades/day).

- [ ] **Step 1: Write the failing tests** — `test_identical_frames_agree_fully`, `test_agreement_thresholds`, `test_bucket_definitions`, `test_parse_decision_keeps_hold_as_zero_side`.
- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_gate_checks -v` — expect failures.
- [ ] **Step 3: Implement** `checks.py`.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: VST-vs-production agreement and AI decision buckets"`.

### Task 16: Indonesian report

**Files:**
- Create: `src/research/pine_gate/report.py`
- Test: `tests/test_pine_gate_report.py`

**Interfaces:**
- Consumes: the selection and holdout JSON files (Tasks 13–14) and the check outputs (Task 15).
- Produces: `render_markdown(results: dict) -> str` with sections, in order: Ringkasan, Integritas data dan sha256, Langkah 0 (sebaran rezim), Tahap latih (grid, 5 teratas, PBO, SPA), Model meta-labeling (log-loss, kalibrasi), Periode uji per kontender dan simbol, Gerbang (kriteria 1–9), Kontrol dan permutasi, VST vs produksi, Keputusan AI (bucket A/B/E/F), Biaya kadens dan guard, Kesimpulan; `write_report(results, out_dir) -> tuple[Path, Path]` (`pine_gate_edge_<YYYYMMDD>.md` and `.json`, sha256 of spec, data, selection and code at the top); `verdict_sentence(summary: dict) -> str`.
- **User contribution (learning mode):** `verdict_sentence` is the one paragraph the manager will read. It is left as a `TODO` stub with its tests for the user to write (5–10 lines): how to phrase "gate passed / not passed / not enough data" honestly, including that passing the gate is not yet proof of 60%.

- [ ] **Step 1: Write the failing tests** — `test_markdown_has_all_sections_in_order_and_sha_header`, `test_json_roundtrip`, `test_verdict_sentence_three_outcomes` (pass → mentions "lulus gerbang" and "belum bukti 60%"; fail → "tidak lulus"; insufficient → "data belum cukup").
- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_gate_report -v` — expect failures.
- [ ] **Step 3: Implement** `render_markdown` and `write_report`; hand `verdict_sentence` to the user.
- [ ] **Step 4: Run** — expect PASS once the user's `verdict_sentence` is in.
- [ ] **Step 5: Commit** `git commit -m "feat: Indonesian report for the Pine gate evaluation"`.

### Task 17: CLI, review gate, and the VPS run

**Files:**
- Create: `src/research/pine_gate/cli.py`, `src/research/pine_gate/__main__.py`
- Test: `tests/test_pine_gate_cli.py`

**Interfaces:**
- Produces: `main(argv: Sequence[str] | None = None) -> int` with subcommands `spec --write PATH` (prints the sha256), `fetch --source {prod,vst} --symbols … --start --end`, `warmup --start --end`, `funding --start --end`, `collect`, `check-data` (complete days, native-vs-aggregated 1h/4h agreement ≥ 99.9%, the 500-time truncation test on real data), `calibrate`, `train`, `test`, `vst-check --symbol`, `decisions`, `report`.

- [ ] **Step 1: Write the failing tests** — `test_spec_command_writes_document_and_prints_sha`, `test_test_command_refuses_without_selection` (exit code ≠ 0, message names the missing file).
- [ ] **Step 2: Run** `PYTHONPATH=src python -m unittest tests.test_pine_gate_cli -v` — expect failures.
- [ ] **Step 3: Implement** `cli.py` (argparse; thin wiring only) and `__main__.py`.
- [ ] **Step 4: Run the full gate locally:** `PYTHONPATH=src python -m compileall src tests`, `PYTHONPATH=src python -m coverage run -m unittest discover -s tests`, `PYTHONPATH=src python -m coverage report` — expect all tests OK and `TOTAL` ≥ 80%; `git diff --check` clean.
- [ ] **Step 5: Commit** `git commit -m "feat: CLI for the Pine gate research"`.
- [ ] **Step 6: Review gate before any outcome is computed:** whole-branch review (`superpowers:requesting-code-review`, `ecc:python-reviewer`, `ecc:security-reviewer` for the fetcher); fix findings and recommit. The holdout must not be touched before this passes.
- [ ] **Step 7: Sync to the VPS:** `rsync -a --delete --exclude __pycache__ src/ 0xAlvary2:~/replay/pine_gate_repo/src/` — then `ssh 0xAlvary2 'cd ~/replay/pine_gate_repo/src && find research bot/strategy/pine_indicators.py -name "*.py" | sort | xargs sha256sum'` and record the output.
- [ ] **Step 8: Pre-register:** `… -m research.pine_gate.cli spec --write ~/replay/pine_gate_spec_20261014.json` — record the printed sha256 in project memory (`boss-pine-adaptation.md`) **before** the next step.
- [ ] **Step 9: Data:** `fetch --source prod --symbols BTC-USDT,ETH-USDT --start 2025-08-01 --end 2026-10-13` (expect about 880 fetched, 0 failed), `warmup --start 2025-02-01 --end 2025-08-01`, `funding --start 2025-08-01 --end 2026-10-14`, then `check-data` (expect all checks PASS).
- [ ] **Step 10: Collector cron:** append one line, leaving both existing lines untouched — `23 5 * * * cd ~/ai-perpetual-trading && PYTHONPATH=$HOME/replay/pine_gate_repo/src nice -n 19 .venv/bin/python -m research.pine_gate.cli collect >> $HOME/replay/pine_gate/collect.log 2>&1`; verify with `crontab -l`.
- [ ] **Step 11: Calibrate, train, freeze:** `calibrate` (prints s\* and the latency count), `train` (prints the selection path), then `sha256sum` that file and record it in memory.
- [ ] **Step 12: Holdout once:** `test`, then `vst-check --symbol BTC-USDT`, `decisions`, `report` — expect the report paths printed.
- [ ] **Step 13: Bot untouched:** `pm2 describe ai-perp-volume` restart count unchanged versus before Step 7, and no new ERROR lines in `~/.pm2/logs/ai-perp-volume-error.log` caused by the run.
- [ ] **Step 14 (≥ 2 weeks later):** `vst-check --symbol ETH-USDT` on the collected VST ETH data; record the verdict.

### Task 18: Documentation, TDD evidence and PR

**Files:**
- Create: `.github/tdd/pine-gate-phase1.tdd.md`
- Modify: `CLAUDE.md` (a short "Research: Pine gate evaluation" section — package location, never imported by `bot`, how to run, the holdout lock)

- [ ] **Step 1:** Write the TDD evidence document in the repo's shape (`.github/tdd/bingx-volume-cycle.tdd.md`): user journey, RED evidence per task, GREEN summary, verification numbers (tests, coverage %, `compileall`, `git diff --check`, `pip check`, secret hygiene), the commit trail, the VPS run (sha256s, s\*, selection, gate verdicts).
- [ ] **Step 2:** Update `CLAUDE.md`; if contender 1 was frozen, set the defaults of `docs/tradingview/pine_adapted_gate_v2.pine` to its parameters (spec §5.15).
- [ ] **Step 3:** Run `PYTHONPATH=src python -m unittest tests.test_secret_hygiene -v` — expect OK.
- [ ] **Step 4:** Commit `git commit -m "docs: TDD evidence and CLAUDE.md for the Pine gate research"`.
- [ ] **Step 5:** `superpowers:finishing-a-development-branch` → push and open the PR with the report summary and the gate verdicts; the PR changes no bot behaviour.
