from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import build_forward_env
from bot.config import Settings
from build_forward_env import (
    BLANKED_KEYS,
    CYCLE_KEY,
    FORWARD_OVERRIDES,
    ForwardEnvError,
    build_forward_env as build,
    main,
)

CYCLE = "2026-10-01T09:00:00+07:00"

# Fake values only. Every one of them must stay out of stdout, stderr, errors and the report.
FAKE_VALUES = {
    "BINGX_API_KEY": "x-test-bingx-key-1",
    "BINGX_SECRET_KEY": "x-test-bingx-secret-2",
    "XAI_API_KEY": "x-test-xai-key-3",
    "SUPABASE_URL": "https://x-test-project-4.example.test",
    "SUPABASE_SERVICE_ROLE_KEY": "x-test-service-role-5",
    "COINMARKETCAP_API_KEY": "x-test-cmc-6",
}

SOURCE_LINES = [
    "# live bot env (fake)",
    f"BINGX_API_KEY={FAKE_VALUES['BINGX_API_KEY']}",
    f"BINGX_SECRET_KEY={FAKE_VALUES['BINGX_SECRET_KEY']}",
    f"XAI_API_KEY={FAKE_VALUES['XAI_API_KEY']}",
    f"SUPABASE_URL={FAKE_VALUES['SUPABASE_URL']}",
    f"SUPABASE_SERVICE_ROLE_KEY={FAKE_VALUES['SUPABASE_SERVICE_ROLE_KEY']}",
    "SUPABASE_SCHEMA=public",
    "",
    "# sizing",
    "LEVERAGE=100",
    "TRADE_TARGET_PER_DAY=28",
    "ENTRY_GATE_DIRECTION=short",
    f"COINMARKETCAP_API_KEY={FAKE_VALUES['COINMARKETCAP_API_KEY']}",
    "#SUPABASE_SCHEMA=commented-out-stays",
    "TELEMETRY_ENABLED=true",
    "SYMBOLS=BTC/USDT,ETH/USDT",
]


@contextlib.contextmanager
def clean_environment():
    """Settings reads process env over a dotenv file; a shell that exports SUPABASE_SCHEMA or
    STRATEGY_MODE must not decide what these tests see. Only the file counts."""
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


class BuildForwardEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "live.env"
        self.target = self.root / "forward.env"
        self.write_source("\n".join(SOURCE_LINES) + "\n")

    def write_source(self, text: str) -> None:
        self.source.write_text(text, encoding="utf-8")

    def built(self) -> str:
        build(self.source, self.target, CYCLE)
        return self.target.read_text(encoding="utf-8")

    def assert_no_fake_value(self, text: str) -> None:
        for name, value in FAKE_VALUES.items():
            self.assertNotIn(value, text, f"{name}'s value leaked")

    def test_target_always_has_the_forward_schema(self) -> None:
        self.assertEqual(effective(self.built())["SUPABASE_SCHEMA"], "long_forward")

    def test_forward_schema_when_the_source_has_none(self) -> None:
        self.write_source(f"SUPABASE_URL={FAKE_VALUES['SUPABASE_URL']}\n")
        self.assertEqual(effective(self.built())["SUPABASE_SCHEMA"], "long_forward")

    def test_overrides_replace_and_append(self) -> None:
        text = self.built()
        values = effective(text)
        for key, value in FORWARD_OVERRIDES.items():
            self.assertEqual(values[key], value, key)
        self.assertEqual(values[CYCLE_KEY], CYCLE)
        # replaced in place, not duplicated
        self.assertEqual([k for k, _ in parse(text)].count("TRADE_TARGET_PER_DAY"), 1)
        lines = text.splitlines()
        self.assertEqual(lines.index("TRADE_TARGET_PER_DAY=6"), lines.index("LEVERAGE=100") + 1)
        # appended ones sit after the source's lines, under a header naming the script
        header = next(i for i, line in enumerate(lines) if line.startswith("#") and "build_forward_env" in line)
        self.assertGreater(header, lines.index("TELEMETRY_ENABLED=false"))
        self.assertLess(header, lines.index("ENTRY_REGIME_GATE=naik"))

    def test_every_occurrence_of_a_key_is_replaced(self) -> None:
        self.write_source(
            f"SUPABASE_URL={FAKE_VALUES['SUPABASE_URL']}\n"
            "SUPABASE_SCHEMA=public\n"
            "LEVERAGE=100\n"
            "SUPABASE_SCHEMA=other\n"
            f"export XAI_API_KEY={FAKE_VALUES['XAI_API_KEY']}\n"
            f"XAI_API_KEY = {FAKE_VALUES['XAI_API_KEY']}\n"
            "  export  ENTRY_GATE_DIRECTION=short\n"
        )
        text = self.built()
        keys = [k for k, _ in parse(text)]
        self.assertEqual(keys.count("SUPABASE_SCHEMA"), 1)
        self.assertEqual(keys.count("XAI_API_KEY"), 1)
        self.assertEqual(keys.count("ENTRY_GATE_DIRECTION"), 1)
        values = effective(text)
        self.assertEqual(values["SUPABASE_SCHEMA"], "long_forward")
        self.assertEqual(values["XAI_API_KEY"], "")
        self.assertEqual(values["ENTRY_GATE_DIRECTION"], "long")
        for key in BLANKED_KEYS:  # the file keeps SUPABASE_*, but never the second account's secrets
            self.assertNotIn(FAKE_VALUES[key], text)

    def test_case_variants_of_a_forced_key_are_dropped_and_cannot_win(self) -> None:
        self.write_source(
            f"SUPABASE_URL={FAKE_VALUES['SUPABASE_URL']}\n"
            "SUPABASE_SCHEMA=public\n"
            "supabase_schema=public\n"
            "Supabase_Schema=public\n"
            f"BINGX_API_KEY={FAKE_VALUES['BINGX_API_KEY']}\n"
            f"bingx_api_key={FAKE_VALUES['BINGX_API_KEY']}\n"
            f"Bingx_Secret_Key={FAKE_VALUES['BINGX_SECRET_KEY']}\n"
            f"xai_api_key={FAKE_VALUES['XAI_API_KEY']}\n"
            "entry_gate_direction=short\n"
            "Volume_Cycle_Started_At=2020-01-01T00:00:00+00:00\n"
        )
        text = self.built()
        keys = [k.upper() for k, _ in parse(text)]
        for key in (*FORWARD_OVERRIDES, CYCLE_KEY, *BLANKED_KEYS):
            self.assertEqual(keys.count(key), 1, key)
        for name in ("SUPABASE_SCHEMA", "BINGX_API_KEY", "BINGX_SECRET_KEY", "XAI_API_KEY"):
            self.assertIn(name + "=", text)
        for key in BLANKED_KEYS:
            self.assertNotIn(FAKE_VALUES[key], text)
        with clean_environment():
            settings = Settings(_env_file=str(self.target), BINGX_API_KEY="x", BINGX_SECRET_KEY="y", XAI_API_KEY="z")
        self.assertEqual(settings.supabase_schema, "long_forward")
        self.assertEqual(settings.entry_gate_direction, "long")
        self.assertEqual(settings.volume_cycle_started_at, datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc))
        # the blanked keys must really be empty in the file itself
        self.assertEqual({k: v for k, v in effective(text).items() if k.upper() in BLANKED_KEYS},
                         {k: "" for k in BLANKED_KEYS})

    def test_quoted_keys_count_as_occurrences(self) -> None:
        self.write_source(
            f"SUPABASE_URL={FAKE_VALUES['SUPABASE_URL']}\n"
            f"'BINGX_API_KEY'={FAKE_VALUES['BINGX_API_KEY']}\n"
            f"export 'bingx_secret_key'={FAKE_VALUES['BINGX_SECRET_KEY']}\n"
            f'"XAI_API_KEY"={FAKE_VALUES["XAI_API_KEY"]}\n'
            "'supabase_schema'=public\n"
        )
        text = self.built()
        for key in BLANKED_KEYS:
            self.assertNotIn(FAKE_VALUES[key], text)
        self.assertEqual([k for k, _ in parse(text)].count("SUPABASE_SCHEMA"), 1)
        from dotenv import dotenv_values

        parsed = dotenv_values(self.target)
        self.assertEqual(parsed["SUPABASE_SCHEMA"], "long_forward")
        self.assertEqual({k.upper() for k in parsed if k.upper() in BLANKED_KEYS}, set(BLANKED_KEYS))
        self.assertTrue(all(parsed[k] == "" for k in BLANKED_KEYS))

    def test_lowercase_supabase_url_satisfies_the_source_check(self) -> None:
        self.write_source(f"supabase_url={FAKE_VALUES['SUPABASE_URL']}\n")
        build(self.source, self.target, CYCLE)  # Settings would read it, so the source is the live env

    def test_secrets_are_blanked_and_never_printed(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--source", str(self.source), "--target", str(self.target), "--cycle-start", CYCLE])
        self.assertEqual(code, 0)
        values = effective(self.target.read_text(encoding="utf-8"))
        for key in ("BINGX_API_KEY", "BINGX_SECRET_KEY", "XAI_API_KEY"):
            self.assertEqual(values[key], "")
        self.assertIn("BINGX_API_KEY=\n", self.target.read_text(encoding="utf-8"))
        shown = out.getvalue() + err.getvalue()
        self.assert_no_fake_value(shown)
        for key in ("BINGX_API_KEY", "BINGX_SECRET_KEY", "XAI_API_KEY"):
            self.assertIn(key, shown)

    def test_report_holds_key_names_only(self) -> None:
        report = build(self.source, self.target, CYCLE)
        self.assertEqual(report.blanked_keys, BLANKED_KEYS)
        self.assertEqual(report.blanked_keys, ("BINGX_API_KEY", "BINGX_SECRET_KEY", "XAI_API_KEY"))
        self.assertEqual(report.set_keys, (*FORWARD_OVERRIDES, CYCLE_KEY))
        self.assert_no_fake_value(repr(report))
        self.assertNotIn(CYCLE, repr(report))

    def test_other_lines_and_comments_are_kept(self) -> None:
        text = self.built()
        lines = text.splitlines()
        for kept in (
            "# live bot env (fake)",
            "# sizing",
            "",
            "LEVERAGE=100",
            "#SUPABASE_SCHEMA=commented-out-stays",
            f"SUPABASE_URL={FAKE_VALUES['SUPABASE_URL']}",
            f"SUPABASE_SERVICE_ROLE_KEY={FAKE_VALUES['SUPABASE_SERVICE_ROLE_KEY']}",
            f"COINMARKETCAP_API_KEY={FAKE_VALUES['COINMARKETCAP_API_KEY']}",
        ):
            self.assertIn(kept, lines)
        # relative order of the untouched lines is unchanged
        copied = lines[: next(i for i, line in enumerate(lines) if "build_forward_env" in line) - 1]  # minus the separator
        untouched = [line for line in SOURCE_LINES if line in copied]
        self.assertEqual([line for line in copied if line in untouched], untouched)

    def test_source_without_trailing_newline_and_crlf(self) -> None:
        self.write_source(f"SUPABASE_URL={FAKE_VALUES['SUPABASE_URL']}\nLEVERAGE=100")
        text = self.built()
        self.assertEqual(effective(text)["LEVERAGE"], "100")
        self.assertTrue(text.endswith("\n"))
        self.target.unlink()
        self.source.write_bytes(f"SUPABASE_URL={FAKE_VALUES['SUPABASE_URL']}\r\nTRADE_TARGET_PER_DAY=28\r\n".encode())
        text = self.built()
        self.assertEqual(effective(text.replace("\r", ""))["TRADE_TARGET_PER_DAY"], "6")

    def test_existing_target_is_refused_and_untouched(self) -> None:
        self.target.write_text("precious\n", encoding="utf-8")
        with self.assertRaises(ForwardEnvError):
            build(self.source, self.target, CYCLE)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "precious\n")

    def test_same_path_is_refused_and_source_untouched(self) -> None:
        before = self.source.read_bytes()
        link = self.root / "alias.env"
        link.symlink_to(self.source)
        for target in (self.source, self.root / "." / "live.env", link):
            with self.assertRaises(ForwardEnvError):
                build(self.source, target, CYCLE)
        self.assertEqual(self.source.read_bytes(), before)

    def test_source_without_supabase_url_is_refused_and_leaves_no_target(self) -> None:
        for text in ("LEVERAGE=100\n", "SUPABASE_URL=\n", "#SUPABASE_URL=https://x-test-commented\n"):
            self.write_source(text)
            with self.assertRaises(ForwardEnvError):
                build(self.source, self.target, CYCLE)
            self.assertFalse(self.target.exists())

    def test_missing_source_is_refused(self) -> None:
        with self.assertRaises(ForwardEnvError):
            build(self.root / "nope.env", self.target, CYCLE)
        self.assertFalse(self.target.exists())

    def test_file_mode_is_0600_whatever_the_umask(self) -> None:
        old = os.umask(0)
        try:
            self.built()
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)

    def test_file_is_created_with_the_restricted_mode_not_chmodded_after(self) -> None:
        modes: list[int] = []
        real_open = os.open

        def spy(path, flags, mode=0o777, *args, **kwargs):
            if str(path) == str(self.target):
                modes.append(mode)
                self.assertTrue(flags & os.O_EXCL and flags & os.O_CREAT)
            return real_open(path, flags, mode, *args, **kwargs)

        os.open = spy  # type: ignore[assignment]
        try:
            self.built()
        finally:
            os.open = real_open  # type: ignore[assignment]
        self.assertEqual(modes, [0o600])

    def test_naive_or_invalid_cycle_start_is_refused(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        for bad in ("2026-10-01T09:00:00", "2026-10-01", "", "yesterday", "1760000000", future, "2026-10-01T09:00:00+07:00\nX=1"):
            with self.subTest(bad=bad):
                with self.assertRaises(ForwardEnvError):
                    build(self.source, self.target, bad)
                self.assertFalse(self.target.exists())

    def test_zulu_and_offset_cycle_start_are_accepted_as_given(self) -> None:
        for good in ("2026-10-01T02:00:00Z", "2026-10-01T09:00:00+07:00", "2026-10-01T02:00:00+00:00"):
            with self.subTest(good=good):
                build(self.source, self.target, good)
                self.assertEqual(effective(self.target.read_text(encoding="utf-8"))[CYCLE_KEY], good)
                self.target.unlink()

    def test_cli_failures_exit_1_with_one_error_line_and_no_value(self) -> None:
        self.target.write_text("x", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--source", str(self.source), "--target", str(self.target), "--cycle-start", CYCLE])
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("error:"))
        self.assert_no_fake_value(err.getvalue())

    def test_cli_undecodable_source_does_not_leak(self) -> None:
        self.source.write_bytes(b"SUPABASE_URL=x-test-\xff\xfe\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = main(["--source", str(self.source), "--target", str(self.target), "--cycle-start", CYCLE])
        self.assertEqual(code, 1)
        self.assertNotIn("x-test-", err.getvalue())
        self.assertFalse(self.target.exists())

    def test_cli_usage_error_exits_2(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["--source", str(self.source)])
        self.assertEqual(caught.exception.code, 2)


class ForwardOverridesAreRealSettingsTests(unittest.TestCase):
    def test_every_key_is_a_settings_alias(self) -> None:
        aliases = {field.alias for field in Settings.model_fields.values() if field.alias}
        for key in (*FORWARD_OVERRIDES, CYCLE_KEY, *BLANKED_KEYS):
            self.assertIn(key, aliases, f"{key} is not a Settings alias")

    def test_the_overrides_carry_what_the_long_only_invariant_needs(self) -> None:
        # Settings refuses ENTRY_REGIME_GATE=naik without these; a live .env that lacks them must not
        # leave the forward bot to inherit a mode under which the direction gate never runs.
        self.assertEqual(FORWARD_OVERRIDES["ENTRY_STRETCH_GATE_MODE"], "enforce")
        self.assertEqual(FORWARD_OVERRIDES["STRATEGY_MODE"], "ai_volume")
        self.assertEqual(FORWARD_OVERRIDES["ENTRY_GATE_DIRECTION"], "long")

    def test_a_live_env_with_the_wrong_mode_is_forced_to_the_long_only_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / "live.env", root / "forward.env"
            source.write_text(
                "\n".join([*SOURCE_LINES, "ENTRY_STRETCH_GATE_MODE=off", "STRATEGY_MODE=volume"]) + "\n",
                encoding="utf-8",
            )
            build(source, target, CYCLE)
            with clean_environment():
                settings = Settings(
                    _env_file=str(target), BINGX_API_KEY="x-test-hand-typed-1",
                    BINGX_SECRET_KEY="x-test-hand-typed-2", XAI_API_KEY="x-test-hand-typed-3",
                )
        self.assertEqual(settings.entry_stretch_gate_mode, "enforce")
        self.assertEqual(settings.strategy_mode, "ai_volume")

    def test_the_builder_reuses_clean_envs_line_parser(self) -> None:
        import clean_env

        self.assertIs(build_forward_env._key_from_line, clean_env._key_from_line)

    def test_built_file_parses_into_the_forward_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / "live.env", root / "forward.env"
            source.write_text("\n".join(SOURCE_LINES) + "\n", encoding="utf-8")
            build(source, target, CYCLE)
            # the blanked keys are typed by hand later; supply stand-ins the way a test does
            with clean_environment():
                settings = Settings(
                    _env_file=str(target),
                    BINGX_API_KEY="x-test-hand-typed-1",
                    BINGX_SECRET_KEY="x-test-hand-typed-2",
                    XAI_API_KEY="x-test-hand-typed-3",
                )
        self.assertEqual(settings.supabase_schema, "long_forward")
        self.assertEqual(settings.entry_gate_direction, "long")
        self.assertEqual(settings.symbols, ("BTC/USDT",))  # the source lists ETH too; the gate is BTC-only
        self.assertEqual(settings.entry_regime_gate, "naik")
        self.assertEqual(settings.entry_stretch_gate_mode, "enforce")
        self.assertEqual(settings.strategy_mode, "ai_volume")
        self.assertEqual(settings.entry_volatility_guard_mult, 2.0)
        self.assertEqual(settings.cooldown_minutes_after_exit, 15)
        self.assertEqual(settings.cooldown_minutes_after_entry, 0)
        self.assertEqual(settings.cooldown_minutes_after_stop, 0)
        self.assertEqual(settings.trade_target_per_day, 6)
        self.assertEqual(settings.volume_attempt_target_per_day, 6)
        self.assertIs(settings.volume_stop_at_target_enabled, False)
        self.assertEqual(settings.volume_daily_fee_budget_usdt, 0)
        self.assertEqual(settings.entry_stretch_gate_behind_schedule_entries_per_hour, 0)
        self.assertEqual(settings.entry_weekly_window, "mon 07:00-sat 00:00")
        self.assertEqual(settings.forward_kill_after_positions, 60)
        self.assertEqual(settings.forward_kill_below_per_1m, -500)
        self.assertEqual(settings.forward_review_at_positions, 150)
        self.assertIs(settings.telemetry_enabled, False)
        self.assertEqual(settings.max_stop_loss_per_day, 1000)
        self.assertEqual(settings.max_daily_drawdown_usdt, 1_000_000_000)
        self.assertEqual(settings.max_consecutive_losses, 1000)
        self.assertEqual(settings.volume_cycle_started_at, datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc))
        # untouched live settings still come through
        self.assertEqual(settings.leverage, 100)

    def test_blanked_keys_really_come_out_blank_in_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / "live.env", root / "forward.env"
            source.write_text("\n".join(SOURCE_LINES) + "\n", encoding="utf-8")
            build(source, target, CYCLE)
            self.assertNotIn(FAKE_VALUES["XAI_API_KEY"], target.read_text(encoding="utf-8"))
            with clean_environment(), self.assertRaises(Exception):
                Settings(_env_file=str(target))  # no key typed in: the bot must refuse to boot


if __name__ == "__main__":
    unittest.main()
