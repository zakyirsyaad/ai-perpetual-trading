# Dashboard MiMo token usage

## User journey

1. The operator wants to see how many MiMo tokens the bot spends, on the dashboard. Under the SHORT-only filter the
   model is called as often as before but fewer entries are accepted, so tokens per trade matter.
2. Chosen with the operator: a thin strip under the volume panel that opens for detail, tokens per day with the
   input / reasoning / output mix and entry vs exit, and **tokens per closed trade**. **No cost estimate** (no price
   figure to get wrong). Like volume, it starts at `VOLUME_CYCLE_STARTED_AT`.

## RED evidence

```
ImportError: Failed to import test module: test_dashboard_usage
ImportError: cannot import name 'DailyAiUsage' from 'bot.ai.usage'

(tests/test_dashboard_contract.py, entries UsageSummary / UsageDay added to INTERFACES)
AssertionError: interface UsageDay not found in dashboard/src/lib/bot-types.ts     (failures=4)
```

## GREEN implementation

- `ai/usage.py` - `DailyAiUsage`, one trading day of spend. `output_tokens` includes `reasoning_tokens` (the provider reports
  reasoning as part of the completion), so the screen derives the visible answer as output minus reasoning and the three
  segments add up to the total.
- `storage/supabase_trade_store.py` - `daily_ai_usage_series()` reads `ai_call_metrics` (only the seven columns it sums) and
  `trades.closed_at` since the cycle start, scoped to the exchange, and buckets both by trading-timezone day with
  `_parse_timestamp` (Python 3.10 safe). `_all_cycle_rows` is the old closed-trade pager generalised over table and time
  column; `_all_closed_trade_rows` now delegates to it. A day of model calls can pass PostgREST's 1000-row cap, so paging is tested.
- `telemetry/wire.py`, `hub.py`, `observer.py`, `server.py` - `UsageDay` / `UsageSummary`, `usage_updated`, `record_usage`,
  `usage_body()` (`{}` until the first publish), route `GET /usage`. Today is always listed, with zeros before its first call.
- `core/trade_engine.py` - `_publish_usage` beside `_publish_volume`: start of every tick and after each close; no-op when the
  feed is off; never raises.
- Dashboard: `lib/usage.ts` (pure helpers: token mix, tokens per trade, sums), `lib/useUsage.ts` (polls `/usage` every 30 s,
  keeps the last good figures), `components/UsagePanel/*`, one line in `app/page.tsx`.

## UI / UX

| Decision | Why |
|---|---|
| One line (33 px) under the volume panel, opened by a `Detail` button | the desktop shell is exactly one screen tall and the chart gives way; a full block would have cost ~100 px of chart |
| Open state remembered in `localStorage` (try/catch) | per-viewer convenience; the page is correct without it |
| Mix drawn as solid black / solid grey / hatched, with a text legend | meaning is carried by shape and numbers, never by colour alone; green and red stay reserved for PnL and buy/sell |
| Opened list shows ~4 days and scrolls | opening it must not squeeze the chart on a 768 px screen |
| `no calls yet` and `—/trade` when nothing happened | a division by zero trades would lie |
| Under 760 px the strip wraps to two lines and the list drops the calls and entry/exit columns | no horizontal scroll |

## Verification

| Check | Result |
|---|---|
| Full suite | 884 passed on Python 3.11 and on Python 3.10 (the VPS runtime), coverage 85% |
| Test-double noise | three fake stores/observers now implement the two display-only feeds, which also removes ~40 swallowed `AttributeError` log lines that `volume` had already been causing |
| `compileall`, `git diff --check`, `tsc --noEmit`, `next build` | clean |
| Contract | `test_dashboard_contract` compares `UsageSummary` / `UsageDay` keys with `bot-types.ts` |
| Visual check | real local telemetry server with example data (not real usage), widths 1440, 1024 and 375, closed / open / empty; no horizontal scroll; closed strip 33 px |
| Secret hygiene | `test_secret_hygiene` passes inside the suite |

## Deployment gate

1. Merge only on the operator's approval.
2. No `.env` change. Restart the bot **only while the venue is flat** (check the exchange and `open_position_state`, not the log);
   rebuild the dashboard with `FEED_PROXY_TARGET=http://127.0.0.1:3917 NEXT_PUBLIC_API_URL=/feed` and restart `ai-perp-dashboard`.
3. Cross-check on the VPS: today's total from `/usage` against `report_ai_usage.py --days 1` (they differ only by the day boundary,
   WIB here and UTC there).

## Known limits

- Tokens include failed calls and retries (they were spent); the failed count is shown beside them.
- Tokens per trade divides a day's tokens by the trades *closed* that day, so a day with a long-held position straddling
  midnight is attributed to the closing day.
- `arena_review` calls count in the total but not in the entry / exit split.
- No cost figure: if one is wanted later, `AI_INPUT_COST_PER_MILLION_TOKENS` / `AI_OUTPUT_COST_PER_MILLION_TOKENS` already feed
  `estimated_cost_usd` in `ai_call_metrics`.
