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
- A confirmed match is **resumed** and fully monitored.
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
