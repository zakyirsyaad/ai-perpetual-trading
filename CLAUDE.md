# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Async Python bot that trades **BingX Perpetual Futures VST**. Execution is live-data-only: it never simulates fills outside BingX VST. There is no UI — it runs as a single long-lived loop process (`src/main.py`) and persists to Supabase.

## Commands

`PYTHONPATH=src` is mandatory for every command (the package lives under `src/`, not installed).

```bash
# install (Python >=3.10)
pip install -r requirements.txt

# run the bot
PYTHONPATH=src python src/main.py

# all tests (stdlib unittest — there is no pytest config)
PYTHONPATH=src python -m unittest discover -s tests

# a single test module / case
PYTHONPATH=src python -m unittest tests.test_ai_orchestration
PYTHONPATH=src python -m unittest tests.test_ai_orchestration.ClassName.test_method

# coverage (gate is 80% line coverage, enforced by fail_under in pyproject.toml)
PYTHONPATH=src python -m coverage run -m unittest discover -s tests
PYTHONPATH=src python -m coverage report

# cheap syntax/import check (closest thing to a lint — there is no configured linter)
PYTHONPATH=src python -m compileall src tests

# TradingView relay (separate process; the bot fetches from it — see Gotchas)
PYTHONPATH=src python src/tradingview_relay.py

# rewrite .env onto the DEFAULT_ENV_OVERRIDES baseline (timestamped backup first)
PYTHONPATH=src python src/clean_env.py --env .env --example .env.example

# reporting (read from Supabase)
PYTHONPATH=src python src/report_ai_decisions.py --limit 1000
PYTHONPATH=src python src/report_arena.py attribution --diagnose
PYTHONPATH=src python src/report_ai_usage.py --days 1                      # AI token spend by purpose
PYTHONPATH=src python src/replay_ai_decisions.py --limit 500               # deduplicated decision replay
PYTHONPATH=src python src/realtime_backtest.py --source auto --no-trades   # dry-run, places no orders
```

**CI is the merge gate.** `.github/workflows/tests.yml` runs `compileall` → `coverage run` → `coverage report` on every push to `main` and every pull request. `fail_under = 80` in `pyproject.toml` makes the report step fail the build below 80% line coverage, so run those three locally before proposing a merge.

Note: `README.md` and `AGENTS.md` show PowerShell (`$env:PYTHONPATH="src"`); the dev machine here is macOS/zsh and the VPS is Linux, so use the POSIX forms above.

## Configuration

All config is environment-driven via `pydantic-settings` in `src/bot/config.py`, loaded from `.env`. `.env` is gitignored — **do not read or print it**; use `.env.example` for the public shape. Changing behavior almost always means changing an env var, not code. Most thresholds that look hardcoded are actually `Settings` fields with env aliases.

**Env combinations that refuse to boot** (`Settings.validate_cross_field_settings`): `XAI_API_KEY` is required when `STRATEGY_MODE` is `grok` or `ai_volume`; `LEVERAGE` may not exceed `HARD_MAX_LEVERAGE` or `VENUE_MAX_LEVERAGE`; and `VOLUME_EXECUTION_MODE=maker_only` requires `EXECUTION_POST_ONLY_FALLBACK_TO_MARKET=false`. Field validators additionally constrain `DECISION_TIMEFRAMES` (only `1m`/`3m`/`5m`) and `DUNE_PERFORMANCE_TIER` (`small`/`medium`/`large`).

**`VOLUME_CYCLE_STARTED_AT`** starts a fresh volume-building cycle without deleting history. It must be a timezone-aware ISO-8601 timestamp and is rejected more than five minutes in the future (fail-closed). It scopes **volume accounting only** (`storage/supabase_trade_store.py`); daily PnL, stop-loss counts, and the other risk counters stay scoped to BingX for the whole local day regardless of the cutoff, and historical OKX rows are preserved.

## Architecture

`src/main.py:run()` is the composition root. It builds the BingX client, resolves the venue leverage cap (`exchange/leverage.py`, which may downgrade `settings.leverage`), wires storage/risk/intelligence, selects a strategy, and hands everything to a single `TradeEngine.run_forever()` tick loop.

### Strategy modes

`STRATEGY_MODE` has three values but only **two** strategy classes — `main.py` branches on `volume` vs. everything else:

- `volume` — deterministic rotation, **no AI key needed** (`VolumeStrategy`).
- `grok` — AI risk-sized trading (`GrokStrategy` over `AIOrchestrator`).
- `ai_volume` — gets the *same* `GrokStrategy` object as `grok`. The two diverge inside `AIOrchestrator`: `_candidate_to_trade_plan` sizes from `volume_profile.margin_per_entry_usdt` for `ai_volume` and from `_calculate_position_usdt` otherwise, and `_strategy_objective_payload` swaps the prompt objective ("build volume, don't sit out" vs. "only positive expectancy after fees"). This is the live profile in `.env.example`.

### The AI decision pipeline (the "layers")

Understanding this requires reading `intelligence/market_intelligence.py`, `ai/brain.py`, and `ai/orchestration.py` together:

1. `MarketIntelligence` synthesizes exchange truth (candles/orderbook/funding/OI/fees) + external signals (CMC, Dune, TradingView) + `arena` reviews into one context dict. It depends on `FuturesExchangeProtocol`, not on a venue client.
2. `ai/trade_memory.py:build_trade_decision_memory` folds realized closed trades into compact "lessons" (winning/losing patterns bucketed by reason, direction, and confidence band) that `AIOrchestrator` injects into the payload. It is **advisory only — it creates no code-side gates**; the AI stays responsible for the next decision.
3. `GrokBrain` (`ai/brain.py`) makes the LLM calls. Despite the name "Grok" (and the `xai_*` config aliases), the provider is pluggable via `XAI_API_STYLE`, which selects one of three wire dialects — they differ in endpoint, auth, and how the JSON schema is enforced:
   - `responses` — xAI-native `/responses` with strict server-side `json_schema`, Bearer auth.
   - `chat` — OpenAI-compatible `/chat/completions` with `json_object`; schema is embedded in the system prompt and enforced only by pydantic + one retry.
   - `messages` — Anthropic-style `/messages`: top-level `system`, mandatory `max_tokens` (`XAI_MAX_TOKENS`), `x-api-key` + `anthropic-version` headers. Same prompt-embedded schema as `chat`.

   The **live VPS `.env` runs `XAI_API_STYLE=chat` with `XAI_MODEL=mimo-v2.6-pro`** (since 2026-09-25; before that `mimo-v2.5`, which was also the OKX cycle's model — so any comparison straddling that date carries a model change as a confound). **2.6-pro is a reasoning model**: it returns its chain in a separate `reasoning_content` field beside `content`, so the `chat` parser is unaffected, but those tokens count against `XAI_MAX_TOKENS`. Measured on live entry calls: reasoning ~2.4k tokens (max 2.6k) inside 2.9-3.3k total output against a 4,096 cap, input ~16k, latency up to 64 s against `XAI_TIMEOUT_SECONDS=90` — budget both with headroom, and note that one call now takes longer than `AI_EXIT_CHECK_INTERVAL_SECONDS` (15 s). (For the `messages` style a `thinking` block before the `text` block is still handled: `_extract_response_text` skips to the first `text` block.) Pydantic models (`EntryDecision` with its nested `RiskReview`, and `ExitDecision`) validate the structured output; only `responses` validates schema server-side, so `chat`/`messages` lean on the single repair request. Logs still say "Grok" regardless of provider.
4. `AIOrchestrator` (`ai/orchestration.py`) runs the AI **once per tick**. The old separate `pre_trade_check` call is gone: the skeptical risk review is now a nested `risk_review` object inside each entry decision. `approved=false` vetoes the candidate, and a missing/structurally-invalid review makes it unusable rather than approved (the previous fail-open path was removed). The AI payload is built by `CompactDecisionContextBuilder` (`ai/compact_context.py`), never from the raw context dict.
5. `GrokStrategy.next_trade_plan()` returns a `TradePlan` or `None`.
6. **Decision instrumentation.** Every model *exit* verdict lands in `ai_exit_verdicts` with whether the engine honoured it (`gross >= fee`, or `AI_EXIT_ADVERSE_CUT_USDT` on a loser) and the position's PnL at that moment; the exit policy's deterministic fallback holds carry `ai_called=false` and are not recorded. Every *entry* decision carries a `pacing` snapshot in `payload_json` (volume today vs `volume_profile.daily_volume_usdt`, hours left in the trading-timezone day, trades and PnL today). Pacing is recorded only — it is deliberately absent from the model request until a pacing prompt is validated offline.

### Token optimization

(`ai/compact_context.py`, `ai/exit_policy.py`, `ai/usage.py`) Factor *aggregates* plus a small ranked list per symbol replace the 86 snapshots repeated across four collections, and flow signals are capped.

**Caveat — the external-provider projection is dead code.** `_compact_external_signals` and `_summarize_provider` exist but are called from nowhere; `d96d13e` ("restore safe BingX AI trading parity") made `_deduplicated_market_context` forward `external_signals` verbatim instead. So the documented budgets (~4k for a two-symbol entry, ~1.5k for a one-symbol exit) are **not** what the live bot spends — `ai_call_metrics` shows ~21k tokens per entry call and ~9k per exit call. Do not trust those numbers without re-measuring. The only external pruning that runs is of a provider carrying no data at all (see Deployment & gotchas).

AI exit evaluation is **event-driven** (new closed candle / adverse move / favourable move when `AI_EXIT_FAVORABLE_MOVE_THRESHOLD_PCT` > 0 / regime-or-flow change) behind `AI_EXIT_CHECK_INTERVAL_SECONDS`, `AI_EXIT_MAX_CALLS_PER_TRADE`, and a request-hash skip. Gating is two-stage: budget/interval/candle/adverse resolve with no network call; only if those are inconclusive is context built to test a regime-or-flow change, and those probes share the same interval limit — the deterministic `POSITION_MONITOR_INTERVAL_SECONDS` monitor (default 5s) is unchanged. `AI_EXIT_EVENT_RESERVE_CALLS` keeps the last N calls of the per-trade budget for price moves only: routine candle and flow triggers spent all 25 calls by minute 11–20 on trades #587 and #574, leaving the model blind when those trades later moved. With routine calls exhausted the gate reports `routine_budget_reserved` and no context is built. `AI_EXIT_MIN_HOLD_SECONDS` (default 0) keeps the model out of the first minutes of a trade: net starts near −18 from fees, so early calls read "underwater" and cut noise — AI exits within 3 minutes lost 98.76 over 8 trades versus deterministic exits, later ones saved 282.89 over 24. The deterministic stop, TP, floor and time stop still run.

Retries only cover connection/timeout/429/retryable-5xx; permanent 4xx fails immediately, and an invalid response gets exactly one repair request that carries the validation error and contract, not the original payload. Every provider request writes one `ai_call_metrics` record, with tokens and bytes accumulated across all attempts (a repair really does spend both attempts' tokens); `report_ai_usage.py` summarizes it and is observability-only.

### Debugging "why no trades"

`TradeEngine._tick` logs `"Strategy returned HOLD"` whenever `next_trade_plan()` returns `None`. That `None` is usually because **the LLM itself chose `action="HOLD"`** — a soft, prompt-driven outcome — *not* because a code hard-gate rejected it. A `None`/`ValidationError` from the AI call is also collapsed into the same HOLD log, so infra failures, parse failures, and genuine vetoes all look identical there. Trace the actual cause through the per-symbol lines emitted by `AIOrchestrator._build_entry_candidate`, not the generic HOLD line.

Gates inside `_build_entry_candidate`, in order, each with its own log line: AI returned `HOLD` → symbol not in `SYMBOLS` → `confidence < AI_MIN_CONFIDENCE` → `risk_review` missing (unusable, never approved) → `risk_review.approved == false` (vetoed) → **entry stretch gate** (only when `ENTRY_STRETCH_GATE_MODE=enforce`) → `_resolve_trade_targets` could not build a target. Note the module comment there claims the confidence gate was removed for volume runs; the code still enforces it — trust the code.

### The tick loop and its gates

`run_forever` sleeps `ENTRY_INTERVAL_MINUTES` between ticks, and `_execute_single_trade` → `_manage_open_position` blocks **inside** the tick until the position closes. The loop is single-threaded and there is no concurrent position management, so "one open position total" is structural, not merely configured.

`_tick` gates, in actual execution order, all *before* the strategy runs:

1. `_entries_blocked_reason` — the reconciliation kill-switch (see Restart behavior). Logged at ERROR; nothing else runs.
2. Daily risk guard (`RiskManager.get_daily_state().allowed_to_trade`).
3. Cooldown after stop-loss (`COOLDOWN_MINUTES_AFTER_STOP`).
4. Cooldown after entry (`COOLDOWN_MINUTES_AFTER_ENTRY`).
5. Volume-runtime pause (`_volume_runtime_pause_reason`): the daily volume target and the daily fee budget only. There is **no cumulative-volume stop** any more (the 10M cycle target was removed on 2026-10-12); `VOLUME_TARGET_USDT` is only a planning figure.
6. Daily trade target (`state.closed_trades >= _daily_entry_limit()`).

After a plan exists, two more can still drop it: `PREVENT_NEW_ENTRIES_IF_EXCHANGE_POSITION_OPEN` (re-checks the venue), then `_pre_entry_execution_checks` inside `_execute_single_trade` (spread / orderbook depth / funding blackout / liquidation distance). These guards are intentionally independent of the AI — the AI can say OPEN and the engine still skips on bad exchange state.

**Daily risk guard is stateless across restarts.** `risk/risk_manager.py:get_daily_state()` recomputes stop-loss count, drawdown, and consecutive losses from Supabase every tick, keyed on `datetime.now(timezone.utc).date()`. A pm2 restart does **not** clear a daily pause (`MAX_STOP_LOSS_PER_DAY` / `MAX_DAILY_DRAWDOWN_USDT` / `MAX_CONSECUTIVE_LOSSES`) — the bot stays paused until the real UTC midnight rolls the date over, or you raise the limit. Don't expect "restart to unpause."

### Order routing

**Maker-capable, not taker-only.** Entries route as post-only when `EXECUTION_ENTRY_ORDER_TYPE=post_only` or `VOLUME_EXECUTION_MODE=maker_only` (`_uses_post_only_entry` → `_place_post_only_entry`, with TTL/reprice/fill-poll). On BingX a post-only order is `LIMIT` with `timeInForce=PostOnly`. `EXECUTION_POST_ONLY_FALLBACK_TO_MARKET` decides whether an unfilled maker entry falls back to taker (forced off in `maker_only`). Exits go post-only too, except `_exit_allows_taker` forces taker for `stop_loss`/`liquidation_guard` so a losing exit is never stuck resting. Trust the startup fee log (BingX `commissionRate`) over the `maker_fee_pct` config default; a negative maker rate is a real rebate, so a maker-filled entry *earns* fees while a taker fill *pays*.

**Bounding the loss side** (both default to `0` = disabled, so an unchanged `.env` keeps the old behaviour):
- `MAX_STOP_LOSS_PCT` caps the ATR-scaled stop in `_resolve_trade_targets`, applied *before* the TP floor is derived from `sl_pct`. Without it `max(base_sl, atr_pct * ATR_SL_MULTIPLIER)` is unbounded, so a volatile entry opens with a stop far wider than the profit-taking band. It never widens an already tighter stop and never bypasses `_stop_is_strictly_before_liquidation`.
- `AI_EXIT_ADVERSE_CUT_USDT` lets `_monitor_position` honour an AI `close_now` on a *losing* position once `net_after_fee <= -cut`. The pre-existing `gross >= fee` guard honours it only when already profitable, leaving the full stop and the time stop as the only exits from a losing trade.

**Neutral exit mode** (`VOLUME_NEUTRAL_EXIT_MODE`, default `window`): `window` exits only while a 5 s poll lands inside `MIN_NET ≤ net ≤ MIN_NET + BAND`, so a price that jumps the band in either direction is never taken — trade #574 went +10 → +85 net in one minute, sat at +78..+122 for twenty minutes, and closed on the time stop at −0.84. `floor` exits the first time net ≥ `VOLUME_NEUTRAL_EXIT_MIN_NET_USDT`; `trailing` arms at that floor and exits at `max(floor, peak − VOLUME_NEUTRAL_EXIT_TRAIL_USDT)`, trading some give-back on gaps for more on runners. Every exit replay so far modelled `floor`, not `window`. All modes keep the `neutral_exit` reason, and net is after both taker fees — BingX's unrealised PnL on screen is before fees, so +50 there is about +32 here. `VOLUME_NEUTRAL_EXIT_BASIS=gross` applies the thresholds to the before-fee number instead, and a `Position ... | BingX unrealised (before fees) ... | net ...` line every `POSITION_STATUS_LOG_SECONDS` (default 60) reconciles the two views while a position is open.

**Entry stretch gate** (`ENTRY_STRETCH_GATE_MODE`, default `off`): rejects entries whose price sits more than `ENTRY_STRETCH_GATE_MAX_ATR` (0.75) from the 21-EMA, measured in ATR — the shape that carries the measured losses. `shadow` records the verdict without changing any decision; `enforce` rejects. `ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR` (0 = never) relaxes the gate once the day needs more entries per hour than that to still reach the daily volume target — the volume mandate wins over selectivity on a day that is behind. Every decision carries the verdict at `ai_decisions.payload_json.gate`, so shadow mode is queryable rather than log-only.

**`liquidation_guard` is a historical exit reason.** Closed trades carry it (20% of the OKX cycle, at only −18.21 average — a cheap early stop), but **no code path produces it any more**; it survives only in `_exit_allows_taker`, `trade_memory`, and prompt text. Its removal is why the average stop-loss cost roughly doubled after the BingX migration, and `MAX_STOP_LOSS_PCT` is the explicit replacement.

**Sizing floor:** there is no `MIN_ORDER_CONTRACTS`. `calculate_order_size` returns 0 when the venue's `tradeMinQuantity`/`tradeMinUSDT` rejects the size, and that is the only authority. The old OKX contract-count floor of 1 silently blocked every BTC entry once quantity became base-asset (18,000 USDT notional is ~0.3 BTC).

### Confluence and Arena

**Confluence** (`strategy/confluence.py`, wired into `volume_strategy.py`): derives direction from a consensus of the 86 arena factors with agreement + ATR gates and ATR-scaled TP/SL, controlled by `CONFLUENCE_*` env vars.

**Arena** (`src/bot/arena/`): backend-only factor intelligence (86 factor defs, effectiveness scoring, edge-triggered flow signals, attribution, a sandboxed Program Trader dry-runner). Its reviews are injected into the AI payload as `arena_reviews`. Inspect via `src/report_arena.py`.

### Storage

Supabase is **required** — `main.py` refuses to start without `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`; the local trade.db path is disabled. Apply `supabase/schema.sql` in the Supabase SQL Editor before running. Closed-trade writes need the `confidence`, `factor_metadata`, `trigger_metadata`, and `exchange` columns on `trades`; the migration also adds `ai_call_metrics`, `open_position_state`, and `ai_exit_verdicts`. **`save_learning_note` is a no-op** — there is no `learning_notes` table, so every entry `learning_note` the model writes is dropped. A missing migration surfaces as `column trades.X does not exist` when saving a closed trade.

`SupabaseTradeStore` is the live path. `storage/supabase_journal.py:SupabaseJournal` is **dead code**: `main.py` passes `supabase_journal=None` to both `AIOrchestrator` and `TradeEngine`, and nothing constructs it. Don't debug missing journal rows — there is no journal.

### Safety invariants

`BINGX_ENV=vst` is required and `BINGX_BASE_URL` must be exactly `https://open-api-vst.bingx.com` — `.com` is the mandatory primary host, and `.pro` is deliberately not configurable (it exists only as an internal fallback after a network error/timeout, so config can never skip the primary). Both `BingXFuturesClient.__init__` and `TradeEngine._startup_safety_checks` refuse otherwise, so live endpoints fail closed. Defaults are isolated margin, hedge position mode, explicit leverage caps (venue max resolved at startup in `exchange/leverage.py`), and one open position total. Internal `BTC/USDT` maps to BingX `BTC-USDT`. `POSITION_USDT * LEVERAGE` is target notional; **BingX order quantity is base-asset quantity, not a contract count**, rounded with `quantityPrecision` and validated against `tradeMinQuantity`/`tradeMinUSDT`.

**Exchange-neutral contract:** `exchange/contracts.py` holds `FuturesExchangeProtocol` and the venue-neutral models. `bingx_client.py` is the runtime adapter; `okx_client.py` is legacy, no longer wired into `main.py`, and is deleted only after BingX parity and the VST smoke test pass (plan §12 step 10).

**Auth/transport contract** (rationale in README §"Exchange-Neutral Runtime"): `follow_redirects=False`, and any 3xx raises non-retryable — httpx would otherwise resolve a 307/308 inside one `request()` call, beneath the single-attempt/no-failover order rules, and re-send the body to whatever `Location` names. `X-SOURCE-KEY` must be exactly `BX-AI-SKILL` on every request including public endpoints; the client constructor and `Settings` both reject anything else. `.pro` failover happens only on a connection error or timeout — once an HTTP response exists it is handled on that host (documented transient codes retry there, then raise; every other `code != 0` raises immediately). `get_symbol_position_details` raises when BingX reports more than one nonzero position row for a symbol, so hedge-mode dual exposure can never be collapsed into whichever row came first.

**Order idempotency:** every order intent gets a unique `clientOrderId` (camelCase — that is the documented BingX field; `clientOrderID` is silently ignored by the venue). The order POST is **single-attempt** (`_signed_request(..., allow_retries=False)`), because a retried POST can duplicate a position. On an uncertain outcome the client **never issues a second POST**: `_reconcile_uncertain_order` retries `_lookup_order_by_client_id` (found/absent/unknown) and only a confirmed `found` resolves it; everything else raises for manual reconciliation. `absent` is reported for the documented `109421` only — and even that is not an authorization to resubmit, because BingX describes it as "may have been filled, cancelled, or order ID is incorrect". Free-text "not exist" matching is deliberately gone (`109425` is *trading pair* not found). Closes send `reduceOnly=true` in one-way mode and omit it in hedge mode. `.pro` is never tried for order placement.

**Never restart with an open position.** `_initialize_leverage` calls `set_margin_mode`, which BingX rejects with `104103` ("Please close open positions or cancel pending orders first") while any position is open. `_verify_account_configuration` then reads back the *effective* mode, finds `crossed` instead of `isolated`, and exits — and because pm2 has `autorestart: true`, that becomes a **crash loop** that hammers the venue auth endpoint (36 restarts in one minute, observed 2026-09-02). Recovery is `pm2 stop ai-perp-volume`, close the position on the venue, then start. Flip the position flat *before* any redeploy.

**Startup account verification:** `_initialize_leverage` applies margin mode, position mode, and leverage best-effort, then `_verify_account_configuration` reads the *effective* values back and raises on any mismatch or unreadable value. There is no fail-open flag — trading with the wrong margin mode or leverage is worse than not starting.

### Restart behavior

(Details in README §"Restart Behavior".) Open-position lifecycle state lives in Supabase `open_position_state`. Matching is on identity — `positionSide`/direction, quantity within a partial-fill tolerance, average price, and position id (captured at entry and persisted; if it was known and the readback loses it, that is unknown exposure) — not just symbol, so a persisted LONG is never matched to an exchange SHORT. A confirmed match is resumed and monitored on its *persisted* `stop_loss_pct`/`take_profit_pct` — never the current config, so a redeploy cannot re-price a running trade.

**A position is never left unmonitored.** A failed price read inside `_monitor_position` costs one iteration (WARNING, ERROR from the third consecutive failure) and monitoring continues; any other exception from the monitor is caught in `_manage_open_position` and monitoring resumes with a capped backoff. Failures that still escape happened in the close phase, where an exit order may already exist and a retry could duplicate it, so `_manage_open_position_guarded` asks the venue: flat keeps trading (the trade record may be missing), open or unreadable sets `_entries_blocked_reason`. Both `_execute_single_trade` and `_resume_persisted_positions` use it. Before this, one SSL `ConnectError` on `quote/price` (2026-09-12 15:03 UTC) reached the `run_forever` catch-all and left a LONG unwatched for hours. The supervisor treats this latch as `hold`, never `restart`.

A missing/invalid persisted envelope is unknown exposure. A mismatch, an unreadable position, or exposure with no persisted record is **never auto-flattened** — it sets `_entries_blocked_reason`, which blocks every subsequent tick at gate 1 and alerts the operator until a human resolves it.

### Live dashboard feed (optional, off by default)

`TELEMETRY_ENABLED=true` starts a read-only, loopback-only HTTP/SSE server (`src/bot/telemetry/`) that a laptop reads through an SSH tunnel; the screen is `dashboard/` (Bun + Next.js, adapted from MIT-licensed jev-trade, see `dashboard/THIRD_PARTY_NOTICES.md`). Operator steps are in README §"Live Dashboard".

- **It can never affect trading.** `main.py` builds it before the engine and starts it before `run_forever`. `TradeEngine` only *reports* through six hooks on an observer (`telemetry/observer.py`), after the safety step each one follows (the mark is after the stop-loss check, the close is last). Every hook, hub and bus method catches and logs its own errors; a server that cannot bind logs a warning and trading continues. Keep it that way: no hook may return a value an engine decision reads.
- **`MarketDataPoller` is read-only by test.** It calls only `get_order_book` and `get_klines`; its test client raises on any other attribute. Don't add private-endpoint reads to the feed.
- **`TELEMETRY_PRICE_STREAM=true` adds live prices over a websocket (default off).** `BookTickerFeed` (`telemetry/book_ticker.py`, on the stdlib client in `telemetry/ws.py`, no new dependency) subscribes only to `<PAIR>@bookTicker` on the public VST host `wss://vst-open-api-ws.bingx.com/swap-market`, about every 0.4 s versus one REST poll per 5 s. The host is a constant, not a setting. Polling never stops being the fallback: `poller.on_stream_tick` marks a symbol fresh for `stream_fresh_seconds` (3 s) and `_poll_price` skips it only while fresh, so a dead stream degrades to the old 5 s chart. Nothing the engine reads comes from this: trading prices still come from `FuturesExchangeProtocol`. Use `ws.with_timeout`, never bare `asyncio.wait_for`, in this code: before Python 3.12 `wait_for` can swallow a cancellation, which left a stopped feed running and hung the test suite about one run in ten (RED test `test_cancelling_the_waiter_is_never_swallowed...`).
- **The wire shape is the dashboard's, not ours.** `telemetry/wire.py` mirrors jev-trade's JSON; `tests/test_dashboard_contract.py` fails if `dashboard/src/lib/bot-types.ts` and the pydantic models disagree. Change both together. `probabilities` is one-hot on the action taken (the bot has no distribution); the model's certainty travels as `confidence`.
- **`GET /volume` feeds the volume panel.** `TradeEngine._publish_volume` hands the observer the per-day volumes from `SupabaseTradeStore.daily_volume_series` at the start of every tick and after each close; the observer adds the running total and the hub serves it. It is a separate route, not part of `Totals`, because the figures must survive a restart and stay readable while the weekly entry window is closed (no block events then). `{}` means nothing published yet.
- **The dashboard starts at `VOLUME_CYCLE_STARTED_AT`.** The volume series, the seeded History/Trades and the header `realized` (`SupabaseTradeStore.realized_pnl_since_cycle`, passed to `history_loaded(..., realized_usd=...)`) all count only from that cutoff; History and Trades render one band per WIB day (`lib/fills.ts` `groupByDay`).
- **History and Trades are seeded from the store at startup.** Both tabs read fills off the in-memory chart tape, which a restart empties. `TradeEngine._seed_dashboard_history` (before `_resume_persisted_positions`, which blocks) loads the newest `DASHBOARD_HISTORY_TRADES` closed trades through `SupabaseTradeStore.recent_closed_trades`; `EngineTelemetry.history_loaded` turns each into an open fill and a close fill carrying net PnL and the total fee (gross minus net), and `TelemetryHub.seed_fills` puts them on the tape without publishing. First paint still shows only the newest 12 fills until `/tape` hydrates.
- **Enabling it restarts the bot**, so the "never restart with an open position" rule above applies.

## Repo conventions

- **TDD evidence documents.** Substantive changes land with a `.github/tdd/<feature>.tdd.md` recording the user journey, RED evidence (the actual failure output), the GREEN implementation, verification numbers (tests passed, coverage %, `compileall`, `git diff --check`, `pip check`, secret hygiene), the commit trail, and a deployment gate. See `bingx-volume-cycle.tdd.md` for the shape.
- **`tests/test_secret_hygiene.py` scans every git-tracked file** for `fe_oa_*`, `sk-*`, and `ghp_*` patterns and fails the suite on a hit — including in docs, examples, and TDD evidence. Never paste a real-looking key anywhere tracked.
- There is no configured linter or formatter. `compileall` is the cheap check; CI enforces nothing beyond it plus the coverage gate.

## Deployment & gotchas

- **The VPS is the sole live instance.** It runs under pm2 as app `ai-perp-volume` at `ubuntu@43.129.56.85` (ssh alias `0xAlvary2`), dir `/home/ubuntu/ai-perpetual-trading`. Note `ecosystem.config.js` in the repo still names `/home/0xkayz/...` and is **not** what the VPS runs from. Never start a second live run — duplicate BingX orders / Supabase writes. The bot logs to **stderr**, so live logs are in pm2's *error* log (`~/.pm2/logs/ai-perp-volume-error.log`), not the out log.
- **External providers that return nothing are pruned from the payload.** `_deduplicated_market_context` drops `coinmarketcap`/`dune`/`tradingview` when the payload holds no content anywhere inside it (`_has_signal_content`); providers with data keep full parity, and metadata such as `collected_at` is never pruned. This matters because a failed provider does **not** return a falsy value: `DuneProvider.fetch()` returns the truthy skeleton `{"saved_queries": {}, "builtin_signals": {}}` even when every sub-request failed. Before the prune that reached the model as a present-but-empty provider, which the entry prompt ("reduce confidence when a provider is empty") penalises.
- **The TradingView relay is a passive webhook receiver, not a fetcher.** It only serves what TradingView POSTed to `/tradingview/webhook`. On the live VPS it is not running, `bot_data/` has never existed, and `TRADINGVIEW_WEBHOOK_URL` points at `127.0.0.1` — which TradingView's servers can never reach. TradingView has therefore never delivered a signal on BingX, and starting the relay without a publicly reachable webhook URL plus configured alerts changes nothing.
- **Dune is failing on the live VPS.** All three `BUILTIN_DUNE_SIGNAL_SQL` queries error (two `400 Bad Request` — malformed SQL — and one `429`). Clearing `DUNE_API_KEY` is the only clean way to make the provider absent; `DUNE_USE_BUILTIN_SIGNALS=false` still returns the truthy skeleton.
- `VOLUME_BREAKEVEN_EXIT_ENABLED` must stay `false` in `ai_volume`: enabling it closes winners near breakeven before the confluence TP plays out (gross/fee collapses).
- External signals (CMC/Dune/TradingView) are throttled by `EXTERNAL_SIGNAL_CACHE_SECONDS` (default 300) to avoid burning Dune credits during position monitoring.
- Structural reality to keep in mind when tuning: taker fee ~0.05% per side is locked, so the strategy's edge must clear round-trip fees to be net-positive. Validate any "profitable" claim against closed trades in Supabase, not against assumptions or backtests (the AI path can't be backtested).
- Venue-neutral risk env names replaced the OKX-prefixed ones: `MAX_SPREAD_BPS`, `FUNDING_BLACKOUT_MINUTES`, `MIN_LIQUIDATION_DISTANCE_BPS`, `HARD_MAX_LEVERAGE`, `VENUE_MAX_LEVERAGE`.
- `AI_PRE_TRADE_CHECK` is gone; the risk review is part of the entry response.
