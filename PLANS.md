# Plans

## Perpetual Futures Layer Plan

- [x] Add exchange-agnostic adapter contracts and mock adapter.
- [x] Add strict normalized signal schema for BTC/USDT and ETH/USDT on 1m/3m/5m loops.
- [x] Add isolated-margin sizing and risk checks with fee/slippage/edge buffers.
- [x] Add maker-first execution state machine with reduce-only exits and reconciliation hooks.
- [x] Add conservative backtester with costs, partial fills, funding, and sweep hooks.
- [x] Add safe default config/docs and tests.
- [x] Verify with unit tests and compile check.

## Token Optimization and BingX VST Migration

Source plan: `TOKEN_OPTIMIZATION_AND_BINGX_MIGRATION_PLAN.md`.

Implemented in code:

- [x] §4 Compact AI decision context (`bot/ai/compact_context.py`), no duplicate factor collections.
- [x] §5 Merged risk review inside the entry decision; separate pre-trade AI call and its fail-open path removed.
- [x] §6 Event-driven AI exit policy (`bot/ai/exit_policy.py`); deterministic monitoring still every 5s.
- [x] §7 AI usage telemetry, fail-fast retry classification, single structured repair, `report_ai_usage.py`.
- [x] §8 `FuturesExchangeProtocol` + venue-neutral models (`bot/exchange/contracts.py`).
- [x] §9 BingX VST adapter (`bot/exchange/bingx_client.py`) with signing, VST isolation, idempotent orders.
- [x] §10 Configuration, env cleaner, and Supabase migration (exchange, ai_call_id, ai_call_metrics, open_position_state).
- [x] §11 Tests written per behavior; coverage gate at 80% (`pyproject.toml`, CI workflow).

Review round 1 (PR #2) — all P0/P1/P2/P3 fixed:

- [x] P0 Order POST is single-attempt; clientOrderId lookup is tri-state and fails closed.
- [x] P0 Wire field corrected to the documented `clientOrderId`, with contract-level tests.
- [x] P1 Legacy `MIN_ORDER_CONTRACTS` floor removed; venue metadata is the only minimum.
- [x] P1 Startup verifies effective margin mode, position mode, and leverage; fails closed.
- [x] P1 Restart reconciliation matches side, quantity, average price, and position id.
- [x] P1 AI usage/bytes accumulate across every attempt of a request.
- [x] P1 External signals use a bounded semantic projection instead of element counts.
- [x] P2 `allFillOrders` called with required `tradingUnit`/`startTs`/`endTs`; failures logged.
- [x] P2 `regime_or_flow_change` trigger is reachable via two-stage gating.
- [x] P2 `BINGX_RECV_WINDOW_MS` capped at the documented 5000 ms maximum.
- [x] P3 Trailing whitespace removed; `git diff --check` clean.

Review round 2 (PR #2) — all four follow-ups fixed:

- [x] P0 Not-found detection uses the documented `109421` only; parameter-error codes
      no longer authorise a resubmit, and an unrecognised/empty lookup shape is `unknown`.
- [x] P1 Token usage is estimated *per attempt* when the provider reports none, so a
      large invalid response followed by a small repair is no longer costed as the repair.
- [x] P2 Fill parsing uses the documented `realizedPnl` and `fee` fields, with the
      official `allFillOrders` payload shape in the regression tests.
- [x] P2 `position_id` is captured at entry, persisted, loaded, and compared, so the
      identity-matching claim is now actually enforced.

Operational, not code — must be done on the VPS/exchange before and after cutover:

- [ ] §12.1 Collect a baseline with usage instrumentation before behavior changes.
- [ ] §12.3 Replay deduplicated historical decisions (`replay_ai_decisions.py`) and review deltas.
- [ ] §12.4 Verify BingX VST authentication and every read-only endpoint against the live VST API.
- [ ] §12.5 Validate BingX's test-order endpoint.
- [ ] §12.6 After explicit approval, one minimum-size VST round trip; verify the account is exactly flat.
- [ ] §12.7 Canary one BingX VST instance until 100 trades complete.
- [ ] §12.8 Produce the 100-trade report (tokens, calls by purpose, structured-response validity,
      retry/error rates, net PnL after fees per 1,000 USDT volume, drawdown, stop-loss rate, frequency).
- [ ] §12.9 Use that report as a manual tuning checkpoint. Do not change the strategy automatically.
- [ ] §12.10 Remove the OKX client, credentials, runtime branches, docs, and OKX tests only after
      BingX parity and the VST smoke test pass.
