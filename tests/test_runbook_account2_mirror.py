"""Section 7 of the long-forward runbook is a contract with the tools it names.

The paths exist, the flags parse, and the two inline Python commands do what the text says: they run here
against a mirror `.env` built by the real builder, with `httpx.get` faked for the one that calls out.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from build_forward_env import build_forward_env
from tests.test_build_forward_env_mirror import (
    CYCLE,
    CYCLE_UTC,
    FAKE_VALUES,
    LIVE_LIKE_SOURCE,
    STAND_IN_KEYS,
)

REPO = Path(__file__).resolve().parents[1]
RUNBOOK = REPO / "docs" / "runbooks" / "long-forward-test.md"
HEADING = "## 7. Second start: account 1's latest code on account 2 (LONG)"
ARGPARSE_TOOLS = ("build_forward_env", "forward_schema", "report_live_stages", "supervisor", "flat_check")


def runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def section(number: int) -> str:
    match = re.search(rf"^## {number}\. .*?(?=^## \d+\. |\Z)", runbook(), re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"the runbook has no section {number}")
    return match.group(0)


def commands(text: str) -> list[str]:
    """Every command in the fenced bash blocks of `text` (continuation lines joined), and every inline
    code span that names a script of src/."""
    blocks = re.findall(r"```bash\n(.*?)```", text, re.DOTALL)
    lines = [line.strip() for block in blocks for line in re.sub(r"\\\n\s*", " ", block).splitlines()]
    spans = [span for span in re.findall(r"`([^`\n]+)`", text) if re.search(r"src/\w+\.py", span)]
    return [line for line in lines if line] + spans


def inline_python(text: str) -> list[str]:
    """The code of each `python -c "..."` command (the runbook's snippets hold no double quote)."""
    return re.findall(r'python -c "(.*)"\s*$', text, re.MULTILINE)


class RunbookStructureTests(unittest.TestCase):
    def test_section_7_exists_and_the_status_line_and_section_6_point_to_it(self) -> None:
        self.assertIn(HEADING, runbook())
        self.assertIn("Section 7", runbook().split("\n## 1.")[0])
        self.assertIn("Section 7", section(6))

    def test_every_repo_path_named_in_section_7_exists(self) -> None:
        named = set(
            re.findall(r"(?<![\w/.~-])((?:src|supabase|docs|config)/[\w./-]*\w\.(?:py|sql|md|yaml|txt))", section(7))
        )
        self.assertGreaterEqual(
            named,
            {"src/flat_check.py", "src/build_forward_env.py", "src/forward_schema.py",
             "supabase/long_forward_shadow_entries.sql"},
        )
        for name in sorted(named):
            self.assertTrue((REPO / name).is_file(), name)

    def test_the_new_tag_is_used_consistently_and_the_rollback_names_the_old_one(self) -> None:
        text = section(7)
        self.assertEqual(set(re.findall(r"git tag -a (\S+)", text)), {"long-forward-v2"})
        self.assertIn("git push origin long-forward-v2", text)
        self.assertIn("git checkout long-forward-v2", text)
        self.assertIn("git checkout long-forward-v1", text)

    def test_the_flat_check_runs_from_the_clone_and_the_env_is_built_from_account_one_into_account_two(self) -> None:
        text = section(7)
        self.assertIn("cd /home/ubuntu/ai-perp-long && PYTHONPATH=src .venv/bin/python src/flat_check.py", text)
        self.assertIn("src/build_forward_env.py --mirror-live", text)
        self.assertIn("--source /home/ubuntu/ai-perpetual-trading/.env", text)
        self.assertIn("--target /home/ubuntu/ai-perp-long/.env", text)


class RunbookCommandTests(unittest.TestCase):
    def test_flags_used_with_each_script_are_accepted_by_it(self) -> None:
        used: dict[str, set[str]] = {}
        for command in commands(section(7)):
            for script in re.findall(r"src/(\w+)\.py", command):
                if script in ARGPARSE_TOOLS:  # not main.py: its flags in the pm2 command are pm2's
                    used.setdefault(script, set()).update(re.findall(r"(?<![\w-])--[a-z][a-z-]*", command))
        self.assertGreaterEqual(set(used), {"build_forward_env", "flat_check", "forward_schema", "report_live_stages"})
        env = {"PYTHONPATH": str(REPO / "src"), "PATH": os.environ.get("PATH", "")}
        for script, flags in sorted(used.items()):
            if not flags:
                continue
            result = subprocess.run(
                [sys.executable, str(REPO / "src" / f"{script}.py"), "--help"],
                capture_output=True, text=True, check=False, timeout=60, env=env, cwd=tempfile.gettempdir(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for flag in sorted(flags):
                self.assertIn(flag, result.stdout, f"{script}.py does not take {flag}")
        self.assertIn("--mirror-live", used["build_forward_env"])
        self.assertIn("--only", used["forward_schema"])


class InlinePythonTests(unittest.TestCase):
    """Runs the runbook's own one-liners (exec of repo-owned text) against a real built mirror `.env`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        source = self.root / "live.env"
        source.write_text("\n".join(LIVE_LIKE_SOURCE) + "\n", encoding="utf-8")
        build_forward_env(source, self.root / ".env", CYCLE, mirror_live=True)
        with (self.root / ".env").open("a", encoding="utf-8") as handle:  # the account owner's typing: the last one wins
            handle.write("".join(f"{key}={value}\n" for key, value in STAND_IN_KEYS.items()))
        self.snippets = inline_python(section(7))

    def run_snippet(self, marker: str) -> str:
        found = [snippet for snippet in self.snippets if marker in snippet]
        self.assertEqual(len(found), 1, marker)
        previous, out = Path.cwd(), io.StringIO()
        os.chdir(self.root)
        try:
            with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(out):
                exec(compile(found[0], "<runbook>", "exec"), {"__name__": "__runbook__"})
        finally:
            os.chdir(previous)
        return out.getvalue()

    def test_there_are_exactly_the_two_documented_inline_commands(self) -> None:
        self.assertEqual(len(self.snippets), 2)

    def test_the_table_check_asks_the_rest_api_for_shadow_entries_in_this_schema_and_prints_no_secret(self) -> None:
        with mock.patch.object(httpx, "get", return_value=SimpleNamespace(status_code=200)) as get:
            out = self.run_snippet("shadow_entries")
        self.assertEqual(out, "schema long_forward | shadow_entries answers 200\n")
        (url,), kwargs = get.call_args
        self.assertEqual(url, f"{FAKE_VALUES['SUPABASE_URL']}/rest/v1/shadow_entries")
        self.assertEqual(kwargs["params"], {"select": "id", "limit": "1"})
        self.assertEqual(kwargs["headers"]["Accept-Profile"], "long_forward")
        self.assertEqual(kwargs["headers"]["apikey"], FAKE_VALUES["SUPABASE_SERVICE_ROLE_KEY"])
        self.assertNotIn(FAKE_VALUES["SUPABASE_SERVICE_ROLE_KEY"], out)

    def test_the_effective_values_command_shows_the_mirror_and_no_secret(self) -> None:
        out = self.run_snippet("entry_gate_direction")
        values = dict(line.split(" = ", 1) for line in out.splitlines())
        stamps = ("setup_ledger_since", "volume_cycle_started_at")
        self.assertEqual(
            {name: value for name, value in values.items() if name not in stamps},
            {
                "supabase_schema": "long_forward",
                "entry_gate_direction": "long",
                "entry_regime_gate": "map",
                "ai_entry_payload": "lean",
                "ai_lean_objective": "volume",
                "setup_ledger_mode": "shadow",
                "ai_exit_max_calls_per_trade": "0",
                "symbols": "('BTC/USDT',)",
                "telemetry_enabled": "True",
                "telemetry_port": "3919",
                "entry_weekly_window": "mon 07:00-sat 00:00",
            },
        )
        for name in stamps:
            self.assertEqual(datetime.fromisoformat(values[name]), CYCLE_UTC, name)
        for secret in (*STAND_IN_KEYS.values(), *FAKE_VALUES.values()):
            self.assertNotIn(secret, out)


if __name__ == "__main__":
    unittest.main()
