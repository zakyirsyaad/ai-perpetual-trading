# Live Staged Fixes — TDD Evidence

Spec `docs/superpowers/specs/2026-10-21-live-bot-staged-fixes-design.md`, plan
`docs/superpowers/plans/2026-10-21-live-bot-staged-fixes.md`, branch `feat/live-staged-fixes`
(base `95e61b7`, the merge of PR #44).

## User journey

The operator of account 1 (pm2 `ai-perp-volume`, the live SHORT bot) wants it to lose less, to
trade in both directions where the market supports it, and to learn from its own mistakes. The
operator ruled that **forward results, not backtests, decide**, so the changes go in stages and each
stage is measured before the next. Three things are built, each behind a setting whose default is
today's behaviour:

- **Part A, honest AI entry inputs** (`AI_ENTRY_PAYLOAD=lean`): the model is told the rules it is
  judged by (the stretch limit, the sides the gate allows, the regime), gets features computed in
  code instead of the broken factor library, and is no longer told to trade every tick.
- **Part B, a two-way regime gate** (`ENTRY_REGIME_GATE=map`): SHORT only in TURUN, LONG only in
  NAIK, with a side that may run in **shadow** (decided, scored later against venue candles, never
  traded).
- **Part C, a binding setup ledger** (`SETUP_LEDGER_MODE`): each setup bucket
  (`side|condition|atr_regime`) is judged on its own real and shadow outcomes; in `enforce` a bucket
  whose upper bound cannot reach the hurdle is vetoed in code, and earns its way back through shadow
  outcomes.

Stage 0 is already live: AI exits are off on account 1 since 2026-10-21 00:18:55 WIB
(`AI_EXIT_MAX_CALLS_PER_TRADE=0`, restart while flat). Merging this branch changes nothing on
account 1; the forward bot (account 2) runs a pinned tag and is not redeployed.

## Evidence the design rests on

Forward analysis of every real trade of account 1 (519 trades, 31 Aug – 20 Oct 2026) and 1,084 AI
entry decisions. Gross, in bps of notional per trade; at 0.035 % per leg, net per 1M = 50 x gross
- 350, so -150 per 1M needs +4 bps.

| Bucket (BTC) | n | Gross per trade |
|---|---|---|
| AI LONG, near the mean | 101 | -4.7 +- 2.3 |
| AI LONG, chasing (> 0.75 ATR) | 82 | -7.0 +- 2.7 |
| AI SHORT, chasing | 93 | -8.0 +- 2.4 |
| AI SHORT, near the mean | 175 | +2.0 +- 1.6 |
| AI SHORT in TURUN (October) | 58 | +6.1 +- 3.1 |
| Rules-only exits vs actual, same entries | 519 | +2.1 +- 1.0 better |

The code review found the model was judged by rules it never saw: a prompt that allows HOLD beside a
payload that says "do not sit out", `allowed_actions` always listing both sides, the stretch gate
never mentioned, 81 of 86 arena factors reducing to one formula per category (every volatility
factor votes LONG at BTC's ATR), and lessons that were advisory text only.

## RED / GREEN per task

Every task followed RED (tests first, run, watched fail), then the minimal implementation, then the
full suite. Commands are `PYTHONPATH=src .venv/bin/python -m unittest ...`. Review fix rounds are
listed under their task.

### Task 1 — regime gate `map` and a richer verdict (`079bc6b`)

RED, `tests.test_regime_gate`:

```
ValueError: regime gate mode must be one of ['naik', 'off'], got 'map'
AttributeError: 'RegimeVerdict' object has no attribute 'allowed_direction'
Ran 53 tests ... FAILED (errors=28)
```

GREEN: 53 tests OK; full suite 2673. NAIK -> `long`, TURUN -> `short`, SIDEWAYS and TIDAK_ENTRY ->
no side; reasons `naik`, `turun`, `not_mapped:<K>`; the volatility guard blocks both sides;
`history_short`, `data_error` and an empty answer map no side. `RegimeVerdict` gained
`allowed_direction`, `sma20_1h`, `sma50_1h`, `close_4h`, `ema50_4h`, appended with defaults. The
selector numbers are compared with `HourlyFrame.context_at`, the backtest's own maths.

### Task 2 — settings for map, the lean payload and the ledger (`23c8fc7`)

RED, new `tests.test_live_staged_settings`: `Ran 14 tests ... FAILED (failures=9, errors=12)`, with
`Value error, ENTRY_REGIME_GATE must be one of: off, naik. [input_value='map']` and
`AttributeError` on the new fields. `test_naik_still_demands_long` passes in RED on purpose: it pins
the old message byte for byte.

GREEN: 95 tests (this module, `test_regime_gate`, `test_build_forward_env`); full suite 2687.
`AI_ENTRY_PAYLOAD`, `SETUP_LEDGER_MODE`, `SETUP_LEDGER_SINCE` (timezone-aware, normalised to UTC),
`SETUP_LEDGER_MIN_SAMPLES` (60, `ge=2`), `SETUP_LEDGER_HURDLE_BPS` (4.0). `map` demands the enforcing
stretch gate and an AI strategy and accepts any `ENTRY_GATE_DIRECTION`; a ledger mode other than `off`
demands a `naik`/`map` gate and `SETUP_LEDGER_SINCE`.

### Task 3 — side mapping, shadow marking and the bucket on the trade (`84fc369`)

RED: `Ran 18 tests ... FAILED (failures=5, errors=14)`: `ModuleNotFoundError: No module named
'bot.core.setup_ledger'`, `AttributeError: ... no attribute '_mapped_side'`, `KeyError: 'kondisi'`,
`'mapped_side'`, `'shadow_side'`.

GREEN: 75 tests (orchestration, pipeline, ledger); full suite 2705. Under `map` the stretch verdict
carries `mapped_side` and `shadow_side`; a mapped side the direction filter excludes is marked
shadow, any other side is a `direction` block with `forced_by=regime_gate`; `naik` and `off` verdicts
are unchanged (compared with `==` against the old dicts). The closed trade carries
`factor_metadata.setup.kondisi` and `bucket_key`.

### Task 4 — `shadow_entries` table and store methods (`1c843aa`)

RED: `Ran 19 tests ... FAILED (errors=20)`: `AttributeError: 'SupabaseTradeStore' object has no
attribute 'save_shadow_entry'` (and the four other new methods), plus `public.shadow_entries is not
defined in supabase/schema.sql`.

GREEN: 89 tests across the store, forward-schema drift, dashboard-usage and supabase-only modules;
full suite 2726. The `long_forward` schema is regenerated with `src/forward_schema.py` and the
drift test passes unchanged. The paging loop became a shared `_page_all` with the old
`_all_cycle_rows` params unchanged. A naive datetime is refused at the store boundary.

### Task 5 — shadow evaluation, pure walk plus engine wiring (`590b35d`, fix `aa3b2d6`)

RED: `ModuleNotFoundError: No module named 'bot.core.shadow_eval'`; the engine tests
`Ran 5 tests, FAILED (failures=5, errors=1)`.

GREEN: 65 tests; full suite 2764; `shadow_eval.py` 100 % from its own tests. Three mutations (floor
before stop, the decision minute's own candle walked, stop slip 0) were each caught (2, 12 and 6
failures) and reverted.

Review found two Important issues, both fixed in `aa3b2d6` with RED first:

```
ImportError: cannot import name 'EVAL_BUDGET_SECONDS' from 'bot.core.shadow_eval'
Ran 74 tests ... FAILED (failures=13)    # venue stop, invalid rows, naive timestamps, row without an id
```

- Evaluation ran with no time bound before the entry path (three sequential `get_klines` with
  retries and failover could cost minutes): now one 20 s budget around the whole call via
  `with_timeout`, and the first venue failure ends the loop for the tick.
- A deterministic bad row stayed pending and could hold all three oldest-first slots forever: such a
  row is now closed as `invalid` (`gross_bps` NULL) and logged at ERROR.

GREEN after the fix: 80 tests; full suite 2778.

### Task 6 — setup ledger maths (`f8a2e23`)

RED: `ImportError: cannot import name 'Z_ONE_SIDED_90' from 'bot.core.setup_ledger'`.

GREEN: 21 tests; full suite 2798. `bound = mean + 1.28 x SE`; `blocked` only strictly below the hurdle
(exactly 4.0 is `open`); n = 0 gives no mean and n = 1 a mean without an SE; identical values give SE 0 with no
division; unpriceable trade rows and null or non-finite shadow values are skipped.

### Task 7 — ledger verdicts and shadow rows in the orchestrator (`41a84f5`, fix `189e752`)

RED: `Ran 10 tests ... FAILED (failures=7, errors=2)`; the telling line is
`AssertionError: TradePlan(... direction=LONG ...) is not None`: before this task a shadow-side
candidate still became a plan.

GREEN: 171 tests in the five touched modules; full suite 2808. A shadow candidate is dropped before
`candidates.append` whether or not the row write succeeds.

Review found the shadow walk started from a stale reference: the stored `ref_price` is the 3m last
price from before the AI call, so the gap to the walk's first candle was never walked, and AI entries
are trend-led, which biases shadow LONG outcomes upward (they decide the LONG go-live bar). Fixed in
`189e752`: the walk's reference is the close of the decision-minute candle. RED against the old
evaluator showed the bias itself:

```
FAIL: test_the_walk_starts_from_the_decision_minute_close
AssertionError: Tuples differ: (1, 32.4, 'floor') != (1, 9.999999999999432, 'time')
Ran 50 tests ... FAILED (failures=7)
```

The old code booked +32.4 bps from the stored price where the market gave +10. GREEN: 85 tests; full
suite 2815. A window that no longer reaches the decision minute, or has a hole there, expires.

### Task 8 — lean features and the legacy golden (`966ee95`, `04c204b`, fix `5968c2d`)

The golden `tests/fixtures/legacy_entry_payload.json` was captured **before any change** (`966ee95`);
`test_legacy_payload_matches_the_golden` passed on unchanged code.

RED: `ImportError: cannot import name 'lean_features_from_candles' from
'bot.intelligence.market_intelligence'`.

GREEN: 18 tests; full suite 2833. Removing the one `context.pop("lean_features", None)` line fails 3
tests (mutation), restored. Review found the golden tests compared parsed objects, where
`100 == 100.0` and `True == 1`, which is weaker than byte for byte: fixed in `5968c2d` to compare the
serialized text. Sensitivity: a `last_price` of `60000` instead of `60000.0` is parsed-equal but
text-different, so the new comparison catches what the old one let through.

### Task 9 — lean entry payload (`7aa07ac`, fix `1249c89`)

RED: `Ran 107 tests ... FAILED (failures=7, errors=32)`: `AttributeError: ... no attribute
'build_lean_entry_payload'`, `'lean_market_regime'`, `KeyError: 'payload_profile'`, and
`['OPEN_LONG', 'OPEN_SHORT', 'HOLD'] != ['OPEN_SHORT', 'HOLD']`.

GREEN: 180 tests; full suite 2856. The golden still passes byte for byte, so the legacy request is
unchanged.

Review found the rule sentence and the narrowed `allowed_actions` are false when the stretch gate
does not enforce, `ENTRY_STRETCH_GATE_MAX_ATR` is 0, or the behind-schedule relaxation is on, and
`Settings` did not tie `lean` to any of them. Fixed in `1249c89`: `Settings` refuses
`AI_ENTRY_PAYLOAD=lean` unless `ENTRY_STRETCH_GATE_MODE=enforce`, `ENTRY_STRETCH_GATE_MAX_ATR` > 0 and
`ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR=0`, and the rule text became "an entry with
|stretch_atr| above this limit is rejected in code" (the gate compares the absolute value). RED:

```
7x AssertionError: ValueError not raised            # the lean guard, per missing requirement
2x AssertionError: 'an entry beyond this limit is rejected in code' != 'an entry with |stretch_atr| above this limit is rejected in code'
Ran 129 tests ... FAILED (failures=9)
```

GREEN: 134 tests; full suite 2864. An orchestrator-level test also compares the legacy request with
the builder's output byte for byte.

### Task 10 — stage report (`81e4230`)

RED: `ModuleNotFoundError: No module named 'report_live_stages'`.

GREEN: 35 tests; full suite 2897 when it was run (before the last two tests of the branch were added; 2899 at `81e4230`). The test store exposes only the two reads plus `close`, so the
report is read-only by construction. Exit 2 for a naive time, no stage start, a duplicate start, or an
`--until` not after the first start.

## Review focus, and the test that pins each

| Risk | Test |
|---|---|
| The hour changes between the gate read and the decision: side, bucket and payload must share one gate context | `test_bucket_and_side_use_the_same_gate_context` (`tests/test_ai_orchestration.py`) |
| Pending shadow rows after downtime or a 60-minute hold: a row too old to walk expires, never crashes or stays pending | `test_a_row_older_than_the_candle_window_expires` (`tests/test_shadow_eval.py`) |
| `data_error` or `history_short` under `map`: no side, no AI call, `allowed_actions` never empty | `test_map_data_error_maps_no_side` (`tests/test_regime_gate.py`), `test_lean_payload_refuses_an_empty_side_list` (`tests/test_compact_context.py`), `test_no_mapped_side_under_lean_makes_no_ai_call` (`tests/test_ai_orchestration.py`) |
| Degenerate ledger samples (n = 0 or 1, identical values) | `test_identical_values_have_zero_se_and_still_decide` (`tests/test_setup_ledger.py`) |
| A shadow decision must never trade, even when best scored | `test_a_shadow_side_candidate_never_becomes_a_plan` (`tests/test_ai_orchestration.py`) |
| The legacy entry payload stays byte for byte | `test_legacy_payload_matches_the_golden` (`tests/test_compact_context.py`), `test_the_legacy_request_is_the_builder_s_output_byte_for_byte` (`tests/test_ai_orchestration.py`) |
| Schema drift for `shadow_entries` | `test_schema_sql_defines_shadow_entries` (`tests/test_supabase_shadow_entries.py`), `tests/test_forward_schema.py` |

## Deliberate choices

- **Lean drops `recent_executed_trade_performance` and every `execution_notes` entry.** Spec section
  4's "Removed" list does not name `recent_executed_trade_performance`, and the recent-performance
  summary and trade memory are not read at all under `lean`, because both only fill payload keys. The
  lean objective keeps the volume profile but drops all `execution_notes` (`fee_warning`,
  `sizing_source`). Both are pinned by tests and are part of the one lean step on purpose: its effect
  is measured as one.
- **The entry system prompt (`ai/brain.py`, `ENTRY_SYSTEM_PROMPT`) is unchanged.** It still says
  volume mandate, use recent performance and trade memory, factors and flows are execution truth,
  none of which the lean request carries. That confounds the lean experiment. Editing it is **a
  decision for the user before rollout step 2**; this branch does not touch it.
- **Account 1's settings were checked read-only on 2026-10-21** (a `Settings()` allowlist on the VPS,
  02:55 WIB, no `.env` value printed): `ENTRY_STRETCH_GATE_MODE=enforce`, max ATR 0.75,
  behind-schedule entries per hour 0, `POSITION_HOLD_MINUTES=60`, neutral exit basis `net`,
  `STRATEGY_MODE=ai_volume`, one symbol (BTC). So the lean boot guard passes as is at step 2, and the
  shadow floor assumptions (net basis, `ai_volume` sizing) hold.
- **The lean boot guard** (Task 9 fix): making the config match the sentence is smaller than
  conditional prompt text. Cost if wrong: step 2 would need the relaxation off, which is already 0.
- **Plan refinements recorded in the plan header:** the ledger reads real outcomes once per tick
  (while the mode is not `off`) instead of once per closed trade; the 15 and 60 minute changes are
  computed in bars (`change_pct_5_bars`, `change_pct_20_bars`, with `bar_interval`); the ledger
  requires a `naik`/`map` gate.
- **Rulings along the way:** the shadow walk is priced from the decision-minute close (Task 7); the
  `ledger_table` keys only buckets with at least one counted row (Task 6), so readers use a default;
  new store methods go onto test fakes only where a code path reaches them (Task 4); the golden is
  captured at the start of Task 8, before `compact_context` changes; `expired` also covers a hole at
  the decision minute; an `invalid` row (malformed values) is closed, not retried.
- **The hurdle is in gross bps per trade** (from entry and exit prices, before fees): 4.0 is the -150 per 1M
  bar at 0.035 % per leg. The two comments that said "net bps" (`.env.example`, `Settings`) were corrected
  in the review round.
- **A shadow or ledger-blocked decision is not a HOLD row.** `ai_decisions` keeps the model's own
  action (OPEN_LONG/OPEN_SHORT) with `payload_json.gate.shadow_side=true` or
  `gate.ledger.status=blocked`; only the candidate is dropped, so the tick holds and the engine logs
  "Strategy returned HOLD". Query the gate flags, not HOLD rows.
- **A ledger read failure fails open** (WARNING, every bucket `learning` that tick, the regime and
  stretch gates still apply). Shadow outcomes of source `shadow_side` never reopen a bucket; only
  `ledger_block` outcomes do.
- **`COMPACT_PROMPT_VERSION` is unchanged**, so `ai_call_metrics` cannot tell the profiles apart; the
  report separates them by stage time, and `payload_json.payload_profile` records the profile per
  decision.

## Known limits (deferred review notes, none blocking)

- ~~`ENTRY_STRETCH_GATE_MAX_ATR=inf` passes the lean guard~~ (fixed in the final review fix wave).
- ~~`SETUP_LEDGER_HURDLE_BPS` accepts `nan`/`inf`~~ (fixed in the final review fix wave);
  `SETUP_LEDGER_SINCE` has no future-time guard (a year typo leaves the ledger `learning`).
- `complete_shadow_entry` is not guarded by `evaluated_at is null`; harmless with the single
  evaluator. `shadow_outcomes_since` returns `expired`/`invalid` rows with NULL `gross_bps`; every
  reader skips them.
- The report's ledger section uses rows since the earliest `--stage-start`, the engine uses
  `SETUP_LEDGER_SINCE`; pass the same instant when comparing statuses.
- Under `lean`, a tick with no mapped side skips `build_context`, so the flow detectors do not
  advance on those ticks (the engine normally returns at gate 10 first).
- `stretch_atr` appears twice in the lean payload (`features` and `entry_rules`), from the same
  maths on the same candles.
- `build_forward_env.py` forces none of the new keys; this matters only if the forward bot moves to
  a newer tag.

## Final review fix wave

The whole-branch review found four important and four minor issues; the controller's rulings are
binding and were implemented as written. Every default (`ENTRY_REGIME_GATE=off`,
`AI_ENTRY_PAYLOAD=legacy`, `SETUP_LEDGER_MODE=off`) is untouched, `tests/fixtures/legacy_entry_payload.json`
is unchanged, and the entry system prompt and `COMPACT_PROMPT_VERSION` are untouched.

### 1. Lean showed ledger statuses the code did not enforce (`3da90ff`)

Under `SETUP_LEDGER_MODE=shadow` the lean `setup_ledger` lines said `blocked` while the bot still
traded the bucket. Ruling: the lean payload carries `setup_ledger` only under `enforce`.

RED (`test_lean_omits_the_ledger_block_unless_enforcing`):

```
FAIL: test_lean_omits_the_ledger_block_unless_enforcing (tests.test_ai_orchestration.LeanEntryPayloadTests) (mode='shadow')
AssertionError: True != False
```

GREEN: `_ledger_lines` returns `[]` unless the mode is `enforce`; the builder already omits the key
for no lines. `test_lean_ledger_lines_reuse_the_one_ledger_read` now pins the `enforce` output (and
that the shown `blocked` status really drops the candidate into a `ledger_block` row). `shadow` still
records the verdict at `payload_json.gate.ledger`.

### 2. Shadow rows had no spacing (`85186fb`)

A row was written on every tick the model answered OPEN on a shadow side (and for every
enforce-blocked candidate), so 60-minute walks overlapped, inflating n and understating the SE.
Ruling: no new row for the same (symbol, side, source) while the last written one was decided less
than `POSITION_HOLD_MINUTES + COOLDOWN_MINUTES_AFTER_ENTRY` minutes earlier.

RED:

```
ERROR: test_a_fresh_orchestrator_starts_with_no_spacing_state (tests.test_ai_orchestration.LedgerAndShadowTests)
AttributeError: 'AIOrchestrator' object has no attribute '_shadow_rows_written_at'
FAIL: test_a_second_shadow_row_within_the_spacing_is_skipped_and_still_not_traded (tests.test_ai_orchestration.LedgerAndShadowTests)
AssertionError: Lists differ: [date[58 chars].utc), datetime.datetime(2026, 10, 21, 3, 30, [25 chars]utc)] != [date[58 chars].utc)]
FAIL: test_spacing_is_per_symbol_side_and_source (tests.test_ai_orchestration.LedgerAndShadowTests)
AssertionError: Lists differ: [('BT[139 chars]ide'), ('BTC/USDT', 'long', 'shadow_side'), ('[29 chars]de')] != [('BT[139 chars]ide')]
```

`test_a_failed_write_does_not_start_the_spacing` passed before the change (there was no spacing to
start) and pins the failure path of the new code.

GREEN: `AIOrchestrator.__init__` sets `_shadow_rows_written_at = {}`; `_record_shadow_entry` reads the
clock once (`_utc_now`, the one seam the tests move), skips a row inside the spacing with one INFO
line giving the minutes remaining, and replaces (never mutates) the map only after a successful
write. The candidate is dropped in every case. A restart forgets the map (documented in CLAUDE.md).

### 3. `reopened` never expired (`7ab059a`)

Once reopened a bucket wrote no more `ledger_block` rows, so its old shadow sample kept it reopened
forever. Ruling: a bucket the real rule blocks and the shadow rule reopens is judged again on its
real trades opened after its latest `ledger_block` row's `decided_at`; `min_samples` of them with
mean + 1.28 x SE below the hurdle make it `blocked` again.

RED (behaviour, then the pure API):

```
FAIL: test_ledger_enforce_blocks_a_reopened_bucket_that_loses_again (tests.test_ai_orchestration.LedgerAndShadowTests) (new_trades=3)
AssertionError: 'reopened' != 'blocked'
FAIL: test_a_reopened_bucket_that_loses_again_reads_blocked (tests.test_report_live_stages.CliTests) (status='blocked')
AssertionError: 'reopened' != 'blocked'
ImportError: cannot import name 'EMPTY_BUCKET_LEDGER' from 'bot.core.setup_ledger'
```

GREEN: `ledger_table` returns a frozen `BucketLedger(real, shadow, real_since_block)` per bucket.
`real_since_block` holds the counted trades whose `opened_at` is strictly after the bucket's latest
`ledger_block` `decided_at` (every block row with a readable time sets it, an expired one included;
times are parsed by `shadow_eval._parse_utc`, offset required, so an unreadable or naive time never
counts). `ledger_status(bucket, min_samples, hurdle_bps)` stays pure. The orchestrator reads a
missing bucket as `EMPTY_BUCKET_LEDGER`; the report adds `n sejak blok` and its bound. Named tests:
`test_a_reopened_bucket_that_loses_again_is_blocked_again`,
`test_a_reopened_bucket_with_too_few_new_trades_stays_reopened` (`tests/test_setup_ledger.py`).

### 4. The shadow walk models the floor neutral exit (`5b6b9eb`)

Ruling: `Settings` refuses `ENTRY_REGIME_GATE=map` or `SETUP_LEDGER_MODE` other than `off` unless
`VOLUME_NEUTRAL_EXIT_ENABLED=true` and `VOLUME_NEUTRAL_EXIT_MODE=floor`, one ValueError naming what is
missing; `naik` with the ledger off is not checked. Account 1 already runs the floor exit, enabled
(verified read-only by the controller).

RED:

```
FAIL: test_map_refuses_without_the_floor_neutral_exit (...) (override={'VOLUME_NEUTRAL_EXIT_ENABLED': 'false'})
AssertionError: ValueError not raised
FAIL: test_one_error_names_both_missing_keys (...)
AssertionError: ValueError not raised
FAIL: test_the_ledger_on_refuses_without_the_floor_neutral_exit_even_under_naik (...) (mode='shadow')
AssertionError: ValueError not raised
```

GREEN: `_require_shadow_walk_companions`, after the regime and ledger checks. The map and lean test
Settings now set both keys (`FLOOR_EXIT` in `tests/test_live_staged_settings.py`); the other
real-`Settings` tests (`test_regime_gate.py`, `test_trade_engine_forward_gates.py`) build only `naik`
with the ledger off, which is not checked, and every other map or ledger bot in the tests is a
`SimpleNamespace`, which the validator never sees.

### 5-8. Minor (`5b6b9eb`, `0a8f592`)

- **5. Non-finite hurdle.** `SETUP_LEDGER_HURDLE_BPS` has `allow_inf_nan=False`. RED:
  `test_a_non_finite_hurdle_is_refused` (`nan`, `inf`, `-inf`) `AssertionError: ValueError not raised`.
- **6. Infinite stretch limit.** The lean guard requires `math.isfinite(ENTRY_STRETCH_GATE_MAX_ATR)`
  ("a finite ENTRY_STRETCH_GATE_MAX_ATR above 0"); the field is unchanged, so a legacy `.env` with
  `inf` still boots (`test_legacy_keeps_accepting_an_infinite_stretch_limit`). RED:
  `test_lean_refuses_an_infinite_stretch_limit` `AssertionError: ValueError not raised`.
- **7. Comments.** `config.py` and `.env.example` now say ledger `shadow` only records the verdict at
  `payload_json.gate.ledger` and writes no rows, the regime gate comment names `map`, and the
  `.env.example` header no longer calls the block "LONG forward bot" only.
- **8. One source per ledger read.** `shadow_outcomes_since(since, source=None)` adds
  `source=eq.<value>` when given; the orchestrator passes `ledger_block`, the report reads both. RED:
  `TypeError: SupabaseTradeStore.shadow_outcomes_since() got an unexpected keyword argument 'source'`,
  and the orchestrator's `ledger_reads` assertion (`None != 'ledger_block'`).

### Known limit of the ruling in item 3

The reopening sample is every `ledger_block` outcome since `SETUP_LEDGER_SINCE`. When a re-blocked
bucket writes its next `ledger_block` row and that row is evaluated (hold + 1 minutes later), the
latest block time moves past every real trade, `real_since_block` restarts empty, and the old good
shadow sample reads the bucket `reopened` again. So a re-block lasts about an hour, then another
`min_samples` real trades must fail. Implemented as ruled; flagged for the controller.

### Verification (final wave)

Run on `feat/live-staged-fixes` at `7ab059a` plus this documentation, Python 3.10.20 from `.venv`,
2026-10-21.

```
PYTHONPATH=src python -m compileall -q src tests                                   # exit 0
PYTHONPATH=src python -m unittest discover -s tests                                # Ran 2923 tests, OK
PYTHONPATH=src python -m coverage run --rcfile=<rc> -m unittest discover -s tests  # Ran 2923 tests, OK
python -m coverage report --rcfile=<rc>                                            # TOTAL 17296 1405 92%   (fail_under 80)
git diff --check origin/main...HEAD                                                # exit 0, no output (origin/main = 95e61b7)
uv pip check --python .venv/bin/python                                             # All installed packages are compatible
```

- 2,923 tests (2,899 before the wave: +24). `<rc>` mirrors `pyproject.toml`'s `[tool.coverage]`
  (`source`, `omit`, `exclude_lines`, `fail_under`), as above.
- Modules: `setup_ledger.py` 100 %, `shadow_eval.py` 100 %, `orchestration.py` 96 %, `config.py`
  96 %, `supabase_trade_store.py` 95 %, `report_live_stages.py` 98 %. Every line the wave changed
  under `src/` is covered.
- The secret-hygiene, dashboard-contract, forward-schema and import-structure tests pass unchanged;
  `setup_ledger.py` now imports `bot.core.shadow_eval` (for `_parse_utc`), which imports only
  `bot.telemetry.ws`, so neither module reaches `pine_indicators` or `bot/strategy/modes/`.

## Verification

Run on `feat/live-staged-fixes` at `81e4230` (docs files only added after), Python 3.10.20 from
`.venv`, 2026-10-21.

```
PYTHONPATH=src python -m compileall -q src tests                     # exit 0
PYTHONPATH=src python -m coverage run -m unittest discover -s tests
Ran 2899 tests in 138.931s
OK
PYTHONPATH=src python -m coverage report                             # TOTAL 17223 1405 92%   (fail_under 80)
git diff --check origin/main...HEAD                                  # exit 0, no output (origin/main = 95e61b7)
uv pip check --python .venv/bin/python                               # All installed packages are compatible (17)
PYTHONPATH=src python -m unittest tests.test_secret_hygiene          # Ran 1 test, OK
```

- Baseline on `95e61b7` (a `git archive` snapshot, same interpreter): 2,656 tests, all OK except the
  secret-hygiene test, which needs a git checkout and so cannot run in a snapshot. The branch adds
  243 tests (2,899 - 2,656) and 11 files touched under `tests/` (3,943 added lines) against 12 under `src/` (1,699 added lines).
- Coverage by module (line coverage): `setup_ledger.py` 100 %, `shadow_eval.py` 100 %,
  `regime_gate.py` 99 %, `report_live_stages.py` 98 %, `orchestration.py` 96 %, `config.py` 96 %,
  `supabase_trade_store.py` 95 %, `market_intelligence.py` 95 %, `compact_context.py` 56 % (the old
  `_compact_*` external-provider projection is dead code, see CLAUDE.md). Of the 777 executable lines
  this branch added under `src/`, 773 are covered; the 4 missed are `_run` and `main` in
  `report_live_stages.py`, the CLI glue.
- The `.venv` Python 3.10 cannot read `pyproject.toml` for coverage (no `tomli`), and the venv has no
  `pip`; coverage was run with an rcfile carrying the same `source`, `omit`, `exclude_lines` and
  `fail_under` as `pyproject.toml`, and the dependency check used `uv pip check`. CI (3.11) reads the
  file natively.
- The import structure tests (`test_mode_backtest_structure`, `test_pine_gate_structure`) are
  unchanged and pass: `setup_ledger.py` and `shadow_eval.py` import neither `pine_indicators` nor
  `bot/strategy/modes/`, and `bot` imports no `research`. `shadow_eval.py` imports only
  `bot.telemetry.ws` (`with_timeout`) from `bot`.
- `tests/fixtures/legacy_entry_payload.json` has not changed since its capture in `966ee95`.
- Secret hygiene: the secret scan test passes on the tracked files, including this document; the
  work never read or printed a `.env` value.
- Dashboard contract (`tests.test_dashboard_contract`) is unchanged and passes.

## Commit trail

```
e7bec49 docs: spec for staged live-bot fixes (lean AI payload, two-way regime gate, setup ledger)
f6fa591 docs: implementation plan for the staged live-bot fixes
079bc6b feat: regime gate map mode (NAIK long, TURUN short)                     Task 1
23c8fc7 feat: settings for the map gate, the lean payload and the setup ledger  Task 2
84fc369 feat: map the decision side to the regime and mark shadow sides         Task 3
1c843aa feat: shadow_entries table and store methods                            Task 4
590b35d feat: evaluate shadow entries with the live exit rules                  Task 5
aa3b2d6 fix: bound shadow evaluation and close invalid rows                     Task 5 review
f8a2e23 feat: setup ledger statistics and status                                Task 6
41a84f5 feat: setup ledger verdicts and shadow rows in the entry path           Task 7
189e752 fix: walk shadow outcomes from the decision-minute close                Task 7 review
966ee95 test: golden for the legacy entry payload                               Task 8
04c204b feat: lean 3m features beside the technicals                            Task 8
5968c2d test: compare the legacy payload golden byte for byte                   Task 8 review
7aa07ac feat: lean AI entry payload behind AI_ENTRY_PAYLOAD                     Task 9
1249c89 fix: lean payload only with an enforcing stretch gate                   Task 9 review
81e4230 feat: live stage report                                                 Task 10
        docs: live staged fixes in CLAUDE.md and TDD evidence                   Task 11
5b6b9eb fix: settings guards for the shadow walk exit, a finite hurdle and a finite stretch limit   final review 4-7
0a8f592 perf: read only ledger_block shadow outcomes for the setup ledger       final review 8
3da90ff fix: lean payload shows ledger statuses only while the ledger enforces  final review 1
85186fb fix: space shadow rows by the hold plus the entry cooldown              final review 2
7ab059a fix: judge a reopened ledger bucket again on its new real trades        final review 3
        docs: final review fix wave in CLAUDE.md, the spec and TDD evidence     final review
```

## Deployment gate

Merging changes nothing: `ENTRY_REGIME_GATE=off`, `AI_ENTRY_PAYLOAD=legacy` and
`SETUP_LEDGER_MODE=off` by default, and the legacy payload is pinned by the golden. Merge only on
green CI (`gh pr merge --auto` merges at once here; read the checks first). Every rollout step
below is a settings change plus a restart while flat, on the user's go-ahead for that step only.
Never restart with an open position: check flat on the venue **and** in `open_position_state`
(`~/replay/flat_check.py`; restart `ai-perp-volume` only on `FLAT`).

1. **Before step 1: apply the `shadow_entries` block** of `supabase/schema.sql` in the Supabase SQL
   editor (`public` schema), then verify with a read-only GET that returns `[]`. Without the table,
   every shadow write and every pending read fails (a WARNING per tick, trading continues).
2. **Step 1** (`ENTRY_REGIME_GATE=map`, `ENTRY_GATE_DIRECTION=short`, `SETUP_LEDGER_MODE=shadow`,
   `SETUP_LEDGER_SINCE` = the restart time): pull merged `main` in
   `/home/ubuntu/ai-perpetual-trading`, back up `.env` with a timestamp, set only these keys with a
   names-only check, read the effective values back through `Settings()` with an allowlist, restart
   while flat. Then confirm the startup log, the first `Regime gate BTC/USDT:` line, and no ERROR
   besides the pm2 stop traceback. Watch for `Shadow evaluation failed`, `hit its 20 s budget`,
   `Setup ledger could not be read` and `Lean entry payload has no side to offer`.
3. **Judge each step with `src/report_live_stages.py --stage-start <restart ISO>`** after 60 trades:
   gross bps per trade against the previous step with its SE, and volume per day. A step worse by
   more than one SE is rolled back by reverting its settings and restarting while flat; shadow rows
   are harmless and stay.
4. **Step 2** (`AI_ENTRY_PAYLOAD=lean`) waits for step 1's 60 SHORT trades **and** the user's decision
   on the entry system prompt (see Deliberate choices). **Step 3** (`SETUP_LEDGER_MODE=enforce`)
   follows step 2's 60 trades.
5. **LONG goes live** (`ENTRY_GATE_DIRECTION=any`) only when the LONG shadow outcomes reach n >= 60
   with mean gross >= +4 bps. Until then LONG costs only AI calls.
6. Record each step's start time in project memory and in CLAUDE.md ("Live staged fixes") in a
   separate small docs PR. The forward bot (account 2) is not redeployed.
