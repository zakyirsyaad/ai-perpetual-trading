# LONG module, Phase 1 — offline backtest of step 0 and Mode A on BTC — design

Date: 2026-10-17 · Status: approved (2026-10-17) · Source: the manager's document
"Modul LONG — Spesifikasi & Rencana Uji" (2026-10-17, 11 pages, PDF held by the user) · Related:
`docs/research/2026-10-16-laporan-perkembangan-bot.pdf` (the report the document answers),
`docs/superpowers/specs/2026-10-14-pine-adapted-gate-design.md` (Pine Phase 1, whose data, split and
holdout lock this design reuses).

## 1. Intent

The live bot only shorts BTC (4–6 entries a day) and sits idle while BTC trends up. The manager's
document adds rule-based modes for up-trends (A pullback LONG, B breakout LONG), for sideways
markets (C, both sides) and a mirrored pullback SHORT (D2), wraps the current SHORT as Mode D, and
adds ETH, aiming at 6–10 entries a day across BTC and ETH. Every mode is switched on and tested on
its own, and passes only if its result per 1,000,000 USDT of volume is better than −150. During
testing the AI may not open or close positions; the modes are pure rules on closed candles.

The document fixes a test order: step 0 (split the 16 October Pine holdout by side and trend
condition, no new test), then Mode A on BTC, then ETH (A + D), B, C and D2. Each mode is
backtested on 1m data first (training, lock, one-shot holdout) and only then forward-tested on
VST. Its closing message to the coder says: do not change any number or rule; ask, do not guess,
when something is ambiguous; explain the plan and the files before writing code; start with step
0, then Mode A on BTC.

**This spec covers Phase 1 only:** step 0 and the Mode A on BTC backtest, through its holdout
result and report. Later phases each get their own spec: ETH, B, C and D2 reuse this harness, and
the live implementation (VST forward test, venue-side TP/SL orders, two concurrent positions)
starts only after a holdout result exists, as the document requires.

## 2. Source rules (transcribed from the document)

### 2.1 Indicators

| Name | Definition |
|---|---|
| SMA20 (1h), SMA50 (1h) | Simple mean of the closes of the last 20 and 50 1h candles |
| EMA50 (4h) | EMA 50 of 4h closes |
| EMA20 (15m) | EMA 20 of 15m closes |
| ATR14 (1h) | ATR 14 (Wilder) on 1h candles, in USDT |
| Bollinger (20, 2) | SMA20 (15m) ± 2 standard deviations, on 15m candles (Mode C, later phase) |
| Average volume | SMA 20 of 15m volume (Mode B, later phase) |

### 2.2 Mode selector

At every closed 1h candle each pair gets a condition; the first match holds until the next 1h close:

```python
def pilih_mode(pair):
    # every value from closed candles
    gap = abs(sma20_1h - sma50_1h)
    if gap < 0.2 * atr14_1h:
        return "SIDEWAYS"   # mode C
    if sma20_1h > sma50_1h and close_4h > ema50_4h:
        return "NAIK"       # mode A, B
    if sma20_1h < sma50_1h:
        return "TURUN"      # mode D, D2
    return "TIDAK_ENTRY"
```

`close_4h` is the close of the last closed 4h candle. TURUN deliberately has no 4h filter, so the
current SHORT rule stays as it is being tested.

### 2.3 Mode A — pullback LONG (only in NAIK)

Checked at every closed 15m candle:

1. Touch: the 15m low touches or goes below EMA20 (15m).
2. Confirmation: within 3 15m candles from the touch, the touch candle included, a green candle
   (close > open) closes above EMA20 (15m).
3. Not too high: the confirmation close is at most 1 × ATR14 (1h) above SMA20 (1h).
4. Entry: market order at the open of the next 15m candle.

One touch yields at most one entry; after 3 candles without confirmation, wait for a new touch.
Exit: TP entry + 0.40 %, SL entry − 0.25 %, time limit 90 minutes. If two modes signal on the same
candle, A has priority over B; with one position per pair the second signal is ignored.

### 2.4 Mode D — the current SHORT rule (only in TURUN)

The document: Mode D is today's SHORT rule unchanged, except that it may only enter in TURUN; this
equals the "SHORT in a down-trend" rule being forward-tested. D wins a tie with D2. The document's
configuration block gives D no trigger or exit values (§3 fills them).

### 2.5 Execution, sizing and day

- Signals come only from closed candles; nothing is decided inside a candle.
- Entry is always a market order at the next candle's open, one fill, never added to.
- On fill, TP is placed as a take-profit-market and SL as a stop-market order, both reduce-only
  (live phase). The time limit is enforced by a scheduler with a reduce-only market order.
- A change of condition never closes an open position; only its TP, SL or time limit does.
- Position notional is the same for every mode and pair, equal to today's SHORT position.
- A day is 00:00–23:59 WIB.

### 2.6 Backtest rules

- 1m candles decide whether TP, SL or the time limit is hit first.
- If TP and SL are both touched inside the same 1m candle, the SL is taken.
- Taker fee 0.035 % per side.
- Extra slippage 0.01 % on every market order, TP and SL included.

### 2.7 Position management (all pairs)

At most 1 open position per pair and 2 in total; a 15-minute pause per pair after a position
closes; at most 6 entries per pair and 10 in total per day; skip a signal when ATR14 (1h) is above
2 × the median ATR14 (1h) of the last 30 days. The AI may not open or close positions during
testing.

### 2.8 Test procedure and pass rule

One mode or pair is switched on at a time. For each:

1. Training stage (August 2025 – 4 August 2026) with the configuration numbers; no searching for
   other settings.
2. Lock: record settings and rules before seeing the test data.
3. Test stage (5 August – 14 October 2026), once. The mode passes when all three hold: result per
   1M better than −150; at least 40 positions; with the 3 most profitable positions removed, the
   result is still better than −250. Fewer than 40 positions because the condition is rare is not a
   failure: the mode goes on to the forward test for more sample.
4. Forward test on VST, at least 60 positions per mode, same pass rule; stop a mode early if after
   30 positions it is worse than −300 (later phase).

The only allowed adjustment: if in training Mode A on BTC averages fewer than 3 entries per NAIK
day, its trigger moves from 15m to 5m candles. The decision is made before the test stage.

### 2.9 Reports and trade log

Results per mode, pair and stage, in the 16 October report's terms:

| Mode | Pair | Tahap | Posisi | Entry per hari aktif | Menang | Hasil per 1 juta | Keluar TP / SL / waktu | Rata-rata lama posisi |
|---|---|---|---|---|---|---|---|---|

with two comparators over the same NAIK periods: no trading at all, and the current SHORT rule
run in those periods. Per-trade log columns, in this order:

```
trade_id,pair,mode,sisi,kondisi,sma20_1h,sma50_1h,ema50_4h,atr14_1h,faktor_k,
waktu_sinyal,waktu_entry,harga_entry,harga_tp,harga_sl,waktu_exit,harga_exit,
alasan_exit,fee_usdt,slippage_usdt,pnl_usdt,notional_usdt,volume_usdt
```

`alasan_exit` is TP, SL or WAKTU; `kondisi` is NAIK, TURUN or SIDEWAYS.

## 3. Decisions (user, 2026-10-17)

1. **Follow the document as written.** Where the document is explicit, it wins, including where it
   rests on a misreading of the 16 October report (§4). The trend averages are therefore **SMA**.
2. **Mode D in the backtest is a pure rule**, the near-mean SHORT the manager saw as "the bot's
   current rule" in the Pine test: at every closed 3m candle, SHORT when the price sits within
   0.75 ATR of EMA21, measured exactly as the live stretch gate does (last 39 closed 3m bars,
   `bot.strategy.indicators` EMA21 and ATR14, `build_setup_profile`). Exits TP −0.20 %, SL
   +0.20 %, time limit 60 minutes, as the document describes today's SHORT exit.
3. **Weekly entry window:** entries only Mon 07:00 – Sat 00:00 WIB, as the live bot and the Pine
   test.
4. **A NAIK day** is a WIB day with at least one hour labelled NAIK while entries are allowed
   (exact form in §5). It defines both the 5m switch rule and the "Entry per hari aktif" column.
5. **Scope:** Phase 1 as in §1. The live bot, its `.env` and pm2 are not touched; the two forward
   tests running on the live bot (AI exit minimum hold 600 s, the SHORT-in-down-trend rule) stay
   unconfounded.
6. **Where it runs:** on the Mac (Python 3.10, the `.venv`), on data copied read-only from the VPS.
7. **Branch:** `feat/long-module-backtest` from `main`.

## 4. Known discrepancies with the 16 October report (recorded, rules unchanged)

- The report described the trend as "the 20-hour average above the 50-hour average". The analysis
  behind it (`~/replay/long_trend.py` on the VPS) used **EMA20 vs EMA50** on closed 1h bars. The
  document defines SMA and calls it the same; per decision 1 the backtest uses SMA, so Mode D's
  TURUN is not identical to the pre-registered EMA forward rule.
- The document says the report's LONG simulation used a 0.2 % / 0.2 % / 60-minute exit. It used
  the bot's own `_resolve_trade_targets` bracket (SL ≈ 0.28 %, TP ≈ 0.60 %, ATR-scaled) with a
  60-minute stop; 0.2 % / 0.2 % was the Pine test's bracket.
- The document's breakeven figures (49 %, and 45 % for −150 at TP 0.40 / SL 0.25) leave out its own
  0.01 % slippage. With it a round trip costs 9 bps. Because D6 sets TP and SL from the entry fill,
  a TP nets +32 bps and an SL −33 bps (the Mode A trade logs show exactly that), so breakeven ≈
  50.8 % and the −150 target ≈ 46.2 % (binary TP/SL outcomes; time exits shift both). Corrected
  2026-10-18: this note first said 52.3 % / 47.7 %, which charged the entry slippage against both
  levels.

## 5. Defaults for what the document leaves open (approved 2026-10-17)

- **D1 Candle alignment.** Every candle is built from production 1m bars in UTC-epoch buckets, as
  BingX and TradingView do: 4h candles close at 03, 07, 11, 15, 19 and 23 WIB. A bucket with a
  missing minute is dropped. Native 1h and 4h warm-up bars are prepended before the first
  aggregated bar.
- **D2 Closed candles at a boundary.** At time `t` a series exposes only bars with
  `open + interval <= t`. A 15m candle closing at 10:00 WIB therefore sees the condition from the
  1h candle 09:00–10:00.
- **D3 Indicator warm-up.** SMA and rolling windows are `None` until full. EMA (α = 2/(n+1)) and
  ATR (Wilder RMA of the true range) seed with the mean of the first `n` values and are `None`
  before 8 × n bars, as in `pine_indicators`. A missing input makes the condition TIDAK_ENTRY and
  blocks the signal.
- **D4 Condition check time.** Mode A requires NAIK at the confirmation candle's close (the signal
  time); touches are tracked whatever the condition. Touch and confirmation compare a candle with
  EMA20 (15m) computed through that candle's close.
- **D5 Touch consumption.** A touch is used up only by an entry. A signal blocked by an open
  position, the pause, a daily cap, the volatility guard or the weekly window leaves it available;
  a later confirmation inside the same 3-candle window may still enter. An entry consumes every
  touch whose window contains the signal candle.
- **D6 Levels and gaps.** TP and SL are set from the entry fill (slippage included). A 1m bar that
  opens beyond a level exits at that bar's open, as in the Pine test.
- **D7 Time exit.** At the open of the 1m bar `limit` minutes after entry.
- **D8 Notional** 25,000 USDT (margin 250 × 100x, today's SHORT). Results per 1M do not depend on it.
- **D9 No live-bot safeguards the document does not name:** no funding blackout, no daily-loss or
  stop-count guard, no spread or depth check.
- **D10 Segment edges.** No signal at `t` when `t + time limit` passes the end of its segment, so no
  position straddles training and test, or test and the data after it.

Definitions used by the metrics (from the document and the 16 October report):

- **Hasil per 1 juta** = 1,000,000 × Σ`pnl_usdt` / Σ`volume_usdt`.
- **Menang** = 100 × TP / (TP + SL): of the positions that hit TP or SL, the share that won (the
  report's definition). Time exits count in Posisi and in the result, not in Menang.
- **NAIK day** (decision 4, made exact): a WIB day with at least one closed 1h candle labelled NAIK
  whose close time lies inside the weekly entry window, i.e. an hour in which Mode A could enter.
- **Entry per hari aktif** = positions / NAIK days in the stage. The 5m switch compares the same
  ratio from the 15m training run with 3.
- **Rata-rata lama posisi** = mean of `waktu_exit − waktu_entry`, in minutes.
- **Without the top 3**: drop the 3 positions with the highest `pnl_usdt` (ties: earliest first)
  and recompute Hasil per 1 juta.

## 6. Architecture

Approach (approved): the pure decision rules live in the bot package, unwired from the live bot;
the backtest harness lives in research and calls them. When the live phase comes, its engine calls
the same rules, so what is tested is what runs.

### 6.1 Rule core — `src/bot/strategy/modes/` (pure, no I/O, not imported by the live bot)

The core takes numbers, never bar arrays, so it does not import `pine_indicators` (the existing
guard `test_no_live_module_imports_pine_indicators` stays as it is).

| Module | Responsibility | Interface (sketch) |
|---|---|---|
| `config.py` | Load and validate `config/modes.yaml` into frozen dataclasses; unknown or missing keys refuse; `ai_boleh_buka_tutup` must be `false`; percents stay in percent | `load_modes_config(path) -> ModesConfig` |
| `regime.py` | `pilih_mode` exactly as §2.2; any `None` input → `TIDAK_ENTRY` | `pilih_mode(sma20, sma50, atr14, close_4h, ema50_4h, sideways_mult) -> str` |
| `pullback.py` | Mode A touch/confirm state machine over closed candles (D4, D5); D2 mirrors it later | `PullbackTracker.on_candle(candle, ema20, sma20_1h, atr14_1h) -> Signal \| None`; `consume()` on entry |
| `near_mean.py` | Mode D trigger from a stretch value | `near_mean_short(stretch_atr, max_abs) -> bool` |
| `position_rules.py` | Bracket prices per mode and side; time limit; weekly window (via `bot.core.entry_window`); daily caps; pause; volatility guard | `bracket(entry_fill, side, tp_pct, sl_pct)`, `EntryGate.allows(...)` |

### 6.2 Backtest harness — `src/research/mode_backtest/`

| Module | Responsibility |
|---|---|
| `data.py` | Load production 1m day files and 1h/4h warm-ups through `research.pine_gate.bars`; a sha256 manifest of the files read; training loads never read a day on or after 2026-08-05 |
| `frames.py` | Closed-candle series per pair: 15m, 5m, 3m, 1h and 4h bars (D1); SMA20/SMA50/ATR14 (1h), EMA50 (4h), EMA20 (15m or 5m), the 720-hour ATR median, the 3m stretch (decision 2); lookup by time (D2) |
| `simulate.py` | Event loop over signal-candle closes for one mode and pair: gates, entry at the next open plus slippage, 1m walk with SL-first, gaps (D6) and time exit (D7), fees, pause; returns trades |
| `metrics.py` | Hasil per 1 juta, Menang, exits, hold, entries per NAIK day, without-top-3, pass status |
| `tradelog.py` | The §2.9 CSV, times in ISO 8601 WIB (`+07:00`) |
| `lock.py` | Lock file writing and checking; the one-shot run marker |
| `step0.py` | Step 0 (§8) |
| `report.py` | Indonesian Markdown tables in the §2.9 layout |
| `cli.py`, `__main__.py` | `step0`, `train`, `test` stages |

### 6.3 Configuration — `config/modes.yaml`

The document's block verbatim, plus keys marked `# ditambahkan 2026-10-17` that hold the decisions
of §3 and the test-procedure numbers of §2.8. The document asks for "all numbers in one block", and
the lock binds this file's sha256:

```yaml
umum:
  zona_waktu: Asia/Jakarta
  fee_taker_persen: 0.035
  slippage_persen: 0.01
  maks_posisi_total: 2
  maks_posisi_per_pair: 1
  maks_entry_per_hari_total: 10
  maks_entry_per_hari_per_pair: 6
  jeda_menit: 15
  penjaga_volatilitas_kali_median_atr: 2.0
  ai_boleh_buka_tutup: false
  notional_usdt: 25000                          # ditambahkan 2026-10-17 (D8)
  jendela_entry_mingguan: "mon 07:00-sat 00:00" # ditambahkan 2026-10-17 (keputusan 3)

pemilih_mode:
  sma_cepat_1h: 20
  sma_lambat_1h: 50
  ema_4h: 50
  sideways_jarak_kali_atr: 0.2

pair:
  BTC-USDT:
    aktif: true
    faktor_k: 1.0
  ETH-USDT:
    aktif: false
    harus_sama_kondisi_dengan: BTC-USDT
    faktor_k: otomatis   # median 30 hari, dibatasi 1.0 sampai 2.0

mode:
  A_pullback_long:
    aktif: true
    tf_pemicu: 15m
    ema_pemicu: 20
    konfirmasi_dalam_candle: 3
    maks_jarak_di_atas_sma20_1h_kali_atr: 1.0
    tp_persen: 0.40
    sl_persen: 0.25
    batas_waktu_menit: 90
  B_breakout_long:
    aktif: false
    lookback_candle: 16
    volume_kali_rata_rata: 1.5
    periode_volume_rata_rata: 20
    maks_jarak_di_atas_sma20_1h_kali_atr: 2.0
    tp_persen: 0.40
    sl_persen: 0.25
    batas_waktu_menit: 90
  C_sideways:
    aktif: false
    bollinger_periode: 20
    bollinger_std: 2.0
    min_jarak_tp_persen: 0.25
    sl_persen: 0.25
    batas_waktu_menit: 60
  D_short_sekarang:
    aktif: true          # aturan SHORT lama, hanya saat kondisi TURUN
    tf_pemicu: 3m                        # ditambahkan 2026-10-17 (keputusan 2)
    candle_jendela: 39                   # ditambahkan 2026-10-17 (keputusan 2)
    ema_periode: 21                      # ditambahkan 2026-10-17 (keputusan 2)
    atr_periode: 14                      # ditambahkan 2026-10-17 (keputusan 2)
    maks_jarak_ke_ema_kali_atr: 0.75     # ditambahkan 2026-10-17 (keputusan 2)
    tp_persen: 0.20                      # ditambahkan 2026-10-17 (keputusan 2)
    sl_persen: 0.20                      # ditambahkan 2026-10-17 (keputusan 2)
    batas_waktu_menit: 60                # ditambahkan 2026-10-17 (keputusan 2)
  D2_pullback_short:
    aktif: false
    tf_pemicu: 15m
    ema_pemicu: 20
    konfirmasi_dalam_candle: 3
    maks_jarak_di_bawah_sma20_1h_kali_atr: 1.0
    tp_persen: 0.40
    sl_persen: 0.25
    batas_waktu_menit: 90

uji:                                     # ditambahkan 2026-10-17 (angka §2.8 dokumen)
  latihan_mulai: "2025-08-01T00:00:00+07:00"
  latihan_selesai: "2026-08-05T00:00:00+07:00"
  ujian_selesai: "2026-10-14T00:00:00+07:00"
  lolos_hasil_per_1_juta_lebih_dari: -150
  lolos_minimal_posisi: 40
  lolos_buang_posisi_teratas: 3
  lolos_hasil_tanpa_teratas_lebih_dari: -250
  pindah_5m_jika_entry_per_hari_naik_kurang_dari: 3
  hari_naik_minimal_jam_naik: 1
```

The `aktif` flags describe the live configuration; the backtest runs the one mode named on the
command line. Phase 1 implements A and D; asking for B, C, D2 or ETH refuses with a clear error.

### 6.4 Existing files that change

- `src/bot/strategy/pine_indicators.py`: add `sma`, `stdev` (population, as TradingView; for
  Mode C later) and `rolling_median`.
- `requirements.txt`, `pyproject.toml`: add PyYAML (`yaml.safe_load` only).
- `tests/test_pine_gate_structure.py`: `test_fingerprint_covers_every_bot_module_the_research_imports`
  walks every research package; scope it to `research/pine_gate`, the package whose lock it
  checks. Without that, the new package's bot imports make it fail. Pine Phase 1 is finished, and
  its holdout marker already refuses a second run.
- `CLAUDE.md`: a short entry for the new package and its commands, once it exists.

Untouched: the live bot (`trade_engine`, `orchestration`, `bingx_client`, `main.py`), `.env`,
the VPS and pm2.

### 6.5 Boundaries (enforced by tests)

`bot` never imports `research` (the existing guard covers the new core). Within
`research/mode_backtest`: no module imports another's private name; the code fingerprint covers
every `bot` and `research` module the package imports; files are written only through
`research.pine_gate.fileio` (exclusive, never overwritten); no network and no venue code.

## 7. Data

- **Source (read-only):** VPS `~/replay/pine_gate/data/prod/{BTC-USDT,ETH-USDT}/`: 1m day files
  2025-08-01 → 2026-10-15 (441 WIB days each); native 1h and 4h `warmup.csv.gz` covering
  2025-02-01 → 2025-07-31 WIB (4,344 1h and 1,086 4h bars per symbol, so EMA50 (4h), the 720-hour
  ATR median and every 1h series are warm on the first training day); `funding.json`. About 23 MB in
  total. Production data, because VST volume is not real and VST prices follow production.
- **Local copy:** `~/replay/mode_backtest/data/prod/...` (outside the repo). The copy is accepted
  only when every file's sha256 equals the VPS file's (`sha256sum` there, compared here).
- **Step 0 inputs:** `~/replay/out/pine_gate_{selection,calibration,holdout}_20261016.json`,
  copied to `~/replay/mode_backtest/pine_phase1/`; the selection's sha256 must equal the
  holdout file's `selection_sha256`.
- **Outputs:** `~/replay/mode_backtest/out/`; run markers in
  `~/replay/mode_backtest/data/holdout_runs/`.

## 8. Step 0 — the Pine holdout by side and condition

1. Rebuild the Pine Phase 1 holdout inputs with that package's public API (`load_dataset`,
   `Calibration`, `HoldoutPeriod.spec()`, the spec's execution, guard and gate configs,
   `holdout_markets`, `Holdout`, `build_contenders`) on the copied data and files, under Python 3.10
   as in the original run.
2. Replay contenders c1, c2 and c3 with seed 0, as the test stage did.
3. **Reproduction check:** for every contender and symbol, `symbol_metrics` of the replay must equal
   the holdout file's `contenders.<c>.symbols.<symbol>` field by field. Any difference stops step 0
   with an error; nothing is adjusted. If the current code cannot reproduce the run, the frozen
   Phase 1 code (`~/replay/pine_gate_repo/src.phase1-20261016-212600`) is used instead, and the
   report says so.
   *Note (2026-10-18):* that fallback cannot work as worded. `src/research` is a regular package
   (it has an `__init__.py`), so with the frozen `src` and the current one both on `PYTHONPATH`
   the first one supplies the whole `research` package: the frozen copy either shadows the current
   code (and has no `research.mode_backtest`, so step 0 cannot start) or is shadowed by it and
   never runs. It was not needed: step 0 reproduced the holdout file exactly (0 differences) with
   the current code (Task 7 in `.github/tdd/long-module-phase1.tdd.md`).
4. Label every trade with its side and with `pilih_mode` evaluated on the same production bars at
   its signal time (D1–D3).
5. Report per contender × symbol × side × condition: positions, Menang, mean gross bps and Hasil per
   1 juta. The report notes that these trades used the Pine bracket (0.20 % / 0.20 %, 60 minutes),
   the AI-latency tick clock and the Pine guards, not the new modes' exits.

Step 0 reads only trades that already exist. It tests no rule and changes no decision.

## 9. Mode A on BTC — training, lock, test

### 9.1 Signals and simulation

- **Event clock:** closes of the trigger candles (15m for A, 3m for D) inside the stage. At each
  close `t`, the tracker is updated first. If it signals, the gates run in this order: weekly
  window at `t` → segment edge (D10) → condition (NAIK for A) → open position on the pair → pause
  (`t` ≥ previous exit + 15 min) → daily caps (WIB day of `t`) → volatility guard (skip when
  ATR14 (1h) > 2 × the median of the last 720 closed-hour ATR14 values, the latest included; also
  skip while fewer than 720 values exist). On pass, the position opens at `t` and the touch is
  consumed (D5).
- **Entry:** the 1m bar opening at `t`; `entry_fill = open × (1 + side × 0.0001)`; quantity =
  notional / `entry_fill` (no venue rounding in the backtest).
- **Walk:** from the entry bar. At the bar opening `limit` minutes after entry, exit at its open
  (WAKTU). Otherwise: open beyond SL → exit at open (SL); open beyond TP → exit at open (TP); low
  (LONG) or high (SHORT) through SL → SL at the SL price, even if TP was also touched; else through
  TP → TP at the TP price. The exit fill moves 0.01 % against the position.
- **Money:** fee = 0.035 % × (entry notional + exit notional); `pnl_usdt` = side × quantity ×
  (exit fill − entry fill) − fee; `slippage_usdt` = the cost of both 0.01 % adjustments
  (informational, already inside the fills); `volume_usdt` = entry notional + exit notional.
- **Comparator "current SHORT in NAIK":** Mode D's trigger and exits with the condition gate NAIK
  instead of TURUN, the same gates otherwise, on the same stage. **Comparator "no trading":** 0.

### 9.2 Stages

- `train --mode A --pair BTC-USDT`: runs Mode A at 15m on [2025-08-01 00:00, 2026-08-05 00:00) WIB
  and the comparator. If 15m entries per NAIK day are below 3, it runs Mode A at 5m once more (EMA20
  on 5m, confirmation within 3 5m candles, entry at the next 5m open) and keeps 5m. Writes the
  result JSON, the trade logs and the lock `mode_A_BTC-USDT_lock.json` (exclusive). The lock binds
  the config sha256, the code sha256 (the package and every module it imports), the training data
  manifest sha256, Python major.minor, the chosen trigger timeframe and the training summary.
- `test --mode A --pair BTC-USDT`: refuses (exit 2) unless the lock exists and the current config,
  code, training data and Python version match it, and no run marker exists. It then writes the
  marker `holdout_runs/mode_A_BTC-USDT_<lock sha256>.json` exclusively before reading any test-stage
  bar, runs Mode A (locked timeframe) and the comparator on [2026-08-05 00:00, 2026-10-14 00:00)
  WIB, and writes the result JSON, the trade logs and the pass status: LOLOS, GAGAL, or BELUM CUKUP
  SAMPEL (< 40 positions).
- A lock may be deleted and recreated only while no marker exists for it. Output files are never
  overwritten. Exit codes: 0 ok, 1 expected failure (one `error:` line), 2 usage error or lock
  refusal.

### 9.3 Reports

`docs/research/` gets two Indonesian reports, step 0 and Mode A on BTC, built from the output
tables: the §2.9 table with Latihan and Ujian rows (Forward later), the two comparators, the pass
status with its three numbers, and the caveats of §11.

## 10. Testing

TDD with stdlib `unittest`: every behaviour gets a failing test first, and its RED output goes into
`.github/tdd/long-module-phase1.tdd.md`. Required cases:

- config: the verbatim block loads; an unknown key, a missing key or `ai_boleh_buka_tutup: true`
  refuses;
- `pilih_mode`: each branch, the exact boundary `gap == 0.2 × ATR` (not SIDEWAYS, strict `<`),
  `None` inputs;
- frames: no forming candle leaks into a value at `t`; 4h buckets close at 03/07/…/23 WIB; warm-up
  join; the stretch equals Pine's `FeatureFrame` value on the same bars;
- pullback: touch and confirmation in the same candle; confirmation on the third candle but not the
  fourth; one entry per touch; a blocked signal leaves the touch available (D5); the distance cap;
- simulate: SL-first when both are touched; gaps at the open; time exit at minute 90; slippage and
  fee arithmetic on a hand-computed trade; pause; daily caps across WIB midnight; weekly window
  edges (Mon 06:59 / 07:00, Fri 23:59 / Sat 00:00); segment edge (D10); the volatility guard;
- metrics: Hasil per 1 juta, Menang without time exits, without-top-3 with ties, the three statuses;
- lock: any changed byte in the config, code or data refuses `test`; a second `test` refuses; train
  never reads a day file on or after 2026-08-05;
- step 0: a forced mismatch against the holdout file stops it;
- structure: the §6.5 boundaries.

Global line coverage stays ≥ 80 %; `compileall`, `git diff --check` and secret hygiene pass.

## 11. Risks and limits

- **The test window is not fresh.** 5 August – 14 October 2026 was the Pine holdout, and the
  16 October report's trend findings came partly from it. The modes' numbers are the manager's, not
  fitted, which limits the damage; the forward test is the real gate.
- **Small samples.** With a per-trade spread of about 32 bps, 40 positions give a standard error
  of about ±250 per 1M (±210 at 60). A mode with no edge expects about −450 per 1M (fee 350 +
  slippage 100) and still passes a 40-position test about one time in ten.
- **Mode D is a proxy.** The live SHORT is the AI's decision with ATR-scaled exits and AI exits;
  the backtest's Mode D is the near-mean rule with 0.20 % / 0.20 % / 60-minute exits (decision 2).
- **Production prices, VST fills.** VST prices follow production closely, but live fills will
  differ by a few bps.

## 12. Out of scope (later phases, each with its own spec)

ETH and the k factor; Modes B, C and D2; the forward test on VST; the live engine (two concurrent
positions, venue-side TP/SL orders, a candle-close scheduler, YAML in the live bot, logging to
Supabase); AI analysis notes; the dashboard.

## 13. Workflow

This spec, then `superpowers:writing-plans` for the implementation plan
(`docs/superpowers/plans/2026-10-17-long-module-phase1.md`), then execution with TDD. The user picks
the execution method when the plan is ready. The run order is: copy and verify the data, step 0
and its report, `train`, review the training result and the 5m decision with the user, `test` once,
then the Mode A report.
