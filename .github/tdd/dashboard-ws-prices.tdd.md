# Dashboard Websocket Prices — TDD Evidence

## User journey

The dashboard chart moved once every 5 seconds, because the poller read the order book on that cadence. For a screen shown to a second person that looks patchy. BingX publishes a public market websocket for the VST environment, so the chart can follow the best bid/ask about every 0.4 seconds.

Feed is opt-in: `TELEMETRY_PRICE_STREAM=false` by default. With it off nothing changes, and with it on the polling feed keeps running underneath as the fallback.

## What was checked before building

The BingX docs list `wss://vst-open-api-ws.bingx.com/swap-market` as the simulated-market host (read from the official docs bundle, not a third-party summary). A 20 s read-only probe from the VPS, stdlib only, no keys:

- handshake `101` in 93 ms; subscription to `BTC-USDT@bookTicker` acknowledged with `code: 0`
- 52 data messages in 20 s, a steady ~400 ms apart (max 419 ms), not the 200 ms a third-party page claimed
- every frame gzip-compressed; the server sent a text `Ping` about every 5 s and the probe's `Pong` kept the link up

## Design decisions

- **No new dependency.** `telemetry/ws.py` is a ~200 line RFC 6455 client on asyncio streams. This process holds trading keys, and the server side of the feed is already stdlib-only for the same reason.
- **Read-only by construction.** The feed sends one thing, `{"reqType":"sub","dataType":"<PAIR>@bookTicker"}`, and a `Pong`. A test asserts nothing else is ever sent and no `listenKey` appears. The host is a module constant, not a setting, so it can only be the VST one.
- **Polling is the fallback, not a casualty.** `on_stream_tick` marks a symbol fresh for `stream_fresh_seconds` (3 s). `_poll_price` skips that symbol only while fresh. If the stream dies the chart degrades to the previous 5 s behaviour by itself, without any supervisor deciding anything.
- **The engine never reads it.** Nothing outside `telemetry/` imports the hub or the poller (checked by search). Trading prices still come from `FuturesExchangeProtocol`.
- **Contained like the rest of the feed.** A callback that raises is logged and skipped; the feed reconnects with a backoff that doubles to a cap and resets after a good tick; a silent connection is replaced after an idle timeout; `TelemetryRuntime` supervises the stream task and a crash only logs.
- **Hostile input is bounded.** Frames are capped at 1 MiB before they are read, gzip bodies are inflated with a 1 MiB limit and refused if they would expand past it, and crossed or non-finite prices are dropped.

## A bug the tests found in my own first version

The first full run hung for more than two minutes. Repeating the suite showed 3 hangs in 30 runs, always in the same test. Cause: on Python 3.11 and earlier `asyncio.wait_for` returns the finished result and drops the caller's cancellation when the awaited work completes in the same loop tick. The feed used `wait_for(connection.receive(), idle_timeout)`, so stopping it could silently fail and leave it running. In production that would be a bot that does not shut down cleanly.

RED, deterministic (3 of 3), against a bare `wait_for` implementation:

```text
FAIL: test_cancelling_the_waiter_is_never_swallowed_even_if_the_work_finishes_in_the_same_tick
AssertionError: CancelledError not raised
```

Fix: `ws.with_timeout`, built on `asyncio.wait` plus cancelling whatever is left, used for the connect, the handshake and the receive. After it, 40 of 40 repeated runs passed with no hang.

## RED evidence

```text
ModuleNotFoundError: No module named 'bot.telemetry.ws'                    # codec + client
ModuleNotFoundError: No module named 'bot.telemetry.book_ticker'           # parse + feed
TypeError: MarketDataPoller.__init__() got an unexpected keyword argument 'stream_fresh_seconds'
AttributeError: 'MarketDataPoller' object has no attribute 'on_stream_tick'
AttributeError: 'Settings' object has no attribute 'telemetry_price_stream'
TypeError: TelemetryRuntime.__init__() got an unexpected keyword argument 'stream'
AssertionError: CancelledError not raised                                  # wait_for swallowing cancel
```

## Verification

- Full suite: `Ran 827 tests ... OK`.
- Coverage: 84% total (gate 80%). New modules: `ws.py` 91%, `book_ticker.py` 95%, `poller.py` 99%, `runtime.py` 99%. The uncovered lines are error branches of the socket layer.
- Hang check: the telemetry modules repeated 40 times, each under a 25 s alarm: 40 passed, 0 timed out (30 runs before the fix: 3 timed out).
- `compileall`, `git diff --check` and `tests.test_secret_hygiene` all pass.
- `pip check` could not run in the dev environment (its Python has no pip). No dependency was added and `requirements.txt` is unchanged, so there is nothing for it to catch.
- Semgrep flags `hashlib.sha1` in `accept_key`. This is a false positive: RFC 6455 fixes SHA-1 for the `Sec-WebSocket-Accept` handshake value, it protects nothing, and the call is marked `usedforsecurity=False` with a `nosemgrep` note.

## Not verified

Only 20 s of the live stream was observed, from a probe and not from this client. Behaviour over hours (server-side disconnects, ping cadence drift) is covered by reconnect tests against a fake server, not by a long live run.

## Deployment gate

1. Merge, then `git pull` on the VPS.
2. The new code only takes effect after a restart of `ai-perp-volume`, and a restart with an open position triggers the `set_margin_mode` crash loop. Restart only when the venue is flat.
3. Add `TELEMETRY_PRICE_STREAM=true` to the VPS `.env` in the same restart, and rebuild nothing: the dashboard needs no change.
4. After the restart, check the pm2 error log for `telemetry price stream` warnings and watch the 1 s chart move about twice a second. To roll back, set `TELEMETRY_PRICE_STREAM=false` and restart flat; polling resumes as it was.
