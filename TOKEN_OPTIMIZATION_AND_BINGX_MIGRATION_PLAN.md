# Token Optimization and BingX VST Migration Plan

Date: 2026-08-22
Status: Approved implementation plan
Runtime target: BingX Perpetual Futures VST only

## 1. Objective

Optimize the AI trading pipeline so it preserves the current `ai_volume` strategy, decision quality, sizing, risk behavior, and execution safeguards while substantially reducing token consumption. In parallel, replace the OKX integration completely with BingX Perpetual Futures VST.

This work must not improve cost by silently weakening the model, removing market signals, changing the trading objective, or relaxing existing deterministic safeguards. The primary optimization levers are:

- Remove duplicated context sent to the AI.
- Call the AI only when a new decision is required.
- Merge the secondary AI risk review into the main entry decision.
- Stop retrying permanent or identical failures unnecessarily.
- Measure actual token usage per call and per day.

## 2. Current Baseline and Root Causes

The existing test baseline is 63 passing unit tests.

Representative local payload measurements show:

- A two-symbol entry context is approximately 114,000-139,000 serialized characters, or roughly 28,000-35,000 input tokens before all prompt, schema, and history overhead.
- A one-symbol exit context is approximately 67,000 characters, or roughly 17,000 input tokens.
- The runtime calculates 86 factors per symbol. For BTC and ETH this creates 172 snapshots, which are repeated through `factor_intelligence.snapshots`, `factor_intelligence.by_symbol`, `top_factor_details`, top-level `factor_snapshots`, flow payloads, and arena reviews.
- AI exit evaluation currently runs from the five-second position-monitor loop. A one-hour position can therefore trigger up to 720 large AI calls.
- `AI_EXIT_CHECK_INTERVAL_SECONDS` and `AI_EXIT_PRICE_MOVE_THRESHOLD_PCT` already exist in configuration but are not used by the execution path.
- An accepted entry can trigger a second `PreTradeCheck` AI request. It sees the planned trade and sizing but not the complete live market context. If the request fails or is invalid, it fails open and the trade continues.
- Chat and messages providers receive the complete JSON Schema as prompt text on every request.
- Invalid or failed requests can resend the same full payload up to three times.
- Provider token usage, latency, retry cost, and cache usage are not currently recorded.

The current model/provider is already inexpensive and produces satisfactory results. Model replacement or multi-model routing is therefore outside the initial optimization phase.

## 3. Guardrails: Preserve the Current Strategy

The implementation must preserve the current strategy and deterministic safety behavior:

- Keep `STRATEGY_MODE=ai_volume` semantics.
- Keep the existing model, provider, entry objective, candidate scoring, volume sizing, configured leverage, and SL/TP resolution.
- Keep daily risk and drawdown checks, cooldowns, trade/volume targets, and the single-position policy.
- Keep spread, order-book depth, funding, leverage, liquidation-distance, fee, TP, SL, and time-stop checks.
- Continue checking price and deterministic exit conditions every five seconds.
- Do not reactivate confidence, systemic-risk, or risk/reward gates that were intentionally removed from the current volume-building candidate path.
- Do not automatically alter the strategy based on VST results. Report the evidence first and tune only through a separate approved change.

## 4. Compact AI Decision Context

Add a dedicated `CompactDecisionContextBuilder`. The full market context remains available to deterministic execution, factor attribution, reporting, and audit storage; only the AI representation is compacted.

The compact entry payload must include:

- One rounded technical summary per configured symbol.
- Aggregates from all locally computed factors, including category scores, directional agreement, and strongest positive/negative factors.
- A small ranked factor list per symbol containing only the fields the model uses: name, category, score, direction, and percentile.
- At most five active or newly triggered flow signals.
- Market regime, risk flags, and symbol composite scores.
- Only available and fresh external signals, represented as compact summaries rather than raw provider responses.
- Compact recent performance totals and trade-memory lessons, limited to the most useful winning and losing patterns.
- Existing risk state, strategy objective, fee assumptions, and allowed symbols.

The compact payload must not contain repeated copies of the same factor snapshots or flow structures. JSON must be serialized compactly and stable key ordering should be used when calculating request hashes.

Engineering regression budgets:

- Two-symbol entry input should remain around or below 4,000 estimated tokens.
- One-symbol exit input should remain around or below 1,500 estimated tokens.

These are per-request payload budgets, not a required daily saving percentage. Actual savings will be measured and reported.

## 5. Entry Decision and Risk Review

Merge the current secondary `PreTradeCheck` into the main structured entry response.

Each per-symbol entry decision must retain the fields required by the current runtime and include a nested risk review:

```json
{
  "symbol": "BTC/USDT",
  "action": "OPEN_LONG",
  "confidence": 0.82,
  "market_trend": "Bullish",
  "stop_loss_pct": 0.08,
  "take_profit_pct": 0.12,
  "systemic_risk": 2,
  "reason": "Concise decision reason",
  "risk_review": {
    "approved": true,
    "confirmations": "Concise confirmation summary",
    "invalidation_trigger": "Concise invalidation condition"
  }
}
```

Behavior:

- `risk_review.approved=false` vetoes that candidate.
- A missing or structurally invalid risk review makes the candidate unusable rather than silently inventing approval.
- Risk/reward is calculated deterministically from the resolved SL, TP, and fee values; the model does not need to provide a separate authoritative ratio.
- Existing execution guards still run after AI approval and remain the final mandatory checks.
- The separate pre-trade AI call and its fail-open path are removed.

## 6. Event-Driven AI Exit Policy

Keep deterministic price, SL, TP, fee, liquidation, and time-stop evaluation on every five-second monitor iteration.

Call the AI exit evaluator only when at least one event occurs:

- A new configured three-minute candle has closed.
- Price has moved adversely by at least `AI_EXIT_PRICE_MOVE_THRESHOLD_PCT` since the last AI evaluation.
- A materially new regime or flow trigger is detected.

Additional controls:

- Enforce at least `AI_EXIT_CHECK_INTERVAL_SECONDS` between AI exit calls, with 30 seconds as the current default.
- Allow no more than 12 AI exit calls during one trade.
- Hash the compact exit payload and skip an identical request.
- If the exit budget is exhausted or the provider is unavailable, deterministic protection remains active and the position continues under the current SL/TP/time rules.

## 7. AI Usage Telemetry and Retry Policy

Introduce provider-neutral AI call metadata and persist one record per provider request.

Required fields:

- Call ID and request hash.
- Purpose: entry, exit, arena review, or repair.
- Provider, model, API style, prompt version, and reasoning setting.
- Input, output, reasoning, cached, and total tokens when exposed by the provider.
- Estimated token counts when provider counts are unavailable, explicitly marked as estimated.
- Request and response size, latency, attempt count, success status, and error category.
- Optional estimated monetary cost when reliable pricing is configured.

Retry policy:

- Retry only connection failures, timeouts, short `429` responses, and retryable `5xx` failures.
- Fail immediately on authentication, authorization, validation, and other permanent `4xx` errors.
- A structurally invalid response receives at most one repair request containing only the validation problem and the compact contract.
- Never resend the same large request three times without changing the corrective input.

Add a reporting command that summarizes daily calls and tokens by purpose, retry/error rates, provider-reported versus estimated usage, and deduplication savings. Reporting is observability-only and must not stop trading when a daily amount is reached.

## 8. Exchange-Neutral Runtime Contract

Before implementing BingX, introduce a `FuturesExchangeProtocol` and immutable venue-neutral data models for:

- Instrument metadata.
- Order book.
- Funding-rate information.
- Position details.
- Order acknowledgement and fill details.
- Maker/taker fee rates.
- Exchange API errors.

`TradeEngine`, `MarketIntelligence`, leverage resolution, and realtime backtesting must depend on this contract rather than importing OKX classes.

The protocol must cover the capabilities currently required by the runtime:

- API-access validation and lifecycle close.
- Last price, klines, order book, funding, and open interest.
- Fee rates and instrument constraints.
- Position mode, margin mode, and leverage.
- Market and post-only limit orders.
- Order query, cancellation, fill reconciliation, and position lookup.
- Open-position detection and restart reconciliation.

## 9. BingX Perpetual Futures VST Adapter

Implement `BingXFuturesClient` with the existing `httpx` stack.

Environment and authentication:

- Primary VST URL: `https://open-api-vst.bingx.com`.
- `.pro` VST domain is used only after a network error or timeout, never after a valid business/API error.
- Use a BingX VST API key and secret; BingX does not use the OKX passphrase.
- Add millisecond `timestamp` and `recvWindow=5000`.
- Build the canonical parameter string in ASCII key order and sign with HMAC-SHA256 lowercase hexadecimal output.
- Send `X-BX-APIKEY` and the required source header.
- Keep the machine clock synchronized and report clock-drift errors clearly.

Market and account mapping:

- Map internal `BTC/USDT` to BingX `BTC-USDT`.
- Implement contract metadata, ticker/price, depth, premium index/funding, v3 klines, open interest, positions, commission, order, cancel, leverage, margin mode, and position mode endpoints.
- Filter klines so only candles with `closeTime <= now` are used.
- Respect per-route BingX rate limits using throttling and short-lived caching.

Sizing and orders:

- BingX perpetual order quantity is base-asset quantity, not the OKX contract count.
- Round quantity using `quantityPrecision` and validate `tradeMinQuantity`, `tradeMinUSDT`, contract availability, API trading status, and venue leverage limits before placing an order.
- Post-only orders use `LIMIT` with `timeInForce=PostOnly`.
- In hedge mode, closing a position uses the opposite order side with the same `positionSide`; omit `reduceOnly` because BingX does not accept it for dual-side positions.
- Aggregate fill data for the order ID to determine executed quantity, average price, fees, and realized PnL.

Order idempotency:

- Generate a unique `clientOrderId` for every order intent.
- After a timeout or uncertain POST response, query the order by `clientOrderId` before any resubmission or domain failover.
- Never blindly retry an order placement that could already have reached the exchange.

Official implementation references:

- [BingX base URLs](https://github.com/BingX-API/api-ai-skills/blob/main/skills/references/base-urls.md)
- [BingX authentication](https://github.com/BingX-API/api-ai-skills/blob/main/skills/references/authentication.md)
- [BingX swap market API](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-market/api-reference.md)
- [BingX swap account API](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-account/api-reference.md)
- [BingX swap trade API](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-trade/api-reference.md)

## 10. Configuration and Data Migration

Replace OKX runtime configuration with:

```env
BINGX_API_KEY=your_bingx_vst_api_key
BINGX_SECRET_KEY=your_bingx_vst_secret_key
BINGX_ENV=vst
BINGX_BASE_URL=https://open-api-vst.bingx.com
BINGX_MARGIN_MODE=isolated
BINGX_POSITION_MODE=hedge
```

Rename venue-specific risk settings to neutral names where behavior is shared:

- `MAX_SPREAD_BPS`
- `FUNDING_BLACKOUT_MINUTES`
- `MIN_LIQUIDATION_DISTANCE_BPS`
- `HARD_MAX_LEVERAGE`
- `VENUE_MAX_LEVERAGE`

Update `.env.example`, public configuration validation, environment cleaner, runtime logs, README, reports, realtime backtest source labels, and PM2 runbook. Never inspect, print, or migrate values from `.env` automatically.

Supabase changes:

- Add `exchange` and `ai_call_id` to relevant trade and decision records.
- Backfill historical records as `exchange='okx'`; write all new VST records as `exchange='bingx'`.
- Add an `ai_call_metrics` table for provider usage telemetry.
- Add persistent open-position lifecycle state with exchange, phase/status, position ID, symbol, direction, quantity, entry, SL/TP, order IDs, fill details, and timestamps.
- Prefer BingX history in trade memory. Existing OKX results may be used only as labelled cold-start context until sufficient BingX outcomes exist.

On restart, resume a known persisted BingX position. If the exchange reports an order or position that cannot be matched safely, stop new entries and alert rather than automatically flattening unknown exposure.

## 11. Test-Driven Implementation

Tests must be written before each production behavior and total coverage must reach at least 80%.

AI pipeline tests:

- Compact payload includes the required signals without duplicate factor collections.
- Entry and exit payloads remain within their engineering budgets.
- The merged risk review preserves approval and veto behavior.
- Deterministic guards behave exactly as before.
- Exit AI respects candle events, adverse-move events, minimum interval, request hash, and maximum calls per trade.
- Usage parsing supports responses, chat completions, and messages dialects.
- Retry classification fails fast on permanent errors and performs only one structured repair.
- A historical replay tool deduplicates stored batch requests before comparison.

BingX client tests:

- Signing golden vectors, parameter ordering, timestamp, `recvWindow`, and headers.
- VST/live isolation and `.pro` fallback only for network failures.
- Symbol conversion, contract parsing, precision rounding, minimum quantity/notional, and leverage caps.
- Kline, ticker, depth, funding, open-interest, commission, position, order, and fill parsing.
- Post-only behavior and hedge-mode entry/close semantics.
- Unique `clientOrderId` and lookup-before-resubmit behavior.
- Partial fills, fee aggregation, clock drift, API errors, and rate limiting.

Integration and verification commands:

```powershell
$env:PYTHONPATH="src"; python -m compileall src tests
$env:PYTHONPATH="src"; python -m unittest discover -s tests
```

Add a coverage dependency and command, then require at least 80% line coverage in CI and before merge.

## 12. Rollout and Acceptance

1. Add usage instrumentation and collect a comparable baseline without changing decisions.
2. Build and test the compact payload and merged risk review.
3. Replay deduplicated historical decisions to identify material behavior changes before exchange cutover.
4. Verify BingX VST authentication and all required read-only endpoints.
5. Validate BingX's test-order endpoint.
6. After explicit approval, execute one minimum-size VST round trip and verify the account is exactly flat.
7. Run one BingX VST instance as a canary until 100 trades have completed.
8. Report actual token usage, calls by purpose, structured-response validity, retry/error rates, net PnL after fees per 1,000 USDT volume, drawdown, stop-loss rate, and trade frequency.
9. Use that report as a manual tuning checkpoint; do not change the strategy automatically.
10. After BingX parity and the VST smoke test succeed, remove the OKX client, credentials, runtime branches, documentation, and OKX-specific tests completely.

## 13. Final Acceptance Criteria

- The model/provider and current `ai_volume` strategy remain unchanged during the initial optimization.
- Existing deterministic guards remain active and tested.
- The separate pre-trade AI call is removed and its risk review is present in the entry response.
- Exit AI is event-driven while deterministic monitoring remains five-second.
- No duplicate factor context is sent to the AI.
- Token consumption is measured and reportable per call and per day.
- BingX VST is the only enabled execution environment; live endpoints fail closed.
- All order placement is idempotent and hedge-close semantics are verified.
- Full tests pass with at least 80% coverage.
- A 100-trade BingX VST report is produced before any further strategy tuning.
- OKX is removed completely only after BingX parity and VST validation pass.
