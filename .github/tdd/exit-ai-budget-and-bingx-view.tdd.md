# AI Exit Budget Reserve & BingX View — TDD Evidence

## User journey

Two gaps left open by the neutral exit profit lock:

1. **The model goes blind halfway through a trade.** Routine triggers spend the whole AI exit
   budget in the first minutes, and a favourable move never asks the model at all.
2. **The BingX screen and the bot disagree about profit.** BingX shows unrealised PnL before
   fees; the exit logic judged net after both taker fees, so a position that looked profitable
   on screen was, correctly but confusingly, left open.

## Evidence

- Trade #574: 25/25 AI exit calls used by **minute 20**. At minute 23 net jumped +10 → +85 and
  held +78..+122 for twenty minutes; the model was never asked again.
- Trade #587: budget exhausted by **minute 11**; net reached +32 (screen ≈ +50) at minute 18,
  then closed on the time stop at −21.05.
- Live uses `AI_KLINE_INTERVAL=3m`, so candles alone are 20 calls an hour; the budget is burned
  mostly by regime/flow-change probes, which may fire every `AI_EXIT_CHECK_INTERVAL_SECONDS`.
- Across 329 exit verdicts in the cycle, zero were HOLD while net ≥ +20: the model never
  refused a profitable close.

## RED evidence

`tests.test_ai_exit_policy.ExitBudgetCoversTheWholeHoldTests`:

```
FAIL: test_a_favourable_move_triggers_a_call_when_enabled
AssertionError: False is not true
FAIL: test_reserved_calls_are_not_spent_on_flow_changes
AssertionError: True is not false
FAIL: test_reserved_calls_are_not_spent_on_new_candles
AssertionError: True is not false
FAIL: test_reserved_calls_stay_available_for_price_moves
AssertionError: 'new_candle' != 'favorable_move'
Ran 5 tests — FAILED (failures=4)
```

(`test_the_hard_budget_still_caps_event_calls` is a characterisation guard: the reserve lives
inside the budget, never on top of it.)

`tests.test_ai_exit_orchestration.ReservedBudgetSkipsContextBuildTests`:

```
FAIL: test_no_context_is_built_when_only_reserved_calls_remain
AssertionError: 1 != 0
```

`tests.test_trade_engine_execution.BingXViewTests` and `NeutralExitBasisSettingTests`:

```
ERROR: test_basis_defaults_to_net_and_accepts_gross
AttributeError: 'Settings' object has no attribute 'volume_neutral_exit_basis'
FAIL: test_gross_basis_applies_the_floor_to_the_number_bingx_shows
AssertionError: 'stop_loss' != 'neutral_exit'
FAIL: test_status_line_shows_the_bingx_view_next_to_net
AssertionError: no logs of level INFO or higher triggered on test.engine
FAIL: test_an_unknown_basis_is_rejected
AssertionError: Exception not raised
```

(`test_net_basis_stays_the_default` is a characterisation guard for an unchanged `.env`.)

## GREEN implementation

**AI exit budget** (`ai/exit_policy.py`, `config.py`, `ai/orchestration.py`):
- `AI_EXIT_EVENT_RESERVE_CALLS` (default 0): the last N calls of each trade's budget can only be
  spent on price moves. Once `calls_made >= max_calls − reserve`, `new_candle` and
  `regime_or_flow_change` stop earning calls and the gate reports `routine_budget_reserved`.
  The reserve is inside `AI_EXIT_MAX_CALLS_PER_TRADE`, so the token ceiling per trade is unchanged.
- `AI_EXIT_FAVORABLE_MOVE_THRESHOLD_PCT` (default 0): a move in the position's favour since the
  last call, mirroring the existing adverse-move trigger, now asks the model (`favorable_move`).
- `routine_budget_reserved` short-circuits `exit_signal` before any market context is built, since
  a flow probe could no longer earn a call.

**BingX view** (`core/trade_engine.py`, `config.py`):
- `VOLUME_NEUTRAL_EXIT_BASIS` (`net` default | `gross`, validated): with `gross`, the neutral exit
  thresholds (all modes) apply to unrealised PnL before fees — the number on the BingX screen.
- `POSITION_STATUS_LOG_SECONDS` (default 60, 0 = off): while a position is open, one line per
  interval shows the BingX view, the round-trip fee, net, and the neutral exit threshold in both
  terms, for example
  `Position BTC/USDT LONG | BingX unrealised (before fees) +50.21 | round-trip fee 18.00 | net +32.21 | neutral exit at net +40.00 = BingX +58.00`.
  BingX computes its figure from the mark price and the bot from the last price, so the two can
  differ by a few cents on BTC.

## Verification

```
PYTHONPATH=src python -m compileall -q src tests          # OK
PYTHONPATH=src python -m coverage run -m unittest discover -s tests
Ran 562 tests in 5.183s
OK
PYTHONPATH=src python -m coverage report                  # TOTAL 6736 1259 81%
git diff --check                                          # OK
```

## Deployment gate

1. Merging changes no trading decision: every new setting defaults to the old behaviour; only the
   status line (observability) is on by default.
2. Suggested live values, set together with `VOLUME_NEUTRAL_EXIT_MODE=floor`:
   `AI_EXIT_EVENT_RESERVE_CALLS=8` (of 25) and `AI_EXIT_FAVORABLE_MOVE_THRESHOLD_PCT=0.10`
   (about +18 gross on 18,000 notional). Choose `VOLUME_NEUTRAL_EXIT_BASIS` deliberately: with
   `gross` a +40 floor closes at net ≈ +22.
3. Restart only with the venue flat and between ticks (stop → verify flat → start).
