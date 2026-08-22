from __future__ import annotations

import unittest

from replay_ai_decisions import deduplicate_requests
from report_ai_usage import build_parser, group_by_day, render_report


def _row(**overrides) -> dict:
    base = dict(
        created_at="2026-08-22T10:00:00+00:00",
        purpose="entry",
        success=True,
        attempts=1,
        input_tokens=900,
        output_tokens=40,
        total_tokens=940,
        reasoning_tokens=0,
        cached_tokens=0,
        tokens_estimated=False,
        deduplicated=False,
        repaired=False,
        error_category=None,
        estimated_cost_usd=None,
    )
    base.update(overrides)
    return base


class UsageReportTests(unittest.TestCase):
    def test_parser_exposes_days_and_json(self) -> None:
        args = build_parser().parse_args(["--days", "7", "--json"])
        self.assertEqual(args.days, 7)
        self.assertTrue(args.json)

    def test_empty_report_is_explicit(self) -> None:
        self.assertIn("No AI call metrics", render_report([]))

    def test_report_includes_purpose_error_and_day_breakdowns(self) -> None:
        rows = [
            _row(),
            _row(purpose="exit", total_tokens=300, input_tokens=280, output_tokens=20),
            _row(purpose="exit", success=False, error_category="timeout", attempts=2),
            _row(created_at="2026-08-21T10:00:00+00:00"),
        ]
        text = render_report(rows)
        self.assertIn("calls=4", text)
        self.assertIn("entry", text)
        self.assertIn("exit", text)
        self.assertIn("timeout", text)
        self.assertIn("2026-08-21", text)
        self.assertIn("2026-08-22", text)
        self.assertIn("observability-only", text)

    def test_group_by_day(self) -> None:
        grouped = group_by_day([_row(), _row(created_at="2026-08-21T01:00:00+00:00")])
        self.assertEqual(sorted(grouped), ["2026-08-21", "2026-08-22"])

    def test_report_counts_dedupe_savings(self) -> None:
        text = render_report([_row(deduplicated=True), _row()])
        self.assertIn("deduplicated_calls=1", text)


class ReplayDeduplicationTests(unittest.TestCase):
    def test_identical_stored_requests_are_deduplicated(self) -> None:
        request = {"task": "entry_decision", "symbols": [{"symbol": "BTC/USDT"}]}
        rows = [{"payload_json": {"request": request}} for _ in range(3)]
        summary = deduplicate_requests(rows)
        self.assertEqual(summary["requests"], 3)
        self.assertEqual(summary["unique_requests"], 1)
        self.assertEqual(summary["duplicate_requests"], 2)
        self.assertGreater(summary["estimated_duplicate_tokens"], 0)

    def test_key_order_does_not_create_false_uniques(self) -> None:
        rows = [
            {"payload_json": {"request": {"a": 1, "b": 2}}},
            {"payload_json": {"request": {"b": 2, "a": 1}}},
        ]
        self.assertEqual(deduplicate_requests(rows)["unique_requests"], 1)

    def test_json_encoded_payload_is_parsed(self) -> None:
        rows = [{"payload_json": '{"request": {"a": 1}}'}]
        self.assertEqual(deduplicate_requests(rows)["unique_requests"], 1)

    def test_unusable_rows_are_skipped_not_counted(self) -> None:
        rows = [{"payload_json": "not json"}, {"payload_json": {"no_request": True}}, {}]
        summary = deduplicate_requests(rows)
        self.assertEqual(summary["requests"], 0)
        self.assertEqual(summary["records_skipped"], 3)

    def test_legacy_full_context_requests_still_report_symbols(self) -> None:
        rows = [
            {
                "payload_json": {
                    "request": {"market_context": {"technicals": [{"symbol": "ETH/USDT"}]}},
                }
            }
        ]
        summary = deduplicate_requests(rows)
        self.assertEqual(summary["unique_request_details"][0]["symbols"], ["ETH/USDT"])

    def test_distinct_requests_stay_distinct(self) -> None:
        rows = [
            {"payload_json": {"request": {"symbols": [{"symbol": "BTC/USDT"}]}}},
            {"payload_json": {"request": {"symbols": [{"symbol": "ETH/USDT"}]}}},
        ]
        self.assertEqual(deduplicate_requests(rows)["duplicate_requests"], 0)


if __name__ == "__main__":
    unittest.main()
