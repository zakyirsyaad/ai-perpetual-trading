# Dashboard Engine Hooks — TDD Evidence

## User journey

The operator wants the dashboard to show what the BingX bot actually does: each decision, the entry order, the fill, the open position with its running PnL, and the close with its reason. The earlier changes landed the wire contract, the state, the read-only server and the wiring in `main.py`, but with only prices flowing. This change connects the trading engine to that feed.

The feed is still off by default (`TELEMETRY_ENABLED=false`). With it off the engine holds a do-nothing observer and behaves exactly as before.

## Design decisions

- **The engine only reports, never asks.** `TradeEngine` gets an optional `telemetry` argument and calls six synchronous hooks: `decided`, `entry_quoted`, `entry_filled`, `position_resumed`, `position_marked`, `position_closed`. No decision reads a return value.
- **Hooks run after the safety step they follow.** The mark comes after the stop-loss check in `_monitor_position`. The close is reported after the trade is saved, the position removed and the stop-loss cooldown set. The fill is reported after `save_open_position`. A gated tick (risk guard, cooldowns, daily target) never reaches the observer.
- **A hook cannot fail into trading.** Every public method of `EngineTelemetry` catches and logs its own errors. Two tests break the observer and the hub on purpose and still see a complete trade: entry and exit orders sent, trade stored, venue flat.
- **A mark updates a block in place.** The dashboard replaces a block that arrives with a number it already holds, so a live position is refreshed with `TelemetryHub.refresh_latest` (at most every 5 s) instead of adding a history row per refresh. Decisions are therefore never pushed out of the 1000-row history by a long hold.
- **Defaults for engines built without `__init__`.** `_telemetry` also exists as a class attribute, because several existing tests build `TradeEngine` with `object.__new__`.

## Honest simplifications

The dashboard's contract was written for a model that publishes a probability per action. This bot does not, so:

- `probabilities` is one-hot on the action taken, and the model's own `confidence` travels under its own key. It is not shown as a probability.
- `upIn10`, a required key with no counterpart here, is the neutral 0.5.
- `jevUsd` (AI cost) is 0 for now; `accountValue` and `withdrawable` are null, because reading them would add private endpoint calls.
- Unrealized PnL is the engine's net figure (gross minus estimated round-trip taker fees), the same number its exit logic uses, so it will read lower than the BingX screen, which is before fees.
- One HOLD tick produces one hold block per traded coin; an entry produces one block for its coin only.
- If no price has been seen for a coin yet, its decision block is skipped rather than invented. A close always publishes, using the exit price when no live price is known.

## RED evidence

```text
ModuleNotFoundError: No module named 'bot.telemetry.observer'
Ran 22 tests ... FAILED (errors=5)                      # hub + observer
TypeError: TradeEngine.__init__() got an unexpected keyword argument 'telemetry'
Ran 13 tests ... FAILED (errors=13)                     # engine hooks
KeyError: 'telemetry'
Ran 26 tests ... FAILED (errors=4)                      # runtime observer + main wiring
```

Two early test mistakes were fixed in the tests, not the code: the wire key for a fill's direction is `dir`, and a `None` history was not a realistic fault for `refresh_latest`.

## GREEN implementation

- `telemetry/observer.py` (new): `EngineObserver` protocol, `NullObserver`/`NULL_OBSERVER`, `EngineTelemetry` with the six hooks, running totals and per-coin position state.
- `telemetry/hub.py`: `latest_price(coin)` and `refresh_latest(coin, **update)`.
- `telemetry/runtime.py`: `build_telemetry` builds the observer; `TelemetryRuntime.observer`.
- `core/trade_engine.py`: the `telemetry` argument and six one-line hook calls, one per hook.
- `main.py`: the feed is built before the engine and its observer is passed in.

## Verification

```text
PR 2c tests: 85 passed (observer 25, engine hooks 13, hub 21, runtime 15, main composition 11)
Full test suite: 762 passed (716 before this change)
Line coverage: 84% (required minimum: 80%); observer.py 100%; main.py 100%; trade_engine.py 84%
compileall: passed
git diff --check: passed
secret hygiene test: passed (part of the full suite)
pip check: not run, `pip` is not available in this interpreter; no dependency was added
```

A live read-only smoke run: the poller read the real BingX VST public API (no real keys, no private endpoint) while the observer replayed a simulated trade through the real hub and HTTP server. `/history` returned BTC blocks `hold (flat) → open (long, fill) → close (flat, fill)` and ETH `hold`; totals showed 1 quote, 2 fills, fees 0.08, realized 0.9; `/tape` held 1674 BTC points including 2 fill marks.

## Commit trail

Branch `feat/dashboard-wiring` (stacked on `feat/dashboard-server`). Committed as `feat: report engine decisions, fills and positions to the dashboard feed`.

## Deployment gate

- **Flat position before any restart**, as before: enabling the feed restarts `ai-perp-volume`, and a restart with an open position hits the `set_margin_mode` crash loop (see CLAUDE.md).
- **Default stays off.** Until `TELEMETRY_ENABLED=true` is set on the VPS, this deploy is behaviourally identical; the engine holds the do-nothing observer.
- **First live check when enabling:** watch the pm2 error log for `telemetry ... failed` warnings across one full entry and exit before relying on the dashboard.
- **Not yet visible:** the dashboard itself (next change). Until then the feed can be read with `curl` through the SSH tunnel.
