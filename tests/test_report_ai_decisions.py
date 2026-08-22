from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

import report_ai_decisions as report


class ParserTests(unittest.TestCase):
    def test_defaults_and_overrides(self) -> None:
        args = report.build_parser().parse_args(
            ["--supabase-url", "https://x.supabase.co", "--service-role-key", "k", "--limit", "5"]
        )
        self.assertEqual(args.supabase_url, "https://x.supabase.co")
        self.assertEqual(args.limit, 5)
        self.assertEqual(args.trades_table, "trades")


class SummaryTests(unittest.TestCase):
    def test_groups_by_entry_reason(self) -> None:
        rows = [
            {"entry_reason": "ai_volume", "pnl_usdt": 1.0},
            {"entry_reason": "ai_volume", "pnl_usdt": -0.5},
            {"entry_reason": "ai_volume", "pnl_usdt": 0.0},
            {"pnl_usdt": 2.0},
        ]
        summary = report._summarize(rows)
        by_reason = {item["entry_reason"]: item for item in summary["by_entry_reason"]}
        self.assertEqual(by_reason["ai_volume"]["trades"], 3)
        self.assertEqual(by_reason["ai_volume"]["wins"], 1)
        self.assertEqual(by_reason["ai_volume"]["losses"], 1)
        self.assertEqual(by_reason["ai_volume"]["breakeven"], 1)
        self.assertIn("unknown", by_reason)

    def test_empty_rows_produce_zero_totals(self) -> None:
        summary = report._summarize([])
        self.assertEqual(summary["total_trades"], 0)
        self.assertEqual(summary["by_entry_reason"], [])


class FetchTests(unittest.TestCase):
    def test_missing_credentials_raise(self) -> None:
        with self.assertRaises(RuntimeError):
            report._fetch_trades(
                supabase_url=None, service_role_key=None, schema="public", table="trades", limit=10
            )

    def test_rows_are_filtered_to_dicts(self) -> None:
        response = httpx.Response(
            200,
            request=httpx.Request("GET", "https://x.supabase.co/rest/v1/trades"),
            json=[{"entry_reason": "a", "pnl_usdt": 1.0}, "junk"],
        )
        with patch.object(report.httpx, "get", return_value=response):
            rows = report._fetch_trades(
                supabase_url="https://x.supabase.co",
                service_role_key="k",
                schema="public",
                table="trades",
                limit=10,
            )
        self.assertEqual(rows, [{"entry_reason": "a", "pnl_usdt": 1.0}])

    def test_non_list_response_raises(self) -> None:
        response = httpx.Response(
            200, request=httpx.Request("GET", "https://x.supabase.co/rest/v1/trades"), json={"oops": 1}
        )
        with patch.object(report.httpx, "get", return_value=response):
            with self.assertRaises(RuntimeError):
                report._fetch_trades(
                    supabase_url="https://x.supabase.co",
                    service_role_key="k",
                    schema="public",
                    table="trades",
                    limit=10,
                )

    def test_main_prints_json_summary(self) -> None:
        response = httpx.Response(
            200,
            request=httpx.Request("GET", "https://x.supabase.co/rest/v1/trades"),
            json=[{"entry_reason": "ai_volume", "pnl_usdt": 1.0}],
        )
        printed: list[str] = []
        with patch.object(report.httpx, "get", return_value=response), patch(
            "sys.argv",
            ["report", "--supabase-url", "https://x.supabase.co", "--service-role-key", "k"],
        ), patch("builtins.print", side_effect=lambda text: printed.append(text)):
            report.main()
        self.assertIn("ai_volume", printed[0])


if __name__ == "__main__":
    unittest.main()
