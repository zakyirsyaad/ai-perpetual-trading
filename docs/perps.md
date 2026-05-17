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
- Volume-builder profile can be made explicit with margin per entry, leverage, sides per entry, daily entries, and target cumulative volume.
- Exact exchange API is intentionally unset in core config. Venue REST, WebSocket, auth, and metadata code belongs behind `ExchangeAdapter`.
- Maximum venue leverage is intentionally unset until venue maintenance-margin tiers are known.
- Acceptable maximum drawdown is explicit in config via daily and rolling drawdown percentages and should be reviewed before live use.
- The current app runtime is OKX-first. BingX can be added later by implementing the same execution methods already used by `TradeEngine` instead of changing AI strategy logic.

## Hard Constraints Implemented

- Isolated margin only. `OKX_MARGIN_MODE=cross` is rejected by settings validation.
- Default leverage is `3x`; safe default hard max is `5x`. Higher leverage must be configured explicitly and reviewed against venue tiers.
- One net open position total by default.
- Entries are post-only limit orders through the execution manager.
- Exits attached by the execution manager are reduce-only. Emergency market close is reduce-only and reserved for hard-stop/desync handling.
- Instrument metadata drives tick rounding, quantity step, minimum quantity, minimum notional, leverage cap, margin mode, and maintenance-margin assumptions.
- New entries are rejected when expected gross move is not greater than fees + slippage + edge buffer.
- Funding blackout, stale data, websocket health, rate-limit, drawdown, consecutive-loss, fee-budget, and desync guards block new entries.

## OKX Runtime Execution Guards

The current `src/main.py` runtime still instantiates `OKXFuturesClient`, but it
now has exchange-state gates before any volume entry is placed:

- `USE_EXCHANGE_FEE_RATES=true`: fetch account fee rates from OKX and use the exchange taker fee for PnL/neutral-exit estimates when available.
- `EXECUTION_GUARDS_ENABLED=true`: fail closed when order-book or funding guard data cannot be read.
- `PERP_MAX_SPREAD_BPS`: reject entry when best bid/ask spread is too wide.
- `ORDERBOOK_DEPTH_LEVELS` and `MIN_ORDERBOOK_DEPTH_MULTIPLIER`: require enough executable book depth for the target notional.
- `MAX_ABS_FUNDING_RATE`: reject entry when current funding is unusually large.
- `PERP_FUNDING_BLACKOUT_MINUTES`: reject new entries close to the next funding timestamp.
- `PERP_MIN_LIQUIDATION_DISTANCE_BPS`: close immediately after entry if OKX reports liquidation too close, or if planned stop is beyond liquidation.

`EXECUTION_ENTRY_ORDER_TYPE=market` is the compatible default. For maker-first
testing, set `EXECUTION_ENTRY_ORDER_TYPE=post_only`; unfilled post-only entries
are skipped unless `EXECUTION_POST_ONLY_FALLBACK_TO_MARKET=true`.

## Sizing Defaults

The default `risk` sizing mode implements:

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

## Volume-Builder Sizing

`PERP_SIZING_MODE=volume` switches the execution-facing sizing source from
fee/risk caps to configured margin per entry:

```text
notional_per_side = target_margin_per_entry_usdt * leverage
volume_per_entry  = notional_per_side * volume_sides_per_entry
daily_volume      = volume_per_entry * target_trades_per_day
target_days       = ceil(target_volume_usdt / daily_volume)
```

For the profile below:

- account equity: `1500 USDT`
- margin per entry: `180 USDT`
- leverage: `100x`
- sides per entry: `2` (entry + exit volume)
- target entries per day: `16`
- target volume: `10,000,000 USDT`

The projected notional is `18,000 USDT` per side, `36,000 USDT`
round-trip volume per completed entry, and `576,000 USDT` projected daily
volume. The conservative target window is `18` days.

Volume mode still keeps exchange minimums, isolated margin, leverage caps,
spread/funding/health guards, position reconciliation, and liquidation-distance
checks. If using very high leverage, set venue leverage and liquidation guard
values only after confirming the exchange's instrument tiers.

For AI-guided volume building, use `STRATEGY_MODE=ai_volume`. In that mode Grok
selects the symbol, direction, and timing, while execution still uses the volume
profile for margin/leverage sizing. Deterministic `STRATEGY_MODE=volume` should
be treated as a rotation/volume test mode, not an edge-seeking trading strategy.

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
`default_leverage`, `daily_fee_budget_pct`, `sizing_mode`, and
`target_margin_per_entry_usdt`.

## Venue TODOs

- Implement a concrete adapter for the chosen exchange API.
- Map venue-specific instrument IDs, contract value, tick size, lot size, min notional, and leverage tiers.
- Wire venue websocket freshness and rate-limit telemetry into `HealthStatus`.
- Confirm maintenance-margin tiers before raising leverage above the default `3x`.
- Confirm funding schedule/rate handling for the venue.
- Decide operator-approved daily and rolling maximum drawdown before unattended live trading.
