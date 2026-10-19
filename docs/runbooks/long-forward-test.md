# LONG AI forward test: runbook

The LONG forward bot is a second instance of this repo. It runs the same AI pipeline as the live SHORT bot, restricted to
LONG entries while the hourly regime reads NAIK, on its own BingX VST account, its own Supabase schema and its own
directory. Spec: `docs/superpowers/specs/2026-10-18-long-ai-forward-test-design.md`. Plan:
`docs/superpowers/plans/2026-10-19-long-ai-forward-test.md`. Evidence: `.github/tdd/long-ai-forward-test.tdd.md`.

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
4. Review the key names that came over from the live `.env` and were not forced. Names only: the `grep` keeps just the
   lines that start a `NAME=` assignment (an `export NAME=` line, spaces around `=` and digits in names included), so a
   continuation line of a multi-line quoted value or a stray token never reaches the terminal; the `sed` then cuts
   everything from the first `=`. `[[:space:]]` keeps it working with both GNU and BSD `sed`:
   ```bash
   grep -E '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=' /home/ubuntu/ai-perp-long/.env | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/[[:space:]]*=.*//' | sort
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
   opens a position blocks until it closes. In `_tick` the window gate comes right after the entries latch and the forward
   guard and before the risk guard, the cooldowns, the regime gate and the strategy, so while the window is closed no AI call
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
   forward bot is one symbol); that refusal is visible now. Read the first verdict in the pm2 error log on Monday after
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
9. Record the cycle start and the first Monday in `CLAUDE.md` and in memory (plan Task 9).

Never touch `ai-perp-volume`. Never run `pm2 restart all`, `pm2 delete all` or `pm2 stop all`.

## 3. Weekly (every Monday, for the week before)

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

The guard recomputes from Supabase on every tick (stateless across restarts), logs `Forward guard stops new entries:
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
