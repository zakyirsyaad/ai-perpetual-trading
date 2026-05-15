from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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

    # Legacy BingX/paper strategy settings are optional and are not used by the
    # OKX demo runtime in src/main.py.
    bingx_api_key: str | None = Field(default=None, alias="BINGX_API_KEY")
    bingx_secret_key: str | None = Field(default=None, alias="BINGX_SECRET_KEY")
    bingx_base_url: str = Field(default="https://open-api.bingx.com", alias="BINGX_BASE_URL")
    bingx_insecure_ssl: bool = Field(default=False, alias="BINGX_INSECURE_SSL")
    bingx_debug_http: bool = Field(default=False, alias="BINGX_DEBUG_HTTP")
    use_testnet: bool = Field(default=False, alias="USE_TESTNET")
    enable_live_trading: bool = Field(default=False, alias="ENABLE_LIVE_TRADING")
    strategy_mode: str = Field(default="rule", alias="STRATEGY_MODE")

    symbols: Annotated[tuple[str, ...], NoDecode] = Field(
        default=("BTC/USDT", "ETH/USDT"),
        alias="SYMBOLS",
    )
    decision_timeframes: Annotated[tuple[str, ...], NoDecode] = Field(
        default=("1m", "3m", "5m"),
        alias="DECISION_TIMEFRAMES",
    )
    trade_target_per_day: int = Field(default=20, alias="TRADE_TARGET_PER_DAY")
    max_stop_loss_per_day: int = Field(default=2, alias="MAX_STOP_LOSS_PER_DAY")
    leverage: int = Field(default=3, ge=1, le=5, alias="LEVERAGE")
    position_usdt: float = Field(default=20.0, alias="POSITION_USDT")
    min_order_contracts: float = Field(default=1.0, alias="MIN_ORDER_CONTRACTS")

    entry_interval_minutes: int = Field(default=15, alias="ENTRY_INTERVAL_MINUTES")
    position_hold_minutes: int = Field(default=20, alias="POSITION_HOLD_MINUTES")
    stop_loss_pct: float = Field(default=0.35, alias="STOP_LOSS_PCT")
    take_profit_pct: float = Field(default=0.7, alias="TAKE_PROFIT_PCT")
    taker_fee_pct: float = Field(default=0.05, alias="TAKER_FEE_PCT")
    market_poll_seconds: int = Field(default=5, alias="MARKET_POLL_SECONDS")
    order_fill_wait_seconds: int = Field(default=3, alias="ORDER_FILL_WAIT_SECONDS")

    # Dynamic risk management.
    account_balance_usdt: float = Field(default=60.0, alias="ACCOUNT_BALANCE_USDT")
    max_risk_pct_per_trade: float = Field(default=0.5, alias="MAX_RISK_PCT_PER_TRADE")
    atr_sl_multiplier: float = Field(default=1.5, alias="ATR_SL_MULTIPLIER")
    atr_tp_multiplier: float = Field(default=3.0, alias="ATR_TP_MULTIPLIER")
    cooldown_minutes_after_entry: int = Field(default=10, alias="COOLDOWN_MINUTES_AFTER_ENTRY")
    cooldown_minutes_after_stop: int = Field(default=20, alias="COOLDOWN_MINUTES_AFTER_STOP")
    max_daily_drawdown_usdt: float = Field(default=100.0, alias="MAX_DAILY_DRAWDOWN_USDT")
    max_consecutive_losses: int = Field(default=3, alias="MAX_CONSECUTIVE_LOSSES")
    prevent_new_entries_if_exchange_position_open: bool = Field(
        default=True,
        alias="PREVENT_NEW_ENTRIES_IF_EXCHANGE_POSITION_OPEN",
    )

    # Layer 1: xAI Grok brain.
    xai_api_key: str = Field(alias="XAI_API_KEY")
    xai_base_url: str = Field(default="https://api.x.ai/v1", alias="XAI_BASE_URL")
    xai_model: str = Field(default="grok-4.3", alias="XAI_MODEL")
    xai_reasoning_effort: str = Field(default="high", alias="XAI_REASONING_EFFORT")
    xai_timeout_seconds: int = Field(default=90, alias="XAI_TIMEOUT_SECONDS")
    ai_kline_interval: str = Field(default="1m", alias="AI_KLINE_INTERVAL")
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

    # Exchange-agnostic perpetual futures layer defaults.
    perp_exchange_api_name: str | None = Field(default=None, alias="PERP_EXCHANGE_API_NAME")
    perp_daily_fee_budget_pct: float = Field(default=0.03, ge=0.0, alias="PERP_DAILY_FEE_BUDGET_PCT")
    perp_risk_per_trade_pct: float = Field(default=0.0075, ge=0.0, alias="PERP_RISK_PER_TRADE_PCT")
    perp_fee_round_trip: float = Field(default=0.005, gt=0.0, alias="PERP_FEE_ROUND_TRIP")
    perp_slippage_round_trip: float = Field(default=0.0004, ge=0.0, alias="PERP_SLIPPAGE_ROUND_TRIP")
    perp_edge_buffer_bps: float = Field(default=5.0, ge=0.0, alias="PERP_EDGE_BUFFER_BPS")
    perp_leverage_cap: int = Field(default=3, ge=1, le=5, alias="PERP_LEVERAGE_CAP")
    perp_hard_max_leverage: int = Field(default=5, ge=1, le=5, alias="PERP_HARD_MAX_LEVERAGE")
    perp_venue_max_leverage: int | None = Field(default=None, ge=1, le=125, alias="PERP_VENUE_MAX_LEVERAGE")
    perp_max_spread_bps: float = Field(default=6.0, ge=0.0, alias="PERP_MAX_SPREAD_BPS")
    perp_funding_blackout_minutes: int = Field(default=10, ge=0, alias="PERP_FUNDING_BLACKOUT_MINUTES")
    perp_max_daily_realized_drawdown_pct: float = Field(
        default=0.03,
        ge=0.0,
        alias="PERP_MAX_DAILY_REALIZED_DRAWDOWN_PCT",
    )
    perp_max_rolling_drawdown_pct: float = Field(default=0.05, ge=0.0, alias="PERP_MAX_ROLLING_DRAWDOWN_PCT")

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
    db_path: str = Field(default="bot_data/trades.db", alias="DB_PATH")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    position_monitor_interval_seconds: int = Field(
        default=5,
        ge=1,
        alias="POSITION_MONITOR_INTERVAL_SECONDS",
    )
    min_order_quantity: float = Field(default=0.0001, alias="MIN_ORDER_QUANTITY")

    # Backward compatibility (older envs). Prefer XAI_*.
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")

    @field_validator(
        "bingx_api_key",
        "bingx_secret_key",
        "bingx_base_url",
        "xai_api_key",
        "xai_base_url",
        "xai_model",
        "perp_exchange_api_name",
        "openai_api_key",
        "openai_model",
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

    @field_validator("okx_api_key", "okx_secret_key", "okx_passphrase", "xai_api_key")
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

    @field_validator("perp_venue_max_leverage", mode="before")
    @classmethod
    def parse_optional_int(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        return int(value)

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

    @field_validator("perp_leverage_cap")
    @classmethod
    def validate_perp_leverage_cap(cls, value: int) -> int:
        if value > 3:
            raise ValueError("PERP_LEVERAGE_CAP defaults to 3 and must not exceed 3 until venue tiers are known.")
        return value

    @field_validator("dune_performance_tier")
    @classmethod
    def normalize_dune_performance_tier(cls, value: str) -> str:
        out = value.lower().strip()
        if out not in {"small", "medium", "large"}:
            raise ValueError("DUNE_PERFORMANCE_TIER must be small, medium, or large")
        return out

    @property
    def bingx_is_demo_endpoint(self) -> bool:
        return "open-api-vst.bingx.com" in self.bingx_base_url.lower()

    @property
    def database_path(self) -> Path:
        return Path(self.db_path)
