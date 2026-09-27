# A refused exit keeps the position monitored

## User journey

From 2026-09-25, BingX answered **every** close with `109403 risk forbidden` while accepting every open. Three trades, three refusals:

| Opened (WIB) | Outcome |
|---|---|
| 25/09 01:17 | refused → operator closed it by hand |
| 25/09 12:31 | refused → **liquidated 16:05**, ≈ −252 |
| 27/09 14:26 | refused → **liquidated 17:08**, `BUY SHORT LIMIT 0.2959 @ 85,276.8 profit −237.87` |

The refusal itself was not the damage. One `ExchangeAPIError` escaped `_manage_open_position`, `_manage_open_position_guarded` saw the venue position still open and latched entries — **and the monitor loop that owns the stop died with it**. The persisted stop was 0.35% (≈ 84,770 on the last entry); liquidation was 0.95% away. So a −90 stop became a −250 liquidation, twice, and neither close was recorded in `trades` (cycle 2's net understates by ≈ −502).

The latch itself is right for what it was built for: an exit order that *may* exist must never be re-sent, because a duplicate leveraged position is unrecoverable. But a business `code` with `retryable=False` is the venue saying "no" — **nothing was placed**, so asking again later cannot duplicate anything, and abandoning the position is strictly worse than holding it and retrying.

## RED

```
$ PYTHONPATH=src python -m unittest tests.test_trade_engine_execution.ExitRejectionKeepsThePositionManagedTests
FAIL: test_a_refused_exit_is_retried_and_never_latches_entries
  File "tests/test_trade_engine_execution.py", line 1888, in test_a_refused_exit_is_retried_and_never_latches_entries
    self.assertEqual(calls["n"], 2, "the refused close must be retried on the next monitor pass")
AssertionError: 1 != 2 : the refused close must be retried on the next monitor pass
Ran 3 tests — FAILED (failures=1)
```

The other two tests in the class passed from the start by design: they pin the behaviour that must **not** change (an uncertain submission, and a local validation failure with no venue code, both still latch).

## GREEN

- `TradeEngine._venue_refused_the_order(exc)` — true only when the error is not an `UncertainOrderSubmissionError`, is not retryable, and carries a venue `code`. A local validation failure (`code is None`) and an uncertain submission both fall through to the old latch.
- The taker exit is wrapped: on a refusal the engine logs at ERROR with the venue code and the running refusal count, waits `EXIT_REJECTION_RETRY_SECONDS`, sets `exit_reason = None` and `continue`s — the existing idiom for "resume the monitor" already used by the maker-only exit path. The counter resets on the first accepted close.
- `EXIT_REJECTION_RETRY_SECONDS` (new, default 15.0, `ge=0`) spaces the retries; the 5 s monitor keeps polling price throughout, so the stop, floor and time stop all stay alive.

What this does **not** do: it cannot make a refusing venue accept the close. If BingX keeps refusing, the position now stays watched and is offered again every 15 s instead of drifting unattended — but the loss is still unbounded until a venue-side protective stop exists. That is the follow-up, and it is deliberately not in this PR: BingX's stop-order parameters (`type`, trigger-price field, close-position flag) could not be verified — the docs site is a JS app WebFetch cannot read and the repo carries no exchange reference for them — and guessing an order shape that the venue might accept with the wrong semantics is how a stale stop opens a fresh position.

## Verification

| Check | Result |
|---|---|
| `python -m compileall -q src tests` | clean |
| `python -m unittest discover -s tests` | **576 tests, OK** |
| `python -m coverage report` | **81%** (gate 80%) |
| `git diff --check` | clean |
| `pip check` | not run — no `pip` module on this uv-managed interpreter; no dependency changed |
| `tests.test_secret_hygiene` | OK |

New tests: `ExitRejectionKeepsThePositionManagedTests` (3), `ExitRejectionRetrySettingTests` (2).

## Deployment gate

`git pull` plus a restart, and the restart must verify the venue position is flat first (BingX rejects `set_margin_mode` while a position is open and pm2 turns that into a crash loop). The bot is currently latched and flat, so the restart also clears the stale latch. Then **watch the first trade**: if the log shows `Venue refused the ... exit` followed by an accepted close, the refusal is transient and this change alone restores normal operation; if refusals continue, the position stays monitored but a venue-side stop is required before the bot should be left running unattended.
