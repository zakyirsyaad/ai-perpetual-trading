# Dashboard Feed Wiring — TDD Evidence

## User journey

The operator wants to watch the BingX bot live in a dashboard adapted from aowang-ai/jev-trade (MIT). The previous changes landed the wire contract, the bus, the state and a read-only server. This change wires them into the bot: configuration, a background market-data poller so the chart moves even while the bot is flat, and the `main.py` start and stop.

The feed is off by default (`TELEMETRY_ENABLED=false`). With it off, `build_telemetry` returns `None` and nothing else changes. The engine hooks for decisions, quotes, fills and positions are a separate follow-up.

## Design decisions

- **Loopback only, enforced at boot.** `TELEMETRY_HOST` must be `127.0.0.1`, `localhost` or `::1`; anything else fails settings validation even while the feed is disabled, so a bad value is caught before anyone enables it. Remote access is an SSH tunnel.
- **One CORS origin, validated.** `TELEMETRY_ALLOWED_ORIGIN` must be a bare `http(s)://host[:port]`: no wildcard, path or trailing slash.
- **The poller can only read.** It calls `get_order_book` and `get_klines` and nothing else. The test client raises `AssertionError` on any other attribute access, so touching an order, account or position endpoint fails the suite.
- **Isolated from trading.** It runs in its own task, calls are sequential and bounded by a timeout, failures are contained per symbol and per call kind, and a streak of failures is logged once and then one in twelve instead of every cycle.
- **Trading never depends on it.** A server that cannot bind logs a warning and trading continues. A crashed poller is logged and the server keeps serving. `stop()` never raises, because it runs in `main.py`'s `finally`.

## RED evidence

Config, poller and runtime tests were written before the code:

```text
ERROR: test_telemetry_is_off_by_default_and_loopback_only (tests.test_telemetry_config...)   # no telemetry_* settings
ModuleNotFoundError: No module named 'bot.telemetry.poller'
ModuleNotFoundError: No module named 'bot.telemetry.runtime'
```

The `main.py` wiring tests failed before the edit:

```text
AttributeError: <module 'main'> does not have the attribute 'build_telemetry'
Ran 9 tests ... FAILED (errors=4)
```

The shutdown test failed before `stop()` was made non-raising:

```text
ERROR: test_a_failing_shutdown_is_logged_and_never_masks_the_real_error
Ran 13 tests ... FAILED (errors=1)
```

## GREEN implementation

- `config.py`: `TELEMETRY_ENABLED`, `TELEMETRY_HOST`, `TELEMETRY_PORT`, `TELEMETRY_ALLOWED_ORIGIN`, `TELEMETRY_PRICE_INTERVAL_SECONDS`, with the loopback and origin validators.
- `telemetry/poller.py`: `MarketDataPoller` (`poll_once`, `run`), `coin_of`, `pair_of`.
- `telemetry/runtime.py`: `build_meta`, `TelemetryRuntime` (`start`, `stop`), `build_telemetry`.
- `main.py`: builds the feed after the engine, starts it before `run_forever`, stops it first in `finally`.
- `.env.example`: the five new keys, all safe defaults.

## Verification

```text
PR 2b tests: 39 passed (config 7, poller 10, runtime 14, main composition 8 including 4 new)
Full test suite: 716 passed
Line coverage: 83% (required minimum: 80%); telemetry modules 98% to 100%; main.py 100%
compileall: passed
git diff --check: passed
secret hygiene test: passed (part of the full suite)
pip check: not run, `pip` is not available in this interpreter; no dependency was added
```

A live smoke run of the poller against the real BingX VST public API (no keys, read-only): 2 cycles in 3.3 s, 999 one-minute and 671 fifteen-minute candles loaded, live BTC price around 83,020 changing between cycles, first paint 402 tape points of 1,672.

## Commit trail

Branch `feat/dashboard-wiring` (stacked on `feat/dashboard-server`). Committed as `feat: wire the dashboard feed into the bot behind a flag`.

## Deployment gate

- **Flat position before any restart.** Enabling the feed on the VPS means restarting `ai-perp-volume`, which hits the `set_margin_mode` crash-loop if a position is open (see CLAUDE.md). Flip it flat first.
- **Default stays off.** Add `TELEMETRY_ENABLED=true` to the VPS `.env` only when ready. With the default, this deploy is behaviourally identical.
- **Access is an SSH tunnel**, for example `ssh -L 3000:localhost:3000 0xAlvary2`, then run the dashboard locally against `http://localhost:3000`. Nothing is opened to the internet.
- **Extra load when on:** one order-book request per symbol every 5 s, and two kline requests per symbol every 60 s, all public and read-only.
