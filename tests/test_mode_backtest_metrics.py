"""Tests for research.mode_backtest.metrics: the numbers of a stage, the pass rule of the document
(spec section 2.8) and the 5m switch.

Every expected number is worked out here by hand from the definitions of spec section 5 and never
computed with the code under test. The trades are built directly (``tests.mode_backtest_trades``):
the metrics read only the exit reason, the profit, the volume and the two times of a trade, so a
test sets those and nothing else. The boundaries of the pass rule (strictly better than -150 and
than -250, at least 40 positions, the 3 best removed) are hit with data whose sums are exact in
binary floating point, so "exactly -150" really is -150.0.
"""

import dataclasses
import itertools
import json
import math
import random
import unittest
from fractions import Fraction
from pathlib import Path

from bot.strategy.modes.config import load_modes_config
from research.mode_backtest.metrics import (
    BELUM_CUKUP_SAMPEL,
    GAGAL,
    LOLOS,
    StageMetrics,
    pass_status,
    stage_metrics,
    wants_5m,
)
from research.mode_backtest.simulate import EXIT_POTONG, EXIT_SL, EXIT_TP, EXIT_WAKTU
from tests.mode_backtest_helpers import MINUTE_MS
from tests.mode_backtest_trades import FIRST_ENTRY_MS, make_trade

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config" / "modes.yaml"
UJI = load_modes_config(REPO_CONFIG).uji

DROP_TOP = 3
VOLUME = 50_000.0  # a position of 25,000 USDT, both sides counted


def trade(pnl, volume=VOLUME, reason=EXIT_TP, minute=0, hold_min=30):
    """A trade that entered ``minute`` minutes after the first entry and held ``hold_min``."""
    return make_trade(
        entry_ms=FIRST_ENTRY_MS + minute * MINUTE_MS,
        hold_min=hold_min,
        pnl_usdt=pnl,
        volume_usdt=volume,
        exit_reason=reason,
    )


def book(*pnls, reason=EXIT_TP, volume=VOLUME):
    """One trade per profit, entering a minute apart in the order given."""
    return [trade(pnl, volume, reason, minute) for minute, pnl in enumerate(pnls)]


def metrics(posisi=40, hasil=-100.0, tanpa=-200.0, entry=2.0):
    """A metrics object written out by hand, for the pass rule and the 5m switch."""
    return StageMetrics(
        posisi=posisi,
        entry_per_hari_aktif=entry,
        menang_pct=50.0,
        hasil_per_1_juta=hasil,
        tp=posisi // 2,
        sl=posisi - posisi // 2,
        waktu=0,
        rata_lama_menit=30.0,
        hasil_tanpa_teratas=tanpa,
        hari_naik=10,
    )


def forty(losers, winners=(50.0, 50.0, 62.5)):
    """40 trades of 50,000 volume: the three ``winners`` and 37 losers with the profit ``losers``.

    The default winners add up to 162.5 and 37 losers of -12.5 to -462.5, so the whole book earns
    -300 on 2,000,000 of volume (exactly -150 per 1M) and without the winners -462.5 on 1,850,000
    (exactly -250 per 1M)."""
    pnls = list(winners) + [losers] * 37
    return book(*pnls)


def trades_left_after_dropping_the_best(trades, count):
    """The trades that stay once ``count`` are dropped, by brute force.

    The dropped set is the only set of ``count`` trades in which every dropped trade is more
    profitable than every kept one, or equally profitable and entered earlier; the search tries
    every set and insists that exactly one qualifies."""
    ahead = [
        chosen
        for chosen in itertools.combinations(range(len(trades)), count)
        if all(
            (trades[d].pnl_usdt, -trades[d].entry_ms) > (trades[k].pnl_usdt, -trades[k].entry_ms)
            for d in chosen
            for k in range(len(trades))
            if k not in chosen
        )
    ]
    assert len(ahead) == 1, f"{len(ahead)} sets of {count} could be the best"
    return [t for i, t in enumerate(trades) if i not in ahead[0]]


class FixtureTests(unittest.TestCase):
    def test_the_config_has_the_numbers_of_the_document(self):
        self.assertEqual(UJI.lolos_minimal_posisi, 40)
        self.assertEqual(UJI.lolos_hasil_per_1_juta_lebih_dari, -150.0)
        self.assertEqual(UJI.lolos_hasil_tanpa_teratas_lebih_dari, -250.0)
        self.assertEqual(UJI.lolos_buang_posisi_teratas, DROP_TOP)
        self.assertEqual(UJI.pindah_5m_jika_entry_per_hari_naik_kurang_dari, 3.0)

    def test_the_status_words_are_the_documented_ones(self):
        self.assertEqual(
            (LOLOS, GAGAL, BELUM_CUKUP_SAMPEL), ("LOLOS", "GAGAL", "BELUM CUKUP SAMPEL")
        )

    def test_the_fields_are_those_of_the_interface_in_order(self):
        names = [field.name for field in dataclasses.fields(StageMetrics)]

        self.assertEqual(
            names,
            [
                "posisi",
                "entry_per_hari_aktif",
                "menang_pct",
                "hasil_per_1_juta",
                "tp",
                "sl",
                "waktu",
                "rata_lama_menit",
                "hasil_tanpa_teratas",
                "hari_naik",
                # added for mode L, after Phase 1's fields and with defaults
                "potong",
                "entry_per_hari_kalender",
            ],
        )

    def test_the_metrics_are_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            metrics().posisi = 1

    def test_the_metrics_survive_a_round_trip_through_json(self):
        # the stages write them to the results file
        for m in (
            stage_metrics(book(10.0, -40.0, 5.0), hari_naik=2, drop_top=1),
            stage_metrics([], hari_naik=0, drop_top=DROP_TOP),
        ):
            with self.subTest(posisi=m.posisi):
                as_dict = dataclasses.asdict(m)

                self.assertEqual(json.loads(json.dumps(as_dict, allow_nan=False)), as_dict)


class HasilPer1JutaTests(unittest.TestCase):
    def test_hasil_per_1_juta(self):
        # pnl 10 and -40 on 50,000 of volume each: -30 USDT on 100,000 is -300 per 1M
        m = stage_metrics(book(10.0, -40.0), hari_naik=1, drop_top=DROP_TOP)

        self.assertEqual(m.hasil_per_1_juta, -300.0)

    def test_a_profit_is_positive(self):
        m = stage_metrics(book(25.0), hari_naik=1, drop_top=DROP_TOP)

        self.assertEqual(m.hasil_per_1_juta, 500.0)

    def test_hasil_is_pooled_over_volume_and_not_the_mean_of_the_ratios(self):
        # +200 per 1M on 50,000 and -200 per 1M on 150,000: pooled (10 - 30) / 200,000 is -100
        # per 1M, where the mean of the two ratios would be 0
        trades = [trade(10.0, volume=50_000.0), trade(-30.0, volume=150_000.0, minute=1)]

        m = stage_metrics(trades, hari_naik=1, drop_top=DROP_TOP)

        self.assertEqual(m.hasil_per_1_juta, -100.0)

    def test_hasil_is_the_exact_ratio_of_the_sums(self):
        rng = random.Random(7)
        trades = [
            trade(rng.uniform(-60, 60), rng.uniform(40_000, 60_000), minute=i) for i in range(57)
        ]
        pnl = sum(Fraction(t.pnl_usdt) for t in trades)
        volume = sum(Fraction(t.volume_usdt) for t in trades)

        m = stage_metrics(trades, hari_naik=9, drop_top=DROP_TOP)

        self.assertAlmostEqual(m.hasil_per_1_juta, float(1_000_000 * pnl / volume), places=9)

    def test_hasil_does_not_depend_on_the_order_of_the_trades(self):
        # profits and volumes that are not round numbers, so a plain float sum would change in its
        # last bits with the order; the pass rule is strict, so the same trades must always give
        # the same number
        rng = random.Random(11)
        trades = [
            trade(rng.uniform(-60, 60), rng.uniform(40_000, 60_000), minute=i) for i in range(60)
        ]
        first = stage_metrics(trades, hari_naik=5, drop_top=DROP_TOP)

        for round_ in range(20):
            shuffled = trades[:]
            rng.shuffle(shuffled)
            with self.subTest(round=round_):
                self.assertEqual(stage_metrics(shuffled, hari_naik=5, drop_top=DROP_TOP), first)


class MenangTests(unittest.TestCase):
    def test_menang_excludes_time_exits(self):
        # 3 TP, 1 SL and 6 time exits: 3 / (3 + 1) is 75, not 3 / 10 and not 9 / 10
        trades = (
            book(5.0, 5.0, 5.0, reason=EXIT_TP)
            + [trade(-5.0, reason=EXIT_SL, minute=10)]
            + [trade(0.5, reason=EXIT_WAKTU, minute=11 + i) for i in range(6)]
        )

        m = stage_metrics(trades, hari_naik=2, drop_top=DROP_TOP)

        self.assertEqual(m.menang_pct, 75.0)
        self.assertEqual(m.posisi, 10)

    def test_menang_is_none_without_a_tp_or_an_sl(self):
        for trades in ([], [trade(1.0, reason=EXIT_WAKTU)] * 3):
            with self.subTest(n=len(trades)):
                m = stage_metrics(trades, hari_naik=2, drop_top=DROP_TOP)

                self.assertIsNone(m.menang_pct)

    def test_menang_of_stop_losses_only_is_zero_and_not_missing(self):
        m = stage_metrics(book(-5.0, -5.0, reason=EXIT_SL), hari_naik=1, drop_top=DROP_TOP)

        self.assertEqual(m.menang_pct, 0.0)

    def test_menang_of_take_profits_only_is_100(self):
        m = stage_metrics(book(5.0, 5.0, reason=EXIT_TP), hari_naik=1, drop_top=DROP_TOP)

        self.assertEqual(m.menang_pct, 100.0)

    def test_menang_is_a_percentage(self):
        trades = [
            trade(5.0),
            trade(-5.0, reason=EXIT_SL, minute=1),
            trade(-5.0, reason=EXIT_SL, minute=2),
        ]

        m = stage_metrics(trades, hari_naik=1, drop_top=DROP_TOP)

        self.assertAlmostEqual(m.menang_pct, 100 / 3)

    def test_menang_follows_the_exit_reason_and_not_the_sign_of_the_profit(self):
        # a stop-loss that earned (a gap in favour) is still a loss, a take-profit that lost is
        # still a win: the report counts exits
        trades = [trade(-1.0, reason=EXIT_TP), trade(1.0, reason=EXIT_SL, minute=1)]

        m = stage_metrics(trades, hari_naik=1, drop_top=DROP_TOP)

        self.assertEqual(m.menang_pct, 50.0)
        self.assertEqual((m.tp, m.sl), (1, 1))


class ExitCountTests(unittest.TestCase):
    def test_the_exits_are_counted_by_reason(self):
        trades = (
            book(5.0, 5.0, reason=EXIT_TP)
            + [trade(-5.0, reason=EXIT_SL, minute=2 + i) for i in range(3)]
            + [trade(0.0, reason=EXIT_WAKTU, minute=5 + i) for i in range(4)]
        )

        m = stage_metrics(trades, hari_naik=3, drop_top=DROP_TOP)

        self.assertEqual((m.tp, m.sl, m.waktu), (2, 3, 4))
        self.assertEqual(m.posisi, 9)
        self.assertEqual(m.tp + m.sl + m.waktu, m.posisi)

    def test_an_unknown_exit_reason_is_refused_and_not_left_uncounted(self):
        for reason in ("TIME", "tp", "", None):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, "exit_reason"):
                    stage_metrics([trade(1.0, reason=reason)], hari_naik=1, drop_top=DROP_TOP)


class EntryPerActiveDayTests(unittest.TestCase):
    def test_entry_per_hari_aktif_is_positions_over_naik_days(self):
        m = stage_metrics(book(*[1.0] * 6), hari_naik=4, drop_top=DROP_TOP)

        self.assertEqual(m.entry_per_hari_aktif, 1.5)
        self.assertEqual(m.hari_naik, 4)

    def test_zero_naik_days_is_none_and_never_a_division_error(self):
        for n in (5, 0):
            with self.subTest(positions=n):
                m = stage_metrics(book(*[1.0] * n), hari_naik=0, drop_top=DROP_TOP)

                self.assertIsNone(m.entry_per_hari_aktif)
                self.assertEqual(m.hari_naik, 0)

    def test_no_positions_over_naik_days_is_zero_entries(self):
        m = stage_metrics([], hari_naik=7, drop_top=DROP_TOP)

        self.assertEqual(m.entry_per_hari_aktif, 0.0)

    def test_more_than_one_entry_a_day_is_not_capped(self):
        m = stage_metrics(book(*[1.0] * 9), hari_naik=2, drop_top=DROP_TOP)

        self.assertEqual(m.entry_per_hari_aktif, 4.5)


class HoldTests(unittest.TestCase):
    def test_rata_lama_posisi_is_the_mean_hold_in_minutes(self):
        trades = [trade(1.0, hold_min=30), trade(1.0, minute=40, hold_min=90)]

        m = stage_metrics(trades, hari_naik=1, drop_top=DROP_TOP)

        self.assertEqual(m.rata_lama_menit, 60.0)

    def test_seconds_count_as_a_fraction_of_a_minute(self):
        t = make_trade(entry_ms=FIRST_ENTRY_MS, exit_ms=FIRST_ENTRY_MS + 90_000)

        m = stage_metrics([t], hari_naik=1, drop_top=DROP_TOP)

        self.assertEqual(m.rata_lama_menit, 1.5)

    def test_the_hold_starts_at_the_entry_and_not_at_the_signal(self):
        t = make_trade(
            signal_ms=FIRST_ENTRY_MS - 10 * MINUTE_MS,
            entry_ms=FIRST_ENTRY_MS,
            exit_ms=FIRST_ENTRY_MS + 25 * MINUTE_MS,
        )

        m = stage_metrics([t], hari_naik=1, drop_top=DROP_TOP)

        self.assertEqual(m.rata_lama_menit, 25.0)

    def test_the_hold_is_the_same_for_every_exit_reason(self):
        trades = [
            trade(5.0, reason=EXIT_TP, hold_min=10),
            trade(-5.0, reason=EXIT_SL, minute=20, hold_min=20),
            trade(0.0, reason=EXIT_WAKTU, minute=50, hold_min=90),
        ]

        m = stage_metrics(trades, hari_naik=1, drop_top=DROP_TOP)

        self.assertEqual(m.rata_lama_menit, 40.0)


class WithoutTopTests(unittest.TestCase):
    def test_without_top_three_drops_the_best_and_breaks_ties_by_time(self):
        # (name: profit, volume, minute of the entry)
        #   A 100 / 50k / 5     B 50 / 50k / 4
        #   C  20 / 20k / 2     D 20 / 80k / 1     E 20 / 50k / 3
        #   F -100 / 50k / 6
        # The three best are A, B and one of the three that tie at 20: the earliest, D. What is
        # left is C, E and F: -60 on 120,000, which is -500 per 1M. Dropping the latest of the
        # tie (E) would leave C, D and F: -60 on 150,000, which is -400.
        a = trade(100.0, 50_000.0, minute=5)
        b = trade(50.0, 50_000.0, minute=4)
        c = trade(20.0, 20_000.0, minute=2)
        d = trade(20.0, 80_000.0, minute=1)
        e = trade(20.0, 50_000.0, minute=3)
        f = trade(-100.0, 50_000.0, minute=6)

        for order in ([a, b, c, d, e, f], [e, f, c, a, d, b], [f, e, d, c, b, a]):
            with self.subTest(order=[t.volume_usdt for t in order]):
                m = stage_metrics(order, hari_naik=1, drop_top=DROP_TOP)

                self.assertEqual(m.hasil_tanpa_teratas, -500.0)

    def test_the_best_positions_are_ranked_by_profit_and_not_by_profit_per_volume(self):
        # P earns most (30) but on the biggest volume; Q has the best ratio (10 on 20,000)
        p = trade(30.0, 300_000.0, minute=0)
        q = trade(10.0, 20_000.0, minute=1)
        r = trade(5.0, minute=2)
        s = trade(1.0, minute=3)
        t = trade(-50.0, minute=4)

        m = stage_metrics([p, q, r, s, t], hari_naik=1, drop_top=1)

        # without P: (10 + 5 + 1 - 50) = -34 on 170,000 -> -200 per 1M
        self.assertEqual(m.hasil_tanpa_teratas, -200.0)

    def test_drop_top_is_the_number_given_and_not_a_fixed_three(self):
        trades = book(40.0, 30.0, 20.0, 10.0, -10.0, -20.0)

        results = {
            n: stage_metrics(trades, hari_naik=1, drop_top=n).hasil_tanpa_teratas for n in range(5)
        }

        # the profit that remains after dropping the n best, over the volume that remains:
        # 70 on 300k, 30 on 250k, 0 on 200k, -20 on 150k and -30 on 100k
        self.assertEqual(
            results,
            {
                0: 1_000_000 * 70.0 / 300_000.0,
                1: 1_000_000 * 30.0 / 250_000.0,
                2: 0.0,
                3: 1_000_000 * -20.0 / 150_000.0,
                4: -300.0,
            },
        )

    def test_dropping_none_is_the_whole_result(self):
        m = stage_metrics(book(40.0, -30.0, 5.0), hari_naik=1, drop_top=0)

        self.assertEqual(m.hasil_tanpa_teratas, m.hasil_per_1_juta)

    def test_nothing_left_is_none(self):
        for n in (0, 1, 2, 3):
            with self.subTest(positions=n):
                m = stage_metrics(book(*[5.0] * n), hari_naik=1, drop_top=DROP_TOP)

                self.assertIsNone(m.hasil_tanpa_teratas)

    def test_one_position_left_is_its_own_result(self):
        m = stage_metrics(book(9.0, 8.0, 7.0, -5.0), hari_naik=1, drop_top=DROP_TOP)

        self.assertEqual(m.hasil_tanpa_teratas, -100.0)

    def test_dropping_leaves_the_whole_result_alone(self):
        trades = book(9.0, 8.0, 7.0, -5.0, -6.0)

        whole = stage_metrics(trades, hari_naik=1, drop_top=0)
        dropped = stage_metrics(trades, hari_naik=1, drop_top=DROP_TOP)

        self.assertEqual(dropped.hasil_per_1_juta, whole.hasil_per_1_juta)
        self.assertEqual(dropped.posisi, 5)

    def test_the_trimmed_result_matches_a_brute_force_oracle(self):
        rng = random.Random(3)
        for case in range(25):
            n = rng.randrange(4, 14)
            # few distinct profits, so ties at the cut are the rule and not the exception
            trades = [
                trade(
                    float(rng.choice((-20, -5, 0, 5, 5, 20))),
                    rng.choice((20_000.0, 50_000.0, 80_000.0)),
                    minute=i,
                )
                for i in range(n)
            ]
            rng.shuffle(trades)

            m = stage_metrics(trades, hari_naik=3, drop_top=DROP_TOP)

            rest = trades_left_after_dropping_the_best(trades, DROP_TOP)
            pnl = sum(Fraction(t.pnl_usdt) for t in rest)
            volume = sum(Fraction(t.volume_usdt) for t in rest)
            with self.subTest(case=case, n=n):
                self.assertAlmostEqual(
                    m.hasil_tanpa_teratas, float(1_000_000 * pnl / volume), places=9
                )


class NoTradesAndInputTests(unittest.TestCase):
    def test_no_trades_gives_none_where_nothing_can_be_said(self):
        m = stage_metrics([], hari_naik=3, drop_top=DROP_TOP)

        self.assertEqual(
            m,
            StageMetrics(
                posisi=0,
                entry_per_hari_aktif=0.0,
                menang_pct=None,
                hasil_per_1_juta=None,
                tp=0,
                sl=0,
                waktu=0,
                rata_lama_menit=None,
                hasil_tanpa_teratas=None,
                hari_naik=3,
            ),
        )

    def test_any_iterable_of_trades_is_read_once_and_in_full(self):
        trades = book(10.0, -40.0, 5.0, 5.0)

        from_list = stage_metrics(trades, hari_naik=2, drop_top=1)
        from_tuple = stage_metrics(tuple(trades), hari_naik=2, drop_top=1)
        from_generator = stage_metrics((t for t in trades), hari_naik=2, drop_top=1)

        self.assertEqual(from_list, from_tuple)
        self.assertEqual(from_list, from_generator)
        self.assertEqual(from_list.posisi, 4)

    def test_the_trades_are_not_reordered_or_changed(self):
        trades = book(3.0, 9.0, -1.0, 9.0, 4.0)
        before = list(trades)

        stage_metrics(trades, hari_naik=1, drop_top=DROP_TOP)

        self.assertEqual(trades, before)

    def test_two_calls_with_the_same_trades_agree(self):
        trades = book(3.0, 9.0, -1.0, 9.0, 4.0)

        self.assertEqual(
            stage_metrics(trades, hari_naik=2, drop_top=DROP_TOP),
            stage_metrics(trades, hari_naik=2, drop_top=DROP_TOP),
        )

    def test_a_profit_that_is_not_a_finite_number_is_refused(self):
        # a bool is not a profit, an int beyond the float range cannot be summed and text is not
        # a number
        for pnl in (math.nan, math.inf, -math.inf, True, 10**400, "5"):
            with self.subTest(pnl=pnl):
                with self.assertRaisesRegex(ValueError, "pnl_usdt"):
                    stage_metrics(book(1.0, pnl), hari_naik=1, drop_top=DROP_TOP)

    def test_a_volume_that_is_not_above_zero_is_refused(self):
        for volume in (0.0, -50_000.0, math.nan, math.inf, True, None):
            with self.subTest(volume=volume):
                with self.assertRaisesRegex(ValueError, "volume_usdt"):
                    stage_metrics([trade(1.0, volume=volume)], hari_naik=1, drop_top=DROP_TOP)

    def test_a_count_that_is_not_a_whole_number_of_at_least_zero_is_refused(self):
        for name in ("hari_naik", "drop_top"):
            for bad in (-1, 1.5, True, None, "3"):
                with self.subTest(name=name, value=bad):
                    arguments = {"hari_naik": 1, "drop_top": DROP_TOP, name: bad}

                    with self.assertRaisesRegex(ValueError, name):
                        stage_metrics(book(1.0), **arguments)


class PassStatusTests(unittest.TestCase):
    def test_fewer_than_40_positions_is_not_enough_sample_whatever_the_numbers(self):
        for hasil, tanpa in ((500.0, 400.0), (-900.0, -900.0), (-149.0, -249.0)):
            with self.subTest(hasil=hasil, tanpa=tanpa):
                self.assertEqual(
                    pass_status(metrics(posisi=39, hasil=hasil, tanpa=tanpa), UJI),
                    BELUM_CUKUP_SAMPEL,
                )

    def test_no_positions_is_not_enough_sample_and_not_an_error(self):
        m = stage_metrics([], hari_naik=4, drop_top=DROP_TOP)

        self.assertEqual(pass_status(m, UJI), BELUM_CUKUP_SAMPEL)

    def test_forty_positions_just_inside_both_limits_pass(self):
        self.assertEqual(pass_status(metrics(posisi=40, hasil=-149.0, tanpa=-249.0), UJI), LOLOS)

    def test_forty_positions_is_enough_and_so_is_more(self):
        for posisi in (40, 41, 400):
            with self.subTest(posisi=posisi):
                self.assertEqual(pass_status(metrics(posisi=posisi), UJI), LOLOS)

    def test_hasil_per_1_juta_exactly_minus_150_fails(self):
        self.assertEqual(pass_status(metrics(hasil=-150.0, tanpa=-100.0), UJI), GAGAL)

    def test_hasil_just_either_side_of_minus_150(self):
        self.assertEqual(pass_status(metrics(hasil=-149.999, tanpa=-100.0), UJI), LOLOS)
        self.assertEqual(pass_status(metrics(hasil=-150.001, tanpa=-100.0), UJI), GAGAL)

    def test_without_the_top_three_exactly_minus_250_fails(self):
        self.assertEqual(pass_status(metrics(hasil=100.0, tanpa=-250.0), UJI), GAGAL)

    def test_without_the_top_three_just_either_side_of_minus_250(self):
        self.assertEqual(pass_status(metrics(hasil=100.0, tanpa=-249.999), UJI), LOLOS)
        self.assertEqual(pass_status(metrics(hasil=100.0, tanpa=-250.001), UJI), GAGAL)

    def test_either_limit_failing_alone_fails_the_mode(self):
        self.assertEqual(pass_status(metrics(hasil=-151.0, tanpa=-249.0), UJI), GAGAL)
        self.assertEqual(pass_status(metrics(hasil=-149.0, tanpa=-251.0), UJI), GAGAL)
        self.assertEqual(pass_status(metrics(hasil=-300.0, tanpa=-300.0), UJI), GAGAL)

    def test_a_great_result_that_rests_on_three_positions_fails(self):
        self.assertEqual(pass_status(metrics(hasil=400.0, tanpa=-400.0), UJI), GAGAL)

    def test_the_limits_come_from_the_config_and_are_not_fixed_numbers(self):
        looser = dataclasses.replace(
            UJI,
            lolos_minimal_posisi=10,
            lolos_hasil_per_1_juta_lebih_dari=-100.0,
            lolos_hasil_tanpa_teratas_lebih_dari=-200.0,
        )

        self.assertEqual(
            pass_status(metrics(posisi=9, hasil=0.0, tanpa=0.0), looser), BELUM_CUKUP_SAMPEL
        )
        self.assertEqual(pass_status(metrics(posisi=10, hasil=-99.0, tanpa=-199.0), looser), LOLOS)
        self.assertEqual(pass_status(metrics(posisi=10, hasil=-100.0, tanpa=-199.0), looser), GAGAL)
        self.assertEqual(pass_status(metrics(posisi=10, hasil=-99.0, tanpa=-200.0), looser), GAGAL)
        # the same numbers that pass the document's limits fail these
        self.assertEqual(pass_status(metrics(hasil=-120.0, tanpa=-220.0), UJI), LOLOS)
        self.assertEqual(pass_status(metrics(hasil=-120.0, tanpa=-220.0), looser), GAGAL)

    def test_a_result_that_cannot_be_read_is_refused_and_not_failed(self):
        # 40 positions or more with no result is not a defeat; it is a metrics object that
        # cannot be judged
        for hasil, tanpa, name in (
            (None, -100.0, "hasil_per_1_juta"),
            (-100.0, None, "hasil_tanpa_teratas"),
            (math.nan, -100.0, "hasil_per_1_juta"),
            (-100.0, math.nan, "hasil_tanpa_teratas"),
            (True, -100.0, "hasil_per_1_juta"),
            (-100.0, True, "hasil_tanpa_teratas"),
        ):
            with self.subTest(name=name, hasil=hasil, tanpa=tanpa):
                with self.assertRaisesRegex(ValueError, name):
                    pass_status(metrics(hasil=hasil, tanpa=tanpa), UJI)


class Wants5mTests(unittest.TestCase):
    def test_wants_5m_below_three_only(self):
        self.assertTrue(wants_5m(metrics(entry=2.99), UJI))
        self.assertFalse(wants_5m(metrics(entry=3.0), UJI))
        self.assertFalse(wants_5m(metrics(entry=3.01), UJI))

    def test_far_from_three(self):
        self.assertTrue(wants_5m(metrics(entry=0.0), UJI))
        self.assertTrue(wants_5m(metrics(entry=1.5), UJI))
        self.assertFalse(wants_5m(metrics(entry=12.0), UJI))

    def test_the_limit_comes_from_the_config(self):
        higher = dataclasses.replace(UJI, pindah_5m_jika_entry_per_hari_naik_kurang_dari=5.0)

        self.assertTrue(wants_5m(metrics(entry=4.99), higher))
        self.assertFalse(wants_5m(metrics(entry=5.0), higher))

    def test_without_a_naik_day_the_rule_cannot_be_evaluated_and_is_refused(self):
        m = stage_metrics(book(1.0, 1.0), hari_naik=0, drop_top=DROP_TOP)

        with self.assertRaisesRegex(ValueError, "NAIK"):
            wants_5m(m, UJI)

    def test_a_ratio_that_is_not_a_number_is_refused(self):
        for entry in (None, math.nan, math.inf, True):
            with self.subTest(entry=entry):
                with self.assertRaisesRegex(ValueError, "NAIK"):
                    wants_5m(metrics(entry=entry), UJI)

    def test_the_ratio_of_a_real_stage(self):
        # 5 positions over 2 NAIK days is 2.5 a day (switch); 6 over 2 is exactly 3 (stay)
        self.assertTrue(wants_5m(stage_metrics(book(*[1.0] * 5), 2, DROP_TOP), UJI))
        self.assertFalse(wants_5m(stage_metrics(book(*[1.0] * 6), 2, DROP_TOP), UJI))


class EndToEndTests(unittest.TestCase):
    """40 trades whose sums sit exactly on the two limits, and one step either side of them."""

    def verdict(self, trades):
        m = stage_metrics(trades, hari_naik=10, drop_top=UJI.lolos_buang_posisi_teratas)
        return m, pass_status(m, UJI)

    def test_the_book_sits_exactly_on_both_limits_and_fails(self):
        m, status = self.verdict(forty(losers=-12.5))

        self.assertEqual(m.posisi, 40)
        self.assertEqual(m.hasil_per_1_juta, -150.0)
        self.assertEqual(m.hasil_tanpa_teratas, -250.0)
        self.assertEqual(status, GAGAL)

    def test_half_a_usdt_better_on_one_loser_passes(self):
        trades = forty(losers=-12.5)
        trades[-1] = trade(-12.0, minute=39)

        m, status = self.verdict(trades)

        self.assertEqual(m.hasil_per_1_juta, -149.75)
        self.assertAlmostEqual(m.hasil_tanpa_teratas, 1_000_000 * -462.0 / 1_850_000.0)
        self.assertEqual(status, LOLOS)

    def test_half_a_usdt_worse_on_one_loser_fails(self):
        trades = forty(losers=-12.5)
        trades[-1] = trade(-13.0, minute=39)

        m, status = self.verdict(trades)

        self.assertEqual(m.hasil_per_1_juta, -150.25)
        self.assertEqual(status, GAGAL)

    def test_a_good_result_made_by_three_winners_fails_on_the_trimmed_one(self):
        m, status = self.verdict(forty(losers=-12.5, winners=(200.0, 200.0, 200.0)))

        self.assertEqual(m.hasil_per_1_juta, 68.75)
        self.assertEqual(m.hasil_tanpa_teratas, -250.0)
        self.assertEqual(status, GAGAL)

    def test_the_same_book_with_one_position_fewer_is_not_enough_sample(self):
        trades = forty(losers=-12.5)[:-1]

        m, status = self.verdict(trades)

        self.assertEqual(m.posisi, 39)
        self.assertEqual(status, BELUM_CUKUP_SAMPEL)

    def test_the_three_winners_are_the_ones_dropped_wherever_they_are_in_time(self):
        trades = forty(losers=-12.5)
        trades.reverse()

        m, status = self.verdict(trades)

        self.assertEqual(m.hasil_tanpa_teratas, -250.0)
        self.assertEqual(status, GAGAL)


class LiveMetricsTests(unittest.TestCase):
    """POTONG (the cut of version ii) and the calendar-day rate of mode L."""

    def test_potong_counts_as_a_loss_in_menang(self):
        trades = [
            trade(10.0, reason=EXIT_TP, minute=0),
            trade(10.0, reason=EXIT_TP, minute=100),
            trade(-5.0, reason=EXIT_SL, minute=200),
            trade(-3.0, reason=EXIT_POTONG, minute=300),
            trade(-3.0, reason=EXIT_POTONG, minute=400),
            trade(1.0, reason=EXIT_WAKTU, minute=500),
        ]

        m = stage_metrics(trades, hari_naik=3, drop_top=DROP_TOP)

        # 2 TP of 2 TP + 1 SL + 2 POTONG = 5 decided; the time exit stays out
        self.assertEqual(m.potong, 2)
        self.assertEqual((m.tp, m.sl, m.waktu), (2, 1, 1))
        self.assertEqual(m.posisi, 6)
        self.assertEqual(m.posisi, m.tp + m.sl + m.waktu + m.potong)
        self.assertEqual(m.menang_pct, 40.0)

    def test_only_potong_and_waktu_exits_give_zero_menang(self):
        m = stage_metrics([trade(-3.0, reason=EXIT_POTONG)], hari_naik=1, drop_top=DROP_TOP)
        self.assertEqual(m.menang_pct, 0.0)
        m = stage_metrics([trade(1.0, reason=EXIT_WAKTU)], hari_naik=1, drop_top=DROP_TOP)
        self.assertIsNone(m.menang_pct)

    def test_calendar_entries_per_day(self):
        m = stage_metrics(book(*[1.0] * 70), hari_naik=35, drop_top=DROP_TOP, hari_kalender=70)

        self.assertEqual(m.entry_per_hari_kalender, 1.0)
        self.assertEqual(m.entry_per_hari_aktif, 2.0)

    def test_calendar_rate_is_none_without_days_or_without_the_argument(self):
        for days in (None, 0):
            with self.subTest(days=days):
                m = stage_metrics(book(1.0, 1.0), 1, DROP_TOP, hari_kalender=days)
                self.assertIsNone(m.entry_per_hari_kalender)
        self.assertIsNone(stage_metrics(book(1.0), 1, DROP_TOP).entry_per_hari_kalender)

    def test_no_positions_over_calendar_days_is_zero(self):
        self.assertEqual(stage_metrics([], 1, DROP_TOP, hari_kalender=7).entry_per_hari_kalender, 0.0)

    def test_a_calendar_count_that_is_not_a_whole_number_is_refused(self):
        for bad in (-1, 1.5, True, "7"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "hari_kalender"):
                    stage_metrics(book(1.0), 1, DROP_TOP, hari_kalender=bad)

    def test_mode_a_metrics_unchanged(self):
        trades = [
            trade(10.0, reason=EXIT_TP, minute=0, hold_min=30),
            trade(-40.0, reason=EXIT_SL, minute=100, hold_min=60),
            trade(0.0, reason=EXIT_WAKTU, minute=200, hold_min=90),
        ]

        m = stage_metrics(trades, hari_naik=2, drop_top=3)

        self.assertEqual(m.potong, 0)
        self.assertIsNone(m.entry_per_hari_kalender)
        self.assertEqual(m.posisi, 3)
        self.assertEqual(m.entry_per_hari_aktif, 1.5)
        self.assertEqual(m.menang_pct, 50.0)
        self.assertEqual(m.hasil_per_1_juta, -200.0)
        self.assertEqual((m.tp, m.sl, m.waktu), (1, 1, 1))
        self.assertEqual(m.rata_lama_menit, 60.0)
        self.assertIsNone(m.hasil_tanpa_teratas)
        self.assertEqual(m.hari_naik, 2)


if __name__ == "__main__":
    unittest.main()
