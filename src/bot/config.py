from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from bot.volume import VolumeBuildProfile


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    okx_api_key: str = Field(alias="OKX_API_KEY")
    okx_secret_key: str = Field(alias="OKX_SECRET_KEY")
    okx_passphrase: str = Field(alias="OKX_PASSPHRASE")
    okx_base_url: str = Field(default="https://www.okx.com", alias="OKX_BASE_URL")
    okx_demo_trading: bool = Field(default=True, alias="OKX_DEMO_TRADING")
    okx_margin_mode: str = Field(default="isolated", alias="OKX_MARGIN_MODE")
    okx_position_mode: str = Field(default="long_short_mode", alias="OKX_POSITION_MODE")
    okx_instrument_type: str = Field(default="SWAP", alias="OKX_INSTRUMENT_TYPE")
    okx_insecure_ssl: bool = Field(default=False, alias="OKX_INSECURE_SSL")
    okx_debug_http: bool = Field(default=False, alias="OKX_DEBUG_HTTP")
    okx_require_set_leverage: bool = Field(default=False, alias="OKX_REQUIRE_SET_LEVERAGE")
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
    min_order_contracts: float = Field(default=1.0, alias="MIN_ORDER_CONTRACTS")
    volume_margin_per_entry_usdt: float | None = Field(default=None, gt=0.0, alias="VOLUME_MARGIN_PER_ENTRY_USDT")
    volume_sides_per_entry: int = Field(default=2, ge=1, le=4, alias="VOLUME_SIDES_PER_ENTRY")
    volume_execution_mode: str = Field(default="market", alias="VOLUME_EXECUTION_MODE")
    volume_attempt_target_per_day: int = Field(default=34, ge=1, alias="VOLUME_ATTEMPT_TARGET_PER_DAY")
    volume_min_filled_entries_per_day: int = Field(default=28, ge=1, alias="VOLUME_MIN_FILLED_ENTRIES_PER_DAY")
    volume_target_usdt: float = Field(default=30_000_000.0, ge=0.0, alias="VOLUME_TARGET_USDT")
    volume_daily_target_usdt: float = Field(default=1_000_000.0, ge=0.0, alias="VOLUME_DAILY_TARGET_USDT")
    volume_neutral_exit_enabled: bool = Field(default=False, alias="VOLUME_NEUTRAL_EXIT_ENABLED")
    volume_neutral_exit_min_seconds: int = Field(default=5, ge=0, alias="VOLUME_NEUTRAL_EXIT_MIN_SECONDS")
    volume_neutral_exit_min_net_usdt: float = Field(default=0.0, ge=0.0, alias="VOLUME_NEUTRAL_EXIT_MIN_NET_USDT")
    volume_neutral_exit_band_usdt: float = Field(default=5.0, ge=0.0, alias="VOLUME_NEUTRAL_EXIT_BAND_USDT")
    volume_breakeven_exit_enabled: bool = Field(default=False, alias="VOLUME_BREAKEVEN_EXIT_ENABLED")
    volume_breakeven_min_seconds: int = Field(default=5, ge=0, alias="VOLUME_BREAKEVEN_MIN_SECONDS")
    volume_breakeven_max_loss_usdt: float = Field(default=1.0, ge=0.0, alias="VOLUME_BREAKEVEN_MAX_LOSS_USDT")
    volume_breakeven_min_profit_usdt: float = Field(default=0.0, ge=0.0, alias="VOLUME_BREAKEVEN_MIN_PROFIT_USDT")
    volume_stop_at_target_enabled: bool = Field(default=True, alias="VOLUME_STOP_AT_TARGET_ENABLED")
    volume_daily_fee_budget_usdt: float = Field(default=0.0, ge=0.0, alias="VOLUME_DAILY_FEE_BUDGET_USDT")

    entry_interval_minutes: int = Field(default=15, alias="ENTRY_INTERVAL_MINUTES")
    position_hold_minutes: int = Field(default=60, alias="POSITION_HOLD_MINUTES")
    stop_loss_pct: float = Field(default=0.35, alias="STOP_LOSS_PCT")
    take_profit_pct: float = Field(default=0.7, alias="TAKE_PROFIT_PCT")
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
    execution_entry_order_type: str = Field(default="post_only", alias="EXECUTION_ENTRY_ORDER_TYPE")
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
    okx_max_spread_bps: float = Field(default=6.0, ge=0.0, alias="OKX_MAX_SPREAD_BPS")
    okx_funding_blackout_minutes: int = Field(default=10, ge=0, alias="OKX_FUNDING_BLACKOUT_MINUTES")
    okx_min_liquidation_distance_bps: float = Field(
        default=250.0,
        ge=0.0,
        alias="OKX_MIN_LIQUIDATION_DISTANCE_BPS",
    )
    okx_hard_max_leverage: int = Field(default=100, ge=1, le=200, alias="OKX_HARD_MAX_LEVERAGE")
    okx_venue_max_leverage: int | None = Field(default=100, ge=1, le=200, alias="OKX_VENUE_MAX_LEVERAGE")

    # Dynamic risk management.
    account_balance_usdt: float = Field(default=60.0, alias="ACCOUNT_BALANCE_USDT")
    max_risk_pct_per_trade: float = Field(default=0.5, alias="MAX_RISK_PCT_PER_TRADE")
    atr_sl_multiplier: float = Field(default=1.5, alias="ATR_SL_MULTIPLIER")
    atr_tp_multiplier: float = Field(default=3.0, alias="ATR_TP_MULTIPLIER")
    entry_guard_long_rsi_max: float = Field(default=76.0, ge=0.0, le=100.0, alias="ENTRY_GUARD_LONG_RSI_MAX")
    entry_guard_short_rsi_min: float = Field(default=24.0, ge=0.0, le=100.0, alias="ENTRY_GUARD_SHORT_RSI_MIN")
    cooldown_minutes_after_entry: int = Field(default=10, alias="COOLDOWN_MINUTES_AFTER_ENTRY")
    cooldown_minutes_after_stop: int = Field(default=20, alias="COOLDOWN_MINUTES_AFTER_STOP")
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
    # "responses" = xAI /responses w/ json_schema; "chat" = OpenAI /chat/completions w/ json_object.
    xai_api_style: str = Field(default="chat", alias="XAI_API_STYLE")
    ai_kline_interval: str = Field(default="3m", alias="AI_KLINE_INTERVAL")
    ai_kline_limit: int = Field(default=120, alias="AI_KLINE_LIMIT")
    ai_min_confidence: float = Field(default=0.65, alias="AI_MIN_CONFIDENCE")
    ai_min_setup_score: float = Field(default=0.58, ge=0.0, le=1.0, alias="AI_MIN_SETUP_SCORE")
    ai_min_fee_adjusted_rr: float = Field(default=1.35, ge=0.1, alias="AI_MIN_FEE_ADJUSTED_RR")
    ai_systemic_risk_no_trade_threshold: int = Field(
        default=4,
        ge=1,
        le=5,
        alias="AI_SYSTEMIC_RISK_NO_TRADE_THRESHOLD",
    )
    ai_pre_trade_check: bool = Field(default=True, alias="AI_PRE_TRADE_CHECK")
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

    @field_validator("okx_api_key", "okx_secret_key", "okx_passphrase")
    @classmethod
    def require_non_empty_secret(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Required credential is empty. Fill the matching key in .env.")
        return value.strip()

    @field_validator("symbols", mode="before")
    @classmethod
    def parse_symbols(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            items = [item.strip().upper().replace("-", "/") for item in value.split(",")]
            return tuple(item for item in items if item)
        if isinstance(value, (list, tuple)):
            return tuple(str(item).strip().upper().replace("-", "/") for item in value if str(item).strip())
        return value

    @field_validator("okx_venue_max_leverage", mode="before")
    @classmethod
    def parse_optional_int(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        return int(value)

    @field_validator("volume_margin_per_entry_usdt", mode="before")
    @classmethod
    def parse_optional_float(cls, value: Any) -> float | None:
        if value is None or value == "":
            return None
        return float(value)

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

    @field_validator("okx_margin_mode")
    @classmethod
    def normalize_margin_mode(cls, value: str) -> str:
        out = value.lower().strip()
        if out != "isolated":
            raise ValueError("OKX_MARGIN_MODE must be 'isolated'; cross margin is not allowed.")
        return out

    @field_validator("okx_position_mode")
    @classmethod
    def normalize_position_mode(cls, value: str) -> str:
        out = value.lower().strip()
        if out not in {"long_short_mode", "net_mode"}:
            raise ValueError("OKX_POSITION_MODE must be 'long_short_mode' or 'net_mode'")
        return out

    @field_validator("okx_instrument_type")
    @classmethod
    def normalize_instrument_type(cls, value: str) -> str:
        return value.upper().strip()

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
        if self.leverage > self.okx_hard_max_leverage:
            raise ValueError("LEVERAGE cannot exceed OKX_HARD_MAX_LEVERAGE.")
        if self.okx_venue_max_leverage is not None and self.leverage > self.okx_venue_max_leverage:
            raise ValueError("LEVERAGE cannot exceed OKX_VENUE_MAX_LEVERAGE.")
        if self.volume_execution_mode == "maker_only" and self.execution_post_only_fallback_to_market:
            raise ValueError("EXECUTION_POST_ONLY_FALLBACK_TO_MARKET must be false when VOLUME_EXECUTION_MODE=maker_only.")
        return self

    @property
    def volume_profile(self) -> VolumeBuildProfile:
        return VolumeBuildProfile.from_settings(self)
