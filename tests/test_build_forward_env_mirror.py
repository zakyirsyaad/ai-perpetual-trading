"""`build_forward_env.py --mirror-live`: account 2 runs account 1's settings with LONG instead of SHORT.

Fake values only. Every one of them must stay out of stdout, stderr, errors and the report.
"""
from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

import build_forward_env
from bot.config import Settings
from build_forward_env import ForwardEnvError, build_forward_env as build, main

CYCLE = "2026-10-01T09:00:00+07:00"
CYCLE_UTC = datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "forward_env_legacy_golden.txt"

FAKE_VALUES = {
    "BINGX_API_KEY": "x-test-bingx-key-1",
    "BINGX_SECRET_KEY": "x-test-bingx-secret-2",
    "XAI_API_KEY": "x-test-xai-key-3",
    "SUPABASE_URL": "https://x-test-project-4.example.test",
    "SUPABASE_SERVICE_ROLE_KEY": "x-test-service-role-5",
    "COINMARKETCAP_API_KEY": "x-test-cmc-6",
}

# Account 1 as it runs on the staged-fix configuration (rollout step 2), with fake secrets.
LIVE_LIKE_SOURCE = [
    "# account 1 env (fake values)",
    "BINGX_ENV=vst",
    "BINGX_BASE_URL=https://open-api-vst.bingx.com",
    f"BINGX_API_KEY={FAKE_VALUES['BINGX_API_KEY']}",
    f"BINGX_SECRET_KEY={FAKE_VALUES['BINGX_SECRET_KEY']}",
    f"SUPABASE_URL={FAKE_VALUES['SUPABASE_URL']}",
    f"SUPABASE_SERVICE_ROLE_KEY={FAKE_VALUES['SUPABASE_SERVICE_ROLE_KEY']}",
    "SUPABASE_SCHEMA=public",
    "",
    "# model",
    "XAI_API_STYLE=chat",
    "XAI_MODEL=mimo-v2.6-pro",
    f"XAI_API_KEY={FAKE_VALUES['XAI_API_KEY']}",
    "XAI_MAX_TOKENS=4096",
    "XAI_TIMEOUT_SECONDS=90",
    f"COINMARKETCAP_API_KEY={FAKE_VALUES['COINMARKETCAP_API_KEY']}",
    "",
    "# strategy",
    "STRATEGY_MODE=ai_volume",
    "SYMBOLS=BTC/USDT,ETH/USDT",
    "LEVERAGE=100",
    "ENTRY_REGIME_GATE=map",
    "ENTRY_GATE_DIRECTION=short",
    "ENTRY_STRETCH_GATE_MODE=enforce",
    "ENTRY_STRETCH_GATE_MAX_ATR=0.75",
    "ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR=0",
    "AI_ENTRY_PAYLOAD=lean",
    "AI_LEAN_OBJECTIVE=volume",
    "AI_MIN_CONFIDENCE=0.75",
    "AI_EXIT_MAX_CALLS_PER_TRADE=0",
    "VOLUME_NEUTRAL_EXIT_ENABLED=true",
    "VOLUME_NEUTRAL_EXIT_MODE=floor",
    "VOLUME_NEUTRAL_EXIT_BASIS=net",
    "VOLUME_NEUTRAL_EXIT_MIN_NET_USDT=56",
    "SETUP_LEDGER_MODE=shadow",
    "SETUP_LEDGER_SINCE=2026-10-25T04:07:02+00:00",
    "SETUP_LEDGER_MIN_SAMPLES=60",
    "SETUP_LEDGER_HURDLE_BPS=4.0",
    "",
    "# pacing and guards",
    "VOLUME_CYCLE_STARTED_AT=2026-10-26T00:00:00+07:00",
    "VOLUME_ATTEMPT_TARGET_PER_DAY=10",
    "COOLDOWN_MINUTES_AFTER_STOP=20",
    "MAX_STOP_LOSS_PER_DAY=5",
    "MAX_DAILY_DRAWDOWN_USDT=600",
    "MAX_CONSECUTIVE_LOSSES=15",
    "ENTRY_WEEKLY_WINDOW=mon 07:00-sat 00:00",
    "",
    "# dashboard feed",
    "TELEMETRY_ENABLED=true",
    "TELEMETRY_PORT=3917",
    "TELEMETRY_PRICE_STREAM=true",
]

# The only keys mirror mode may change, whatever the source holds.
MIRROR_KEYS = {
    "SUPABASE_SCHEMA",
    "ENTRY_GATE_DIRECTION",
    "SYMBOLS",
    "TELEMETRY_PORT",
    "VOLUME_CYCLE_STARTED_AT",
    "SETUP_LEDGER_SINCE",
    "BINGX_API_KEY",
    "BINGX_SECRET_KEY",
    "XAI_API_KEY",
}
SECRET_KEYS = ("BINGX_API_KEY", "BINGX_SECRET_KEY", "XAI_API_KEY")
STAND_IN_KEYS = {
    "BINGX_API_KEY": "x-test-hand-typed-1",
    "BINGX_SECRET_KEY": "x-test-hand-typed-2",
    "XAI_API_KEY": "x-test-hand-typed-3",
}


@contextlib.contextmanager
def clean_environment():
    """Settings reads process env over a dotenv file; only the file may count here."""
    with mock.patch.dict(os.environ, {}, clear=True):
        yield


def parse(text: str) -> list[tuple[str, str]]:
    pairs = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        key, value = stripped.split("=", 1)
        pairs.append((key.strip(), value))
    return pairs


def effective(text: str) -> dict[str, str]:
    """dotenv semantics: the last occurrence of a key wins."""
    return dict(parse(text))


def key_of(line: str) -> str | None:
    pairs = parse(line)
    return pairs[0][0].upper() if pairs else None


class MirrorCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "live.env"
        self.target = self.root / "mirror.env"
        self.write_source("\n".join(LIVE_LIKE_SOURCE) + "\n")

    def write_source(self, text: str) -> None:
        self.source.write_text(text, encoding="utf-8")

    def built(self, source_text: str | None = None) -> str:
        if source_text is not None:
            self.write_source(source_text)
        build(self.source, self.target, CYCLE, mirror_live=True)
        return self.target.read_text(encoding="utf-8")

    def run_main(self, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--source", str(self.source), "--target", str(self.target), "--cycle-start", CYCLE, *extra])
        return code, out.getvalue(), err.getvalue()

    def assert_no_fake_value(self, text: str) -> None:
        for name, value in FAKE_VALUES.items():
            self.assertNotIn(value, text, f"{name}'s value leaked")


class MirrorBuildTests(MirrorCase):
    def test_account_ones_regime_payload_ledger_and_exit_settings_are_kept(self) -> None:
        values = effective(self.built())
        for key, expected in {
            "ENTRY_REGIME_GATE": "map",
            "AI_ENTRY_PAYLOAD": "lean",
            "AI_LEAN_OBJECTIVE": "volume",
            "SETUP_LEDGER_MODE": "shadow",
            "SETUP_LEDGER_MIN_SAMPLES": "60",
            "SETUP_LEDGER_HURDLE_BPS": "4.0",
            "AI_EXIT_MAX_CALLS_PER_TRADE": "0",
            "STRATEGY_MODE": "ai_volume",
            "ENTRY_STRETCH_GATE_MODE": "enforce",
            "VOLUME_ATTEMPT_TARGET_PER_DAY": "10",
            "COOLDOWN_MINUTES_AFTER_STOP": "20",
            "MAX_STOP_LOSS_PER_DAY": "5",
            "MAX_DAILY_DRAWDOWN_USDT": "600",
            "TELEMETRY_ENABLED": "true",
            "ENTRY_WEEKLY_WINDOW": "mon 07:00-sat 00:00",
        }.items():
            self.assertEqual(values[key], expected, key)

    def test_no_legacy_forward_override_leaks_into_the_mirror(self) -> None:
        source_values = {key.upper(): value for key, value in effective("\n".join(LIVE_LIKE_SOURCE)).items()}
        target_values = {key.upper(): value for key, value in effective(self.built()).items()}
        for key in build_forward_env.FORWARD_OVERRIDES:
            if key in MIRROR_KEYS:
                continue
            self.assertEqual(target_values.get(key), source_values.get(key), key)
        # the legacy forward bot's guard and window keys are not invented for a source that lacks them
        self.assertNotIn("FORWARD_KILL_AFTER_POSITIONS", target_values)

    def test_exactly_the_listed_keys_change(self) -> None:
        source_values = {key.upper(): value for key, value in effective("\n".join(LIVE_LIKE_SOURCE)).items()}
        target_values = {key.upper(): value for key, value in effective(self.built()).items()}
        changed = {key for key in {*source_values, *target_values} if source_values.get(key) != target_values.get(key)}
        self.assertEqual(changed, MIRROR_KEYS)
        self.assertEqual(target_values["SUPABASE_SCHEMA"], "long_forward")
        self.assertEqual(target_values["ENTRY_GATE_DIRECTION"], "long")
        self.assertEqual(target_values["SYMBOLS"], "BTC/USDT")
        self.assertEqual(target_values["TELEMETRY_PORT"], "3919")
        self.assertEqual(target_values["VOLUME_CYCLE_STARTED_AT"], CYCLE)
        self.assertEqual(target_values["SETUP_LEDGER_SINCE"], CYCLE)
        for key in SECRET_KEYS:
            self.assertEqual(target_values[key], "", key)

    def test_every_other_line_is_copied_byte_for_byte_and_in_order(self) -> None:
        text = self.built()
        self.assertNotIn(build_forward_env.APPENDED_HEADER, text)  # the source held every forced key
        kept_target = [line for line in text.splitlines() if key_of(line) not in MIRROR_KEYS]
        kept_source = [line for line in LIVE_LIKE_SOURCE if key_of(line) not in MIRROR_KEYS]
        self.assertEqual(kept_target, kept_source)

    def test_forced_keys_missing_from_the_source_are_appended_under_the_header(self) -> None:
        text = self.built(f"SUPABASE_URL={FAKE_VALUES['SUPABASE_URL']}\nLEVERAGE=100\n")
        lines = text.splitlines()
        header = next(i for i, line in enumerate(lines) if line.startswith("#") and "build_forward_env" in line)
        self.assertGreater(header, lines.index("LEVERAGE=100"))
        appended = [key for key, _ in parse("\n".join(lines[header:]))]
        self.assertEqual(sorted(appended), sorted(MIRROR_KEYS))
        self.assertEqual(effective(text)["SETUP_LEDGER_SINCE"], CYCLE)

    def test_every_occurrence_and_case_variant_of_a_forced_key_is_replaced(self) -> None:
        text = self.built(
            f"SUPABASE_URL={FAKE_VALUES['SUPABASE_URL']}\n"
            "SUPABASE_SCHEMA=public\n"
            "supabase_schema=public\n"
            "LEVERAGE=100\n"
            "export  Entry_Gate_Direction = short\n"
            "'symbols'=BTC/USDT,ETH/USDT\n"
            "telemetry_port=3917\n"
            "TELEMETRY_PORT=3917\n"
            "setup_ledger_since=2020-01-01T00:00:00+00:00\n"
            "Volume_Cycle_Started_At=2020-01-01T00:00:00+00:00\n"
            f"BINGX_API_KEY={FAKE_VALUES['BINGX_API_KEY']}\n"
            f"export bingx_secret_key={FAKE_VALUES['BINGX_SECRET_KEY']}\n"
            f'"XAI_API_KEY"={FAKE_VALUES["XAI_API_KEY"]}\n'
            f"xai_api_key={FAKE_VALUES['XAI_API_KEY']}\n"
        )
        keys = [key.strip("'\"").upper() for key, _ in parse(text)]
        for key in MIRROR_KEYS:
            self.assertEqual(keys.count(key), 1, key)
        values = {key.strip("'\"").upper(): value for key, value in parse(text)}
        self.assertEqual(values["SUPABASE_SCHEMA"], "long_forward")
        self.assertEqual(values["ENTRY_GATE_DIRECTION"], "long")
        self.assertEqual(values["SYMBOLS"], "BTC/USDT")
        self.assertEqual(values["TELEMETRY_PORT"], "3919")
        self.assertEqual(values["SETUP_LEDGER_SINCE"], CYCLE)
        self.assertEqual(values["VOLUME_CYCLE_STARTED_AT"], CYCLE)
        for key in SECRET_KEYS:
            self.assertEqual(values[key], "", key)
        self.assert_no_fake_value(text.replace(FAKE_VALUES["SUPABASE_URL"], ""))

    def test_report_holds_key_names_only(self) -> None:
        report = build(self.source, self.target, CYCLE, mirror_live=True)
        self.assertEqual(
            report.set_keys,
            ("SUPABASE_SCHEMA", "ENTRY_GATE_DIRECTION", "SYMBOLS", "TELEMETRY_PORT",
             "VOLUME_CYCLE_STARTED_AT", "SETUP_LEDGER_SINCE"),
        )
        self.assertEqual(report.blanked_keys, SECRET_KEYS)
        self.assertEqual(
            build_forward_env.MIRROR_OVERRIDES,
            {"SUPABASE_SCHEMA": "long_forward", "ENTRY_GATE_DIRECTION": "long",
             "SYMBOLS": "BTC/USDT", "TELEMETRY_PORT": "3919"},
        )
        self.assert_no_fake_value(repr(report))
        self.assertNotIn(CYCLE, repr(report))

    def test_the_cli_flag_builds_the_mirror_and_prints_names_only(self) -> None:
        code, out, err = self.run_main("--mirror-live")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(
            out.splitlines(),
            [
                f"built {self.target} (mode 600)",
                "set: SUPABASE_SCHEMA, ENTRY_GATE_DIRECTION, SYMBOLS, TELEMETRY_PORT, "
                "VOLUME_CYCLE_STARTED_AT, SETUP_LEDGER_SINCE",
                "blanked, type these by hand: BINGX_API_KEY, BINGX_SECRET_KEY, XAI_API_KEY",
            ],
        )
        self.assert_no_fake_value(out + err)
        self.assertEqual(effective(self.target.read_text(encoding="utf-8"))["ENTRY_REGIME_GATE"], "map")

    def test_the_cli_without_the_flag_is_still_the_legacy_build(self) -> None:
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("set: " + ", ".join((*build_forward_env.FORWARD_OVERRIDES, build_forward_env.CYCLE_KEY)), out)
        self.assertEqual(effective(self.target.read_text(encoding="utf-8"))["ENTRY_REGIME_GATE"], "naik")

    def test_the_same_safety_checks_apply_in_mirror_mode(self) -> None:
        self.target.write_text("precious\n", encoding="utf-8")
        with self.assertRaises(ForwardEnvError):
            build(self.source, self.target, CYCLE, mirror_live=True)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "precious\n")
        self.target.unlink()
        with self.assertRaises(ForwardEnvError):
            build(self.source, self.source, CYCLE, mirror_live=True)
        for text in ("LEVERAGE=100\n", "SUPABASE_URL=\n"):
            self.write_source(text)
            with self.assertRaises(ForwardEnvError):
                build(self.source, self.target, CYCLE, mirror_live=True)
            self.assertFalse(self.target.exists())
        self.write_source("\n".join(LIVE_LIKE_SOURCE) + "\n")
        for bad in ("2026-10-01T09:00:00", "yesterday", ""):
            with self.assertRaises(ForwardEnvError):
                build(self.source, self.target, bad, mirror_live=True)
            self.assertFalse(self.target.exists())

    def test_file_mode_is_0600_whatever_the_umask(self) -> None:
        old = os.umask(0)
        try:
            self.built()
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)

    def test_a_failure_exits_1_with_one_error_line_and_no_value(self) -> None:
        self.target.write_text("x", encoding="utf-8")
        code, out, err = self.run_main("--mirror-live")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(len(err.splitlines()), 1)
        self.assertTrue(err.startswith("error:"))
        self.assert_no_fake_value(err)


class MirrorSettingsTests(MirrorCase):
    def settings_from(self, path: Path) -> Settings:
        with clean_environment():
            return Settings(_env_file=str(path), **STAND_IN_KEYS)

    def test_the_fake_live_env_is_itself_a_valid_account_one_config(self) -> None:
        aliases = {field.alias for field in Settings.model_fields.values() if field.alias}
        for key, _ in parse("\n".join(LIVE_LIKE_SOURCE)):
            self.assertIn(key, aliases, f"{key} is not a Settings alias")
        settings = self.settings_from(self.source)
        self.assertEqual(settings.entry_gate_direction, "short")
        self.assertEqual(settings.entry_regime_gate, "map")
        self.assertEqual(settings.supabase_schema, "public")

    def test_the_mirror_keys_are_settings_aliases(self) -> None:
        aliases = {field.alias for field in Settings.model_fields.values() if field.alias}
        for key in (*build_forward_env.MIRROR_OVERRIDES, build_forward_env.CYCLE_KEY,
                    build_forward_env.LEDGER_SINCE_KEY, *build_forward_env.BLANKED_KEYS):
            self.assertIn(key, aliases, f"{key} is not a Settings alias")

    def test_the_built_file_loads_through_settings_with_account_ones_behaviour_and_long(self) -> None:
        self.built()
        settings = self.settings_from(self.target)
        self.assertEqual(settings.supabase_schema, "long_forward")
        self.assertEqual(settings.entry_gate_direction, "long")
        self.assertEqual(settings.symbols, ("BTC/USDT",))
        self.assertEqual(settings.telemetry_port, 3919)
        self.assertIs(settings.telemetry_enabled, True)
        self.assertEqual(settings.volume_cycle_started_at, CYCLE_UTC)
        self.assertEqual(settings.setup_ledger_since, CYCLE_UTC)
        self.assertEqual(settings.entry_regime_gate, "map")
        self.assertEqual(settings.ai_entry_payload, "lean")
        self.assertEqual(settings.ai_lean_objective, "volume")
        self.assertEqual(settings.setup_ledger_mode, "shadow")
        self.assertEqual(settings.ai_exit_max_calls_per_trade, 0)
        self.assertEqual(settings.volume_attempt_target_per_day, 10)
        self.assertEqual(settings.entry_weekly_window, "mon 07:00-sat 00:00")

    def test_the_two_accounts_settings_differ_in_exactly_the_mirrors_fields(self) -> None:
        self.built()
        account_one = self.settings_from(self.source).model_dump()
        account_two = self.settings_from(self.target).model_dump()
        differing = {name for name in account_one if account_one[name] != account_two[name]}
        self.assertEqual(
            differing,
            {"supabase_schema", "entry_gate_direction", "symbols", "telemetry_port",
             "volume_cycle_started_at", "setup_ledger_since"},
        )

    def test_blanked_keys_really_come_out_blank_so_the_bot_refuses_to_boot_until_each_is_typed(self) -> None:
        self.built()
        self.assertNotIn(FAKE_VALUES["XAI_API_KEY"], self.target.read_text(encoding="utf-8"))

        def refusal(**typed: str) -> list[tuple[tuple, str]]:
            with clean_environment(), self.assertRaises(ValidationError) as caught:
                Settings(_env_file=str(self.target), **typed)
            return [(error["loc"], error["msg"]) for error in caught.exception.errors()]

        bingx = {key: value for key, value in STAND_IN_KEYS.items() if key != "XAI_API_KEY"}
        expected_bingx = {("BINGX_API_KEY",), ("BINGX_SECRET_KEY",)}
        self.assertEqual({loc for loc, _ in refusal()}, expected_bingx)  # nothing typed in yet
        self.assertEqual({loc for loc, _ in refusal(XAI_API_KEY=STAND_IN_KEYS["XAI_API_KEY"])}, expected_bingx)
        only_xai_missing = refusal(**bingx)
        self.assertEqual(len(only_xai_missing), 1)
        self.assertIn("XAI_API_KEY is required", only_xai_missing[0][1])


class ReadbackAndCleanupTests(MirrorCase):
    """The builder renders line by line but the bot reads the file with python-dotenv: a file that does not read
    back as forced is refused (key names only), and an interrupted write leaves nothing behind. Both modes."""

    def refuses(self, source_text: str, key: str) -> None:
        for mirror in (True, False):
            with self.subTest(mirror_live=mirror):
                self.write_source(source_text)
                with self.assertRaises(ForwardEnvError) as caught:
                    build(self.source, self.target, CYCLE, mirror_live=mirror)
                self.assertIn(key, str(caught.exception))
                self.assert_no_fake_value(str(caught.exception))
                self.assertFalse(self.target.exists())

    def test_a_forced_key_inside_a_multi_line_quoted_value_is_refused_not_silently_missed(self) -> None:
        self.refuses(
            f"SUPABASE_URL={FAKE_VALUES['SUPABASE_URL']}\n"
            'NOTE="first\n'
            "SUPABASE_SCHEMA=public\n"
            'last"\n',
            "SUPABASE_SCHEMA",
        )

    def test_a_key_spelled_so_that_only_the_bots_parser_folds_it_is_refused(self) -> None:
        # U+212A (Kelvin sign) lower-cases to "k": the bot reads this later line as BINGX_API_KEY and it would win
        self.refuses(
            f"SUPABASE_URL={FAKE_VALUES['SUPABASE_URL']}\n"
            f"BINGX_API_KEY={FAKE_VALUES['BINGX_API_KEY']}\n"
            f"BINGX_API_KEY={FAKE_VALUES['BINGX_SECRET_KEY']}\n",
            "BINGX_API_KEY",
        )

    def test_an_ordinary_source_still_reads_back_as_forced_in_both_modes(self) -> None:
        for mirror in (True, False):
            with self.subTest(mirror_live=mirror):
                build(self.source, self.target, CYCLE, mirror_live=mirror)
                self.assertEqual(effective(self.target.read_text(encoding="utf-8"))["SUPABASE_SCHEMA"], "long_forward")
                self.target.unlink()

    def test_an_interrupted_write_leaves_no_file_behind(self) -> None:
        real_fdopen = os.fdopen

        class Interrupting:
            def __init__(self, handle) -> None:
                self._handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> bool:
                self._handle.close()
                return False

            def write(self, data: bytes) -> None:
                self._handle.write(data[:20])  # a source secret may be among the first bytes
                self._handle.flush()
                raise KeyboardInterrupt

        for mirror in (True, False):
            with self.subTest(mirror_live=mirror):
                with mock.patch.object(os, "fdopen", lambda fd, mode: Interrupting(real_fdopen(fd, mode))):
                    with self.assertRaises(KeyboardInterrupt):
                        build(self.source, self.target, CYCLE, mirror_live=mirror)
                self.assertFalse(self.target.exists())


class CommentedAndMultiLineSecretTests(MirrorCase):
    """Blanking covers the active assignments. A commented-out copy of a secret, or the tail of a multi-line one, would
    carry an old value into the other account's file, where its owner opens it to type the keys. Both modes."""

    OLD = ("x-test-old-commented-1", "x-test-old-commented-2", "x-test-old-commented-3")

    def commented_source(self) -> str:
        return "\n".join(
            [
                *LIVE_LIKE_SOURCE,
                f"# BINGX_API_KEY={self.OLD[0]}",
                f"#XAI_API_KEY={self.OLD[1]}",
                f"   ## export bingx_secret_key = {self.OLD[2]}",
                "# SUPABASE_SCHEMA=commented-out-stays",
                "# the BINGX_API_KEY above is account 1's",  # prose that names a key, not an assignment
            ]
        ) + "\n"

    def test_commented_out_assignments_of_a_blanked_key_are_left_out_and_counted(self) -> None:
        for mirror in (True, False):
            with self.subTest(mirror_live=mirror):
                self.write_source(self.commented_source())
                report = build(self.source, self.target, CYCLE, mirror_live=mirror)
                text = self.target.read_text(encoding="utf-8")
                for old in self.OLD:
                    self.assertNotIn(old, text)
                self.assertEqual(report.dropped_comment_lines, 3)
                self.assertIn("# SUPABASE_SCHEMA=commented-out-stays", text)  # any other comment stays
                self.assertIn("# the BINGX_API_KEY above is account 1's", text)
                self.target.unlink()

    def test_the_cli_says_how_many_it_left_out_and_never_what_they_held(self) -> None:
        self.write_source(self.commented_source())
        code, out, err = self.run_main("--mirror-live")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[-1], "left out 3 commented-out line(s) that assigned a blanked key")
        for old in self.OLD:
            self.assertNotIn(old, out)

    def test_a_source_without_such_comments_reports_none(self) -> None:
        self.assertEqual(build(self.source, self.target, CYCLE, mirror_live=True).dropped_comment_lines, 0)

    def test_a_secret_whose_value_spans_several_lines_is_refused_by_key_name_only(self) -> None:
        for mirror in (True, False):
            with self.subTest(mirror_live=mirror):
                self.write_source(
                    "\n".join(LIVE_LIKE_SOURCE) + '\nBINGX_SECRET_KEY="x-test-line-one\nx-test-line-two"\n'
                )
                with self.assertRaises(ForwardEnvError) as caught:
                    build(self.source, self.target, CYCLE, mirror_live=mirror)
                self.assertIn("BINGX_SECRET_KEY", str(caught.exception))
                self.assertNotIn("x-test-line", str(caught.exception))
                self.assertFalse(self.target.exists())


class LegacyOutputUnchangedTests(MirrorCase):
    """Characterization: these pass on the builder as it was before mirror mode existed."""

    def test_the_legacy_build_equals_the_golden_file(self) -> None:
        build(self.source, self.target, CYCLE)
        self.assertEqual(self.target.read_text(encoding="utf-8"), GOLDEN.read_text(encoding="utf-8"))

    def test_an_explicit_mirror_live_false_is_the_legacy_build(self) -> None:
        build(self.source, self.target, CYCLE, mirror_live=False)
        self.assertEqual(self.target.read_text(encoding="utf-8"), GOLDEN.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
