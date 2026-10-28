# Exit variants shadow report — TDD Evidence

## Hypothesis (written first, before any forward data)

> **A profit lock adds at least +2 USDT per trade in calm markets** (BTC ATR3m % below 0.113), measured paired on every closed live trade, under the realistic booking with one extra basis point of slippage on lock exits (convention C+1bp).

It is falsified by a mean paired difference below +2 USDT per trade over at least 250 calm trades, or by a difference that is not positive in at least 3 of the last 4 WIB ISO weeks. The report prints exactly this rule (below) and never acts on it.

## User journey

On 28 Oct 2026, reviewing the 22 live trades of 26 to 28 Oct, the user suggested "SL+": move the stop into profit once a trade is net positive, so a winner is not given back. The offline study found no robust gain (next section), so nothing goes live. The agreed next step is a zero-risk measurement: replay pre-registered exit rules on every closed live trade from 1m candles and report the **paired** difference against the live rules. Paired means the same entry and the same candles under two exit rules, so the market's noise cancels; that needs 12 to 20 times fewer trades than a live A/B (a +5 USDT per trade effect needs about 40 to 70 trades, +2 about 250 to 450, +1 about 900 to 1,800). The report changes no trade, places no order, writes nothing but stdout, and is run by hand from either bot's directory: account 1 reads schema `public`, account 2 `long_forward`.

## Evidence base and its caveats

Comparator S (the near-mean SHORT entry in TURUN hours with the live exits, backtested 1 Aug 2025 to 13 Oct 2026: 1,098 positions, real fee 0.05 %): baseline -504 per 1M, gross -0.09 +- 0.84 bps.

| Result | Number |
|---|---|
| Locks and trails, convention C | +0.3 to +0.9 USDT per trade (+7 to +19 per 1M); every 95 % day-bootstrap interval spans 0; about 0 with +1 bp of lock slippage; positive in the train half and negative in the test half |
| The same, corrected convention B | -53 to -80 per 1M (interval below 0) |
| Lower floors +30 to +50 | about 0 |
| Mirror LONG in NAIK (852 positions, gross -1.02 +- 0.92 bps) | variants about 0 (C -0.2 to +0.2; B -75 to -77 per 1M) |
| Calm-market hint, ATR3m % below 0.113 | `lock 45->20` +1.90 USDT per trade [+0.45, +3.51] on S, concentrated in a few months and not replicated on LONG (-0.74 [-2.05, +0.59]) |
| Live, 22 trades (26 to 28 Oct) | in-sample +6 to +8 USDT per trade +- 4 to 7 (t 1.0 to 1.6), chosen after seeing the peaks: not evidence |

Caveats from the independent review: 1m bars cannot show the wicks a 5-second poll sees; the target is booked at 0 bps while a lock pays 0.5 bps (live floor exits averaged +1.5 USDT better than the level, stops 1.7 worse); the test half is not a holdout and the intervals are not adjusted for the many variants tried.

## Design

- **Inputs.** Closed trades with `closed_at` in `[--since, --until)` (ISO-8601 with a timezone; a naive time exits 2), read through `SupabaseTradeStore.closed_trade_rows_since` from the schema of the working directory's `.env`. The model's OPEN decisions come from one extra read, `ReportStore.open_decision_rows_between` (a subclass inside the script, so `src/bot` is untouched), which asks PostgREST for scalars only: `payload_json->decision->>stop_loss_pct`, because a stored request payload is tens of kilobytes. The 1m candles come from the kline cache `{"k1": {"<bar open ms>": [open, high, low, close]}}` (`--klines`, default `~/replay/edge_klines.json`).
- **The stop is derived, not stored**, as `AIOrchestrator._resolve_trade_targets` does it: `max(model stop or STOP_LOSS_PCT, ATR_SL_MULTIPLIER x setup.atr_pct)`, capped at `MAX_STOP_LOSS_PCT` when that is above 0. The model's stop is read from the nearest preceding OPEN decision of the same symbol, side and confidence within 15 minutes before `opened_at`. Measured on the 22 live trades, that decision was always 3.6 to 4.2 seconds before `opened_at` and the only candidate in its window, so there is no clock-skew tolerance. A trade without a match, without `setup.atr_pct`, with unreadable fields or for another symbol is listed and skipped, never guessed.
- **The walk** (`walker.py`, a port of the validated scratch implementation). Entry at the first full 1m bar after the entry minute at the real fill; time limit `POSITION_HOLD_MINUTES - 1` bars; the live bracket (hard stop, net floor, time exit) inside each bar in the order of `resolve_live_exit` (a bar that touches both the stop and the target is a stop); the variants on top. Nets are the bot's own estimate after both taker fees (`TAKER_FEE_PCT`), the number its neutral exit reads.
- **Booking conventions** for a lock inside the bar that arms it. A arms from the next bar (not a bound, not reported). B arms and stops in the same bar (an extreme bad-case bound, corrected: the arming best is capped at the target, which would have closed the trade first). C is A plus "a bar that arms or ratchets the stop and closes beyond it is stopped inside the bar at the level" (the realistic middle). C+1bp adds one more basis point of adverse slippage to lock exits (closest to a live stop-market order). Market exits cost 0.5 bps and the target 0 bps (`config/modes_live_exit.yaml`).
- **Variants, fixed in code, no tuning.** `lock 45->20` (primary: arm at net +45 USDT, stop at net +20), `floor +45`, `lock 40->15`, `trail 40/20` (arm at +40, trail 20 behind the peak). The live floor is +56; the report warns when the setting differs, and when `USE_EXCHANGE_FEE_RATES=true` (the live exit then nets the venue's rate, not `TAKER_FEE_PCT`).
- **Output.** Indonesian headings and plain-text tables: the sample (n, period, every skipped trade with its reason), the calibration line (the simulated live exit reason equals the real one: x/n, a warning below 90 %), the parameters, then for C+1bp, C and B, over all trades and over the calm ones, per variant: the paired mean in USDT per trade +- SE, t, a 95 % day-bootstrap interval (only from 8 distinct days), the difference per 1M of real volume, how many trades the variant changed, and the mean by WIB ISO week. Last, the decision rule as text and a line saying the report changes no trade. Exit codes: 0; 1 (one `error:` line: unusable cache, live exits the study does not model, a failed read); 2 (a naive time, `--until` not after `--since`).

## Decision rule (pre-registered)

Evaluated for the primary variant under C+1bp only. The secondary variants are descriptive and get no verdict, because nothing here corrects for the number of variants tried.

| Criterion | All trades (`semua`) | Calm trades (`tenang`) |
|---|---|---|
| Trades | at least 60 | at least 250 |
| Mean paired difference | at least +5 USDT per trade | at least +2 USDT per trade |
| t (mean over its standard error) | at least 2 | at least 2 |
| Weeks | a positive weekly mean in at least 3 of the last 4 WIB ISO weeks (fewer than 4 weeks cannot show it) | same |
| Calibration | at least 90 % | same |

All five must hold for "layak jadi lengan live"; otherwise "belum cukup". Even then the report says it is the result of a rule and not an instruction to enable anything: enabling a lock is the manager's decision at a decision point, with its own spec, hypothesis, offline test and a flat bot.

## Structure, and one deliberate exception

- `src/research/exit_variants/walker.py`: the pure walker (`ExitRule`, `walk`, `settle`, `price_for_net`, `net_for_price`) and the statistics (`mean_se`, `day_bootstrap`, `group_by_day`). `fee_pct` is a percent everywhere (the repo's convention) and the slippage assumptions are one `Costs` tuple.
- `src/research/exit_variants/study.py`: the registry above, the stop derivation, the decision matching, the paired simulation, the aggregation and the decision rule, all pure. It is not in the brief's file list; with it, the script alone would have been about 1,000 lines.
- `src/report_exit_variants.py`: the I/O (store, kline cache, stdout) and the Indonesian text.
- `tests/exit_variants_helpers.py`: shared synthetic rows and bars (the repo's `tests/<name>_helpers.py` convention).
- **`tests/test_pine_gate_structure.py` changed.** Its `test_no_bot_module_or_main_imports_research` forbids every source under `src/` outside `research/`, scripts included, from importing `research`, and no script did. The brief says the report script may, so `RESEARCH_IMPORTERS` now names exactly `src/report_exit_variants.py`, limited to `research.exit_variants`. Three new tests prove the entry is not dead, that a second script is still refused, and that the script may not reach another research package or import dynamically. Nothing under `src/bot` changed, and `research/__init__.py` (inside the lock fingerprints) is untouched.

## RED evidence

The first run of each new test module, before its code existed:

```
tests.test_exit_variants_walker   ModuleNotFoundError: No module named 'research.exit_variants'
tests.test_exit_variants_study    ImportError: cannot import name 'study' from 'research.exit_variants'
tests.test_report_exit_variants   ModuleNotFoundError: No module named 'report_exit_variants'
Ran 1 test — FAILED (errors=1)    (each)
```

Two small cycles inside the work:

```
ERROR: test_the_variants_were_registered_for_a_live_floor_of_56 (tests.test_exit_variants_study.RegistryTests)
AttributeError: module 'research.exit_variants.study' has no attribute 'REGISTERED_FLOOR_NET'

FAIL: test_missing_figures_print_as_dashes (tests.test_report_exit_variants.CandidateTextTests)
  the line "pekan searah: belum ada pekan" was absent: the first wording read "0 dari 0 pekan terakhir positif (baru 0 pekan)"
```

The isolation rule, after the script existed and before the exemption:

```
FAIL: test_no_bot_module_or_main_imports_research (test_pine_gate_structure.IsolationTests)
AssertionError: Lists differ: ['report_exit_variants.py: research.exit_variants',
  'report_exit_variants.py: research.exit_variants.study', 'report_exit_variants.py: research.exit_variants.walker',
  'report_exit_variants.py: research.exit_variants.walker.Cols'] != []
```

and the three new guard tests, written first: `NameError: name 'RESEARCH_IMPORTERS' is not defined` (3 of 3).

## GREEN implementation

The first GREEN pass was not clean, and every miss was in a test's expectation, none in the code under test. Walker: 7 of 86 failed (a 10-bar tape for a 10-bar limit has no time-exit bar; `dataclasses.replace` on a NamedTuple; the SHORT exit notional in a fee expectation). Study: 71 of 71. Report: 43 of 46 (a "few weeks" filter that kept three weeks; a calm-market case whose trade was not calm; a second trade that closed after `--until`). The null-rule equivalence test passed the first time it ran.

- The walker's null rule is pinned to the backtest's own walk: on 3,000 seeded random walks per side (jumps between bars, missing minutes, data ending early, stops, targets and time exits) it gives the exit reason, the bar, the raw price and the gap flag of `research.mode_backtest.live_sim.resolve_live_exit`, and the test asserts that every kind of ending and more than 100 gaps were really compared.
- The 38 hand-built checks of the scratch study became unittest cases run for both sides (lock and trail under A, B and C, the two bugs the independent review found, the stop-first order, gaps, the time limit), plus the money (`settle`) and the statistics.
- The study is tested against hand-computed deltas (a path that peaks at net +47 and ends at +5: `lock 45->20` +15.0 under C and +12.5 under C+1bp, `floor +45` +41.25, `trail 40/20` +22.0 and +19.5, `lock 40->15` free under C and -2.5 under C+1bp), every skip code, the WIB week boundary (Sunday 23:30 WIB is week 44, Monday 00:30 WIB is week 45) and each criterion of the rule failing on its own.
- The script is tested with a fake store that has only the two reads the report may use, a temporary kline file, the one-line `error:` rule (a multi-line Supabase body collapses to one line), the exit codes, and the `ai_decisions` request through the store's own fake HTTP: select, exchange filter, action filter, the time window, ordering, paging past 1,000 rows, and `Accept-Profile` for both schemas.

## Verification

Python 3.10.20 (the local `.venv`), on commit `06eb438`; coverage uses an rc file equivalent to `pyproject.toml` because coverage on 3.10 cannot read it:

```
PYTHONPATH=src python -m compileall -q src tests                                   # OK
PYTHONPATH=src python -m coverage run --rcfile=<pyproject equivalent> -m unittest discover -s tests
Ran 3232 tests in 1229.623s
OK
PYTHONPATH=src python -m coverage report --rcfile=<pyproject equivalent>
src/report_exit_variants.py     231 statements   0 missed  100%
src/research/exit_variants/study.py   273         0         100%
src/research/exit_variants/walker.py  177         0         100%
TOTAL                          18197   1399   92%          # fail_under = 80: passed
git diff --check                                                                     # OK
tests.test_secret_hygiene                                                            # OK (inside the full run)
```

The new tests are 88 (walker), 73 (study) and 52 (report), plus 3 isolation guards in `tests/test_pine_gate_structure.py`; the rest of the suite is unchanged. The first full run (before the logging fix) was 3,225 tests, OK, and 92 % overall; its coverage report is what showed the "unusable kline cache" test was mostly vacuous (see the commit of the fix).

## Real-data check

The report was run on the 22 real trades of 26 to 28 Oct: a local export of account 1's `trades` and `ai_decisions` through a fake store, with the VPS cron's kline cache (68,897 bars, no minute missing, loaded in 1.1 s). Calibration was 22 of 22 (100 %), and the paired means reproduce the scratch study's live-22 figures to the second decimal in every column it printed, A, C and B: `floor +45` +7.27 +- 6.59, `lock 40->15` +5.87 +- 4.31, `lock 45->20` +6.69 +- 4.59, `trail 40/20` +8.35 +- 5.32 under C and +3.83 +- 5.81 under B. Under the report's main row C+1bp, `lock 45->20` is +6.35 +- 4.44 USDT per trade (t 1.4) and changes 3 of the 22 trades; the verdict is "belum cukup" (22 of the 60 trades, t below 2, one week of the four). The same 22 trades inspired the variants' numbers, so this checks the code, not the hypothesis.

## Limitations

1. **1m bars.** A 5-second poll can see a touch that a bar's range hides, and the walk reads a bar's close as the next bar's open. C and B bracket the realistic reading and the bad case; booking A is not reported because it is not a bound.
2. **Costs.** The target is booked at 0 bps while a lock pays 0.5 bps (live floor exits averaged +1.5 USDT better than the level, stops 1.7 worse). C+1bp is the closest reading to a live stop-market order, and the decision rule reads it.
3. **Statistics.** Most trades are unchanged (difference 0) and a few move a lot (above, a lock changes 3 of 22), so t is a rough guide; the day-bootstrap interval and the weekly sign check are the guards. Trades of one day are not independent, and a week with one trade counts like a week with ten.
4. **Multiplicity.** Four variants, three conventions and two subsets are printed but only the primary variant under C+1bp is judged, and the 14-month backtest's "test half" was not a holdout. A secondary variant that stands out is a hypothesis for a new pre-registration, not a candidate.
5. **The cache.** It is refreshed daily by cron, so the newest trades (under 60 minutes of candles) are skipped and listed, and a hole in the cache skips the trades that cross it.
6. **The stop is derived.** If the engine's formula changes, `derive_stop_pct` must follow; a trade whose OPEN decision row is missing is skipped. Calibration (the live walk reproduces the real exit reason) is what shows the derivation and the floor are right, and below 90 % the report says so.
7. **The `ai_decisions` read** uses PostgREST JSON extraction, `payload_json->decision->>stop_loss_pct`. The PostgREST documentation gives the returned key as the last path segment (`stop_loss_pct`), the `in.(A,B)` filter as written, and two filters on `created_at` are the pattern `_closed_trade_rows_for_day` already uses in production. It is tested here through the store's fake HTTP (parameters, paging, schema header) and not against the live API, which this session could not reach (no network, no `.env`); if the server rejects it, the report exits 1 with the response body in its one `error:` line, so the first run on the VPS settles it.
8. **Account 2** (retired) used AI exits, so its trades calibrate low and the report says so. With `USE_EXCHANGE_FEE_RATES=true` the report warns that it uses `TAKER_FEE_PCT` while the bot nets the venue's rate.
9. **Second-order effects.** The report asks what a rule would have done to trades the current rules opened. A different exit changes how long a position is open and so when the next entry can happen (one position at a time); that is not measured.
10. **Logging.** The script does not call `setup_logging`: that opens `logs/bot.log` in the working directory, which in a bot's directory is the bot's own rotating log (a test pins that no `logs/` appears).

## Deployment gate

1. Merging changes nothing for trading: no setting, no trade path, nothing under `src/bot` imports the new code, and `src/bot` is not edited. Pulling `main` on the VPS needs no restart.
2. Run it by hand from the bot's directory: `cd /home/ubuntu/ai-perpetual-trading && PYTHONPATH=src python src/report_exit_variants.py --since <ISO with timezone>`. The first run also settles the PostgREST select (limitation 7).
3. Its verdict is data for a decision point. Enabling any exit variant is a separate change with its own spec, hypothesis, offline test and a restart while flat; the report says so itself.
