"""The LONG module configuration: config/modes.yaml and its strict loader.

The file is hand-edited and a lock file later binds its sha256, so every refusal has to name the
full YAML key path: a typo must be findable from the message alone.
"""
from __future__ import annotations

import copy
import dataclasses
import functools
import hashlib
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from bot.strategy.modes.config import (
    BreakoutConfig,
    GeneralConfig,
    ModesConfig,
    ModesConfigError,
    NearMeanConfig,
    PairConfig,
    PullbackConfig,
    SelectorConfig,
    SidewaysConfig,
    TestConfig,
    config_sha256,
    load_modes_config,
    parse_modes_config,
)

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config" / "modes.yaml"
WIB = timezone(timedelta(hours=7))
REMOVE = object()

# Every leaf of config/modes.yaml belongs to exactly one rule table (checked below), so a rule
# can be exercised on all of its keys at once.
POSITIVE_FLOATS = """
umum.fee_taker_persen umum.slippage_persen umum.penjaga_volatilitas_kali_median_atr
umum.notional_usdt pemilih_mode.sideways_jarak_kali_atr
mode.A_pullback_long.maks_jarak_di_atas_sma20_1h_kali_atr
mode.A_pullback_long.tp_persen mode.A_pullback_long.sl_persen
mode.B_breakout_long.volume_kali_rata_rata
mode.B_breakout_long.maks_jarak_di_atas_sma20_1h_kali_atr
mode.B_breakout_long.tp_persen mode.B_breakout_long.sl_persen
mode.C_sideways.bollinger_std mode.C_sideways.min_jarak_tp_persen mode.C_sideways.sl_persen
mode.D_short_sekarang.maks_jarak_ke_ema_kali_atr
mode.D_short_sekarang.tp_persen mode.D_short_sekarang.sl_persen
mode.D2_pullback_short.maks_jarak_di_bawah_sma20_1h_kali_atr
mode.D2_pullback_short.tp_persen mode.D2_pullback_short.sl_persen
uji.pindah_5m_jika_entry_per_hari_naik_kurang_dari
""".split()
SIGNED_FLOATS = """
uji.lolos_hasil_per_1_juta_lebih_dari uji.lolos_hasil_tanpa_teratas_lebih_dari
""".split()
FAKTOR_K = ["pair.BTC-USDT.faktor_k", "pair.ETH-USDT.faktor_k"]
WHOLE_AT_LEAST_ONE = """
umum.maks_posisi_total umum.maks_posisi_per_pair umum.maks_entry_per_hari_total
umum.maks_entry_per_hari_per_pair
pemilih_mode.sma_cepat_1h pemilih_mode.sma_lambat_1h pemilih_mode.ema_4h
mode.A_pullback_long.ema_pemicu mode.A_pullback_long.konfirmasi_dalam_candle
mode.A_pullback_long.batas_waktu_menit
mode.B_breakout_long.lookback_candle mode.B_breakout_long.periode_volume_rata_rata
mode.B_breakout_long.batas_waktu_menit
mode.C_sideways.bollinger_periode mode.C_sideways.batas_waktu_menit
mode.D_short_sekarang.candle_jendela mode.D_short_sekarang.ema_periode
mode.D_short_sekarang.atr_periode mode.D_short_sekarang.batas_waktu_menit
mode.D2_pullback_short.ema_pemicu mode.D2_pullback_short.konfirmasi_dalam_candle
mode.D2_pullback_short.batas_waktu_menit
uji.lolos_minimal_posisi uji.hari_naik_minimal_jam_naik
""".split()
WHOLE_AT_LEAST_ZERO = ["umum.jeda_menit", "uji.lolos_buang_posisi_teratas"]
FLAGS = """
umum.ai_boleh_buka_tutup pair.BTC-USDT.aktif pair.ETH-USDT.aktif mode.A_pullback_long.aktif
mode.B_breakout_long.aktif mode.C_sideways.aktif mode.D_short_sekarang.aktif
mode.D2_pullback_short.aktif
""".split()
TEXTS = """
umum.zona_waktu umum.jendela_entry_mingguan mode.A_pullback_long.tf_pemicu
mode.D_short_sekarang.tf_pemicu mode.D2_pullback_short.tf_pemicu
pair.ETH-USDT.harus_sama_kondisi_dengan
""".split()
DATES = ["uji.latihan_mulai", "uji.latihan_selesai", "uji.ujian_selesai"]
NUMERIC = POSITIVE_FLOATS + SIGNED_FLOATS + FAKTOR_K + WHOLE_AT_LEAST_ONE + WHOLE_AT_LEAST_ZERO
OPTIONAL = "pair.ETH-USDT.harus_sama_kondisi_dengan"

# YAML block -> the dataclass that holds it
BLOCK_OF = {
    "umum": lambda cfg: cfg.umum,
    "pemilih_mode": lambda cfg: cfg.pemilih_mode,
    "pair.BTC-USDT": lambda cfg: cfg.pair["BTC-USDT"],
    "pair.ETH-USDT": lambda cfg: cfg.pair["ETH-USDT"],
    "mode.A_pullback_long": lambda cfg: cfg.mode_a,
    "mode.B_breakout_long": lambda cfg: cfg.mode_b,
    "mode.C_sideways": lambda cfg: cfg.mode_c,
    "mode.D_short_sekarang": lambda cfg: cfg.mode_d,
    "mode.D2_pullback_short": lambda cfg: cfg.mode_d2,
    "uji": lambda cfg: cfg.uji,
}
# The one place where a YAML key and its dataclass field differ: modes A and D2 share a type.
RENAMED = {
    "mode.A_pullback_long.maks_jarak_di_atas_sma20_1h_kali_atr": "maks_jarak_sma20_1h_kali_atr",
    "mode.D2_pullback_short.maks_jarak_di_bawah_sma20_1h_kali_atr": "maks_jarak_sma20_1h_kali_atr",
}


@functools.lru_cache(maxsize=1)
def _repo_doc() -> dict:
    return yaml.safe_load(REPO_CONFIG.read_text(encoding="utf-8"))


def valid_doc() -> dict:
    """A fresh, mutable copy of the repository file as the plain mapping yaml gives."""
    return copy.deepcopy(_repo_doc())


def leaves(node, prefix=()):
    """Dotted path of every value that is not a mapping."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from leaves(value, (*prefix, key))
    else:
        yield ".".join(prefix)


def blocks(node, prefix=()):
    """Dotted path of every mapping below the root, nested ones included."""
    for key, value in node.items():
        if isinstance(value, dict):
            yield ".".join((*prefix, key))
            yield from blocks(value, (*prefix, key))


def walk(doc, dotted):
    holder = doc
    for key in dotted.split("."):
        holder = holder[key]
    return holder


def edited(dotted, value=REMOVE):
    """A fresh document with the value at `dotted` replaced, or removed when no value is given."""
    doc = valid_doc()
    parent, _, last = dotted.rpartition(".")
    holder = walk(doc, parent) if parent else doc
    if value is REMOVE:
        del holder[last]
    else:
        holder[last] = value
    return doc


def value_at(cfg, dotted):
    block, _, key = dotted.rpartition(".")
    return getattr(BLOCK_OF[block](cfg), RENAMED.get(dotted, key))


class ConfigCase(unittest.TestCase):
    def assertRefuses(self, doc, *named):
        """parse_modes_config(doc) raises ModesConfigError naming every given key path."""
        with self.assertRaises(ModesConfigError) as caught:
            parse_modes_config(doc)
        message = str(caught.exception)
        for text in named:
            self.assertIn(text, message)
        return message

    def assertAccepts(self, dotted, value):
        """The repository document with `value` at `dotted` parses; returns what was read."""
        return value_at(parse_modes_config(edited(dotted, value)), dotted)


class RepoConfigTests(ConfigCase):
    def test_repo_config_holds_the_document_values(self):
        c = load_modes_config(REPO_CONFIG)
        self.assertEqual((c.mode_a.tp_persen, c.mode_a.sl_persen, c.mode_a.batas_waktu_menit), (0.40, 0.25, 90))
        self.assertEqual((c.mode_a.tf_pemicu, c.mode_a.ema_pemicu, c.mode_a.konfirmasi_dalam_candle), ("15m", 20, 3))
        self.assertEqual((c.mode_d.tf_pemicu, c.mode_d.candle_jendela, c.mode_d.maks_jarak_ke_ema_kali_atr), ("3m", 39, 0.75))
        self.assertEqual((c.mode_d.tp_persen, c.mode_d.sl_persen, c.mode_d.batas_waktu_menit), (0.20, 0.20, 60))
        self.assertEqual((c.umum.fee_taker_persen, c.umum.slippage_persen, c.umum.notional_usdt), (0.035, 0.01, 25000))
        self.assertEqual(c.umum.jendela_entry_mingguan, "mon 07:00-sat 00:00")
        self.assertIsNone(c.pair["ETH-USDT"].faktor_k)
        self.assertEqual(c.uji.ujian_selesai, datetime(2026, 10, 14, tzinfo=timezone(timedelta(hours=7))))

    def test_repo_config_equals_the_document_block_for_block(self):
        expected = ModesConfig(
            umum=GeneralConfig(
                zona_waktu="Asia/Jakarta", fee_taker_persen=0.035, slippage_persen=0.01,
                maks_posisi_total=2, maks_posisi_per_pair=1, maks_entry_per_hari_total=10,
                maks_entry_per_hari_per_pair=6, jeda_menit=15,
                penjaga_volatilitas_kali_median_atr=2.0, ai_boleh_buka_tutup=False,
                notional_usdt=25000.0, jendela_entry_mingguan="mon 07:00-sat 00:00"),
            pemilih_mode=SelectorConfig(
                sma_cepat_1h=20, sma_lambat_1h=50, ema_4h=50, sideways_jarak_kali_atr=0.2),
            pair={
                "BTC-USDT": PairConfig(aktif=True, faktor_k=1.0),
                "ETH-USDT": PairConfig(aktif=False, faktor_k=None,
                                       harus_sama_kondisi_dengan="BTC-USDT"),
            },
            mode_a=PullbackConfig(
                aktif=True, tf_pemicu="15m", ema_pemicu=20, konfirmasi_dalam_candle=3,
                maks_jarak_sma20_1h_kali_atr=1.0, tp_persen=0.40, sl_persen=0.25,
                batas_waktu_menit=90),
            mode_b=BreakoutConfig(
                aktif=False, lookback_candle=16, volume_kali_rata_rata=1.5,
                periode_volume_rata_rata=20, maks_jarak_di_atas_sma20_1h_kali_atr=2.0,
                tp_persen=0.40, sl_persen=0.25, batas_waktu_menit=90),
            mode_c=SidewaysConfig(
                aktif=False, bollinger_periode=20, bollinger_std=2.0, min_jarak_tp_persen=0.25,
                sl_persen=0.25, batas_waktu_menit=60),
            mode_d=NearMeanConfig(
                aktif=True, tf_pemicu="3m", candle_jendela=39, ema_periode=21, atr_periode=14,
                maks_jarak_ke_ema_kali_atr=0.75, tp_persen=0.20, sl_persen=0.20,
                batas_waktu_menit=60),
            mode_d2=PullbackConfig(
                aktif=False, tf_pemicu="15m", ema_pemicu=20, konfirmasi_dalam_candle=3,
                maks_jarak_sma20_1h_kali_atr=1.0, tp_persen=0.40, sl_persen=0.25,
                batas_waktu_menit=90),
            uji=TestConfig(
                latihan_mulai=datetime(2025, 8, 1, tzinfo=WIB),
                latihan_selesai=datetime(2026, 8, 5, tzinfo=WIB),
                ujian_selesai=datetime(2026, 10, 14, tzinfo=WIB),
                lolos_hasil_per_1_juta_lebih_dari=-150.0, lolos_minimal_posisi=40,
                lolos_buang_posisi_teratas=3, lolos_hasil_tanpa_teratas_lebih_dari=-250.0,
                pindah_5m_jika_entry_per_hari_naik_kurang_dari=3.0,
                hari_naik_minimal_jam_naik=1),
        )
        self.assertEqual(load_modes_config(REPO_CONFIG), expected)

    def test_the_rule_tables_cover_every_leaf_of_the_repo_file(self):
        tabled = POSITIVE_FLOATS + SIGNED_FLOATS + FAKTOR_K + WHOLE_AT_LEAST_ONE
        tabled += WHOLE_AT_LEAST_ZERO + FLAGS + TEXTS + DATES
        self.assertEqual(sorted(tabled), sorted(leaves(valid_doc())))

    def test_dataclass_leaf_names_equal_the_yaml_keys(self):
        doc, cfg = valid_doc(), load_modes_config(REPO_CONFIG)
        for block, getter in BLOCK_OF.items():
            with self.subTest(block=block):
                from_yaml = {RENAMED.get(f"{block}.{key}", key) for key in walk(doc, block)}
                fields = {f.name for f in dataclasses.fields(getter(cfg))}
                self.assertLessEqual(from_yaml, fields)
                self.assertLessEqual(fields - from_yaml, {"harus_sama_kondisi_dengan"})

    def test_eth_follows_btc_and_btc_names_no_reference(self):
        cfg = load_modes_config(REPO_CONFIG)
        self.assertEqual(cfg.pair["ETH-USDT"].harus_sama_kondisi_dengan, "BTC-USDT")
        self.assertIsNone(cfg.pair["BTC-USDT"].harus_sama_kondisi_dengan)

    def test_the_distance_cap_is_read_from_each_modes_own_key(self):
        doc = valid_doc()
        doc["mode"]["A_pullback_long"]["maks_jarak_di_atas_sma20_1h_kali_atr"] = 0.5
        doc["mode"]["D2_pullback_short"]["maks_jarak_di_bawah_sma20_1h_kali_atr"] = 1.5
        doc["mode"]["B_breakout_long"]["maks_jarak_di_atas_sma20_1h_kali_atr"] = 2.5
        cfg = parse_modes_config(doc)
        self.assertEqual(
            (cfg.mode_a.maks_jarak_sma20_1h_kali_atr, cfg.mode_d2.maks_jarak_sma20_1h_kali_atr,
             cfg.mode_b.maks_jarak_di_atas_sma20_1h_kali_atr),
            (0.5, 1.5, 2.5))

    def test_the_order_of_keys_in_the_file_does_not_matter(self):
        def reversed_keys(node):
            if not isinstance(node, dict):
                return node
            return {key: reversed_keys(node[key]) for key in reversed(list(node))}

        shuffled = reversed_keys(valid_doc())
        self.assertNotEqual(list(shuffled), list(valid_doc()))
        self.assertEqual(parse_modes_config(shuffled), load_modes_config(REPO_CONFIG))

    def test_the_loader_does_not_modify_its_input_or_alias_it(self):
        doc = valid_doc()
        snapshot = copy.deepcopy(doc)
        cfg = parse_modes_config(doc)
        self.assertEqual(doc, snapshot)
        doc["pair"]["BTC-USDT"]["aktif"] = False
        doc["umum"]["notional_usdt"] = 1
        self.assertTrue(cfg.pair["BTC-USDT"].aktif)
        self.assertEqual(cfg.umum.notional_usdt, 25000)


class StructureRefusalTests(ConfigCase):
    def test_unknown_key_refuses_and_names_its_path(self):
        message = self.assertRefuses(edited("umum.extra", 1), "umum.extra")
        self.assertIn("unknown key", message)
        for block in blocks(valid_doc()):
            with self.subTest(block=block):
                doc = valid_doc()
                walk(doc, block)["surprise"] = 1
                self.assertRefuses(doc, f"{block}.surprise")

    def test_an_unknown_top_level_key_refuses(self):
        doc = valid_doc()
        doc["modul"] = {}
        self.assertRefuses(doc, "modul: unknown key")

    def test_an_unknown_key_that_is_not_text_is_named_too(self):
        doc = valid_doc()
        doc[True] = 1  # what YAML makes of a bare `yes:` or `on:` key
        doc["umum"][7] = 1
        self.assertRefuses(doc, "True: unknown key")
        del doc[True]
        self.assertRefuses(doc, "umum.7: unknown key")

    def test_missing_key_refuses(self):
        message = self.assertRefuses(edited("mode.A_pullback_long.sl_persen"), "mode.A_pullback_long.sl_persen")
        self.assertIn("missing key", message)
        for dotted in leaves(valid_doc()):
            if dotted == OPTIONAL:
                continue
            with self.subTest(key=dotted):
                self.assertRefuses(edited(dotted), dotted, "missing key")

    def test_a_missing_block_refuses(self):
        for top in ("umum", "pemilih_mode", "pair", "mode", "uji"):
            with self.subTest(block=top):
                self.assertRefuses(edited(top), f"{top}: missing key")

    def test_all_unknown_and_missing_keys_of_a_block_are_named_at_once(self):
        doc = valid_doc()
        del doc["umum"]["jeda_menit"]
        doc["umum"]["extra1"] = 1
        doc["umum"]["extra2"] = 2
        self.assertRefuses(doc, "umum.jeda_menit", "umum.extra1", "umum.extra2")

    def test_the_optional_reference_may_be_absent(self):
        cfg = parse_modes_config(edited(OPTIONAL))
        self.assertIsNone(cfg.pair["ETH-USDT"].harus_sama_kondisi_dengan)

    def test_a_block_must_be_a_mapping(self):
        for block in blocks(valid_doc()):
            for bad in (None, [], "text", 5):
                with self.subTest(block=block, bad=bad):
                    self.assertRefuses(edited(block, bad), block, "expected a block of settings")

    def test_the_document_must_be_a_mapping(self):
        for bad in (None, [], "umum", 5):
            with self.subTest(bad=bad):
                self.assertRefuses(bad, "top level", "expected a block of settings")

    def test_the_pair_names_are_fixed(self):
        doc = valid_doc()
        doc["pair"]["SOL-USDT"] = {"aktif": False, "faktor_k": 1.0}
        self.assertRefuses(doc, "pair.SOL-USDT", "unknown key")
        self.assertRefuses(edited("pair.ETH-USDT"), "pair.ETH-USDT", "missing key")

    def test_a_mode_refuses_the_distance_key_of_the_other_direction(self):
        for mode, own, other in (
            ("A_pullback_long", "maks_jarak_di_atas_sma20_1h_kali_atr", "maks_jarak_di_bawah_sma20_1h_kali_atr"),
            ("D2_pullback_short", "maks_jarak_di_bawah_sma20_1h_kali_atr", "maks_jarak_di_atas_sma20_1h_kali_atr"),
        ):
            with self.subTest(mode=mode):
                doc = valid_doc()
                block = doc["mode"][mode]
                block[other] = block.pop(own)
                self.assertRefuses(doc, f"mode.{mode}.{other}: unknown key", f"mode.{mode}.{own}: missing key")


class ValueRefusalTests(ConfigCase):
    def test_ai_may_not_open_or_close(self):
        message = self.assertRefuses(edited("umum.ai_boleh_buka_tutup", True), "umum.ai_boleh_buka_tutup")
        self.assertIn("must be false", message)
        self.assertFalse(load_modes_config(REPO_CONFIG).umum.ai_boleh_buka_tutup)

    def test_bool_is_not_a_number(self):
        self.assertRefuses(edited("mode.A_pullback_long.tp_persen", True), "mode.A_pullback_long.tp_persen")
        for dotted in NUMERIC:
            for flag in (True, False):
                with self.subTest(key=dotted, value=flag):
                    self.assertRefuses(edited(dotted, flag), dotted, "bool")

    def test_wrong_types_refuse_and_name_their_path(self):
        for dotted in leaves(valid_doc()):
            for bad in ([1], {"a": 1}, None):
                with self.subTest(key=dotted, bad=bad):
                    self.assertRefuses(edited(dotted, bad), dotted)

    def test_flags_must_be_real_booleans(self):
        for dotted in FLAGS:
            for bad in ("true", "yes", 1, 0, 1.0):
                with self.subTest(key=dotted, bad=bad):
                    self.assertRefuses(edited(dotted, bad), dotted, "true or false")
        for dotted in FLAGS[1:]:
            for flag in (True, False):
                with self.subTest(key=dotted, value=flag):
                    self.assertIs(self.assertAccepts(dotted, flag), flag)

    def test_whole_number_fields_refuse_floats_and_text(self):
        for dotted in WHOLE_AT_LEAST_ONE + WHOLE_AT_LEAST_ZERO:
            for bad in (20.0, 1.5, "20", float("nan")):
                with self.subTest(key=dotted, bad=bad):
                    self.assertRefuses(edited(dotted, bad), dotted, "whole number")

    def test_int_is_accepted_for_float_fields_and_returned_as_float(self):
        for dotted in POSITIVE_FLOATS + SIGNED_FLOATS:
            with self.subTest(key=dotted):
                value = self.assertAccepts(dotted, 3)
                self.assertEqual(value, 3)
                self.assertIsInstance(value, float)
        for dotted in FAKTOR_K:
            with self.subTest(key=dotted):
                value = self.assertAccepts(dotted, 2)
                self.assertEqual(value, 2)
                self.assertIsInstance(value, float)

    def test_an_int_too_long_to_print_is_named_not_leaked(self):
        # CPython refuses to print an int of more than 4300 digits (ValueError), and the message
        # that names the bad value or key must not be the thing that raises.
        huge = 10 ** 5000
        for dotted in ("mode.A_pullback_long.aktif", "umum.zona_waktu", "umum.jendela_entry_mingguan"):
            with self.subTest(key=dotted):
                self.assertRefuses(edited(dotted, huge), dotted)
        self.assertRefuses(edited("umum.jeda_menit", -huge), "umum.jeda_menit", "at least 0")
        doc = valid_doc()
        doc["umum"][huge] = 1
        self.assertRefuses(doc, "umum.", "unknown key")

    def test_non_finite_and_huge_numbers_refuse(self):
        for dotted in POSITIVE_FLOATS + SIGNED_FLOATS + FAKTOR_K:
            for bad in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(key=dotted, bad=bad):
                    self.assertRefuses(edited(dotted, bad), dotted)
        message = self.assertRefuses(edited("umum.fee_taker_persen", 10 ** 400), "umum.fee_taker_persen")
        self.assertIn("too large", message)

    def test_percents_and_multipliers_must_be_positive(self):
        for dotted in POSITIVE_FLOATS:
            for bad in (0, 0.0, -1, -0.001):
                with self.subTest(key=dotted, bad=bad):
                    self.assertRefuses(edited(dotted, bad), dotted, "greater than 0")
            with self.subTest(key=dotted, good="small"):
                self.assertEqual(self.assertAccepts(dotted, 0.001), 0.001)

    def test_signed_thresholds_may_be_negative_or_zero(self):
        for dotted in SIGNED_FLOATS:
            for good in (-300.5, 0, 12):
                with self.subTest(key=dotted, good=good):
                    self.assertEqual(self.assertAccepts(dotted, good), good)

    def test_counts_and_periods_have_minimums(self):
        for dotted in WHOLE_AT_LEAST_ONE:
            for bad in (0, -1):
                with self.subTest(key=dotted, bad=bad):
                    self.assertRefuses(edited(dotted, bad), dotted, "at least 1")
            with self.subTest(key=dotted, good=1):
                self.assertEqual(self.assertAccepts(dotted, 1), 1)
        for dotted in WHOLE_AT_LEAST_ZERO:
            with self.subTest(key=dotted, bad=-1):
                self.assertRefuses(edited(dotted, -1), dotted, "at least 0")
            with self.subTest(key=dotted, good=0):
                self.assertEqual(self.assertAccepts(dotted, 0), 0)

    def test_trigger_timeframes_are_limited_per_mode(self):
        for dotted in ("mode.A_pullback_long.tf_pemicu", "mode.D2_pullback_short.tf_pemicu"):
            for good in ("15m", "5m"):
                with self.subTest(key=dotted, good=good):
                    self.assertEqual(self.assertAccepts(dotted, good), good)
            for bad in ("3m", "1m", "1h", "15M", 15, None):
                with self.subTest(key=dotted, bad=bad):
                    self.assertRefuses(edited(dotted, bad), dotted, "'15m' or '5m'")
        dotted = "mode.D_short_sekarang.tf_pemicu"
        self.assertEqual(self.assertAccepts(dotted, "3m"), "3m")
        for bad in ("15m", "5m", "1m", 3):
            with self.subTest(key=dotted, bad=bad):
                self.assertRefuses(edited(dotted, bad), dotted, "'3m'")

    def test_faktor_k_is_otomatis_or_at_least_one(self):
        for dotted in FAKTOR_K:
            self.assertIsNone(self.assertAccepts(dotted, "otomatis"))
            for good in (1, 1.0, 1.5, 2.0, 3.5):
                with self.subTest(key=dotted, good=good):
                    self.assertEqual(self.assertAccepts(dotted, good), good)
            for bad in (0.99, 0, -1.0, "auto", "Otomatis", "", None):
                with self.subTest(key=dotted, bad=bad):
                    self.assertRefuses(edited(dotted, bad), dotted)
        self.assertRefuses(edited("pair.BTC-USDT.faktor_k", 0.5), "pair.BTC-USDT.faktor_k", "at least 1.0")
        self.assertRefuses(edited("pair.BTC-USDT.faktor_k", "auto"), "pair.BTC-USDT.faktor_k", "'otomatis'")

    def test_harus_sama_kondisi_dengan_must_name_a_listed_pair(self):
        dotted = OPTIONAL
        self.assertEqual(self.assertAccepts(dotted, "BTC-USDT"), "BTC-USDT")
        for bad in ("XRP-USDT", "btc-usdt", "BTC/USDT", "", 5, None):
            with self.subTest(bad=bad):
                self.assertRefuses(edited(dotted, bad), dotted, "'BTC-USDT' or 'ETH-USDT'")

    def test_zona_waktu_must_be_jakarta(self):
        self.assertEqual(self.assertAccepts("umum.zona_waktu", "Asia/Jakarta"), "Asia/Jakarta")
        for bad in ("UTC", "Asia/Bangkok", "asia/jakarta", "WIB", "", 7, None):
            with self.subTest(bad=bad):
                self.assertRefuses(edited("umum.zona_waktu", bad), "umum.zona_waktu", "'Asia/Jakarta'")

    def test_bad_weekly_window_or_naive_date_refuses(self):
        window = "umum.jendela_entry_mingguan"
        for bad in ("mon 07:00", "sat 00:00-mon 07:00", "mon 07:00-mon 07:00", "mon 25:00-sat 00:00",
                    "", "   ", 5, None):
            with self.subTest(window=bad):
                self.assertRefuses(edited(window, bad), window)
        self.assertRefuses(edited(window, ""), window, "must not be empty")
        self.assertRefuses(edited(window, 5), window, "expected text such as 'mon 07:00-sat 00:00'")

        class LooksLikeAWindow:  # str() is a valid window, but the value is not a string
            def __str__(self):
                return "mon 07:00-sat 00:00"

        self.assertRefuses(edited(window, LooksLikeAWindow()), window, "expected text")
        self.assertEqual(self.assertAccepts(window, "tue 08:30-fri 24:00"), "tue 08:30-fri 24:00")
        for dotted in DATES:
            with self.subTest(date=dotted):
                self.assertRefuses(edited(dotted, "2025-08-01T00:00:00"), dotted, "UTC offset")

    def test_dates_need_the_exact_shape_and_a_real_calendar_moment(self):
        for dotted in DATES:
            for bad in ("2025-08-01", "2025-08-01 00:00:00+07:00", "2025-08-01T00:00:00Z",
                        "2025-08-01T00:00:00+0700", "2025-08-01T00:00:00.5+07:00", "garbage",
                        "20250801T00:00:00+07:00", 20250801, None,
                        datetime(2025, 8, 1, tzinfo=WIB)):
                with self.subTest(key=dotted, bad=bad):
                    self.assertRefuses(edited(dotted, bad), dotted, "quoted string")
            for bad in ("2025-13-01T00:00:00+07:00", "2025-08-32T00:00:00+07:00",
                        "2025-08-01T24:00:00+07:00", "2025-08-01T00:00:00+25:00",
                        # valid on their own, but year 0 / year 10000 once seen in UTC
                        "0001-01-01T00:00:00+07:00", "9999-12-31T23:59:59-23:59"):
                with self.subTest(key=dotted, bad=bad):
                    self.assertRefuses(edited(dotted, bad), dotted, "not a valid date")

    def test_dates_must_be_wib_midnights(self):
        dotted = "uji.latihan_mulai"
        for bad in ("2025-08-01T07:00:00+07:00", "2025-08-01T00:00:00+00:00",
                    "2025-08-01T00:00:01+07:00", "2025-08-01T00:30:00+07:00"):
            with self.subTest(bad=bad):
                self.assertRefuses(edited(dotted, bad), dotted, "midnight in Asia/Jakarta")

    def test_dates_come_back_in_wib_whatever_offset_the_file_used(self):
        started = self.assertAccepts("uji.latihan_mulai", "2025-07-31T17:00:00+00:00")
        self.assertEqual(started, datetime(2025, 8, 1, tzinfo=WIB))
        self.assertEqual(started.utcoffset(), timedelta(hours=7))
        self.assertEqual((started.year, started.month, started.day, started.hour), (2025, 8, 1, 0))

    def test_training_must_precede_the_test_period(self):
        early, middle, late = ("2025-08-01T00:00:00+07:00", "2026-08-05T00:00:00+07:00",
                               "2026-10-14T00:00:00+07:00")
        cases = {
            "training ends where it starts": (early, early, late),
            "training ends before it starts": (middle, early, late),
            "the test ends where training ends": (early, middle, middle),
            "the test ends before training ends": (early, middle, "2026-01-01T00:00:00+07:00"),
        }
        for name, (start, end, exam_end) in cases.items():
            with self.subTest(name):
                doc = valid_doc()
                doc["uji"].update(latihan_mulai=start, latihan_selesai=end, ujian_selesai=exam_end)
                message = self.assertRefuses(doc, "uji")
                self.assertIn("latihan_mulai < latihan_selesai < ujian_selesai", message)


class YamlFileTests(ConfigCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

    def written(self, text: str) -> Path:
        path = self.dir / "modes.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def assertFileRefuses(self, text, *named):
        path = self.written(text)
        with self.assertRaises(ModesConfigError) as caught:
            load_modes_config(path)
        message = str(caught.exception)
        for fragment in (str(path), *named):
            self.assertIn(fragment, message)
        return message

    def test_loading_a_file_matches_parsing_its_mapping(self):
        self.assertEqual(load_modes_config(REPO_CONFIG), parse_modes_config(valid_doc()))
        self.assertEqual(load_modes_config(str(REPO_CONFIG)), load_modes_config(REPO_CONFIG))

    def test_content_errors_name_the_file_and_the_key(self):
        text = REPO_CONFIG.read_text(encoding="utf-8").replace("  jeda_menit: 15\n", "  jeda_menit: 15\n  extra: 1\n")
        self.assertFileRefuses(text, "umum.extra", "unknown key")

    def test_yaml_syntax_errors_become_config_errors(self):
        self.assertFileRefuses("umum: [unclosed\n", "cannot read as YAML")

    def test_an_unquoted_date_refuses_and_says_to_quote_it(self):
        text = REPO_CONFIG.read_text(encoding="utf-8")
        text = text.replace('"2025-08-01T00:00:00+07:00"', "2025-08-01T00:00:00+07:00")
        self.assertFileRefuses(text, "uji.latihan_mulai", "quoted string")

    def test_an_unquoted_date_no_calendar_has_is_a_config_error_not_a_value_error(self):
        # PyYAML builds a datetime from an unquoted date; its own ValueError used to escape.
        text = REPO_CONFIG.read_text(encoding="utf-8")
        started = '"2025-08-01T00:00:00+07:00"'
        cases = {
            "month 13 in a date field": text.replace(started, "2025-13-01T00:00:00+07:00"),
            "day 32 in a date field": text.replace(started, "2025-08-32"),
            "30 February in a number field": text.replace("  jeda_menit: 15\n", "  jeda_menit: 2025-02-30\n"),
        }
        for name, case in cases.items():
            with self.subTest(name):
                self.assertFileRefuses(case, "cannot read as YAML")

    def test_a_quoted_date_outside_the_supported_range_names_its_key(self):
        # astimezone() overflows for year 1 and for the last day of year 9999 behind a -23:59 offset
        text = REPO_CONFIG.read_text(encoding="utf-8")
        for bad in ("0001-01-01T00:00:00+07:00", "9999-12-31T23:59:59-23:59"):
            with self.subTest(bad=bad):
                case = text.replace('"2025-08-01T00:00:00+07:00"', f'"{bad}"')
                self.assertFileRefuses(case, "uji.latihan_mulai", "not a valid date")

    def test_whatever_pyyaml_cannot_build_is_a_config_error(self):
        # Each text is valid YAML, but PyYAML's constructor then raises something that is not a
        # YAMLError: ValueError, KeyError or AttributeError.
        text = REPO_CONFIG.read_text(encoding="utf-8")
        started, number, flag, fee = (
            '"2025-08-01T00:00:00+07:00"', "  jeda_menit: 15\n",
            "  ai_boleh_buka_tutup: false\n", "  fee_taker_persen: 0.035\n")
        cases = {
            "an hour no clock has": (started, "2025-08-01T25:00:00+07:00"),
            "a UTC offset of more than a day": (started, "2025-08-01T00:00:00+25:00"),
            "year zero": (started, "0000-01-01"),
            "an explicit !!timestamp that is no timestamp": (started, "!!timestamp abc"),
            "an explicit !!int that is no integer": (number, "  jeda_menit: !!int abc\n"),
            "an explicit !!float that is no float": (fee, "  fee_taker_persen: !!float abc\n"),
            "an explicit !!bool that is no boolean": (flag, "  ai_boleh_buka_tutup: !!bool maybe\n"),
            # Python refuses to read an integer this long (CPython's digit limit) or, without
            # that limit, the float conversion overflows: either way a config error.
            "an integer of 5000 digits": (fee, "  fee_taker_persen: " + "9" * 5000 + "\n"),
        }
        for name, (old, new) in cases.items():
            with self.subTest(name):
                self.assertFileRefuses(text.replace(old, new))
        for name in ("an hour no clock has", "year zero", "an explicit !!bool that is no boolean"):
            old, new = cases[name]
            with self.subTest(name, says="cannot read as YAML"):
                self.assertFileRefuses(text.replace(old, new), "cannot read as YAML")

    def test_a_hex_number_too_long_for_python_to_print_is_a_config_error(self):
        # PyYAML reads a hex literal of any length (the digit limit covers decimal only), then
        # CPython cannot print the value for the message.
        text = REPO_CONFIG.read_text(encoding="utf-8")
        long_hex = "0x" + "f" * 4000
        cases = {
            "in a flag": ("    aktif: true\n    tf_pemicu: 15m", f"    aktif: {long_hex}\n    tf_pemicu: 15m",
                          "mode.A_pullback_long.aktif"),
            "in a whole-number field, negative": ("  jeda_menit: 15\n", f"  jeda_menit: -{long_hex}\n",
                                                  "umum.jeda_menit"),
            "in a text field": ("  zona_waktu: Asia/Jakarta\n", f"  zona_waktu: {long_hex}\n",
                                "umum.zona_waktu"),
            "as an explicit key": ("  jeda_menit: 15\n", f"  jeda_menit: 15\n  ? {long_hex}\n  : 1\n", "umum."),
        }
        for name, (old, new, named) in cases.items():
            with self.subTest(name):
                self.assertFileRefuses(text.replace(old, new), named)

    def test_absurdly_deep_nesting_is_a_config_error(self):
        text = REPO_CONFIG.read_text(encoding="utf-8")
        deep = "[" * 3000 + "]" * 3000
        self.assertFileRefuses(text.replace("  jeda_menit: 15\n", f"  jeda_menit: {deep}\n"))

    def test_a_key_written_twice_refuses_and_names_its_path(self):
        base = REPO_CONFIG.read_text(encoding="utf-8")
        slippage, mode_b = "  slippage_persen: 0.01\n", "  B_breakout_long:\n"
        cases = {
            "umum.slippage_persen": base.replace(slippage, slippage + "  slippage_persen: 0.02\n"),
            "mode.A_pullback_long": base.replace(mode_b, "  A_pullback_long:\n    aktif: false\n" + mode_b),
            "umum": base + "umum:\n  zona_waktu: Asia/Jakarta\n",
        }
        for path, text in cases.items():
            with self.subTest(path=path):
                message = self.assertFileRefuses(text, f"{path}: key written twice")
                self.assertNotIn("cannot read as YAML", message)

    def test_a_key_is_a_duplicate_even_when_one_spelling_is_quoted(self):
        text = REPO_CONFIG.read_text(encoding="utf-8").replace(
            "  jeda_menit: 15\n", '  jeda_menit: 15\n  "jeda_menit": 20\n')
        self.assertFileRefuses(text, "umum.jeda_menit: key written twice")

    def test_a_self_referencing_alias_is_refused_without_looping(self):
        text = REPO_CONFIG.read_text(encoding="utf-8").replace(
            "  jeda_menit: 15\n", "  jeda_menit: 15\n  loop: &l\n    again: *l\n")
        self.assertFileRefuses(text, "umum.loop", "unknown key")

    def test_an_alias_to_a_scalar_is_not_mistaken_for_a_duplicate(self):
        text = REPO_CONFIG.read_text(encoding="utf-8")
        text = text.replace("  maks_posisi_per_pair: 1\n", "  maks_posisi_per_pair: &one 1\n").replace(
            "  jeda_menit: 15\n", "  jeda_menit: 15\n  extra: *one\n")
        message = self.assertFileRefuses(text, "umum.extra", "unknown key")
        self.assertNotIn("written twice", message)

    def test_only_one_document_is_allowed(self):
        text = REPO_CONFIG.read_text(encoding="utf-8")
        self.assertFileRefuses(text + "---\n" + text, "cannot read as YAML")

    def test_an_empty_file_refuses(self):
        self.assertFileRefuses("", "top level", "expected a block of settings")
        self.assertFileRefuses("# only a comment\n", "top level")

    def test_a_top_level_list_refuses(self):
        self.assertFileRefuses("- umum\n- uji\n", "top level")

    def test_bytes_that_are_not_utf8_refuse(self):
        path = self.dir / "bad.yaml"
        path.write_bytes(b"umum: \xff\xfe\n")
        with self.assertRaises(ModesConfigError) as caught:
            load_modes_config(path)
        self.assertIn(str(path), str(caught.exception))

    def test_a_missing_file_is_an_os_error_not_a_config_error(self):
        with self.assertRaises(FileNotFoundError):
            load_modes_config(self.dir / "absent.yaml")


class Sha256Tests(unittest.TestCase):
    def test_config_sha256_is_the_sha256_of_the_files_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.yaml"
            path.write_bytes(b"abc")
            self.assertEqual(config_sha256(path),
                             "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
            self.assertEqual(config_sha256(str(path)), config_sha256(path))
        self.assertEqual(config_sha256(REPO_CONFIG), hashlib.sha256(REPO_CONFIG.read_bytes()).hexdigest())

    def test_any_changed_byte_changes_the_hash(self):
        original = REPO_CONFIG.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.yaml"
            for name, changed in (
                ("a number", original.replace(b"0.035", b"0.036", 1)),
                ("a comment", original.replace(b"# ditambahkan", b"# Ditambahkan", 1)),
                ("trailing space", original + b" "),
            ):
                with self.subTest(name):
                    self.assertNotEqual(changed, original)
                    path.write_bytes(changed)
                    self.assertNotEqual(config_sha256(path), config_sha256(REPO_CONFIG))


class ImmutabilityTests(unittest.TestCase):
    def test_every_block_is_frozen(self):
        cfg = load_modes_config(REPO_CONFIG)
        with self.assertRaises(FrozenInstanceError):
            cfg.umum.notional_usdt = 1.0
        with self.assertRaises(FrozenInstanceError):
            cfg.mode_a = cfg.mode_d2
        with self.assertRaises(FrozenInstanceError):
            cfg.pair["BTC-USDT"].faktor_k = 2.0
        with self.assertRaises(FrozenInstanceError):
            cfg.uji.lolos_minimal_posisi = 1

    def test_the_pair_mapping_is_read_only(self):
        cfg = load_modes_config(REPO_CONFIG)
        with self.assertRaises(TypeError):
            cfg.pair["SOL-USDT"] = cfg.pair["BTC-USDT"]
        with self.assertRaises(TypeError):
            del cfg.pair["BTC-USDT"]
        self.assertEqual(sorted(cfg.pair), ["BTC-USDT", "ETH-USDT"])


if __name__ == "__main__":
    unittest.main()
