# Telemetry Cancellation — TDD Evidence

## User journey

On 2026-10-16, with Python 3.11 locally, `tests/test_telemetry_poller.py::RunLoopTests::test_run_keeps_polling_until_cancelled` hung indefinitely at ~30% CPU, and an earlier full-suite run stalled in the same poller tests. `MarketDataPoller._guarded` awaited `asyncio.wait_for(action(), call_timeout)`. Before Python 3.12, when a `task.cancel()` lands in the same loop tick as the wrapped call finishing, `wait_for` sees the inner future done and **returns its result instead of raising `CancelledError`**. The `except asyncio.CancelledError: raise` in `_guarded` is never reached, so `run()`'s `while True` keeps polling after it was told to stop.

CLAUDE.md ("Live dashboard feed") already records this bug for the websocket feed and says to use `ws.with_timeout`, never bare `asyncio.wait_for`, in telemetry code. A grep of `src/bot/telemetry` found two more bare calls in `server.py`:

- `_read_request`: `wait_for(reader.readuntil(...), request_timeout)`. A swallowed cancel lets one request finish serving after a stop.
- The SSE loop: `wait_for(queue.get(), ping_seconds)` inside `while not reader.at_eof()`. A message published in the same tick as `TelemetryServer.stop()` swallows the stop's cancel, the stream loops back to `queue.get()`, and `stop()`'s `gather` over the handler tasks never returns, so bot shutdown hangs.

Live trading impact: none. Telemetry only reports; no engine decision reads it. The poller and server run only with `TELEMETRY_ENABLED=true`.

## RED evidence

Three tests were written first and run against the unfixed code (commit `40569bd`, on top of `9e86d36`), modelled on `tests/test_telemetry_ws.py::test_cancelling_the_waiter_is_never_swallowed_even_if_the_work_finishes_in_the_same_tick`. Each one parks the wait on an event, completes it, and cancels in the same tick. Each fails within about 1 s instead of hanging.

```text
FAIL: test_cancelling_run_is_never_swallowed_even_if_the_call_finishes_in_the_same_tick (tests.test_telemetry_poller.RunLoopTests)
AssertionError: False is not true : run() swallowed the cancellation and kept polling
Ran 1 test in 1.031s
FAILED (failures=1)
```

```text
FAIL: test_cancelling_a_request_read_is_never_swallowed_even_if_the_request_arrives_in_the_same_tick (tests.test_telemetry_server.CancellationTests)
AssertionError: CancelledError not raised
FAIL: test_stopping_the_server_is_never_lost_to_a_message_arriving_in_the_same_tick (tests.test_telemetry_server.CancellationTests)
AssertionError: False is not true : stop() hung: the stream swallowed its cancellation
Ran 2 tests in 1.068s
FAILED (failures=2)
```

Determinism: poller RED failed 20/20 runs, server RED 10/10.

The first draft of the poller test used `interval_seconds=0.01` and hung during a repeat run: its own cleanup `task.cancel()` was swallowed by the same bug. The final test uses `interval_seconds=60`, so a poller that swallowed the first cancel parks in `asyncio.sleep()` and the cleanup cancel always lands. The server test cleans up by cancelling the `stop()` task, whose `gather` re-cancels a stream that swallowed the first cancel.

## GREEN implementation

- `poller.py` `_guarded`: `asyncio.wait_for(...)` → `ws.with_timeout(...)`. The `except asyncio.CancelledError: raise` and the failure counting are unchanged.
- `server.py` `_read_request` and the SSE loop: same swap. `with_timeout` still raises `asyncio.TimeoutError` on timeout (caught by `_on_connection` and the ping branch), and `task.result()` re-raises `LimitOverrunError`, so the 431 path is unchanged.
- `grep -rn wait_for src/bot/telemetry` now matches only the `with_timeout` docstring in `ws.py`.

`with_timeout` waits with `asyncio.wait({task}, timeout=...)`, so a cancel of the caller always raises at that await, and its `finally` cancels whatever is left unfinished.

## Verification

```text
RED tests after the fix: 0/20 poller, 0/10 server+poller cancellation repeats failed
Telemetry tests (poller + server + ws): 61 passed
Full test suite: 1547 passed, run 3 times back to back (41-50 s each, no hang)
Line coverage: 89% (required minimum: 80%); poller.py 100%, server.py 99%, ws.py 91%
compileall: passed
git diff --check: passed
pip check: not available (the local Python 3.11.15 venv has no pip module); `uv pip check` against the same interpreter: all 118 packages compatible
secret hygiene test: passed
```

## Commit trail

- `40569bd` — `test: pin telemetry cancellation against swallowed wait_for`
- `d3da0c5` — `fix: telemetry waits never swallow a cancellation`

## Deployment gate

No env change. The VPS runs this code only with `TELEMETRY_ENABLED=true` (the dashboard feed is live there, `TELEMETRY_PORT=3917`), and picking it up needs a pm2 restart of `ai-perp-volume`. That restart is allowed **only while the bot is flat** (CLAUDE.md "Never restart with an open position": `set_margin_mode` fails with `104103` and pm2 crash-loops). Deploy only with the operator's approval. After restart, confirm `curl -s http://127.0.0.1:3917/` answers and the chart keeps updating. No trading behaviour changes.
