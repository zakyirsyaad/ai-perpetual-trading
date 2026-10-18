# LONG AI forward test on a second VST account — design

Date: 2026-10-18 · Status: approved 2026-10-18 · Source: the manager's decisions of 2026-10-18 (relayed
by the user) · Companion: `docs/superpowers/specs/2026-10-18-long-mirror-live-exit-design.md` (the
backtest of the same idea without the AI, "mode L"), whose §3 lists the live rules this mirrors.

## 1. Intent

The live bot shorts BTC with an AI that opens and closes positions. The AI cannot be backtested, so
the only way to learn whether an AI-run LONG adds value is to run one forward. This spec runs a
**second instance of the live bot** on a **second VST account**: the same code and the same
settings, except that it only goes LONG and only while the condition is NAIK. It starts now, in
parallel with the mode L backtest, and never touches the live SHORT bot, its account, its Supabase
rows or its forward tests.

## 2. Decisions (manager, 2026-10-18)

1. It works exactly like the live SHORT bot. The only differences: direction LONG, and entries only
   in NAIK (the document's condition selector).
2. The general limits and the weekly window are those of the mode L backtest (the document's
   `umum`): one position, 15-minute pause after an exit, at most 6 entries a day, the volatility
   guard, Mon 07:00 – Sat 00:00 WIB. Not the live bot's own limits.
3. Every AI decision is recorded with its conviction score: entries, refusals and closes.
4. Stop if after 60 positions the result is worse than −500 per 1M. Decide at 150 positions:
   continue if it is better than −250 per 1M.
5. Every week, compare it with mode L, both versions, backtested on the same dates.
6. The second account's API key: trading permission only, no withdrawal, IP whitelist on the VPS
   address where BingX offers it. The account owner creates it.
7. It starts now, not after the backtest: the AI never mixes into a rule test (the document's p. 7
   concern), and about 150 positions take weeks.

## 3. What runs

The live bot's code at a tagged release, in its own directory, with its own `.env`, pm2 app and
Supabase schema. Its settings are a copy of the live `.env` with these overrides only:

| Setting | Live SHORT | LONG forward |
|---|---|---|
| `BINGX_API_KEY` / `BINGX_SECRET_KEY` | account 1 | account 2 (typed in by the account owner) |
| `SUPABASE_SCHEMA` | `public` | `long_forward` |
| `ENTRY_GATE_DIRECTION` | `short` | `long` |
| `ENTRY_REGIME_GATE` (new) | `off` | `naik` |
| `ENTRY_VOLATILITY_GUARD_MULT` (new) | `0` (off) | `2.0` |
| `COOLDOWN_MINUTES_AFTER_EXIT` (new) | `0` (off) | `15` |
| `COOLDOWN_MINUTES_AFTER_ENTRY` / `_AFTER_STOP` | live values | `0` / `0` (the 15-minute pause replaces them) |
| `TRADE_TARGET_PER_DAY` | live value | `6` |
| `VOLUME_DAILY_TARGET_USDT`, `VOLUME_DAILY_FEE_BUDGET_USDT` | live values | `0` (no volume pacing) |
| `ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR` | live value | `0` |
| `ENTRY_WEEKLY_WINDOW` | live value | `mon 07:00-sat 00:00` |
| `VOLUME_CYCLE_STARTED_AT` | live cycle | the forward start |
| `FORWARD_*` (new, §4.4) | unset (off) | 60 / −500 / 150 |
| `TELEMETRY_ENABLED` | live value | `false` |

Everything else stays the live value: the AI provider and model, the prompt, `ai_volume`, the
stretch gate at 0.75 ATR, margin 250 × 100x (25,000 USDT), the SL and TP formulas, the neutral exit
at net +56 USDT, the 60-minute time stop, AI exits from minute 10 with the −35 USDT adverse cut.

## 4. New code (every new setting defaults to off, so the live SHORT bot behaves as today)

### 4.1 Regime gate `ENTRY_REGIME_GATE` (`off` | `naik`)

In `TradeEngine._tick`, after the window gate and before the strategy runs, so a closed gate costs
no AI call. It reads closed 1h and 4h VST klines and labels the condition with the Phase 1 rule
core (`bot/strategy/modes/regime.py`: SMA20/SMA50 1h, ATR14 1h, EMA50 4h, first match wins). Only
`NAIK` lets an entry through. The core takes numbers only; a new `bot/core/regime_gate.py` computes
the series and calls it. `tests/test_mode_backtest_structure.py` currently forbids any live module
from importing the core; it changes to allow exactly that one module. Each condition change is
logged once, and the condition goes into each AI decision's `gate` payload.

### 4.2 Volatility guard `ENTRY_VOLATILITY_GUARD_MULT`

Skips an entry when ATR14 (1h) exceeds the multiple times the median of the last 720 closed-hour
ATR14 values, the latest included, and while fewer than 720 exist (the Phase 1 definition). It needs
at least 735 closed 1h bars per check, read once per closed hour. Its verdict joins the `gate`
payload.

### 4.3 Pause after any exit `COOLDOWN_MINUTES_AFTER_EXIT`

No entry until the given minutes have passed since the last position closed, whatever the exit
reason. Kept in engine state and recomputed from the last closed trade at startup.

### 4.4 Forward guard `FORWARD_KILL_AFTER_POSITIONS`, `FORWARD_KILL_BELOW_PER_1M`, `FORWARD_REVIEW_AT_POSITIONS`

Recomputed from the schema's closed trades since `VOLUME_CYCLE_STARTED_AT` at every tick, so a
restart never clears it (like the daily risk guard). Result per 1M = 1,000,000 × Σ net PnL / Σ
volume, with the venue's fees. Once the count reaches the kill threshold, every tick checks the
result; worse than the limit sets the entries latch with an ERROR line. At the review count it
latches regardless. Open positions are still managed. Continuing past 150 means raising the review
count after the manager decides, with a restart while flat.

### 4.5 Decision records

Already in place and kept: every entry decision, HOLD included, goes to `ai_decisions` with its
`confidence` (the conviction score), systemic risk, reason and gate verdict. Every AI exit verdict,
holds included, goes to `ai_exit_verdicts` with `confidence`, whether it was honoured and why not.
Every provider call, failed ones included, goes to `ai_call_metrics`. New: the gate payload carries
the condition and the volatility verdict.

## 5. Isolation and deployment

- Directory `/home/ubuntu/ai-perp-long`, its own `.venv`, pm2 app `ai-perp-long`, its own logs. The
  live app `ai-perp-volume`, its directory and its `.env` are never touched.
- Supabase schema `long_forward`, created by the account owner in the SQL editor from the SQL the
  plan provides (the live `schema.sql` applied in that schema, grants for `service_role`) and added
  to the API's exposed schemas. The live bot's daily guards, volume and dashboard read `public`, so
  they never see these trades.
- The `.env` is built from the live one by a script that copies it and applies §3's overrides
  without printing it. The account owner types the second account's key and secret into it.
- The startup safety checks apply unchanged (VST host, isolated margin, hedge mode, leverage read
  back). The rule "never restart with an open position" applies to this app too.

## 6. Weekly comparison and reports

Every Monday, for the week before: the forward results (positions, entries per calendar day, win
rate, exit mix, result per 1M, AI exits against rule exits) beside mode L (i) and (ii) backtested on
the same dates. The backtest needs that week's production 1m klines (public endpoints, as the Pine
data path fetches them) and a `forward-compare` stage in the harness: a plain run without a lock,
since forward weeks are not a holdout. It comes after the backtest plan has built mode L; until
then the forward test runs and the comparisons are filled in afterwards. Reports to the manager are
in Indonesian. The 60-position check and the 150-position decision each get their own report.

## 7. Testing

TDD with stdlib unittest, evidence in `.github/tdd/long-ai-forward-test.tdd.md`, coverage ≥ 80 %.
Each new gate has RED tests, among them the regime gate's labels on hand-built 1h/4h series
matching the Phase 1 core, and the volatility guard below 720 values. A defaults test runs one tick
with every new setting unset and pins that the decision path is unchanged. The structure test pins
the single allowed live import of the core. Nothing in the tests calls the venue or Supabase.

## 8. Risks and limits

- **AI cost and quota.** A second bot roughly doubles AI spend, and if it shares the provider key
  its 429s can delay the live bot's calls.
- **Regime from VST candles.** The backtest labels the condition on production candles. VST prices
  follow production closely but not exactly, so an hour near a threshold can differ.
- **The prompt still tells the AI to pick a direction every tick**, and the direction gate drops
  SHORT picks, as it drops LONG picks in the live bot. Many ticks will end in a blocked SHORT.
- **150 positions at a few entries per NAIK day** take several weeks, longer if the market is not
  rising.
- **The second account's fee** must be read from the startup log; the live account pays 0.035 %.

## 9. Decided with the user (2026-10-18)

1. **Daily risk guards** (`MAX_STOP_LOSS_PER_DAY`, `MAX_DAILY_DRAWDOWN_USDT`,
   `MAX_CONSECUTIVE_LOSSES`): off, set out of reach, so the limits match the backtest; the
   forward guard of §4.4 is the stop.
2. **The kill check**: at every close from the 60th position on, not only once at 60.
3. **AI provider key**: a separate key for this bot, so its quota never touches the live bot.
