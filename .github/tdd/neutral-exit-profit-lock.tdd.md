# Neutral Exit Profit Lock — TDD Evidence

## User journey

A trade that is clearly in profit must not give it all back just because the price moved
faster than the monitor polls. The neutral exit should lock a profit once it exists, not only
while a 5-second poll happens to land inside a narrow window.

## Incident that motivated the change

Trade #574, BTC/USDT LONG @ 77,683.6, opened 2026-09-14 18:52 WIB, 0.2317 BTC. Net after both
taker fees, reconstructed from 1m candles:

| Minute | What happened |
|---|---|
| 0–21 | net crawled from −25 to +11 |
| 20 | the 25th and last AI exit call of the trade's budget |
| 23 | +10 → **+85 within one minute**, jumping the 40–60 neutral band |
| 23–42 | held **+78 to +122**, entirely above the band |
| 43 | +122 → +0 within one minute, jumping the band again |
| 60 | time stop, net **−0.84** |

The legacy check is `min_net <= net_after_fee <= min_net + band`, evaluated on each poll. A
move that skips the 20 USDT window in either direction is never taken, and nothing above the
window is ever locked. With the AI exit budget already spent, the only exits left were the
stop loss, a distant take profit, and the time stop.

Two more observations from the same pull: across 329 exit verdicts in the cycle, **zero** were
HOLD while net ≥ +20, so the model never refused a profitable close — it was simply not asked.
And every exit replay so far modelled this exit as a *floor* (exit the first time net reaches
the minimum), so live behaviour has been quietly worse than the measured replays whenever the
price jumped the window.

## RED evidence

`tests.test_trade_engine_execution.NeutralExitProfitLockTests`, replaying the #574 shape
`[+10, +85, +100, +122, +95]` then a stop-loss price:

```
FAIL: test_floor_mode_takes_a_profit_that_jumped_over_the_window
AssertionError: 'stop_loss' != 'neutral_exit'
FAIL: test_trailing_mode_rides_a_runner_and_exits_on_the_pullback
AssertionError: 'stop_loss' != 'neutral_exit'
FAIL: test_trailing_mode_never_lets_an_armed_profit_fall_below_the_floor
AssertionError: 44.999999999999076 != 39.0 within 0.5 delta
Ran 4 tests — FAILED (failures=3)
```

`test_window_mode_keeps_the_legacy_behaviour_by_default` passed before and after: it is a
characterisation guard that an unchanged `.env` keeps today's exits.

The mode validator was first written alongside the implementation, then removed so its tests
could fail before it existed:

```
FAIL: test_an_unknown_mode_is_rejected
AssertionError: Exception not raised
FAIL: test_modes_are_accepted_case_insensitively
AssertionError: 'Trailing' != 'trailing'
```

## GREEN implementation

- `config.py` — `VOLUME_NEUTRAL_EXIT_MODE` (`window` default | `floor` | `trailing`, validated,
  case-insensitive; a typo refuses to boot rather than silently meaning `window`) and
  `VOLUME_NEUTRAL_EXIT_TRAIL_USDT` (default 0).
- `core/trade_engine.py:_monitor_position`, same position in the check order and same
  `neutral_exit` reason, so order routing and analytics are unchanged:
  - `window` — the legacy band check, untouched.
  - `floor` — exit the first time net ≥ `VOLUME_NEUTRAL_EXIT_MIN_NET_USDT`, however far past
    it the price jumped. This is exactly what the exit replays have always modelled.
  - `trailing` — arm at the floor, track the peak net, exit once net ≤
    `max(min_net, peak − trail)`. With a trail of 0 it behaves like `floor`.

Trade-off, stated plainly: `trailing` can give back profit that `floor` would have kept. A
trade that touches +45 and then gaps straight down is locked at +45 by `floor`, while
`trailing` exits on the first poll at or below +40, which in a gap can be far lower. Its upside
is runners like #574, where `floor` locks roughly +85 and a 20 USDT trail roughly +100.

## Verification

```
PYTHONPATH=src python -m compileall -q src tests          # OK
PYTHONPATH=src python -m coverage run -m unittest discover -s tests
Ran 551 tests in 5.127s
OK
PYTHONPATH=src python -m coverage report                  # TOTAL 6690 1258 81%
git diff --check                                          # OK
```

## Deployment gate

1. Merging changes nothing: the default mode is `window`.
2. Choose a mode in the VPS `.env`: `VOLUME_NEUTRAL_EXIT_MODE=floor`, or `trailing` together
   with `VOLUME_NEUTRAL_EXIT_TRAIL_USDT` (for example 20).
3. Restart only with the venue flat and between ticks — use the stop, verify flat, start
   sequence.
4. Not addressed here: the AI exit budget is still spent within the first 11–21 minutes, and
   BingX shows unrealised PnL before fees while the exit logic uses net after both fees.
