"""Section 7 of the long-forward runbook is a contract with the tools and the code it names.

The paths exist, the flags parse, the log lines it tells the operator to look for exist in the source, and the
inline Python commands do what the text says: they run here against files built by the real builder, with
`httpx.get` faked for the one that calls out.
"""
from __future__ import annotations

import contextlib
import io
import json
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
# What the runbook tells the operator to look for in the log, and where the engine writes it.
LOG_PHRASES = {
    "Shadow evaluation failed": "src/bot/core/trade_engine.py",
    "could not be read from the store": "src/bot/ai/shadow_ledger.py",
    "its shadow row could not be saved": "src/bot/ai/shadow_ledger.py",
    "Regime gate": "src/bot/core/regime_gate.py",
    "Strategy returned HOLD": "src/bot/core/trade_engine.py",
}


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
    """The code of each `python [-I] -c "..."` command (the runbook's snippets hold no double quote)."""
    return re.findall(r'python (?:-I )?-c "([^"]*)"', text)


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
            {"src/flat_check.py", "src/build_forward_env.py", "src/forward_schema.py", "src/main.py",
             "supabase/long_forward_shadow_entries.sql"},
        )
        for name in sorted(named):
            self.assertTrue((REPO / name).is_file(), name)

    def test_the_log_lines_it_tells_the_operator_to_look_for_exist_in_the_source(self) -> None:
        text = section(7)
        for phrase, path in LOG_PHRASES.items():
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
                self.assertIn(phrase, (REPO / path).read_text(encoding="utf-8"))

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

    def test_both_flat_checks_refuse_a_directory_that_names_another_schema(self) -> None:
        text = section(7)
        # step 5 holds the command and the rollback names it again; step 9 reuses step 5's command by reference
        self.assertGreaterEqual(text.count("src/flat_check.py --expect-schema long_forward"), 2)
        step_9 = text.split("9. **Flat check, the gate.**", 1)[1].split("10. **", 1)[0]
        self.assertIn("--expect-schema long_forward", step_9)

    def test_the_code_parity_check_covers_the_composition_root_and_a_dirty_live_checkout(self) -> None:
        text = section(7)
        self.assertEqual(text.count("src/bot src/main.py config requirements.txt"), 2)
        self.assertIn("git -C /home/ubuntu/ai-perpetual-trading status --porcelain", text)

    def test_the_rollback_is_split_by_phase_and_the_env_swap_comes_after_the_backup_exists(self) -> None:
        rollback = section(7).split("**Rollback.**", 1)[1]
        self.assertIn("window closed", rollback)
        phases = [rollback.index(marker) for marker in ("Steps 4 and 5", "Steps 6 to 9", "Step 10", "After step 11")]
        self.assertEqual(phases, sorted(phases))
        first_swap = rollback.index("mv .env .env.mirror-unused")
        self.assertGreater(first_swap, phases[1])  # not offered for steps 4 and 5, where no backup exists yet
        self.assertLess(first_swap, phases[2])
        self.assertGreater(rollback.index("pm2 delete ai-perp-long"), phases[3])  # no pm2 entry before step 11


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
        self.assertIn("--expect-schema", used["flat_check"])


class InlinePythonTests(unittest.TestCase):
    """Runs the runbook's own one-liners (exec of repo-owned text) against files built by the real builder."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.live_env = self.root / "live.env"
        self.live_env.write_text("\n".join(LIVE_LIKE_SOURCE) + "\n", encoding="utf-8")
        self.env = self.root / ".env"
        build_forward_env(self.live_env, self.env, CYCLE, mirror_live=True)
        with self.env.open("a", encoding="utf-8") as handle:  # the account owner's typing: the last one wins
            handle.write("".join(f"{key}={value}\n" for key, value in STAND_IN_KEYS.items()))
        self.snippets = inline_python(section(7))

    def snippet(self, marker: str) -> str:
        found = [snippet for snippet in self.snippets if marker in snippet]
        self.assertEqual(len(found), 1, marker)
        return found[0]

    def run_snippet(self, marker: str, *, argv: tuple = (), stdin: str = "", env: dict | None = None) -> str:
        previous, out = Path.cwd(), io.StringIO()
        os.chdir(self.root)
        try:
            with mock.patch.dict(os.environ, env or {}, clear=True), \
                    mock.patch.object(sys, "argv", ["-c", *argv]), \
                    mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                    mock.patch.object(sys, "excepthook", sys.excepthook), \
                    contextlib.redirect_stdout(out):
                exec(compile(self.snippet(marker), "<runbook>", "exec"), {"__name__": "__runbook__"})
        finally:
            os.chdir(previous)
        return out.getvalue()

    def test_there_are_exactly_the_five_documented_inline_commands(self) -> None:
        self.assertEqual(len(self.snippets), 5)

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

    def test_a_settings_failure_prints_only_the_exception_class_and_no_key_material(self) -> None:
        # keys typed in, XAI_API_KEY left blank: Settings refuses, and pydantic's own message quotes the input
        broken = self.root / "broken"
        broken.mkdir()
        lines = [line for line in self.env.read_text(encoding="utf-8").splitlines() if not line.startswith("XAI_API_KEY")]
        (broken / ".env").write_text("\n".join([*lines, "XAI_API_KEY="]) + "\n", encoding="utf-8")
        env = {"PYTHONPATH": str(REPO / "src"), "PATH": os.environ.get("PATH", "")}

        def run(code: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, "-c", code], cwd=broken, env=env, capture_output=True, text=True, check=False, timeout=60,
            )

        for marker in ("shadow_entries", "entry_gate_direction"):
            with self.subTest(marker=marker):
                plain = self.snippet(marker).split("; ", 2)[2]  # the same command without its excepthook prefix
                control = run(plain)  # the control: unprotected, the traceback quotes the typed key
                self.assertIn(STAND_IN_KEYS["BINGX_API_KEY"][:6], control.stderr)  # pydantic quotes the head of the input
                result = run(self.snippet(marker))
                self.assertEqual((result.returncode, result.stdout), (1, ""))
                self.assertEqual(result.stderr, "failed: ValidationError\n")

    def test_the_env_override_check_names_what_the_new_env_also_defines_and_nothing_else(self) -> None:
        self.assertEqual(self.run_snippet("os.environ", argv=(str(self.env),), env={"PATH": "/usr/bin"}), "[]\n")
        shadowing = {"ai_exit_max_calls_per_trade": "25", "SETUP_LEDGER_MODE": "off", "PATH": "/usr/bin"}
        out = self.run_snippet("os.environ", argv=(str(self.env),), env=shadowing)
        self.assertEqual(out, "['SETUP_LEDGER_MODE', 'ai_exit_max_calls_per_trade']\n")  # sorted: upper case first

    def test_the_key_check_says_same_empty_or_different_and_prints_no_key(self) -> None:
        same = self.root / "same.env"
        same.write_text(f"BINGX_API_KEY={FAKE_VALUES['BINGX_API_KEY']}\n", encoding="utf-8")
        empty = self.root / "empty.env"
        empty.write_text("BINGX_API_KEY=\n", encoding="utf-8")
        cases = {
            (self.live_env, same): "SAME KEY\n",
            (self.live_env, empty): "EMPTY KEY\n",
            (self.live_env, self.env): "different keys\n",
        }
        for (first, second), expected in cases.items():
            with self.subTest(second=second.name):
                out = self.run_snippet("SAME KEY", argv=(str(first), str(second)))
                self.assertEqual(out, expected)
        # the order on the command line is account 1's file, then account 2's
        self.assertIn("/home/ubuntu/ai-perpetual-trading/.env /home/ubuntu/ai-perp-long/.env", section(7))

    def test_the_after_start_check_names_what_the_app_environment_shares_with_the_env(self) -> None:
        def jlist(**extra) -> str:
            app = {"name": "ai-perp-long", "pm_cwd": "/home/ubuntu/ai-perp-long", "PATH": "/usr/bin", **extra}
            other = {"name": "ai-perp-volume", "pm2_env": {"SETUP_LEDGER_MODE": "enforce"}}
            return json.dumps([other, {"name": "ai-perp-long", "pm2_env": app}])

        self.assertEqual(self.run_snippet("pm2_env", argv=(str(self.env),), stdin=jlist()), "[]\n")
        out = self.run_snippet("pm2_env", argv=(str(self.env),), stdin=jlist(SETUP_LEDGER_MODE="off", AI_ENTRY_PAYLOAD="legacy"))
        self.assertEqual(out, "['AI_ENTRY_PAYLOAD', 'SETUP_LEDGER_MODE']\n")


if __name__ == "__main__":
    unittest.main()
