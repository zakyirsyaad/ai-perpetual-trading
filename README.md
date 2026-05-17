# OKX Demo AI Futures Bot

Async Python trading bot for OKX demo futures. The runtime is intentionally live-data only: it does not include paper trading, offline CSV backtests, mock exchanges, or simulated orders.

## Architecture

```text
src/
  main.py
  bot/
    ai/
      brain.py              # Layer 1: Grok decision brain
      orchestration.py      # Layer 2: risk, learning, and execution orchestration
    data/
      providers.py          # Layer 3: OKX-adjacent external signals
      market_signals.py
    intelligence/
      market_intelligence.py # Layer 4: market intelligence synthesis
    exchange/
      okx_client.py
    core/
      trade_engine.py
    risk/
      risk_manager.py
    storage/
      trade_store.py
      supabase_journal.py
    strategy/
      grok_strategy.py
      indicators.py
    perps/
      exchange_adapter.py   # Venue-neutral perpetual futures boundary
      execution.py          # Maker-first entry and reduce-only exit state machine
      risk.py               # Fee/slippage-aware sizing and kill-switch checks
      backtester.py         # Conservative event-driven/hybrid backtester
```

## Live Signal Sources

- OKX public/private API: execution source of truth, candles, ticker, order book, account fee rate, funding rate, open interest, positions, leverage, and orders.
- CoinMarketCap: latest quotes via `/v2/cryptocurrency/quotes/latest` when `COINMARKETCAP_API_KEY` is configured.
- Dune Analytics: runs built-in SQL signals via `/api/v1/sql/execute` when `DUNE_API_KEY` is configured. Saved query IDs in `DUNE_QUERY_IDS` are optional.
- TradingView: uses the included local relay at `src/tradingview_relay.py`. TradingView does not provide a general public market-data API for bots, so alerts must be pushed into the relay.
- Supabase: optional mirror for executed trades and AI decisions when Supabase env vars are configured. SQLite remains the local source for the bot loop.

## Safety Defaults

- `OKX_DEMO_TRADING=true` is required. The bot refuses to start if this is false.
- OKX demo requests include the `x-simulated-trading: 1` header.
- xAI defaults to `grok-4.3`.
- xAI reasoning defaults to `high` for the Grok 4.3 High strategy profile.
- Perpetual futures safety defaults are isolated margin, 3x leverage cap, 5x hard max, and one net open position total.
- `STRATEGY_MODE=ai_volume` lets Grok choose timing, symbol, and direction while execution size follows the volume-builder profile. `STRATEGY_MODE=volume` keeps deterministic rotation, and `STRATEGY_MODE=grok` keeps normal AI risk-sized trading.
- The OKX runtime refreshes exchange taker fees when `USE_EXCHANGE_FEE_RATES=true`, blocks entries when order-book spread/depth is poor, skips entries near funding, and checks liquidation distance after entry.
- The bot learns only from executed demo trades and saved AI decisions, not from fake or replayed fills.

## Perpetual Futures Layer

The exchange-agnostic perpetual futures layer lives in `src/bot/perps`. It keeps venue REST, WebSocket, auth, and instrument metadata behind `ExchangeAdapter`, with `MockExchangeAdapter` available for tests.

See [docs/perps.md](docs/perps.md) and [config/perps.safe-defaults.json](config/perps.safe-defaults.json) for assumptions, sizing math, kill-switch defaults, backtest behavior, and venue-specific TODOs.

## Volume Builder Profile

The included `.env.example` is set up for an AI-guided volume-builder run:

```env
STRATEGY_MODE=ai_volume
ACCOUNT_BALANCE_USDT=1500
POSITION_USDT=180
VOLUME_MARGIN_PER_ENTRY_USDT=180
LEVERAGE=100
TRADE_TARGET_PER_DAY=16
VOLUME_SIDES_PER_ENTRY=2
VOLUME_TARGET_USDT=10000000
```

That profile projects `180 * 100 * 2 = 36,000 USDT` round-trip volume per
completed entry and `36,000 * 16 = 576,000 USDT` per day, so a `10,000,000 USDT`
target takes about `18` conservative trading days before partial fills, skipped
entries, fees, and exchange limits. `VOLUME_NEUTRAL_EXIT_ENABLED=true` lets the
monitor close when estimated net PnL after fee is inside the configured positive
neutral band, for example `VOLUME_NEUTRAL_EXIT_MIN_NET_USDT=12` through `17`
when `VOLUME_NEUTRAL_EXIT_BAND_USDT=5`. In `ai_volume` mode the xAI key is
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
PERP_MAX_SPREAD_BPS=6
PERP_FUNDING_BLACKOUT_MINUTES=10
PERP_MIN_LIQUIDATION_DISTANCE_BPS=10
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

- `OKX_API_KEY`
- `OKX_SECRET_KEY`
- `OKX_PASSPHRASE`
- `XAI_API_KEY`
- optional `COINMARKETCAP_API_KEY` and `DUNE_API_KEY`

OKX demo keys must be created from OKX Demo Trading, not production trading. Keep `.env` out of git.

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

Supabase is optional. When these env vars are filled, the bot mirrors closed demo trades and AI entry decisions to Supabase:

```env
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key
SUPABASE_SCHEMA=public
SUPABASE_TRADES_TABLE=trades
SUPABASE_AI_DECISIONS_TABLE=ai_decisions
```

Create the tables by running [supabase/schema.sql](supabase/schema.sql) in the Supabase SQL Editor. Use the service role key only on your local machine or a private server; do not expose it in a browser or client app.

## Run

```powershell
$env:PYTHONPATH="src"
python src/main.py
```

The engine will:

1. Validate OKX demo API access.
2. Refresh OKX account fee rates where available.
3. Set account position mode and leverage where OKX allows it.
4. Gather live OKX data plus configured CMC/Dune/TradingView signals.
5. Ask Grok for a structured entry or hold decision when `STRATEGY_MODE=grok` or `STRATEGY_MODE=ai_volume`, or rotate deterministic volume-builder entries when `STRATEGY_MODE=volume`.
6. Run execution guards for spread, depth, funding, and liquidation distance.
7. Execute OKX demo market entries by default, or post-only entries when configured.
8. Monitor the open position with live prices, TP/SL, AI exits, and risk guards.
9. Save executed outcomes for future decision context.

## Reporting

```powershell
$env:PYTHONPATH="src"
python src/report_ai_decisions.py --db "bot_data/trades.db" --limit 20
```

This summarizes real executed demo trades by AI entry reason.

## Notes

- For OKX SWAP instruments, internal symbols such as `BTC/USDT` are mapped to OKX instrument IDs such as `BTC-USDT-SWAP`.
- `POSITION_USDT * LEVERAGE` defines target notional. The OKX client converts base quantity into contract size using OKX instrument metadata.
- The current runtime is OKX-first. BingX credentials can stay empty until you receive the API key; when switching later, implement the same execution surface now used by the OKX client before pointing `main.py` at BingX.
- Trading is risky even in demo mode. Validate order sizing, contract metadata, and account position mode before leaving the bot unattended.
