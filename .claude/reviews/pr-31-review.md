# PR Review: #31 — Pine-adapted gate Phase 1: pre-registered offline evaluation (research only)

**Reviewed**: 2026-10-14
**Author**: zakyirsyaad
**Branch**: feat/pine-adapted-gate → main (head `1f72232`)
**Decision**: COMMENT (draft PR). Fix the 2 HIGH findings and decide on the methodology MEDIUMs before the VPS run.

## Summary

The engineering is careful, and most of the research holds up under adversarial checks. The following all verified:

- the execution model, guards and purge;
- Pine parity and look-ahead safety;
- the statistics core and metalabel cross-validation;
- the gate's fail-closed behaviour and fetch hygiene.

Two HIGH findings weaken the claims the research rests on:

- The one-shot holdout can be repeated by re-running the pipeline into a new folder.
- The VST-vs-production check can pass with no signal overlap.

Several MEDIUM findings are methodology choices. Because the run is one-shot, they can only be changed before it.

## Read first: run order

`code_sha256` covers all 31 research files, including `report.py`, `checks.py` and every CLI module (`lock.py:220-232`). `test` refuses when the code differs from the code that trained (`holdout.py:527-530`). So every fix below, and `verdict_sentence`, must land before `train` runs on the VPS. An edit after `train` costs a full re-train.

Suggested order:
1. Land the fixes.
2. Write `verdict_sentence`.
3. Run `spec` to pre-register the sha, and commit it.
4. Run Task 17.

## Findings

### CRITICAL

None.

### HIGH

**H1. "The holdout runs once" holds only per folder, and the calibration windows have no end date**

`holdout.py:511-545`, `holdout.py:628`, `calibrate.py:152`, `calibrate.py:252`

- **The folder check:** `check_ready` refuses only when a `pine_gate_holdout_*.json` sits in `out_dir` or in the (unresolved) folder of the selection path. Nothing ties the run to the spec or selection sha, and nothing is written before evaluation starts.
- **The calibration window:** both calibration queries are `gte.<since>` with no upper bound. Every `calibrate` run therefore returns a different latency sample, and possibly a different s\*.
- **Scenario:** re-run the documented chain `calibrate → train → test` into a fresh `--out-dir` on a later day. It passes every check and gives a second, different, fully "locked" holdout result. Copying or symlinking the selection file into a new folder does the same (confirmed by running `check_ready`). An operator can end up with two holdout reads and keep the better one, even without meaning to.
- **Fix:**
  1. Give both calibration windows a pre-registered end (for example `HOLDOUT_END`) in `FillConfig`, so `calibrate` is reproducible.
  2. Before reading any holdout data, write an exclusive marker keyed by the spec sha in a fixed place (for example `<data_dir>/holdout_runs/<spec_sha>.json`), and refuse when it exists.
  3. Resolve the selection path before taking its parent.
  4. Commit the spec sha and the selection sha before running `test`. No local lock can stop a second machine.

**H2. The VST-vs-production check can pass when VST and production never signal on the same tick**

`checks.py:114-149`

- **Defect:** `same_side` counts every tick, so ticks where both sides say "no signal" dominate the 97% bar. Outcomes and both win rates are computed only at production's signal ticks, using production's side. VST's own signals never reach the outcome or win-rate numbers, so the WR comparison mostly re-measures price-path agreement.
- **Scenario (confirmed by running):** production signals at tick 10 and VST at tick 50 give `side_agree=0.98, outcome_agree=1.0, passed=True`. At realistic signal rates of 1–3% of ticks, the bar is met with zero overlap. Spec 5.12 makes this check the gate for ETH moving to enforce.
- **Fix:** the code follows the spec's wording, so the fix is to the definition. Update spec 5.12 to match.
  1. Require agreement on ticks where either side signals (both the same ÷ either non-zero, at least 97%).
  2. Compute VST outcomes and win rate at VST's own signal ticks, with VST's side.
  3. Report both numbers.

### MEDIUM

**M1. A block with few exit days gets a near-zero clustered SE, so a lucky rule can rank first**

`selection.py:98`, `selection.py:124-132`, `stats.py:99-116`

- **Defect:** with few informative days, the day-clustered SE collapses.
  - Two all-TP days give a block lower bound of 100.0.
  - Three days of 3 TP / 1 SL give 75.0 (SE = 0).
  - One thin block barely moves the median of four (53.0 → 53.3). Two thin blocks move it to about 77 and pass the 3-of-4 vote.
- **Impact:** this needs a rule that is nearly silent in two blocks, which is most plausible under the supply-limited fallback. It costs power rather than creating a false holdout pass: contender 1 is picked wrongly, and contender 3's primary rule follows it. Review Focus 5 tests only rules that are thin overall.
- **Fix:** count a block with fewer than about 60 TP+SL trades or about 10 exit days as failing (no WR vote, lower bound −inf). Alternatively, floor the SE at the binomial SE.

**M2. PBO measures a different selector from the one used, and thin rules inflate it**

`selection.py:208`, `stats.py:249-250`

- **Defect:** CSCV picks the in-sample winner by raw pooled WR among all 61 evaluated rules, ineligible ones included. The real selection uses eligibility and block lower bounds. Thin rules win in-sample by noise and then rank low out of sample.
- **Scenario (confirmed by running):**
  - 60 rules, one with a true 56% edge: PBO 0.19.
  - Adding 3 null rules of about 32 trades: 0.47.
  - Adding 5 null rules of 120 trades: 0.14–0.64 across seeds, above 0.5 in 3 of 5.
- **Impact:** this is conservative, but the run is one-shot. Contender 1, and contender 3 (`holdout_gate.py:25`), can fail criterion 9 on a real edge.
- **Fix:** run CSCV only over rules passing `_sample_ok`, or pick the in-sample winner by the same lower-bound ranking. Record the change in the spec and plan; the plan currently says "pooled WR".

**M3. Contender 3 silently disappears when every Stage-1 rule ranks −inf**

`pipeline.py:517-528`

- **Defect:** spec 5.8 says "If contender 1 has no eligible rule, the best-ranked Stage-1 base rule is used." When no rule passes the 3-of-4 vote, `_primary_rule` returns `None` and contender 3 is skipped (counted as p = 1). The test `test_no_ranked_rule_means_no_primary_and_no_contender3` encodes this, but the ruling is in neither the spec nor `_policies()`.
- **Why it is plausible:** both-touch bars count as stops, so no-edge block WRs sit just under 50%, and the 36 rules are correlated. That is exactly the case where meta-labeling is supposed to add something.
- **Fix:** fall back to a total order: the median block lower bound without the vote, then pooled WR, then the simpler rule. Or pre-register the ruling.

**M4. The meta-model threshold is validated on fold models but applied to a differently regularised refit**

`metalabel.py:173-177`, `metalabel.py:458-459`

- **Defect:** the loss is a sum, `Σ w·logloss + |β|²/(2C)`, so a fixed C regularises the B1-only fold much more than the B1–B4 refit. p\* is chosen to give at least 10 trades/day on out-of-fold probabilities, then applied to the refit's more spread-out probabilities.
- **Measured:** with C = 0.01 and uniqueness weights of about 0.1, the share of candidates with p ≥ 0.55 was 3.5% at the fold-1 size and 17.2% after the full refit. Contender 3's holdout supply and selectivity are therefore not the validated ones (criteria 6 and 8).
- **Fix:** divide the data term by Σw, so C means the same at every sample size. Or re-check p\* against the final model's training supply (this uses no labels) and record it.

**M5. The report's VST and decisions sections are not tied to the contender the holdout chose**

`cli_stages.py:132-150`, `cli_checks.py:206-227`, `report.py:628-669`

- **Defect:** the VST output records no contender and no selection sha. `gather_results` keeps only `buckets` from the decisions file and drops its `contender`. The newest files are attached by name.
- **Scenario:** an exploratory `vst-check --contender c1` while the holdout chose c3 is shown under the c3 verdict with no label. Spec 5.13 requires "the chosen contender".
- **Fix:**
  1. Write `contender` and `selection_sha256` into both outputs.
  2. In `gather_results`, refuse or warn when they differ from `holdout.chosen` or the selection's sha.
  3. Show the contender in both tables.

### LOW

**Pre-registration hardening**

- **L1** (`lock.py:199-208`, `pipeline.py:673`): the seed is not in `spec_document()`. Calling `train_stage(seed=k)` through the Python API writes a valid locked selection for any k. Add `"seed": 0` to the spec document and refuse any other seed.
- **L2** (`cli_checks.py:72-93`): `vst-check` and `decisions` unlock with a hand-written holdout stub, because `selection_sha256` is optional and neither the code sha nor the folder is checked. Require both.
- **L3:** the Python version is not recorded. Python 3.12+ changed float `sum()`, which can move last-bit ties between machines (today the VPS has 3.10 and the Mac and CI have 3.11). Record `sys.version_info[:2]` in the selection and holdout files.
- **L4:** the spec text and the code disagree in three places. Sync the markdown.
  - Criterion 9's PBO applies to c1 and c3 in code (stricter, commented at `holdout_gate.py:23-25`), but only to c1 in spec 5.11.
  - The funding blackout is "around" funding in spec 5.10, but 0–600 s before it in code (the same as the live bot).
  - Criterion 2's C0 baseline pools the long and short day counts instead of averaging the two win rates (`holdout.py:172-180`). This is conservative.

**Data integrity**

- **L5** (`fetch.py:480-510`, `pipeline.py:178-186`): funding history is never checked for cadence or coverage, so a short list is stored as valid and never refreshed. Confirmed: a venue capped at 100 rows stored 100 of about 1,201 times. Reject gaps larger than the funding interval, and ends far from the requested bounds. Add this to `check-data`.
- **L6** (`bars.py:320-347`): `load_days` checks only that files exist and join, not that each file covers its own WIB day, and `days_check` checks existence only. Confirmed with a mis-named file. Assert the first and last bar bounds, or 1,440 rows per file.
- **L7** (`features.py:219-226`): a warm-up that starts at or after the first aggregated bar is silently ignored, so H4 EMA50 stays `None` for about 66 days. Raise, or record the warm-up as missing, when `keep == 0`.
- **L8:** two fetch gaps.
  - `fetch.py:333-385`: `fetch --end <today>` late in the day can store a last bar that is still forming. Refuse windows ending after now − 2 minutes.
  - `fetch.py:200-210`: an in-body rate-limit code (100410, still to verify) becomes an ordinary `ExchangeError`, so the job continues. Map it to `RateLimitedError`.

**Simulation fidelity (each negligible)**

- **L9** (`simulate.py:170`): an empty latency sample silently means zero latency, and nothing refuses `latencies_ms == []`.
- **L10** (`pipeline.py:107-110`): the 8 CSCV boundaries are not purged, leaving at most about 7 straddling trades per rule.
- **L11:** two timing differences from the live bot.
  - `simulate.py:154-156`: the funding blackout is checked at the tick; live checks it after the AI call, at about t + latency.
  - `simulate.py:131-138`: daily guards count on the entry day; live counts on `closed_at`.
- **L12** (`cli_checks.py:284-303`): decision ticks may be placed late, because latency is timed after the context build. This slightly favours buckets B, E and F over A. Medium confidence.

**Calibration and statistics**

- **L13** (`calibrate.py:190-191`): s\* rebuilds stops with a fixed 1.5 × ATR and a 0.4% cap. If the live `MAX_STOP_LOSS_PCT` or `ATR_SL_MULTIPLIER` differed since 2026-09-12, s\* is biased; an s\* that is too small inflates WR. Confirm the live history, record it in the calibration file, and fail loudly when skipped stops outnumber rebuilt ones.
- **L14** (`stats.py:99`, `stats.py:152`): the clustered SE and the day bootstrap treat days as independent, but trend regimes last weeks. Report criterion 2 under a 5-day block bootstrap as a sensitivity. Criterion 7's day-shift permutation already mitigates this.

**Report, CLI and tests**

- **L15** (`report.py:88-121`): one-decimal rounding shows failing values at the threshold. 56.96% renders as "57.0% | gagal: 3", and a side agreement of 0.9696 renders as "97.0%". Use two decimals near thresholds.
- **L16** (`tests/test_pine_gate_report.py:597`): `VerdictSentenceTests` is skipped unconditionally, so its contract tests stay off after `verdict_sentence` is written. Skip only while it raises `NotImplementedError`.
- **L17** (`cli_stages.py:95-106`): explicit `--vst` paths with non-standard names map to the symbol `''` and overwrite each other (confirmed). Read the symbol from the file content, or reject the name.
- **L18** (`report.py:287-353`): a spec or code mismatch with the holdout shows up only as a "TIDAK COCOK" row in section 2. Add a warning at the top, like the C0 one.
- **L19** (`cli.py:56-57`): `--symbols ","` parses to `[]`, and `fetch` then exits 0 having done nothing.
- **L20** (`tests/test_pine_gate_structure.py:119-144`): the isolation guard misses `importlib` and `__import__`. It scans only `src/bot` and `main.py`, not `supervisor.py`, and has no check that nothing imports `pine_indicators`.
- **Test gap:** no test pins that the entry bar's own high and low are ignored, because every fixture's entry bar is flat.

**Notes (not defects)**

- E200 is `None` until about 2025-08-06, because there is no 5m data before training starts. E200 rules lose about 6% of B1 and still pay for those days in the supply denominator. State this in the report.
- Two edge cases differ from Pine: RSI when both averages are 0, and DI when the smoothed true range is 0. Neither is reachable on real prices.
- The POC bin grid is anchored at multiples of price × 5 bps, so it moves with price. Worth one sentence in the report.
- Spec 5.14 asks for both training stages, but the report tables only the Stage-1 top 5.

## Validation Results

| Check | Result |
|---|---|
| `compileall src tests` | Pass |
| Tests (`unittest discover`, 1,431) | Pass, 3 skipped by design |
| Coverage | 89% (gate 80%) |
| `git diff --check main...HEAD` | Pass |
| GitHub CI (`test`) | Pass |
| Type check / lint | Skipped (none configured) |

## Verified OK

- **Execution model (spec 5.10):** the tick clock; latency drawn only on entry ticks; order time = tick + latency + 5 s; entry at the close of the containing 1m bar + 0.25 bps; bracket set from the fill; walk from the next bar; gaps filled at the open; a both-touch bar counts as a stop; time stop at bar 60; fees; LONG/SHORT signs.
- **Guards:** they roll at WIB midnight. The volume cap (50k per round trip), drawdown and consecutive-loss rules match `risk_manager.py`, and the funding blackout matches `trade_engine.py`.
- **Purge:** `check_purge_covers` guarantees no training trade crosses a B boundary, and nothing reads bars past the training cut.
- **Pine v5 parity:** an independent bar-by-bar replica of RMA/EMA/RSI/ATR/DMI/percentrank matches to about 1e-14. Fuzzing every bar not yet closed leaves the snapshot and POC unchanged (0 leaks in 60 times).
- **Contender 2** mirrors the live near-mean gate (39 closed 3m bars, the same indicators).
- **Statistics:** Holm, Hansen SPA, the PBO math, the paired day bootstrap and the metalabel CV are correct. The CV has no label leakage, the scaler and weights are fitted on the training fold, and the embargo is applied.
- **Gate:** criteria 1–9 match spec 5.11 and fail closed on NaN/None and on zero trades. c1 and c3 must beat c2.
- **Integrity tooling:** `spec_sha256` is canonical and stable, and `write_exclusive` is atomic and never overwrites.
- **Fetching:** only the two public hosts and paths, with `X-SOURCE-KEY`, no keys and no redirects. Requests are at least 1 s apart, including retries, and use fixed windows. A day must be exactly 1,440 contiguous bars.
- **Supabase:** GET-only through the bot store, constant select paths and stable paging. Secrets are never printed (`call_live`).
- **Report and CLI:** report numbers come from the right fields in the right units, CLI exit codes 0/1/2 behave as documented, and symbols are validated, which rules out path traversal.

## Files Reviewed

All 52 files are added (A):

- **Source (29):** `src/bot/strategy/pine_indicators.py`, `src/research/__init__.py`, and 27 modules in `src/research/pine_gate/`.
- **Tests (19):** `tests/test_pine_indicators.py` and 18 `tests/test_pine_gate_*.py` modules.
- **Docs (4):** the design spec, the Phase 1 plan, the evidence review, and `docs/tradingview/pine_adapted_gate_v2.pine`.
