import argparse
import unittest
from datetime import datetime, timedelta, timezone

from research.pine_gate import calibrate, checks
from research.pine_gate.timeparse import parse_iso_datetime, parse_iso_ms

UTC_NOON = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
NOON_MS = int(UTC_NOON.timestamp() * 1000)


class ParseIsoTests(unittest.TestCase):
    def test_offsets_in_every_postgrest_form(self):
        for text in ("2026-09-30T12:00:00Z", "2026-09-30T12:00:00z",
                     "2026-09-30T12:00:00+00:00", "2026-09-30T19:00:00+07:00",
                     "2026-09-30T19:00:00+0700", "2026-09-30T19:00:00+07",
                     "2026-09-30 12:00:00", "2026-09-30T12:00:00",
                     "2026-09-30T05:00:00-07:00", " 2026-09-30T12:00:00Z "):
            with self.subTest(text=text):
                self.assertEqual(parse_iso_ms(text), NOON_MS)
                self.assertEqual(parse_iso_datetime(text), UTC_NOON)

    def test_any_number_of_fraction_digits(self):
        for frac, micro in (("1", 100_000), ("12", 120_000), ("123", 123_000),
                            ("1234", 123_400), ("123456", 123_456), ("123456789", 123_456)):
            with self.subTest(frac=frac):
                parsed = parse_iso_datetime(f"2026-09-30T12:00:00.{frac}+00:00")
                self.assertEqual(parsed, UTC_NOON + timedelta(microseconds=micro))
        self.assertEqual(parse_iso_ms("2026-09-30T12:00:00.9999Z"), NOON_MS + 999)

    def test_garbage_is_none(self):
        for value in (None, 5, "", "2026-09-30", "yesterday", "2026-13-01T00:00:00Z",
                      "2026-09-30T12:00:00+7", "2026-09-30T12:00:00 UTC"):
            with self.subTest(value=value):
                self.assertIsNone(parse_iso_datetime(value))
                self.assertIsNone(parse_iso_ms(value))

    def test_calibrate_and_checks_share_the_parser(self):
        # one parser for both readers, so s* cannot depend on the Python version
        self.assertIs(calibrate.parse_iso_datetime, parse_iso_datetime)
        self.assertIs(checks.parse_iso_ms, parse_iso_ms)
        row = {"symbol": "BTC/USDT", "action": "OPEN_LONG", "created_at": "2026-09-30T19:00:00+0700"}
        index = calibrate._DecisionIndex([row])
        self.assertIs(index.latest_before("BTC/USDT", "OPEN_LONG", UTC_NOON), row)


class CliDateTests(unittest.TestCase):
    def test_cli_dates_are_strict_yyyy_mm_dd(self):
        from research.pine_gate import cli

        self.assertEqual(str(cli._iso_date("2026-10-14")), "2026-10-14")
        for text in ("20261014", "2026-W42-3", "2026-10-14T00:00", "14-10-2026", "2026-10-32"):
            with self.subTest(text=text):
                with self.assertRaises(argparse.ArgumentTypeError):
                    cli._iso_date(text)


if __name__ == "__main__":
    unittest.main()
