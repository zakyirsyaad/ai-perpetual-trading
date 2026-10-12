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

## Follow-up fix: timestamp parsing on Python 3.10 (found on the first deploy)

Right after PR #26 went live, `GET /volume` returned only today with 0 volume while `total_volume_all_time()` read
9,242,929 from the same 202 rows. `daily_volume_series()` returned `[]`.

**Cause.** `_parse_timestamp` collected *every* digit after the first dot, including the digits of the UTC offset
(`+00:00`), then sliced the string with that length, producing a malformed fraction. Python 3.11 (the laptop) is
lenient and parsed it; Python 3.10 (the VPS) raised `ValueError`, every row was skipped, and the series was empty.
The tests passed locally because they only compared the final grouped days, and 3.11 hid the malformed string.

**RED.** `tests/test_dashboard_volume.py::TimestampNormalisationTests` asserts the exact string handed to
`fromisoformat` (`_normalise_iso`), so the bug cannot hide behind a lenient interpreter:

```
ImportError: cannot import name '_normalise_iso' from 'bot.storage.supabase_trade_store'
Ran 1 test ... FAILED (errors=1)
```

**GREEN.** `_normalise_iso` rewrites only the fraction (padded or truncated to 6 digits) and leaves the offset alone;
`_parse_timestamp` uses it.

**Verification.** 847 tests pass on Python 3.11 **and** on Python 3.10 (`uv run --python 3.10 ...`), coverage 84%.
Lesson: run the suite on the runtime that production uses, not only on the laptop's interpreter.
