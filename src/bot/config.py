from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from bot.core.entry_window import format_weekly_window, parse_weekly_window
from bot.exchange.bingx_client import (
    BINGX_VST_BASE_URL,
    BINGX_VST_FALLBACK_BASE_URL,
    REQUIRED_SOURCE_KEY as BINGX_REQUIRED_SOURCE_KEY,
)
from bot.volume import VolumeBuildProfile

# `.com` is the mandatory primary host. `.pro` is deliberately NOT configurable:
# the documented policy allows it only as an internal fallback after a network
# error or timeout, so accepting it here would let config skip the primary.
BINGX_VST_PRIMARY_HOST = BINGX_VST_BASE_URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # BingX Perpetual Futures VST is the only enabled execution environment.
    # Live endpoints fail closed (see `validate_bingx_vst_only`).
    bingx_api_key: str = Field(alias="BINGX_API_KEY")
    bingx_secret_key: str = Field(alias="BINGX_SECRET_KEY")
    bingx_env: str = Field(default="vst", alias="BINGX_ENV")
    bingx_base_url: str = Field(default=BINGX_VST_BASE_URL, alias="BINGX_BASE_URL")
    bingx_margin_mode: str = Field(default="isolated", alias="BINGX_MARGIN_MODE")
    bingx_position_mode: str = Field(default="hedge", alias="BINGX_POSITION_MODE")
    # BingX documents recvWindow as "max 5000" ms; a larger value is rejected.
    bingx_recv_window_ms: int = Field(default=5000, ge=1000, le=5000, alias="BINGX_RECV_WINDOW_MS")
    # BingX requires the exact X-SOURCE-KEY value BX-AI-SKILL on every request.
    bingx_source_key: str = Field(default=BINGX_REQUIRED_SOURCE_KEY, alias="BINGX_SOURCE_KEY")
    bingx_route_min_interval_seconds: float = Field(
        default=0.2,
        ge=0.0,
        alias="BINGX_ROUTE_MIN_INTERVAL_SECONDS",
    )
    bingx_market_cache_seconds: float = Field(default=1.0, ge=0.0, alias="BINGX_MARKET_CACHE_SECONDS")
    bingx_insecure_ssl: bool = Field(default=False, alias="BINGX_INSECURE_SSL")
    bingx_debug_http: bool = Field(default=False, alias="BINGX_DEBUG_HTTP")
    strategy_mode: str = Field(default="grok", alias="STRATEGY_MODE")

    symbols: Annotated[tuple[str, ...], NoDecode] = Field(
        default=("BTC/USDT", "ETH/USDT"),
        alias="SYMBOLS",
    )
    decision_timeframes: Annotated[tuple[str, ...], NoDecode] = Field(
        default=("1m", "3m", "5m"),
        alias="DECISION_TIMEFRAMES",
    )
    trade_target_per_day: int = Field(default=28, alias="TRADE_TARGET_PER_DAY")
    max_stop_loss_per_day: int = Field(default=2, alias="MAX_STOP_LOSS_PER_DAY")
    leverage: int = Field(default=100, ge=1, le=200, alias="LEVERAGE")
    position_usdt: float = Field(default=180.0, alias="POSITION_USDT")
    volume_margin_per_entry_usdt: float | None = Field(default=None, gt=0.0, alias="VOLUME_MARGIN_PER_ENTRY_USDT")
    volume_sides_per_entry: int = Field(default=2, ge=1, le=4, alias="VOLUME_SIDES_PER_ENTRY")
    volume_execution_mode: str = Field(default="market", alias="VOLUME_EXECUTION_MODE")
    volume_attempt_target_per_day: int = Field(default=34, ge=1, alias="VOLUME_ATTEMPT_TARGET_PER_DAY")
    volume_min_filled_entries_per_day: int = Field(default=28, ge=1, alias="VOLUME_MIN_FILLED_ENTRIES_PER_DAY")
    volume_target_usdt: float = Field(default=30_000_000.0, ge=0.0, alias="VOLUME_TARGET_USDT")
    volume_daily_target_usdt: float = Field(default=1_000_000.0, ge=0.0, alias="VOLUME_DAILY_TARGET_USDT")
    volume_cycle_started_at: datetime | None = Field(default=None, alias="VOLUME_CYCLE_STARTED_AT")
    volume_neutral_exit_enabled: bool = Field(default=False, alias="VOLUME_NEUTRAL_EXIT_ENABLED")
    volume_neutral_exit_min_seconds: int = Field(default=5, ge=0, alias="VOLUME_NEUTRAL_EXIT_MIN_SECONDS")
    volume_neutral_exit_min_net_usdt: float = Field(default=0.0, ge=0.0, alias="VOLUME_NEUTRAL_EXIT_MIN_NET_USDT")
    volume_neutral_exit_band_usdt: float = Field(default=5.0, ge=0.0, alias="VOLUME_NEUTRAL_EXIT_BAND_USDT")
    volume_neutral_exit_mode: str = Field(default="window", alias="VOLUME_NEUTRAL_EXIT_MODE")
    volume_neutral_exit_trail_usdt: float = Field(default=0.0, ge=0.0, alias="VOLUME_NEUTRAL_EXIT_TRAIL_USDT")
    # net: thresholds apply to PnL after both taker fees. gross: to the unrealised PnL
    # before fees, which is the number the BingX position screen shows.
    volume_neutral_exit_basis: str = Field(default="net", alias="VOLUME_NEUTRAL_EXIT_BASIS")
    # Seconds between position status lines reconciling the BingX view with net. 0 = off.
    position_status_log_seconds: float = Field(default=60.0, ge=0.0, alias="POSITION_STATUS_LOG_SECONDS")
    # How long to wait before offering a refused exit to the venue again. The
    # position stays monitored throughout; this only spaces the retries.
    exit_rejection_retry_seconds: float = Field(default=15.0, ge=0.0, alias="EXIT_REJECTION_RETRY_SECONDS")
    volume_breakeven_exit_enabled: bool = Field(default=False, alias="VOLUME_BREAKEVEN_EXIT_ENABLED")
    volume_breakeven_min_seconds: int = Field(default=5, ge=0, alias="VOLUME_BREAKEVEN_MIN_SECONDS")
    volume_breakeven_max_loss_usdt: float = Field(default=1.0, ge=0.0, alias="VOLUME_BREAKEVEN_MAX_LOSS_USDT")
    volume_breakeven_min_profit_usdt: float = Field(default=0.0, ge=0.0, alias="VOLUME_BREAKEVEN_MIN_PROFIT_USDT")
    volume_stop_at_target_enabled: bool = Field(default=True, alias="VOLUME_STOP_AT_TARGET_ENABLED")
    volume_daily_fee_budget_usdt: float = Field(default=0.0, ge=0.0, alias="VOLUME_DAILY_FEE_BUDGET_USDT")
    trading_timezone: str = Field(default="Asia/Jakarta", alias="TRADING_TIMEZONE")

    entry_interval_minutes: int = Field(default=3, alias="ENTRY_INTERVAL_MINUTES")
    position_hold_minutes: int = Field(default=60, alias="POSITION_HOLD_MINUTES")
    stop_loss_pct: float = Field(default=0.35, alias="STOP_LOSS_PCT")
    take_profit_pct: float = Field(default=0.7, alias="TAKE_PROFIT_PCT")
    # Hard ceiling on the ATR-scaled stop. 0 disables it. Without a ceiling the
    # stop grows with volatility while profit taking stays pinned, so a volatile
    # entry can lose several winners' worth in one move.
    max_stop_loss_pct: float = Field(default=0.0, ge=0.0, alias="MAX_STOP_LOSS_PCT")
    entry_stretch_gate_mode: str = Field(default="off", alias="ENTRY_STRETCH_GATE_MODE")
    entry_stretch_gate_max_atr: float = Field(default=0.75, ge=0.0, alias="ENTRY_STRETCH_GATE_MAX_ATR")
    entry_stretch_gate_behind_schedule_entries_per_hour: float = Field(
        default=0.0,
        ge=0.0,
        alias="ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR",
    )
    # Weekly window for NEW entries in the trading timezone, e.g. "mon 07:00-sat 00:00".
    # Empty = always open. Open positions are still managed outside the window.
    entry_weekly_window: str = Field(default="", alias="ENTRY_WEEKLY_WINDOW")
    # Which side the entry gate lets through while it is on. "any" keeps both sides.
    entry_gate_direction: str = Field(default="any", alias="ENTRY_GATE_DIRECTION")
    # Regime gate of the LONG forward bot (bot/core/regime_gate.py). "off" makes no venue call and
    # changes nothing; "naik" lets an entry through only while the hourly mode selector reads NAIK.
    entry_regime_gate: str = Field(default="off", alias="ENTRY_REGIME_GATE")
    # Volatility guard: skip entries while ATR14 (1h) exceeds this multiple of its 720-hour median,
    # and while that median is not yet known. 0 turns the guard off. Read only with the regime gate on.
    entry_volatility_guard_mult: float = Field(default=0.0, ge=0.0, alias="ENTRY_VOLATILITY_GUARD_MULT")
    # The selector's sideways band, in ATR (config/modes.yaml pemilih_mode.sideways_jarak_kali_atr).
    regime_sideways_atr_mult: float = Field(default=0.2, gt=0.0, alias="REGIME_SIDEWAYS_ATR_MULT")
    # Entry payload shape: "legacy" is the full context the bot has always sent; "lean" is the compact
    # per-setup feature block of the staged live-bot fixes (docs/superpowers/specs/2026-10-21-*).
    ai_entry_payload: str = Field(default="legacy", alias="AI_ENTRY_PAYLOAD")
    # Setup ledger: shadow records every candidate's forward outcome; enforce also vetoes a setup whose
    # upper bound (mean + 1.28 x SE of gross bps per trade, before fees) stays below the hurdle. "off" changes nothing. A mode other
    # than off needs a regime gate and a timezone-aware SETUP_LEDGER_SINCE (the first row it may count).
    setup_ledger_mode: str = Field(default="off", alias="SETUP_LEDGER_MODE")
    setup_ledger_since: datetime | None = Field(default=None, alias="SETUP_LEDGER_SINCE")
    setup_ledger_min_samples: int = Field(default=60, ge=2, alias="SETUP_LEDGER_MIN_SAMPLES")
    setup_ledger_hurdle_bps: float = Field(default=4.0, alias="SETUP_LEDGER_HURDLE_BPS")
    # Live dashboard feed (src/bot/telemetry). Read-only and off by default. The
    # listener refuses any non-loopback host: reach it through an SSH tunnel.
    telemetry_enabled: bool = Field(default=False, alias="TELEMETRY_ENABLED")
    telemetry_host: str = Field(default="127.0.0.1", alias="TELEMETRY_HOST")
    telemetry_port: int = Field(default=3000, ge=1, le=65535, alias="TELEMETRY_PORT")
    telemetry_allowed_origin: str = Field(default="http://localhost:3001", alias="TELEMETRY_ALLOWED_ORIGIN")
    telemetry_price_interval_seconds: float = Field(
        default=5.0,
        ge=1.0,
        le=60.0,
        alias="TELEMETRY_PRICE_INTERVAL_SECONDS",
    )
    # Live dashboard prices from BingX's public VST websocket, about every 0.4s instead of every
    # TELEMETRY_PRICE_INTERVAL_SECONDS. Off by default; polling stays on as the fallback either way.
    telemetry_price_stream: bool = Field(default=False, alias="TELEMETRY_PRICE_STREAM")
    # Extra symbols the dashboard shows (price and candles only). Display-only: nothing that
    # trades reads it. Empty = the traded SYMBOLS; see `telemetry_display_symbols`.
    telemetry_symbols: Annotated[tuple[str, ...], NoDecode] = Field(default=(), alias="TELEMETRY_SYMBOLS")
    # Selective confluence entry (validated on 3m/5m; 1m is too noisy).
    confluence_enabled: bool = Field(default=True, alias="CONFLUENCE_ENABLED")
    confluence_min_agreement: float = Field(default=0.55, ge=0.0, le=1.0, alias="CONFLUENCE_MIN_AGREEMENT")
    confluence_min_atr_pct: float = Field(default=0.05, ge=0.0, alias="CONFLUENCE_MIN_ATR_PCT")
    confluence_min_score: float = Field(default=0.05, ge=0.0, alias="CONFLUENCE_MIN_SCORE")
    confluence_atr_tp_mult: float = Field(default=3.0, gt=0.0, alias="CONFLUENCE_ATR_TP_MULT")
    confluence_atr_sl_mult: float = Field(default=1.0, gt=0.0, alias="CONFLUENCE_ATR_SL_MULT")
    taker_fee_pct: float = Field(default=0.05, alias="TAKER_FEE_PCT")
    maker_fee_pct: float = Field(default=0.02, alias="MAKER_FEE_PCT")
    market_poll_seconds: int = Field(default=5, alias="MARKET_POLL_SECONDS")
    order_fill_wait_seconds: int = Field(default=3, alias="ORDER_FILL_WAIT_SECONDS")
    use_exchange_fee_rates: bool = Field(default=True, alias="USE_EXCHANGE_FEE_RATES")
    execution_guards_enabled: bool = Field(default=True, alias="EXECUTION_GUARDS_ENABLED")
    execution_entry_order_type: str = Field(default="market", alias="EXECUTION_ENTRY_ORDER_TYPE")
    execution_post_only_timeout_seconds: float = Field(
        default=3.0,
        ge=0.5,
        alias="EXECUTION_POST_ONLY_TIMEOUT_SECONDS",
    )
    execution_post_only_max_reprices: int = Field(
        default=5,
        ge=0,
        le=20,
        alias="EXECUTION_POST_ONLY_MAX_REPRICES",
    )
    execution_post_only_fallback_to_market: bool = Field(
        default=False,
        alias="EXECUTION_POST_ONLY_FALLBACK_TO_MARKET",
    )
    maker_entry_ttl_seconds: float = Field(default=3.0, ge=0.5, alias="MAKER_ENTRY_TTL_SECONDS")
    maker_exit_ttl_seconds: float = Field(default=3.0, ge=0.5, alias="MAKER_EXIT_TTL_SECONDS")
    maker_max_reprices: int = Field(default=5, ge=0, le=20, alias="MAKER_MAX_REPRICES")
    maker_allow_taker_emergency_exit: bool = Field(default=True, alias="MAKER_ALLOW_TAKER_EMERGENCY_EXIT")
    orderbook_depth_levels: int = Field(default=20, ge=1, le=400, alias="ORDERBOOK_DEPTH_LEVELS")
    min_orderbook_depth_multiplier: float = Field(default=1.2, ge=0.0, alias="MIN_ORDERBOOK_DEPTH_MULTIPLIER")
    max_abs_funding_rate: float = Field(default=0.001, ge=0.0, alias="MAX_ABS_FUNDING_RATE")
    max_spread_bps: float = Field(default=6.0, ge=0.0, alias="MAX_SPREAD_BPS")
    funding_blackout_minutes: int = Field(default=10, ge=0, alias="FUNDING_BLACKOUT_MINUTES")
    hard_max_leverage: int = Field(default=100, ge=1, le=200, alias="HARD_MAX_LEVERAGE")
    venue_max_leverage: int | None = Field(default=100, ge=1, le=200, alias="VENUE_MAX_LEVERAGE")

    # Dynamic risk management.
    account_balance_usdt: float = Field(default=60.0, alias="ACCOUNT_BALANCE_USDT")
    max_risk_pct_per_trade: float = Field(default=0.5, alias="MAX_RISK_PCT_PER_TRADE")
    atr_sl_multiplier: float = Field(default=1.5, alias="ATR_SL_MULTIPLIER")
    atr_tp_multiplier: float = Field(default=3.0, alias="ATR_TP_MULTIPLIER")
    entry_guard_long_rsi_max: float = Field(default=76.0, ge=0.0, le=100.0, alias="ENTRY_GUARD_LONG_RSI_MAX")
    entry_guard_short_rsi_min: float = Field(default=24.0, ge=0.0, le=100.0, alias="ENTRY_GUARD_SHORT_RSI_MIN")
    cooldown_minutes_after_entry: int = Field(default=10, alias="COOLDOWN_MINUTES_AFTER_ENTRY")
    cooldown_minutes_after_stop: int = Field(default=20, alias="COOLDOWN_MINUTES_AFTER_STOP")
    # Pause after a close of any reason (stop, neutral exit, AI exit, time stop). 0 = off.
    cooldown_minutes_after_exit: int = Field(default=0, ge=0, alias="COOLDOWN_MINUTES_AFTER_EXIT")
    # LONG forward bot self-stop (0 = off). From FORWARD_KILL_AFTER_POSITIONS closed positions, entries latch
    # off while the pooled result is below FORWARD_KILL_BELOW_PER_1M per 1M; FORWARD_REVIEW_AT_POSITIONS
    # latches them whatever the result, so the manager decides.
    forward_kill_after_positions: int = Field(default=0, ge=0, alias="FORWARD_KILL_AFTER_POSITIONS")
    forward_kill_below_per_1m: float = Field(default=-500.0, alias="FORWARD_KILL_BELOW_PER_1M")
    forward_review_at_positions: int = Field(default=0, ge=0, alias="FORWARD_REVIEW_AT_POSITIONS")
    max_daily_drawdown_usdt: float = Field(default=100.0, alias="MAX_DAILY_DRAWDOWN_USDT")
    max_consecutive_losses: int = Field(default=3, alias="MAX_CONSECUTIVE_LOSSES")
    prevent_new_entries_if_exchange_position_open: bool = Field(
        default=True,
        alias="PREVENT_NEW_ENTRIES_IF_EXCHANGE_POSITION_OPEN",
    )

    # Layer 1: OpenAI-compatible AI brain.
    xai_api_key: str | None = Field(default=None, alias="XAI_API_KEY")
    xai_base_url: str = Field(default="https://api.freemodel.dev/v1", alias="XAI_BASE_URL")
    xai_model: str = Field(default="gpt-5.4", alias="XAI_MODEL")
    xai_reasoning_effort: str = Field(default="high", alias="XAI_REASONING_EFFORT")
    xai_timeout_seconds: int = Field(default=90, alias="XAI_TIMEOUT_SECONDS")
    # "responses" = xAI /responses w/ json_schema; "chat" = OpenAI /chat/completions w/ json_object;
    # "messages" = Anthropic-style /messages (e.g. deepseek-v4-flash on openmodel.ai), schema in prompt.
    xai_api_style: str = Field(default="chat", alias="XAI_API_STYLE")
    # Required by the Anthropic /messages dialect; reasoning models also spend it on thinking.
    xai_max_tokens: int = Field(default=4096, alias="XAI_MAX_TOKENS")
    ai_kline_interval: str = Field(default="3m", alias="AI_KLINE_INTERVAL")
    ai_kline_limit: int = Field(default=120, alias="AI_KLINE_LIMIT")
    ai_recent_performance_limit: int = Field(default=20, ge=1, le=100, alias="AI_RECENT_PERFORMANCE_LIMIT")
    ai_trade_memory_limit: int = Field(default=40, ge=1, le=200, alias="AI_TRADE_MEMORY_LIMIT")
    ai_min_confidence: float = Field(default=0.75, ge=0.75, le=1.0, alias="AI_MIN_CONFIDENCE")
    ai_min_setup_score: float = Field(default=0.58, ge=0.0, le=1.0, alias="AI_MIN_SETUP_SCORE")
    ai_min_fee_adjusted_rr: float = Field(default=1.35, ge=0.1, alias="AI_MIN_FEE_ADJUSTED_RR")
    ai_systemic_risk_no_trade_threshold: int = Field(
        default=4,
        ge=1,
        le=5,
        alias="AI_SYSTEMIC_RISK_NO_TRADE_THRESHOLD",
    )
    external_signal_cache_seconds: int = Field(default=300, alias="EXTERNAL_SIGNAL_CACHE_SECONDS")
    ai_exit_check_interval_seconds: int = Field(
        default=30,
        ge=5,
        alias="AI_EXIT_CHECK_INTERVAL_SECONDS",
    )
    ai_exit_price_move_threshold_pct: float = Field(
        default=0.08,
        ge=0.0,
        alias="AI_EXIT_PRICE_MOVE_THRESHOLD_PCT",
    )
    # A favourable price move since the last AI exit call, in percent, that asks the
    # model again. 0 keeps the old behaviour where only adverse moves trigger a call.
    ai_exit_favorable_move_threshold_pct: float = Field(
        default=0.0,
        ge=0.0,
        alias="AI_EXIT_FAVORABLE_MOVE_THRESHOLD_PCT",
    )
    # Calls at the end of each trade's budget that only price moves may spend, so
    # routine candle and flow triggers cannot exhaust the budget early. 0 = off.
    ai_exit_event_reserve_calls: int = Field(
        default=0,
        ge=0,
        le=200,
        alias="AI_EXIT_EVENT_RESERVE_CALLS",
    )
    # Seconds a position must be held before the AI exit evaluator is consulted. In
    # the first minutes net is already about -18 from fees, so the model reads
    # "underwater" and cuts noise: exits within 3 minutes lost 98.76 over 8 trades
    # versus deterministic exits, later ones saved 282.89 over 24. 0 = off.
    ai_exit_min_hold_seconds: int = Field(
        default=0,
        ge=0,
        le=3600,
        alias="AI_EXIT_MIN_HOLD_SECONDS",
    )
    ai_exit_max_calls_per_trade: int = Field(
        default=12,
        ge=0,
        le=200,
        alias="AI_EXIT_MAX_CALLS_PER_TRADE",
    )
    # Loss beyond which an AI close_now is honoured on a losing position. 0 keeps
    # the old behaviour where only a profitable trade may be closed by the AI.
    ai_exit_adverse_cut_usdt: float = Field(
        default=0.0,
        ge=0.0,
        alias="AI_EXIT_ADVERSE_CUT_USDT",
    )
    # Compact AI payload shaping (see bot/ai/compact_context.py).
    ai_compact_max_factors_per_symbol: int = Field(
        default=6,
        ge=1,
        le=30,
        alias="AI_COMPACT_MAX_FACTORS_PER_SYMBOL",
    )
    ai_compact_max_flow_signals: int = Field(default=5, ge=0, le=20, alias="AI_COMPACT_MAX_FLOW_SIGNALS")
    ai_compact_max_memory_lessons: int = Field(default=3, ge=0, le=20, alias="AI_COMPACT_MAX_MEMORY_LESSONS")
    # Optional pricing so AI call telemetry can report an estimated cost.
    ai_input_cost_per_million_tokens: float | None = Field(
        default=None,
        ge=0.0,
        alias="AI_INPUT_COST_PER_MILLION_TOKENS",
    )
    ai_output_cost_per_million_tokens: float | None = Field(
        default=None,
        ge=0.0,
        alias="AI_OUTPUT_COST_PER_MILLION_TOKENS",
    )

    # Backend-only arena features inspired by Hyper-Alpha-Arena.
    arena_factors_enabled: bool = Field(default=True, alias="ARENA_FACTORS_ENABLED")
    arena_flow_signals_enabled: bool = Field(default=True, alias="ARENA_FLOW_SIGNALS_ENABLED")
    arena_volume_spike_pct: float = Field(default=65.0, alias="ARENA_VOLUME_SPIKE_PCT")
    arena_volume_collapse_pct: float = Field(default=-70.0, alias="ARENA_VOLUME_COLLAPSE_PCT")
    arena_price_momentum_bps: float = Field(default=55.0, ge=0.0, alias="ARENA_PRICE_MOMENTUM_BPS")
    arena_open_interest_surge_pct: float = Field(default=7.5, ge=0.0, alias="ARENA_OPEN_INTEREST_SURGE_PCT")
    arena_funding_extreme_abs: float = Field(default=0.001, ge=0.0, alias="ARENA_FUNDING_EXTREME_ABS")
    arena_factor_score_trigger: float = Field(default=0.75, ge=0.0, le=1.0, alias="ARENA_FACTOR_SCORE_TRIGGER")
    arena_factor_percentile_trigger: float = Field(
        default=0.90,
        ge=0.5,
        le=1.0,
        alias="ARENA_FACTOR_PERCENTILE_TRIGGER",
    )
    arena_order_flow_imbalance_pct: float = Field(default=25.0, ge=0.0, alias="ARENA_ORDER_FLOW_IMBALANCE_PCT")
    arena_min_depth_notional: float = Field(default=25_000.0, ge=0.0, alias="ARENA_MIN_DEPTH_NOTIONAL")
    arena_program_trader_enabled: bool = Field(default=False, alias="ARENA_PROGRAM_TRADER_ENABLED")
    arena_attribution_lookback_trades: int = Field(default=100, ge=1, alias="ARENA_ATTRIBUTION_LOOKBACK_TRADES")
    arena_diagnosis_enabled: bool = Field(default=True, alias="ARENA_DIAGNOSIS_ENABLED")

    # Layer 3 external market signals.
    coinmarketcap_api_key: str | None = Field(default=None, alias="COINMARKETCAP_API_KEY")
    coinmarketcap_base_url: str = Field(
        default="https://pro-api.coinmarketcap.com",
        alias="COINMARKETCAP_BASE_URL",
    )
    dune_api_key: str | None = Field(default=None, alias="DUNE_API_KEY")
    dune_base_url: str = Field(default="https://api.dune.com/api/v1", alias="DUNE_BASE_URL")
    dune_query_ids: Annotated[tuple[str, ...], NoDecode] = Field(default=(), alias="DUNE_QUERY_IDS")
    dune_result_limit: int = Field(default=25, alias="DUNE_RESULT_LIMIT")
    dune_use_builtin_signals: bool = Field(default=True, alias="DUNE_USE_BUILTIN_SIGNALS")
    dune_performance_tier: str = Field(default="medium", alias="DUNE_PERFORMANCE_TIER")
    dune_execution_poll_seconds: float = Field(default=2.0, alias="DUNE_EXECUTION_POLL_SECONDS")
    dune_execution_timeout_seconds: int = Field(default=45, alias="DUNE_EXECUTION_TIMEOUT_SECONDS")
    tradingview_signal_url: str | None = Field(default=None, alias="TRADINGVIEW_SIGNAL_URL")
    tradingview_signal_token: str | None = Field(default=None, alias="TRADINGVIEW_SIGNAL_TOKEN")

    # Optional Supabase mirror for learning/event tables.
    supabase_url: str | None = Field(default=None, alias="SUPABASE_URL")
    supabase_service_role_key: str | None = Field(default=None, alias="SUPABASE_SERVICE_ROLE_KEY")
    supabase_schema: str = Field(default="public", alias="SUPABASE_SCHEMA")
    supabase_trades_table: str = Field(default="trades", alias="SUPABASE_TRADES_TABLE")
    supabase_ai_decisions_table: str = Field(
        default="ai_decisions",
        alias="SUPABASE_AI_DECISIONS_TABLE",
    )
    supabase_ai_exit_verdicts_table: str = Field(
        default="ai_exit_verdicts",
        alias="SUPABASE_AI_EXIT_VERDICTS_TABLE",
    )
    supabase_ai_call_metrics_table: str = Field(
        default="ai_call_metrics",
        alias="SUPABASE_AI_CALL_METRICS_TABLE",
    )
    supabase_open_positions_table: str = Field(
        default="open_position_state",
        alias="SUPABASE_OPEN_POSITIONS_TABLE",
    )

    request_timeout_seconds: int = Field(default=20, alias="REQUEST_TIMEOUT_SECONDS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    position_monitor_interval_seconds: int = Field(
        default=5,
        ge=1,
        alias="POSITION_MONITOR_INTERVAL_SECONDS",
    )
    min_order_quantity: float = Field(default=0.0001, alias="MIN_ORDER_QUANTITY")

    @field_validator(
        "xai_api_key",
        "xai_base_url",
        "xai_model",
        mode="before",
    )
    @classmethod
    def strip_optional_strings(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return str(v).strip()

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        return value.upper()

    @field_validator("volume_neutral_exit_mode")
    @classmethod
    def normalize_volume_neutral_exit_mode(cls, value: str) -> str:
        mode = str(value or "window").strip().lower()
        if mode not in {"window", "floor", "trailing"}:
            raise ValueError("VOLUME_NEUTRAL_EXIT_MODE must be one of: window, floor, trailing.")
        return mode

    @field_validator("volume_neutral_exit_basis")
    @classmethod
    def normalize_volume_neutral_exit_basis(cls, value: str) -> str:
        basis = str(value or "net").strip().lower()
        if basis not in {"net", "gross"}:
            raise ValueError("VOLUME_NEUTRAL_EXIT_BASIS must be one of: net, gross.")
        return basis

    @field_validator("entry_stretch_gate_mode")
    @classmethod
    def normalize_entry_stretch_gate_mode(cls, value: str) -> str:
        mode = str(value or "off").strip().lower()
        if mode not in {"off", "shadow", "enforce"}:
            raise ValueError("ENTRY_STRETCH_GATE_MODE must be one of: off, shadow, enforce.")
        return mode

    @field_validator("entry_weekly_window")
    @classmethod
    def normalize_entry_weekly_window(cls, value: str) -> str:
        return format_weekly_window(parse_weekly_window(value))

    @field_validator("entry_gate_direction")
    @classmethod
    def normalize_entry_gate_direction(cls, value: str) -> str:
        direction = str(value or "any").strip().lower()
        if direction not in {"any", "long", "short"}:
            raise ValueError("ENTRY_GATE_DIRECTION must be one of: any, long, short.")
        return direction

    @field_validator("entry_regime_gate")
    @classmethod
    def normalize_entry_regime_gate(cls, value: str) -> str:
        mode = str(value or "off").strip().lower()
        if mode not in {"off", "naik", "map"}:
            raise ValueError("ENTRY_REGIME_GATE must be one of: off, naik, map.")
        return mode

    @field_validator("ai_entry_payload")
    @classmethod
    def normalize_ai_entry_payload(cls, value: str) -> str:
        payload = str(value or "legacy").strip().lower()
        if payload not in {"legacy", "lean"}:
            raise ValueError("AI_ENTRY_PAYLOAD must be one of: legacy, lean.")
        return payload

    @field_validator("setup_ledger_mode")
    @classmethod
    def normalize_setup_ledger_mode(cls, value: str) -> str:
        mode = str(value or "off").strip().lower()
        if mode not in {"off", "shadow", "enforce"}:
            raise ValueError("SETUP_LEDGER_MODE must be one of: off, shadow, enforce.")
        return mode

    @field_validator("telemetry_host")
    @classmethod
    def require_loopback_telemetry_host(cls, value: str) -> str:
        host = str(value or "").strip().lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "TELEMETRY_HOST must be a loopback address (127.0.0.1, localhost or ::1). "
                "Reach the dashboard feed through an SSH tunnel instead of exposing it."
            )
        return host

    @field_validator("telemetry_allowed_origin")
    @classmethod
    def require_bare_telemetry_origin(cls, value: str) -> str:
        origin = str(value or "").strip()
        parts = urlsplit(origin)
        bare = (
            parts.scheme in {"http", "https"}
            and bool(parts.netloc)
            and not any(ch.isspace() for ch in origin)
            and not parts.path
            and not parts.query
            and not parts.fragment
        )
        if not bare:
            raise ValueError(
                "TELEMETRY_ALLOWED_ORIGIN must be one bare origin such as http://localhost:3001 "
                "(no path, no trailing slash, no wildcard)."
            )
        return origin

    @field_validator("bingx_api_key", "bingx_secret_key")
    @classmethod
    def require_non_empty_secret(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Required credential is empty. Fill the matching key in .env.")
        return value.strip()

    @field_validator("symbols", "telemetry_symbols", mode="before")
    @classmethod
    def parse_symbols(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            items = [item.strip().upper().replace("-", "/") for item in value.split(",")]
            return tuple(item for item in items if item)
        if isinstance(value, (list, tuple)):
            return tuple(str(item).strip().upper().replace("-", "/") for item in value if str(item).strip())
        return value

    @field_validator("venue_max_leverage", mode="before")
    @classmethod
    def parse_optional_int(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        return int(value)

    @field_validator(
        "volume_margin_per_entry_usdt",
        "ai_input_cost_per_million_tokens",
        "ai_output_cost_per_million_tokens",
        mode="before",
    )
    @classmethod
    def parse_optional_float(cls, value: Any) -> float | None:
        if value is None or value == "":
            return None
        return float(value)

    @field_validator("volume_cycle_started_at", "setup_ledger_since", mode="before")
    @classmethod
    def parse_optional_datetime(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("volume_cycle_started_at")
    @classmethod
    def require_timezone_aware_volume_cycle_start(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("VOLUME_CYCLE_STARTED_AT must be a timezone-aware ISO-8601 timestamp")
        normalized = value.astimezone(timezone.utc)
        if normalized > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("VOLUME_CYCLE_STARTED_AT cannot be more than 5 minutes in the future")
        return normalized

    @field_validator("setup_ledger_since")
    @classmethod
    def require_timezone_aware_setup_ledger_since(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SETUP_LEDGER_SINCE must be a timezone-aware ISO-8601 timestamp")
        return value.astimezone(timezone.utc)

    @field_validator("strategy_mode")
    @classmethod
    def normalize_strategy_mode(cls, value: str) -> str:
        out = value.lower().strip()
        if out in {"volume", "volume_builder", "rule"}:
            return "volume"
        if out in {"ai_volume", "volume_ai", "grok_volume", "ai-volume", "volume-ai"}:
            return "ai_volume"
        if out in {"grok", "ai"}:
            return "grok"
        raise ValueError("STRATEGY_MODE must be grok, ai_volume, or volume")

    @field_validator("execution_entry_order_type")
    @classmethod
    def normalize_entry_order_type(cls, value: str) -> str:
        out = value.lower().strip()
        if out not in {"market", "post_only"}:
            raise ValueError("EXECUTION_ENTRY_ORDER_TYPE must be market or post_only")
        return out

    @field_validator("volume_execution_mode")
    @classmethod
    def normalize_volume_execution_mode(cls, value: str) -> str:
        out = value.lower().strip().replace("-", "_")
        if out not in {"maker_only", "hybrid", "market"}:
            raise ValueError("VOLUME_EXECUTION_MODE must be maker_only, hybrid, or market")
        return out

    @field_validator("trading_timezone")
    @classmethod
    def validate_trading_timezone(cls, value: str) -> str:
        out = value.strip()
        try:
            ZoneInfo(out)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown TRADING_TIMEZONE: {out}") from exc
        return out

    @field_validator("decision_timeframes", mode="before")
    @classmethod
    def parse_decision_timeframes(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            return tuple(item.strip().lower() for item in value.split(",") if item.strip())
        if isinstance(value, (list, tuple)):
            return tuple(str(item).strip().lower() for item in value if str(item).strip())
        return value

    @field_validator("dune_query_ids", mode="before")
    @classmethod
    def parse_dune_query_ids(cls, value: Any) -> tuple[str, ...]:
        if value is None or value == "":
            return ()
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        if isinstance(value, (list, tuple)):
            return tuple(str(item).strip() for item in value if str(item).strip())
        return value

    @field_validator("bingx_margin_mode")
    @classmethod
    def normalize_margin_mode(cls, value: str) -> str:
        out = value.lower().strip()
        if out != "isolated":
            raise ValueError("BINGX_MARGIN_MODE must be 'isolated'; cross margin is not allowed.")
        return out

    @field_validator("bingx_position_mode")
    @classmethod
    def normalize_position_mode(cls, value: str) -> str:
        out = value.lower().strip()
        if out not in {"hedge", "one_way"}:
            raise ValueError("BINGX_POSITION_MODE must be 'hedge' or 'one_way'")
        return out

    @field_validator("bingx_source_key")
    @classmethod
    def validate_source_key(cls, value: str) -> str:
        out = value.strip()
        if out != BINGX_REQUIRED_SOURCE_KEY:
            raise ValueError(
                f"BINGX_SOURCE_KEY must be exactly {BINGX_REQUIRED_SOURCE_KEY!r} "
                "(required by BingX on every request, including public endpoints)."
            )
        return out

    @field_validator("bingx_env")
    @classmethod
    def normalize_bingx_env(cls, value: str) -> str:
        out = value.lower().strip()
        if out != "vst":
            raise ValueError("BINGX_ENV must be 'vst'; live BingX trading is not enabled for this runtime.")
        return out

    @field_validator("bingx_base_url")
    @classmethod
    def validate_bingx_vst_only(cls, value: str) -> str:
        out = value.strip().rstrip("/")
        if out != BINGX_VST_PRIMARY_HOST:
            raise ValueError(
                f"BINGX_BASE_URL must be exactly {BINGX_VST_PRIMARY_HOST} — `.com` is the mandatory "
                f"primary host. {BINGX_VST_FALLBACK_BASE_URL} is an internal fallback used only after "
                "a network error or timeout, and live endpoints fail closed."
            )
        return out

    @field_validator("xai_reasoning_effort")
    @classmethod
    def normalize_reasoning_effort(cls, value: str) -> str:
        out = value.lower().strip()
        if out not in {"none", "low", "medium", "high"}:
            raise ValueError("XAI_REASONING_EFFORT must be none, low, medium, or high")
        return out

    @field_validator("decision_timeframes")
    @classmethod
    def validate_decision_timeframes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        allowed = {"1m", "3m", "5m"}
        invalid = [item for item in value if item not in allowed]
        if invalid:
            raise ValueError("DECISION_TIMEFRAMES may only contain 1m, 3m, and 5m")
        return value

    @field_validator("dune_performance_tier")
    @classmethod
    def normalize_dune_performance_tier(cls, value: str) -> str:
        out = value.lower().strip()
        if out not in {"small", "medium", "large"}:
            raise ValueError("DUNE_PERFORMANCE_TIER must be small, medium, or large")
        return out

    @model_validator(mode="after")
    def validate_cross_field_settings(self) -> "Settings":
        if self.strategy_mode in {"grok", "ai_volume"} and not (self.xai_api_key and self.xai_api_key.strip()):
            raise ValueError("XAI_API_KEY is required when STRATEGY_MODE is grok or ai_volume.")
        if self.leverage > self.hard_max_leverage:
            raise ValueError("LEVERAGE cannot exceed HARD_MAX_LEVERAGE.")
        if self.venue_max_leverage is not None and self.leverage > self.venue_max_leverage:
            raise ValueError("LEVERAGE cannot exceed VENUE_MAX_LEVERAGE.")
        if self.volume_execution_mode == "maker_only" and self.execution_post_only_fallback_to_market:
            raise ValueError("EXECUTION_POST_ONLY_FALLBACK_TO_MARKET must be false when VOLUME_EXECUTION_MODE=maker_only.")
        self._require_regime_gate_companions()
        self._require_setup_ledger_companions()
        self._require_lean_payload_companions()
        return self

    def _require_regime_gate_companions(self) -> None:
        """What a regime gate needs to be safe; the side filter lives elsewhere, so demand it.

        Only AIOrchestrator enforces ENTRY_GATE_DIRECTION, and only inside the stretch gate when its
        mode is `enforce`; the deterministic `volume` strategy never reaches it. `naik` means a LONG-only
        bot, so a gate-on .env that inherited another direction would trade SHORTs. `map` picks the side
        from the hourly regime itself (NAIK long, TURUN short), so it accepts any ENTRY_GATE_DIRECTION but
        still needs the enforcing stretch gate and an AI strategy to carry that side. A gate-off Settings
        is not checked at all.
        """
        if self.entry_regime_gate == "off":
            return
        problems = []
        if self.entry_regime_gate == "naik" and self.entry_gate_direction != "long":
            problems.append("ENTRY_GATE_DIRECTION=long")
        if self.entry_stretch_gate_mode != "enforce":
            problems.append("ENTRY_STRETCH_GATE_MODE=enforce")
        if self.strategy_mode not in {"grok", "ai_volume"}:
            problems.append("STRATEGY_MODE=grok or ai_volume")
        if not problems:
            return
        needs = ", ".join(problems)
        if self.entry_regime_gate == "naik":
            raise ValueError(
                "ENTRY_REGIME_GATE is on, which makes this a LONG-only bot; it needs "
                + needs
                + " (the direction filter only runs in an AI strategy with the stretch gate enforcing)."
            )
        raise ValueError(
            "ENTRY_REGIME_GATE=map picks the side from the hourly regime; it needs "
            + needs
            + " (the side filter only runs in an AI strategy with the stretch gate enforcing)."
        )

    def _require_setup_ledger_companions(self) -> None:
        """A ledger that records or vetoes needs a regime gate (its buckets) and a start time."""
        if self.setup_ledger_mode == "off":
            return
        problems = []
        if self.entry_regime_gate not in {"naik", "map"}:
            problems.append("ENTRY_REGIME_GATE=naik or map")
        if self.setup_ledger_since is None:
            problems.append("a timezone-aware SETUP_LEDGER_SINCE")
        if problems:
            raise ValueError("SETUP_LEDGER_MODE is on; it needs " + " and ".join(problems) + ".")

    def _require_lean_payload_companions(self) -> None:
        """What a lean entry payload needs for its stretch rule to be true.

        The lean request tells the model that an entry beyond ENTRY_STRETCH_GATE_MAX_ATR is rejected in code
        and offers only the sides the gate lets through. Both hold only while the stretch gate enforces
        (the side filter is honoured only then, or under a regime gate that already demands it), has a
        limit to enforce, and has no behind-schedule bypass. A legacy Settings is not checked at all.
        """
        if self.ai_entry_payload != "lean":
            return
        problems = []
        if self.entry_stretch_gate_mode != "enforce":
            problems.append("ENTRY_STRETCH_GATE_MODE=enforce")
        if self.entry_stretch_gate_max_atr <= 0:
            problems.append("ENTRY_STRETCH_GATE_MAX_ATR above 0")
        if self.entry_stretch_gate_behind_schedule_entries_per_hour != 0:
            problems.append("ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR=0")
        if problems:
            raise ValueError(
                "AI_ENTRY_PAYLOAD=lean tells the model the stretch rule is enforced in code; it needs "
                + ", ".join(problems)
                + "."
            )

    @property
    def exchange_name(self) -> str:
        """Venue label written to storage, logs, and AI payloads."""
        return "bingx"

    @property
    def telemetry_display_symbols(self) -> tuple[str, ...]:
        """Symbols the dashboard shows: SYMBOLS first, then TELEMETRY_SYMBOLS, without repeats.

        The traded symbols are always included, so a symbol the bot trades can never disappear
        from the dashboard. For the telemetry feed only; trading code must keep reading `symbols`.
        """
        return tuple(dict.fromkeys((*self.symbols, *self.telemetry_symbols)))

    @property
    def margin_mode(self) -> str:
        return self.bingx_margin_mode

    @property
    def position_mode(self) -> str:
        return self.bingx_position_mode

    @property
    def volume_profile(self) -> VolumeBuildProfile:
        return VolumeBuildProfile.from_settings(self)
