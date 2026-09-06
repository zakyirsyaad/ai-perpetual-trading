# AI Exit Call Blocks Stop-Loss Detection — TDD Evidence

## User journey

Make `MAX_STOP_LOSS_PCT` actually bound the loss. The cap was correct and live,
but the price was not being watched while it mattered.

## Evidence that motivated the change

12 of 19 stop losses after the cap was deployed overshot it materially:

```
ETH  0.824  0.786  0.757  0.736  0.718  0.691  0.638  0.623  0.619  0.542  0.524
BTC  0.571
                                                   cap = 0.400%
```

Overshoot ranged from +31% to +106%. Actual loss on those twelve: −1,540.72
against −1,080 had the cap held — **−581 of avoidable loss**.

Two hypotheses were tested and rejected before the real one was found:

1. *Config not applied* — rejected. `Settings()` on the VPS reports
   `max_stop_loss_pct = 0.4`.
2. *Volatility slippage* — rejected. Overshoot does not track ATR:
   ETH at `atr_low` (ATR 0.148%) overshot +89%, while BTC at `atr_low`
   (ATR 0.131%) came in at +3%. Same regime, opposite outcome.

The cause is structural. `exit_signal` is a network round trip to the model
provider and was awaited **inside** the price-monitoring loop, so `_hit_stop_loss`
was not evaluated for its entire duration. Measured from `ai_call_metrics` over
732 exit calls:

| | latency | blind window (latency + 5s sleep) |
|---|---:|---:|
| median | 3.9s | 8.9s |
| p90 | 17.9s | 22.9s |
| p99 | 78.9s | 83.9s |
| max | 316.9s | **5m 22s** |

ETH suffered far more than BTC because it is 2.3× more volatile
(median ATR 0.299% vs 0.130%), so the same blind window travels further.

A second defect sat in the same place: `gross` and `net_after_fee`, which gate
`AI_EXIT_ADVERSE_CUT_USDT`, were computed **before** the call, so a decision was
executed against a price that could be minutes stale.

## RED evidence

`tests.test_trade_engine_execution.ExitCallDoesNotBlockStopLossTests` — 3 failed:

```text
FAIL: test_price_is_still_polled_while_the_model_is_thinking
AssertionError: 1 not greater than 1 : no price was fetched while exit_signal
was in flight: the stop loss was unwatched

FAIL: test_a_breach_during_the_call_is_caught_without_waiting_for_the_model
AssertionError: the monitor waited for the model before acting on the stop

ERROR: test_a_failing_exit_call_does_not_abandon_the_position
RuntimeError: provider exploded
```

## GREEN implementation

- `_monitor_position` now dispatches `exit_signal` with `asyncio.create_task` and
  consumes the result on a later iteration once it is done. Price polling
  continues at `POSITION_MONITOR_INTERVAL_SECONDS` throughout, so the blind
  window shrinks from a full model round trip to a single price fetch.
- Because the result is consumed in a later iteration, `gross`/`fee` used to judge
  it are that iteration's — fresh, not the price the model saw.
- A pending task is cancelled in `finally`, so returning early (a stop firing
  mid-call) leaks nothing.
- `_collect_exit_signal` swallows provider failures. Previously an exception
  unwound `_monitor_position` through `_tick` to `run_forever`, **leaving the open
  position unmanaged** — far worse than missing one model opinion.

The loss cap itself is unchanged at 0.40%. Nothing here tightens a stop or caps
an upside: across both venues, `stop_loss` (108 trades) and `time_stop` (25) have
a **0.0% win rate** — letting a losing trade run has never once recovered.

### Test cost

The first draft ran the monitor with `position_monitor_interval_seconds=0`, which
hot-spins for the whole simulated model call. Harmless bare (5.9s suite) but
+57s under coverage tracing, which is what CI runs. The tests now use a real 0.01s
interval: suite under coverage went 62.7s → 7.6s.

## Verification

```text
Focused (ExitCallDoesNotBlockStopLossTests): 3 passed in 0.184s
Full test suite: 503 passed (was 500)
Line coverage: 81% (required minimum: 80%), coverage report exit 0
compileall: passed
git diff --check: passed
secret hygiene test: passed
```

## Not claimed

The blind window is a real defect and explains the ETH-vs-BTC asymmetry, but it
is not proven to account for all 12 breaches: at ~5.3 model calls per trade the
loop is blocked for roughly 6–29% of a hold, while 63% of stops leaked. A second
contributor may remain. This fix stands on its own regardless — a network call to
a model must not sit in the stop-loss detection path.

## Deployment gate

Flatten any open position before restarting: BingX rejects `set_margin_mode`
while a position is open and the fail-closed verification then crash-loops.
