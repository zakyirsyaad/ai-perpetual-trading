from __future__ import annotations

import logging
import math
import unittest
from datetime import datetime, timedelta, timezone

from bot.core.setup_ledger import (
    EMPTY_BUCKET_LEDGER,
    Z_ONE_SIDED_90,
    BucketLedger,
    BucketStats,
    bucket_key,
    bucket_stats,
    gross_bps,
    ledger_status,
    ledger_table,
)
from bot.core.shadow_eval import evaluate_pending

MIN_SAMPLES = 60
HURDLE_BPS = 4.0
EMPTY = BucketStats(0, None, None, None)
BLOCK_AT = datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc)


def _trade(
    bucket: str | None, direction: str, entry: float, exit_: float, opened_at: object = None
) -> dict:
    setup = {} if bucket is None else {"bucket_key": bucket}
    row = {
        "direction": direction,
        "entry_price": entry,
        "exit_price": exit_,
        "factor_metadata": {"setup": setup},
    }
    return row if opened_at is None else {**row, "opened_at": opened_at}


def _shadow(
    bucket: str,
    gross: float | None,
    source: str = "ledger_block",
    side: str = "LONG",
    decided_at: object = BLOCK_AT.isoformat(),
) -> dict:
    row = {"bucket_key": bucket, "source": source, "gross_bps": gross, "side": side}
    return row if decided_at is None else {**row, "decided_at": decided_at}


def _first_episode(bucket: str, new_exits: list[float]) -> tuple[list[dict], list[dict]]:
    """60 SHORTs at -1 bps five days before BLOCK_AT, 60 good ledger_block outcomes (6 bps) decided every 70
    minutes up to BLOCK_AT (the first 69 hours before it, after those trades), then one SHORT per `new_exits`
    exit price, opened every 10 minutes after BLOCK_AT."""
    before = [_trade(bucket, "SHORT", 100.0, 100.01, (BLOCK_AT - timedelta(days=5)).isoformat()) for _ in range(60)]
    shadows = [_shadow(bucket, 6.0, decided_at=(BLOCK_AT - timedelta(minutes=70 * i)).isoformat()) for i in range(60)]
    after = [
        _trade(bucket, "SHORT", 100.0, exit_, (BLOCK_AT + timedelta(minutes=10 * (i + 1))).isoformat())
        for i, exit_ in enumerate(new_exits)
    ]
    return before + after, shadows


def _status(real: BucketStats, shadow_run: BucketStats, since_block: BucketStats = EMPTY) -> str:
    return ledger_status(
        BucketLedger(real=real, shadow_run=shadow_run, real_since_block=since_block), MIN_SAMPLES, HURDLE_BPS
    )


class BucketKeyTests(unittest.TestCase):
    def test_bucket_key_format(self) -> None:
        self.assertEqual(bucket_key("short", "TURUN", "atr_mid"), "SHORT|TURUN|atr_mid")
        self.assertEqual(bucket_key("LONG", "NAIK", "atr_high"), "LONG|NAIK|atr_high")


class GrossBpsTests(unittest.TestCase):
    def test_gross_bps_signs_by_direction(self) -> None:
        self.assertAlmostEqual(gross_bps("LONG", 100.0, 100.5), 50.0)
        self.assertAlmostEqual(gross_bps("LONG", 100.0, 99.5), -50.0)
        self.assertAlmostEqual(gross_bps("SHORT", 100.0, 99.5), 50.0)
        self.assertAlmostEqual(gross_bps("SHORT", 100.0, 100.5), -50.0)

    def test_gross_bps_accepts_lower_case_direction(self) -> None:
        self.assertAlmostEqual(gross_bps("short", 100.0, 99.0), 100.0)

    def test_gross_bps_rejects_an_unknown_direction(self) -> None:
        with self.assertRaises(ValueError):
            gross_bps("FLAT", 100.0, 101.0)


class BucketStatsTests(unittest.TestCase):
    def test_z_constant_is_the_spec_value(self) -> None:
        self.assertEqual(Z_ONE_SIDED_90, 1.28)

    def test_stats_below_two_have_no_se(self) -> None:
        self.assertEqual(bucket_stats([]), BucketStats(0, None, None, None))
        one = bucket_stats([3.5])
        self.assertEqual(one, BucketStats(1, 3.5, None, None))

    def test_stats_use_sample_stdev_and_the_one_sided_bound(self) -> None:
        stats = bucket_stats([0.0, 2.0, 4.0, 6.0])
        sample_stdev = math.sqrt(20.0 / 3.0)
        expected_se = sample_stdev / math.sqrt(4)
        self.assertEqual(stats.n, 4)
        self.assertAlmostEqual(stats.mean_bps, 3.0)
        self.assertAlmostEqual(stats.se_bps, expected_se)
        self.assertAlmostEqual(stats.upper_bps, 3.0 + 1.28 * expected_se)

    def test_stats_accept_any_sequence_without_mutating_it(self) -> None:
        values = (1.0, 2.0, 3.0)
        self.assertEqual(bucket_stats(values).n, 3)
        self.assertEqual(values, (1.0, 2.0, 3.0))


class LedgerStatusTests(unittest.TestCase):
    def test_identical_values_have_zero_se_and_still_decide(self) -> None:
        low = bucket_stats([1.0] * 60)
        self.assertEqual(low.se_bps, 0.0)
        self.assertEqual(_status(low, BucketStats(0, None, None, None)), "blocked")
        high = bucket_stats([4.0] * 60)
        self.assertEqual(_status(high, BucketStats(0, None, None, None)), "open")

    def test_learning_below_min_samples(self) -> None:
        empty = BucketStats(0, None, None, None)
        self.assertEqual(_status(empty, empty), "learning")
        one = bucket_stats([-50.0])
        self.assertEqual(_status(one, empty), "learning")
        short = bucket_stats([-50.0] * 59)
        self.assertEqual(_status(short, empty), "learning")
        full = bucket_stats([-50.0] * 60)
        self.assertEqual(_status(full, empty), "blocked")

    def test_blocked_when_the_upper_bound_is_below_the_hurdle(self) -> None:
        real = BucketStats(60, 1.0, 1.5, 1.0 + Z_ONE_SIDED_90 * 1.5)
        self.assertAlmostEqual(real.upper_bps, 2.92)
        empty = BucketStats(0, None, None, None)
        self.assertEqual(_status(real, empty), "blocked")

    def test_open_when_the_upper_bound_reaches_the_hurdle(self) -> None:
        empty = BucketStats(0, None, None, None)
        at_hurdle = BucketStats(60, 2.0, 1.5625, 4.0)
        self.assertEqual(_status(at_hurdle, empty), "open")
        above = BucketStats(60, 3.0, 1.5, 3.0 + Z_ONE_SIDED_90 * 1.5)
        self.assertEqual(_status(above, empty), "open")

    def test_reopened_needs_min_samples_of_shadow_at_or_above_the_hurdle(self) -> None:
        real = bucket_stats([1.0] * 60)
        good_shadow = bucket_stats([5.0] * 60)
        self.assertEqual(_status(real, good_shadow), "reopened")
        at_hurdle = bucket_stats([4.0] * 60)
        self.assertEqual(_status(real, at_hurdle), "reopened")
        too_few = bucket_stats([5.0] * 59)
        self.assertEqual(_status(real, too_few), "blocked")
        below = bucket_stats([3.0] * 60)
        self.assertEqual(_status(real, below), "blocked")

    def test_shadow_cannot_reopen_a_bucket_that_is_still_learning_or_open(self) -> None:
        good_shadow = bucket_stats([5.0] * 60)
        learning = bucket_stats([1.0] * 10)
        self.assertEqual(_status(learning, good_shadow), "learning")
        open_ = bucket_stats([6.0] * 60)
        self.assertEqual(_status(open_, good_shadow), "open")


class LedgerTableTests(unittest.TestCase):
    def test_table_skips_rows_without_a_bucket_and_shadow_side_rows(self) -> None:
        bucket = "SHORT|TURUN|atr_mid"
        trades = [
            _trade(bucket, "SHORT", 100.0, 99.0),
            _trade(bucket, "SHORT", 100.0, 100.5),
            _trade("", "SHORT", 100.0, 99.0),
            _trade(None, "SHORT", 100.0, 99.0),
            {"direction": "SHORT", "entry_price": 100.0, "exit_price": 99.0, "factor_metadata": {}},
            {"direction": "SHORT", "entry_price": 100.0, "exit_price": 99.0},
            {"direction": "SHORT", "entry_price": 100.0, "exit_price": 99.0, "factor_metadata": None},
        ]
        shadows = [
            _shadow(bucket, 7.0),
            _shadow(bucket, None),
            _shadow(bucket, 9.0, source="shadow_side"),
            _shadow("", 9.0),
            {"source": "ledger_block", "gross_bps": 9.0},
        ]
        table = ledger_table(trades, shadows)
        self.assertEqual(set(table), {bucket})
        real, shadow = table[bucket].real, table[bucket].shadow_run
        self.assertEqual(real.n, 2)
        self.assertAlmostEqual(real.mean_bps, (100.0 - 50.0) / 2)
        self.assertEqual(shadow, BucketStats(1, 7.0, None, None))

    def test_a_shadow_only_bucket_has_empty_real_stats(self) -> None:
        bucket = "LONG|NAIK|atr_high"
        table = ledger_table([], [_shadow(bucket, 2.0), _shadow(bucket, 4.0)])
        real, shadow = table[bucket].real, table[bucket].shadow_run
        self.assertEqual(real, BucketStats(0, None, None, None))
        self.assertEqual(shadow.n, 2)
        self.assertAlmostEqual(shadow.mean_bps, 3.0)
        self.assertIsNotNone(shadow.upper_bps)

    def test_a_trade_only_bucket_has_empty_shadow_stats(self) -> None:
        bucket = "LONG|NAIK|atr_high"
        table = ledger_table([_trade(bucket, "LONG", 100.0, 100.1)], [])
        real, shadow = table[bucket].real, table[bucket].shadow_run
        self.assertEqual(real.n, 1)
        self.assertAlmostEqual(real.mean_bps, 10.0)
        self.assertIsNone(real.upper_bps)
        self.assertEqual(shadow, BucketStats(0, None, None, None))

    def test_a_shadow_side_only_bucket_is_not_a_table_key(self) -> None:
        table = ledger_table([], [_shadow("LONG|NAIK|atr_high", 5.0, source="shadow_side")])
        self.assertEqual(table, {})

    def test_trade_rows_that_cannot_be_priced_are_skipped(self) -> None:
        bucket = "SHORT|TURUN|atr_mid"
        trades = [
            _trade(bucket, "SHORT", 0.0, 99.0),
            _trade(bucket, "SHORT", -5.0, 99.0),
            _trade(bucket, "SHORT", 100.0, 0.0),
            _trade(bucket, "FLAT", 100.0, 99.0),
            {"direction": "SHORT", "entry_price": None, "exit_price": 99.0, "factor_metadata": {"setup": {"bucket_key": bucket}}},
            {"direction": "SHORT", "entry_price": 100.0, "exit_price": None, "factor_metadata": {"setup": {"bucket_key": bucket}}},
            _trade(bucket, "SHORT", 100.0, 99.0),
        ]
        table = ledger_table(trades, [])
        real = table[bucket].real
        self.assertEqual(real.n, 1)
        self.assertAlmostEqual(real.mean_bps, 100.0)

    def test_non_finite_shadow_values_are_skipped(self) -> None:
        bucket = "SHORT|TURUN|atr_mid"
        table = ledger_table([], [_shadow(bucket, float("nan")), _shadow(bucket, float("inf")), _shadow(bucket, 1.0)])
        self.assertEqual(table[bucket].shadow_run, BucketStats(1, 1.0, None, None))

    def test_table_feeds_the_status_end_to_end(self) -> None:
        bucket = "SHORT|TURUN|atr_mid"
        trades = [_trade(bucket, "SHORT", 100.0, 100.01) for _ in range(60)]
        shadows = [_shadow(bucket, 6.0) for _ in range(60)]
        entry = ledger_table(trades, shadows)[bucket]
        self.assertAlmostEqual(entry.real.mean_bps, -1.0)
        self.assertEqual(ledger_status(entry, MIN_SAMPLES, HURDLE_BPS), "reopened")


class ReopenedBucketTests(unittest.TestCase):
    """A bucket the real rule blocks and the shadow rule reopens is judged again on its real trades opened
    after its latest `ledger_block` row: `min_samples` of them with an upper bound below the hurdle block it
    again; fewer, or a bound at or above the hurdle, keep it `reopened`."""

    BUCKET = "SHORT|TURUN|atr_mid"

    def _rows(self, new_exits: list[float]) -> tuple[list[dict], list[dict]]:
        return _first_episode(self.BUCKET, new_exits)

    def _status_of(self, new_exits: list[float]) -> str:
        trades, shadows = self._rows(new_exits)
        return ledger_status(ledger_table(trades, shadows)[self.BUCKET], MIN_SAMPLES, HURDLE_BPS)

    def test_a_reopened_bucket_that_loses_again_is_blocked_again(self) -> None:
        self.assertEqual(self._status_of([]), "reopened")
        self.assertEqual(self._status_of([100.01] * 60), "blocked")   # 60 new trades at -1 bps

    def test_a_reopened_bucket_with_too_few_new_trades_stays_reopened(self) -> None:
        self.assertEqual(self._status_of([100.01] * 59), "reopened")

    def test_a_reopened_bucket_whose_new_trades_clear_the_hurdle_stays_reopened(self) -> None:
        # 60 new trades at +5 bps: the whole sample (120, mean 2.0) is still below the hurdle, the new ones are not.
        trades, shadows = self._rows([99.95] * 60)
        entry = ledger_table(trades, shadows)[self.BUCKET]
        self.assertLess(entry.real.upper_bps, HURDLE_BPS)
        self.assertAlmostEqual(entry.real_since_block.upper_bps, 5.0)
        self.assertEqual(ledger_status(entry, MIN_SAMPLES, HURDLE_BPS), "reopened")

    def test_the_status_rule_reads_the_new_trades_only_after_a_shadow_reopening(self) -> None:
        losing_new = bucket_stats([-1.0] * 60)
        blocked_real = bucket_stats([-1.0] * 60)
        good_shadow = bucket_stats([6.0] * 60)
        poor_shadow = bucket_stats([3.0] * 60)
        self.assertEqual(_status(blocked_real, good_shadow, losing_new), "blocked")
        self.assertEqual(_status(blocked_real, good_shadow, bucket_stats([-1.0] * 59)), "reopened")
        self.assertEqual(_status(blocked_real, good_shadow, bucket_stats([4.0] * 60)), "reopened")
        # without a shadow reopening the new trades change nothing
        self.assertEqual(_status(blocked_real, poor_shadow, bucket_stats([9.0] * 60)), "blocked")
        self.assertEqual(_status(bucket_stats([1.0] * 10), good_shadow, losing_new), "learning")
        self.assertEqual(_status(bucket_stats([6.0] * 60), good_shadow, losing_new), "open")

    def test_only_trades_opened_strictly_after_the_latest_block_row_count(self) -> None:
        bucket = self.BUCKET
        later = BLOCK_AT + timedelta(hours=1)
        trades = [
            _trade(bucket, "SHORT", 100.0, 99.9, (BLOCK_AT - timedelta(minutes=1)).isoformat()),   # before
            _trade(bucket, "SHORT", 100.0, 99.8, BLOCK_AT.isoformat()),                           # at the first row
            _trade(bucket, "SHORT", 100.0, 99.7, later.isoformat()),                              # at the latest row
            _trade(bucket, "SHORT", 100.0, 99.6, (later + timedelta(seconds=1)).isoformat()),    # after: counts
            _trade(bucket, "SHORT", 100.0, 99.5, later + timedelta(minutes=5)),                   # a datetime: counts
            _trade(bucket, "SHORT", 100.0, 99.4),                                                 # no opened_at
            _trade(bucket, "SHORT", 100.0, 99.3, "not a time"),                                   # unreadable
            _trade(bucket, "SHORT", 100.0, 99.2, "2026-10-22T09:00:00"),                          # naive: refused
        ]
        shadows = [
            _shadow(bucket, 5.0, decided_at=BLOCK_AT.isoformat()),
            _shadow(bucket, None, decided_at=later.isoformat().replace("+00:00", "Z")),   # expired, still a block row
            _shadow(bucket, 5.0, source="shadow_side", decided_at=(later + timedelta(hours=1)).isoformat()),
            _shadow("LONG|NAIK|atr_mid", 5.0, decided_at=(later + timedelta(hours=1)).isoformat()),
            _shadow(bucket, 5.0, decided_at="garbage"),
        ]
        entry = ledger_table(trades, shadows)[bucket]
        self.assertEqual(entry.real.n, 8)
        self.assertEqual(entry.real_since_block.n, 2)
        self.assertAlmostEqual(entry.real_since_block.mean_bps, (40.0 + 50.0) / 2)

    def test_a_bucket_without_a_readable_block_row_has_no_new_trades(self) -> None:
        trades = [_trade(self.BUCKET, "SHORT", 100.0, 99.0, BLOCK_AT.isoformat())]
        for shadows in ([], [_shadow(self.BUCKET, 5.0, decided_at=None)], [_shadow(self.BUCKET, 5.0, decided_at="")]):
            with self.subTest(shadows=shadows):
                self.assertEqual(ledger_table(trades, shadows)[self.BUCKET].real_since_block, EMPTY)

    def test_the_empty_ledger_reads_learning(self) -> None:
        self.assertEqual(EMPTY_BUCKET_LEDGER, BucketLedger(EMPTY, EMPTY, EMPTY))
        self.assertEqual(ledger_status(EMPTY_BUCKET_LEDGER, MIN_SAMPLES, HURDLE_BPS), "learning")



# The first episode's 60 new trades open every 10 minutes after BLOCK_AT, the last at BLOCK_AT + 600 min.
RE_BLOCKED_AT = BLOCK_AT + timedelta(hours=11)


def _re_blocked(bucket: str, new_rows: list[dict], new_exits: tuple[float, ...] = ()) -> BucketLedger:
    """Reopened by the first run, blocked again by 60 losing trades opened after it; then `new_rows` and one
    trade per `new_exits` price, opened every 10 minutes after the last new row (decided 70 minutes apart)."""
    trades, shadows = _first_episode(bucket, [100.01] * 60)
    last = RE_BLOCKED_AT + timedelta(minutes=70 * max(len(new_rows) - 1, 0))
    later = [
        _trade(bucket, "SHORT", 100.0, exit_, (last + timedelta(minutes=10 * (i + 1))).isoformat())
        for i, exit_ in enumerate(new_exits)
    ]
    return ledger_table(trades + later, shadows + new_rows)[bucket]


class LatestEpisodeTests(unittest.TestCase):
    """Only the bucket's latest blocked episode can reopen it.

    Real trades (by `opened_at`) and evaluated `ledger_block` rows (by `decided_at`) share one timeline. The
    reopening sample is the run of block rows decided after the last real trade opened before the latest block
    row; the re-judging sample is the real trades opened after that row. Expired and invalid rows mark time and
    carry no outcome, and a row or trade without a readable time is off the timeline.
    """

    BUCKET = "SHORT|TURUN|atr_mid"

    def _new_rows(self, values: list[float | None]) -> list[dict]:
        return [
            _shadow(self.BUCKET, value, decided_at=(RE_BLOCKED_AT + timedelta(minutes=70 * i)).isoformat())
            for i, value in enumerate(values)
        ]

    def _re_blocked(self, new_rows: list[dict], new_exits: tuple[float, ...] = ()) -> BucketLedger:
        return _re_blocked(self.BUCKET, new_rows, new_exits)

    @staticmethod
    def _status(ledger: BucketLedger) -> str:
        return ledger_status(ledger, MIN_SAMPLES, HURDLE_BPS)

    def test_the_first_episode_reopening_sample_is_every_block_outcome(self) -> None:
        trades, shadows = _first_episode(self.BUCKET, [100.01] * 30)
        ledger = ledger_table(trades, shadows)[self.BUCKET]
        self.assertEqual(self._status(ledger), "reopened")
        self.assertEqual(ledger.shadow_run, bucket_stats([6.0] * 60))
        self.assertEqual(ledger.real_since_block.n, 30)
        self.assertAlmostEqual(ledger.real_since_block.mean_bps, -1.0)

    def test_a_re_blocked_bucket_stays_blocked_before_its_next_block_row(self) -> None:
        self.assertEqual(self._status(self._re_blocked([])), "blocked")

    def test_one_good_block_row_does_not_bring_back_the_old_shadow_sample(self) -> None:
        ledger = self._re_blocked(self._new_rows([6.0]))
        self.assertEqual(self._status(ledger), "blocked")
        self.assertEqual(ledger.shadow_run, BucketStats(1, 6.0, None, None))
        self.assertEqual(ledger.real_since_block, EMPTY)

    def test_a_re_blocked_bucket_needs_a_fresh_run_of_min_samples_to_reopen(self) -> None:
        self.assertEqual(self._status(self._re_blocked(self._new_rows([6.0] * 59))), "blocked")
        ledger = self._re_blocked(self._new_rows([6.0] * 60))
        self.assertEqual(self._status(ledger), "reopened")
        self.assertEqual(ledger.shadow_run, bucket_stats([6.0] * 60))

    def test_an_expired_row_marks_time_without_breaking_the_run(self) -> None:
        rows = self._new_rows([6.0] * 30 + [None] + [6.0] * 30)
        ledger = self._re_blocked(rows)
        self.assertEqual(ledger.shadow_run.n, 60)
        self.assertEqual(self._status(ledger), "reopened")

    def test_a_fresh_run_is_judged_again_on_the_trades_opened_after_it(self) -> None:
        rows = self._new_rows([6.0] * 60)
        self.assertEqual(self._status(self._re_blocked(rows, (100.01,) * 59)), "reopened")
        ledger = self._re_blocked(rows, (100.01,) * 60)
        self.assertEqual(self._status(ledger), "blocked")
        self.assertEqual(ledger.real_since_block.n, 60)
        self.assertEqual(ledger.shadow_run.n, 60)

    def test_a_real_trade_between_block_rows_ends_the_run(self) -> None:
        losers = [_trade(self.BUCKET, "SHORT", 100.0, 100.01, (BLOCK_AT - timedelta(days=2)).isoformat())] * 60
        between = _trade(self.BUCKET, "SHORT", 100.0, 100.01, (BLOCK_AT - timedelta(minutes=30)).isoformat())
        rows = [
            *(_shadow(self.BUCKET, 5.0, decided_at=(BLOCK_AT - timedelta(hours=h)).isoformat()) for h in (3, 2, 1)),
            _shadow(self.BUCKET, 7.0, decided_at=BLOCK_AT.isoformat()),
            _shadow(self.BUCKET, 8.0, decided_at=(BLOCK_AT + timedelta(hours=1)).isoformat()),
        ]
        ledger = ledger_table([*losers, between], rows)[self.BUCKET]
        self.assertEqual(ledger.shadow_run.n, 2)
        self.assertAlmostEqual(ledger.shadow_run.mean_bps, 7.5)

    def test_a_trade_opened_exactly_at_the_latest_block_row_ends_the_run(self) -> None:
        # The tie cannot leave the earlier run standing: the trade ends it, so the run is empty.
        trades, shadows = _first_episode(self.BUCKET, [])
        tie = _trade(self.BUCKET, "SHORT", 100.0, 100.01, BLOCK_AT.isoformat())
        ledger = ledger_table([*trades, tie], shadows)[self.BUCKET]
        self.assertEqual(ledger.shadow_run, EMPTY)
        self.assertEqual(ledger.real_since_block, EMPTY)
        self.assertEqual(self._status(ledger), "blocked")

    def test_a_block_row_without_a_readable_time_never_counts(self) -> None:
        timeless = [
            _shadow(self.BUCKET, 6.0, decided_at=None),
            _shadow(self.BUCKET, 6.0, decided_at="garbage"),
            _shadow(self.BUCKET, 6.0, decided_at="2026-10-22T09:00:00"),   # naive: refused
        ] * 20
        losers = [_trade(self.BUCKET, "SHORT", 100.0, 100.01, (BLOCK_AT - timedelta(days=2)).isoformat())] * 60
        ledger = ledger_table(losers, timeless)[self.BUCKET]
        self.assertEqual(ledger.shadow_run, EMPTY)
        self.assertEqual(self._status(ledger), "blocked")
        self.assertEqual(ledger_table([], timeless), {})

    def test_a_trade_without_a_readable_open_time_does_not_end_the_run(self) -> None:
        trades, shadows = _first_episode(self.BUCKET, [])
        timeless_trade = _trade(self.BUCKET, "SHORT", 100.0, 100.01)
        ledger = ledger_table([*trades, timeless_trade], shadows)[self.BUCKET]
        self.assertEqual(ledger.real.n, 61)
        self.assertEqual(ledger.shadow_run.n, 60)
        self.assertEqual(ledger.real_since_block, EMPTY)


class _EvaluatorStore:
    """Hands `evaluate_pending` every open row and keeps the outcome it closes each one with."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.closed: dict[int, tuple[float | None, str]] = {}

    async def pending_shadow_entries(self, decided_before: datetime, limit: int) -> list[dict]:
        return [row for row in self.rows if row["id"] not in self.closed][:limit]

    async def complete_shadow_entry(self, entry_id: int, gross_bps: float | None, exit_reason: str) -> None:
        self.closed[entry_id] = (gross_bps, exit_reason)


class _CandlesFrom:
    """A venue whose 1m window starts at `start`, after every decision minute here, and that serves the full
    window it is asked for (only a full window that misses the decision minute expires a row)."""

    def __init__(self, start: datetime) -> None:
        self.start = start

    async def get_klines(self, symbol: str, interval: str = "1m", limit: int = 120) -> list[dict]:
        first = int(self.start.timestamp() * 1000)
        return [
            {"timestamp": first + 60_000 * index, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
             "volume": 1.0}
            for index in range(limit)
        ]


class EvaluatedBlockRowTests(unittest.IsolatedAsyncioTestCase):
    """Expired and invalid rows exactly as `evaluate_pending` closes them carry no outcome and only mark time."""

    BUCKET = "SHORT|TURUN|atr_mid"

    async def test_an_expired_or_invalid_block_row_cannot_reopen(self) -> None:
        base = {
            "bucket_key": self.BUCKET, "source": "ledger_block", "symbol": "BTC/USDT", "side": "short",
            "ref_price": 100.0, "stop_loss_pct": 0.3, "floor_pct": 0.324, "hold_minutes": 60,
        }
        rows = [
            {**base, "id": 1, "decided_at": RE_BLOCKED_AT.isoformat(), "stop_loss_pct": 0.0},  # no walk can use it
            {**base, "id": 2, "decided_at": (RE_BLOCKED_AT + timedelta(minutes=70)).isoformat()},  # out of the window
        ]
        store = _EvaluatorStore(rows)
        now = RE_BLOCKED_AT + timedelta(days=1)
        with self.assertLogs("test.ledger.evaluator", level="INFO"):
            await evaluate_pending(store, _CandlesFrom(now - timedelta(minutes=5)), now=now,
                                   logger=logging.getLogger("test.ledger.evaluator"))
        self.assertEqual(store.closed, {1: (None, "invalid"), 2: (None, "expired")})

        for row in rows:
            gross, reason = store.closed[row["id"]]
            evaluated = {**row, "gross_bps": gross, "exit_reason": reason, "evaluated_at": now.isoformat()}
            with self.subTest(exit_reason=reason):
                ledger = _re_blocked(self.BUCKET, [evaluated])
                self.assertEqual(ledger_status(ledger, MIN_SAMPLES, HURDLE_BPS), "blocked")
                self.assertEqual(ledger.shadow_run, EMPTY)
                self.assertEqual(ledger.real_since_block, EMPTY)


if __name__ == "__main__":
    unittest.main()
