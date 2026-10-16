# BingX VST AI Futures Bot

Async Python trading bot for **BingX Perpetual Futures VST**. The runtime is intentionally live-data only for execution: it does not simulate fills outside BingX VST. Live BingX endpoints fail closed — `BINGX_ENV` must be `vst` and `BINGX_BASE_URL` must be exactly `https://open-api-vst.bingx.com`.

> **Shell note:** command blocks below use PowerShell (`$env:PYTHONPATH="src"; python ...`). The dev machine is macOS/zsh and the VPS is Linux, so use the POSIX form instead — prefix the var inline: `PYTHONPATH=src python src/main.py`.

## Architecture

```text
src/
  main.py
  bot/
    ai/
      brain.py              # Layer 1: decision brain (merged entry + risk review)
      compact_context.py    # Compact AI payload builder
      exit_policy.py        # Event-driven AI exit gating
      usage.py              # AI call telemetry + retry classification
      orchestration.py      # Layer 2: risk, learning, and execution orchestration
    data/
      providers.py          # Layer 3: external market signals
      market_signals.py
    intelligence/
      market_intelligence.py # Layer 4: market intelligence synthesis
    arena/                 # Backend-only arena factors, flow signals, attribution, and program runner
    exchange/
      contracts.py          # Venue-neutral models + FuturesExchangeProtocol
      bingx_client.py       # BingX VST adapter (runtime venue)
      okx_client.py         # Legacy; unwired, removed after VST validation
      leverage.py
    core/
      trade_engine.py
    risk/
      risk_manager.py
    storage/
      interfaces.py
      supabase_trade_store.py
      supabase_journal.py
    strategy/
      grok_strategy.py
      indicators.py
```

## Live Signal Sources

- BingX VST public/private API: execution source of truth — candles (v3, closed candles only), ticker, depth, commission rate, premium index/funding, open interest, positions, leverage, and orders.
- CoinMarketCap: latest quotes via `/v2/cryptocurrency/quotes/latest` when `COINMARKETCAP_API_KEY` is configured.
- Dune Analytics: runs built-in SQL signals via `/api/v1/sql/execute` when `DUNE_API_KEY` is configured. Saved query IDs in `DUNE_QUERY_IDS` are optional.
- TradingView: uses the included local relay at `src/tradingview_relay.py`. TradingView does not provide a general public market-data API for bots, so alerts must be pushed into the relay.
- Supabase: primary store for executed trades and AI decisions. `src/main.py` refuses to start until Supabase env vars are configured.

## Safety Defaults

- `BINGX_ENV=vst` is required and `BINGX_BASE_URL` must be exactly `https://open-api-vst.bingx.com`, the mandatory primary host. Both the client constructor and the trade engine refuse to start otherwise, so live endpoints fail closed. `https://open-api-vst.bingx.pro` is **not** configurable: it exists only as an internal fallback after a network error or timeout, so configuration can never skip the primary host.
- Every order intent carries a unique `clientOrderId`. The order POST itself is **single-attempt** — the transport never retries it, because a 5xx or timeout can still mean the order reached the exchange. On an uncertain outcome the client **never issues a second POST**: it reconciles by `clientOrderId` with a bounded number of lookups, and anything short of a confirmed hit raises for operator intervention. BingX documents `109421` as "may have been filled, cancelled, or order ID is incorrect", so even that code does not prove the intent never landed. A missed entry is recoverable; a duplicate leveraged position is not. Automatic resubmission can be reconsidered once the VST contract is validated against the live venue.
- Margin mode, position mode, and leverage are applied and then **verified** against the venue's effective state at startup. A mismatch, or a value that cannot be read, aborts startup rather than trading with the wrong configuration.
- The `.pro` VST domain is used only after a network error or timeout, never after a valid business/API error.
- AI provider is pluggable via `XAI_API_STYLE` (`responses` / `chat` / `messages`). `config.py` defaults to the `chat` profile (`XAI_MODEL=gpt-5.4`, freemodel.dev); the live `.env` runs `messages` + `deepseek-v4-flash` on openmodel.ai. The `xai_*` names and "Grok" log lines are historical — the brain is provider-agnostic.
- xAI reasoning effort defaults to `high` (`XAI_REASONING_EFFORT`); ignored by providers that don't accept a reasoning field.
- Keep AI requests compact with `XAI_MAX_TOKENS`, `AI_RECENT_PERFORMANCE_LIMIT`, and `AI_TRADE_MEMORY_LIMIT`. This reduces model TPM rate-limit risk without changing the decision strategy.
- Venue safety defaults are isolated margin, hedge position mode, explicit leverage caps, and one open position total.
- `STRATEGY_MODE=ai_volume` lets Grok choose timing, symbol, and direction while execution size follows the volume-builder profile. `STRATEGY_MODE=volume` keeps deterministic rotation, and `STRATEGY_MODE=grok` keeps normal AI risk-sized trading.
- The runtime refreshes exchange taker fees when `USE_EXCHANGE_FEE_RATES=true`, blocks entries when order-book spread/depth is poor, skips entries near funding, and checks liquidation distance after entry. These deterministic guards are independent of the AI and run last.
- The bot learns only from executed demo trades and saved AI decisions, not from fake or replayed fills.
- Backend-only arena features add factor snapshots, edge-triggered market-flow signals, attribution diagnostics, and a local Program Trader dry-run runner without adding UI, accounts, or extra exchanges.

## Token Optimization

The AI pipeline is optimized for token cost without weakening the model, removing
market signals, changing the trading objective, or relaxing any deterministic guard.

- **Compact decision context** (`bot/ai/compact_context.py`). The full market context
  still feeds deterministic execution, factor attribution, reporting, and audit
  storage; only the *AI representation* is compacted. The 86 factors per symbol are
  collapsed into aggregates (category scores, directional agreement, strongest
  positive/negative) plus one small ranked list per symbol, instead of being repeated
  across `factor_snapshots`, `factor_intelligence.snapshots`, `.by_symbol`, and
  `top_factor_details`. Flow signals are capped, and absent providers are omitted rather
  than sent empty. External provider responses go through a **bounded semantic
  projection** that preserves the decision-relevant fields (bias, direction, confidence,
  liquidity, price/volume change) instead of collapsing them to element counts.
  Engineering budgets: two-symbol entry at or below ~4,000 estimated tokens,
  one-symbol exit at or below ~1,500.
- **Merged risk review** (`bot/ai/brain.py`). The separate `PreTradeCheck` provider
  call is gone. Each entry decision now carries a nested `risk_review`
  (`approved`, `confirmations`, `invalidation_trigger`). `approved=false` vetoes that
  candidate; a missing or structurally invalid review makes the candidate **unusable**
  rather than silently approved — the old fail-open path is removed. Risk/reward is
  still computed deterministically from the resolved SL, TP, and fee values.
- **Event-driven AI exits** (`bot/ai/exit_policy.py`). Deterministic price, SL, TP,
  fee, liquidation, and time-stop checks still run on every five-second monitor
  iteration. The AI exit evaluator is only called when a new configured candle has
  closed, price has moved adversely by at least `AI_EXIT_PRICE_MOVE_THRESHOLD_PCT`, or
  a materially new regime/flow trigger appears — subject to a minimum
  `AI_EXIT_CHECK_INTERVAL_SECONDS` gap, a cap of `AI_EXIT_MAX_CALLS_PER_TRADE` calls per
  trade, and a request-hash skip for identical payloads. Gating is two-stage: budget,
  interval, candle close, and adverse move resolve with no network call at all; only when
  those are inconclusive is context built to test for a regime/flow change, and those
  probes are rate-limited by the same interval. When the budget is exhausted or the
  provider is unavailable, deterministic protection stays fully active.
- **Retry policy** (`bot/ai/usage.py`). Only connection failures, timeouts, short
  `429`s, and retryable `5xx` are retried. Authentication, authorization, validation,
  and other permanent `4xx` errors fail immediately. A structurally invalid response
  gets at most **one** repair request containing only the validation problem and the
  compact contract — the same large payload is never resent three times.
- **Usage telemetry.** One `ai_call_metrics` record per provider request: call id,
  request hash, purpose, provider/model/api-style/prompt-version, provider-reported or
  explicitly-estimated tokens, sizes, latency, attempts, error category, dedupe and
  repair flags, and optional estimated cost. Tokens and bytes are **accumulated across
  every attempt** of a request, so a retried or repaired call reports what it actually
  spent rather than only the final attempt.

```bash
PYTHONPATH=src python src/report_ai_usage.py --days 1
PYTHONPATH=src python src/replay_ai_decisions.py --limit 500
```

Reporting is observability-only. Reaching any daily token or cost amount is reported,
never enforced — it will not stop or throttle trading.

## Exchange-Neutral Runtime

`bot/exchange/contracts.py` defines `FuturesExchangeProtocol` and immutable
venue-neutral models (`Instrument`, `OrderBookSnapshot`, `FundingRateInfo`,
`PositionDetails`, `OrderAck`, `OrderFill`, `TradeFeeRates`, `ExchangeAPIError`).
`TradeEngine`, `MarketIntelligence`, leverage resolution, and the realtime backtest
depend on that contract, never on a venue client.

BingX specifics handled by `bot/exchange/bingx_client.py`:

- ASCII-key-ordered canonical parameter string, HMAC-SHA256 lowercase-hex signature,
  millisecond `timestamp` + `recvWindow`, and `X-BX-APIKEY` on signed routes. The
  `X-SOURCE-KEY: BX-AI-SKILL` header is required by BingX on **every** request including
  public market data, so both the client and config validate that exact value and fail
  fast on anything else. Clock-drift responses are reported with an explicit
  "synchronize the system clock" hint.
- HTTP redirects are never followed. Left enabled, the HTTP client would resolve a
  307/308 inside a single request — below this client's own retry and failover control —
  and re-send the body, including an order placement, to whatever host `Location` named.
  Any 3xx is a fail-closed error.
- `.pro` is used **only** after a connection error or timeout. Any HTTP response — success
  or business error — is handled on the host that produced it: documented transient codes
  (`100500`, `110500`, `101214`, `101219`, `109500`) are retried on the same host and then
  raise, and every other `code != 0` raises immediately.
- Hedge mode can report a LONG and a SHORT for one symbol at once. Rather than returning
  whichever row came first — which would make exposure detection depend on response
  ordering — multiple nonzero rows raise, because this runtime's invariant is one open
  position in total.
- Klines are filtered so only candles whose close time has passed are used.
- Hedge mode: closing uses the **opposite order side with the same `positionSide`**, and
  `reduceOnly` is omitted because BingX rejects it for dual-side positions. One-way mode
  does send `reduceOnly=true`, so a close whose quantity exceeds the live position cannot
  flip into a new opposite position.
- Per-route throttling and short-lived market-data caching for rate limits.

## Restart Behavior

Open-position lifecycle state is persisted to Supabase (`open_position_state`). On
restart the engine matches persisted state against the exchange:

- Matching is on **identity, not just symbol**: `positionSide`/direction, quantity (within a
  small partial-fill tolerance), average entry price, and position id must all agree. A
  persisted LONG is never matched to an exchange SHORT, or to a materially different size.
- A confirmed match is **resumed** and fully monitored **on its own persisted SL/TP**, never
  on whatever the configuration says after a redeploy — a running trade keeps the risk
  envelope it was opened under. A persisted record whose envelope is missing, non-positive,
  or non-finite is treated as unknown exposure rather than silently re-priced with current
  settings.
- Anything else — a mismatch, an unreadable position, or exposure with no persisted record —
  is unknown exposure and is **never auto-flattened**. New entries are blocked and the
  operator is alerted, because flattening unknown exposure could close a position this
  process did not open.

## Volume Builder Profile

The included `.env.example` is set up for an AI-guided volume-builder run:

```env
STRATEGY_MODE=ai_volume
ACCOUNT_BALANCE_USDT=1500
POSITION_USDT=180
VOLUME_MARGIN_PER_ENTRY_USDT=180
LEVERAGE=100
TRADE_TARGET_PER_DAY=28
VOLUME_SIDES_PER_ENTRY=2
VOLUME_TARGET_USDT=30000000
VOLUME_DAILY_TARGET_USDT=1000000
VOLUME_EXECUTION_MODE=market
VOLUME_BREAKEVEN_EXIT_ENABLED=false
VOLUME_BREAKEVEN_MIN_PROFIT_USDT=0
VOLUME_STOP_AT_TARGET_ENABLED=true
VOLUME_DAILY_FEE_BUDGET_USDT=600
TRADING_TIMEZONE=Asia/Jakarta
```

That profile projects `180 * 100 * 2 = 36,000 USDT` round-trip volume per
completed entry and `36,000 * 28 = 1,008,000 USDT` per day, so a `30,000,000 USDT`
target is roughly a one-month demo run before partial fills, skipped entries,
fees, and exchange limits. `VOLUME_DAILY_TARGET_USDT=1000000` pauses new volume
entries after the daily minimum is reached for the configured `TRADING_TIMEZONE`,
while `VOLUME_DAILY_FEE_BUDGET_USDT` caps estimated daily taker fee spend. Keep
`VOLUME_BREAKEVEN_EXIT_ENABLED=false`
for the live `ai_volume` profile; enabling it closes winners near breakeven
before the confluence TP can play out. In `ai_volume` mode the AI key is
required because Grok chooses whether to trade; in deterministic `volume` mode
the xAI key is not required.

For high-leverage volume mode, these execution guards are intentionally separate
from AI. Grok can say `OPEN`, but the engine can still skip the trade if the
exchange state is too expensive:

```env
USE_EXCHANGE_FEE_RATES=true
EXECUTION_GUARDS_ENABLED=true
EXECUTION_ENTRY_ORDER_TYPE=market
ORDERBOOK_DEPTH_LEVELS=20
MIN_ORDERBOOK_DEPTH_MULTIPLIER=1.2
MAX_ABS_FUNDING_RATE=0.001
MAX_SPREAD_BPS=6
FUNDING_BLACKOUT_MINUTES=10
MIN_LIQUIDATION_DISTANCE_BPS=10
```

## Backend Arena Core

The `src/bot/arena` package ports the useful backend ideas from Hyper-Alpha-Arena into this bot:

- Factor intelligence exposes 86 built-in factor definitions across momentum, trend, volatility, volume, and microstructure categories from existing exchange candles, order book, funding, and OI data.
- Factor effectiveness scoring includes win rate, average PnL, total PnL, forward-return correlation, IC, ICIR, and a trade-count decay half-life estimate.
- The safe `FactorExpressionEngine` exposes 69 whitelisted functions for custom local factor expressions.
- Market-flow signal detection is edge-triggered for volume, price momentum, OI, funding, liquidity, order-flow imbalance, factor score breakouts, and factor percentile breakouts.
- Arena reviews are injected into Grok payloads through internal services, with backend-only helpers for `HyperAI`, `SignalAI`, `PromptAI`, `ProgramAI`, and `AttributionAI`.
- Program Trader dry-runs execute a restricted local `build_signal(context)` function and must return `NormalizedSignal` or `TradePlan`-shaped data with explicit TP/SL.
- Attribution reports group realized trades by symbol, direction, factor, trigger, hour, exit reason, and confidence band.

Supabase reports:

```powershell
$env:PYTHONPATH="src"
python src/report_arena.py attribution --diagnose
python src/report_arena.py factors
python src/report_arena.py health --target-volume 30000000
python src/report_arena.py config-assist "1,000,000 USDT daily volume with breakeven exits"
python src/report_arena.py program-dry-run path\to\strategy.py --context-json '{"symbol":"BTC/USDT"}'
```

`EXECUTION_ENTRY_ORDER_TYPE=post_only` can be used for maker-first entry tests.
When `EXECUTION_POST_ONLY_FALLBACK_TO_MARKET=false`, an unfilled maker entry is
skipped instead of converted into a taker entry.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill `.env` with:

- `BINGX_API_KEY`
- `BINGX_SECRET_KEY`
- `XAI_API_KEY`
- optional `COINMARKETCAP_API_KEY` and `DUNE_API_KEY`

BingX keys must be VST (virtual) API keys, not live-trading keys. BingX does not use an OKX-style passphrase. Keep `.env` out of git.

## Dune Signals

If `DUNE_API_KEY` is filled and `DUNE_USE_BUILTIN_SIGNALS=true`, the bot runs these built-in SQL signals automatically:

- `eth_dex_volume_regime`: ETH/WETH DEX volume over the last hour.
- `stablecoin_liquidity_flow`: USDT/USDC transfer volume over the last hour.
- `large_eth_transfer_activity`: ETH/WETH transfers of at least $1M over the last hour.

You can still add custom saved Dune queries later:

```env
DUNE_QUERY_IDS=1234567,9876543
```

When `DUNE_QUERY_IDS` is empty, the built-in signals still run.

`EXTERNAL_SIGNAL_CACHE_SECONDS=300` keeps Dune/CMC/TradingView refreshes to at most once every five minutes so Dune credits are not burned during position monitoring.

## TradingView Relay

Run the local relay in a second terminal:

```powershell
$env:PYTHONPATH="src"
python src/tradingview_relay.py
```

The bot reads latest TradingView signals from:

```env
TRADINGVIEW_SIGNAL_URL=http://127.0.0.1:8787/tradingview/latest
```

TradingView alerts should POST JSON to the webhook URL in `.env`:

```env
TRADINGVIEW_WEBHOOK_URL=http://127.0.0.1:8787/tradingview/webhook?token=...
```

For a real TradingView cloud alert, expose that relay with a tunnel or deploy the relay on a small server, then replace `127.0.0.1` with the public HTTPS URL.

## Supabase

Supabase is required. The bot stores closed demo trades and AI entry decisions in Supabase:

```env
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key
SUPABASE_SCHEMA=public
SUPABASE_TRADES_TABLE=trades
SUPABASE_AI_DECISIONS_TABLE=ai_decisions
```

Create or update the tables by running [supabase/schema.sql](supabase/schema.sql) in the Supabase SQL Editor. It includes arena metadata columns on `trades` and an optional `arena_diagnostics` table. Use the service role key only on your local machine or a private server; do not expose it in a browser or client app.

## Run

```powershell
$env:PYTHONPATH="src"
python src/main.py
```

The engine will:

1. Validate BingX VST API access.
2. Refresh BingX commission rates where available.
3. Set margin mode, position mode, and leverage where BingX allows it.
4. Gather live BingX data plus configured CMC/Dune/TradingView signals.
5. Produce an entry or hold decision through Grok (`grok`/`ai_volume`) or deterministic rotation (`volume`).
6. Run execution guards for spread, depth, funding, and liquidation distance.
7. Execute BingX VST market entries by default, or post-only (`LIMIT` + `timeInForce=PostOnly`) entries when configured.
8. Monitor the open position with live prices, TP/SL, AI exits, and risk guards.
9. Save executed outcomes for future decision context.

## Live Dashboard

A read-only live view of the bot: decisions, orders, fills, the open position and its PnL, on a
chart of real prices. The bot publishes the feed; the screen ([dashboard/](dashboard/)) runs on
your laptop. It is **off by default** and changes nothing about trading while off.

```
bot (VPS, loopback :TELEMETRY_PORT)  --ssh -L-->  laptop :3000  <--  dashboard (laptop :3001)
```

`TELEMETRY_PORT` defaults to 3000, but that port may already belong to another app on the VPS.
The live VPS shares its host with several apps and uses **3917**; the examples below write it as
`<port>`.

**What is exposed.** Only market data the bot already reads and its own trade events: no keys,
no wallet, no account balance. The listener answers `GET` only (`/`, `/snapshot`, `/history`,
`/tape`, `/events`), accepts only a loopback `Host` header, allows exactly one CORS origin, and
refuses to start on any non-loopback `TELEMETRY_HOST`. Nothing is opened to the internet and no
firewall rule is needed.

### Turn it on (VPS)

1. **Flatten first.** Enabling it restarts `ai-perp-volume`, and a restart with an open
   position triggers the `set_margin_mode` crash loop (see CLAUDE.md). Wait until the venue is
   flat, or flatten it yourself.
2. **Pick a free port.** The feed cannot bind a port another process already holds (the bot
   would log a warning and keep trading, but there would be no dashboard). Check, and choose a
   free one if the default is taken:

   ```bash
   ss -ltn | awk 'NR>1 {print $4}' | grep -E ':3000$' || echo "3000 free"
   ```

3. Add to the VPS `.env` (the other `TELEMETRY_*` keys have safe defaults). Omit the second
   line only if 3000 is free:

   ```bash
   TELEMETRY_ENABLED=true
   TELEMETRY_PORT=<port>
   ```

4. Restart and confirm the listener came up:

   ```bash
   pm2 restart ai-perp-volume --update-env
   grep "dashboard feed listening" ~/.pm2/logs/ai-perp-volume-error.log | tail -1
   curl -s http://127.0.0.1:<port>/ | head -c 200
   ```

   The bot logs to stderr, so the line is in pm2's error log. The `curl` returns the meta JSON
   (`"venue":"bingx"`, your symbols).

### Watch it (laptop)

```bash
ssh -L 3000:localhost:<port> 0xAlvary2     # keep this open; <port> is the VPS TELEMETRY_PORT
cd dashboard && bun install && bun run dev
```

Open <http://localhost:3001>. The left side of `-L` is the port on your laptop; the dashboard
reads `NEXT_PUBLIC_API_URL` (default `http://localhost:3000`, see `dashboard/.env.example`), so
keep it at 3000 unless you change that variable too. If you open it from another origin,
set `TELEMETRY_ALLOWED_ORIGIN` on the bot to that exact origin (`http://host:port`, no path,
no wildcard). The `Host` check ignores the port, so any local tunnel port works.

### Smoother prices (optional websocket)

By default the chart price comes from one order-book read every `TELEMETRY_PRICE_INTERVAL_SECONDS`
(5 s). Set `TELEMETRY_PRICE_STREAM=true` to read it from BingX's public VST market websocket
instead, about every 0.4 s. It subscribes only to `<PAIR>@bookTicker` on
`wss://vst-open-api-ws.bingx.com/swap-market` (a fixed host, no keys, no account stream), and
polling keeps running underneath: if the stream goes quiet for 3 s the chart falls back to the
5 s poll by itself. It changes the dashboard only; the engine never reads it. Like any `.env`
change it needs a restart, so apply it with a flat position.

### Show a symbol the bot does not trade (optional)

The dashboard shows one card per traded symbol (`SYMBOLS`). To watch another market without
trading it, list it in `TELEMETRY_SYMBOLS`, for example `TELEMETRY_SYMBOLS=BTC/USDT,ETH/USDT`
while `SYMBOLS=BTC/USDT`. The extra symbol gets price and candles only: no AI decision, no
new fills, always flat (closed trades of that coin still inside the seeded history window since
`VOLUME_CYCLE_STARTED_AT` may appear on its History and Trades tabs). The traded `SYMBOLS` are always shown first even if you leave them out, and
nothing that trades reads this list. Like any `.env` change it needs a restart with a flat
position.

### Share it with one other person (VPS, behind a login)

The feed has no login of its own, so it must never be reachable from the internet directly. To
let a second person watch, run the screen on the VPS and put an identity-aware proxy (for
example Cloudflare Tunnel + Access, with an "allow these emails" policy) in front of it:

```
browser --> Cloudflare Access (email login) --> tunnel --> next start 127.0.0.1:3001
                                                            `-- /feed/* --> 127.0.0.1:<port>
```

- One hostname serves both the screen and its data: the browser calls `/feed/...` on the same
  origin and Next forwards it to the loopback feed. Build with
  `FEED_PROXY_TARGET=http://127.0.0.1:<port> NEXT_PUBLIC_API_URL=/feed bun run build`. Both are
  read at **build** time, so rebuild after changing either. Without `FEED_PROXY_TARGET` the
  rewrite does not exist and the laptop workflow above is unchanged.
- Start it with `next start -H 127.0.0.1 -p 3001`. Binding to loopback is what stops anyone
  from skipping the login by opening `<vps-ip>:3001`; never start it on `0.0.0.0`.
- Turn the Access policy on **before** the hostname resolves, or the screen is briefly public.
- `compress: false` in `dashboard/next.config.ts` is deliberate: gzip buffers the SSE stream.

### Turn it off

Set `TELEMETRY_ENABLED=false` (or delete the line) and restart, again with a flat position.

### What it shows, and what it does not

- Each strategy tick that reaches the model: `HOLD`, or `OPEN LONG/SHORT` with the model's
  confidence and reason. Ticks stopped earlier (risk guard, cooldowns, daily target) are not
  shown; the log still says why.
- Entry quote and fill, the open position with unrealized PnL (updated at most every 5 s), and
  the close with its reason, PnL and fees.
- **Not shown or approximated:** account balance (not read), AI cost (shows 0), per-action
  probabilities (the bot has none: the bar is the model's confidence). Unrealized PnL is
  **after** estimated round-trip taker fees, so it reads lower than the BingX screen, which is
  before fees.
- History lives in memory. A restart empties the decision list; the chart refills from BingX
  candles, and a resumed position reappears as soon as the first price arrives.
- Extra load when on: one public order-book request per symbol every 5 s and two public kline
  requests per symbol every 60 s. It never calls an order, account or position endpoint.

### If something looks wrong

| Symptom | Likely cause |
|---|---|
| Dashboard says Offline | The tunnel is closed, or the bot is down: `curl http://127.0.0.1:<port>/` on the VPS. |
| Browser console shows a CORS error | `TELEMETRY_ALLOWED_ORIGIN` does not match the page's origin. |
| Log: `Telemetry server failed to start; trading continues` | The port is taken by another process (check with `ss -ltn`, then set a free `TELEMETRY_PORT` and restart when flat). Trading is unaffected. |
| Prices frozen, log: `Telemetry poller stopped unexpectedly` | The poller crashed; trades keep recording. Restart when flat. |
| Log: `telemetry ... failed` warnings | A dashboard hook failed and was contained; trading is unaffected. Report the traceback. |
| Another process already uses :3000 or :3001 on your laptop | Pick other local ports (`ssh -L 3010:localhost:<port>`, `bunx next dev -p 3011` with `NEXT_PUBLIC_API_URL=http://localhost:3010`) and set `TELEMETRY_ALLOWED_ORIGIN=http://localhost:3011` on the bot. |

The dashboard's chart, feed and state code is adapted from MIT-licensed projects; see
[dashboard/THIRD_PARTY_NOTICES.md](dashboard/THIRD_PARTY_NOTICES.md).

## Reporting

```powershell
$env:PYTHONPATH="src"
python src/report_ai_decisions.py --limit 1000
```

This summarizes Supabase trades by AI entry reason. For arena attribution, use `src/report_arena.py`.
For AI token spend by purpose, retry/error rates, and dedupe savings, use
`src/report_ai_usage.py`; for deduplicated historical decision replay, use
`src/replay_ai_decisions.py`.

Realtime dry-run backtest without placing orders:

```powershell
$env:PYTHONPATH="src"
python src/realtime_backtest.py --source auto --no-trades
```

`--source auto` tries BingX public candles/orderbook/funding first and falls back
to CoinGecko price candles if BingX is unreachable from the local machine. Fallback
results are useful for rough PnL/volume math, but they do not include real venue
book depth, funding, or fill behavior.

## Notes

- Internal symbols such as `BTC/USDT` are mapped to BingX symbols such as `BTC-USDT`.
- `POSITION_USDT * LEVERAGE` defines target notional. BingX perpetual order quantity is **base-asset quantity**, not a contract count; the client rounds it with `quantityPrecision` and validates `tradeMinQuantity` / `tradeMinUSDT` before placing an order. That venue metadata is the only minimum-size authority — there is no contract-count floor (the legacy `MIN_ORDER_CONTRACTS=1` silently blocked every sub-1-BTC entry and has been removed).
- Trading is risky even in demo mode. Validate order sizing, contract metadata, and account position mode before leaving the bot unattended.
