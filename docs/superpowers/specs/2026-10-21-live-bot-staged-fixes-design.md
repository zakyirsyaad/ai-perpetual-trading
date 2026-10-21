# Live bot (account 1): staged fixes from the forward evidence — design

Date: 2026-10-21 · Status: draft for the user's review · Evidence: forward analysis of every real
BingX trade of account 1 (519 trades, 31 Aug – 20 Oct 2026) and 1,084 AI entry decisions, project
memory `forward-deep-analysis-2026-10-21`, plus a read-only review of the AI pipeline. Companion
reports (Indonesian): `docs/research/2026-10-20-analisis-kerugian-trade.md`.

## 1. Intent

The user wants account 1 (pm2 `ai-perp-volume`, the live SHORT bot) to lose less, trade in both
directions where the market supports it, and learn from its own mistakes. The user ruled that
**forward results, not backtests, decide**. Changes go in **stages**, each measured forward before
the next, so the data can tell which stage helped.

Stage 0 is done: AI exits are off on account 1 since 2026-10-21 00:18:55 WIB
(`AI_EXIT_MAX_CALLS_PER_TRADE=0`, restart while flat). Exits are now the stop, the neutral floor
(+56 net), the take-profit and the 60-minute time stop.

This spec covers the three code stages that follow:

- **Part A – honest AI entry inputs** (`AI_ENTRY_PAYLOAD`).
- **Part B – two-way regime gate** (`ENTRY_REGIME_GATE=map`): SHORT only in TURUN, LONG only in
  NAIK, with a side that may run in shadow.
- **Part C – binding setup ledger** (`SETUP_LEDGER_MODE`): learning from mistakes in code, not in
  prompt text.

Every new setting defaults to today's behaviour, so merging the code changes nothing until a
setting is switched. The LONG forward bot (account 2) runs a pinned tag and is not affected.

## 2. Forward evidence the design rests on

Gross is before fees, in bps of notional per trade. At today's fee (0.035 % per leg) net per 1M =
50 × gross − 350; −150 per 1M needs +4 bps, break-even +7 bps.

| Finding (account 1, BTC) | n | Gross per trade |
|---|---|---|
| AI LONG, near the mean | 101 | −4.7 ± 2.3 |
| AI LONG, chasing (> 0.75 ATR) | 82 | −7.0 ± 2.7 |
| AI SHORT, chasing | 93 | −8.0 ± 2.4 |
| AI SHORT, near the mean | 175 | +2.0 ± 1.6 |
| AI SHORT in TURUN (October) | 58 | +6.1 ± 3.1 |
| Rules-only exits vs actual (same entries, one price source) | 519 | +2.1 ± 1.0 better |

- October (SHORT only, stretch gate, new fee) is −282 per 1M with gross +75; September was −838.
- AI LONG loses in every period; LONG decisions blocked since 10 Oct are negative too.
- Confidence ≥ 0.78 did worse than < 0.78: the score carries no information.
- Written lessons in the prompt did not change behaviour (chasing rose 57 % → 72 % after they
  appeared, memory `llm-prompt-diagnosis`).

## 3. What is wrong in the code today (verified)

1. **Conflicting objective.** The system prompt allows HOLD (`ai/brain.py:145,151`), the payload
   says "opening a directional trade every tick" and "do not sit out"
   (`ai/orchestration.py:303,307`).
2. **The model does not know the rules it is judged by.** `allowed_actions` is always
   `OPEN_LONG, OPEN_SHORT, HOLD` (`ai/compact_context.py:89`) although only SHORT can trade; the
   stretch gate (`orchestration.py:583-603`) runs after the call and is never mentioned; the
   confidence floor is printed (`compact_context.py:136`), so answers sit at 0.75–0.77.
3. **No higher-timeframe context.** Every technical comes from 3m candles; the 1h/4h regime verdict
   is stored (`orchestration.py:676`) but never sent.
4. **Broken factor library.** 81 of the 86 arena factors are one formula per category
   (`arena/factors.py:398-413`). Trend factors are `ema_gap × k + macd_hist` with the MACD
   histogram in USD, so they saturate at ±1 and flip together on a 3m MACD sign change; every
   volatility factor is `1 − |atr% − c|`, positive at BTC's ATR, so it **always votes LONG**
   (`factors.py:406-408`, direction threshold `factors.py:189-195`). Percentiles of a single symbol
   are always 0.0 or 1.0 (`factors.py:208-217`, `arena/flow_signals.py:76-88`).
5. **Lessons cannot bind.** `trade_memory` is advisory text over the last 40 trades
   (`ai/trade_memory.py:11-12`); the model is never told which bucket the current setup is in.

## 4. Part A — honest AI entry inputs

Setting `AI_ENTRY_PAYLOAD` (`legacy` | `lean`, default `legacy`). `legacy` is byte-for-byte
today's payload. `lean` changes the entry request only (the exit AI is off and out of scope):

- **Objective.** `primary_goal`: "Open a position only when the setup shows a directional edge
  larger than the round-trip fee; HOLD is the expected answer most ticks." The "every tick" and
  "do not sit out" lines are removed. The volume profile stays as information, pacing stays absent.
- **Allowed actions.** `output_rules.allowed_actions` = `["OPEN_<side>", "HOLD"]` for each side the
  regime gate maps this hour (Part B), the shadow side included so its calls can be measured; with
  the regime gate off, the side `ENTRY_GATE_DIRECTION` allows (both for `any`).
- **Entry rules block** (`entry_rules`): the current `stretch_atr` computed in code before the call
  (`build_setup_profile`, the same number the stretch gate uses), the limit
  `ENTRY_STRETCH_GATE_MAX_ATR`, and the sentence "an entry with |stretch_atr| above this limit is
  rejected in code".
- **Boot guard.** `lean` refuses to boot unless `ENTRY_STRETCH_GATE_MODE=enforce`,
  `ENTRY_STRETCH_GATE_MAX_ATR` > 0 and `ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR=0`, so
  the sentence above and the narrowed `allowed_actions` describe what the running bot enforces.
- **Regime block** (`market_regime`): the regime gate's verdict with its numbers — condition, 1h
  SMA20 and SMA50, 1h ATR14 and its 720-hour median, 4h close and EMA50, and the distance of price
  from the 1h SMA20 in 1h ATR. `RegimeVerdict` gains the fields it lacks (`sma20_1h`, `sma50_1h`,
  `close_4h`, `ema50_4h`). Absent when the gate is off.
- **Features instead of factors.** Removed: `factor_snapshots`, `active_flow_signals`,
  `new_flow_signals`, `intelligence.symbol_scores`, `intelligence.factor_intelligence`,
  `intelligence.market_flow_signals`, `arena_reviews`, `learned_trade_memory`. Kept: `technicals`
  (raw 3m numbers), the pruned `external_signals`, `liquidation_constraints`, the order book and
  funding fields. Added `features`, computed in code from closed 3m candles: stretch in ATR, EMA21
  slope over the last 5 bars in ATR, RSI14, price change in % as `change_pct_5_bars` and
  `change_pct_20_bars` with a `bar_interval` field (15 and 60 minutes on 3m candles), ATR14 in %.
- **Confidence.** `risk_constraints.min_confidence` is removed from the payload; the code still
  enforces `AI_MIN_CONFIDENCE`.
- **Ledger status** (Part C enforcing): the bucket status of each allowed side, one line each. Absent
  while the ledger is `off` or `shadow`, because `shadow` still trades a `blocked` bucket (final review).

The arena still runs and is still recorded; only what reaches the model changes. Every decision
records `payload_json.payload_profile` = `legacy` or `lean`.

*PR #46 review:* the lean request is sent under its own system prompt, `ENTRY_SYSTEM_PROMPT_LEAN`
(same role, output contract and JSON/risk-review rules as the legacy prompt; no volume mandate, no
instruction to use recent performance, trade memory, factors or flows; HOLD is the expected answer
most ticks; it names `features`, `entry_rules`, `market_regime` and `setup_ledger` as the inputs),
and its calls record `ai_call_metrics.prompt_version` = `lean-entry-v1`. The legacy prompt and
`COMPACT_PROMPT_VERSION` are unchanged.

## 5. Part B — two-way regime gate

`ENTRY_REGIME_GATE` gains the value `map` (`off` | `naik` | `map`); `naik` is unchanged.

- **Mapping per closed hour** (same selector, candles and cache as `naik`): NAIK → `long`,
  TURUN → `short`, SIDEWAYS and TIDAK_ENTRY → none. The volatility guard applies as in `naik`.
  Reasons: `naik`, `turun`, `not_mapped:<CONDITION>` plus the existing data reasons.
  `RegimeVerdict` gains `allowed_direction` (`long`, `short` or `""`).
- **No mapped side, no AI call.** The tick returns at gate 10 as `naik` does today
  (`core/trade_engine.py:293-300`).
- **Trade or shadow.** `ENTRY_GATE_DIRECTION` keeps its meaning — the sides that may actually
  trade. With `map`:
  - decision side = mapped side, and `ENTRY_GATE_DIRECTION` allows it → the stretch gate runs as
    today and the entry may trade;
  - decision side = mapped side, but `ENTRY_GATE_DIRECTION` excludes it → **shadow**: the stretch
    verdict and the plan's stop and floor are computed and recorded, the model's decision is kept
    unchanged in `ai_decisions` (`payload_json.gate.shadow_side=true`), the candidate is dropped so
    the tick holds, and a `shadow_entries` row is written (Part C evaluates it);
  - decision side ≠ mapped side → blocked, `reason=direction`, `forced_by=regime_gate`.
- **Validator.** `map` needs `ENTRY_STRETCH_GATE_MODE=enforce` and an AI strategy, like `naik`;
  `ENTRY_GATE_DIRECTION` may be `any`, `short` or `long`. `naik` still requires `long`. The
  single-symbol rule in `build_regime_gate` holds for `map`.
- **First use on account 1** (decision D2): `ENTRY_REGIME_GATE=map`, `ENTRY_GATE_DIRECTION=short`.
  SHORT trades only in TURUN; LONG in NAIK runs in shadow until its own forward evidence reaches
  the bar in §8.

## 6. Part C — binding setup ledger

Setting `SETUP_LEDGER_MODE` (`off` | `shadow` | `enforce`, default `off`), with
`SETUP_LEDGER_SINCE` (timezone-aware ISO time, required when not `off`; trades before it are
ignored, so no AI-exit trade mixes in; *second PR #46 review:* "before it" means opened before it,
even when closed after, and a time more than 5 minutes ahead is refused, since it would leave every
bucket `learning` with nothing logged), `SETUP_LEDGER_MIN_SAMPLES` (default 60) and
`SETUP_LEDGER_HURDLE_BPS` (default 4.0, the −150 per 1M bar at 0.035 %).

- **Bucket key** = `side|condition|atr_regime`, e.g. `SHORT|TURUN|atr_mid`. The condition comes from
  the regime verdict; `atr_regime` from `build_setup_profile`. Both are written into
  `factor_metadata.setup` at entry (`kondisi`, `bucket_key`), so a closed trade carries its bucket.
- **Real outcomes.** For each closed trade since `SETUP_LEDGER_SINCE`: gross bps from entry and
  exit prices. Read from the store once per closed trade, cached in memory between closes.
  *PR #46 review:* as built, the snapshot (trades and `ledger_block` outcomes) is read at most once
  per `LEDGER_CACHE_SECONDS` = 300 s and held in memory, so a status can lag a close by up to 5
  minutes; a restart reads it afresh. *Second PR #46 review:* a tick that returns a plan expires
  the snapshot, so the next strategy call reads it again: with no entry cooldown (account 1) a trade
  can open and close inside the 300 s, and the cache must not hide it.
- **Shadow outcomes.** Table `shadow_entries` (new, in `supabase/schema.sql`; the `long_forward`
  copy is regenerated, the drift test pins it): decision time, symbol, side, bucket, reference
  price, stop %, floor %, hold minutes, source (`shadow_side` or `ledger_block`), and the outcome
  columns `gross_bps`, `exit_reason`, `evaluated_at`. Each tick, right after the entries-latch check
  (gate 1) and before the other gates, so closed windows and unmapped hours still evaluate, the
  engine evaluates at most 3 rows older than hold + 1 minutes: it reads the venue's 1m candles from the decision
  minute on and walks them like the live exit rules (stop first when a candle touches both, stop
  filled 1 bp worse, time exit at the last close). A read failure leaves the row for the next tick
  and is logged at WARNING; it never blocks trading.
  - *PR #46 review:* a row expires only when the venue served a full window (at least the limit
    asked for minus one, since the forming candle may be dropped) that no longer reaches the
    decision-minute candle. A shorter answer that misses it did not honour the limit, so it is a
    venue failure: the row stays pending and the loop stops for the tick. *Re-review:* a row
    `CANDLE_WINDOW` (1000) minutes old or more is out of reach of any window, so it expires whatever
    the answer length; otherwise one old row would stall every newer row behind it.
  - *Final review:* the walk models the floor neutral exit, so `Settings` refuses
    `ENTRY_REGIME_GATE=map` or a ledger mode other than `off` unless `VOLUME_NEUTRAL_EXIT_ENABLED=true`
    and `VOLUME_NEUTRAL_EXIT_MODE=floor` (`naik` with the ledger off is not checked). *PR #46 review:*
    the floor is 100 × `VOLUME_NEUTRAL_EXIT_MIN_NET_USDT` / (ai_volume margin × leverage) plus the
    round-trip fee, a net threshold on the `ai_volume` notional, so the same check also requires
    `VOLUME_NEUTRAL_EXIT_BASIS=net` and `STRATEGY_MODE=ai_volume` (one error names everything missing).
    *Second PR #46 review:* the fee is the venue's taker rate the live neutral exit nets
    (`TradeEngine._taker_fee_pct`, handed to the strategy after the startup fee refresh), not the
    `TAKER_FEE_PCT` estimate in the candidate's targets: on an account that reads the venue rate
    (0.035 % against 0.05 %) the floor sat about 3 bps further than the live one. Account 1 runs
    `USE_EXCHANGE_FEE_RATES=false`, so its live exit and its floor both net the 0.05 % estimate.
  - *Final review:* rows are spaced at write time. No new row is written for the same (symbol, side,
    source) while the last one written for it was decided less than `POSITION_HOLD_MINUTES` +
    `COOLDOWN_MINUTES_AFTER_ENTRY` minutes earlier, so walks do not overlap and n is not inflated. A
    skipped candidate is still dropped (one INFO line); a failed write does not start the spacing; the
    times are kept in memory, so a restart forgets them.
  - *PR #46 review:* the spacing key is (symbol, side, source, `bucket_key`), so two buckets of one
    side are spaced apart. *Re-review:* only for `ledger_block` rows (a bucket reopens on its own
    block outcomes). `shadow_side` rows of every bucket share one spacing, because the LONG go-live
    bar pools them and two rows a minute apart in neighbouring ATR buckets would walk the same hour. A key with no time in memory is seeded once per process from the store's
    newest row for it (any evaluation state), so a restart keeps the spacing; a failed seed read is
    a WARNING and the row is written. Under `map`, an hour whose mapped side `ENTRY_GATE_DIRECTION`
    does not make live can trade nothing, so while every symbol's `shadow_side` row for that side (any
    bucket) is inside its spacing the tick returns after the context is built and before the AI call
    (one INFO line). *Second PR #46 review:* the same skip covers an enforcing ledger's blocked live
    side (the mapped side under `map`, the configured one under `naik`): its answer could only become a
    `ledger_block` row, so while that row is inside its spacing no call is made. The ledger is read
    before the check.
- **Verdict per bucket** (pre-registered):
  - `blocked` when real n ≥ `MIN_SAMPLES` and mean + 1.28 × SE < `HURDLE_BPS` (90 % one-sided:
    the bucket cannot reach the bar);
  - a blocked bucket is `reopened` when its shadow outcomes since the block reach n ≥ `MIN_SAMPLES`
    and mean + 1.28 × SE ≥ `HURDLE_BPS`;
  - *final review:* a reopened bucket is judged again on its real trades opened (`opened_at`) after
    its latest `ledger_block` row's `decided_at`: once those reach n ≥ `MIN_SAMPLES` with
    mean + 1.28 × SE < `HURDLE_BPS` it is `blocked` again, otherwise it stays `reopened`;
  - *episode scope (after the final review):* "since the block" means the bucket's latest blocked
    episode. Its counted real trades (by `opened_at`) and its evaluated `ledger_block` rows (by
    `decided_at`) share one timeline; expired and invalid rows carry no outcome and only mark time.
    The reopening sample is the run of block rows decided after the last real trade opened at or
    before the latest block row (every block row in the first episode, as before; a tie ends the
    run); the re-judging sample is the real trades opened after the latest block row. So a
    re-blocked bucket stays `blocked` until a fresh run reaches n ≥ `MIN_SAMPLES` with
    mean + 1.28 × SE ≥ `HURDLE_BPS`, and an expired or invalid row can never reopen it. Before this,
    the next evaluated block row of a re-blocked bucket moved the re-judging cutoff past every real
    trade while the old shadow sample still counted, so the bucket read `reopened` again about an
    hour later. A row or trade without a readable time is off the timeline (the store writes both
    `NOT NULL`). Any real trade filed in a blocked bucket ends its run, so a rollback from `enforce`
    to `shadow` (where a blocked bucket still trades) restarts that bucket's episode. Stateless: the
    same two reads per tick;
  - *episode review:* the stored verdict adds `shadow_run_n`, `shadow_run_upper_bps`,
    `since_block_n` and `since_block_upper_bps`, so a decision row shows why its bucket is `blocked`
    or `reopened`; and a failed ledger read reuses the last snapshot read without error (in memory),
    so a transient store error lets no entry into a blocked bucket and so cannot end its run. Before
    the first good read the ledger still fails open (every bucket `learning`); *PR #46 review:* the
    reuse holds whatever the snapshot's age, a failed read is never cached, and falling open is an
    ERROR under `enforce` (a WARNING under `shadow`);
  - otherwise `open`, with `learning` while n < `MIN_SAMPLES`.
- **Mode.** `shadow`: the verdict is recorded at `payload_json.gate.ledger` and changes nothing.
  `enforce`: the candidate of an entry in a `blocked` bucket is dropped in code (the tick holds; the
  decision row keeps the model's action, with `gate.ledger.status=blocked`), logged, and written to
  `shadow_entries` with source `ledger_block`, so the bucket can earn its way back.
- **What the model sees** (lean payload, `enforce` only): per allowed side, `bucket`, `n`, mean gross
  bps and status. Facts about the setup in front of it, not a list of old lessons. Under `shadow` the
  block is absent, so the model is never shown a status the code does not apply.
- `learned_trade_memory` stays in the legacy payload; the lean payload drops it (§4).

## 7. Report

`src/report_live_stages.py --stage-start <ISO> [--stage-start <ISO> ...]`: for each stage window,
trades, win rate, gross bps ± SE, net per 1M, volume per day; the shadow table by side and bucket;
the ledger table. Read-only, prints Indonesian headings like the other reports. *PR #46 review:* the
shadow table groups by source as well (`shadow_side` and `ledger_block` never share a mean), and the
ledger table reads the engine's window, every trade and `ledger_block` row since
`SETUP_LEDGER_SINCE` up to `--until`, named in its heading (with it unset, the stage rows, and the
heading says the ledger is off).

## 8. Rollout and measurement

Each step is a settings change and a restart while flat, after the code is merged with every
setting at its default.

| Step | Settings | Measure before the next step |
|---|---|---|
| 0 (done 21 Oct) | `AI_EXIT_MAX_CALLS_PER_TRADE=0` | 60 trades |
| 1 | `ENTRY_REGIME_GATE=map`, `ENTRY_GATE_DIRECTION=short`, `SETUP_LEDGER_MODE=shadow` | 60 SHORT trades; LONG shadow rows accumulate |
| 2 | `AI_ENTRY_PAYLOAD=lean` | 60 trades |
| 3 | `SETUP_LEDGER_MODE=enforce` | ongoing |

- A step is judged on gross bps per trade versus the previous step, with its SE, and on volume per
  day. A step that is worse by more than one SE is rolled back.
- **LONG goes live** (`ENTRY_GATE_DIRECTION=any`) only when the LONG shadow outcomes reach n ≥ 60
  with mean gross ≥ +4 bps. Until then LONG costs only AI calls.
- 60 trades at about 2–4 SHORT trades a day (TURUN only) takes 2–4 weeks per step.

## 9. Testing

Stdlib `unittest`, TDD, coverage ≥ 80 %, evidence in `.github/tdd/live-staged-fixes.tdd.md`.

- Regime gate `map`: each condition's `allowed_direction`, reasons, volatility guard, cache, and
  `naik` unchanged.
- Stretch gate with `map`: trade, shadow and direction-block paths; the shadow path never returns a
  plan; `forced_by` recorded.
- Validators: `map` with `short`, `long`, `any`; refusals without `enforce` or an AI strategy.
- Lean payload: removed keys absent, kept keys present, `allowed_actions` per mapped side, regime
  and rules blocks, no `min_confidence`; legacy payload identical to today's (golden test).
- Ledger maths: bounds, block and reopen thresholds, `learning` below n, buckets from
  `factor_metadata`.
- Shadow evaluator: stop first, floor, time exit, at most 3 per tick, venue failure leaves the row.
- Schema drift test for `shadow_entries`; dashboard contract unchanged.

## 10. Risks and limits

- **Volume falls.** TURUN covered about a quarter of the hours since 31 Aug; SHORT outside TURUN is
  gone. Expect roughly half of October's trade count, far below 1M per day. Decision D1 asks the
  user to accept fewer trades for a better result per 1M.
- **Small samples.** Each step's verdict rests on about 60 trades (SE about 2 bps); the ledger
  needs 60 per bucket and will mostly read `learning` for weeks.
- **Shadow outcomes are simulated** on venue candles with the live exit rules; with AI exits off
  they follow the same rules the real trades use.
- **The lean payload changes many inputs at once.** It is one step on purpose (the parts depend on
  each other); its effect is measured as one.
- **AI tokens.** LONG shadow calls cost tokens in NAIK hours; the lean payload is much smaller than
  today's ~21k tokens per entry call.

## 11. Decisions for the user

- **D1 — objective.** Remove "trade every tick / do not sit out" (fewer trades, volume further below
  1M per day). Proposed: yes.
- **D2 — LONG on account 1.** Shadow first, live after n ≥ 60 at ≥ +4 bps. Proposed: shadow first.
- **D3 — step order.** Regime map with the ledger in shadow first, then the lean payload, then the
  ledger enforcing. Proposed: this order, because the payload and the ledger need the map's
  condition.
- **D4 — ledger bar.** n ≥ 60, 90 % one-sided bound against +4 bps. Proposed: as written.

## 12. Out of scope

- The exit AI (off). Re-enabling it needs its own spec: the entry thesis, stop, floor and net in its
  payload, and a higher-timeframe invalidation rule.
- The manager's Pine script: waiting for the original script and its Strategy Tester properties to
  re-measure it with our fees and fills.
- Account 2 (the LONG forward bot) and its weekly comparison.
