from __future__ import annotations

from itertools import cycle
from typing import TYPE_CHECKING

from bot.config import Settings
from bot.models import PositionDirection, TradePlan
from bot.strategy.base import BaseStrategy
from bot.strategy.confluence import evaluate_confluence
from bot.volume import VolumeBuildProfile

if TYPE_CHECKING:
    from bot.intelligence.market_intelligence import MarketIntelligence


class VolumeStrategy(BaseStrategy):
    """
    A deterministic, volume-first strategy:
    - Rotates symbols (BTC/ETH).
    - Alternates LONG and SHORT to keep both sides active.
    - Uses fixed notional size to generate predictable volume.
    """

    def __init__(self, settings: Settings, intelligence: "MarketIntelligence | None" = None) -> None:
        self._settings = settings
        self._profile = VolumeBuildProfile.from_settings(settings)
        self._intelligence = intelligence
        self._symbol_cycle = cycle(settings.symbols)
        self._direction_cycle = cycle((PositionDirection.LONG, PositionDirection.SHORT))

    def next_trade(self) -> TradePlan:
        return self._build_plan(context=None)

    async def next_trade_plan(self) -> TradePlan:
        context = None
        if self._intelligence is not None:
            context = (await self._intelligence.build_context()).to_dict()
        return self._build_plan(context=context)

    async def close(self) -> None:
        if self._intelligence is not None:
            await self._intelligence.close()

    def _build_plan(self, context: dict | None) -> TradePlan:
        profile_fields = self._profile.as_log_fields()
        symbol = next(self._symbol_cycle)
        factor_metadata = self._factor_metadata_for_symbol(context, symbol)
        trigger_metadata = self._trigger_metadata_for_symbol(context, symbol)

        direction = next(self._direction_cycle)
        stop_loss_pct = self._settings.stop_loss_pct
        take_profit_pct = self._settings.take_profit_pct
        confidence = 0.5
        setup_score = 0.5
        decision_reason = (
            "volume_builder "
            f"entry_volume={profile_fields['volume_per_entry_usdt']:.2f} "
            f"daily_target={profile_fields['daily_volume_usdt']:.2f}"
        )

        if context is not None and bool(getattr(self._settings, "confluence_enabled", True)):
            snapshots = self._all_snapshots_for_symbol(context, symbol)
            atr_pct = self._atr_pct_for_symbol(context, symbol)
            decision = evaluate_confluence(
                snapshots,
                atr_pct=atr_pct,
                min_agreement=float(self._settings.confluence_min_agreement),
                min_atr_pct=float(self._settings.confluence_min_atr_pct),
                min_score=float(self._settings.confluence_min_score),
                atr_tp_mult=float(self._settings.confluence_atr_tp_mult),
                atr_sl_mult=float(self._settings.confluence_atr_sl_mult),
                tp_floor_pct=float(self._settings.take_profit_pct),
                sl_floor_pct=float(self._settings.stop_loss_pct),
            )
            direction = decision.direction
            stop_loss_pct = decision.stop_loss_pct
            take_profit_pct = decision.take_profit_pct
            confidence = min(1.0, abs(decision.mean_score) + decision.agreement / 2.0)
            setup_score = decision.agreement
            decision_reason = f"confluence:{symbol} {decision.reason}"

        return TradePlan(
            symbol=symbol,
            direction=direction,
            usdt_size=float(self._profile.margin_per_entry_usdt),
            leverage=self._profile.leverage,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            decision_reason=decision_reason,
            decision_payload={
                "volume_profile": profile_fields,
                "arena_factors": factor_metadata,
                "arena_triggers": trigger_metadata,
            },
            confidence=confidence,
            setup_score=setup_score,
            max_hold_minutes=self._settings.position_hold_minutes,
            factor_metadata=factor_metadata,
            trigger_metadata=trigger_metadata,
        )

    @staticmethod
    def _all_snapshots_for_symbol(context: dict | None, symbol: str) -> list:
        if context is None:
            return []
        return [
            item
            for item in context.get("factor_snapshots", [])
            if isinstance(item, dict) and item.get("symbol") == symbol
        ]

    @staticmethod
    def _atr_pct_for_symbol(context: dict | None, symbol: str) -> float:
        if context is None:
            return 0.0
        for item in context.get("technicals", []):
            if not isinstance(item, dict) or item.get("symbol") != symbol:
                continue
            last = float(item.get("last_price") or 0.0)
            atr_14 = float(item.get("atr_14") or 0.0)
            return (atr_14 / last * 100.0) if last > 0 else 0.0
        return 0.0

    @staticmethod
    def _factor_metadata_for_symbol(context: dict | None, symbol: str) -> dict:
        if context is None:
            return {"top_factors": [], "snapshots": []}
        snapshots = [
            item
            for item in context.get("factor_snapshots", [])
            if isinstance(item, dict) and item.get("symbol") == symbol
        ]
        top = sorted(snapshots, key=lambda item: abs(float(item.get("score") or 0.0)), reverse=True)[:5]
        return {
            "top_factors": [str(item.get("name")) for item in top if item.get("name")],
            "snapshots": top,
        }

    @staticmethod
    def _trigger_metadata_for_symbol(context: dict | None, symbol: str) -> dict:
        if context is None:
            return {"trigger_names": [], "signals": []}
        triggers = [
            item
            for item in context.get("active_flow_signals", [])
            if isinstance(item, dict) and item.get("symbol") == symbol
        ]
        return {
            "trigger_names": [str(item.get("name")) for item in triggers if item.get("name")],
            "signals": triggers,
        }
