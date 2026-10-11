from __future__ import annotations

import unittest

from pydantic import ValidationError

from bot.config import Settings


def settings(**env: object) -> Settings:
    return Settings(_env_file=None, BINGX_API_KEY="test-key", BINGX_SECRET_KEY="test-secret", STRATEGY_MODE="volume", **env)


class TelemetryConfigTests(unittest.TestCase):
    def test_telemetry_is_off_by_default_and_loopback_only(self) -> None:
        s = settings()

        self.assertFalse(s.telemetry_enabled)
        self.assertEqual(s.telemetry_host, "127.0.0.1")
        self.assertEqual(s.telemetry_port, 3000)
        self.assertEqual(s.telemetry_allowed_origin, "http://localhost:3001")
        self.assertEqual(s.telemetry_price_interval_seconds, 5.0)

    def test_enabling_is_an_explicit_choice(self) -> None:
        self.assertTrue(settings(TELEMETRY_ENABLED="true").telemetry_enabled)

    def test_a_non_loopback_host_refuses_to_boot_even_while_disabled(self) -> None:
        for host in ("0.0.0.0", "::", "192.168.1.5", "example.com", ""):
            with self.subTest(host=host):
                with self.assertRaises(ValidationError):
                    settings(TELEMETRY_HOST=host)

    def test_loopback_names_are_accepted_and_normalised(self) -> None:
        self.assertEqual(settings(TELEMETRY_HOST="LOCALHOST").telemetry_host, "localhost")
        self.assertEqual(settings(TELEMETRY_HOST="::1").telemetry_host, "::1")

    def test_the_cors_origin_must_be_a_bare_http_origin(self) -> None:
        for origin in ("*", "localhost:3001", "http://localhost:3001/", "ftp://localhost", "http://a b", "http://localhost:3001/x"):
            with self.subTest(origin=origin):
                with self.assertRaises(ValidationError):
                    settings(TELEMETRY_ALLOWED_ORIGIN=origin)

    def test_a_valid_origin_is_kept(self) -> None:
        self.assertEqual(settings(TELEMETRY_ALLOWED_ORIGIN="https://dash.example.com").telemetry_allowed_origin, "https://dash.example.com")

    def test_port_and_interval_are_bounded(self) -> None:
        for bad in ({"TELEMETRY_PORT": 0}, {"TELEMETRY_PORT": 70000}, {"TELEMETRY_PRICE_INTERVAL_SECONDS": 0.5}, {"TELEMETRY_PRICE_INTERVAL_SECONDS": 120}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    settings(**bad)


if __name__ == "__main__":
    unittest.main()
