# Pine-adapted entry gate — design

Date: 2026-10-14 · Status: approved design, awaiting spec review · Supersedes the unexecuted
2026-10-12 trend-filter test plan · Evidence base:
`docs/research/2026-10-14-indicator-method-evidence.md`.

## 1. Intent

The user's manager proposed raising the bot's win rate with the trend logic of a TradingView
PineScript. The script **adapts** the existing pipeline (AI entry decision + code-side gates); it
does **not** replace the AI. Success is a higher win rate at a fixed 1:1 reward-to-risk, with
enough trades to keep volume flowing.

The Pine script contributes a **structure**, not numbers:

- a trend direction filter (the script used close vs EMA(125) on 5m);
- entry on a pullback against that direction (the script used RSI(5) < 40 long / > 55 short);
- a trend-strength filter (the script used ADX(11) > 20; DI+/DI− were computed but unused — this
  design adds DI agreement with the trade direction as an option).

The user considers the script's numbers inaccurate, so the parameters are searched on training data
and verified once on a locked holdout. The script's trailing stop (0.08% trigger, 0.01% offset),
its 0.02% fee and its position reversal are not carried over.

The evidence review found no rigorous out-of-sample support for "more powerful" oscillators: the
usual ones are near-duplicates (RSI14 vs the bot's stretch measure ρ = 0.99), and published
out-of-sample edges at this horizon are about 0–2 bps (WR ≈ 50–55% on a ±0.20% bracket). The design
therefore keeps one oscillator, adds the alternatives the evidence does support — **breakout
entries**, a **market-state filter**, and a **meta-labeling model** — and hardens the selection.

## 2. Decisions (user, 2026-10-14)

1. **Supply:** an average of at least 10 trades per trading day, measured over 20 trading days
   (individual days may be lower or higher). A trading day is a WIB day inside the weekly entry
   window (Mon 07:00 – Sat 00:00 WIB).
2. **Bracket:** take-profit = stop-loss = 0.20% for **both BTC and ETH** (RR 1:1).
3. **Per-symbol judgement:** BTC and ETH are judged separately; a symbol that fails is not traded.
4. **Volume profile:** an entry **location filter**, tested in the second selection stage.
5. **Exits:** bracket + time stop only. AI exits, the adverse cut, the neutral exit and the
   breakeven exit are off.
6. **Contenders:** (1) the Pine structure with parameters searched on training data, (2) the
   bot's existing logic, fixed, and (3) a meta-labeling model on top of contender 1's rule.
7. **Grid:** one oscillator (Wilder RSI14), pullback **or** breakout entries, three trend lookbacks,
   ADX/DI strength; the volatility state and the volume profile are second-stage filters.
8. **Pass rule — staged:** the holdout is a **gate to continue** (edge present, WR ≥ 57%); the 60%
   verdict is made per symbol after about 600 trades (section 8).
9. **Order flow** (Binance taker volume) is deferred to a separate study.
10. **Workflow:** superpowers and ECC plugins (section 12).

Defaults assumed unless the user objects:

- **WR** = take-profits / (take-profits + stop-losses); time stops are reported separately.
- **Gross** = average price move per trade over all trades (time stops included) after modelled
  slippage, before fees.
- **Money:** net per 1M volume is reported against the live baseline (−190) at every stage; it is
  not a gate.
- **Margin** stays 250 (25,000 notional): 10 trades/day ≈ 0.5M volume/day. Raising margin to
  reach 1M/day is a separate decision (margin-ladder coupling applies).

## 3. Current state (read 2026-10-14, non-secret settings)

| Area | Live value |
|---|---|
| Mode / symbols | `ai_volume`, BTC/USDT only, 3-minute ticks, 3m candles (40 requested → 39 closed) |
| Size | margin 250 × 100x = 25,000 notional |
| Targets | AI SL/TP as minimums; SL = max(AI, ATR×1.5) capped at `MAX_STOP_LOSS_PCT=0.4`; TP = max(AI, ATR×3, fee floor) ≈ **0.4%** (the "target 0.4%" in the meeting) |
| Gates | `ENTRY_STRETCH_GATE_MODE=enforce`, `ENTRY_GATE_DIRECTION=short` (K2 forward sample since 2026-10-10 19:31 WIB), stretch ≤ 0.75 ATR |
| Exits | neutral floor 56; AI exit (adverse cut 35, 25 calls/trade, 180 s min hold); time stop 60 min; 5 s monitor; all exits are market orders |
| Guards | 12 stop-losses/day, 15 consecutive losses, drawdown 1,500; cooldowns 0; funding blackout 10 min |
| Fee | real fills 0.035%/side; `TAKER_FEE_PCT=0.05` kept in config by user choice |

Baseline, 72 BTC trades since 2026-10-06 14:36 WIB: WR 38.9%, average price move +0.279% on wins and
−0.123% on losses, gross +166 per 1M, net −190 per 1M.

Prior evidence: M2 fade was +5 bps in-sample (best of 12 configs) and ≈0 forward; K2 (SHORT
near-mean) +3.2 ± 2.3 bps; near-mean in the 1h trend direction −0.9 ± 0.7 bps at 15 minutes.

## 4. Economics of the 1:1 bracket

Gross per 1M volume = 50 × bps per trade; net per 1M = 50 × (bps − 7) at 0.035%/side.

| WR at 1:1 (0.20%) | Gross bps/trade, no slippage | Net per 1M |
|---|---|---|
| 50% | 0 | −350 |
| 55% | +2 | −250 |
| 60% | +4 | −150 |
| 67.5% | +7 | 0 |

With realistic costs (0.25 bps entry slip, 0.25 bps take-profit slip, ~1 bp stop overshoot from
5-second polling, ~10% of trades ending on the time stop at about −0.5 bps), **WR 60% gives about
+2.8 bps, roughly −210 per 1M** — the live bot's level. −150 per 1M needs about **63%**;
breakeven about **71%**. Published out-of-sample edges at this horizon are 0–2 bps, so the expected
outcome is WR 50–55% and "no change, K2 continues" is the most likely result.

## 5. Phase 1 — offline evaluation (no bot change)

Research code lives in the repository under `src/research/pine_gate/` (tested, never imported by
`bot`), is synced to `~/replay/pine_gate_repo/` on the VPS, and runs there with `nice -n 19` and
the bot's venv; data goes to `~/replay/pine_gate/data/` and outputs to `~/replay/out/`. No orders,
no private endpoints, no restart. Apart from the research package and the unwired indicator module
(5.4), nothing in the repository changes; the bot's runtime behaviour does not change.

### 5.1 Pre-registration and the holdout lock

- `~/replay/pine_gate_spec_20261014.json`, written by `python -m research.pine_gate.cli spec`,
  holds every choice below (grids, boundaries, selection rules, contenders, model settings,
  controls, pass criteria). Its sha256 is written to project memory **before** any outcome is
  computed. This document, committed to git, is the human-readable copy.
- `python -m research.pine_gate.cli` has two evaluation stages:
  - `train` loads data only up to the training boundary (plus 60 minutes for trades to
    resolve), runs both selection stages and the model fit, and writes
    `~/replay/out/pine_gate_selection_<date>.json` with the chosen rule, the frozen model
    (coefficients, scaler, threshold), the training metrics and the spec sha256.
  - `test` refuses to run unless that selection file exists and its recorded spec sha256
    matches the current spec. Only then does it load holdout outcomes, once. Under the spec
    sha256 it also requires the code sha256 and the training-data hash the selection recorded,
    and writes the holdout file next to the selection file (its real folder, links resolved);
    any earlier holdout file there or in the output folder refuses the run. Before any holdout
    data is read it writes the run marker `<data dir>/holdout_runs/<spec sha256>.json` (spec,
    selection-file and code sha256, time, output folder) exclusively; an existing marker refuses
    the run, whatever folder the selection file was copied to.
  - Runbook: the spec JSON, its sha256 and the selection file's sha256 are committed before
    `test` runs; no local lock can stop a run on a second machine.
- A bug found after the test stage is fixed in a new script version, everything is rerun, and the
  change is documented in the report.

### 5.2 Data

- **Production klines (public, no keys):** `GET https://open-api.bingx.com/openApi/swap/v3/quote/klines`
  for BTC-USDT and ETH-USDT, 1m, with volume. BingX 1m history starts 2025-08-01 WIB
  (2025-07-31 has only 60 bars).
  - Fixed windows `startTime` + `endTime` of 1,440 bars; ≤ 1 request/second; every window must
    return exactly 1,440 consecutive bars or it is retried; never paginate on `startTime` alone.
  - All 5m/1h/4h bars are aggregated from 1m (BingX native 5m history is only about six weeks; the
    native bars that exist are used to check the aggregation).
- **VST data:** the existing BTC 3m/1m cache (`~/replay/edge_klines.json`, OHLC only).
- **Indicator warm-up for 1h/4h:** native BingX 1h and 4h klines from 2025-02-01 to 2025-08-01
  (features only, never outcomes); from 2025-08-01 the 1h/4h bars are aggregated from 1m, and the
  native bars of the overlap are used to check the aggregation.
- **New daily collector** (its own cron line; the existing `kline_keeper.py` is not modified):
  VST ETH 1m with volume (3m is aggregated from it), production BTC/ETH 1m. This is the forward
  dataset.
- **Supabase (read-only):** `ai_decisions` (BTC, `exchange=bingx`, since 2026-08-31, including HOLD),
  `trades` (BingX, including old ETH trades), `ai_call_metrics` (entry latency from 2026-09-25).
  Both calibration windows end at the holdout end, 2026-10-14 00:00 WIB (exclusive), so a later
  `calibrate` run reads the same rows.
- VST volume is **not** real market volume (2026-10-14: VST hourly BTC volume was 1.6×–6.3×
  production, ratio unstable), so the volume profile always uses production volume.

### 5.3 Time split (WIB)

| Segment | Range |
|---|---|
| Training | 2025-08-01 00:00 → 2026-08-05 00:00 (369 days) |
| Selection blocks inside training | B1 2025-08-01 → 11-01 · B2 → 2026-02-01 · B3 → 05-04 · B4 → 08-05 |
| Overfitting check (CSCV) | training cut into 8 equal time blocks |
| Holdout | 2026-08-05 00:00 → 2026-10-14 00:00 (70 days) |
| Holdout, last 20 trading days | 2026-09-16 → 2026-10-13 |

Features may look back across a boundary; outcomes may not. A trade must enter at least 60 minutes
before any block or segment boundary (purge), and the model's validation folds also skip the first
60 minutes after a boundary (embargo).

### 5.4 Indicators

- New module `src/bot/strategy/pine_indicators.py` (stdlib only), written test-first with
  `tests/test_pine_indicators.py`, **not wired into the bot**. The VPS harness imports a copy with
  the same sha256.
  - `rma` (Wilder: alpha 1/n, seeded with the mean of the first n values), `ema` series (alpha
    2/(n+1)), Wilder RSI, Wilder ATR, DMI/ADX matching Pine v5 `ta.dmi`, rolling highest/lowest,
    and percentile rank (share of the previous `length` values ≤ the current value, as Pine
    `ta.percentrank`).
  - Recursive smoothers (RMA, EMA and everything built on them) return `None` before 8 × period
    bars; window functions (highest, lowest, percentile rank) return `None` until their window is
    full.
- `src/bot/strategy/indicators.py` is not touched: the live stretch gate depends on its simple-mean
  RSI/ATR and on its `ema` (which returns the last close when data is short).
- No look-ahead: a feature at time t uses only bars whose open + interval ≤ t. A truncation test at
  500 random times must give identical features on truncated and full data (5m, 1h, 4h, volume
  profile, volatility percentile, stretch).

### 5.5 Contender 1 — two-stage grid

**Stage 1 — 36 base rules** (one set for BTC and ETH):

| Dimension | Values |
|---|---|
| Trend source (3) | E200: 5m close vs EMA(200), no band · H1: 1h EMA(20) − EMA(50) beyond ±0.25 × ATR14(1h) · H4: the same on 4h. Inside the band = neutral = no entry |
| Entry (4) | **Pullback** with Wilder RSI14 on 5m, band 35/65 or 40/60: long when RSI < lower in an uptrend, short when RSI > upper in a downtrend · **Breakout** over N = 12 or 24 closed 5m bars (1 or 2 hours): long when close > the highest high of the previous N bars in an uptrend, short when close < the lowest low of the previous N bars in a downtrend |
| Trend strength (3) | DMI(14, 14) on 5m (Pine `ta.dmi(14, 14)`): off · ADX > 20 · ADX > 20 with DI agreeing (DI+ > DI− for a long, DI− > DI+ for a short) |

**Stage 2 — filters, only on the 5 best base rules** (6 variants each, including "both off"):

| Filter | Values |
|---|---|
| Volatility state (3) | off · high: 1h ATR14 / close at or above the 67th percentile of its previous 720 closed 1h bars (30 days) · not high: below that percentile |
| Volume profile (2) | off · on: rolling 24-hour profile from production 1m volume, 5 bps bins, each bar's volume spread evenly over the bins its high–low range covers, POC = busiest bin (ties → nearest to price), price = the last closed 5m close. Pullback: long only if price ≤ POC, short only if price ≥ POC. Breakout: long only if price ≥ POC, short only if price ≤ POC |

At most 61 distinct rules are evaluated (36 + 5 × 5). Signals are read at each simulated bot tick
from the last closed 5m bar (and the last closed 1h/4h bar where needed).

### 5.6 Selection rule (training data only)

1. Run the one-position simulation (5.10) for each rule on training data, BTC and ETH together.
2. **Eligible:** at least 1,000 take-profit + stop-loss trades over training, and a combined supply
   of at least 10 trades per trading day. If no rule is eligible, selection continues without the
   supply condition and the result is reported as supply-limited.
3. **Rank** by the median over B1–B4 of WR − 2 × SE (SE clustered by WIB day); a rule must also show
   WR > 50% in at least 3 of the 4 blocks.
4. Stage 1 → the 5 best base rules → Stage 2 variants → the best variant by the same ranking.
5. **Stable area:** at least 4 of the 7 Stage-1 neighbours of the chosen base rule (one dimension
   changed) have a training WR no more than 3 points below the chosen base rule's; otherwise move
   to the next in rank.
6. **Overfitting checks:** the probability of backtest overfitting over all evaluated rules,
   computed by CSCV on the 8 training blocks, must be < 0.5; Hansen's SPA of each rule's
   direction value is reported: per training trading day, the mean over its trades of the rule
   side's gross minus a seeded random side's gross on the same entry bar and bracket (0 on a day
   without trades).
7. Freeze the choice in the selection file (5.1).

### 5.7 Contender 2 — the bot's own logic, fixed

- Near-mean gate exactly as live: |close − EMA21| ≤ 0.75 × ATR14 on the last 39 closed 3m bars,
  using `bot.strategy.indicators` and `build_setup_profile`.
- Direction from H1 (1h EMA20 − EMA50 beyond ±0.25 × ATR14(1h)); neutral = no entry.
- No tuning; it enters the holdout test as is.

### 5.8 Contender 3 — meta-labeling

- **Primary rule:** the trend source and entry of contender 1's frozen rule, with strength off and
  no Stage-2 filters, so the model sees as many candidates as possible. If contender 1 has no
  eligible rule, the best-ranked Stage-1 base rule is used.
- **Label:** 1 if the candidate's bracket trade ends with a positive gross return (take-profit, or a
  time stop above zero), else 0.
- **Features (8), computed at the tick from closed bars and signed by the trade side:** trend
  strength in ATR units for the chosen trend source; RSI14 (100 − RSI14 for a short); ADX14;
  DI+ − DI−; the volatility percentile of 5.5; distance to the 24-hour POC in 5m ATR14 units; hour of day
  (WIB) as sine and cosine. Standardised with training means and standard deviations.
- **Model:** L2-regularised logistic regression. Candidates overlap in time, so samples are weighted
  by uniqueness (the inverse of their average concurrency). The penalty C ∈ {0.01, 0.1, 1} is chosen
  by expanding-window validation over B1–B4 (fit on B1…Bk, validate on Bk+1) with the 5.3 purge and
  embargo, on mean log-loss.
- **Threshold:** the highest probability p\* at which the one-position simulation on the
  out-of-fold predictions (B2–B4) still delivers ≥ 10 trades per trading day; if none does,
  p\* = 0.5. The final model is refitted on all training data and frozen with its scaler and p\*.
- In the bot this is a dot product and a sigmoid; no ML library.

### 5.9 Reference and controls

- **Reference:** the Pine script's numbers as written (EMA125, RSI5 40/55, ADX(11) > 20 via
  `ta.dmi(11, 11)`), reported only.
- **C0:** always long and always short on the same tick clock — must show 50 ± 3% WR and gross
  within ±2 bps, or the fill model is fixed before anything else is read. It runs on training in
  `train` too; a failing training C0 makes `test` refuse to run.
- **C1:** a contender's entry times with a random side (200 seeds) — the value of the direction.
- **Permutation:** the trend-label series shifted by whole days k ∈ [7, D − 7], 1,000 shifts, timing
  and outcomes fixed.
- **Step 0 before metrics:** share of time in up/down/neutral per trend source per month and n per
  cell; a cell with n < 60 is "insufficient", not "failed".

### 5.10 Execution model (the bot's, not TradingView's)

- **Tick clock:** a tick starts 180 s after the previous one finishes; a tick with a candidate adds
  an AI latency sampled from live entry calls (successful entry calls from 2026-09-25 to
  2026-10-14 00:00 WIB, exclusive); the order goes out at tick + latency + 5 s; 20
  seeded start offsets.
- **Entry:** close of the 1m bar containing the order time, +0.25 bps adverse. Cooldown 5 minutes
  after each entry (one entry per 5m signal bar).
- **Bracket:** ±0.20% from the entry fill, walked on 1m bars from the next bar. A bar touching both
  levels is a stop (the count is reported; if > 2% of trades, an upper bound with them as wins is
  reported too). Gaps fill at the bar open.
- **Fills:** stop at level − s\*; take-profit at level − 0.25 bps; time stop at 60 minutes, at that
  1m close − 0.25 bps. s\* is the median stop overshoot of live `stop_loss` exits opened from
  2026-09-12 to 2026-10-14 00:00 WIB (exclusive), each stop level reconstructed from its stored decision (AI stop, ATR at decision, 0.4% cap),
  rounded up to 0.5 bp. If fewer than 15 stops can be reconstructed, s\* = 1 bp. Sensitivity
  0/1/2 bps.
- **Fees:** 0.035% per side; sensitivity 0.05%.
- **Portfolio:** one position across BTC and ETH; BTC first when both signal (ETH-first as a
  sensitivity check; co-signal frequency reported).
- **Guards and limits as live:** entry window, 12 stops/day, 15 consecutive losses, drawdown
  1,500 USDT at 25,000 notional, 34 attempts/day, 1M daily volume cap, funding blackout 10 minutes
  around actual funding times.

### 5.11 Holdout gate

The three contenders are each run once on the holdout. Within each symbol, p-values are
Holm-corrected for the three (an absent contender counts with p = 1). A contender **passes the gate for a symbol** when all of these hold:

1. At least 60 take-profit + stop-loss trades for that symbol.
2. Holm-adjusted p < 0.05, one-sided, WR above C0's WR (the mean of always-long and always-short over
   the same period; day-block bootstrap, 10,000 resamples) — the edge is present.
3. WR ≥ 57%.
4. Gross > 0 (after modelled slippage, before fees).
5. WR > 50% in each half of the holdout.
6. Robustness: WR ≥ 55% with 2 bps stop overshoot, and WR > 50% in at least 15 of 20 tick offsets.
7. The edge has the claimed source: for contenders 1 and 2, trend permutation p < 0.05; for
   contender 3, the WR of the trades it takes is higher than the WR of its primary rule run on its
   own over the same period (day-block bootstrap p < 0.05).
8. Supply of the passing symbols combined: ≥ 10 trades per trading day on average, over the
   holdout and over its last 20 trading days.
9. Controls behave as expected, and contender 1's training PBO was < 0.5.

Choosing what moves on:

- One contender is used for both symbols: the one passing on more symbols; then the higher pooled
  WR lower bound; then the simpler one (2, then 1, then 3).
- A symbol that fails under the chosen contender is not traded.
- Contender 1 or 3 is used only if it beats contender 2 on the holdout.
- If nothing passes, the bot is not changed, K2 continues, and the manager receives the WR-vs-supply
  table per contender and symbol.

Passing the gate means "an edge is present and consistent with the target", not "WR 60% proven":
with about 250–500 holdout trades per symbol, a true 60% has a 95% interval of roughly 54–66%.

### 5.12 VST versus production

Execution happens on VST, so the production backtest must be a valid stand-in:

- signal side from VST vs production 5m agrees on ≥ 97% of the ticks where either side signals
  (same side ÷ ticks with a non-zero side on VST or production; ticks where both are flat are
  left out);
- bracket outcome on VST vs production 1m agrees on ≥ 95% of the ticks where both signal the same
  side;
- WR differs by ≤ 2 points, each WR taken at that side's own signal ticks with its own side and
  bars (VST's signals on VST bars, production's on production bars);
- the counts behind each share are reported (ticks where either signals, where both signal the
  same side, production and VST signal ticks with a resolved outcome); without a tick where
  either side signals the check fails.

BTC is checked on the VST cache since 2026-09-09. ETH is checked once the collector has two weeks
of VST ETH data; until then ETH cannot move to enforce. If the check fails, the forward read on VST
decides.

### 5.13 AI decision-level view

On the stored BTC decisions since 2026-08-31, with the chosen contender and the 0.20%/0.20%
bracket:

| Bucket | Definition |
|---|---|
| A | every AI candidate (OPEN, confidence ≥ 0.75, risk review approved) |
| B | AI candidate with a same-side signal (design X) |
| E | signal present, AI said HOLD |
| F | every signal at the stored tick times |

This answers whether the gate raises the WR of the AI's own entries, whether the AI adds anything
over the rule, and what supply design X would get (corrected for ticks not stored while a position
was open).

### 5.14 Outputs

- `~/replay/out/pine_gate_edge_<date>.{json,md}` (report in Indonesian) with the sha256 of the
  spec, the data files, the selection file and the script at the top.
- Contents: both training stages, PBO and SPA, the model's validation log-loss and calibration,
  holdout results per contender and symbol, controls, permutation, cadence cost, guard/supply grid,
  VST divergence, fill calibration, decision-level buckets.
- Results recorded in project memory.

### 5.15 TradingView visual aid

`docs/tradingview/pine_adapted_gate_v2.pine` (Pine v5) draws contender 1's structure on a
TradingView chart so the manager can see where signals appear. Every grid dimension is an input
(the meta-labeling model is not, because its coefficients exist only after Phase 1). It is **not
evidence**:

- Its Strategy Tester fills orders at TradingView bar prices; it does not model the bot's tick
  clock, AI latency, 5-second polling overshoot, daily guards or funding blackouts.
- Its volume profile is built from the chart's own bars, not from 1m production bars.
- Its default inputs are placeholders. After Phase 1 freezes the selection, the defaults are
  updated to the frozen parameters.
- Parameters must not be chosen from its Strategy Tester: the chart contains the locked holdout
  (2026-08-05 → 2026-10-14), so choosing from it would leak the holdout into the selection.
- The original script's BingX signal-bot alert codes are deliberately left out, because those
  codes can trigger orders on their owner's signal bot.

## 6. Integration design, chosen with Phase 1 data

- **X — gate after the AI:** the AI is called every tick as today; code accepts only a same-side
  signal. Least code; supply is the AI ∩ signal overlap (estimated 2–6 per day).
- **Y — the rule proposes, the AI approves or vetoes:** without a signal there is no AI call (about
  90–95% fewer tokens); with a signal the AI receives a small `entry_candidate` block and may veto
  via its risk review. Needs a prompt change; an optional offline probe (~2–3M MiMo tokens, asked
  first) measures the approval rate.
- **Rule:** X if its corrected supply is ≥ 10/day and bucket B passes; otherwise Y if signals ×
  approval rate ≥ 10/day and the approved WR is at most 1 point below the rule's WR. If contender 3
  wins, its model filter runs before the AI in either design.

## 7. Phase 2 — bot integration (only if Phase 1 passes; new approval)

Always:

- `src/bot/strategy/pine_gate.py`: the frozen winning specification (rule constants and, if
  contender 3 wins, the model's coefficients, scaler and threshold, all pinned by tests rather than
  env vars) and `evaluate()` → long / short / none / unavailable.
- `MarketIntelligence.entry_gate_snapshot()` fetches the needed candles from the VST client,
  **separately from `build_context`**: `_deduplicated_market_context`
  (`src/bot/ai/compact_context.py:141-143`) deep-copies the whole context into the prompt, so new
  features must not reach the AI (that would change its decisions and spoil the K2 sample).
- `src/bot/ai/orchestration.py`: snapshot at the start of `next_trade_plan`; a `gate.pine` verdict
  recorded in `_stretch_gate_verdict` (also on HOLD rows); rejection in `_build_entry_candidate`
  under a new `ENTRY_PINE_GATE_MODE=off|shadow|enforce`, separate from `ENTRY_GATE_DIRECTION`
  (whose check sits behind the stretch-mode early return). Unavailable data blocks in enforce
  (fail closed).
- Fixed bracket: `FIXED_STOP_LOSS_PCT` / `FIXED_TAKE_PROFIT_PCT` (default 0 = current behaviour;
  both 0 or both > 0) in `_resolve_trade_targets`, bypassing the AI's values, ATR scaling, the stop
  cap and the take-profit floor; the liquidation check stays.
- Exits by env only: `VOLUME_NEUTRAL_EXIT_ENABLED=false`, `AI_EXIT_MAX_CALLS_PER_TRADE=0` (an
  explicit 0 means no AI exit calls, `src/bot/ai/exit_policy.py:220-226`),
  `AI_EXIT_ADVERSE_CUT_USDT=0`, `POSITION_HOLD_MINUTES=60`, `COOLDOWN_MINUTES_AFTER_ENTRY=5`.

Only if the winner needs it:

- **H1/H4 trend or the volatility state:** 1h/4h candles (limit ≥ 750 for the 720-bar percentile),
  cached until the next bar closes.
- **Volume profile:** `src/bot/exchange/bingx_reference_market.py`, a read-only production client:
  host as a module constant, GET klines only, no credentials or signing, redirects refused, not an
  implementation of the exchange protocol, fenced by tests in the style of
  `tests/test_telemetry_poller.py`. This is a deliberate exception to the VST-only invariant for
  market data and **needs explicit user approval at that point**; it is recorded in CLAUDE.md.
- **ETH:** `SYMBOLS=BTC/USDT,ETH/USDT`; review the +0.05 non-first-symbol score bonus
  (`src/bot/ai/orchestration.py:278`).
- **Design Y:** return before the AI call when there is no candidate; an `entry_candidate` block
  in `build_entry_payload` and one prompt sentence.

Tests: `tests/test_pine_indicators.py` (hand-computed cases, a golden 5m fixture, truncation),
`tests/test_pine_gate.py` (including the model's frozen coefficients if contender 3 wins),
`PineGateTests` and `FixedBracketTests` in `tests/test_ai_orchestration.py` (following
`StopLossCeilingTests`), an identical AI payload with the gate in shadow/enforce
(`tests/test_compact_context.py`), `tests/test_market_intelligence.py`, and settings tests. TDD
evidence: `.github/tdd/pine-entry-gate.tdd.md` and `.github/tdd/fixed-entry-bracket.tdd.md`;
`.env.example` and CLAUDE.md updated.

## 8. Phase 3 — rollout and evaluation

1. **Merge** with every new setting at its default (no behaviour change). Restart only when flat;
   the best slot is Saturday ≥ 01:00 WIB.
2. **Shadow** for ≥ 5 trading days or ≥ 100 signalling ticks: recorded `gate.pine` verdicts must
   recompute exactly offline; signal frequency must match the harness; the frozen harness is rerun
   on data after the freeze date (≥ 60 trades: continue if WR ≥ 55% and gross > 0; stop if
   WR < 50% after 100 trades).
3. **Enforce** on a weekend while flat; the switch time ends the K2 sample and starts the live
   evaluation. Rollback = restore env and restart while flat.
4. **Day 20 (supply):** the combined average must be ≥ 10 trades per trading day.
5. **The 60% verdict, per symbol, after about 600 take-profit + stop-loss trades** (holdout +
   post-freeze forward + live; at ~5 trades/day/symbol that is roughly 3–4 months of data in total):
   confirmed if WR ≥ 60%; below target if WR < 57%; otherwise keep collecting. Live trades are also
   reported on their own; if live WR differs from the simulated WR by more than 3 points over at
   least 100 live trades, the live figure decides. Secondary: net per 1M against the −190 baseline.
6. **Early stop** (checked on trading days 5, 10, 15 of live): WR < 47% at ~50 trades, < 51% at
   ~100, < 52.5% at ~150; supply < 7/day on day 10; fill model agreeing with live < 90%; any safety
   issue → roll back.

## 9. Risks

- **The target exceeds published evidence:** out-of-sample edges at this horizon are 0–2 bps; WR 60%
  would beat all of them.
- **Overfitting the search:** one oscillator, two-stage selection (at most 61 rules), at least 1,000
  training trades per eligible rule, block consistency, PBO < 0.5, the locked holdout and the
  forward read.
- **Cadence:** the bot enters 0–3.5 minutes plus 20–64 seconds after a signal; a pullback or breakout
  edge may be front-loaded.
- **VST vs production wicks:** up to 3 bps against a 20 bps bracket; ADX and breakouts depend on
  highs and lows.
- **Stop overshoot from 5-second polling:** each extra bp costs about 20 per 1M.
- **Others:** regime dependence, supply (a selective model trades less), feature leakage into the
  AI, production-data availability (rate limits, outages), and limited power of a short live test.

## 10. Open questions (do not block Phase 1)

1. Does the manager accept WR = TP/(TP+SL), the staged gate and the ~600-trade verdict?
2. Margin at enforce: 250 (≈0.5M/day) or 500 (≈1M/day)?
3. Budget for the design-Y probe (~2–3M tokens), if Y is considered.
4. A taker-flow study (Binance archive + live Binance feed from the VPS) is deferred; it needs its
   own approval, including the file downloads.

## 11. Verification

- **Phase 1:** C0 at ~50% and within ±2 bps; truncation test passes; PBO and SPA computed; model
  calibration reported; the spec → selection → test sha256 chain proves the holdout was untouched
  until the freeze; pm2 `ai-perp-volume` does not restart and VPS load stays normal.
- **Phase 2:** `PYTHONPATH=src python -m compileall src tests`;
  `PYTHONPATH=src python -m coverage run -m unittest discover -s tests` then `coverage report`
  (≥ 80%); `git diff --check`; `pip check`; secret hygiene; Python 3.10 (VPS) and 3.11 (CI).
- **Phase 3:** a daily report of WR per symbol, supply, gross/net per 1M and fill-model agreement.

## 12. Workflow

- This spec → user review → `superpowers:writing-plans` for the Phase 1 implementation plan →
  `superpowers:subagent-driven-development` (or `executing-plans`) with
  `superpowers:test-driven-development`.
- ECC: `ecc:tdd-guide` for `pine_indicators.py`; `ecc:python-reviewer` and `ecc:code-reviewer`
  after code; `ecc:security-reviewer` for the production-data collector and any reference client;
  `ecc:silent-failure-hunter` for the fail-closed paths.
- Before claiming results: `superpowers:verification-before-completion`; for PRs:
  `superpowers:requesting-code-review` and `superpowers:finishing-a-development-branch`.
