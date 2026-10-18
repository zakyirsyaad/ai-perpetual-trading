# LONG mirror of the live SHORT bot, with the live exits and measured costs — design

Date: 2026-10-18 · Status: draft for review · Source: the manager's answers of 2026-10-18 (relayed
by the user, quoted in §2) to three questions about the live exit rules, plus his earlier request to
replace the slippage assumption with measured fills · Builds on:
`docs/superpowers/specs/2026-10-17-long-module-phase1-design.md` (Phase 1: harness, data, split and
one-shot lock, all reused), `docs/research/2026-10-18-modul-long-mode-a.md` (Mode A on BTC: GAGAL),
`docs/research/2026-10-18-log-order-{entry,exit}-12-16okt.{pdf,csv}` (the measured fills).

## 1. Intent

Mode A failed its one-shot test and cannot be retested on the same period. The manager now wants
a LONG that differs from the live SHORT bot in **one thing only, the direction**, so the two can be
compared directly:

- **Entry:** the live bot's entry rule as a rule (no AI): price near its 3m mean, LONG, only while
  the condition is NAIK.
- **Exit:** the live SHORT bot's exits, mirrored, written as formulas. The AI exit cannot be a
  formula, so the backtest runs two versions: without an early close, and with the manager's
  stand-in for the AI.
- **Costs:** the measured fills replace the 0.01 % per order assumption.

The AI version (live exits including AI closes) cannot be backtested. It goes to a forward test on
a second VST account with its own bot; that is a separate spec (§10).

## 2. Source: the manager's answers (2026-10-18, verbatim in substance)

1. **Entry: (b)**, the mirror of the current entry rule, only in NAIK. Mode A failed and cannot be
   retested on the same period; (b) changes only the direction, so the result compares directly
   with SHORT.
2. **SL: (b)**, `max(0.28 %, 1.5 × ATR 3m)` capped at 0.4 %. More important:
   - TP must use the live **net +56 USDT** rule, not the AI's take-profit level.
   - Without the AI the backtest loses its main early stop. Run two versions: (i) no early close;
     (ii) the stand-in for the AI: **close when price is against the position by ≥ 0.10 % after at
     least 10 minutes**, as the AI has acted since 16 Oct 20:50 WIB.
   - **Slippage:** entry 0.3 bps, market exit 0.5 bps, TP filled exactly at its trigger level.
3. **Forward test: (a)**, a second VST account with a separate bot. The SHORT forward test is
   evaluating the 10-minute AI fix and the down-trend rule; sharing one position slot would mix the
   two results and cut the SHORT sample.

## 3. The live rules this mirrors (read 2026-10-18 from the code and the VPS settings)

| Rule | Live SHORT | Backtest LONG (this spec) |
|---|---|---|
| Entry filter | stretch gate: `abs(close − EMA21) / ATR14` on the last 39 closed 3m bars ≤ 0.75 (`enforce`) | the same number, the same band (Mode D's trigger) |
| Who opens | the AI, once per tick, then the gate | every closed 3m candle that passes, in NAIK |
| SL | `min(max(AI stop, 1.5 × ATR14 3m %), 0.4)`; the AI's value set it in 30 of the last 32 trades | `min(max(0.28, 1.5 × ATR14 3m %), 0.40)` |
| TP that acts | neutral exit, mode `floor`: first poll with net ≥ +56 USDT, net after the bot's own fee estimate of 0.05 % per side; a +0.324 % move at 25,000 USDT | the same level, reached on 1m bars |
| AI take-profit level | `max(AI TP, 3 × ATR, fee floor)`, only if gross ≥ fee; never reached first in practice | not used |
| Time stop | 60 min | 60 min |
| AI close | from minute 10; honoured if gross ≥ fee estimate or net ≤ −35 USDT | (i) none; (ii) price ≥ 0.10 % against from minute 10 |
| Fills | taker market orders after a 5 s price poll | §5 |

ATR14 here is `bot.strategy.indicators.atr` (the mean of the last 14 true ranges) on closed 3m
bars, the same value the stretch uses, so one number serves both.

## 4. Rules of mode L

**Mode L (LONG):** at each closed 3m candle `t` inside the stage, when Mode D's measurement gives
`abs(stretch) ≤ 0.75` and the condition at `t` is NAIK, the signal meets the general gates of
Phase 1 (§6) and, on pass, enters LONG at the open of the 1m bar at `t`.

**Comparator S (SHORT):** the same trigger and exits with side SHORT in TURUN: the live bot's own
rule under this backtest's exits and costs. **Comparator "no trading":** 0.

**Levels**, fixed at entry from the entry fill `e`, quantity `q = notional / e`, `r` = the bot's fee
estimate (0.0005), `N` = 56 USDT, `side` = +1 for LONG, −1 for SHORT:

- `sl_pct = min(max(0.28, 1.5 × atr_pct), 0.40)`, with `atr_pct = ATR14(3m) / close × 100` at `t`;
  SL price `e × (1 − side × sl_pct / 100)`.
- TP price: where the bot's estimated net reaches `N`: LONG `(e(1 + r) + N/q) / (1 − r)`, SHORT
  `(e(1 − r) − N/q) / (1 + r)`; at 25,000 USDT that is `e × 1.0032416` (+0.3242 %) for LONG and
  `e × 0.9967616` (−0.3238 %) for SHORT.
- Cut price (version ii only): `e × (1 − side × 0.10 / 100)`, active from the bar that opens 10
  minutes after entry.
- Time exit: the open of the bar 60 minutes after entry.

**Walk** on 1m bars from the entry bar, first bar that decides, Phase 1 order extended:

1. the time-exit bar: exit at its open (WAKTU);
2. open beyond SL: exit at the open (SL); open beyond the active cut: exit at the open (CUT); open
   beyond TP: exit at the open (TP);
3. low (LONG) or high (SHORT) through SL: SL at the SL price; else through the active cut: CUT at
   the cut price; else high (LONG) or low (SHORT) through TP: TP at the TP price. A bar that
   reaches a loss level and TP is taken as the loss (pessimistic, as in Phase 1).

The cut sits inside the SL (0.10 % < 0.28 %), so in version (ii) the SL acts only within the first
10 minutes or on a bar that jumps past both.

## 5. Costs (replacing the 0.01 % per order)

| Fill | Slippage | Source |
|---|---|---|
| Entry (market) | 0.3 bps against | 32 live entries: mean +0.31 bps from the book mid |
| SL, CUT, WAKTU (market exits) | 0.5 bps against | 20 live AI exits: mean +0.40 bps from the trigger price; the manager rounded up to 0.5 |
| TP | 0 | the manager: filled exactly at its trigger level |
| Fee | 0.035 % of each fill's notional | all 32 live fills |

Result per 1M stays `1,000,000 × Σ pnl / Σ volume`. The live SL overshoot (≈ 1 bp, from a 5 s poll)
is not modelled: the manager set 0.5 bps for every market exit.

## 6. Unchanged from Phase 1

- Data: the same production 1m copy (`~/replay/mode_backtest/data`, 892 files, `verify-copy`).
- Condition selector, closed-candle discipline (D2), warm-up rules (D3), segment edges (D10).
- General rules from `config/modes.yaml` `umum`: one position per pair, 15-minute pause after an
  exit, 6 entries per pair and 10 in total per WIB day, the volatility guard, the weekly window Mon
  07:00 – Sat 00:00 WIB, notional 25,000 USDT. **Default, needs a yes:** the live bot has its own
  limits (one position in total, cooldowns, daily trade target); this spec keeps the document's
  general rules because they apply to every mode and make L and S comparable inside one harness.
- Stages and the one-shot lock pattern: `train` (training 2025-08-01 – 2026-08-04 WIB, writes the
  lock) and `test` (2026-08-05 – 2026-10-13 WIB, once, start record and marker before any test-day
  read). Nothing is selected in training; it runs for the report and the lock.
- Pass rule, for each version separately: ≥ 40 positions, result per 1M > −150, and > −250 without
  the top 3. Fewer than 40 positions is BELUM CUKUP SAMPEL, not a failure.

## 7. Configuration

A new file `config/modes_live_exit.yaml` holds mode L's numbers, so `config/modes.yaml` and Mode A's
lock stay as they are:

```yaml
mode:
  L_cermin_live:
    aktif: true
    tf_pemicu: 3m
    candle_jendela: 39
    ema_periode: 21
    atr_periode: 14
    maks_jarak_ke_ema_kali_atr: 0.75
    sl_minimal_persen: 0.28
    sl_kali_atr: 1.5
    sl_maksimal_persen: 0.40
    tp_net_usdt: 56
    fee_perkiraan_persen: 0.05      # the live bot's own estimate, used only for the TP level
    batas_waktu_menit: 60
    potong_rugi_persen: 0.10        # version (ii)
    potong_rugi_setelah_menit: 10   # version (ii)
biaya:
  slippage_entry_bps: 0.3
  slippage_exit_market_bps: 0.5
  slippage_tp_bps: 0
```

The loader keeps Phase 1's strictness (unknown, missing, mistyped or repeated keys, plain numbers
only). The lock binds the sha256 of both YAML files, the code fingerprint, the training manifest
and the Python minor version.

## 8. Harness changes

- `bot/strategy/modes/near_mean.py`: a side-neutral `near_mean(stretch, max_abs)`; `near_mean_short`
  stays as a name for it.
- `bot/strategy/modes/live_exit.py` (new, numbers only): `live_exit_levels(entry_fill, side,
  atr_pct, notional, rules) -> LiveExitLevels(sl, tp, cut)`.
- `research/mode_backtest/simulate.py`: a walk with the optional cut and its start bar, exit kind
  CUT, and fills priced per exit kind (§5). Mode A's walk and money stay byte-for-byte as they are
  (its lock fingerprint changes anyway once these files change; Mode A's test is spent).
- `research/mode_backtest/stages.py` and `cli.py`: `train --mode L` and `test --mode L` on BTC-USDT,
  running L(i), L(ii), S(i), S(ii) and no trading; records `L_BTC-USDT_test_started.json` and
  `holdout_runs/L_BTC-USDT_<lock sha>.json`. Mode A stays refused for a rerun by its own records.
- `tradelog.py`: a `versi` column (`i`/`ii`) and the exit reason `POTONG`; `report.py`: the §2.9
  table with both versions and both comparators, and the pass status per version.
- `rescore` (new stage, reporting only): Mode A's and the Phase 1 comparator's existing trade logs
  repriced with §5's costs, keeping each trade's exit event; labelled a cost sensitivity, never a
  pass or fail. It reads only the trade logs, not the holdout bars.

## 9. Testing

Stdlib unittest, TDD evidence in `.github/tdd/long-mirror-live-exit.tdd.md`, coverage ≥ 80 %.
Unit tests pin the level formulas (TP = `e × 1.0032416` LONG and `e × 0.9967616` SHORT at 25,000
USDT; SL clamps at 0.28 and 0.40), the walk order (cut not active before minute 10; loss before TP in one bar; a gap past the
cut at the minute-10 open exits at the open), the per-kind fills, the config refusals, and that
Mode A's trade log on the synthetic week is unchanged. Stage tests use the synthetic week only;
nothing rehearses against `~/replay/mode_backtest/data`.

## 10. Out of scope: the forward test (its own spec)

The AI version runs on a **second VST account with a separate bot** (manager's answer 3). It needs:
a new VST account and API key that the user creates, its own pm2 app and working directory, its
own Supabase rows (an account tag or separate tables, so the live bot's daily guards, volume and
dashboard never count them), a LONG-only NAIK gate, and the mirrored exits with the AI close.

**Question for the manager before that spec:** his document says (p. 7) that if the AI is used,
it should be "a separate test after the pure rules pass". Should the AI forward test start only if
L(i) or L(ii) passes this backtest, or start now in parallel?

## 11. Risks and limits

- **The test window is used again** (Pine, step 0, Mode A, now L). Each further test on it raises
  the chance that one passes by luck; the forward test stays the real gate.
- **Two versions on one test** are two hypotheses; both are reported, each with its own status.
- **The backtest enters on every near-mean NAIK candle**; the live bot enters only when the AI says
  OPEN. L measures the rule, not the AI's entry choice.
- **The TP level uses the bot's 0.05 % fee estimate** (as the live trigger does), while the result
  books the real 0.035 % fee.
- **1m bars cannot see the 5 s poll**; the live SL overshoot of about 1 bp is not modelled (§5).
