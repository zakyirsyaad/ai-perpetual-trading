# LONG AI forward test: runbook

The LONG forward bot is a second instance of this repo. It runs the same AI pipeline as the live SHORT bot, restricted to
LONG entries while the hourly regime reads NAIK, on its own BingX VST account, its own Supabase schema and its own
directory. Spec: `docs/superpowers/specs/2026-10-18-long-ai-forward-test-design.md`. Plan:
`docs/superpowers/plans/2026-10-19-long-ai-forward-test.md`. Evidence: `.github/tdd/long-ai-forward-test.tdd.md`.

**Status (2026-10-25): retired.** The operator stopped the bot on 2026-10-25 at 11:31 WIB (`pm2 stop ai-perp-long`, flat before and after). At 15:43 WIB, on the manager's answer to stop account 2 completely, it was removed from pm2 and its saved list. Section 6 says why, what was kept and how to start it again; sections 2 to 4 describe the bot while it runs. Section 7 is the procedure for a second start on account 1's latest code and settings with LONG (prepared 2026-10-28, not started).

Ground rules:

- The live app `ai-perp-volume` (dir `/home/ubuntu/ai-perpetual-trading`, its `.env`, its pm2 entry) is never touched.
  The builder below only reads its `.env`.
- Nobody pastes a key into a chat, a ticket or a tracked file. The account owner types the three secrets into the new
  `.env` on the VPS. Every command below prints key names, never values.
- Every command uses `PYTHONPATH=src`.
- All times are WIB (UTC+7) unless stated.

## 1. Account owner

1. **BingX second account, API key.** Permissions: trade only. No withdraw, no transfer. IP whitelist: `43.129.56.85`
   only. The key stays on the VST host (`https://open-api-vst.bingx.com`), as with the live bot.
2. **A separate AI key** for this bot (spec 9.3), so its token spend and rate limits are its own.
3. **Supabase, schema `long_forward`.** In the SQL editor of the project the live bot uses, run the contents of
   `supabase/long_forward_schema.sql` (generated from `supabase/schema.sql`; it creates the schema, the tables and the
   `service_role` grants).
4. **Expose the schema.** Supabase, Settings, API, Exposed schemas: add `long_forward`. Without this PostgREST answers
   the bot's `Accept-Profile: long_forward` header with an error, and nothing is written.
5. If `supabase/schema.sql` gains a table later, regenerate the file
   (`PYTHONPATH=src python src/forward_schema.py --schema long_forward > supabase/long_forward_schema.sql`, which
   `tests/test_forward_schema.py` checks for drift) and run it again. The file holds no `alter default privileges`, so a
   table added later has no grants until the new SQL runs.
6. Type the three secrets into the new `.env` (section 2, step 5): `BINGX_API_KEY`, `BINGX_SECRET_KEY`, `XAI_API_KEY`.

## 2. Operator (first deployment)

Starts only on the user's go-ahead (plan Task 9), on merged `main`.

1. **Pin the code with a tag** (spec section 5: the live bot's code at a tagged release). After this branch is merged
   to `main`, the operator creates the tag on the merge commit from a machine with push rights (nobody creates it
   automatically):
   ```bash
   git fetch origin && git tag -a long-forward-v1 -m "LONG AI forward test, first deployment" origin/main
   git push origin long-forward-v1
   ```
   Then clone into its own directory at that tag and give it its own venv:
   ```bash
   git clone <repo url> /home/ubuntu/ai-perp-long && cd /home/ubuntu/ai-perp-long
   git checkout long-forward-v1   # detached HEAD on purpose: the forward bot runs exactly this release
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```
   A later code change reaches the forward bot only as a new tag (`long-forward-v2`, ...), checked out while the account
   is flat (section 4) and recorded with the report of the week it starts in.
2. Build the `.env` from the live one. The source is only read; the target must not exist; the file is created with mode
   600. Pick the cycle start now (a timezone-aware ISO-8601 instant, not more than 5 minutes in the future):
   ```bash
   PYTHONPATH=src .venv/bin/python src/build_forward_env.py \
     --source /home/ubuntu/ai-perpetual-trading/.env \
     --target /home/ubuntu/ai-perp-long/.env \
     --cycle-start "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
   ```
   It prints the key names it set and the three it blanked. Show that output to the user. Among the keys it forces are
   `ENTRY_STRETCH_GATE_MODE=enforce` and `STRATEGY_MODE=ai_volume`: the LONG-only filter (`ENTRY_GATE_DIRECTION=long`) runs
   only inside the enforcing stretch gate of an AI strategy, so `Settings` refuses to boot with `ENTRY_REGIME_GATE=naik`
   unless all three hold (the error names the keys).
3. **`VOLUME_CYCLE_STARTED_AT` must be the forward start** (this `--cycle-start`). The forward guard counts closed trades
   since that instant in the configured schema; an earlier cutoff would pool older trades into the 60 and 150 counts.
   Never edit it afterwards.
4. Review the key names that came over from the live `.env` and were not forced. Names only: python-dotenv parses the file
   the way the bot does (quoted multi-line values, `export NAME=...`, comments) and only the names are printed, so no value
   and no continuation line of a quoted value ever reaches the terminal. A bare token with no `=` has no value and is
   skipped. Run it from `/home/ubuntu/ai-perp-long`:
   ```bash
   .venv/bin/python -I -c "import sys; from dotenv import dotenv_values; print('\n'.join(sorted(k for k, v in dotenv_values(sys.argv[1]).items() if v is not None)))" /home/ubuntu/ai-perp-long/.env
   ```
   Values the second account needs to differ on are the operator's call. **Decide on the external-provider keys here:**
   the builder copies `COINMARKETCAP_API_KEY` and `DUNE_API_KEY` from the live `.env` when it has them, so both bots then
   call those providers on the same quota (external calls double, and an exhausted quota prunes the provider from the live
   bot's payload too). Choose one on purpose: keep them (accept the shared quota), give the forward bot its own keys, or
   blank them in the forward `.env` (the provider is then absent from its payload).
5. The account owner types `BINGX_API_KEY`, `BINGX_SECRET_KEY` and `XAI_API_KEY` into `/home/ubuntu/ai-perp-long/.env`
   with an editor. `chmod 600 .env` stays.
6. **Process environment overrides `.env`** (pydantic-settings reads the environment first). The pm2 app must not export
   any forced key (`SUPABASE_SCHEMA`, `BINGX_*`, `XAI_*`, `ENTRY_*`, `COOLDOWN_*`, `FORWARD_*`, `VOLUME_*`, `TRADE_*`,
   `MAX_*`, `TELEMETRY_*`, ...). Two read-only checks that print names only, never values:
   - **Before the first start**, in the shell you will start the bot from (foreground or pm2). It must print nothing. If a
     name shows up, `unset NAME` first.
     ```bash
     env | cut -d= -f1 | grep -iE '^(SUPABASE_|BINGX_|XAI_|ENTRY_|COOLDOWN_|FORWARD_|VOLUME_|TRADE_|MAX_|REGIME_|TELEMETRY_|LEVERAGE$|SYMBOLS$)'
     ```
   - **After `pm2 start`** (it fails with an IndexError before the app exists). It must print `[]`:
     ```bash
     pm2 jlist | python3 -c 'import json,re,sys
     app=[p for p in json.load(sys.stdin) if p["name"]=="ai-perp-long"][0]["pm2_env"]
     print(sorted(k for k in app if re.match(r"(SUPABASE_|BINGX_|XAI_|ENTRY_|COOLDOWN_|FORWARD_|VOLUME_|TRADE_|MAX_|REGIME_|TELEMETRY_|LEVERAGE$|SYMBOLS$)", k, re.I)))'
     ```
7. **First start, outside the entry window, in the foreground.** The entry window is `mon 07:00-sat 00:00` in
   `TRADING_TIMEZONE` (default `Asia/Jakarta`, WIB). Start only between **Saturday 00:00 and Monday 07:00 WIB**, and finish
   steps 7 and 8 before Monday 07:00.

   Why outside the window, and why it is safe: `run_forever` runs the startup checks, then ticks at once, and a tick that
   opens a position blocks until it closes. In `_tick` the window gate comes right after the entries latch and before the forward
   guard, the risk guard, the cooldowns, the regime gate and the strategy, so while the window is closed no AI call
   and no order can happen. Inside the window a NAIK hour could open a real position within minutes, and the handover to
   pm2 would then be a restart with an open position (`104103` crash loop, section 4). The alternative of skipping the
   foreground run and starting under pm2 straight away is less safe: with autorestart a bad key or a wrong `.env` becomes a
   crash loop that hammers the venue, while a foreground run just exits.
   ```bash
   cd /home/ubuntu/ai-perp-long && PYTHONPATH=src .venv/bin/python src/main.py
   ```
   What you will see: isolated margin, hedge mode, leverage read back, the fee log (trust it over the config default),
   `VST trade engine started`, the schema being reachable (no PostgREST error), and `Entry window closed (mon 07:00-sat
   00:00): no new entries until it opens again.` (that line confirms the forced window parsed and that the timezone puts you
   outside it). If it does not appear, the window is open: Ctrl-C at once and check the venue for a position or order before doing anything else.
   What you will not see: the regime gate sits after the window gate, so its first verdict (`Regime gate ...`) is not
   logged until the window opens. `main.py` refuses `ENTRY_REGIME_GATE=naik` with more than one symbol in `SYMBOLS` (the
   forward bot is one symbol; `build_forward_env.py` forces `SYMBOLS=BTC/USDT`); a refusal is one error line and exit 1, visible now. Read the first verdict in the pm2 error log on Monday after
   07:00 and the guard state from the first report.

   Stop it with Ctrl-C after reading the lines. Before stopping, confirm on the venue that the account is flat (the BingX VST
   app shows no position or open order; with the window closed there can be none).
8. **Then pm2, still outside the window.** The foreground to pm2 handover is itself a restart, so the same rule applies:
   flat on the venue first (confirmed in step 7), then start. The plan's command is kept, with three changes: the
   interpreter and cwd are absolute, `PYTHONPATH=src` is set (the repo has no `sys.path` fix, so `src/main.py` fails to
   import `bot` without it; `ecosystem.config.js` sets it the same way for `ai-perp-volume`), and `--restart-delay
   10000` mirrors that file's `restart_delay`.
   ```bash
   cd /home/ubuntu/ai-perp-long && PYTHONPATH=src pm2 start src/main.py --name ai-perp-long \
     --interpreter /home/ubuntu/ai-perp-long/.venv/bin/python --cwd /home/ubuntu/ai-perp-long --restart-delay 10000
   pm2 status    # ai-perp-volume must still be online and unrestarted
   ```
   Then run the "after `pm2 start`" check of step 6. Logs go to stderr, so they are in
   `~/.pm2/logs/ai-perp-long-error.log`; the same startup lines should be there.
9. **Supervisor cron** (the watchdog for the entries latch, `src/supervisor.py`; the same five-minute cadence the live bot's
   cron uses, see the script's docstring). It must run with the forward bot's directory as cwd, because the default state
   file `logs/supervisor_state.json` is relative, and it needs `--app` and `--log` for this bot (the defaults point at
   `ai-perp-volume`). First once by hand, `--dry-run` (decides and reports, never restarts), and read the output:
   ```bash
   cd /home/ubuntu/ai-perp-long && mkdir -p logs && PYTHONPATH=src .venv/bin/python src/supervisor.py \
     --app ai-perp-long --log /home/ubuntu/.pm2/logs/ai-perp-long-error.log --dry-run
   ```
   Then add this line with `crontab -e` (cron does not expand `~`, so the log path is absolute; do not touch the live bot's
   line):
   ```
   */5 * * * * cd /home/ubuntu/ai-perp-long && PYTHONPATH=src .venv/bin/python src/supervisor.py --app ai-perp-long --log /home/ubuntu/.pm2/logs/ai-perp-long-error.log >> logs/supervisor.log 2>&1
   ```
   It restarts `ai-perp-long` only when the venue shows no position and the order lookup finds nothing, and at most once per
   order id; every other case it leaves for a person. It reads the forward bot's `.env` (cwd) for the account-2 keys.
10. Record the cycle start and the first Monday in `CLAUDE.md` and in memory (plan Task 9).

Never touch `ai-perp-volume`. Never run `pm2 restart all`, `pm2 delete all` or `pm2 stop all`.

## 3. Weekly (every Monday, for the week before)

The bot has been retired since 2026-10-25 (section 6): the steps below apply again once it runs. The report for `--from 2026-10-19 --to 2026-10-26` can still be produced from the kept data.

Choose `FROM` (the previous Monday) and `TO` (this Monday, exclusive), WIB dates. The entry window is closed from Saturday
00:00 to Monday 07:00, so by Monday every position of the week has closed.

1. **Report and export** (in the forward bot's directory, so it reads that `.env`; read-only against Supabase):
   ```bash
   cd /home/ubuntu/ai-perp-long && PYTHONPATH=src .venv/bin/python src/report_forward_test.py \
     --from FROM --to TO --out-dir ~/forward_weeks
   ```
   It prints `VOLUME_CYCLE_STARTED_AT`, the week's summary and the guard state (whole cycle) and writes the trades CSV
   (never overwritten). Two `PERINGATAN` lines can appear and each needs an answer before the numbers go to the manager:
   `--from` earlier than the cycle start (the store reads only trades closed since the cycle start, so the early days
   are silently missing: use `--from` on or after it), and `N posisi bukan LONG` (the forward bot is LONG-only, so a
   SHORT row means the side filter failed: stop and look).
2. **A separate forward data dir, one per week.** `compare-forward` refuses a `--data-dir` that holds `holdout_runs/`
   (that is the Phase 1 one-shot data dir, exit 2), so never point it at `~/replay/mode_backtest/data` or
   `~/replay/pine_gate/data`. `fetch` and `warmup` skip a file that already exists, so reuse of last week's dir would keep
   last week's warm-up: use a new dir each week. Both commands take the global `--data-dir` before the subcommand (default
   `~/replay/pine_gate/data`, so always pass it) and write `prod/<SYMBOL>/<interval>/` under it; `warmup` fetches both
   BTC-USDT and ETH-USDT, 1h and 4h.

   With `D1 = FROM - 1 day` and `D2 = TO - 1 day`:
   ```bash
   cd /home/ubuntu/ai-perp-long   # the repo, for PYTHONPATH=src and config/
   W=~/replay/forward_week_FROM
   # 1m production days D1 .. D2 inclusive (public endpoints, at most 1 request per second; D2 must be over in WIB)
   PYTHONPATH=src nice -n 19 .venv/bin/python -m research.pine_gate.cli --data-dir $W \
     fetch --source prod --symbols BTC-USDT --start D1 --end D2
   # native 1h/4h warm-ups ending exactly at D1 00:00 WIB (end exclusive); start = D1 minus 80 days
   PYTHONPATH=src nice -n 19 .venv/bin/python -m research.pine_gate.cli --data-dir $W \
     warmup --start D1_MINUS_80_DAYS --end D1
   ```
   `warmup` takes one `--start`/`--end` for both intervals; requests are windows of at most 1000 bars, so 80 days is
   fine. The need: at least about 831 closed 1h bars (the 720-hour ATR median plus the 112-bar ATR14 warm-up) and 401 4h
   bars before the first loaded day. A warm-up that stops before `D1 00:00` gives exit 1, "warm-up files ... do not
   reach the first loaded day".
3. **The earliest allowed `FROM` is 2026-10-15** (its day before, `D1`, is 2026-10-14, the first day after mode L's test
   period). `compare-forward` refuses an earlier one (exit 2) without opening a day file.
4. **Warm-up hours overlap mode L's test period (ruling R3).** The fetched warm-up reaches 80 days back from `D1`, and
   mode L's test period ends at 2026-10-14 00:00 WIB (5 Aug to 13 Oct). Those bars are indicator history only (SMA50, the
   4h EMA50, the 720-hour ATR median) and are never simulated or reported: no mode L outcome over the test period is
   computed. The overlap is long-lived because the 4h EMA50 needs about 401 bars (about 67 days) before it is defined, and
   the fetched file holds 80 days:
   - the file contains test-period bars while `D1` is before 2027-01-02 (2026-10-14 plus 80 days);
   - the 4h EMA50 at the window start still rests on them while `D1` is before about 2026-12-20 (plus 67 days);
   - the 720-hour ATR median (about 831 hourly bars, about 35 days) stops resting on them from about 2026-11-18.

   The manager should know this before counting those hours as "seen". Mode L's one-shot `test` is not affected:
   `compare-forward` reads no lock, start record or marker and writes none.
5. **Compare:**
   ```bash
   cd /home/ubuntu/ai-perp-long   # the repo: the config paths below are relative to it
   PYTHONPATH=src nice -n 19 .venv/bin/python -m research.mode_backtest compare-forward --pair BTC-USDT \
     --config config/modes.yaml --live-config config/modes_live_exit.yaml --data-dir $W \
     --forward-trades ~/forward_weeks/<the CSV from step 1> --from FROM --to TO --out-dir ~/forward_weeks
   ```
   Output: a JSON and the Indonesian `.md` (outputs are never overwritten; a rerun for the same window exits 1).
6. **Edge asymmetry** (the `.md` now states it itself; kept here for reference). Mode L takes no entry in the last 60 minutes of the simulated window (the
   live gates' segment edge), so every mode L position opens and closes inside `[FROM 00:00, TO 00:00)`. The forward side
   counts a position in the window by its `closed_at`. The Saturday-to-Monday gap of the entry window makes the two
   agree in practice; they could differ for a position near an edge.
7. **The Indonesian report to the manager** (code and docs stay English): the `.md` table (mode L (i), (ii), forward),
   the exit mix with AI exits against rule exits, the guard state, and anything the guard printed. The 60-position check
   and the 150-position decision each get their own report.

## 4. Stop rules

The guard sits right after the entry window gate in `_tick`, so it only runs (and logs) while the window is open. It
recomputes from Supabase on every such tick (stateless across restarts), logs `Forward guard stops new entries:
<reason>` at ERROR on every blocked tick, never sets the supervisor's `_entries_blocked_reason` latch, and fails closed
(entries paused, ERROR with traceback) when the store cannot be read. Open positions keep being managed.

- **Kill at 60:** from `FORWARD_KILL_AFTER_POSITIONS` (60) closed positions, while the pooled result is below
  `FORWARD_KILL_BELOW_PER_1M` (-500 per 1M), entries stay off. Report to the manager. Entries resume on their own only if
  the pooled result recovers above the limit, which it cannot without new entries; lifting the stop is the manager's
  decision, made by changing the setting, never by editing the data.
- **Review at 150:** at `FORWARD_REVIEW_AT_POSITIONS` (150) entries stop whatever the result. To continue past 150, the
  manager decides first, then the operator raises `FORWARD_REVIEW_AT_POSITIONS` in `/home/ubuntu/ai-perp-long/.env`
  and restarts the app **while flat**.
- **Never restart with an open position.** This applies to each app separately. `_initialize_leverage` calls
  `set_margin_mode`, which BingX rejects with `104103` while any position is open; the startup check then reads back
  `crossed`, exits, and pm2's autorestart turns it into a crash loop that hammers the venue auth endpoint. Recovery:
  `pm2 stop ai-perp-long`, close the position on the venue, then start. Flip the position flat before any redeploy or
  `.env` change, and confirm flat on the venue (the BingX VST app, or the bot's `Position ...` status lines having
  stopped) before `pm2 restart ai-perp-long --update-env`. Rerun the clean-shell check of section 2 step 6 in the
  shell you restart from first: `--update-env` copies that shell's environment into the app, and it overrides `.env`.
- Do not start a second process on the same account, schema or directory. `ai-perp-long` on the second account, schema
  `long_forward` and its own directory is the only sanctioned second live instance.

## 5. Mode L's one-shot and this branch

This branch changes mode L's code sha (`research.mode_backtest` and the modules it imports grew:
`forward_compare.py`, `bot/core/forward_summary.py`, the docstring of `bot/strategy/modes/__init__.py`). Mode L's `train`
and `test` bind that sha in the lock and must run on one code version. Either merge this branch before mode L's `train`,
or run `train` and `test` both before merging. Never merge between them, and never pull `main` between them. Mode L's
`train` and `test` have not run as of 2026-10-19.

## 6. Retired on 2026-10-25, and how to start it again

The operator stopped the bot on 2026-10-25 at 11:31:27 WIB (option B of the 25 Oct review) with `pm2 stop ai-perp-long`; `flat_check` printed FLAT right before and after. The manager then answered the 25 Oct report: stop account 2 completely and focus on account 1. At 15:43 WIB the stop was made permanent (see "Retired" below).

- **Why.** One week gave 6 trades, all on 19 Oct (-189 USDT, -630 per 1M, 0.30M volume), and none after: 225 SHORT proposals were dropped in NAIK hours. Its AI exits averaged -4.3 bps against +7.0 bps for the rules' exits on the same entries, and the 6 trades cost 9.2M tokens. Account 1's LONG shadow rows (the AI's LONG entries in NAIK hours, walked with the rules' exits) answer the same question at no cost.
- **Kept.** The forward `.env`, `logs/`, the `long_forward` schema, and the week-1 export `~/replay/out/fwd_week1/forward_trades_2026-10-19_2026-10-26.csv`. Nothing was deleted.
- **Still running.** `ai-perp-long-dashboard` and `ai-perp-tunnel` (the dashboard shows frozen data).
- **Retired (15:43 WIB).** `flat_check` on account 2 printed FLAT (it has to be pointed at `/home/ubuntu/ai-perp-long` and the `long_forward` schema, because `~/replay/flat_check.py` is hard-wired to account 1), then `pm2 delete ai-perp-long` and `pm2 save`. The saved dump had still listed the bot as online and `pm2-ubuntu` is enabled, so a VPS reboot could have started it; now it is in neither pm2 nor the dump. Only the two account 2 entries changed in the saved list, and the live bot kept its pid and restart count. The dump as it was: `~/.pm2/dump.pm2.bak-acct2-retire-20261025T164322`. The 5-minute supervisor cron line is commented out (crontab backup `~/crontab.bak-acct2-retire-20261025T164342`).
- **Account 1's latest code on account 2.** Section 7 is the procedure for starting account 2 on the code and the `.env` settings account 1 runs, with LONG (`build_forward_env.py --mirror-live`, tag `long-forward-v2`). The list below restarts the first version unchanged (tag `long-forward-v1`, its own settings).
- **To start it again,** only on a new decision from the manager and while flat on the venue:
    1. Decide whether it keeps the AI exits (`AI_EXIT_MAX_CALLS_PER_TRADE=25`) or uses the rules' exits (`0`, as account 1 has since 2026-10-21).
    2. Set a new `VOLUME_CYCLE_STARTED_AT` in `/home/ubuntu/ai-perp-long/.env`. The guard and the weekly report count from it, so without a new value the old 6 trades stay in the result and two versions mix.
    3. Back up `.env`, rerun the clean-shell check of section 2 step 6, then start it with the `pm2 start` command of section 2 (the pm2 entry is gone, so `pm2 restart` has nothing to restart), read the startup log and run `pm2 save`.
    4. Uncomment the supervisor cron line (section 2, step 9).
    5. The code stays pinned at `long-forward-v1`; a change of code is a new tag, as in section 2.

## 7. Second start: account 1's latest code on account 2 (LONG)

Account 2 runs account 1's code and `.env` as they are, with one difference of behaviour: `ENTRY_GATE_DIRECTION=long`. Under `ENTRY_REGIME_GATE=map` the hourly regime picks the side (NAIK long, TURUN short) and `ENTRY_GATE_DIRECTION` says which side may trade, so on account 2 LONG is live in NAIK hours and the SHORT of TURUN hours is recorded as a `shadow_entries` row (`source=shadow_side`). Between them the two accounts then trade both regimes with real fills. Everything else is account 1's: `AI_ENTRY_PAYLOAD=lean`, `AI_LEAN_OBJECTIVE=volume`, `SETUP_LEDGER_MODE=shadow`, the rules' exits (`AI_EXIT_MAX_CALLS_PER_TRADE=0`), the daily guards and the entry window. There is no exit change and no SL+ here: that is a separate, later stage.

This section only prepares the start. Nothing in it runs by itself, and the start needs the manager's new decision (section 6). Order: merge, tag, add the `shadow_entries` table, update the clone, flat check, build the `.env`, type the keys, review, flat check again, first start in the foreground, pm2, cron.

**What `--mirror-live` changes** in the copy of account 1's `.env` (every other line, comments included, is copied as it is):

| Key | Account 2 |
|---|---|
| `SUPABASE_SCHEMA` | `long_forward` |
| `ENTRY_GATE_DIRECTION` | `long` |
| `SYMBOLS` | `BTC/USDT` |
| `TELEMETRY_PORT` | `3919` (account 1's feed owns 3917; `ai-perp-long-dashboard` proxies 3919) |
| `VOLUME_CYCLE_STARTED_AT`, `SETUP_LEDGER_SINCE` | the `--cycle-start` instant, both |
| `BINGX_API_KEY`, `BINGX_SECRET_KEY`, `XAI_API_KEY` | blank, typed by the account owner |

Two things follow from copying the rest. The forward guard of section 4 stays off, because its keys are not in account 1's file: add `FORWARD_KILL_AFTER_POSITIONS`, `FORWARD_KILL_BELOW_PER_1M` and `FORWARD_REVIEW_AT_POSITIONS` to the new `.env` before the first start if the manager wants an automatic stop (they count from the cycle start, in `long_forward`). And `TELEMETRY_ENABLED` is copied, so the feed listens on 3919 again and the account 2 dashboard shows live data instead of the frozen one.

**Preconditions** (all before step 4):

- **The manager's new decision** to start account 2 again (section 6). Until it exists, do nothing past step 3: steps 1 to 3 touch nothing on the VPS but one table.
- **Account 1's settings are the ones to mirror.** The builder copies `/home/ubuntu/ai-perpetual-trading/.env` as it is when it runs, so make any pending change to account 1 first.
- **The key policy.** `XAI_API_KEY` comes out blank. Decide whose key the account owner types: account 2's own (spec 9.3: its own token spend and rate limit) or the one account 1 uses (the choice taken on 2026-10-19). With one key both bots draw on one provider limit, and a `mimo-v2.6-pro` call can take about a minute, so their calls can overlap. Write the decision down. The copied `COINMARKETCAP_API_KEY` and `DUNE_API_KEY` are decided in step 8.
- **Sizing.** `POSITION_USDT`, `VOLUME_MARGIN_PER_ENTRY_USDT` and `LEVERAGE` are copied too. Check that account 2's VST balance can margin what account 1's can; the venue rejects an order its balance cannot cover.
- **Timing.** Account 1's `ENTRY_WEEKLY_WINDOW` is copied. Do steps 4 to 12 between Saturday 00:00 and Monday 07:00 WIB (section 2 step 7 says why).

Steps:

1. **Merge** the pull request that adds the mirror tooling. The user merges it; nobody merges automatically.
2. **Tag** the merge commit from a machine with push rights, as in section 2 step 1:
   ```bash
   git fetch origin && git tag -a long-forward-v2 -m "Account 2 mirrors account 1's code with LONG" origin/main
   git push origin long-forward-v2
   ```
3. **Add `shadow_entries` to `long_forward`.** The schema was created on 2026-10-19, before that table joined `supabase/schema.sql`, so the REST API answers 404 for it, while `ENTRY_REGIME_GATE=map` and the ledger read and write it on every tick. In the SQL editor of the project the live bot uses, run the contents of `supabase/long_forward_shadow_entries.sql`. It is generated from `supabase/schema.sql` by `src/forward_schema.py --schema long_forward --only shadow_entries` (a test pins the drift). Its creates are `if not exists` and the rest is row level security and the schema's `service_role` grants, which can be repeated, so running it twice is safe. Then check that the API serves the table. This prints the schema name and a status code, nothing else (a failure prints only `failed:` and the exception's class name, never a value), and uses the old `.env`:
   ```bash
   cd /home/ubuntu/ai-perp-long && PYTHONPATH=src .venv/bin/python -c "import sys; sys.excepthook = lambda t, v, tb: print('failed:', t.__name__, file=sys.stderr); import httpx; from bot.config import Settings; s = Settings(); k = s.supabase_service_role_key; r = httpx.get(s.supabase_url.rstrip('/') + '/rest/v1/shadow_entries', params={'select': 'id', 'limit': '1'}, headers={'apikey': k, 'Authorization': 'Bearer ' + k, 'Accept-Profile': s.supabase_schema}, timeout=20); print('schema', s.supabase_schema, '| shadow_entries answers', r.status_code)"
   ```
   Expect `schema long_forward | shadow_entries answers 200`. A `404` means PostgREST's schema cache has not seen the table yet: run `notify pgrst, 'reload schema';` in the SQL editor and check again. The schema stays in the exposed schemas from section 1.
4. **Update the clone to the tag.** No trading process runs from it (the pm2 entry was deleted on 2026-10-25). The account 2 dashboard `ai-perp-long-dashboard` has its directory under the clone, but it serves a prebuilt, untracked `.next` that a checkout does not touch. Check that no `ai-perp-long` app is listed, then update:
   ```bash
   pm2 jlist | python3 -c 'import json,sys; print([p["name"] for p in json.load(sys.stdin)])'    # must not list ai-perp-long
   cd /home/ubuntu/ai-perp-long && git status --short    # no tracked change; .env and logs/ are ignored
   git fetch --tags origin && git checkout long-forward-v2    # detached HEAD on purpose, as in section 2 step 1
   .venv/bin/pip install -r requirements.txt
   ```
   Account 2 must run the code account 1 runs. Both of these print nothing when the trading code of the tag equals what the live checkout has; the second one catches an uncommitted edit there:
   ```bash
   git diff --stat "$(git -C /home/ubuntu/ai-perpetual-trading rev-parse HEAD)" long-forward-v2 -- src/bot src/main.py config requirements.txt
   git -C /home/ubuntu/ai-perpetual-trading status --porcelain -- src/bot src/main.py config requirements.txt
   ```
   Any output means `main` has moved past what account 1 runs, or the live checkout is edited. Settle that first (deploy to account 1 as its own staged decision, or move the tag); do not start a mirror of different code. `requirements.txt` has ranges, not pins, so also compare what the two venvs resolved, and look at httpx, pydantic, pydantic-settings, python-dotenv, tenacity and PyYAML:
   ```bash
   diff <(/home/ubuntu/ai-perpetual-trading/.venv/bin/pip freeze) <(/home/ubuntu/ai-perp-long/.venv/bin/pip freeze)
   ```
5. **Flat check, first look.** `src/flat_check.py` ships in the tag, so this is the first moment the clone has it. Run the clean-shell check of section 2 step 6 ("Before the first start"; it must print nothing), then, with the old `.env` still in place:
   ```bash
   cd /home/ubuntu/ai-perp-long && PYTHONPATH=src .venv/bin/python src/flat_check.py --expect-schema long_forward
   ```
   It must print exactly `FLAT`, and its stderr line must say `store schema long_forward`. `--expect-schema` makes a run from a directory whose `.env` names another schema fail with `UNREADABLE SchemaMismatch` instead of reporting that account as flat. `OPEN ...` or `UNREADABLE ...` means stop: account 2 is not flat or cannot be read; close a position on the venue before anything else (section 4). `UNREADABLE ValidationError (settings)` only says the old `.env` does not load under this code: carry on and make the check in step 9 the one that counts. The script reads only; it places, cancels and closes nothing.
6. **Back up the old `.env` and build the new one.** The builder refuses to overwrite, so the old file is moved aside first (`mv` keeps its mode 600; `ls .env.bak-mirror-*` finds the backup later):
   ```bash
   cd /home/ubuntu/ai-perp-long && STAMP=$(date -u +%Y%m%dT%H%M%SZ) && mv .env ".env.bak-mirror-$STAMP"
   PYTHONPATH=src .venv/bin/python src/build_forward_env.py --mirror-live \
     --source /home/ubuntu/ai-perpetual-trading/.env \
     --target /home/ubuntu/ai-perp-long/.env \
     --cycle-start "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
   ```
   It prints the key names it set and the three it blanked, never a value; show that output to the user. If it fails nothing was written: `mv ".env.bak-mirror-$STAMP" .env` restores the old file. `VOLUME_CYCLE_STARTED_AT` and `SETUP_LEDGER_SINCE` are now both this instant, and they are never edited afterwards (section 2 step 3): the volume accounting, the setup ledger and the report all count from here.
7. **The account owner types** `BINGX_API_KEY`, `BINGX_SECRET_KEY` and `XAI_API_KEY` (the key policy above) into `/home/ubuntu/ai-perp-long/.env` with an editor. `chmod 600 .env` stays.
8. **Review,** in the shell you will start the bot from:
   - the process environment overrides `.env`, and the prefix list of section 2 step 6 does not know the `AI_*` and `SETUP_*` names that account 1's file brings. Use this instead; it prints the names of any environment variable that the new `.env` also defines, and it must print `[]`:
     ```bash
     cd /home/ubuntu/ai-perp-long && .venv/bin/python -I -c "import os, sys; from dotenv import dotenv_values as d; k = {x.upper() for x in d(sys.argv[1])}; print(sorted(n for n in os.environ if n.upper() in k))" /home/ubuntu/ai-perp-long/.env
     ```
   - the two accounts must not share a BingX key. The file was built from account 1's and the keys are typed by hand: if account 1's key went in, the second bot would trade on account 1's account, and step 9 could still print `FLAT` while the live bot is flat. This prints a verdict only and must print `different keys`:
     ```bash
     cd /home/ubuntu/ai-perp-long && .venv/bin/python -I -c "import sys; from dotenv import dotenv_values as d; a, b = (d(p).get('BINGX_API_KEY') or '' for p in sys.argv[1:3]); print('SAME KEY' if a == b else 'EMPTY KEY' if not (a and b) else 'different keys')" /home/ubuntu/ai-perpetual-trading/.env /home/ubuntu/ai-perp-long/.env
     ```
   - the names-only review of section 2 step 4. Decide the copied `COINMARKETCAP_API_KEY` and `DUNE_API_KEY` there: both bots then call those providers on one quota, so keep them, give account 2 its own, or blank them in the new `.env`. `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are copied as well: it is one project, so only `SUPABASE_SCHEMA=long_forward` keeps account 2 away from account 1's tables, which the next bullet shows;
   - the effective values of the mirror. This prints an allowlist of non-secret settings, and `Settings()` also refuses to boot on a bad combination, so it proves the file loads:
     ```bash
     cd /home/ubuntu/ai-perp-long && PYTHONPATH=src .venv/bin/python -c "import sys; sys.excepthook = lambda t, v, tb: print('failed:', t.__name__, file=sys.stderr); from bot.config import Settings; s = Settings(); print(*(f'{n} = {getattr(s, n)}' for n in ('supabase_schema', 'entry_gate_direction', 'entry_regime_gate', 'ai_entry_payload', 'ai_lean_objective', 'setup_ledger_mode', 'setup_ledger_since', 'volume_cycle_started_at', 'ai_exit_max_calls_per_trade', 'symbols', 'telemetry_enabled', 'telemetry_port', 'entry_weekly_window')), sep='\n')"
     ```
     Expect `supabase_schema = long_forward`, `entry_gate_direction = long`, `entry_regime_gate = map`, `ai_entry_payload = lean`, `ai_lean_objective = volume`, `setup_ledger_mode = shadow`, `ai_exit_max_calls_per_trade = 0`, `symbols = ('BTC/USDT',)`, `telemetry_port = 3919`, `entry_weekly_window = mon 07:00-sat 00:00`, and `setup_ledger_since` equal to `volume_cycle_started_at` (the instant of step 6). If the window reads empty or differs, stop: the foreground start of step 10 is safe only because the entry window is closed then.
9. **Flat check, the gate.** The command of step 5 (with `--expect-schema long_forward`) in the same clean shell, now with the new `.env` and the keys typed in: exactly `FLAT`. Anything else: do not start. It looks at positions only, so also look at the BingX VST app for resting orders: BingX rejects `set_margin_mode` (`104103`) while a position or a pending order exists, and the startup check then turns a restart into a crash loop (section 4).
10. **First start in the foreground, outside the entry window:** exactly section 2 step 7 (same command, same reasons). You will see the same startup lines. One difference: with `map` and the ledger on, the engine evaluates pending shadow rows right after the entries latch and before the window gate, so `shadow_entries` is read on the first tick even though the window is closed. These lines mean the table is missing or not served (go back to step 3): `Shadow evaluation failed`, `could not be read from the store`, `its shadow row could not be saved`. Stop it with Ctrl-C after reading the lines, flat as before.
11. **pm2, still outside the window and flat:** the command of section 2 step 8, then the after-start check below (the same idea as step 8's first check, applied to the app's environment; it must print `[]`), then `pm2 save`. The entry was removed from the saved list on 2026-10-25 and `pm2-ubuntu` is enabled, so without the save a VPS reboot would start only the live bot.
    ```bash
    cd /home/ubuntu/ai-perp-long && PYTHONPATH=src pm2 start src/main.py --name ai-perp-long \
      --interpreter /home/ubuntu/ai-perp-long/.venv/bin/python --cwd /home/ubuntu/ai-perp-long --restart-delay 10000
    pm2 status    # ai-perp-volume must still be online and unrestarted
    pm2 jlist | .venv/bin/python -I -c "import json, sys; from dotenv import dotenv_values as d; k = {x.upper() for x in d(sys.argv[1])}; app = [p for p in json.load(sys.stdin) if p['name'] == 'ai-perp-long'][0]['pm2_env']; print(sorted(n for n in app if n.upper() in k))" /home/ubuntu/ai-perp-long/.env    # must print []
    pm2 save
    ```
12. **Supervisor cron.** First once by hand with `--dry-run`, as in section 2 step 9. Then uncomment the line that was commented out on 2026-10-25, after a backup, and leave the live bot's line alone:
    ```bash
    crontab -l > ~/crontab.bak-acct2-mirror-$(date -u +%Y%m%dT%H%M%SZ)
    crontab -l | grep ai-perp-long    # the commented line; if it is gone, add the one of section 2 step 9
    crontab -e                        # remove the leading # of that line only
    ```
13. **Record** the cycle start, the tag `long-forward-v2` and the first Monday in `CLAUDE.md` and in memory, as in section 2 step 10.

**What to read afterwards.** On the first Monday after 07:00 WIB, in `~/.pm2/logs/ai-perp-long-error.log`:

- `grep "Regime gate" ~/.pm2/logs/ai-perp-long-error.log | tail -5`. The first line reads `Regime gate BTC/USDT: <reason> | allowed=... kondisi=...`, logged once per change. Under `map` the reason is `naik` (a LONG hour) or `turun` (a SHORT hour); `not_mapped:...`, `volatility` and `history_short` map no side, and then no AI call is made.
- NAIK hours: LONG is live. A trade carries its bucket at `factor_metadata.setup` (`LONG|NAIK|atr_...`).
- TURUN hours: the lean request offers SHORT only, and `ENTRY_GATE_DIRECTION=long` makes it a shadow side. The engine logs `Strategy returned HOLD`, `ai_decisions.payload_json.gate.shadow_side` is `true`, and a `shadow_entries` row with `source=shadow_side` is written, at most one per hold-plus-cooldown spacing. No SHORT order is ever sent: a SHORT in the `trades` of `long_forward` means the side filter failed, so stop and look.
- The stage report from the clone, read-only, reading this directory's `.env` and so `long_forward`: `PYTHONPATH=src .venv/bin/python src/report_live_stages.py --stage-start <the instant of step 6>`. Judge the LONG trades the way account 1's step 2 is judged (gross bps per trade with its standard error, at the 30, 60 and 100 closed-trade reads) unless the manager says otherwise.

**Rollback.** Do it with the entry window closed (Saturday 00:00 to Monday 07:00 WIB), like the start: with the window open an entry can appear between the flat check and the stop, and the stop, floor and time exits live in the bot process. Pick the phase you are in:

- Steps 4 and 5 (the clone is on the tag, the old `.env` is untouched): `cd /home/ubuntu/ai-perp-long && git checkout long-forward-v1`. Nothing else changed on the VPS.
- Steps 6 to 9 (the new `.env` exists, no bot has run): that, then `mv .env .env.mirror-unused && mv .env.bak-mirror-<STAMP> .env`. The `shadow_entries` table stays; it does no harm.
- Step 10 (the foreground run): Ctrl-C, check that the account is flat, then the previous bullet.
- After step 11 (pm2): the position first. `pm2 stop` alone would leave an open position unwatched. The rules' exits end any trade within 60 minutes (the stop, the net +56 floor or take-profit, and the time stop), so wait for it or close it on the venue (section 4), and confirm `FLAT` with `src/flat_check.py --expect-schema long_forward`. Then, flat and with the window closed:
  ```bash
  pm2 delete ai-perp-long && pm2 save     # back to the retired state of section 6; never `all`
  crontab -e                              # comment the ai-perp-long supervisor line again
  cd /home/ubuntu/ai-perp-long && git checkout long-forward-v1
  mv .env .env.mirror-stopped-<STAMP> && mv .env.bak-mirror-<STAMP> .env
  ```
  Nothing in `long_forward` is deleted.
