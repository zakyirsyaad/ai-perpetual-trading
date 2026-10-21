"""The setup ledger and the shadow rows of the AI entry path, as a mixin of `AIOrchestrator`.

The ledger files every OPEN decision under a bucket (`side|condition|atr_regime`) and judges the bucket on
its realized outcomes (`bot/core/setup_ledger.py`); a candidate that may not trade (a shadow side under
`ENTRY_REGIME_GATE=map`, or an enforce-blocked bucket) is written to `shadow_entries` instead, at most one
row per spacing key (`ShadowKey`) per spacing, for `bot/core/shadow_eval.py` to walk later.

`ShadowLedgerMixin` holds that state and its methods; it expects the class it is mixed into to provide
`_settings`, `_logger`, `_trade_store`, `_features_for_symbol`, `_current_gate_context`,
`_regime_gate_mode`, `_mapped_side` and `_live_sides`, and to call `_init_shadow_ledger_state` from its
`__init__` and `_expire_ledger_snapshot` whenever a tick returns a plan.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from bot.ai.brain import EntryDecision
from bot.arena.setup_profile import build_setup_profile
from bot.core.setup_ledger import (
    EMPTY_BUCKET_LEDGER,
    SHADOW_BLOCK_SOURCE,
    STATUS_BLOCKED,
    BucketLedger,
    bucket_key,
    ledger_status,
    ledger_table,
)

if TYPE_CHECKING:
    from bot.ai.orchestration import EntryCandidate

OPEN_ACTIONS = ("OPEN_LONG", "OPEN_SHORT")
# `shadow_entries.source` of a mapped side that ENTRY_GATE_DIRECTION does not make live.
SHADOW_SIDE_SOURCE = "shadow_side"
# What one shadow row spacing is kept for: (symbol, side as stored, source, bucket_key). The bucket is
# ANY_BUCKET for `shadow_side`: the LONG go-live bar pools those outcomes over every bucket, so two rows a
# minute apart in neighbouring ATR buckets would walk the same hour twice.
ShadowKey = tuple[str, str, str, str]
ANY_BUCKET = ""
# A setup-ledger snapshot younger than this is reused instead of read again (two paged GETs).
LEDGER_CACHE_SECONDS = 300


def _utc_now() -> datetime:
    """The wall clock that stamps a shadow row's `decided_at` and ages the ledger snapshot (one seam, so
    tests can move it)."""
    return datetime.now(timezone.utc)


def _bucket_label(bucket: str) -> str:
    """How a log line names a spacing key's bucket."""
    return f"bucket {bucket}" if bucket else "any bucket"


class ShadowLedgerMixin:
    """Setup-ledger verdicts, the ledger snapshot cache and the shadow rows with their spacing."""

    _shadow_rows_written_at: dict[ShadowKey, datetime]
    _shadow_seeded_keys: frozenset[ShadowKey]
    _last_ledger_snapshot: dict[str, BucketLedger] | None
    _last_ledger_read_at: datetime | None
    _taker_fee_pct_by_symbol: dict[str, float]

    def _init_shadow_ledger_state(self) -> None:
        """The in-memory state, all empty: nothing survives a restart except what the store holds."""
        # `decided_at` of the last shadow row per spacing key (`ShadowKey`), as written here or
        # read from the store; the keys already read from the store this process (each is read once).
        self._shadow_rows_written_at = {}
        self._shadow_seeded_keys = frozenset()
        # The last setup-ledger snapshot read without error and when it was read; `None` until the first
        # one. Reused for `LEDGER_CACHE_SECONDS`, and on a failed read whatever its age.
        self._last_ledger_snapshot = None
        self._last_ledger_read_at = None
        # The venue's taker rate per symbol, handed over by the engine (`set_taker_fee_pcts`).
        self._taker_fee_pct_by_symbol = {}

    def set_taker_fee_pcts(self, fees: Mapping[str, float]) -> None:
        """The venue's taker rate per symbol, as the engine read it at startup. The live neutral exit nets
        that rate (`TradeEngine._taker_fee_pct`), so the shadow floor does too; a symbol without one falls
        back to `TAKER_FEE_PCT`, as the engine does."""
        self._taker_fee_pct_by_symbol = dict(fees)

    def _shadow_taker_fee_pct(self, symbol: str) -> float:
        return float(self._taker_fee_pct_by_symbol.get(symbol, self._settings.taker_fee_pct))

    def _ledger_mode(self) -> str:
        return str(getattr(self._settings, "setup_ledger_mode", "off") or "off").strip().lower()

    async def _ledger_snapshot(self) -> dict[str, BucketLedger]:
        """Real and shadow outcome statistics per bucket since `SETUP_LEDGER_SINCE`; `{}` while the ledger is off.

        Only `ledger_block` shadow rows count, so only those are read; `ledger_table` puts them and the
        trades on one timeline, so only a bucket's latest run of block rows can reopen it. A snapshot read
        less than `LEDGER_CACHE_SECONDS` ago is reused without a read. A failed read is logged and reuses
        the last snapshot read without error, whatever its age, so a transient store error lets no entry
        into a blocked bucket (that trade would also end the bucket's reopening run). Before the first good
        read it gives `{}`: every bucket reads `learning` for the tick, so the ledger fails open there (an
        ERROR under `enforce`) and the regime and stretch gates still apply."""
        if self._ledger_mode() == "off":
            return {}
        now = _utc_now()
        last, read_at = self._last_ledger_snapshot, self._last_ledger_read_at
        if last is not None and read_at is not None and (now - read_at).total_seconds() < LEDGER_CACHE_SECONDS:
            return last
        since = self._settings.setup_ledger_since
        try:
            trades = await self._trade_store.closed_trade_rows_since(since)
            shadows = await self._trade_store.shadow_outcomes_since(since, source=SHADOW_BLOCK_SOURCE)
            snapshot = ledger_table(trades, shadows, since=since)
        except Exception as exc:  # noqa: BLE001 - a ledger read must never stop the entry path
            failing_open = last is None
            self._logger.log(
                logging.ERROR if failing_open and self._ledger_mode() == "enforce" else logging.WARNING,
                "Setup ledger could not be read (%s: %s); %s",
                type(exc).__name__,
                exc,
                "every bucket reads as learning this tick." if failing_open else "the last snapshot is used this tick.",
            )
            return {} if failing_open else last
        # Replaced, never mutated.
        self._last_ledger_snapshot, self._last_ledger_read_at = snapshot, now
        return snapshot

    def _expire_ledger_snapshot(self) -> None:
        """Make the next tick read the ledger again; the snapshot stays for a failed read to reuse.

        Called when a tick returns a plan: the trade can open and close before the next strategy call, which
        must then count it even inside `LEDGER_CACHE_SECONDS` (account 1 runs no entry cooldown)."""
        self._last_ledger_read_at = None

    def _ledger_bucket(self, side: str, features: dict[str, Any] | None) -> str:
        """`side|condition|atr_regime` for an entry on `side` now: the condition of the gate context set
        before the strategy ran, the ATR regime of the symbol's 3m technicals."""
        kondisi = str((self._current_gate_context() or {}).get("kondisi") or "")
        return bucket_key(side, kondisi, build_setup_profile(features, side)["atr_regime"])

    def _ledger_verdict(
        self,
        side: str,
        features: dict[str, Any] | None,
        ledger: dict[str, BucketLedger],
    ) -> dict[str, Any]:
        bucket = self._ledger_bucket(side, features)
        # `ledger_table` keys only buckets with a counted row; any other bucket has no outcomes yet.
        entry = ledger.get(bucket, EMPTY_BUCKET_LEDGER)
        return {
            "mode": self._ledger_mode(),
            "bucket": bucket,
            "n": entry.real.n,
            "mean_bps": entry.real.mean_bps,
            "upper_bps": entry.real.upper_bps,
            # The two samples the status turns on once the real bound is below the hurdle.
            "shadow_run_n": entry.shadow_run.n,
            "shadow_run_upper_bps": entry.shadow_run.upper_bps,
            "since_block_n": entry.real_since_block.n,
            "since_block_upper_bps": entry.real_since_block.upper_bps,
            "status": ledger_status(
                entry,
                int(self._settings.setup_ledger_min_samples),
                float(self._settings.setup_ledger_hurdle_bps),
            ),
        }

    def _with_ledger_verdict(
        self,
        gate_verdict: dict[str, Any],
        decision: EntryDecision,
        market_context: dict[str, Any],
        ledger: dict[str, BucketLedger],
    ) -> dict[str, Any]:
        """The gate verdict plus `ledger` for an OPEN decision while the ledger is on; otherwise unchanged.

        A new dict, so the stored decision (`payload_json.gate.ledger`) and the outcome read the same verdict."""
        if self._ledger_mode() == "off" or decision.action not in OPEN_ACTIONS:
            return gate_verdict
        side = "LONG" if decision.action == "OPEN_LONG" else "SHORT"
        features = self._features_for_symbol(market_context, decision.symbol)
        return {**gate_verdict, "ledger": self._ledger_verdict(side, features, ledger)}

    def _ledger_lines(
        self,
        sides: Sequence[str],
        features: dict[str, Any] | None,
        ledger: dict[str, BucketLedger],
    ) -> list[dict[str, Any]]:
        """One line per offered side, from the tick's one ledger snapshot; `[]` unless the ledger enforces.

        A `shadow` ledger records its verdict but still trades a `blocked` bucket, so showing the model that
        status would describe a rule the code does not apply; the lean request then has no `setup_ledger`."""
        if self._ledger_mode() != "enforce":
            return []
        verdicts = [(side, self._ledger_verdict(side, features, ledger)) for side in sides]
        return [
            {
                "side": side,
                "bucket": verdict["bucket"],
                "n": verdict["n"],
                "mean_bps": round(verdict["mean_bps"], 2) if verdict["mean_bps"] is not None else None,
                "status": verdict["status"],
            }
            for side, verdict in verdicts
        ]

    def _shadow_source(self, decision: EntryDecision, gate_verdict: dict[str, Any]) -> str:
        """Why a candidate that cleared every entry check is recorded instead of traded; `""` trades it.

        A shadow side always; a `blocked` ledger bucket only under `enforce` (`shadow` logs it and trades)."""
        # Truthiness, not `is True`: on this path any doubt must end in "not traded".
        if gate_verdict.get("shadow_side"):
            return SHADOW_SIDE_SOURCE
        ledger = gate_verdict.get("ledger")
        if not isinstance(ledger, dict) or ledger.get("status") != STATUS_BLOCKED:
            return ""
        if ledger.get("mode") == "enforce":
            return SHADOW_BLOCK_SOURCE
        self._logger.info(
            "Setup ledger (shadow) would block %s: bucket %s has n=%s, upper bound %s bps below the hurdle.",
            decision.symbol,
            ledger.get("bucket"),
            ledger.get("n"),
            ledger.get("upper_bps"),
        )
        return ""

    def _shadow_row(
        self,
        decision: EntryDecision,
        candidate: EntryCandidate,
        source: str,
        market_context: dict[str, Any],
        decided_at: datetime | None = None,
    ) -> dict[str, Any]:
        """One `shadow_entries` row for a candidate that is recorded, not traded (the store adds `exchange`).

        The floor is the live neutral-exit level, `100 x VOLUME_NEUTRAL_EXIT_MIN_NET_USDT / notional` plus
        the round-trip taker fee the live exit nets (the venue's rate, not the `TAKER_FEE_PCT` estimate in
        the candidate's targets), capped at the candidate's take-profit. `decided_at` defaults to now.
        Raises `ValueError` without a 3m price."""
        features = self._features_for_symbol(market_context, decision.symbol) or {}
        ref_price = float(features.get("last_price") or 0.0)
        if not math.isfinite(ref_price) or ref_price <= 0:
            raise ValueError(f"no 3m last price for {decision.symbol}")
        side = candidate.direction.value
        notional = float(self._settings.volume_profile.margin_per_entry_usdt) * float(self._settings.leverage)
        floor_pct = min(
            candidate.target.take_profit_pct,
            100.0 * float(self._settings.volume_neutral_exit_min_net_usdt) / notional
            + 2.0 * self._shadow_taker_fee_pct(decision.symbol),
        )
        return {
            "decided_at": decided_at if decided_at is not None else _utc_now(),
            "symbol": decision.symbol,
            "side": side.lower(),
            "bucket_key": self._ledger_bucket(side, features),
            "source": source,
            "ref_price": ref_price,
            "stop_loss_pct": candidate.target.stop_loss_pct,
            "floor_pct": floor_pct,
            "hold_minutes": int(self._settings.position_hold_minutes),
        }

    def _shadow_spacing(self) -> timedelta:
        """The least time between two shadow rows of one spacing key (`ShadowKey`).

        A shadow row stands for a trade that was not taken; a real trade holds up to
        `POSITION_HOLD_MINUTES` and is followed by `COOLDOWN_MINUTES_AFTER_ENTRY`, so rows closer than that
        would overlap their walk windows, count one move several times and understate the SE."""
        hold = int(getattr(self._settings, "position_hold_minutes", 0) or 0)
        cooldown = int(getattr(self._settings, "cooldown_minutes_after_entry", 0) or 0)
        return timedelta(minutes=hold + cooldown)

    def _shadow_key(self, symbol: str, side: str, source: str, market_context: dict[str, Any]) -> ShadowKey:
        """The spacing key of a shadow row: its symbol, side (lower case, as stored), source and bucket.

        Only `ledger_block` rows are spaced per bucket, since a bucket reopens on its own block outcomes;
        the `shadow_side` rows of every bucket share one spacing (`ANY_BUCKET`)."""
        if source != SHADOW_BLOCK_SOURCE:
            return (symbol, side.lower(), source, ANY_BUCKET)
        features = self._features_for_symbol(market_context, symbol)
        return (symbol, side.lower(), source, self._ledger_bucket(side.upper(), features))

    async def _last_shadow_row_at(self, key: ShadowKey) -> datetime | None:
        """`decided_at` of the key's last row: from memory, else read from the store once per process.

        The store read seeds the spacing after a restart. A failed read logs a WARNING and counts as no
        earlier row (the row is written), and it is tried again the next time the key comes up."""
        last = self._shadow_rows_written_at.get(key)
        if last is not None or key in self._shadow_seeded_keys:
            return last
        try:
            last = await self._trade_store.latest_shadow_decided_at(*key)
        except Exception as exc:  # noqa: BLE001 - a seed read must never stop the entry path
            self._logger.warning(
                "Shadow row spacing for %s could not be read from the store (%s: %s); treated as no earlier row.",
                "|".join(key),
                type(exc).__name__,
                exc,
            )
            return None
        # Replaced, never mutated.
        self._shadow_seeded_keys = self._shadow_seeded_keys | {key}
        if last is not None:
            self._shadow_rows_written_at = {**self._shadow_rows_written_at, key: last}
        return last

    async def _spacing_left(self, key: ShadowKey, now: datetime) -> tuple[datetime, timedelta] | None:
        """(last row's `decided_at`, time left) while the key's last row is inside the spacing; else None."""
        last = await self._last_shadow_row_at(key)
        if last is None:
            return None
        left = self._shadow_spacing() - (now - last)
        return (last, left) if left > timedelta(0) else None

    def _only_gate_side(self) -> str:
        """The one side that can pass the side filter this tick, or `""`: the mapped side under `map`, the
        single side `ENTRY_GATE_DIRECTION` names under `naik`; none with the regime gate off."""
        mode = self._regime_gate_mode()
        if mode == "map":
            return self._mapped_side()
        live = self._live_sides()
        return next(iter(live)) if mode != "off" and len(live) == 1 else ""

    async def _call_is_spaced(self, market_context: dict[str, Any], ledger: dict[str, BucketLedger]) -> bool:
        """True, after one INFO line, when this tick's AI call could change nothing.

        Under a regime gate only one side can pass the side filter (`_only_gate_side`). When that side cannot
        trade either, its answer can only become a shadow row: a mapped side `ENTRY_GATE_DIRECTION` does not
        make live (`shadow_side`), or, under an enforcing ledger, a live side whose bucket is `blocked`
        (`ledger_block`). While every symbol's row for it is inside its spacing, no row would be written."""
        side = self._only_gate_side()
        if not side:
            return False
        if side not in self._live_sides(side):
            source = SHADOW_SIDE_SOURCE
        elif self._ledger_mode() == "enforce":
            source = SHADOW_BLOCK_SOURCE
        else:
            return False
        now = _utc_now()
        waits: list[tuple[ShadowKey, datetime, timedelta]] = []
        for symbol in self._settings.symbols:
            if source == SHADOW_BLOCK_SOURCE:
                features = self._features_for_symbol(market_context, str(symbol))
                if self._ledger_verdict(side.upper(), features, ledger)["status"] != STATUS_BLOCKED:
                    return False
            key = self._shadow_key(str(symbol), side, source, market_context)
            spaced = await self._spacing_left(key, now)
            if spaced is None:
                return False
            waits.append((key, *spaced))
        if not waits:
            return False
        key, last, left = min(waits, key=lambda wait: wait[2])
        kondisi = (self._current_gate_context() or {}).get("kondisi") or "?"
        if source == SHADOW_SIDE_SOURCE:
            reason = "Shadow-only hour (%s maps %s, which is not traded here)" % (kondisi, side)
        else:
            reason = "Ledger-blocked setup (%s maps %s, whose bucket the ledger blocks)" % (kondisi, side)
        self._logger.info(
            "%s: the last %s shadow row (%s) was decided at %s, %.1f min of the %.0f-minute spacing remain; "
            "holding without an AI call.",
            reason,
            side.upper(),
            _bucket_label(key[3]),
            last.isoformat(timespec="seconds"),
            left.total_seconds() / 60.0,
            self._shadow_spacing().total_seconds() / 60.0,
        )
        return True

    async def _record_shadow_entry(
        self,
        decision: EntryDecision,
        candidate: EntryCandidate,
        source: str,
        market_context: dict[str, Any],
    ) -> None:
        """Write the candidate's shadow row, at most one per spacing key (`ShadowKey`) per spacing.

        A row inside the spacing is skipped (one INFO line) and a failed write is logged; the caller drops
        the candidate in every case. Only a written row starts the spacing. After a restart the spacing of
        a key is seeded once from the store's newest row for it."""
        key = self._shadow_key(decision.symbol, candidate.direction.value, source, market_context)
        now = _utc_now()
        spaced = await self._spacing_left(key, now)
        if spaced is not None:
            last, left = spaced
            self._logger.info(
                "Entry candidate %s %s is not traded (%s); no shadow row: the last one for %s was decided "
                "at %s, %.1f min of the %.0f-minute spacing remain.",
                decision.symbol,
                candidate.direction.value,
                source,
                _bucket_label(key[3]),
                last.isoformat(timespec="seconds"),
                left.total_seconds() / 60.0,
                self._shadow_spacing().total_seconds() / 60.0,
            )
            return
        try:
            row = self._shadow_row(decision, candidate, source, market_context, decided_at=now)
            await self._trade_store.save_shadow_entry(row)
        except Exception as exc:  # noqa: BLE001 - the candidate is not traded whether or not the row is saved
            self._logger.warning(
                "Entry candidate %s %s is not traded (%s), but its shadow row could not be saved (%s: %s).",
                decision.symbol,
                candidate.direction.value,
                source,
                type(exc).__name__,
                exc,
            )
            return
        # Replaced, never mutated.
        self._shadow_rows_written_at = {**self._shadow_rows_written_at, key: row["decided_at"]}
        self._logger.info(
            "Entry candidate %s %s is not traded (%s): shadow row for bucket %s, stop %.3f%%, floor %.3f%%.",
            decision.symbol,
            candidate.direction.value,
            source,
            row["bucket_key"],
            row["stop_loss_pct"],
            row["floor_pct"],
        )
