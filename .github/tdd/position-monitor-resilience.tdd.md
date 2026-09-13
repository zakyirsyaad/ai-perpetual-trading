# Position Monitor Resilience — TDD Evidence

## User journey

A live position must never be left without its monitor. Whatever fails — a venue call, a
network hiccup, an unexpected bug — the engine either keeps watching the position or
refuses to open anything new until an operator has looked at it. Silently returning to the
entry loop while exposure may still exist is never acceptable.

## Incident that motivated the change

2026-09-12, BTC/USDT LONG 0.232 @ 77,580 opened 14:51 UTC. At 15:03:03 UTC, twelve minutes
in, one price read failed:

```
ERROR | volume_bot | Unhandled error in trading loop.
  File "src/bot/core/trade_engine.py", line 821, in _monitor_position
bot.exchange.contracts.ExchangeAPIError: BingX request failed for /openApi/swap/v2/quote/price:
  ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed
```

The exception escaped `_monitor_position`, `_manage_open_position` and
`_execute_single_trade`, reached the catch-all in `run_forever`, and the loop went on to the
next tick. From then on nothing evaluated the stop loss, the time stop or the model's exit
verdicts; the only guard still working was the duplicate-entry check. The position sat
unmonitored for hours (uPnL −48.10 at last check) until it was closed by hand.

The resume path had the same defect in a different shape: `_resume_persisted_positions`
caught every exception from `_manage_open_position`, logged it, and moved on.

## RED evidence

`MonitorSurvivesVenueErrorsTests` — the exact failure from the incident escapes the monitor:

```
ERROR: test_a_failed_price_read_does_not_abandon_the_position
bot.exchange.contracts.ExchangeAPIError: BingX request failed for /openApi/swap/v2/quote/price:
  ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed
ERROR: test_repeated_price_failures_are_escalated_to_error
bot.exchange.contracts.ExchangeAPIError: ... CERTIFICATE_VERIFY_FAILED ...
ERROR: test_unexpected_monitor_failure_keeps_managing_the_position
RuntimeError: unexpected failure inside the monitor loop
Ran 3 tests — FAILED (errors=3)
```

`PositionManagementFailureLatchTests` — close-phase failures abandon the position:

```
ERROR: test_a_failed_exit_with_the_position_still_open_blocks_new_entries
AttributeError: 'TradeEngine' object has no attribute '_manage_open_position_guarded'
ERROR: test_a_failure_after_the_venue_is_flat_does_not_block_entries      (same)
ERROR: test_an_unreadable_venue_after_a_failure_blocks_new_entries        (same)
ERROR: test_a_fresh_entry_whose_management_fails_blocks_new_entries
RuntimeError: exit order rejected          <- escaped _execute_single_trade, the unguarded call site
FAIL: test_a_resumed_position_whose_management_fails_blocks_new_entries
AssertionError: unexpectedly None          <- resume path logged the error and moved on
Ran 5 tests — FAILED (failures=1, errors=4)
```

## GREEN implementation

Three layers, split by whether an order may already exist:

1. **Price reads inside `_monitor_position`** catch `ExchangeAPIError`, wait one monitor
   interval and try again. A failed read costs one iteration, never the monitor. The third
   consecutive failure escalates from WARNING to ERROR.
2. **Unexpected exceptions from `_monitor_position`** are caught in `_manage_open_position`,
   logged with traceback, and monitoring resumes with a capped linear backoff (interval × n,
   at most 60 s). No order has been sent at that point, so monitoring again is safe.
3. **Failures that still escape** happened in the close phase, after an exit order may
   already exist. Retrying automatically could duplicate an order, which the idempotency
   rules forbid, so `_manage_open_position_guarded` asks the venue instead:
   - flat → nothing exposed (for example a Supabase 504 after a clean close); log that the
     trade record may be missing and keep trading;
   - open or unreadable → set `_entries_blocked_reason`. Gate 1 of every later tick then
     refuses entries and logs `Entries blocked: ...` until an operator resolves it.

Both call sites — `_execute_single_trade` (previously unguarded) and
`_resume_persisted_positions` (previously log-and-drop) — go through the guarded wrapper.

The reconciliation supervisor restarts only on its `Entry order ... could not be confirmed`
pattern, so this new latch is always treated as `hold`: nothing restarts while exposure may
exist, which the `104103` crash loop makes mandatory anyway.

## Verification

```
PYTHONPATH=src python -m compileall -q src tests          # OK
PYTHONPATH=src python -m coverage run -m unittest discover -s tests
Ran 545 tests in 5.427s
OK
PYTHONPATH=src python -m coverage report                  # TOTAL 6669 1261 81%
git diff --check                                          # OK
```

## Deployment gate

1. Deploy only while the venue is flat **and** no monitor is running: a restart mid-trade
   loses that trade's record even when the venue is already flat.
2. After restart, confirm the engine started and entries are not latched:
   `grep -E "trade engine started|Entries blocked" ~/.pm2/logs/ai-perp-volume-error.log | tail`.
3. If `Entries blocked: position management for ... failed` ever appears, check the venue
   position first. Close or reconcile it by hand, then restart once the venue is flat.
