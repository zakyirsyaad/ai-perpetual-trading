from __future__ import annotations

import unittest
from decimal import Decimal

from bot.exchange.contracts import MaintenanceMarginTier
from bot.exchange.liquidation import estimate_isolated_liquidation_constraint, stop_loss_is_strictly_before_liquidation
from bot.models import PositionDirection


def _tiers() -> list[MaintenanceMarginTier]:
    return [
        MaintenanceMarginTier(Decimal("0"), Decimal("50000"), Decimal("0.004"), Decimal("0")),
        MaintenanceMarginTier(Decimal("50000"), Decimal("250000"), Decimal("0.005"), Decimal("50")),
    ]


class PiecewiseLiquidationTests(unittest.TestCase):
    def test_short_crossing_boundary_uses_the_self_consistent_second_tier(self) -> None:
        constraint = estimate_isolated_liquidation_constraint(
            reference_price=100.0,
            quantity_base=499.0,
            leverage=100,
            taker_close_fee_rate=0.0005,
            tiers=_tiers(),
        )

        self.assertAlmostEqual(constraint.estimated_short_liquidation_price, 100.547191, places=5)
        self.assertEqual(constraint.short_maintenance_margin_ratio, 0.005)
        self.assertEqual(constraint.long_maintenance_margin_ratio, 0.004)
        self.assertIsNone(constraint.maintenance_margin_ratio)
        self.assertFalse(
            stop_loss_is_strictly_before_liquidation(
                direction=PositionDirection.SHORT,
                stop_loss_pct=0.5473,
                constraint=constraint,
            )
        )

    def test_long_crossing_boundary_uses_the_self_consistent_first_tier(self) -> None:
        constraint = estimate_isolated_liquidation_constraint(
            reference_price=100.5,
            quantity_base=499.0,
            leverage=100,
            taker_close_fee_rate=0.0005,
            tiers=_tiers(),
        )

        self.assertLess(constraint.estimated_long_liquidation_price * 499.0, 50000.0)
        self.assertEqual(constraint.long_maintenance_margin_ratio, 0.004)
        self.assertEqual(constraint.short_maintenance_margin_ratio, 0.005)

    def test_gap_between_tiers_fails_closed(self) -> None:
        tiers = [
            MaintenanceMarginTier(Decimal("0"), Decimal("50000"), Decimal("0.004"), Decimal("0")),
            MaintenanceMarginTier(Decimal("60000"), Decimal("250000"), Decimal("0.005"), Decimal("50")),
        ]
        with self.assertRaises(ValueError):
            estimate_isolated_liquidation_constraint(
                reference_price=100.0,
                quantity_base=499.0,
                leverage=100,
                taker_close_fee_rate=0.0005,
                tiers=tiers,
            )

    def test_uncovered_entry_notional_between_tiers_fails_closed(self) -> None:
        tiers = [
            MaintenanceMarginTier(Decimal("0"), Decimal("50000"), Decimal("0.004"), Decimal("0")),
            MaintenanceMarginTier(Decimal("50001"), Decimal("250000"), Decimal("0.005"), Decimal("50")),
        ]
        with self.assertRaises(ValueError):
            estimate_isolated_liquidation_constraint(
                reference_price=50000.05,
                quantity_base=1.0,
                leverage=100,
                taker_close_fee_rate=0.0005,
                tiers=tiers,
            )

    def test_later_tier_gap_fails_even_when_entry_and_liquidation_use_first_tier(self) -> None:
        tiers = [
            MaintenanceMarginTier(Decimal("0"), Decimal("50000"), Decimal("0.004"), Decimal("0")),
            MaintenanceMarginTier(Decimal("60000"), Decimal("250000"), Decimal("0.005"), Decimal("50")),
        ]
        with self.assertRaises(ValueError):
            estimate_isolated_liquidation_constraint(
                reference_price=100.0,
                quantity_base=100.0,
                leverage=100,
                taker_close_fee_rate=0.0005,
                tiers=tiers,
            )


if __name__ == "__main__":
    unittest.main()
