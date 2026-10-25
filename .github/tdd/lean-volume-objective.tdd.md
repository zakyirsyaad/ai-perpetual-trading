# Lean entry objective `volume`: trade the bounce inside the stretch gate's limit

## User journey

On Sunday 25 Oct 2026, with the entry window closed (Saturday 00:00 to Monday 07:00 WIB) and both bots flat, the operator asked for a review of the week's results and what to fix. The review of rollout step 1 (live since 21 Oct 20:53 WIB) found:

| | |
|---|---|
| Trades after the restart | **2** in the first 26 TURUN hours (51 window hours), 32k volume per day, 3 % of the 1M target |
| What the legacy request produced in those 26 hours | 387 calls: 246 HOLD, 115 OPEN_LONG (dropped by the side filter), 24 OPEN_SHORT (dropped by the stretch gate, stretch 0.85 to 4.5 ATR), 2 allowed |
| Near-mean ticks in the same hours | about half (247 of 519) |
| Consequence for the rollout | the spec's "60 SHORT trades in 2-4 weeks" is about three months, so no step can be judged by it |

The cause is in the request, not the gate. The stored `learned_trade_memory` read:

> Entry shape SHORT|stretch_flat|atr_low lost -1023.66 USDT net over 48 trades (33 losing); require unusually clear live evidence before opening that shape again.

with `winning_patterns` empty. The gate admits only near-mean entries, so `stretch_flat` is the only shape that ever has data, and its loss is fee: 877 of the 1,024 USDT, gross -146 USDT over 48 trades (-1.2 bps). 65 % of the TURUN HOLD reasons cite `stretch_flat`, and 24 of the 26 SHORT proposals were `stretch_down` entries the code rejects. The lean request (step 2) drops the memory, but its prompt says "HOLD is the expected answer most ticks" and hides `min_confidence`: in the replay below, its OPEN answers carry confidence 0.52 to 0.58 and the code (`AI_MIN_CONFIDENCE`, which cannot go below 0.75) drops every one.

The operator chose option C: a lean variant with a volume objective, prepared for review, replayed offline on the TURUN ticks, delivered as a PR. No merge and no restart before the operator says so.

## Design

`AI_LEAN_OBJECTIVE` (`edge` | `volume`, default `edge`) is read only with `AI_ENTRY_PAYLOAD=lean`.

- `edge` is today's lean request, byte for byte: its prompt is pinned by sha256 in `tests/test_ai_brain_contract.py`, as the legacy prompt already is.
- `volume` changes three things and nothing else in the request:
  1. the system prompt: `ENTRY_SYSTEM_PROMPT_LEAN_VOLUME` (`GrokBrain._entry_prompt` picks it from the setting);
  2. the recorded `ai_call_metrics.prompt_version`: `lean-entry-volume-v1` (`LEAN_VOLUME_PROMPT_VERSION`);
  3. the payload's `strategy_objective.primary_goal`: `LEAN_VOLUME_PRIMARY_GOAL`.
- A settings object without the field (the namespaces other tests build) means `edge` (`lean_objective()` in `compact_context.py`); a legacy call never sees the volume prompt.
- Settings refuses `volume` unless `AI_ENTRY_PAYLOAD=lean` (the lean request carries the regime block and offers one side), `ENTRY_REGIME_GATE` is `naik` or `map` (otherwise the request would offer the live sides and there is no hourly side to follow), `STRATEGY_MODE=ai_volume` (the goal is volume and the cap is the `ai_volume` one) and `AI_MIN_CONFIDENCE=0.75` (the prompt states that number; `LEAN_VOLUME_MIN_CONFIDENCE` in `config.py`, tied to the prompt text by a test). One error names every missing key.
- The pace is set by the code, not by the model: one position at a time, the stretch gate, the side filter and the daily cap. On account 1 the cap is `VOLUME_ATTEMPT_TARGET_PER_DAY` (`TradeEngine._daily_entry_limit` returns it for `ai_volume` with a daily volume target), not `TRADE_TARGET_PER_DAY`.

What the volume prompt says, and why:

- **The bounce half of what the gate lets through.** The gate checks only |stretch|; the sign split is the prompt's. "A pullback to the 21-EMA against the hourly move": SHORT with `stretch_atr` from 0 to `max_stretch_atr` (price has bounced up to or slightly through the EMA), LONG from `-max_stretch_atr` to 0. Price still on the trend side of the EMA is "the chase side": HOLD.
- **HOLD only for a reason the model can name** (chase side, a decisive move against the side, missing data, a stop that cannot be set safely); "a small expected edge" is not one, because the code controls frequency and risk.
- **The execution threshold.** "The code executes an OPEN only at 0.75 or higher." Lean hides `min_confidence`, so without this sentence honest confidences are dropped silently.
- **Unchanged from the lean prompt:** the inputs list, the stop and liquidation rules, the skeptical `risk_review` sentence (a test pins that it is the lean prompt's), one decision per allowed symbol, short free text. No memory, factors or flow signals. Added: stops "around 0.25 to 0.40 percent" (guidance; the engine only caps them at 0.40 when `MAX_STOP_LOSS_PCT` is set).
- **Pinned:** the prompt's sha256 and `lean-entry-volume-v1` are pinned in `tests/test_ai_brain_contract.py`; a changed text needs a new version and a new pin. The model still sees `trade_target_per_day` in `risk_constraints`; the cap that binds is `VOLUME_ATTEMPT_TARGET_PER_DAY`.

## Hypothesis and decision rule (written before the forward run)

The "bounce" half of the setup comes from a diagnostic on the venue's own candles, 8 to 23 Oct, mechanical entries on every near-mean 3m tick of TURUN hours, one position at a time, the live exits (stop 0.30 %, floor +0.324 %, 60 minutes), regime replayed offline with `RegimeGate._classify` (it reproduces the live log's transitions exactly):

| TURUN SHORT, near-mean | n | gross bps |
|---|---|---|
| all | 139 | +1.9 +- 2.2 |
| stretch >= 0 (price at or above the 21-EMA) | 112 | **+4.4 +- 2.4** |
| stretch < 0 (price already below it) | 108 | +0.0 +- 2.5 |

It is data-snooped (five splits were looked at) and rests on one market episode, so it is a hypothesis, not a finding. It agrees with two earlier readings (SHORT "chase" -8.0 bps, n=93, and the 26-hour window: above the EMA +7.7, below -4.3). A sub-zero stretch costs nothing to skip beyond throughput.

- **H1:** under `volume`, TURUN SHORT entries (stretch 0 to 0.75) average at least +4 bps gross per trade, the bar at which -150 per 1M is reachable (net per 1M = -350 + 50 x gross bps).
- **Forward test:** the setup ledger already scores each bucket (`SHORT|TURUN|atr_*`) on gross bps: from 60 trades it keeps a bucket open while mean + 1.28 SE >= the 4 bps hurdle. Restart `SETUP_LEDGER_SINCE` at the step-2 restart so legacy-payload trades do not mix in. The bucket does not split by stretch sign and the model also opens slightly below 0 (replay: 10 of 21 ticks in the -0.2 to 0 band in the draft, 4 of 6 in the final), so at the read split the closed trades by the sign of `factor_metadata.setup.stretch_atr` and judge H1 on the 0 to 0.75 half.
- **Early stop:** at 30 trades, a mean gross below 0 means rolling back (`AI_LEAN_OBJECTIVE=edge` or `AI_ENTRY_PAYLOAD=legacy`, restart while flat). The daily guards pause a bad day by themselves; their live values are loose, see the week-1 guards in the deployment gate.
- **What the forward read measures:** under `volume` the model opens on essentially every eligible tick (replay: 78 of 78 in the draft's bounce bands, 27 of 28 in the final) and the "decisive move against the side" exception never fired, so the read is the edge of the bounce rule with an AI veto, not a test of AI selection. Whether the AI adds value stays open; the daily funnel should keep reporting P(OPEN | eligible tick) so a change in that rate is visible.
- **Expected cost if H1 is false:** gross 0 gives -350 per 1M; at the 10-per-day cap that is about -175 USDT per TURUN day on 0.5M volume.

## RED

The tests were written first against names that did not exist yet:

```
$ PYTHONPATH=src python -m unittest tests.test_ai_brain_contract tests.test_compact_context tests.test_live_staged_settings tests.test_ai_orchestration
ImportError: cannot import name 'ENTRY_SYSTEM_PROMPT_LEAN_VOLUME' from 'bot.ai.brain'
ImportError: Failed to import test module: test_ai_brain_contract
FAIL: test_volume_refuses_without_a_regime_gate (tests.test_live_staged_settings.LeanObjectiveSettingsTests)
FAIL: test_volume_refuses_the_legacy_payload (tests.test_live_staged_settings.LeanObjectiveSettingsTests)
FAIL: test_volume_names_both_missing_requirements_at_once (tests.test_live_staged_settings.LeanObjectiveSettingsTests)
FAIL: test_the_volume_objective_swaps_only_the_goal (tests.test_ai_orchestration.LeanEntryPayloadTests)
FAIL: test_an_unknown_objective_is_refused (tests.test_live_staged_settings.LeanObjectiveSettingsTests)
ERROR: test_volume_boots_with_the_lean_payload_and_a_regime_gate ... test_the_objective_is_normalised ... test_the_default_objective_is_edge (x3) ...
ERROR: test_lean_volume_objective_text_is_exact (tests.test_compact_context.LeanPayloadTests)
ERROR: test_edge_is_never_checked (tests.test_live_staged_settings.LeanObjectiveSettingsTests)
Ran 175 tests in 0.278s - FAILED (failures=5, errors=8)
```

`test_an_orchestrator_without_the_objective_setting_keeps_the_edge_goal` passed from the start. The sha256 pin of the unchanged `edge` prompt could not fail in RED (its module did not import) and passes in GREEN; together they guard that nothing else moves.

## GREEN

- `ai/compact_context.py`: `LEAN_VOLUME_PROMPT_VERSION`, `LEAN_OBJECTIVE_EDGE/VOLUME`, `LEAN_VOLUME_PRIMARY_GOAL`, `lean_objective(settings)`.
- `ai/brain.py`: `ENTRY_SYSTEM_PROMPT_LEAN_VOLUME`; `entry_decisions` asks `_entry_prompt(lean)` for the (prompt, version) pair.
- `ai/lean_payload.py`: `_lean_strategy_objective_payload` picks the goal by objective.
- `config.py`: `ai_lean_objective` (`AI_LEAN_OBJECTIVE`), its normaliser and `_require_volume_objective_companions`.
- `.env.example` documents the key.

## Offline replay on the step 1 TURUN ticks

What it is: the 387 stored decisions of account 1 made in TURUN hours between 22 Oct 22:02 and 24 Oct 00:00 WIB. For each sampled tick the lean request is rebuilt with the bot's own builder: the stored legacy `market_context` (technicals, depth, external signals and liquidation limits as the bot saw them), the 3m lean features recomputed from the venue's closed 3m candles (exactly the last 40, as `AI_KLINE_LIMIT=40`; the window is matched to the stored `last_price`, so 325 of the 387 ticks align and the other 62 are dropped from every arm), the stored regime verdict and the stretch the gate measures. The model's answer then goes through the bot's entry gates (confidence >= 0.75, `risk_review` approved, |stretch| <= 0.75) and the live exits are walked over the venue's 1m candles (stop = min(max(model stop, 1.5 x ATR%), 0.40), floor +0.324 %, 60 minutes), one position at a time and at most 10 entries per day. The key was read in-process from the live `.env` and never printed; the scripts and outputs are on the VPS under `~/replay` (`lean_replay.py`, `out/lean_replay_*.jsonl`). About 2.2M tokens were spent in all.

| Arm | Ticks | OPEN_SHORT answers | Pass the code's gates | Trades, one at a time, <= 10 a day | Tokens |
|---|---|---|---|---|---|
| legacy request, as stored (live) | 387 | 26 (and 115 OPEN_LONG) | 2 | **2** | about 20k a call (not replayed) |
| lean, `edge` prompt | 111 | 18 | **0** | **0** | 404k (3.6k a call) |
| lean, `volume` prompt, draft wording | 325 | 88 | 88 | 11 | 1,288k (4.0k a call) |
| lean, `volume` prompt, final wording | 111 | 31 | 31 | 11 | 443k |

The `volume` wording changed after the independent review (the `risk_review` sentence went back to the lean prompt's, two sentences were reworded). On the same 111 ticks the two wordings gave the same action 109 times (98 %) and the same tradable set in 31 of 33 cases.

By the stretch the gate measures (`volume`, draft / final wording): price on the trend side of the EMA (stretch below -0.2): 0 of 146 / 0 of 50 opened; at the mean (-0.2 to 0): 10 of 21 / 4 of 6; bounce 0 to 0.375: 43 of 43 / 13 of 13; bounce 0.375 to 0.75: 35 of 35 / 14 of 15; beyond 0.75: 0 of 80 / 0 of 27. Every OPEN answer carries confidence 0.75 to 0.85, `risk_review` approved, and a stop of 0.28 to 0.40 % (median 0.30).

What it shows:

1. **Lean as built (`edge`) would not trade.** Its 18 OPEN answers carry confidence 0.52 to 0.58 and the code drops all of them at `AI_MIN_CONFIDENCE`. Lean hides `min_confidence`, and the setting cannot go below 0.75, so honest confidences never pass. This is a defect of the `edge` prompt as shipped in PR #46, left unchanged here (its prompt is pinned); `volume` states the threshold.
2. **`volume` follows its setup.** It opens on the bounce side and not on the chase side or beyond the limit, and it kept its HOLD discretion for one case in 15 in the 0.375 to 0.75 band. In 26 TURUN hours the legacy request produced 2 trades; this one produces 11 with one position at a time (about 10 a day with the cap), in 4k tokens a call instead of 20k.
3. **The outcomes are not evidence.** Walked with the live exits, the tradable answers average +4.9 +- 2.6 bps gross (88, overlapping), +5.6 +- 5.1 (18, an hour apart) and +3.3 +- 7.7 (11 trades, one at a time) for the draft wording, +4.0 +- 4.3 (31) and +3.7 +- 7.9 (11) for the final one; by band, bounce 0 to 0.375 +6.7 +- 3.8 (43), 0.375 to 0.75 +4.1 +- 3.8 (35), at the mean -0.2 +- 8.2 (10). That window is the one the "bounce" hypothesis was read from, so these numbers cannot confirm it; the forward test below can.

Limits: one market episode; the model is stochastic (a rerun would differ by a few answers); depth and volume are the stored ones but the entry time is approximated (the stored context time plus the call latency); the 62 unaligned ticks are missing; NAIK hours (LONG, shadow only) were not replayed.


## Verification

| Check | Result |
|---|---|
| `python -m compileall -q src tests` | clean |
| `python -m unittest discover -s tests` | **3,004 tests, OK** (2,978 on `main` before this branch) |
| `python -m coverage report` | **92 %** (gate 80 %) |
| `git diff --check` | clean |
| `tests.test_secret_hygiene` | OK |
| Independent review (`python-reviewer`, read-only) | verified the three hard constraints (defaults byte for byte, a legacy call never sees the volume prompt, settings objects without the field still work); its findings and what was done: see below |

New tests: `LeanObjectiveSettingsTests` (12) in `tests/test_live_staged_settings.py`; 11 in `EntrySystemPromptTests` (`tests/test_ai_brain_contract.py`: the pins, the selection by setting, the contract, the skeptical sentence, the confidence number, the cited feature names); 1 in `LeanPayloadTests` and 2 in `LeanEntryPayloadTests`.

Review findings:

- **Fixed:** the prompt hardcoded 0.75 while `AI_MIN_CONFIDENCE` accepts up to 1.0 (Settings now refuses `volume` unless it is 0.75, and a test ties the prompt text to `LEAN_VOLUME_MIN_CONFIDENCE`); the `risk_review` sentence had been softened without being declared (restored to the lean prompt's, with a test); the volume prompt was not hash-pinned (now pinned); `volume` with `naik` and `STRATEGY_MODE=grok` booted (now needs `ai_volume`); "the system's stops run 0.25 to 0.40" claimed more than the code guarantees (now guidance); "buys the dip in an uptrend hour" and "the setup the stretch gate admits" overstated (LONG is shadow on account 1; the gate checks only |stretch|, the sign split is the prompt's); a blank objective, `lean_objective()` and the feature names the prompt cites now have tests.
- **Second review (the operator asked for one before merging):** 14 findings, none breaking a default. Fixed in this PR, documentation and one test name only: the week-1 guards above, the step-2 row in `CLAUDE.md` (lean with the default `edge` trades nothing), the `.env.example` comment, the test name, and the two statements above (what the forward read measures; the stretch-sign split at the read). Held for the prompt review, because they change the text and so the pin and the version: which `stretch_atr` field governs, the boundary just below 0, the "chase side" label. Left as follow-ups: a pre-call skip for ticks the stretch alone decides (about 70 % of calls), passing `min_confidence` in the payload instead of hardcoding 0.75, a replay of NAIK hours, the objective on the decision record, the 800-line files, and the payload's `trade_target_per_day`.
- **Not changed:** `ai_decisions.payload_json` records `payload_profile` but not the objective; the stored `request.strategy_objective.primary_goal` and `ai_call_metrics.prompt_version` already tell the two apart. The model still sees `trade_target_per_day` in `risk_constraints` while the cap that binds is `VOLUME_ATTEMPT_TARGET_PER_DAY`.


## Deployment gate

Merging changes nothing: `AI_LEAN_OBJECTIVE` defaults to `edge` and only matters with the lean payload. Taking it live is a settings change plus a restart on the operator's go-ahead (names only, never values in chat):

1. Back up `.env`; fast-forward the live checkout to the merged `main`.
2. Set `AI_ENTRY_PAYLOAD=lean`, `AI_LEAN_OBJECTIVE=volume`, `VOLUME_ATTEMPT_TARGET_PER_DAY=10` and a new `SETUP_LEDGER_SINCE` (the restart time). `ENTRY_STRETCH_GATE_MODE=enforce`, `ENTRY_STRETCH_GATE_MAX_ATR=0.75`, `ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR=0`, `ENTRY_REGIME_GATE=map`, `ENTRY_GATE_DIRECTION=short`, `STRATEGY_MODE=ai_volume` and `AI_MIN_CONFIDENCE=0.75` already satisfy the checks; read the effective values back through `Settings()` with an allowlist (`Settings()` itself refuses a combination that does not hold).
3. **Week-1 guards (the operator's call).** The live values, read on 25 Oct, were set for a 20-a-day cadence: `COOLDOWN_MINUTES_AFTER_STOP` 0, `MAX_STOP_LOSS_PER_DAY` 12, `MAX_DAILY_DRAWDOWN_USDT` 1,500, `MAX_CONSECUTIVE_LOSSES` 15. The model opens on almost every bounce tick and the regime verdict changes only at an hourly close, so an hour that flips into a rally lets the bot re-short the next tick after every stop-out: 3 to 5 stops of about -92 USDT each (stop 0.30 % of 25,000 plus fee) is -280 to -460 USDT before any of the four trips. Suggested for week 1: `COOLDOWN_MINUTES_AFTER_STOP=20` (the `.env.example` value); `MAX_STOP_LOSS_PER_DAY=5` (the pocket's stop rate is about 30 %, so 5 stops in 10 trades is about one day in seven, and the day then pauses until the UTC date rolls); `MAX_DAILY_DRAWDOWN_USDT=600` (about six stop-outs). Leave `MAX_CONSECUTIVE_LOSSES` as it is: it counts every net-negative trade, time exits that do not cover the fee included, and roughly 60 % of the pocket's trades are net negative, so a limit of 4 would trip on about one day in two (a run of 4 net losers in 10 trades has a 40 to 50 % chance) and cut the sample. Loosen the three once a week of funnel looks healthy. Read back, with the others, `COOLDOWN_MINUTES_AFTER_STOP`, `MAX_STOP_LOSS_PER_DAY`, `MAX_DAILY_DRAWDOWN_USDT`, `MAX_CONSECUTIVE_LOSSES`, `VOLUME_ATTEMPT_TARGET_PER_DAY`, `AI_MIN_CONFIDENCE` and `STRATEGY_MODE` through `Settings()` with an allowlist.
4. `~/replay/flat_check.py` must print `FLAT` (the window is closed until Monday 07:00 WIB); restart `ai-perp-volume` only then. Never restart with an open position.
5. After the restart: the startup lines, the first regime gate line, and `ai_call_metrics.prompt_version = lean-entry-volume-v1` on the first entry call.
6. Judge it with `report_live_stages.py --stage-start <restart time>`; the first daily read is the funnel (calls, OPEN answers, gate drops, trades).

Rollback: `AI_LEAN_OBJECTIVE=edge` is not a trading rollback (lean with `edge` does not trade, see the replay); use `AI_ENTRY_PAYLOAD=legacy` (and the guard keys back to their old values), restart while flat. The forward LONG bot (account 2) runs a pinned tag and is not touched.

## Rollout record (applied 2026-10-25)

Applied on the operator's go-ahead on Sunday 2026-10-25, with the entry window closed (it reopens Monday 26 Oct 07:00 WIB):

| | |
|---|---|
| Merge | PR #49, merge commit `7a6ffa9`, CI green on head `8348664` |
| Live checkout | `34695fd` -> `7a6ffa9` (fast-forward) |
| `.env` backup | `.env.bak-step2-20261025T040647Z` (mode 600, in the live directory) |
| Keys | `AI_ENTRY_PAYLOAD=lean` and `AI_LEAN_OBJECTIVE=volume` (both unset before); `VOLUME_ATTEMPT_TARGET_PER_DAY=10` (unset, so 34); `COOLDOWN_MINUTES_AFTER_STOP=20` (was 0); `MAX_STOP_LOSS_PER_DAY=5` (was 12); `MAX_DAILY_DRAWDOWN_USDT=600` (was 1500); `SETUP_LEDGER_SINCE=2026-10-25T04:07:02+00:00` (was `2026-10-21T13:53:30+00:00`). `MAX_CONSECUTIVE_LOSSES` left at 15. |
| Validation | the new env was written to a staging copy and read back through `Settings(_env_file=...)` on the merged code with an allowlist, then installed and read back again; no pm2 process env defines any of the keys; the staging copy was deleted |
| Restart | 2026-10-25 11:07:21 WIB (12:07:21 CST, 04:07:21 UTC); `flat_check` printed FLAT before the `.env` change, after it, right before the restart and after the restart; pm2 restarts 29 -> 30 |
| Startup | `BINGX VST trade engine started.`, then `Entry window closed (mon 07:00-sat 00:00)`; no traceback |
| Prompt check | on the live configuration `GrokBrain._entry_prompt(True)` returns `lean-entry-volume-v1` (sha256 prefix `22297422db9f95d3`), and a legacy call returns `okx-context-parity-v3` |
| Stage starts for the report | `report_live_stages.py --stage-start 2026-10-21T00:18:55+07:00 --stage-start 2026-10-21T20:53:31+07:00 --stage-start 2026-10-25T11:07:21+07:00` |

Not verified yet, because the window is closed: `ai_call_metrics.prompt_version = lean-entry-volume-v1` on the first entry call, the first regime gate line, and the funnel (calls, OPEN answers, gate drops, trades). Check those on Monday after 07:00 WIB, then read the first 30 trades against H1.

## Manager's answers (25 Oct 2026)

The plain-language report of 25 Oct (section 9) put three decisions to the manager. The answers came back the same afternoon (about 15:30 WIB):

1. **Accepted result for step 2: -200 to -250 per 1M.** That is gross +3 to +2 bps per trade (net per 1M = -350 + 50 x gross bps), which sits inside the report's estimate of -250 (rough range -360 to -140) and short of H1's -150 bar. The early-stop rule stays as written: at 30 trades a mean gross below 0 rolls back (zero edge is -350, outside the accepted band). 30 trades cannot tell +2 from 0 (the standard error is about 4 to 5 bps), so the 60-trade read is the one to hold against the band. The setup ledger's hurdle is +4 bps (-150 per 1M), stricter than this tolerance: settle the hurdle before step 3 (`enforce`).
2. **The week-1 guards loosen after one week if the result is healthy.** First review Monday 2 Nov. What "healthy" means is settled at the review from that week's funnel and result (proposal: no unexplained guard trips and a result inside the accepted band), and the new values are proposed from the data then.
3. **Account 2 is retired for good** (focus on account 1). Done on the VPS at 15:43 WIB, `flat_check` on account 2 printing FLAT first: `pm2 delete ai-perp-long` and `pm2 save` (only the two account 2 entries changed in the saved list, and the live bot kept its pid and restart count), and its supervisor cron line commented out. Backups: the saved list `~/.pm2/dump.pm2.bak-acct2-retire-20261025T164322` and the crontab `~/crontab.bak-acct2-retire-20261025T164342`. Nothing else was touched (`.env`, `logs/`, data, clone, dashboard, tunnel).
