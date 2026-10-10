# Dashboard State and Server — TDD Evidence

## User journey

The operator wants to watch the BingX bot live in a dashboard adapted from aowang-ai/jev-trade (MIT). The previous change landed the wire contract and the bus. This change adds what sits behind them: the per-symbol decision history and chart tape, and a small read-only HTTP and Server-Sent Events server that serves them in the shape the dashboard already reads.

Nothing is wired into `main.py` or `TradeEngine` yet and no dependency was added, so the live trading path is untouched. The follow-ups are the background price and candle poller with `main.py` wiring, then the engine hooks.

## Design decisions

- **Standard-library asyncio, no `aiohttp`.** This process holds trading keys, so it avoids a new dependency for six GET routes.
- **Narrow surface.** GET and OPTIONS only, one request per connection, request heads capped at 8 KiB and 5 s, SSE clients capped. The `Host` header must name a loopback host (stops DNS rebinding through the loopback listener), and CORS allows exactly one configured dashboard origin.
- **Tape built the way the dashboard reads it.** `VenueChart` is a port of jev-trade's chart store: venue 1 minute and 15 minute candles, live 1 second candles folded from mids, and fills as separate points. `clip` reproduces its first-paint and `/tape` size limits.
- **Kept bounded.** One second bars are capped at 900 (15 minutes), minute bars at 5,000, 15 minute bars at 1,000, history at 1,000 events per symbol.
- **Never in the trading path.** Every `record_*` method on the hub swallows and logs its own failures, and the server never awaits or writes to the engine.
- **Cloudflare Quick Tunnel rejected for access.** Its documentation states it does not support SSE and is for testing only. Access is a local-only bind reached through an SSH tunnel.

## RED evidence

The tests were written before the modules existed. The first run failed on every new module:

```text
ImportError: cannot import name 'PricePoint' from 'bot.telemetry.wire'
ModuleNotFoundError: No module named 'bot.telemetry.clip'
ModuleNotFoundError: No module named 'bot.telemetry.chart'
(hub and server tests failed the same way)
```

## GREEN implementation

- `wire.py`: `PricePoint`, `PricePointFill` and `sse_event`, with the same omit-when-unset rules as the rest of the contract.
- `clip.py`: `clip_history`, `clip_tape`, `clip_snapshot_tape`.
- `chart.py`: `VenueChart` with `load_klines`, `upsert_candle`, `add_mid`, `add_fill`, `points`.
- `hub.py`: `TelemetryHub` with `record_block`, `record_quote`, `record_fill`, `record_price`, `load_klines` and the JSON bodies for `/`, `/snapshot`, `/history`, `/tape`.
- `server.py`: `TelemetryServer` with the five routes plus `/events`.

## Verification

```text
Telemetry tests: 88 passed on the first implementation run, then 7 more added for uncovered branches
Full test suite: 682 passed
Line coverage: 83% (required minimum: 80%); telemetry modules 99% to 100%
compileall: passed
git diff --check: passed
secret hygiene test: passed (part of the full suite)
pip check: not run, `pip` is not available in this interpreter; no dependency was added
```

A live smoke run with `curl` against the real server confirmed: `/snapshot` returns 200 with the expected keys, `/events` opens with a snapshot frame and the CORS header, a foreign `Host` header gets 403, `POST` gets 405, and no subscriber is left after the client disconnects.

## Commit trail

Branch `feat/dashboard-server` (stacked on `feat/dashboard-wire`), commit `feat: add dashboard state and read-only server`.

## Deployment gate

None for this change: nothing imports the server or hub from `main.py` yet, so a deploy is behaviourally identical.

The gate belongs to the wiring change: `TELEMETRY_ENABLED` stays `false` by default, the listener binds to loopback only and refuses any other host at startup, the position must be flat before any redeploy (see the restart crash-loop note in CLAUDE.md), and the dashboard is reached through an SSH tunnel.
