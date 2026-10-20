from __future__ import annotations

import unittest

from bot.core.setup_ledger import bucket_key


class BucketKeyTests(unittest.TestCase):
    def test_bucket_key_format(self) -> None:
        self.assertEqual(bucket_key("short", "TURUN", "atr_mid"), "SHORT|TURUN|atr_mid")
        self.assertEqual(bucket_key("LONG", "NAIK", "atr_high"), "LONG|NAIK|atr_high")


if __name__ == "__main__":
    unittest.main()
