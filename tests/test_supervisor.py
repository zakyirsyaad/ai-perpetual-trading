from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
import unittest.mock

import supervisor
from supervisor import BlockedEntry, decide, parse_block

UNCERTAIN = (
    "2026-09-07 12:30:45 | ERROR | volume_bot | Entries blocked: Entry order "
    "aptc7c179895cba4fa0a38238e4 for BTC/USDT could not be confirmed accepted or "
    "rejected; manual reconciliation is required before restart."
)
UNMATCHED = (
    "2026-09-07 09:00:00 | ERROR | volume_bot | STARTUP ALERT: Unmatched exchange exposure: "
    "BTC/USDT (no persisted record). The bot cannot safely model these positions, so it "
    "will not trade or flatten them. Close or reconcile them manually, then restart."
)
NORMAL = "2026-09-07 13:06:24 | INFO | volume_bot | Strategy returned HOLD; no entry opened this tick."
STARTED = "2026-09-07 13:06:16 | INFO | volume_bot | BINGX VST trade engine started."


class ParseBlockTests(unittest.TestCase):
    def test_uncertain_order_is_parsed_with_its_identity(self) -> None:
        block = parse_block(NORMAL + "\n" + UNCERTAIN)
        self.assertIsNotNone(block)
        self.assertEqual(block.kind, "uncertain_order")
        self.assertEqual(block.order_id, "aptc7c179895cba4fa0a38238e4")
        self.assertEqual(block.symbol, "BTC/USDT")

    def test_unmatched_exposure_is_a_different_kind(self) -> None:
        block = parse_block(UNMATCHED)
        self.assertEqual(block.kind, "unmatched_exposure")

    def test_no_block_in_a_healthy_log(self) -> None:
        self.assertIsNone(parse_block(NORMAL))

    def test_the_most_recent_block_wins(self) -> None:
        # A stale uncertain-order line must not mask a later unmatched-exposure halt.
        block = parse_block(UNCERTAIN + "\n" + NORMAL + "\n" + UNMATCHED)
        self.assertEqual(block.kind, "unmatched_exposure")

    def test_a_block_before_the_last_restart_is_ignored(self) -> None:
        # The latch lives in the process. Once it restarts the latch is gone, but
        # the old ERROR lines stay in the log forever -- reading them would make
        # the supervisor restart a perfectly healthy bot.
        self.assertIsNone(parse_block("\n".join([UNCERTAIN, STARTED, NORMAL])))

    def test_a_block_after_the_last_restart_is_seen(self) -> None:
        self.assertIsNotNone(parse_block("\n".join([UNCERTAIN, STARTED, UNCERTAIN])))

    def test_only_the_last_restart_counts(self) -> None:
        self.assertIsNone(parse_block("\n".join([STARTED, UNCERTAIN, STARTED, NORMAL])))

    def test_empty_log_is_handled(self) -> None:
        self.assertIsNone(parse_block(""))


class DecideTests(unittest.TestCase):
    """Only one situation may be cleared automatically: an ambiguous order that
    provably produced nothing. Everything else waits for a human."""

    def _uncertain(self) -> BlockedEntry:
        return BlockedEntry(kind="uncertain_order", order_id="ord-1", symbol="BTC/USDT")

    def test_not_blocked_means_no_action(self) -> None:
        self.assertEqual(decide(None, position_size=0.0, lookup_status="absent").action, "none")

    def test_clears_only_when_flat_and_the_order_is_absent(self) -> None:
        d = decide(self._uncertain(), position_size=0.0, lookup_status="absent")
        self.assertEqual(d.action, "restart")

    def test_open_exposure_is_never_cleared(self) -> None:
        d = decide(self._uncertain(), position_size=0.23, lookup_status="absent")
        self.assertEqual(d.action, "hold")

    def test_a_found_order_is_never_cleared(self) -> None:
        # The order exists on the venue; it may still fill.
        d = decide(self._uncertain(), position_size=0.0, lookup_status="found")
        self.assertEqual(d.action, "hold")

    def test_an_unknown_lookup_is_never_cleared(self) -> None:
        d = decide(self._uncertain(), position_size=0.0, lookup_status="unknown")
        self.assertEqual(d.action, "hold")

    def test_an_unreadable_position_is_never_cleared(self) -> None:
        d = decide(self._uncertain(), position_size=None, lookup_status="absent")
        self.assertEqual(d.action, "hold")

    def test_unmatched_exposure_is_never_cleared(self) -> None:
        # By definition this halt means a real position the bot cannot model.
        block = BlockedEntry(kind="unmatched_exposure", order_id="", symbol="BTC/USDT")
        d = decide(block, position_size=0.0, lookup_status="absent")
        self.assertEqual(d.action, "hold")

    def test_the_same_order_is_only_recovered_once(self) -> None:
        # A second halt on the same order means the latch was not the problem.
        d = decide(self._uncertain(), position_size=0.0, lookup_status="absent",
                   last_recovered_order_id="ord-1")
        self.assertEqual(d.action, "hold")

    def test_a_different_order_is_still_recoverable(self) -> None:
        d = decide(self._uncertain(), position_size=0.0, lookup_status="absent",
                   last_recovered_order_id="ord-0")
        self.assertEqual(d.action, "restart")

    def test_every_decision_carries_a_reason(self) -> None:
        for d in (
            decide(None, position_size=0.0, lookup_status="absent"),
            decide(self._uncertain(), position_size=0.0, lookup_status="absent"),
            decide(self._uncertain(), position_size=1.0, lookup_status="absent"),
        ):
            self.assertTrue(d.reason.strip(), "a decision without a reason is unauditable")


if __name__ == "__main__":
    unittest.main()


class StateFileTests(unittest.TestCase):
    def test_missing_or_corrupt_state_reads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            missing = pathlib.Path(d) / "nope.json"
            self.assertEqual(supervisor._load_state(missing), {})
            corrupt = pathlib.Path(d) / "bad.json"
            corrupt.write_text("{not json", encoding="utf-8")
            self.assertEqual(supervisor._load_state(corrupt), {})

    def test_state_roundtrips_and_creates_its_directory(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "nested" / "state.json"
            supervisor._save_state(path, {"last_recovered_order_id": "ord-9"})
            self.assertEqual(supervisor._load_state(path)["last_recovered_order_id"], "ord-9")


class MainTests(unittest.TestCase):
    """The entry point must never reach the exchange unless it has to."""

    def _run(self, log_text: str, *extra: str) -> int:
        with tempfile.TemporaryDirectory() as d:
            log = pathlib.Path(d) / "bot.log"
            log.write_text(log_text, encoding="utf-8")
            state = pathlib.Path(d) / "state.json"
            argv = ["supervisor", "--log", str(log), "--state", str(state), *extra]
            with unittest.mock.patch.object(sys, "argv", argv):
                return supervisor.main()

    def test_a_healthy_log_exits_without_touching_the_exchange(self) -> None:
        with unittest.mock.patch.object(supervisor, "_reconcile", side_effect=AssertionError("must not be called")):
            self.assertEqual(self._run(NORMAL), 0)

    def test_unmatched_exposure_holds_without_touching_the_exchange(self) -> None:
        # No reconciliation can make unmodelled exposure safe, so none is attempted.
        with unittest.mock.patch.object(supervisor, "_reconcile", side_effect=AssertionError("must not be called")):
            self.assertEqual(self._run(UNMATCHED), 0)

    def test_an_unreadable_log_reports_failure(self) -> None:
        argv = ["supervisor", "--log", "/nonexistent/path/bot.log"]
        with unittest.mock.patch.object(sys, "argv", argv):
            self.assertEqual(supervisor.main(), 1)

    def test_dry_run_never_restarts_even_when_clearable(self) -> None:
        async def _flat(symbol, client_order_id):
            return 0.0, "absent"

        with unittest.mock.patch.object(supervisor, "_reconcile", _flat), \
             unittest.mock.patch.object(supervisor.subprocess, "run", side_effect=AssertionError("must not restart")):
            self.assertEqual(self._run(UNCERTAIN, "--dry-run"), 0)
