# AI Exit Minimum Hold — TDD Evidence

## User journey

Stop the model from cutting a brand-new position on noise. For the first minutes of a trade
only the deterministic protection (stop loss, take profit, neutral exit floor, time stop) acts;
the AI exit evaluator is consulted once the position has had time to move past its own fees.

## Evidence (live, cycle started 2026-09-14 11:10 WIB)

Each of the 32 AI exits was compared with the same trade exited by the deterministic rules
alone, simulated on 1m candles with the live geometry:

| AI exit taken | n | AI (real) | Deterministic only | AI minus deterministic | AI better |
|---|---|---|---|---|---|
| within 3 minutes | 8 | −362.29 | −263.53 | **−98.76** | 4/8 |
| after 3 minutes | 24 | −623.49 | −906.38 | **+282.89** | 15/24 |

Three of the eight quick cuts (#576, #577, #590) would have reached the +40 floor; four were
spared a −90 stop. Mechanism: the first exit call fires seconds after entry — the policy sees
any candle as "new" at trade start — when net is already about −18 from round-trip fees. The
model reports the position as underwater, and `AI_EXIT_ADVERSE_CUT_USDT=25` honours the close
after a 0.04% move.

Sample is small (8 quick exits), so the feature ships off; the later-exit result (24 trades,
AI better in 15) is why the model is delayed rather than removed.

## RED evidence

```
FAIL: test_the_model_is_not_asked_before_the_minimum_hold
AssertionError: True is not false          <- the model was asked and closed at 60 s
ERROR: test_defaults_to_off_and_reads_the_env
AttributeError: 'Settings' object has no attribute 'ai_exit_min_hold_seconds'
FAIL: test_negative_values_are_rejected
AssertionError: Exception not raised
Ran 5 tests — FAILED (failures=2, errors=1)
```

`test_the_model_is_asked_once_the_minimum_hold_has_passed` and
`test_zero_keeps_the_previous_behaviour` are characterisation guards that pass before and after.

## GREEN implementation

- `config.py` — `AI_EXIT_MIN_HOLD_SECONDS` (int, default 0 = off, 0..3600).
- `ai/orchestration.py:exit_signal` — after `_ensure_exit_trade_started` (so per-trade budget
  state still resets on a new position), a position held for less than the minimum returns the
  deterministic fallback with `ai_called=False`: no provider call, no market context build, no
  budget spent, nothing recorded in `ai_exit_verdicts`. The first model call happens at the hold
  boundary, because the policy then sees its first candle as new.
- The monitor's stop loss, take profit, neutral exit floor and time stop are untouched.

## Verification

```
PYTHONPATH=src python -m compileall -q src tests          # OK
PYTHONPATH=src python -m coverage run -m unittest discover -s tests
Ran 567 tests in 5.019s
OK
PYTHONPATH=src python -m coverage report                  # TOTAL 6741 1259 81%
git diff --check                                          # OK
```

## Deployment gate

1. Merging changes nothing: the default is 0.
2. Set `AI_EXIT_MIN_HOLD_SECONDS=180` in the VPS `.env`.
3. Restart only with the venue flat and between ticks (stop → verify flat → start).
4. Re-measure quick versus later AI exits once each group has at least 60 trades.
