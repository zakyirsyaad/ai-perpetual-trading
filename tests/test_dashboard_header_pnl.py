"""The dashboard header must not read zero just because a price mark has arrived.

`useFeed` paints a synthetic stub block (block 0, flat, totals 0) for a sleeve that has a price mark but no block yet,
so "is there a block?" cannot be answered by "is `latest` set?". `headerPnl` counts only blocks the bot published, and
falls back to the `/pnl` figures otherwise. The dashboard has no JS test runner, so this runs the real function with
bun through a plain script; it is skipped where bun is not installed (CI runs Python only).
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "dashboard" / "scripts" / "header_pnl.check.mjs"
BUN = shutil.which("bun") or str(Path.home() / ".bun" / "bin" / "bun")


class HeaderPnlTests(unittest.TestCase):
    @unittest.skipUnless(Path(BUN).exists(), "bun is not installed here (CI runs Python only)")
    def test_a_price_only_stub_never_hides_the_figures_from_pnl(self) -> None:
        result = subprocess.run([BUN, str(SCRIPT)], capture_output=True, text=True, timeout=60, cwd=SCRIPT.parent)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("headerPnl: ok", result.stdout)

    def test_the_check_script_exists_so_the_skip_cannot_hide_a_deleted_test(self) -> None:
        self.assertTrue(SCRIPT.is_file(), SCRIPT)


if __name__ == "__main__":
    unittest.main()
