from __future__ import annotations

import unittest

from bot.telemetry.clip import clip_history, clip_snapshot_tape, clip_tape
from bot.telemetry.wire import PricePoint, PricePointFill


def candle(ts: int, bar: str) -> PricePoint:
    return PricePoint(ts=ts, mid=100.0, open=100.0, high=101.0, low=99.0, close=100.0, bar=bar)


def fill(ts: int) -> PricePoint:
    return PricePoint(ts=ts, mid=100.0, fill=PricePointFill(side="buy", price=100.0, size=1.0))


def sample_tape() -> list[PricePoint]:
    points = [candle(i * 900_000, "15m") for i in range(1, 4)]
    points += [candle(i * 60_000, "1m") for i in range(1, 11)]
    points += [candle(i * 1_000, "1s") for i in range(1, 11)]
    points += [fill(5_000), fill(300_000)]
    return sorted(points, key=lambda p: (p.ts, 1 if p.fill else 0))


class ClipHistoryTests(unittest.TestCase):
    def test_keeps_the_most_recent_events(self) -> None:
        self.assertEqual(clip_history(list("abcdef"), 3), ["d", "e", "f"])

    def test_returns_everything_when_under_the_limit(self) -> None:
        self.assertEqual(clip_history(list("ab"), 3), ["a", "b"])


class ClipTapeTests(unittest.TestCase):
    def test_returns_the_whole_tape_when_within_both_caps(self) -> None:
        tape = sample_tape()

        self.assertEqual(clip_tape(tape, max_mids=50, max_secs=50), tape)

    def test_trims_minute_and_second_bars_but_keeps_every_fill_and_15m_bar(self) -> None:
        clipped = clip_tape(sample_tape(), max_mids=4, max_secs=3)

        self.assertEqual(sum(1 for p in clipped if p.bar == "15m"), 3)
        self.assertEqual(sum(1 for p in clipped if p.bar == "1m"), 4)
        self.assertEqual(sum(1 for p in clipped if p.bar == "1s"), 3)
        self.assertEqual(sum(1 for p in clipped if p.fill), 2)

    def test_trimmed_bars_are_the_newest_ones(self) -> None:
        clipped = clip_tape(sample_tape(), max_mids=4, max_secs=3)

        self.assertEqual([p.ts for p in clipped if p.bar == "1m"], [420_000, 480_000, 540_000, 600_000])
        self.assertEqual([p.ts for p in clipped if p.bar == "1s"], [8_000, 9_000, 10_000])

    def test_result_is_sorted_by_time(self) -> None:
        clipped = clip_tape(sample_tape(), max_mids=4, max_secs=3)

        self.assertEqual([p.ts for p in clipped], sorted(p.ts for p in clipped))


class ClipSnapshotTapeTests(unittest.TestCase):
    def test_drops_15m_bars_and_keeps_only_recent_fills(self) -> None:
        clipped = clip_snapshot_tape(sample_tape(), max_mids=4, max_fills=1, max_secs=3)

        self.assertEqual(sum(1 for p in clipped if p.bar == "15m"), 0)
        self.assertEqual([p.ts for p in clipped if p.fill], [300_000])
        self.assertEqual(sum(1 for p in clipped if p.bar == "1m"), 4)
        self.assertEqual(sum(1 for p in clipped if p.bar == "1s"), 3)


if __name__ == "__main__":
    unittest.main()
