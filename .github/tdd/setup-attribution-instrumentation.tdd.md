# Setup Attribution Instrumentation — TDD Evidence

## User journey

Make the *shape* of an entry visible to the feedback loop, so a repeatedly
losing setup can be identified from closed trades instead of being repeated
indefinitely. This is Phase 3 step 1: instrumentation only, no entry gate.

## Evidence that motivated the change

The BingX cycle opened LONG at 78,293.8 / 78,309.8 / 78,301.8 — effectively the
same price at the top of a 77,110–78,416 range — and was stopped out all three
times (−99.46 / −99.28 / −104.01). It also opened SHORT at 77,110.3, the range
low (−227.46), and at 77,495.7 (−91.03). Long the high, short the low.

`AttributionAnalyzer` buckets by symbol, direction, factor, trigger, hour and
confidence band. Every one of those puts the three losing LONGs in the same
bucket as every other BTC LONG, so no grouping could isolate the pattern.
`build_trade_decision_memory` is worse: it derives its "winning/losing patterns"
from the **exit reason**, which with this data yields the tautology "neutral_exit
wins, stop_loss loses" and teaches nothing about entries.

After the exit-geometry fix the residual bleed is gross −0.0443% of volume across
55 trades. Barrier arithmetic gives an expected gross of ≈0% for any fair barrier
pair, so the remainder is directional/timing quality, not exit geometry — which
is what this instrumentation is meant to expose.

## RED evidence

`tests.test_setup_profile` — module did not exist:

```text
ModuleNotFoundError: No module named 'bot.arena.setup_profile'
```

`tests.test_ai_orchestration.SetupMetadataTests` and
`tests.test_arena_attribution.SetupAttributionTests` — 6 failures/errors:

```text
TypeError: _factor_metadata_for_symbol() takes 2 positional arguments but 3 were given
AttributeError: 'AttributionSummary' object has no attribute 'by_setup'
AssertionError: 'by_setup' not found in {...}
```

`tests.test_arena_attribution.SetupDiagnosisTests` — 1 failure (no card emitted).

Two expectations in the first RED run were wrong in the test, not the code:
`atr_pct` 0.249% belongs to `atr_mid`, not `atr_low`. Corrected before GREEN.

## GREEN implementation

- New `bot/arena/setup_profile.py`. `build_setup_profile(features, direction)`
  classifies an entry on three axes and returns `setup_key` plus the raw
  measurements, so thresholds can be retuned later without re-instrumenting:
  - `direction` — LONG / SHORT
  - `stretch_bucket` — `(last_price - ema_21) / atr_14` against ±0.75 ATR. This
    is the axis that expresses "bought while already extended", and being in ATR
    units it is comparable across symbols and volatility regimes in a way a raw
    price or percentage is not.
  - `atr_regime` — ATR as a percentage of price, split at 0.15% / 0.35%.

  2 × 3 × 3 = 18 buckets. At the observed ~55 trades/day that is ~3 per bucket
  per day, so a bucket reaches a usable sample in about a week.

  It never raises: unusable features degrade to `unknown` so the trade is still
  recorded rather than dropped.

- `AIOrchestrator._factor_metadata_for_symbol` now takes the direction and nests
  the profile under `factor_metadata.setup`. Nesting inside the existing jsonb
  column deliberately avoids a Supabase migration.

- `AttributionSummary` gains `by_setup`, populated via `_setup_key()`, which
  tolerates JSON-string columns and skips rows written before instrumentation
  rather than lumping them into an `unknown` bucket that would dilute the real
  ones.

- `DiagnosisGenerator` emits a "Losing entry setup" card once a bucket has at
  least `MIN_SETUP_SAMPLE = 5` trades and negative PnL. Below that a losing
  bucket is noise, and flagging it would invite acting on nothing.

`report_arena.py attribution` needed no change — it serialises the whole summary,
so `by_setup` and the new cards appear automatically.

## Verification

```text
Focused (setup_profile + SetupMetadataTests + test_arena_attribution): 24 passed
Full test suite: 491 passed (was 474)
Line coverage: 81% (required minimum: 80%), coverage report exit 0
compileall: passed
git diff --check: passed
secret hygiene test: passed
```

## Deliberately out of scope

- **No entry gate.** Nothing blocks a candidate yet. With 82 closed trades and no
  setup profile on any of them, every bucket is empty; gating now would act on
  noise. The gate is the next increment, once buckets carry real samples.
- **`trade_memory` still buckets by exit reason.** Repointing it at setup keys is
  a separate change and pointless until the keys have data behind them.
- Existing closed trades cannot be backfilled: `ema_21` and `atr_14` at entry
  time were never stored.

## Deployment gate

Safe to deploy at any time — recording-only, no behaviour change. But per
`CLAUDE.md`, flatten any open position **before** restarting: BingX rejects
`set_margin_mode` while a position is open and the fail-closed verification then
crash-loops.

Read the buckets with:

```bash
PYTHONPATH=src python src/report_arena.py attribution --limit 500 --diagnose
```
