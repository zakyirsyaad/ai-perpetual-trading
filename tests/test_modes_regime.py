"""The mode selector of the LONG module: ``pilih_mode`` of spec section 2.2, as a pure function."""

import unittest

from bot.strategy.modes.regime import NAIK, SIDEWAYS, TIDAK_ENTRY, TURUN, pilih_mode


class PilihModeTests(unittest.TestCase):
    def test_sideways_when_gap_is_below_the_band(self):
        # checked before the trend
        self.assertEqual(pilih_mode(100.1, 100.0, 1.0, 1.0, 2.0, 0.2), SIDEWAYS)

    def test_gap_equal_to_the_band_is_not_sideways(self):
        self.assertEqual(pilih_mode(101.0, 100.0, 5.0, 3.0, 2.0, 0.2), NAIK)  # strict <

    def test_naik_needs_the_4h_close_above_its_ema(self):
        self.assertEqual(pilih_mode(102.0, 100.0, 1.0, 2.0, 2.0, 0.2), TIDAK_ENTRY)

    def test_turun_ignores_the_4h_filter(self):
        self.assertEqual(pilih_mode(98.0, 100.0, 1.0, 9.0, 1.0, 0.2), TURUN)

    def test_sideways_is_checked_before_both_trends(self):
        self.assertEqual(pilih_mode(100.1, 100.0, 1.0, 3.0, 2.0, 0.2), SIDEWAYS)  # NAIK would hold
        self.assertEqual(pilih_mode(99.9, 100.0, 1.0, 1.0, 2.0, 0.2), SIDEWAYS)  # TURUN would hold

    def test_the_band_is_the_given_multiple_of_atr(self):
        self.assertEqual(pilih_mode(100.8, 100.0, 2.0, 1.0, 2.0, 0.5), SIDEWAYS)  # gap 0.8 < 1.0
        self.assertEqual(pilih_mode(101.2, 100.0, 2.0, 3.0, 2.0, 0.5), NAIK)  # gap 1.2 is not < 1.0

    def test_any_missing_input_is_tidak_entry(self):
        base = {
            "sma20_1h": 101.0,
            "sma50_1h": 100.0,
            "atr14_1h": 5.0,
            "close_4h": 3.0,
            "ema50_4h": 2.0,
        }
        self.assertEqual(pilih_mode(**base, sideways_jarak_kali_atr=0.2), NAIK)  # base is NAIK
        for name in base:
            with self.subTest(missing=name):
                inputs = {**base, name: None}
                self.assertEqual(pilih_mode(**inputs, sideways_jarak_kali_atr=0.2), TIDAK_ENTRY)


if __name__ == "__main__":
    unittest.main()
