# Pine-adapted entry gate — design

Date: 2026-10-14 · Status: approved design, awaiting spec review · Supersedes the unexecuted
2026-10-12 trend-filter test plan.

## 1. Intent

The user's manager proposed raising the bot's win rate with the trend logic of a TradingView
PineScript. The script **adapts** the existing pipeline (AI entry decision + code-side gates); it
does **not** replace the AI. Success is a higher win rate at a fixed 1:1 reward-to-risk, with
enough trades to keep volume flowing.

The Pine script contributes a **structure**, not numbers:

- a trend direction filter (the script used close vs EMA(125) on 5m);
- entry on a pullback against that direction (the script used RSI(5) < 40 long / > 55 short);
- a trend-strength filter (the script used ADX(11) > 20; DI+/DI− were computed but unused).

The user considers the script's numbers inaccurate, so the parameters are searched on training data
and verified once on a locked holdout. The script's trailing stop (0.08% trigger, 0.01% offset),
its 0.02% fee and its position reversal are not carried over.

## 2. Decisions (user, 2026-10-14)

1. **Supply:** an average of at least 10 trades per trading day, measured over 20 trading days
   (individual days may be lower or higher). A trading day is a WIB day inside the weekly entry
   window (Mon 07:00 – Sat 00:00 WIB).
2. **Bracket:** take-profit = stop-loss = 0.20% for **both BTC and ETH** (RR 1:1).
3. **Per-symbol pass:** BTC and ETH must **each** reach WR ≥ 60% at 1:1 on their own. A symbol
   that fails is not traded.
4. **Volume profile:** used as an entry **location filter**.
5. **Exits:** bracket + time stop only. AI exits, the adverse cut, the neutral exit and the
   breakeven exit are off.
6. **Parameters:** searched on training data with the Pine structure (contender 1). The bot's
   existing logic is a fixed contender (contender 2) that the search must beat.
7. **Workflow:** superpowers and ECC plugins (section 12).

Defaults assumed unless the user objects:

- **WR** = take-profits / (take-profits + stop-losses); time stops are reported separately.
- **Money floor:** gross ≥ +3.2 bps per trade (no worse than the live bot's −190 per 1M);
  target +4 bps (−150 per 1M).
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
breakeven about **71%**. A rough drift model says 60% at 1:1 from trend alone would need price to
lean 2–4% per day in the trade's favour, so the pullback timing has to carry the edge. "No change,
K2 continues" is an acceptable outcome.

## 5. Phase 1 — offline evaluation (no bot change)

All research code runs on the VPS under `~/replay/` with `nice -n 19` and the bot's venv. No
orders, no private endpoints, no restart. The only repository change in Phase 1 is the unwired
indicator module (5.4).

### 5.1 Pre-registration and the holdout lock

- `~/replay/pine_gate_spec_20261014.json` holds every choice below (grid, boundaries, selection
  rule, contenders, controls, pass criteria). Its sha256 is written to project memory **before**
  any outcome is computed. This document, committed to git, is the human-readable copy.
- `~/replay/pine_gate_edge.py` has two stages:
  - `--stage train` loads data only up to the training boundary (plus 60 minutes for trades to
    resolve), runs the grid, and writes `~/replay/out/pine_gate_selection_<date>.json` with the
    chosen parameters, the training metrics and the spec sha256.
  - `--stage test` refuses to run unless that selection file exists and its recorded spec sha256
    matches the current spec. Only then does it load holdout outcomes, once.
- A bug found after the test stage is fixed in a new script version, everything is rerun, and the
  change is documented in the report.

### 5.2 Data

- **Production klines (public, no keys):** `GET https://open-api.bingx.com/openApi/swap/v3/quote/klines`
  for BTC-USDT and ETH-USDT, 1m, with volume.
  - Fixed windows `startTime` + `endTime` of 1,440 bars; ≤ 1 request/second; every window must
    return exactly 1,440 consecutive bars or it is retried; never paginate on `startTime` alone.
  - Native 5m/1h/4h klines are fetched too and must equal the bars aggregated from 1m.
  - If 1m history does not reach the start date, training starts at the earliest complete WIB day;
    fewer than 120 training days stops Phase 1 with a "data insufficient" report.
- **VST data:** the existing BTC 3m/1m cache (`~/replay/edge_klines.json`, OHLC only).
- **New daily collector** (its own cron line; the existing `kline_keeper.py` is not modified):
  VST ETH 1m/3m with volume, production BTC/ETH 1m. This is the forward dataset.
- **Supabase (read-only):** `ai_decisions` (BTC, `exchange=bingx`, since 2026-08-31, including HOLD),
  `trades` (BingX, including old ETH trades), `ai_call_metrics` (entry latency since 2026-09-25).
- VST volume is **not** real market volume (2026-10-14: VST hourly BTC volume was 1.6×–6.3×
  production, ratio unstable), so the volume profile always uses production volume.

### 5.3 Time split (WIB)

| Segment | Range |
|---|---|
| Training | 2025-10-15 00:00 → 2026-08-05 00:00 (or from the earliest complete day) |
| Holdout | 2026-08-05 00:00 → 2026-10-14 00:00 (70 days) |
| Holdout, last 20 trading days | 2026-09-16 → 2026-10-13 |

Features may look back across a boundary; outcomes may not. A training trade must enter at least
60 minutes before the boundary.

### 5.4 Indicators

- New module `src/bot/strategy/pine_indicators.py` (stdlib only), written test-first with
  `tests/test_pine_indicators.py`, **not wired into the bot**. The VPS harness imports a copy with
  the same sha256.
  - `rma` (Wilder: alpha 1/n, seeded with the mean of the first n values), `ema` series (alpha
    2/(n+1)), Wilder RSI, Wilder ATR, DMI/ADX matching Pine v5 `ta.dmi`.
  - Returns `None` when there are fewer than 8 × period bars.
- `src/bot/strategy/indicators.py` is not touched: the live stretch gate depends on its simple-mean
  RSI/ATR and on its `ema` (which returns the last close when data is short).
- No look-ahead: a feature at time t uses only bars whose open + interval ≤ t. A truncation test at
  500 random times must give identical features on truncated and full data (5m, 1h, 4h, volume
  profile, stretch).

### 5.5 Contender 1 — Pine structure, parameters from data

One parameter set for BTC and ETH. 48 combinations:

| Dimension | Values |
|---|---|
| Trend source | E200: 5m close vs EMA(200), no band · H1: 1h EMA(20) − EMA(50) beyond ±0.25 × ATR14(1h) · H4: the same on 4h. Inside the band = neutral = no entry |
| Pullback timing | Wilder RSI on 5m, length {5, 14} × symmetric thresholds {35/65, 40/60}: long when RSI < lower in an uptrend, short when RSI > upper in a downtrend |
| Trend strength | ADX(14) on 5m: {off, > 20} |
| Volume profile | {off, on}: rolling 24-hour profile from production 1m volume, 5 bps bins, each bar's volume spread evenly over the bins its high–low range covers; POC = busiest bin (ties → nearest to price). Price = the last closed 5m close at the tick. Long only if price ≤ POC, short only if price ≥ POC |

Signals are read at each simulated bot tick from the last closed 5m bar (and the last closed 1h/4h
bar for H1/H4).

### 5.6 Selection rule (training data only)

1. For each combination, run the one-position simulation (5.9) on training data, BTC and ETH
   together.
2. Keep combinations whose combined supply is ≥ 10 trades per trading day. If none qualifies,
   continue without this condition and report the result as supply-limited.
3. Rank by the lower bound WR − 2 × SE, with SE clustered by WIB day.
4. **Stable area:** the chosen combination needs at least 3 of its 6 neighbours (one dimension
   changed) at WR ≥ 57% on training data; otherwise take the next in rank.
5. Freeze the choice in the selection file (5.1).

### 5.7 Contender 2 — the bot's own logic, fixed

- Near-mean gate exactly as live: |close − EMA21| ≤ 0.75 × ATR14 on the last 39 closed 3m bars,
  using `bot.strategy.indicators` and `build_setup_profile`.
- Direction from H1 (1h EMA20 − EMA50 beyond ±0.25 × ATR14(1h)); neutral = no entry.
- No tuning; it enters the holdout test as is.

### 5.8 Reference and controls

- **Reference:** the Pine script's numbers as written (EMA125, RSI5 40/55, ADX(11) > 20 via
  `ta.dmi(11, 11)`), reported only.
- **C0:** always long and always short on the same tick clock — must show 50 ± 3% WR and gross
  within ±2 bps, or the fill model is fixed before anything else is read.
- **C1:** the contender's entry times with a random side (200 seeds) — the value of the direction.
- **Permutation:** the trend-label series shifted by whole days k ∈ [7, D − 7], 1,000 shifts, timing
  and outcomes fixed.
- **Step 0 before metrics:** share of time in up/down/neutral per trend source per month and n per
  cell; a cell with n < 60 is "insufficient", not "failed".

### 5.9 Execution model (the bot's, not TradingView's)

- **Tick clock:** a tick starts 180 s after the previous one finishes; a tick with a candidate adds
  an AI latency sampled from live entry calls; the order goes out at tick + latency + 5 s; 20
  seeded start offsets.
- **Entry:** close of the 1m bar containing the order time, +0.25 bps adverse. Cooldown 5 minutes
  after each entry (one entry per 5m signal bar).
- **Bracket:** ±0.20% from the entry fill, walked on 1m bars from the next bar. A bar touching both
  levels is a stop (the count is reported; if > 2% of trades, an upper bound with them as wins is
  reported too). Gaps fill at the bar open.
- **Fills:** stop at level − s\*; take-profit at level − 0.25 bps; time stop at 60 minutes, at that
  1m close − 0.25 bps. s\* is the median stop overshoot of live `stop_loss` exits since 2026-09-12,
  each stop level reconstructed from its stored decision (AI stop, ATR at decision, 0.4% cap),
  rounded up to 0.5 bp. If fewer than 15 stops can be reconstructed, s\* = 1 bp. Sensitivity
  0/1/2 bps.
- **Fees:** 0.035% per side; sensitivity 0.05%.
- **Portfolio:** one position across BTC and ETH; BTC first when both signal (ETH-first as a
  sensitivity check; co-signal frequency reported).
- **Guards and limits as live:** entry window, 12 stops/day, 15 consecutive losses, drawdown
  1,500 USDT at 25,000 notional, 34 attempts/day, 1M daily volume cap, funding blackout 10 minutes
  around actual funding times.

### 5.10 Holdout test and pass criteria

Contenders 1 and 2 are each run once on the holdout; p-values are Holm-corrected for the two.
Criteria apply **per symbol, BTC and ETH each**, at s\* on the bot's tick clock:

1. At least 60 take-profit + stop-loss trades.
2. Holm-adjusted p < 0.05, one-sided, WR above C0's WR (the mean of always-long and always-short
   over the same period; day-block bootstrap, 10,000 resamples).
3. WR ≥ 60% and gross ≥ +3.2 bps per trade, where gross covers every trade (time stops included)
   after modelled slippage and before fees (whether +4 bps is met is reported).
4. Each half of the holdout: WR ≥ 57% and gross > 0.
5. Robustness: WR ≥ 58% with 2 bps stop overshoot, and WR ≥ 60% in at least 15 of 20 tick offsets.
6. Trend permutation p < 0.05.
7. Supply of the passing symbols combined: ≥ 10 trades per trading day on average, over the
   holdout and over its last 20 trading days.
8. Controls behave as expected.

Outcome rules:

- A symbol that fails is not traded.
- If both contenders pass, the higher WR lower bound wins; a tie goes to contender 2 (simpler).
- Contender 1 is used only if it beats contender 2 on the holdout.
- If nothing passes, the bot is not changed, K2 continues, and the manager receives the WR-vs-supply
  table per contender and symbol.

### 5.11 VST versus production

Execution happens on VST, so the production backtest must be a valid stand-in:

- signal side from VST vs production 5m agrees on ≥ 97% of ticks;
- bracket outcome on VST vs production 1m agrees on ≥ 95% of entries;
- WR differs by ≤ 2 points.

BTC is checked on the VST cache since 2026-09-09. ETH is checked once the collector has two weeks
of VST ETH data; until then ETH cannot move to enforce. If the check fails, the forward read on VST
decides.

### 5.12 AI decision-level view

On the stored BTC decisions since 2026-08-31, with the winning specification and the 0.20%/0.20%
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

### 5.13 Outputs

- `~/replay/out/pine_gate_edge_<date>.{json,md}` (report in Indonesian) with the sha256 of the
  spec, the data files, the selection file and the script at the top.
- Contents: training grid, holdout results per contender and symbol, controls, permutation, cadence
  cost, guard/supply grid, VST divergence, fill calibration, decision-level buckets.
- Results recorded in project memory.

## 6. Integration design, chosen with Phase 1 data

- **X — gate after the AI:** the AI is called every tick as today; code accepts only a same-side
  signal. Least code; supply is the AI ∩ signal overlap (estimated 2–6 per day).
- **Y — the rule proposes, the AI approves or vetoes:** without a signal there is no AI call (about
  90–95% fewer tokens); with a signal the AI receives a small `entry_candidate` block and may veto
  via its risk review. Needs a prompt change; an optional offline probe (~2–3M MiMo tokens, asked
  first) measures the approval rate.
- **Rule:** X if its corrected supply is ≥ 10/day and bucket B passes; otherwise Y if signals ×
  approval rate ≥ 10/day and the approved WR is at most 1 point below the rule's WR.

## 7. Phase 2 — bot integration (only if Phase 1 passes; new approval)

Always:

- `src/bot/strategy/pine_gate.py`: the frozen winning specification (constants pinned by tests,
  not env vars) and `evaluate()` → long / short / none / unavailable.
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

- **H1/H4 trend:** 1h/4h candles (limit 500), cached until the next bar closes.
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
`tests/test_pine_gate.py`, `PineGateTests` and `FixedBracketTests` in
`tests/test_ai_orchestration.py` (following `StopLossCeilingTests`), an identical AI payload with the
gate in shadow/enforce (`tests/test_compact_context.py`), `tests/test_market_intelligence.py`, and
settings tests. TDD evidence: `.github/tdd/pine-entry-gate.tdd.md` and
`.github/tdd/fixed-entry-bracket.tdd.md`; `.env.example` and CLAUDE.md updated.

## 8. Phase 3 — rollout and the 20-day evaluation

1. **Merge** with every new setting at its default (no behaviour change). Restart only when flat;
   the best slot is Saturday ≥ 01:00 WIB.
2. **Shadow** for ≥ 5 trading days or ≥ 100 signalling ticks: recorded `gate.pine` verdicts must
   recompute exactly offline; signal frequency must match the harness; the frozen harness is rerun
   on data after the freeze date (≥ 60 trades: pass if WR ≥ 55% and gross > 0; stop if WR < 50%
   after 100 trades).
3. **Enforce** on a weekend while flat; the switch time ends the K2 sample and starts the 20-day
   evaluation. Rollback = restore env and restart while flat.
4. **Verdict at day 20, per symbol:** pass = WR ≥ 60% and combined average ≥ 10 trades/day;
   inconclusive = WR 55–60% → extend 20 days; fail = WR < 55%. Secondary: net per 1M against the
   −190 baseline. About 200 trades still leave roughly ±7 points, so these three verdicts are
   agreed with the manager in advance.
5. **Early stop** (checked on trading days 5, 10, 15): WR < 47% at ~50 trades, < 51% at ~100,
   < 52.5% at ~150; supply < 7/day on day 10; fill model agreeing with live < 90%; any safety issue
   → roll back.

## 9. Risks

- **Overfitting the search:** small grid, one set for both symbols, the stable-area rule, the
  locked holdout and the forward read.
- **Cadence:** the bot enters 0–3.5 minutes plus 20–64 seconds after a signal; the pullback edge
  may be front-loaded.
- **VST vs production wicks:** up to 3 bps against a 20 bps bracket; ADX depends on highs and lows.
- **Stop overshoot from 5-second polling:** each extra bp costs about 20 per 1M.
- **Others:** regime dependence, supply, feature leakage into the AI, production-data availability
  (rate limits, outages), and limited power of a 20-day test.

## 10. Open questions (do not block Phase 1)

1. Does the manager accept WR = TP/(TP+SL) and the three verdicts?
2. Margin at enforce: 250 (≈0.5M/day) or 500 (≈1M/day)?
3. Budget for the design-Y probe (~2–3M tokens), if Y is considered.

## 11. Verification

- **Phase 1:** C0 at ~50% and within ±2 bps; truncation test passes; the spec → selection → test
  sha256 chain proves the holdout was untouched until the freeze; pm2 `ai-perp-volume` does not
  restart and VPS load stays normal.
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
