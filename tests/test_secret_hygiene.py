from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = (
    re.compile(r"fe_oa_[0-9a-fA-F]{32,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{36}"),
)


class SecretHygieneTests(unittest.TestCase):
    def test_tracked_files_do_not_contain_real_api_key_patterns(self) -> None:
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()

        offenders: list[str] = []
        for relative_path in tracked:
            path = ROOT / relative_path
            if path.suffix in {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".pdf"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    offenders.append(relative_path)
                    break

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
