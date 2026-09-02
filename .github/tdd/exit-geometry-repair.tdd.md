# Exit Geometry Repair — TDD Evidence

## User journey

Reduce the net loss of the BingX VST volume cycle without changing the volume
objective, by removing the exit asymmetry that lets losses run to their maximum
while winners are clipped early.

## Evidence that motivated the change

Closed trades in Supabase, OKX cycle vs BingX cycle:

| | OKX (14 Jun – 20 Jul) | BingX (31 Aug – 1 Sep) |
|---|---:|---:|
| Trades | 281 | 27 |
| Net PnL | −4,889.47 | −1,182.02 |
| Volume | 10,035,085 | 971,772 |
| Fee at 0.05% | −5,017.54 | −485.89 |
| **Gross PnL** | **+128.07** | **−696.13** |
| Gross / volume | +0.0013% | −0.0716% |
| Win rate | 37.7% | 22.2% |

Exit-reason mix (the regression):

| Reason | OKX n (%) · avg | BingX n (%) · avg |
|---|---|---|
| `stop_loss` | 89 (31.7%) · −67.46 | 8 (29.6%) · **−130.40** |
| `liquidation_guard` | 56 (19.9%) · −18.21 | 0 (removed from code) |
| `strategy_exit` | 63 (22.4%) · +7.63 · WR 71.4% | 1 (3.7%) · −5.09 |
| `neutral_exit` | 57 (20.3%) · +34.14 · WR 94.7% | 6 (22.2%) · +34.73 |

Two mechanisms were identified:

1. No code path produces `liquidation_guard` any more, so the cheap early exit
   that bounded 20% of OKX losses at −18.21 is gone. Those trades now run to the
   unbounded ATR stop, which doubled the average stop-loss cost.
2. `_monitor_position` honours an AI `close_now` only when `gross >= fee`, so a
   losing position can only be released by the full stop loss or the time stop.

## RED evidence

`tests.test_ai_orchestration.StopLossCeilingTests` — 2 of 5 failed:

```text
FAIL: test_atr_scaled_stop_is_capped
AssertionError: 1.2000000000000002 != 0.35 within 6 places

FAIL: test_capping_improves_fee_adjusted_rr
AssertionError: 1.7692307692307692 not greater than 1.7692307692307692
```

`tests.test_trade_engine_execution.AIExitAdverseCutTests` — 1 of 4 failed:

```text
FAIL: test_ai_may_cut_a_losing_position_once_past_the_threshold
AssertionError: 'stop_loss' != 'strategy_exit:ai_close'
```

The other six tests passed in RED by design: they pin the existing behaviour so
the new settings cannot silently change a runtime that leaves them unset.

## GREEN implementation

- Added `MAX_STOP_LOSS_PCT` (default `0`, disabled). Clamps the ATR-scaled stop
  in `AIOrchestrator._resolve_trade_targets`, applied *before* the take-profit
  floor is derived from `sl_pct` so a capped stop also tightens the reward
  requirement and raises the fee-adjusted RR.
- Added `AI_EXIT_ADVERSE_CUT_USDT` (default `0`, disabled). In
  `TradeEngine._monitor_position` an AI `close_now` is now honoured when
  `net_after_fee <= -cut`, restoring the bounded-loss role the removed
  `liquidation_guard` used to serve. The middle zone (a loss smaller than the
  threshold) still holds, so the change does not add fee churn.
- Both defaults are off, so an unchanged `.env` keeps the current behaviour and
  activation is an explicit operator decision.

The stop ceiling deliberately does **not** bypass
`_stop_is_strictly_before_liquidation`; a ceiling at or beyond the liquidation
limit still rejects the entry, covered by
`test_ceiling_does_not_bypass_the_liquidation_guard`.

## Verification

```text
Focused tests (StopLossCeilingTests + AIExitAdverseCutTests): 9 passed
Full test suite: 466 passed (was 457)
Line coverage: 80% (required minimum: 80%), coverage report exit 0
compileall: passed
git diff --check: passed
secret hygiene test: passed
```

## Deployment gate

Not yet deployed and not yet committed. Before activating on the VPS:

1. Resolve the orphaned BingX position that is currently blocking every entry
   (128 consecutive `Skipped entry because an exchange position is already open`).
2. Restart the TradingView relay — the live log shows `All connection attempts
   failed`, which pushes the AI toward HOLD on every tick.
3. Set `MAX_STOP_LOSS_PCT` and `AI_EXIT_ADVERSE_CUT_USDT` to non-zero values on
   the VPS; both are inert until then.
4. Measure with **gross PnL per unit volume**, not net. Fees are fixed at 0.05%
   per side by the taker-only decision, so net will stay negative regardless and
   would mask whether the change worked. Baseline to beat: −0.0716% gross/volume.
