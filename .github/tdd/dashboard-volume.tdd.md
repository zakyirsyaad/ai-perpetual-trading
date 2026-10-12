# Dashboard volume panel

## User journey

The operator wants the dashboard to show (1) today's traded volume against the daily target and (2) the
volume of every day together with the running total up to that day. The bot's own in-memory totals restart
from zero with the process, so the figures come from the trade store, the same source the bot's targets read.

## RED evidence

`tests/test_dashboard_volume.py` and the extended contract test were written before any implementation:

```
ImportError: cannot import name 'VolumeDay' from 'bot.telemetry.wire'
Ran 1 test ... FAILED (errors=1)

tests/test_dashboard_contract.py
AttributeError: module 'bot.telemetry.wire' has no attribute 'VolumeSummary'
```

After the Python side was done the contract test still failed on purpose until the TypeScript types existed
(`interface VolumeDay not found in dashboard/src/lib/bot-types.ts`).

## GREEN implementation

- `telemetry/wire.py` - `VolumeDay`, `VolumeSummary` (camelCase wire, unknown keys refused).
- `storage/supabase_trade_store.py` - `daily_volume_series()` groups closed trades by trading-timezone day since
  the cycle start (same cutoff as `total_volume_all_time`). `_parse_timestamp` normalises Supabase's 5-digit
  fractions, which Python 3.10 on the VPS rejects. Added to `TradeStoreProtocol`.
- `telemetry/observer.py` - `volume_updated(today, daily, daily_target_usdt)` builds the series with the running
  total and always lists today. Wrapped by `_never_raises`.
- `telemetry/hub.py` + `server.py` - `record_volume`, `volume_body`, and `GET /volume` (`{}` until the first update).
  The figures ride a separate route instead of every block event: they change rarely, and they stay readable while
  the weekly entry window is closed and the feed publishes no events.
- `core/trade_engine.py` - `_publish_volume()` at the start of every tick and after each closed trade. It does
  nothing when the dashboard feed is off, and a failing store read is logged and swallowed. Nothing the engine
  decides reads it back.
- Dashboard: `VolumeSummary`/`VolumeDay` types, `useVolume` (polls `/volume` every 30 s), `VolumePanel` under the header.

## Verification

| Check | Result |
|---|---|
| New tests | 14 in `test_dashboard_volume.py` (wire, observer, hub, server, store, engine) |
| Contract test | passes: TS interfaces match the Python models |
| Full suite | 841 passed |
| Coverage | 84% (gate 80%) |
| `compileall`, `git diff --check` | clean |
| `tsc --noEmit`, `next build` | clean |
| Visual check | desktop and 400 px width against a mock `/volume` (example data, not real figures) |
| Secret hygiene | `test_secret_hygiene` passes inside the suite |
| `pip check` | not run - uv-managed interpreter has no pip; no dependency changed |

## Deployment gate

1. Merge only on the operator's approval.
2. The feed change is Python, so the bot restarts once, **only while the venue is flat**.
3. Rebuild the dashboard on the VPS with `FEED_PROXY_TARGET=http://127.0.0.1:3917 NEXT_PUBLIC_API_URL=/feed`
   and restart pm2 `ai-perp-dashboard`; `/feed/volume` is covered by the existing rewrite.
4. Check `curl http://127.0.0.1:3917/volume` returns the figures and the pm2 error log has no
   `Could not publish volume` warnings.

## Known limits

- The cumulative series starts at `VOLUME_CYCLE_STARTED_AT` and does not distinguish accounts.
- Days without trades (weekends) are not listed, only days that carry volume plus today.
- After midnight the panel's "today" row flips on the next tick (up to one tick, about 4 minutes).
