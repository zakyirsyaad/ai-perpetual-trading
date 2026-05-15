from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


LOG_FIELDS = (
    "timestamp",
    "event",
    "symbol",
    "timeframe",
    "side",
    "confidence",
    "expected_move_bps",
    "spread_bps",
    "entry_price",
    "stop_price",
    "take_price",
    "qty",
    "notional",
    "leverage",
    "margin_used",
    "projected_fee",
    "realized_fee",
    "projected_slippage_bps",
    "realized_slippage_bps",
    "funding_flag",
    "order_type",
    "post_only",
    "reduce_only",
    "client_order_id",
    "exchange_order_id",
    "status",
    "pnl_realized",
    "pnl_unrealized",
    "kill_switch_reason",
    "api_latency_ms",
    "order_ack_latency_ms",
    "websocket_freshness_seconds",
    "position_reconciled",
)


class PerpsJsonLogger:
    """Emit compact JSON-lines records through the repo logger."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def emit(self, event: str, **fields: Any) -> None:
        payload = {key: None for key in LOG_FIELDS}
        payload["timestamp"] = datetime.now(timezone.utc).isoformat()
        payload["event"] = event
        payload.update(fields)
        self._logger.info(json.dumps(_json_ready(payload), separators=(",", ":"), ensure_ascii=True))


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    return value
