# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Async Python bot that trades OKX **demo** perpetual futures. Execution is live-data-only: it never simulates fills outside OKX demo trading. There is no UI — it runs as a single long-lived loop process (`src/main.py`) and persists to Supabase.

## Commands

`PYTHONPATH=src` is mandatory for every command (the package lives under `src/`, not installed).

```bash
# install (Python >=3.10)
pip install -r requirements.txt

# run the bot
PYTHONPATH=src python src/main.py

# all tests (stdlib unittest, no pytest config despite .pytest_cache existing)
PYTHONPATH=src python -m unittest discover -s tests

# a single test module / case
PYTHONPATH=src python -m unittest tests.test_ai_orchestration
PYTHONPATH=src python -m unittest tests.test_ai_orchestration.ClassName.test_method

# cheap syntax/import check (closest thing to a lint — there is no configured linter)
PYTHONPATH=src python -m compileall src tests

# TradingView relay (separate process; the bot fetches from it — see Gotchas)
PYTHONPATH=src python src/tradingview_relay.py

# reporting (read from Supabase)
PYTHONPATH=src python src/report_ai_decisions.py --limit 1000
PYTHONPATH=src python src/report_arena.py attribution --diagnose
PYTHONPATH=src python src/realtime_backtest.py --source auto --no-trades   # dry-run, places no orders
```

Note: `README.md` and `AGENTS.md` show PowerShell (`$env:PYTHONPATH="src"`); the dev machine here is macOS/zsh and the VPS is Linux, so use the POSIX forms above.

## Configuration

All config is environment-driven via `pydantic-settings` in `src/bot/config.py`, loaded from `.env`. `.env` is gitignored — **do not read or print it**; use `.env.example` for the public shape. Changing behavior almost always means changing an env var, not code. Most thresholds that look hardcoded are actually `Settings` fields with env aliases.

## Architecture

`src/main.py:run()` is the composition root. It reads `STRATEGY_MODE` and wires one of three strategies, then hands everything to a single `TradeEngine.run_forever()` tick loop.

**Strategy modes** (`main.py:85-90`):
- `volume` — deterministic rotation, **no AI key needed** (`VolumeStrategy`).
- `grok` — AI risk-sized trading (`GrokStrategy` over `AIOrchestrator`).
- `ai_volume` — AI chooses direction/symbol/timing, execution size follows the volume-builder profile. This is the live profile in `.env.example`.

**The AI decision pipeline (the "layers")** — understanding this requires reading `intelligence/market_intelligence.py`, `ai/brain.py`, and `ai/orchestration.py` together:
1. `MarketIntelligence` synthesizes OKX truth (candles/orderbook/funding/OI/fees) + external signals (CMC, Dune, TradingView) + `arena` reviews into one context dict.
2. `GrokBrain` (`ai/brain.py`) makes the LLM calls. Despite the name "Grok" (and the `xai_*` config aliases), the provider is pluggable via `XAI_API_STYLE`, which selects one of three wire dialects — they differ in endpoint, auth, and how the JSON schema is enforced:
   - `responses` — xAI-native `/responses` with strict server-side `json_schema`, Bearer auth.
   - `chat` — OpenAI-compatible `/chat/completions` with `json_object`; schema is embedded in the system prompt and enforced only by pydantic + one retry.
   - `messages` — Anthropic-style `/messages`: top-level `system`, mandatory `max_tokens` (`XAI_MAX_TOKENS`), `x-api-key` + `anthropic-version` headers. Same prompt-embedded schema as `chat`.
   `config.py` *defaults* still point at the `chat` profile (freemodel.dev / `gpt-5.4`), but the **live `.env` runs `messages` + `deepseek-v4-flash` on openmodel.ai** — a reasoning model that emits a `thinking` content block before the `text` block, so `_extract_response_text` skips to the first `text` block and `XAI_MAX_TOKENS` must leave headroom for the thinking spend. Pydantic models (`EntryDecision`, `PreTradeCheck`, `ExitDecision`) validate the structured output; only `responses` validates schema server-side, so `chat`/`messages` lean on the retry. Logs still say "Grok" regardless of provider.
3. `AIOrchestrator` (`ai/orchestration.py`) runs the AI **twice per tick**: first `entry_decisions` (one decision per symbol), then a separate skeptical `pre_trade_check` on the selected candidate. Between them it applies hard gates: `ai_min_confidence`, `ai_systemic_risk_no_trade_threshold`, RR ratio, fee-adjusted RR.
4. `GrokStrategy.next_trade_plan()` returns a `TradePlan` or `None`.

**Crucial nuance for debugging "why no trades":** `TradeEngine._tick` logs `"Strategy returned HOLD"` whenever `next_trade_plan()` returns `None` (`trade_engine.py:103-104`). That `None` is usually because **the LLM itself chose `action="HOLD"`** (`orchestration.py:158-160`) — a soft, prompt-driven outcome — *not* because a code hard-gate rejected it. The hard gates at `orchestration.py:164+` only execute after the AI returns `OPEN_*`. A `None`/`ValidationError` from the AI call is also collapsed into the same HOLD log, so infra failures, parse failures, and genuine vetoes all look identical in the log. Trace the actual cause by reading the per-symbol `"Grok entry decision HOLD ... <reason>"` lines, not the generic HOLD line.

**TradeEngine tick gates** (`core/trade_engine.py:_tick`, run *before* the strategy and execution): daily risk guard, cooldown-after-stop, cooldown-after-entry, daily trade target, volume-runtime pause. After a plan exists, `_pre_entry_execution_checks` (spread / orderbook depth / funding blackout / liquidation distance) can still silently drop it — these guards are intentionally independent of the AI (the AI can say OPEN and the engine still skips on bad exchange state).

**Daily risk guard is stateless across restarts.** `risk/risk_manager.py:get_daily_state()` recomputes stop-loss count, drawdown, and consecutive losses from Supabase every tick, keyed on `datetime.now(timezone.utc).date()`. A pm2 restart does **not** clear a daily pause (`MAX_STOP_LOSS_PER_DAY` / `MAX_DAILY_DRAWDOWN_USDT` / `MAX_CONSECUTIVE_LOSSES`) — the bot stays paused until the real UTC midnight rolls the date over, or you raise the limit. Don't expect "restart to unpause."

**Order routing is maker-capable, not taker-only.** Entries route as post-only when `EXECUTION_ENTRY_ORDER_TYPE=post_only` or `VOLUME_EXECUTION_MODE=maker_only` (`_uses_post_only_entry` → `_place_post_only_entry`, with TTL/reprice/fill-poll). `EXECUTION_POST_ONLY_FALLBACK_TO_MARKET` decides whether an unfilled maker entry falls back to taker (forced off in `maker_only`). Exits go post-only too, except `_exit_allows_taker` forces taker for `stop_loss`/`liquidation_guard` so a losing exit is never stuck resting. On the live OKX account the maker rate is a **real negative rebate** (startup log shows `maker=-0.0200%`), so a maker-filled entry *earns* fees while a taker fill *pays* — trust the startup fee log over the misleading `maker_fee_pct` config default.

**Confluence** (`strategy/confluence.py`, wired into `volume_strategy.py`): derives direction from a consensus of the 86 arena factors with agreement + ATR gates and ATR-scaled TP/SL, controlled by `CONFLUENCE_*` env vars.

**Arena** (`src/bot/arena/`): backend-only factor intelligence (86 factor defs, effectiveness scoring, edge-triggered flow signals, attribution, a sandboxed Program Trader dry-runner). Its reviews are injected into the AI payload as `arena_reviews`. Inspect via `src/report_arena.py`.

**Storage:** Supabase is **required** — `main.py:31-35` refuses to start without `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`; the local trade.db path is disabled. Apply `supabase/schema.sql` in the Supabase SQL Editor before running. Closed-trade writes need the `confidence`, `factor_metadata`, and `trigger_metadata` columns on `trades`; if a schema migration is missing, saving closed trades fails with `column trades.X does not exist`.

**Safety invariants:** `OKX_DEMO_TRADING=true` is required (bot refuses otherwise) and demo requests carry the `x-simulated-trading: 1` header; OKX defaults are isolated margin, explicit leverage caps (venue max resolved at startup in `exchange/leverage.py`), and one net open position total. OKX SWAP instruments map internal `BTC/USDT` → `BTC-USDT-SWAP`; `POSITION_USDT * LEVERAGE` is target notional, converted to contract size via OKX instrument metadata.

## Deployment & gotchas

- **The VPS is the sole live instance.** It runs under pm2 as app `ai-perp-volume` (host `0xkayz@103.127.98.103`, dir `/home/0xkayz/ai-perpetual-trading`). Never start a second live run — duplicate OKX orders / Supabase writes. The bot logs to **stderr**, so live logs are in pm2's *error* log (`~/.pm2/logs/ai-perp-volume-error.log`), not the out log.
- **TradingView relay must be running** or every fetch fails. The bot reads `TRADINGVIEW_SIGNAL_URL` (default `http://127.0.0.1:8787/tradingview/latest`); if the relay process is down the feed 404s/refuses, and with the current entry prompt ("reduce confidence when a provider is empty" + `never_trade_if_data_is_missing_or_unclear`) a missing TradingView signal pushes the AI toward HOLD on every tick.
- `VOLUME_BREAKEVEN_EXIT_ENABLED` must stay `false` in `ai_volume`: enabling it closes winners near breakeven before the confluence TP plays out (gross/fee collapses).
- External signals (CMC/Dune/TradingView) are throttled by `EXTERNAL_SIGNAL_CACHE_SECONDS` (default 300) to avoid burning Dune credits during position monitoring.
- Structural reality to keep in mind when tuning: taker fee ~0.05% per side is locked, so the strategy's edge must clear round-trip fees to be net-positive. Validate any "profitable" claim against closed trades in Supabase, not against assumptions or backtests (the AI path can't be backtested).
