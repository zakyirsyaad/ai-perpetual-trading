# Pine Gate Phase 1 — TDD Evidence

Offline evaluation of a Pine-adapted entry gate for the BingX VST bot. The work lives in `src/research/pine_gate/` and `src/bot/strategy/pine_indicators.py`. The bot never imports `research`, and `pine_indicators.py` is not wired into the live bot. Spec: `docs/superpowers/specs/2026-10-14-pine-adapted-gate-design.md`. Plan: `docs/superpowers/plans/2026-10-14-pine-gate-phase1.md`. Report (Indonesian): `docs/research/2026-10-16-pine-gate-phase1-report.md`.

## User journey

The manager asked on 14 October 2026 to adapt, not replace, a Pine trend/pullback script into the bot's gates, with TP = SL = 0.2% on BTC and ETH and a target win rate of at least 60% on each symbol., with a realistic net of about -210 per 1M volume at a 60% win rate.

The decisions taken with the user:

- Phase 1 is research only: it does not touch the live bot or `.env`, and no gate reaches the bot unless a candidate passes the locked holdout.
- The evaluation is pre-registered. Every number is in `spec.py`, hashed as `spec_sha256()`, and the holdout (5 August – 14 October 2026) is read once, after the selection file is written.
- Three contenders: (1) Pine structure, (2) the bot's current logic as a control, (3) a meta-labeling model on the contender 1 rule. Plus naive always-long and always-short controls (C0) that validate the fill model.
- Production public klines are the data source because VST volume is not real (VST prices match production; volume does not).
- The verdict sentence of the report (Indonesian) was left as a stub (ruling R7), then drafted at the user's request and approved by the user before commit.
- The user approved the VPS run, approved merging PR #31 (the controller merged and ran it on the VPS), and asked for per-PR review waves (see Review gates).
- Phase 2 (wiring any gate into the bot) is only triggered by a pass. Nothing passed.

## RED evidence

Each task started with a failing test. Excerpts come from the per-task reports.

| Task | Module | RED excerpt |
|---|---|---|
| 1 | `pine_indicators` | `Ran 1 test ... FAILED (errors=1)` (module did not exist); fix round: DMI TR seed differed from Pine `ta.dmi` (no RED run recorded, verified by hand) |
| 2 | `spec` | import error, no `research` package; fix round: `ImportError: CONTENDER_PREFERENCE etc. not defined`, then 22 tests OK |
| 3 | `bars` | `ImportError` (1 error); second RED pass showed `load_days` signature errors until `interval_to_ms` was added |
| 4 | `fetch` | no RED run: the module was written before its tests in one sitting (recorded as a process deviation, reviewed clean, 33 tests, 100% line coverage) |
| 5 | `features` | `ModuleNotFoundError: No module named 'research.pine_gate.features'`; fix round: `AssertionError: 784.0343921079512 != 764.3571063396703` (4h/1h warm-up seam dropped the native bar of a partial leading bucket), `FAILED (failures=3)` |
| 6 | `rules` | `ModuleNotFoundError: No module named 'research.pine_gate.rules'` |
| 7 | `outcomes` | `ModuleNotFoundError: No module named 'research.pine_gate.outcomes'` |
| 8 | `simulate` | `ModuleNotFoundError: No module named 'research.pine_gate.simulate'` |
| 9 | `stats` | `ModuleNotFoundError: No module named 'research.pine_gate.stats'`; the dispatch formula for the SPA recentring failed the clear-winner test with p = 0.511, replaced by Hansen's (2005) consistent recentring |
| 10 | `selection` | `ModuleNotFoundError: No module named 'research.pine_gate.selection'`; fix round: `FAIL: test_supply_limited_fallback - RuleSpec(E200,P35,ADX,OFF,OFF) unexpectedly found in sel.top`, `Ran 19 tests ... FAILED (failures=4)` |
| 11 | `metalabel` | `ImportError: cannot import name 'metalabel' from 'research.pine_gate'` |
| 12 | `calibrate` | `ModuleNotFoundError: No module named 'research.pine_gate.calibrate'` |
| 13 | `pipeline` | `ImportError: cannot import name 'pipeline' from 'research.pine_gate'`; fix round (lock accepted injected configs under the real sha): `Ran 46 tests`, `FAILED (failures=9, errors=5)` |
| 14 | `holdout` | `ImportError: cannot import name 'holdout' from 'research.pine_gate'`; fix round: `AssertionError: dataset.loaded_until_ms read before the lock refused`, `Ran 94 tests`, `FAILED (failures=6, errors=4)` |
| 15 | `checks` | `ModuleNotFoundError: No module named 'research.pine_gate.checks'` |
| 16 | `report` | `ImportError: cannot import name 'report' from 'research.pine_gate'`; fix round: `ERROR: test_non_finite_numbers_render_as_dash` (`round(inf)` overflows) |
| 17 | `cli` | `ImportError: cannot import name 'cli' from 'research.pine_gate'`; fix round: `AssertionError: data loaded` in three vst-check/decisions tests |
| Final fix wave | A (holdout integrity) | `KeyError: 'code_sha256'`, `KeyError: 'c0_training'`, `AttributeError: 'Dataset' object has no attribute 'train_part_sha256'`; real-sha lock cases `AssertionError: dataset.loaded_until_ms read before the lock refused` (5 subtests) |
| Fingerprint fix | code sha | `AssertionError: Lists differ: ['bot/arena/setup_profile.py', 'bot/strategy/indicators.py'] != []` |
| PR #31 wave 1 | meta-model M4 | `FAIL: test_fit_satisfies_first_order_conditions  AssertionError: 4.652389265024596 != 0.0 within 6 places`, `FAIL: test_the_penalty_means_the_same_at_every_weight_scale  AssertionError: 2.339352852923954 != 3.259937223452468`, `Ran 33 tests — FAILED (failures=3, errors=1)` |
| PR #31 wave 2 | lock before data | under the real sha with `seed=1` the whole training ran, then failed inside the meta-model (`fold 1: 0 training, 0 validation candidates`) instead of refusing up front |
| PR #31 follow-up | F2 / F3 | `ImportError: cannot import name 'threshold_candidates'`; `AttributeError: ... has no attribute 'holdout_run_status'`; behavioural RED `chosen C 10.0 ... threshold 0.5 supply_oof_at_threshold 2.7` against a floor of 10 trades/day |
| PR #32 | fetch limits | `IncompleteDayError: BTC-USDT 1h 0..5183999999: got 1000 of 1440 bars`; `['1440', '1060'] != ['1000', '1000', '500']`; `ExchangeError: exchange code 100410`; funding `3 != 14` windows (11 failures/errors) |
| PR #33 | purge from latency | `ValueError: a trade can last 74.1 min from its tick, longer than the 65-minute purge`; `65 != 75`; `Ran 201 tests ... FAILED (failures=5, errors=9)` |

Honest gaps: Task 4 has no RED run, and Task 1's fix round has no recorded RED run. Both were flagged in the ledger and accepted by review. In the final fix wave two RED-phase tests reached the public BingX kline endpoint (no keys; one 109400 response); tests now block every `httpx` entry point.

## GREEN implementation

- `pine_indicators.py`: Pine-compatible RMA, EMA, RSI, ATR, DMI, causal, `None` until warmed up.
- `spec.py`: every pre-registered number, hashed in `spec_sha256()`; rule semantics and policies are inside the hashed document.
- `bars.py`, `fetch.py`: columnar bars, WIB day files, atomic writes, public production klines and funding (host allow-list, no redirects, at least 1 s between requests), daily collector with 7-day backfill.
- `features.py`, `rules.py`, `outcomes.py`, `simulate.py`: look-ahead-free feature snapshots with a 24 h POC; the three contenders plus the reference; first-touch bracket outcomes with gap and stop overshoot; a tick-clock simulator with the bot's guards and timing.
- `stats.py`, `selection.py`, `metalabel.py`: win-rate statistics with day-clustered SE, Holm, PBO and SPA; two-stage rule selection with block consistency; purged expanding-window logistic meta-model.
- `calibrate.py`, `pipeline.py`, `holdout.py`, `lock.py`: latency and stop overshoot from live Supabase records (read-only); train stage; one-shot holdout with a run marker.
- `checks.py`, `report.py`, `report_format.py`, `report_checks.py`, `cli*.py`: VST agreement, decision buckets, Indonesian report, CLI with exit codes 0/1/2.

Holdout lock as built:

- `<data_dir>/holdout_runs/<spec_sha>.json` is the run marker, written before the first holdout outcome is read; `.started.json` is its audit file.
- The lock binds the spec sha, the code sha (research package, `pine_indicators.py`, `bot/strategy/indicators.py`, `bot/arena/setup_profile.py`), the Python version, the seed, the latency sample hash, the training data hash and the training C0 result.
- The tick-time purge is derived from the calibrated latency: 60 + ceil((max latency + 5 s + one 1m bar) / 1 min) minutes.
- Calibration windows end at 2026-10-14 00:00 WIB.

## Verification

Measured on this branch (docs-only changes on top of main `9b189cf`), Python 3.11.15:

```text
Full test suite: Ran 1529 tests in 83.7s, OK
Line coverage: 89% (12,983 statements, 1,399 missed; required minimum: 80%)
compileall: passed
git diff --check: passed
secret hygiene test (tests.test_secret_hygiene): passed
pip check: not run (the interpreter used has no pip module)
```

Progress of the suite during development: 900 tests before Task 1, 1,429 at the first push of PR #31, 1,511 at merge.

## Commit and PR trail

- PR #31, merged as `151b9e8`: the research package, CLI, spec, tests (head before merge `c276805`). Core task commits include `6aff8d6` (indicators), `2b5a562` (spec), `d227b44` (bars), `7193c2b` (fetch), `390d824` (features), `6b11dd6` (rules), `ee48f23` (outcomes), `933c5bd` (simulate), `35727dc` (stats), `b2c5463` (selection), `6d537e0` (metalabel), `1dbd9e0` (calibrate), `6becda7` (train stage), `781d45f` (holdout), `04ef748` (checks), `2e4c125` (report), `b683141` (CLI). Fixes: `107e78d`, `70c67bc`, `6a42250`, `249954f`, `c6a22da`, `d01ae03`, `a347076`, `bbbb466`, final fix wave `d1c276b`, `24ce56b`, `4e30282`, `26a206c`, `a8022ff`, fingerprint `1f72232`, PR review wave 1 `9c3b06d`..`ab7b3a6`, wave 2 `36b5319`..`82b43fd`, follow-up `b8826ac`, `8b06e76`, `6b889dc`, `673b9be`, verdict sentence `c276805`.
- PR #32, merged as `79c9e14` (fix commit `3c73de4`): BingX fetch limits found on the VPS.
- PR #33, merged as `9b189cf` (fix commit `7d013bf`): tick-time purge derived from the calibrated latency.

## Review gates

- Per-task review (independent reviewer, fresh context, re-review after each fix round). Seven tasks needed a fix round: Task 1 (DMI TR seed), Task 2 (rule parameters and policies not in the hashed document), Task 5 (HTF warm-up seam), Task 10 (supply-limited pool admitted thin rules), Task 13 (lock accepted injected configs), Task 14 (one-shot holdout bypassable across days or seeds), Task 17 (vst-check and decisions could score holdout outcomes before `test`). Task 16 was approved with one Important (a vacuous NaN test), also fixed. All others were clean at first review.
- Whole-branch review (Task 17 step 6): a code review plus a security review (no CRITICAL or HIGH; 3 MEDIUM, 6 LOW, 2 INFO) and a Python review (3 HIGH, about 11 MEDIUM, clean on Python 3.10). The code review found 6 Important (code sha not in the chain, out-dir bypass of the one-shot, Holm family ignored, C0 only in test, SPA bias, bucket supply uncorrected). One fix wave over the consolidated list: re-review 29 of 29 addressed, one residual Important (code sha missed two imported bot modules), fixed in `1f72232` with an AST guard test.
- PR #31 review by the user's account (6 parallel reviewers): 2 HIGH (one-shot holdout only per folder plus open-ended calibration window; VST check passing with zero overlap), 5 MEDIUM, 20 LOW. Wave 1 fixed H1, H2, M1–M5, L4 (re-review clean). Wave 2 fixed the LOWs except L10–L12, which were documented as caveats (re-review clean). Follow-up review F1–F5: C grid, threshold scan, `.started` audit file, VST note fixed; the suite-runtime regression F5 was not reproducible (27–33 s plain) and is parked. Replies were posted on the PR threads.
- Ruling that changed pre-registered numbers before registration: the meta-model C grid was rescaled to a weighted-mean objective, then widened (F1). The spec was not registered at that time, so no pre-registration was broken.

## VPS run (2026-10-15 to 2026-10-16)

- Code synced to `~/replay/pine_gate_repo/src` from main via `git archive`; VPS Python 3.10.12. Bot, `.env`, pm2 and `~/replay/kline_keeper.py` were not touched.
- Pre-registration, 2026-10-15 20:47 WIB: `spec_sha256` `fd77d36af66167f071ec597efb9989d397c12b087202f7eab96b6f85aed0898c`, spec file `~/replay/pine_gate_spec_20261014.json`, code sha then `7131252f…`.
- Data (step 9): production 1m BTC and ETH 2025-08-01 to 2026-10-13, 439 days each (11 days needed a re-run after BingX in-body code 100410); warm-up 1h 4,344 and 4h 1,086 bars per symbol; native check window 2025-08-01 to 2025-09-01; funding 1,317 times per symbol.
- Two fixes found before any outcome was computed:
  - PR #32 (`79c9e14`): 100410 is "retry later", so back off 5/15/45 s; warm-up windows at most 1000 bars; funding windows at most 30 days (long BTC funding windows had been silently truncated at their start). The spec sha did not change.
  - PR #33 (`9b189cf`): the first `train` was refused by the purge check ("a trade can last 74.6 min from its tick, longer than the 65-minute purge"). Live latency sample: n 1,903, median 54 s, p90 106 s, p99 227 s, max 808.5 s. The purge is now derived from the calibration (75 minutes). Final code sha `e915262310aeea70e183db9b518d5a2bd5d2d33ca6eff0951b3cc559d272d0c5`. No outcome was computed in the refused run.
- `check-data` (22:54 wall, 412 MB peak): PASS days, funding cadence, truncation invariance at 500 real times. FAIL native-vs-aggregated 1h/4h agreement (BTC 98.92% and 95.70%, ETH 99.60% and 98.92%, threshold 99.9%). Deviation ruling: OHLC is identical in 100% of the overlapping bars; only volume differs, in 2–8 bars per series, worst relative difference 1.28e-4 against a tolerance of 1e-6. The 1h and 4h features read OHLC only, higher-timeframe volume is unused, and the volume profile uses 1m volume, so the aggregation is valid for every feature used. The 99.9% threshold is a code-level integrity check, not spec-hashed and not a gate. Training proceeded without a code change (the report states the deviation). Follow-up done after the holdout (see Follow-ups): `check-data` now gates OHLC at 99.9% agreement and volume only on a loose worst-difference bound (1e-3).
- `calibrate`: s* 1.0 bp (fallback: 13 rebuilt stops, below the 15 needed), 4 skipped, 1,903 latencies, written to `~/replay/out/pine_gate_calibration_20261016.json`. A collector cron line (`23 5 * * *`) was added; the existing six lines are unchanged.
- `train` (3:28 wall, 359 MB peak): selection file `~/replay/out/pine_gate_selection_20261016.json`, sha256 `3687376145f2f111c976ed78b50e25b7e58be07308615387a7d3cdcee7a9e310`. Contender 1 frozen as `H4|P40|ADX|OFF|OFF` (training WR 50.5%, lower bound 48.9%, gross -0.44 bps, 14.6 trades/day); the top five all sit near 50% WR; stable; PBO 0.014; SPA p 0.0005; not supply-limited. Contender 3 primary `H4|P40|OFF|OFF|OFF`, C 10, p* 0.4945. Training C0 passed.
- Pre-registration record posted on PR #31 before the test: https://github.com/zakyirsyaad/ai-perpetual-trading/pull/31#issuecomment-6203188477
- `test` (one run, 2:02 wall, 383 MB peak; run marker and `.started` file written): holdout file `~/replay/out/pine_gate_holdout_20261016.json`, sha256 `3459341410e5694a455594304baecf5966a7d362c298a4845069116f29c05702`. Nothing passed; chosen contender is None.

| Contender | Symbol | WR | Lower bound | Gross bps | Net per 1M | Trades per day |
|---|---|---|---|---|---|---|
| 1 | BTC | 48.1% | 42.9 | -0.98 | -399 | 9.0 |
| 1 | ETH | 52.2% | 44.2 | +0.26 | -337 | 3.8 |
| 2 | BTC | 47.6% | 44.2 | -1.58 | -429 | 13.0 |
| 2 | ETH | 50.7% | 45.6 | -0.33 | -366 | 6.2 |
| 3 | BTC | 47.6% | 42.3 | -1.40 | -420 | 8.1 |
| 3 | ETH | 56.8% | 47.4 | +2.05 | -247 | 3.9 |

  The C0 controls behaved normally (WR 47.7–50.5%, gross -0.5 to -1.5 bps), so the fill model is valid.
- `vst-check` and `decisions` were skipped: spec sections 5.12 and 5.13 apply to the chosen contender and none was chosen. The ETH VST check (plan step 14) is moot.
- `report`: `~/replay/out/pine_gate_edge_20261016.md` and `.json` on the VPS; the Markdown copy is `docs/research/2026-10-16-pine-gate-phase1-report.md`.
- Bot untouched: pm2 `ai-perp-volume` restarts were 25 before and after, with 0 ERROR lines.

## Final verdict

No contender passed the staged gate. Out-of-sample gross edge is between about -1.6 and +2.1 bps against a break-even near 7 bps round-trip fee, and the best net result (contender 3 on ETH) is -247 per 1M versus the live bot's -190 reference. The bot is unchanged and the current K2 configuration continues. Contender 1's frozen parameters are recorded in `docs/tradingview/pine_adapted_gate_v2.pine` as reference defaults only; they failed the holdout.

## Deployment gate

Nothing was deployed to the bot. The results branch (PR #34) changed documentation, the TradingView helper script defaults and this evidence file only; no Python code and no `.env` value changed. The later Follow-ups (PR #36) change research code and carry their own deployment gate below. The research package ran on the VPS from a separate directory (`~/replay/pine_gate_repo/src`) under `nice -n 19`, and the pm2 app `ai-perp-volume` was neither restarted nor reconfigured. The only persistent VPS change was the daily collector cron line, which wrote public market data to `~/replay/pine_gate/data/`; it was removed on 2026-10-16 after the verdict (see Follow-ups). Any Phase 2 work (wiring a gate into `AIOrchestrator`) requires a contender that passes a new pre-registered holdout and a fresh decision from the user.

## Follow-ups (2026-10-16, after the verdict)

### Collector cron removed

Nothing passed, so the forward VST ETH data the collector gathered for the section 5.12 check is no longer needed. The `23 5 * * *` `research.pine_gate.cli collect` line and its comment were removed from the VPS crontab; the remaining six lines are byte-identical to the backup taken before the line was added (`diff` against `~/replay/pine_gate/crontab.before` is empty), so the supervisor and `kline_keeper.py` lines are unchanged. The crontab with the collector is kept as `~/replay/pine_gate/crontab.with-collector-<timestamp>`. The data already collected stays in `~/replay/pine_gate/data/`.

### `check-data`: OHLC gates strictly, volume gates loosely

User journey: an operator runs `check-data` on complete data and gets FAIL on all four native-vs-aggregated lines, although the 1h/4h features read only OHLC and OHLC agrees in 100% of the bars. The step-9 ruling above had to overrule the tool by hand.

Round 1 (commit a9c0091), RED first: `HtfAgreementTest` expected `htf_agreement` to return an `HtfAgreement` with `bars`, `ohlc_share`, `volume_share` and `worst_volume_rel`, including the VPS case (OHLC identical, one bar's volume off by 1.28e-4); `CheckDataCommandTest` expected a gating OHLC line plus a volume line. Result: `FAILED (failures=6, errors=4)` (`AttributeError: 'float' object has no attribute 'ohlc_share'`; `AssertionError: False is not true : FAIL native vs aggregated prod/BTC-USDT/1h: 97.9167% of 48 bars agree (need >= 99.9%)`). GREEN made volume an INFO line that never failed. One test expectation was corrected during GREEN: the relative difference is taken against the larger value, so a 1.28e-4 increase reads as 1.28e-4 / (1 + 1.28e-4).

Round 2 (code review of PR #36): an ungated volume line removed the only cross-check of the 1m volume that the 24h POC reads, so a 1m day file with zeroed or doubled volume and correct OHLC would have passed. RED first: a gross volume difference (x2 in one bar) must FAIL, the 1.28e-4 VPS case must PASS, a NaN worst difference must FAIL, a NaN volume must make `worst_volume_rel` NaN wherever it sits (Python's `max` skips a NaN after a finite value), and `check-data`'s exit code must follow the native lines with every other check patched to PASS. Result: `FAILED (failures=7)`.

GREEN:
- `features.py`: frozen `HtfAgreement`; `_ohlc_agrees` (price tolerance 1e-9 unchanged); `_relative_difference` (the measure `math.isclose` uses); the 1e-6 volume share is kept for information; `worst_volume_rel` is NaN when any pair's is.
- `cli_data.native_check` returns `(lines, ok)` with two gating lines: OHLC share >= 99.9% (unchanged) and worst volume relative difference <= 1e-3 (NaN fails). Every missing-file, missing-day and no-overlap case still FAILs; the duplicate set-intersection overlap check is gone.
- `docs/superpowers/plans/2026-10-14-pine-gate-phase1.md`: the Interfaces line notes the new return type.

Round 3 (second code review of PR #36): the native file was read outside the `try`, so one invalid native row aborted the whole `check-data` run with a single `error:` line; the widened `except ValueError` reported a corrupt 1m day as an OHLC disagreement; the volume cross-check covers only the native-check window (2025-08, about 31 of 439 days); and the two volume tolerances lived in different modules. RED first: a native file with high below low must give one `FAIL native check ... unreadable` line while `check-data` still runs its later checks; a non-gzip 1m day must read `1m data unreadable`, not OHLC; both native lines must name the WIB days they cover; `days_check` must list a day whose 1m volume is all zero; `htf_agreement` must raise a dedicated `NoOverlapError` (a `ValueError` subclass) and keep a plain `ValueError` for an interval mismatch; `VOLUME_REL_TOL` and `VOLUME_WORST_REL_MAX` must sit together in `features.py`. Result: `FAILED (failures=3, errors=2)` (`ValueError: bar at 1753981200000: high/low do not bracket open and close` escaping `native_check`; the `features` test module failing to import the new names).

GREEN:
- `native_check`: the native read is guarded (`... unreadable (...)`); `MissingDayError`, other `ValueError`s from `load_days` (`1m data unreadable`) and `NoOverlapError` each get their own message under the `native check` label; nothing else is caught, so a bug in `aggregate` still surfaces as a traceback. Both result lines carry `over <first>..<last>`. The docstring states the window and how to widen it (`native-check-fetch --start/--end` into a fresh file).
- `days_check` reads each day file once (`_day_problems`) and also lists days whose total 1m volume is zero, so a zeroed volume column is caught on every day, not only inside the native window. A doubled volume outside the window is still caught only by widening that window.
- `features.py`: `NoOverlapError`; `VOLUME_WORST_REL_MAX = 1e-3` next to `VOLUME_REL_TOL`; `cli_data.NATIVE_VOLUME_MAX_REL` removed. The observed worst difference (1.28e-4) is recorded here rather than in a code comment.

Round 4 (third code review of PR #36): the native window was cut at the training start but not at the last holdout day, so a native file fetched past the stored 1m data (as the docstring now suggests) failed on missing 1m days instead of comparing over the overlap; `days_check` read rows without the loader's validation, so a day with a NaN volume or high below low passed and only crashed `train` later (and a NaN volume also slipped past the zero-volume sum); only an all-zero day was flagged, not a partly zeroed one; and `_day_problems` returned an unnamed pair of booleans. Before choosing a threshold, the stored production data was measured on the VPS: 441 day files per symbol hold no zero-volume minute at all. RED first: a native file running one contiguous day past a patched holdout end must PASS over the overlap; `days_check` must list a day with a NaN volume or high below low as `invalid bars` (and not as zero volume), list a day whose last six hours have zero volume, and tolerate a 59-minute zero run. Result: `FAILED (failures=5)` (e.g. `FAIL native check ...: missing prod/BTC-USDT/1m day files: 2025-08-03`).

GREEN:
- `native_check`: `last` is capped at the last holdout day, like `days_check`'s range.
- `days_check`: `_day_problem` returns one key of `_DAY_PROBLEMS` (`shape`, `bars`, `zero_volume`) or `None`; it runs `bars_from_rows` on the rows it already read, and flags a run of at least `ZERO_VOLUME_RUN_MAX` = 60 zero-volume minutes (which also covers an all-zero day). One line per kind, as before.

Round 5 (fourth code review of PR #36): an unreadable day file was still reported as "not a whole WIB day"; `ZERO_VOLUME_RUN_MAX = 60` failed at 60, so the name read as the largest run allowed; a zero-volume run crossing WIB midnight was split between two files and escaped the limit; and `days_check` built a full `Bars` per day only to validate it. RED first: a non-gzip day must be listed as `unreadable`; `ZERO_VOLUME_RUN_LIMIT` must be 60; 45 zero minutes either side of midnight must flag the second day; `bars.check_rows` must raise what `bars_from_rows` raises. Result: `FAILED (failures=2, errors=2)`. During GREEN two existing tests caught a bug in the first version: a run that ended exactly at the previous day's last minute flagged the next day, which has no zero volume; a carried-in run now counts only if it continues into the day.

GREEN:
- `bars.py`: `_validated` is the one place that checks spacing and `_check_bar`; `bars_from_rows` builds from it and `check_rows` only consumes it.
- `days_check` walks each symbol's days in order, carrying the open zero-volume run across midnight (a missing or broken day resets it); `_day_verdict` returns a `_DayVerdict(problem, zero_run_after)`; problem kinds are `unreadable`, `shape`, `bars`, `zero_volume`; the limit is `ZERO_VOLUME_RUN_LIMIT` (a run this long fails).

Round 6 (fifth code review of PR #36): `days_check` re-implemented the per-day reading of `load_days`, so the two could drift; nothing tested that a missing or broken day resets the carried zero-volume run; a run crossing midnight was listed only on the later day, which may hold just a few zero minutes; problem kinds were bare strings; and the commit trail ended without a hash. RED first: `bars.read_valid_day` must return a valid day's rows and raise `UnreadableDayError` / `DayShapeError` / `BarGapError` / `ValueError` by cause, and `load_days` and `days_check` must both go through it (patched in the tests); `read_day` must raise `UnreadableDayError`, a `ValueError` subclass; a run of 45 + 45 or 50 + 10 minutes across midnight must list both days; `DayProblem` must be an Enum in print order. Result: `FAILED (failures=2, errors=3)` (`ImportError: cannot import name 'DayProblem'`; `... does not have the attribute 'read_valid_day'`; the `bars` test module failing to import). The two reset tests (a missing day, a short day between two zero runs) passed on the round-5 code, as intended: they pin behaviour that was right but untested.

GREEN:
- `bars.py`: `UnreadableDayError(ValueError)` from `read_day`; `read_valid_day(path, day, interval_ms)` reads, checks the day's shape and runs `check_rows`; `load_days` builds from it, and its final `bars_from_rows` still checks that the days join.
- `days_check` / `_symbol_day_problems`: one `read_valid_day` call per day classified by exception type into `DayProblem` (`UNREADABLE`, `SHAPE`, `BARS`, `ZERO_VOLUME`); `_zero_volume_runs` reports how far a carried-in run grew, and a run that reaches the limit after crossing midnight charges every day it touches.
- `CLAUDE.md`: the "split is still to do" note now describes the checks as they are.

Verification on the real VPS data (branch code copied to a temporary directory, run under `nice -n 19`, directory removed afterwards; round 6 run: `days_check` PASS over 2025-08-01..2026-10-13 in 6 s, native lines unchanged, and `load_days` of the BTC training range (531,360 bars) in 3.2 s with the per-day validation; round 5 run: `days_check` PASS over 2025-08-01..2026-10-13 in 4 s, native lines unchanged; round 4 run: `days_check` PASS over 2025-08-01..2026-10-13 in 5 s, native lines unchanged; round 3 run: `days_check` PASS over 2025-08-01..2026-10-13 with no zero-volume day, 3 s; the native lines cover 2025-08-01..2025-08-31 for 1h and ..2025-09-01 for 4h):

| Series | OHLC (>= 99.9%) | Volume worst difference (<= 1e-3) | Volume within 1e-6 (info) |
|---|---|---|---|
| BTC 1h | PASS 100.0000% of 744 | PASS 1.28e-04 | 98.9247% |
| BTC 4h | PASS 100.0000% of 186 | PASS 3.62e-05 | 95.6989% |
| ETH 1h | PASS 100.0000% of 744 | PASS 6.58e-05 | 99.5968% |
| ETH 4h | PASS 100.0000% of 186 | PASS 9.85e-06 | 98.9247% |

The volume shares match the figures behind the step-9 ruling exactly; the worst difference (1.28e-4) is eight times inside the 1e-3 bound.

Local (round 6): full suite 1,573 tests OK at 89% total line coverage (`bars.py` 97%, `features.py` 99%, `cli_data.py` 96%); this run took 431 s on a loaded machine. Round 5: full suite 1,565 tests OK at 89% total line coverage (`bars.py` 97%, `features.py` 99%, `cli_data.py` 96%). Round 4: full suite 1,562 tests OK at 89% total line coverage (`features.py` 99%, `cli_data.py` 95%). Round 3: full suite 1,557 tests OK at 89% total line coverage (`features.py` 99%, `cli_data.py` 94%); one test for the `NoOverlapError` path in `native_check` was added afterwards and passes with its module (`tests.test_pine_gate_cli`). Round 2: 1,551 tests OK, 89%. Both rounds: `compileall` clean, `git diff --check` clean, secret hygiene OK (`tests.test_secret_hygiene`), `pip check` clean (`uv pip check` locally: all 15 packages compatible; the VPS venv: no broken requirements). One round-2 full run stalled past 600 s inside the telemetry `MarketDataPoller` tests (untouched here); the rerun finished in 146 s.

Lock note: `bars.py`, `features.py` and `cli_data.py` are inside the code fingerprint, so `code_sha256` changes and the frozen selection file no longer matches current code. This is expected and harmless: the holdout already ran once and its run marker refuses any second test regardless. A future study needs its own pre-registration.

Commit trail (branch `fix/pine-gate-check-data-volume-info`, PR #36):
- a9c0091 `fix: gate check-data on native OHLC agreement, report volume as info` (round 1, plus the collector-cron record)
- b5e6c91 `fix: bound native volume difference in check-data instead of ignoring it` (round 2)
- 3fee2ec `fix: keep check-data running on unreadable native or 1m files` (round 3)
- 7df1ac0 `fix: validate every stored 1m day in check-data and cap the native window` (round 4)
- a2fd0f5 `fix: carry zero-volume runs across midnight and name unreadable day files` (round 5)
- round 6: `fix: share day validation between load_days and check-data` (its hash is in `git log` on the branch; a squash merge folds all six into one)

Deployment gate (PR #36): research code only (`src/research/pine_gate/bars.py`, `features.py`, `cli_data.py`, their tests and docs); `bot` does not import `research`, so nothing is deployed and pm2 `ai-perp-volume` needs no restart. After merge, the VPS research copy `~/replay/pine_gate_repo/src` must be resynced from main before `check-data` runs there with these checks; until then it runs the Phase 1 code that produced the recorded results.
