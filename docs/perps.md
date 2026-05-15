# Exchange-Agnostic Perpetual Futures Layer

This repo now has a venue-neutral perpetual futures layer under `src/bot/perps`.
It does not replace the existing signal-generation entry points. The current
Grok/xAI wiring can continue to produce decisions, and execution-facing signals
must be normalized into `NormalizedSignal` before any order flow.

## Explicit Assumptions

- Strategy label: `Grok 4.3 High`, represented by xAI model `grok-4.3` and `reasoning.effort="high"`.
- Research equity: `60 USDT`.
- Research fee model: `0.5%` round-trip per completed trade, configurable as `PERP_FEE_ROUND_TRIP=0.005`.
- Research slippage model: `0.04%` round-trip, configurable as `PERP_SLIPPAGE_ROUND_TRIP=0.0004`.
- Target operating profile: about `20` completed trades per day across BTC/USDT and ETH/USDT when volatility and edge filters allow.
- Exact exchange API is intentionally unset in core config. Venue REST, WebSocket, auth, and metadata code belongs behind `ExchangeAdapter`.
- Maximum venue leverage is intentionally unset until venue maintenance-margin tiers are known.
- Acceptable maximum drawdown is explicit in config via daily and rolling drawdown percentages and should be reviewed before live use.

## Hard Constraints Implemented

- Isolated margin only. `OKX_MARGIN_MODE=cross` is rejected by settings validation.
- Default leverage is `3x`; hard max is `5x`.
- One net open position total by default.
- Entries are post-only limit orders through the execution manager.
- Exits attached by the execution manager are reduce-only. Emergency market close is reduce-only and reserved for hard-stop/desync handling.
- Instrument metadata drives tick rounding, quantity step, minimum quantity, minimum notional, leverage cap, margin mode, and maintenance-margin assumptions.
- New entries are rejected when expected gross move is not greater than fees + slippage + edge buffer.
- Funding blackout, stale data, websocket health, rate-limit, drawdown, consecutive-loss, fee-budget, and desync guards block new entries.

## Sizing Defaults

The sizing module implements:

```text
N_fee  = (equity * daily_fee_budget_pct) / (target_trades_per_day * fee_round_trip)
N_risk = (equity * risk_per_trade_pct) / (stop_distance + fee_round_trip + slippage_round_trip)
N_final = min(N_fee, N_risk, N_liq, N_exchange_limits)
```

Default values:

- `daily_fee_budget_pct = 0.03`
- `risk_per_trade_pct = 0.0075`
- `fee_round_trip = 0.005`
- `slippage_round_trip = 0.0004`
- `edge_buffer_bps = 5`
- `target_trades_per_day = 20`
- `default_leverage = 3`
- `hard_max_leverage = 5`

For `60 USDT` equity and the default fee budget, `N_fee` is `18 USDT`.
The final order notional is rounded down to the venue quantity step and checked
against exchange minimum quantity/notional.

## Backtester

`ConservativePerpsBacktester` is event-driven around signal timestamps and
conservative inside candles:

- maker entry at bid/ask, never free candle-close fills
- open and close fees
- funding cost when a trade spans a funding timestamp
- slippage on entry and exit
- partial fill and timeout assumptions
- spread filter
- mark-price liquidation-distance guard through the shared sizing module
- worst-case intra-candle rule: stop-loss wins when both TP and SL are inside one candle

Metrics include net PnL after costs, win rate, average net win/loss, expectancy,
profit factor, max drawdown, fee as percent of gross PnL, fill ratio, slippage
distribution, and PnL by symbol/timeframe.

Parameter sweep hooks accept keys such as `confidence_threshold`,
`expected_move_bps_min`, `stop_bps`, `take_bps`, `cooldown_seconds`,
`max_spread_bps`, `entry_timeout_seconds`, `max_reprice_count`,
`default_leverage`, and `daily_fee_budget_pct`.

## Venue TODOs

- Implement a concrete adapter for the chosen exchange API.
- Map venue-specific instrument IDs, contract value, tick size, lot size, min notional, and leverage tiers.
- Wire venue websocket freshness and rate-limit telemetry into `HealthStatus`.
- Confirm maintenance-margin tiers before raising leverage above the default `3x`.
- Confirm funding schedule/rate handling for the venue.
- Decide operator-approved daily and rolling maximum drawdown before unattended live trading.
