# Dashboard per day, cycle restart, and no 10M stop

## User journey

1. The dashboard's History and Trades mixed trades of several days in one list with only a clock time, and the
   volume list still started on 14 Sep. The operator wants the dashboard organised per day and starting today.
2. The operator chose to move the volume cycle to today (`VOLUME_CYCLE_STARTED_AT`) so the dashboard and the bot
   agree, and to **delete the "bot stops at 10M cumulative volume" rule**, which is no longer needed.
3. The header's `realized` read `+$0.00` right after a restart although three trades had closed that day, because
   its counters live in memory (`unrealized` correctly read 0: the position was flat).

## RED evidence

```
FAIL: test_the_cumulative_volume_target_no_longer_stops_the_bot
AssertionError: 'volume_target_reached' is not None
FAIL: test_trades_closed_before_the_cycle_start_are_left_out
AssertionError: Lists differ: [10, 12] != [12]
ERROR: test_the_stored_realized_pnl_shows_in_the_totals_of_the_next_block   (no realized_usd parameter)
ERROR: test_sums_the_net_pnl_of_every_trade_since_the_cycle_start           (no realized_pnl_since_cycle)
FAIL:  test_also_hands_over_the_realized_pnl_since_the_cycle_start
Dashboard grouping (bun script): SyntaxError: Export named 'dayKey' not found in module .../lib/fills.ts
```

## GREEN implementation

- `core/trade_engine.py` - the `volume_target_reached` branch of `_volume_runtime_pause_reason` is deleted. Only the daily
  target and the daily fee budget pause entries now. `VOLUME_TARGET_USDT` stays as a planning figure for the volume profile,
  the arena planners and reports (it is woven through those); it no longer gates anything.
- `storage/supabase_trade_store.py` - `recent_closed_trades` drops trades closed before `VOLUME_CYCLE_STARTED_AT`;
  `realized_pnl_since_cycle()` sums the net PnL since the cycle start.
- `telemetry/observer.py` - `history_loaded(trades, realized_usd=...)` also seeds the realized counter that feeds
  `Totals.realizedUsd` and `pnlUsd` (assigned once at startup, before any live trade).
- `dashboard/src/lib/fills.ts` - `dayKey` (WIB day, whatever the browser timezone), `groupByDay`, `lotVolume`.
- `dashboard/src/components/Book/Book.tsx` + CSS - History and Trades render one band per day
  (`Mon 12 Oct  TODAY · 3 trades · net +86.76 · fees 52.44 · volume 150K`), newest day first.
- The volume panel needs no change: its series already starts at the cycle start.

## Verification

| Check | Result |
|---|---|
| Full suite | 859 passed on Python 3.11 and on Python 3.10 (the VPS runtime), coverage 84% |
| Flake watch | one full-suite run hung (CPU spinning); six consecutive reruns and two more full runs passed. Not attributed to this change; CLAUDE.md already records an intermittent telemetry hang |
| `compileall`, `git diff --check`, `tsc --noEmit`, `next build` | clean |
| Day grouping | bun script: 23:59:59 WIB stays the 11th, 00:00:00 WIB starts the 12th, order kept, lot volume correct |
| Visual check | desktop width against a real local telemetry server seeded with example multi-day trades: day bands, TODAY tag, per-day sums |
| Secret hygiene | `test_secret_hygiene` passes inside the suite |

## Deployment gate

1. Merge only on the operator's approval.
2. VPS `.env`: `VOLUME_CYCLE_STARTED_AT=2026-10-12T00:00:00+07:00` (a backup first). The line `VOLUME_TARGET_USDT` can stay.
3. Restart the bot **only while the venue is flat** (check the exchange and `open_position_state`, not the log), then rebuild
   the dashboard with `FEED_PROXY_TARGET=http://127.0.0.1:3917 NEXT_PUBLIC_API_URL=/feed` and restart `ai-perp-dashboard`.
4. Expect `/volume` to list only 12 Oct onward and the header `realized` to match the day's closed trades after the first tick.

## Known limits

- Header `realized` is cumulative since the cycle start, not per day; the per-day figure is in each History band.
- The header still needs a block event: on a weekend (entry window closed) no block is published after a restart, so
  `realized` shows a placeholder until Monday's first tick.
- The Python flake above is unexplained; if CI ever hangs, rerun and report the test name.
