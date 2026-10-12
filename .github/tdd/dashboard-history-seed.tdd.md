# Dashboard History and Trades survive a restart

## User journey

After a bot restart the dashboard's History and Trades tabs were empty even though the day's trades (three on
12 Oct) were safely in the database. Both tabs are built from fills on the chart tape, and the tape lives in
memory, so every restart reset them. The operator expects them to keep showing the recent trades.

## RED evidence

`tests/test_dashboard_history.py` was written before any implementation (11 tests):

```
Ran 11 tests ... FAILED (failures=1, errors=10)
  AttributeError: 'TelemetryHub' object has no attribute 'seed_fills'
  AttributeError: 'EngineTelemetry' object has no attribute 'history_loaded'
  AttributeError: 'SupabaseTradeStore' object has no attribute 'recent_closed_trades'
  AttributeError: 'TradeEngine' object has no attribute '_seed_dashboard_history'
```

## GREEN implementation

- `storage/supabase_trade_store.py` - `recent_closed_trades(limit)` turns the newest closed-trade rows into
  `ClosedTrade` objects, oldest first. Unreadable rows (bad direction, bad timestamp) are logged and skipped;
  timestamps go through `_parse_timestamp` (Python 3.10 safe). Added to `TradeStoreProtocol`.
- `telemetry/observer.py` - `history_loaded(trades)` rebuilds the two fills the live path would have produced:
  an open fill, then a close fill carrying the net PnL and the **total** fee, as `position_closed` does. The store
  keeps only the net figure, so the fee is recovered as gross minus net. One unreadable trade is skipped, not fatal.
- `telemetry/hub.py` - `seed_fills(coin, fills)` adds them to the chart tape quietly: no block is touched and
  nothing is published, because no client has anything to replace at startup. Clients get them through `/tape`.
- `core/trade_engine.py` - `_seed_dashboard_history()` reads `DASHBOARD_HISTORY_TRADES` (60) trades once in
  `run_forever`, **before** `_resume_persisted_positions()` (that call blocks until a resumed position closes).
  It does nothing when the feed is off, and a failing store read is logged and swallowed.

No dashboard code changed: History and Trades already derive their rows from tape fills.

## Verification

| Check | Result |
|---|---|
| New tests | 11 in `test_dashboard_history.py` (hub, observer, store, engine, startup order) |
| Full suite | 858 passed on Python 3.11 **and** Python 3.10 (the VPS runtime) |
| Coverage | 84% (gate 80%) |
| `compileall`, `git diff --check` | clean |
| Dashboard logic | the seeded tape was fed through the dashboard's own `tapeFills` and `closedLots` with bun: 3 trades gave 6 Trades rows and 3 History rows with the right side, entry, exit, PnL and fee |
| Secret hygiene | `test_secret_hygiene` passes inside the suite |
| `pip check` | not run - uv-managed interpreter has no pip; no dependency changed |

## Deployment gate

1. Merge only on the operator's approval.
2. The change is Python only, so the bot restarts once, **only while the venue is flat** (check the exchange and
   `open_position_state`, not the log, which rotates at 00:00 CST). The dashboard needs no rebuild.
3. After the restart, `curl http://127.0.0.1:3917/tape` should contain fill points for the recent trades, and
   the History tab should list them after a hard refresh.

## Known limits

- The first paint (`/snapshot`) keeps only the newest 12 fills, so the tabs show about six trades for a moment
  until `/tape` hydrates the rest.
- Only the newest 60 trades are loaded. Older trades appear in the volume panel but not in History.
- The entry fill carries no fee (the store does not keep it separately); the total sits on the close fill, so the
  Trades tab shows the fee on the close row only.
