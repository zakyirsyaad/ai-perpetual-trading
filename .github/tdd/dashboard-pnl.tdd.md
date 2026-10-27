# Dashboard header figures before the first block

## User journey

1. The operator moved the dashboard's start to 26 Oct (`VOLUME_CYCLE_STARTED_AT`, which needs a bot restart). The header takes
   realized from the newest feed block's `totals` and unrealized from its `position`, and a run publishes no block until its
   first AI decision, position resume or close. The bot was idle (daily cap reached), and the same happens over a weekend, in a
   closed entry window or in a regime hour that maps no side. So after the restart the header had no real figure for hours.
2. What it showed in the meantime was not a skeleton, as first assumed, but `+$0.00` for both figures: `useFeed` paints a
   price-only stub block (`block: 0`, flat, totals 0) from the first price event, and the page took any non-null `latest` for a
   block. An independent review found it; a reproduction with the page's own reducer and a browser check with price events
   confirmed it. (The first fake-feed check published no price events, so it could not have shown it.)
3. Asked for: the header shows realized right after a restart, through a route of its own like `/volume` and `/usage`.
   Not merged and not deployed without the operator's go-ahead.

## RED evidence

```
ImportError: cannot import name 'PnlSummary' from 'bot.telemetry.wire'        (tests/test_dashboard_pnl.py)
AttributeError: module 'bot.telemetry.wire' has no attribute 'PnlSummary'       (tests/test_dashboard_contract.py, entry added to INTERFACES)

AssertionError: Expected values to be strictly deep-equal:                      (tests/test_dashboard_header_pnl.py, headerPnl given a stub block)
+   realized: 0
-   realized: -190.67
```

The page's real `reducer`, `paintLatest` and `lastRealDecision` against a `/pnl` of -190.67: after the snapshot of a freshly
restarted bot the header read `{ unrealized: 0, realized: -190.67 }`; after the first price event `latest.block` was 0,
`latest.totals.realizedUsd` was 0 and the header read `{ unrealized: 0, realized: 0 }`.

## GREEN implementation

- `telemetry/wire.py` - `PnlSummary(realized_usd, unrealized_usd)`, dumped as `realizedUsd` / `unrealizedUsd`.
- `telemetry/hub.py`, `telemetry/server.py` - `record_pnl`, `pnl_body()` (`{}` until the first publish) and the route `GET /pnl`.
- `telemetry/observer.py` - `_publish_pnl()` hands the hub `_realized_usd` and the sum of the open positions' marks. It is called
  from `history_loaded` (the startup seed), `position_closed`, `entry_filled`, `position_resumed` and `position_marked`, that is
  from every hook that changes either figure. The engine is untouched: no new hook, no new store read.
- Dashboard - `PnlSummary` in `lib/bot-types.ts` (the contract test compares it with the wire model); `lib/usePnl.ts` (polls
  `/pnl` every 15 s, keeps the last figures on a failed poll and on one empty answer, and clears them on a second empty answer in a row); `headerPnl()` in `lib/pnl.ts`,
  which counts only blocks with `block > 0` and otherwise uses `/pnl`; a few lines in `app/page.tsx`.
- `dashboard/scripts/header_pnl.check.mjs` - a plain script that runs the real `headerPnl` against a stub, a real block and
  nothing; `tests/test_dashboard_header_pnl.py` runs it under bun and is skipped where bun is not installed.

## Decisions

| Decision | Why |
|---|---|
| Blocks the bot published win while any exist; `/pnl` only fills the gap before the first | blocks carry the live mark of an open position every `MARK_INTERVAL_SECONDS` (5 s); `/pnl` is polled every 15 s |
| The price-only stub is not a block (`block > 0`) | `useFeed` paints it from the first price event; counting it made the header read zero. The bot's own blocks start at 1 (`_next_block` increments before use) |
| `latestByCoin` itself is left alone | `SleeveStrip` uses the stub to show a price before the first block |
| An open tab after a bot restart falls back to `/pnl` too | `useFeed` replaces its per-coin state from `/snapshot` on every reconnect, and a restarted bot's snapshot has no block |
| A failed poll or one empty answer keeps the last figures; a second empty answer in a row clears them | the figures are published only after the bot's startup reads, so one empty answer must not flash a skeleton at a normal restart; two mean an older process's figure (possibly of an older cycle) must not stay up |
| Both figures in the route, not only realized | otherwise the header would show a number next to a skeleton |

## Verification

| Check | Result |
|---|---|
| New tests | `tests/test_dashboard_pnl.py`: wire keys, the seed served before any block, realized moves on a close, unrealized follows the open position and returns to 0, an entry fill, a resumed position, bad input keeps the old figures, `{}` before the first publish, the HTTP route |
| Header check | `tests/test_dashboard_header_pnl.py` runs `header_pnl.check.mjs` under bun: nothing known, `/pnl` only, a stub block, the bot's first block (number 1), a later block beside a display-only stub |
| Full suite | 3016 tests OK on Python 3.10 (the VPS runtime); line coverage 92% (gate 80%): `hub.py` 94%, `observer.py` 98%, `server.py` 99%, `wire.py` 100% |
| `compileall`, `git diff --check`, `tsc --noEmit`, `next build` | clean |
| Contract | `test_dashboard_contract` compares `PnlSummary` keys with `bot-types.ts` |
| Browser check | the real hub, observer and server with example data (not real figures), a price event every 2 s as `MarketDataPoller` publishes them, and this branch's dashboard: before the `headerPnl` fix `unrealized +$0.00 realized +$0.00` with the seed in place and no block; after it `realized -$190.67`; first block published, unchanged; then a simulated bot restart with the tab left open (a new feed process, another seed, no block): `-$500.00` without a reload |
| Secret hygiene | `test_secret_hygiene` passes inside the suite |
| Local coverage run | Python 3.10 cannot read `pyproject.toml` for coverage (no tomli), so an equivalent rc file was used (`source = src`, the same omit, `fail_under = 80`); CI's 3.11 reads the real one |

## Deployment gate

1. Merge only on the operator's approval.
2. No `.env` change. The new route needs a bot restart, **only while the venue is flat** (`flat_check`, not the log). Rebuild the
   dashboard with `FEED_PROXY_TARGET=http://127.0.0.1:3917 NEXT_PUBLIC_API_URL=/feed` after `export PATH=$HOME/.bun/bin:$PATH`,
   check the build's exit code, then restart `ai-perp-dashboard` (a restart after a failed build serves the old one).
3. Check on the VPS: `curl 127.0.0.1:3917/pnl` returns the realized figure right after the restart, before any block, and equals
   the header once a block exists; `127.0.0.1:3918/feed/pnl` answers through the dashboard's proxy.

## Known limits

- A new page against an old bot gets a 404 from `/pnl`: with only a price stub and no figures it draws a skeleton where the old page
  drew `+$0.00` (not a regression); an old page ignores `/pnl`.
- If the startup seed fails ("Could not seed the dashboard history"), `/pnl` stays `{}` until the first hook fires, and an open tab
  keeps the previous process's figure for one more poll (up to 30 s in all) before it shows a skeleton.
- Route and blocks both read `_realized_usd`; after a close they differ only by the poll interval, because the block updates at once
  and the page prefers blocks.
- The dashboard has no JS test runner. `headerPnl` is pinned by a bun script that CI skips (CI has no bun); adding
  `oven-sh/setup-bun` to the workflow would run it there. The page wiring is otherwise covered by `tsc` and the browser check.
- Pre-existing, not changed here: if a close-phase failure finds the venue flat, no `position_closed` fires and `_open` keeps a
  phantom unrealized figure (in the blocks and now in `/pnl`) until a restart.
