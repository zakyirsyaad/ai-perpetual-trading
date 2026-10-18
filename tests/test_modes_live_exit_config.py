"""Mode L's configuration: config/modes_live_exit.yaml and its strict loader.

The loader reuses Phase 1's readers, so a refusal names the full YAML key path. The trigger numbers
must also agree with Mode D of config/modes.yaml, whose frames the mode L run reuses.
"""
from __future__ import annotations

import dataclasses
import hashlib
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

import yaml

from bot.strategy.modes.config import (
    CostConfig,
    LiveExitConfig,
    LiveExitModeConfig,
    ModesConfigError,
    check_live_trigger_matches,
    load_modes_config,
    load_live_exit_config_with_sha256,
    parse_live_exit_config,
)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
LIVE_PATH = CONFIG_DIR / "modes_live_exit.yaml"
MODES_PATH = CONFIG_DIR / "modes.yaml"
MODE = "mode.L_cermin_live"
REMOVE = object()


def repo_doc() -> dict:
    return yaml.safe_load(LIVE_PATH.read_text(encoding="utf-8"))


def with_value(path: str, value: object) -> dict:
    """The repo document with the value at the dotted ``path`` replaced (or removed)."""
    doc = repo_doc()
    *parents, leaf = path.split(".")
    node = doc
    for key in parents:
        node = node[key]
    if value is REMOVE:
        del node[leaf]
    else:
        node[leaf] = value
    return doc


def write_text(text: str) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    handle.write(text)
    handle.close()
    path = Path(handle.name)
    return path


class RepoFileTest(unittest.TestCase):
    def test_repo_file_loads_with_the_spec_values(self):
        live, _ = load_live_exit_config_with_sha256(LIVE_PATH)

        self.assertEqual(
            live.mode_l,
            LiveExitModeConfig(
                aktif=True,
                tf_pemicu="3m",
                candle_jendela=39,
                ema_periode=21,
                atr_periode=14,
                maks_jarak_ke_ema_kali_atr=0.75,
                sl_minimal_persen=0.28,
                sl_kali_atr=1.5,
                sl_maksimal_persen=0.40,
                tp_net_usdt=56.0,
                fee_perkiraan_persen=0.05,
                batas_waktu_menit=60,
                potong_rugi_persen=0.10,
                potong_rugi_setelah_menit=10,
            ),
        )
        self.assertEqual(live.biaya, CostConfig(0.3, 0.5, 0.0))
        self.assertEqual(live.mode_l.sl_minimal_persen, 0.28)
        self.assertEqual(live.mode_l.tp_net_usdt, 56)
        self.assertEqual(live.biaya.slippage_exit_market_bps, 0.5)

    def test_the_result_is_frozen(self):
        live, _ = load_live_exit_config_with_sha256(LIVE_PATH)
        for target, field in ((live, "mode_l"), (live.mode_l, "aktif"), (live.biaya, "slippage_tp_bps")):
            with self.assertRaises(FrozenInstanceError):
                setattr(target, field, None)
        self.assertTrue(dataclasses.is_dataclass(LiveExitConfig))

    def test_parse_gives_the_same_as_the_loader(self):
        self.assertEqual(parse_live_exit_config(repo_doc()), load_live_exit_config_with_sha256(LIVE_PATH)[0])

    def test_the_trigger_numbers_agree_with_the_repo_mode_d(self):
        live, _ = load_live_exit_config_with_sha256(LIVE_PATH)
        check_live_trigger_matches(live, load_modes_config(MODES_PATH))


class RefusalTest(unittest.TestCase):
    def assert_refused(self, doc: dict, *paths: str) -> str:
        with self.assertRaises(ModesConfigError) as caught:
            parse_live_exit_config(doc)
        for path in paths:
            self.assertIn(path, str(caught.exception))
        return str(caught.exception)

    def test_unknown_missing_mistyped_and_repeated_keys_are_refused(self):
        # unknown keys, at every level
        doc = repo_doc()
        doc["extra"] = 1
        self.assert_refused(doc, "extra: unknown key")
        self.assert_refused(with_value(f"{MODE}.surprise", 1), f"{MODE}.surprise: unknown key")
        self.assert_refused(with_value("biaya.surprise", 1), "biaya.surprise: unknown key")
        doc = repo_doc()
        doc["mode"]["A_pullback_long"] = {}
        self.assert_refused(doc, "mode.A_pullback_long: unknown key")

        # missing keys, at every level
        self.assert_refused(with_value(f"{MODE}.sl_kali_atr", REMOVE), f"{MODE}.sl_kali_atr: missing key")
        self.assert_refused(with_value("biaya.slippage_tp_bps", REMOVE), "biaya.slippage_tp_bps: missing key")
        doc = repo_doc()
        del doc["biaya"]
        self.assert_refused(doc, "biaya: missing key")
        doc = repo_doc()
        del doc["mode"]["L_cermin_live"]
        self.assert_refused(doc, "mode.L_cermin_live: missing key")

        # mistyped values
        self.assert_refused(with_value(f"{MODE}.aktif", "ya"), f"{MODE}.aktif")
        self.assert_refused(with_value(f"{MODE}.aktif", 1), f"{MODE}.aktif")
        self.assert_refused(with_value(f"{MODE}.candle_jendela", 39.5), f"{MODE}.candle_jendela")
        self.assert_refused(with_value(f"{MODE}.candle_jendela", True), f"{MODE}.candle_jendela")
        self.assert_refused(with_value(f"{MODE}.tp_net_usdt", "56"), f"{MODE}.tp_net_usdt")
        self.assert_refused(with_value(f"{MODE}.tf_pemicu", "5m"), f"{MODE}.tf_pemicu")
        self.assert_refused(with_value("biaya.slippage_entry_bps", "0.3"), "biaya.slippage_entry_bps")

        # out of range
        for key in ("sl_minimal_persen", "sl_kali_atr", "sl_maksimal_persen", "tp_net_usdt",
                    "fee_perkiraan_persen", "potong_rugi_persen"):
            self.assert_refused(with_value(f"{MODE}.{key}", 0), f"{MODE}.{key}")
        for key in ("candle_jendela", "ema_periode", "atr_periode", "batas_waktu_menit",
                    "potong_rugi_setelah_menit"):
            self.assert_refused(with_value(f"{MODE}.{key}", 0), f"{MODE}.{key}")
        for key in ("slippage_entry_bps", "slippage_exit_market_bps", "slippage_tp_bps"):
            self.assert_refused(with_value(f"biaya.{key}", -0.1), f"biaya.{key}")

        # a key written twice
        text = LIVE_PATH.read_text(encoding="utf-8").replace(
            "    sl_kali_atr: 1.5\n", "    sl_kali_atr: 1.5\n    sl_kali_atr: 2\n"
        )
        path = write_text(text)
        self.addCleanup(path.unlink)
        with self.assertRaises(ModesConfigError) as caught:
            load_live_exit_config_with_sha256(path)
        self.assertIn(f"{MODE}.sl_kali_atr: key written twice", str(caught.exception))
        self.assertIn(str(path), str(caught.exception))

    def test_a_merge_key_is_refused(self):
        text = LIVE_PATH.read_text(encoding="utf-8").replace(
            "biaya:\n", "base: &b\n  x: 1\nbiaya:\n  <<: *b\n"
        )
        path = write_text(text)
        self.addCleanup(path.unlink)
        with self.assertRaises(ModesConfigError) as caught:
            load_live_exit_config_with_sha256(path)
        self.assertIn("a merge key is not allowed", str(caught.exception))

    def test_numbers_must_be_plain_decimal(self):
        original = LIVE_PATH.read_text(encoding="utf-8")
        cases = {
            "015": ("    batas_waktu_menit: 60\n", "    batas_waktu_menit: 015\n", f"{MODE}.batas_waktu_menit"),
            "1:30": ("    batas_waktu_menit: 60\n", "    batas_waktu_menit: 1:30\n", f"{MODE}.batas_waktu_menit"),
            "0x15": ("    tp_net_usdt: 56\n", "    tp_net_usdt: 0x15\n", f"{MODE}.tp_net_usdt"),
            "25_000": ("    tp_net_usdt: 56\n", "    tp_net_usdt: 5_6\n", f"{MODE}.tp_net_usdt"),
            "yes": ("    aktif: true\n", "    aktif: yes\n", f"{MODE}.aktif"),
            "!!int 015": ("    batas_waktu_menit: 60\n", "    batas_waktu_menit: !!int 015\n",
                          f"{MODE}.batas_waktu_menit"),
        }
        for label, (old, new, path_name) in cases.items():
            with self.subTest(label):
                self.assertIn(old, original)
                path = write_text(original.replace(old, new, 1))
                self.addCleanup(path.unlink)
                with self.assertRaises(ModesConfigError) as caught:
                    load_live_exit_config_with_sha256(path)
                self.assertIn(path_name, str(caught.exception))

    def test_sl_bounds_are_ordered(self):
        parse_live_exit_config(with_value(f"{MODE}.sl_maksimal_persen", 0.28))  # equal is fine
        message = self.assert_refused(
            with_value(f"{MODE}.sl_maksimal_persen", 0.27), f"{MODE}.sl_minimal_persen"
        )
        self.assertIn("sl_maksimal_persen", message)

    def test_cut_must_sit_inside_the_minimal_stop(self):
        parse_live_exit_config(with_value(f"{MODE}.potong_rugi_persen", 0.27))
        for value in (0.28, 0.35):
            with self.subTest(value=value):
                message = self.assert_refused(
                    with_value(f"{MODE}.potong_rugi_persen", value), f"{MODE}.potong_rugi_persen"
                )
                self.assertIn("sl_minimal_persen", message)


class TriggerMatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live, _ = load_live_exit_config_with_sha256(LIVE_PATH)
        cls.modes = load_modes_config(MODES_PATH)

    def with_mode_l(self, **changes) -> LiveExitConfig:
        return dataclasses.replace(
            self.live, mode_l=dataclasses.replace(self.live.mode_l, **changes)
        )

    def test_trigger_numbers_must_match_mode_d(self):
        for key, other in (
            ("candle_jendela", 40),
            ("ema_periode", 20),
            ("atr_periode", 15),
            ("maks_jarak_ke_ema_kali_atr", 0.8),
        ):
            with self.subTest(key):
                with self.assertRaises(ModesConfigError) as caught:
                    check_live_trigger_matches(self.with_mode_l(**{key: other}), self.modes)
                self.assertIn(f"{MODE}.{key}", str(caught.exception))
                self.assertIn(f"mode.D_short_sekarang.{key}", str(caught.exception))

    def test_trigger_timeframe_must_match_mode_d(self):
        nearly = dataclasses.replace(self.modes.mode_d, tf_pemicu="5m")
        modes = dataclasses.replace(self.modes, mode_d=nearly)
        with self.assertRaises(ModesConfigError) as caught:
            check_live_trigger_matches(self.live, modes)
        self.assertIn(f"{MODE}.tf_pemicu", str(caught.exception))


class ShaTest(unittest.TestCase):
    def test_sha_is_of_the_bytes_read(self):
        _, sha = load_live_exit_config_with_sha256(LIVE_PATH)
        self.assertEqual(sha, hashlib.sha256(LIVE_PATH.read_bytes()).hexdigest())

        original = LIVE_PATH.read_text(encoding="utf-8")
        path = write_text(original.replace("    sl_kali_atr: 1.5\n", "    sl_kali_atr: 1.5   # edited\n"))
        self.addCleanup(path.unlink)
        _, edited = load_live_exit_config_with_sha256(path)
        self.assertEqual(edited, hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertNotEqual(edited, sha)
        self.assertEqual(load_live_exit_config_with_sha256(str(path))[1], edited)

    def test_a_malformed_file_names_the_file(self):
        path = write_text("mode: [unclosed\n")
        self.addCleanup(path.unlink)
        with self.assertRaises(ModesConfigError) as caught:
            load_live_exit_config_with_sha256(path)
        self.assertIn(str(path), str(caught.exception))

    def test_invalid_utf8_is_refused(self):
        path = write_text("")
        self.addCleanup(path.unlink)
        path.write_bytes(b"\xff\xfe\x00")
        with self.assertRaises(ModesConfigError) as caught:
            load_live_exit_config_with_sha256(path)
        self.assertIn("not valid UTF-8", str(caught.exception))

    def test_modes_yaml_is_untouched_by_this_loader(self):
        self.assertEqual(load_modes_config(MODES_PATH).mode_d.tf_pemicu, "3m")


if __name__ == "__main__":
    unittest.main()
