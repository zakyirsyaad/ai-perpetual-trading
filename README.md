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
```

## Live Signal Sources

- OKX public/private API: execution source of truth, candles, ticker, funding rate, open interest, positions, leverage, and market orders.
- CoinMarketCap: latest quotes via `/v2/cryptocurrency/quotes/latest` when `COINMARKETCAP_API_KEY` is configured.
- Dune Analytics: runs built-in SQL signals via `/api/v1/sql/execute` when `DUNE_API_KEY` is configured. Saved query IDs in `DUNE_QUERY_IDS` are optional.
- TradingView: uses the included local relay at `src/tradingview_relay.py`. TradingView does not provide a general public market-data API for bots, so alerts must be pushed into the relay.
- Supabase: optional mirror for executed trades and AI decisions when Supabase env vars are configured. SQLite remains the local source for the bot loop.

## Safety Defaults

- `OKX_DEMO_TRADING=true` is required. The bot refuses to start if this is false.
- OKX demo requests include the `x-simulated-trading: 1` header.
- xAI defaults to `grok-4.3`.
- The bot learns only from executed demo trades and saved AI decisions, not from fake or replayed fills.

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
2. Set account position mode and leverage where OKX allows it.
3. Gather live OKX data plus configured CMC/Dune/TradingView signals.
4. Ask Grok for a structured entry or hold decision.
5. Execute only OKX demo market orders.
6. Monitor the open position with live prices, TP/SL, AI exits, and risk guards.
7. Save executed outcomes for future decision context.

## Reporting

```powershell
$env:PYTHONPATH="src"
python src/report_ai_decisions.py --db "bot_data/trades.db" --limit 20
```

This summarizes real executed demo trades by AI entry reason.

## Notes

- For OKX SWAP instruments, internal symbols such as `BTC/USDT` are mapped to OKX instrument IDs such as `BTC-USDT-SWAP`.
- `POSITION_USDT * LEVERAGE` defines target notional. The OKX client converts base quantity into contract size using OKX instrument metadata.
- Trading is risky even in demo mode. Validate order sizing, contract metadata, and account position mode before leaving the bot unattended.
