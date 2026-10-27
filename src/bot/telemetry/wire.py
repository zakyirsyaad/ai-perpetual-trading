"""Wire contract for the live dashboard stream.

The dashboard (adapted from aowang-ai/jev-trade, MIT) reads one fixed JSON
shape, so this bot emits that same shape instead of inventing its own. Every
model forbids unknown fields: a payload can only carry what is declared here,
which is the whitelist that keeps secrets, account addresses and raw env values
off the stream.

Python names are snake_case and dump as the camelCase the dashboard reads.
`ai_cost_usd`, `fee_usd` and `fee_sz` keep the dashboard's historical keys
`jevUsd`, `gasUsd` and `gasSz`. `ts` is epoch milliseconds throughout.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_serializer
from pydantic.alias_generators import to_camel

REASON_MAX_CHARS = 300

Side = Literal["buy", "sell"]
PositionSide = Literal["long", "short", "flat"]


class _Wire(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )

    # Keys the TypeScript contract declares optional (`key?:`). They are left out of
    # the wire when None. Every other nullable key is always emitted as an explicit
    # null, exactly as the dashboard expects. Names are camelCase: `to_wire` always
    # serialises by alias.
    _omit_when_none: ClassVar[frozenset[str]] = frozenset()

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        for key in self._omit_when_none:
            if key in data and data[key] is None:
                del data[key]
        return data


class Probabilities(_Wire):
    _omit_when_none: ClassVar[frozenset[str]] = frozenset({"long", "short", "open", "close"})

    buy: float
    sell: float
    hold: float
    long: float | None = None
    short: float | None = None
    open: float | None = None
    close: float | None = None


class Decision(_Wire):
    _omit_when_none: ClassVar[frozenset[str]] = frozenset({"intent", "bias", "leverage", "confidence", "reason"})

    action: Literal["buy", "sell", "hold"]
    intent: Literal["open", "close", "hold"] | None = None
    bias: Literal["long", "short"] | None = None
    leverage: float | None = None
    probabilities: Probabilities
    up_in10: float
    latency_ms: int = Field(ge=0)
    late: bool
    # Extensions beyond the upstream contract. A model's self-reported confidence is
    # not a calibrated probability, so it travels under its own name.
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str | None = Field(default=None, max_length=REASON_MAX_CHARS)


class Quote(_Wire):
    _omit_when_none: ClassVar[frozenset[str]] = frozenset({"reduceOnly", "unchanged", "taker"})

    side: Side
    price: float = Field(ge=0.0)
    size: float = Field(ge=0.0)
    tx_hash: str | None
    cancel: list[int]
    status: Literal["placed", "reverted", "sim"]
    order_id: int | None
    capped: bool
    reduce_only: bool | None = None
    unchanged: bool | None = None
    taker: bool | None = None


class FillData(_Wire):
    _omit_when_none: ClassVar[frozenset[str]] = frozenset({"feeUsd", "closedPnl", "dir"})

    side: Side
    size: float = Field(ge=0.0)
    price: float = Field(ge=0.0)
    tx_hash: str | None
    order_id: int
    simulated: bool
    fee_usd: float | None = None
    closed_pnl: float | None = None
    direction: Literal["open", "close", "flip"] | None = Field(default=None, alias="dir")


class Position(_Wire):
    side: PositionSide
    size: float = Field(ge=0.0)
    entry_price: float | None
    leverage: float | None
    unrealized_usd: float
    unrealized_sz: float


class Totals(_Wire):
    blocks: int = Field(ge=0)
    decisions: int = Field(ge=0)
    quotes: int = Field(ge=0)
    fills: int = Field(ge=0)
    reverted: int = Field(ge=0)
    late_blocks: int = Field(ge=0)
    ai_cost_usd: float = Field(alias="jevUsd")
    fee_sz: float = Field(alias="gasSz")
    fee_usd: float = Field(alias="gasUsd")
    realized_usd: float
    pnl_usd: float
    pnl_sz: float
    pnl_pct: float


class VolumeDay(_Wire):
    """One trading day's traded volume and the running total up to and including it."""

    day: str
    volume_usd: float = Field(ge=0.0)
    cumulative_usd: float = Field(ge=0.0)


class VolumeSummary(_Wire):
    """Volume figures the dashboard reads from `/volume`. Sourced from the trade store, not from memory."""

    today: str
    today_usd: float = Field(ge=0.0)
    daily_target_usd: float = Field(ge=0.0)
    total_usd: float = Field(ge=0.0)
    days: list[VolumeDay]


class UsageDay(_Wire):
    """One trading day of model token spend and the trades it closed (tokens per trade is derived on screen)."""

    day: str
    calls: int = Field(ge=0)
    failed: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    entry_tokens: int = Field(ge=0)
    exit_tokens: int = Field(ge=0)
    closed_trades: int = Field(ge=0)


class UsageSummary(_Wire):
    """Token figures the dashboard reads from `/usage`. Sourced from the trade store, not from memory."""

    today: str
    days: list[UsageDay]


class PnlSummary(_Wire):
    """The header's two figures, taken from the observer's own counters so they exist before the first block does."""

    realized_usd: float
    unrealized_usd: float


class Resting(_Wire):
    bid_sz: float = Field(ge=0.0)
    ask_sz: float = Field(ge=0.0)


class PricePointFill(_Wire):
    _omit_when_none: ClassVar[frozenset[str]] = frozenset({"dir", "hash", "closedPnl", "feeUsd"})

    side: Side
    price: float = Field(gt=0.0)
    size: float = Field(ge=0.0)
    direction: Literal["open", "close", "flip"] | None = Field(default=None, alias="dir")
    hash: str | None = None
    closed_pnl: float | None = None
    fee_usd: float | None = None


class PricePoint(_Wire):
    """One point on the chart tape: a candle (`bar` set) or a fill mark (`fill` set)."""

    _omit_when_none: ClassVar[frozenset[str]] = frozenset({"open", "high", "low", "close", "bar", "block", "fill"})

    ts: int = Field(gt=0)
    mid: float = Field(gt=0.0)
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    bar: Literal["1s", "1m", "15m"] | None = None
    block: int | None = Field(default=None, ge=0)
    fill: PricePointFill | None = None


class BlockEvent(_Wire):
    event: ClassVar[str] = "block"

    coin: str = Field(min_length=1, max_length=32)
    block: int = Field(ge=0)
    ts: int = Field(gt=0)
    mid: float = Field(gt=0.0)
    best_bid: float = Field(gt=0.0)
    best_ask: float = Field(gt=0.0)
    spread_bps: float = Field(ge=0.0)
    decision: Decision | None
    quote: Quote | None
    fill: FillData | None
    resting: Resting
    position: Position
    totals: Totals
    account_value: float | None
    withdrawable: float | None


class QuoteMessage(_Wire):
    event: ClassVar[str] = "quote"

    coin: str = Field(min_length=1, max_length=32)
    block: int = Field(ge=0)
    quote: Quote


class FillMessage(_Wire):
    _omit_when_none: ClassVar[frozenset[str]] = frozenset({"ts"})
    event: ClassVar[str] = "fill"

    coin: str = Field(min_length=1, max_length=32)
    block: int = Field(ge=0)
    fill: FillData
    ts: int | None = Field(default=None, gt=0)


class PriceMessage(_Wire):
    event: ClassVar[str] = "price"

    coin: str = Field(min_length=1, max_length=32)
    ts: int = Field(gt=0)
    mid: float = Field(gt=0.0)
    best_bid: float = Field(gt=0.0)
    best_ask: float = Field(gt=0.0)
    spread_bps: float = Field(ge=0.0)


class SleeveMeta(_Wire):
    coin: str
    pair: str
    label: str
    wallet: str | None


class Meta(_Wire):
    model: str
    wallet: str | None
    dry_run: bool
    market: str
    started_at: int = Field(gt=0)
    venue: str
    coin: str
    pair: str
    explorer_tx: str
    tick_ms: int = Field(gt=0)
    sleeves: list[SleeveMeta]


WireMessage = Union[BlockEvent, QuoteMessage, FillMessage, PriceMessage]
WIRE_MESSAGE_TYPES = (BlockEvent, QuoteMessage, FillMessage, PriceMessage)


def to_wire(model: _Wire) -> dict[str, Any]:
    """JSON-ready dict in the camelCase shape the dashboard reads."""
    return model.model_dump(mode="json", by_alias=True)


def sse_event(event: str, payload: Any) -> bytes:
    """One Server-Sent Events frame: event name, compact JSON, blank line."""
    data = json.dumps(payload, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")


def sse_frame(message: WireMessage) -> bytes:
    return sse_event(message.event, to_wire(message))
