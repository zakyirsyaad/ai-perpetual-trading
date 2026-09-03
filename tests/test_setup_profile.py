from __future__ import annotations

import unittest

from bot.arena.setup_profile import build_setup_profile


def _features(*, last_price: float = 100.0, ema_21: float = 100.0, atr_14: float = 0.25, **extra) -> dict:
    base = {"symbol": "BTC/USDT", "last_price": last_price, "ema_21": ema_21, "atr_14": atr_14, "rsi_14": 50.0}
    base.update(extra)
    return base


class SetupProfileTests(unittest.TestCase):
    """Classify an entry into stable buckets so attribution can see the setup.

    The BingX cycle repeatedly opened LONG at the top of a range (78,293 /
    78,309 / 78,301, all stopped out) and SHORT at the bottom. Attribution
    buckets by symbol, direction, factor and hour, none of which can express
    "bought while already stretched above the mean", so the pattern stayed
    invisible and the AI repeated it.
    """

    def test_long_while_stretched_above_the_mean_is_labelled(self) -> None:
        # price 1.6 ATR above ema_21 -> stretched up
        profile = build_setup_profile(_features(last_price=100.4, ema_21=100.0, atr_14=0.25), "LONG")
        self.assertEqual(profile["stretch_bucket"], "stretch_up")
        self.assertEqual(profile["setup_key"], "LONG|stretch_up|atr_mid")

    def test_short_while_stretched_below_the_mean_is_labelled(self) -> None:
        profile = build_setup_profile(_features(last_price=99.6, ema_21=100.0, atr_14=0.25), "SHORT")
        self.assertEqual(profile["stretch_bucket"], "stretch_down")
        self.assertEqual(profile["setup_key"], "SHORT|stretch_down|atr_mid")

    def test_price_near_the_mean_is_flat(self) -> None:
        profile = build_setup_profile(_features(last_price=100.1, ema_21=100.0, atr_14=0.25), "LONG")
        self.assertEqual(profile["stretch_bucket"], "stretch_flat")

    def test_atr_regime_buckets(self) -> None:
        for atr_pct, expected in ((0.10, "atr_low"), (0.25, "atr_mid"), (0.50, "atr_high")):
            with self.subTest(atr_pct=atr_pct):
                profile = build_setup_profile(_features(atr_14=atr_pct), "LONG")
                self.assertEqual(profile["atr_regime"], expected)

    def test_measurements_are_preserved_for_later_threshold_tuning(self) -> None:
        profile = build_setup_profile(_features(last_price=100.4, ema_21=100.0, atr_14=0.25, rsi_14=71.5), "LONG")
        self.assertAlmostEqual(profile["stretch_atr"], 1.6, places=3)
        self.assertAlmostEqual(profile["atr_pct"], 0.249, places=3)
        self.assertAlmostEqual(profile["rsi_14"], 71.5, places=3)

    def test_missing_features_degrade_to_unknown_without_raising(self) -> None:
        for features in (None, {}, _features(atr_14=0.0), _features(last_price=0.0)):
            with self.subTest(features=features):
                profile = build_setup_profile(features, "LONG")
                self.assertEqual(profile["setup_key"], "LONG|unknown|unknown")

    def test_unknown_direction_is_normalised(self) -> None:
        profile = build_setup_profile(_features(), "")
        self.assertTrue(profile["setup_key"].startswith("UNKNOWN|"))

    def test_zero_ema_does_not_divide_by_zero(self) -> None:
        profile = build_setup_profile(_features(ema_21=0.0), "LONG")
        self.assertEqual(profile["stretch_bucket"], "unknown")


if __name__ == "__main__":
    unittest.main()


class FactorFamilyTests(unittest.TestCase):
    """Record the category mix of the factors that triggered an entry.

    Arena factors already carry a `category` (momentum / trend / volatility /
    volume / microstructure) and it is persisted in factor_metadata.snapshots.
    A retrospective test suggested microstructure-led entries were worse, but it
    held on OKX (n=281) and had no usable sample on BingX (n=5), so this records
    the axis for a prospective test rather than acting on it.
    """

    def _snaps(self, *categories: str) -> list[dict]:
        return [{"category": c} for c in categories]

    def test_microstructure_led_entry(self) -> None:
        p = build_setup_profile(_features(), "LONG", self._snaps("microstructure", "microstructure", "microstructure", "trend", "momentum"))
        self.assertAlmostEqual(p["micro_share"], 0.6, places=3)
        self.assertEqual(p["factor_family"], "micro_led")
        self.assertEqual(p["family_key"], "LONG|micro_led")

    def test_trend_led_entry(self) -> None:
        p = build_setup_profile(_features(), "SHORT", self._snaps("trend", "momentum", "volume", "trend", "trend"))
        self.assertEqual(p["micro_share"], 0.0)
        self.assertEqual(p["factor_family"], "trend_led")

    def test_mixed_entry(self) -> None:
        p = build_setup_profile(_features(), "LONG", self._snaps("microstructure", "microstructure", "trend", "trend", "momentum"))
        self.assertAlmostEqual(p["micro_share"], 0.4, places=3)
        self.assertEqual(p["factor_family"], "mixed")

    def test_absent_snapshots_degrade_to_unknown(self) -> None:
        for snaps in (None, [], [{"no_category": 1}]):
            with self.subTest(snaps=snaps):
                p = build_setup_profile(_features(), "LONG", snaps)
                self.assertEqual(p["factor_family"], "unknown")
                self.assertIsNone(p["micro_share"])

    def test_family_does_not_change_the_setup_key(self) -> None:
        # setup_key stays a 3-axis bucket; adding family as a 4th axis would
        # triple the bucket count and starve each one of sample.
        a = build_setup_profile(_features(), "LONG", self._snaps("microstructure") * 5)
        b = build_setup_profile(_features(), "LONG", self._snaps("trend") * 5)
        self.assertEqual(a["setup_key"], b["setup_key"])
        self.assertNotEqual(a["family_key"], b["family_key"])
