# Account 2 Mirror Package — TDD Evidence

## User journey

On 2026-10-28 the user asked to prepare running account 1's latest code on account 2 with LONG. Account 1 (pm2 app `ai-perp-volume`) trades SHORT in TURUN hours on the staged-fixes configuration: `ENTRY_REGIME_GATE=map`, `ENTRY_GATE_DIRECTION=short`, `AI_ENTRY_PAYLOAD=lean`, `AI_LEAN_OBJECTIVE=volume`, `SETUP_LEDGER_MODE=shadow`, rule exits (`AI_EXIT_MAX_CALLS_PER_TRADE=0`). Account 2 (BingX VST account 2, Supabase schema `long_forward`, dir `/home/ubuntu/ai-perp-long`) was a LONG forward test, retired on 2026-10-25; its clone sits on tag `long-forward-v1`, whose code has no `map` gate, no lean payload and no ledger.

The design: account 2 runs account 1's code and `.env` settings exactly, with one difference, `ENTRY_GATE_DIRECTION=long`. Under `map` LONG is then live in NAIK hours and the SHORT of TURUN hours becomes a `shadow_side` row, so between the two accounts both regimes are covered and LONG gets real fills. No SL+ and no exit change: that is a separate, later stage.

Restarting account 2 needs the manager's new decision (runbook section 6), so this change only prepares the start. Nothing is started, tagged, deployed or merged by it. What the operator was missing, and what this adds:

1. A way to build account 2's `.env` as a copy of account 1's with only the mirror's keys changed (`build_forward_env.py --mirror-live`).
2. The `shadow_entries` table in `long_forward` (a live check on 2026-10-28 found the REST API answering 404 for it, because the schema predates the table): `forward_schema.py --only TABLE` and the generated `supabase/long_forward_shadow_entries.sql`.
3. A flat check that reads the directory's own account and schema (`~/replay/flat_check.py` is VPS-only and hard-wired to account 1): `src/flat_check.py`.
4. The ordered procedure, with a rollback: runbook section 7.

## RED evidence

Each test module was written and run before the code it tests existed.

### `build_forward_env.py --mirror-live`

`tests/test_build_forward_env_mirror.py` (19 tests at this point). A golden file of the legacy output was generated first, from the builder as it was before this change, so that "the legacy output is unchanged" is a characterization test that must be green before and after. Three tests pass on the old code by design: the golden comparison, the legacy CLI, and the check that the fake live env is itself a valid `Settings` config. The other 16 failed, all for the missing feature:

```text
TypeError: build_forward_env() got an unexpected keyword argument 'mirror_live'        (13 tests)
SystemExit: 2                                                                           (2 tests: argparse rejects --mirror-live)
AttributeError: module 'build_forward_env' has no attribute 'MIRROR_OVERRIDES'          (1 test)
----------------------------------------------------------------------
Ran 19 tests in 0.023s

FAILED (errors=16)
```

(The counts in parentheses are annotations added here.)

### `forward_schema.py --only`

`tests/test_forward_schema_only.py` (26 tests at this point, sub-tests counted separately in the totals below):

```text
AttributeError: module 'forward_schema' has no attribute 'table_sql'                     (41 failures)
AssertionError: 2 != 0 : usage: forward_schema.py [-h] --schema SCHEMA [--base BASE]    (2 CLI tests: --only is not a flag yet)
AssertionError: 2 != 1                                                                  (the one-error-line CLI test)
----------------------------------------------------------------------
Ran 26 tests in 0.132s

FAILED (failures=3, errors=41)
```

### `flat_check.py`

`tests/test_flat_check.py` (27 tests at this point):

```text
ImportError: Failed to import test module: test_flat_check
  File "tests/test_flat_check.py", line 21, in <module>
    import flat_check
ModuleNotFoundError: No module named 'flat_check'
----------------------------------------------------------------------
Ran 1 test in 0.001s

FAILED (errors=1)
```

### Runbook section 7

`tests/test_runbook_account2_mirror.py` was written after the runbook text, so it has no RED run. It was mutation-checked instead: each of three edits to the runbook was made, the test module run, and the runbook restored byte-for-byte from a backup.

```text
typo'd flag (--mirror-livee)         -> FAIL: test_flags_used_with_each_script_are_accepted_by_it
wrong schema header in the REST check -> FAIL: test_the_table_check_asks_the_rest_api_for_shadow_entries_in_this_schema_and_prints_no_secret
--target pointed at the live .env     -> FAIL: test_the_flat_check_runs_from_the_clone_and_the_env_is_built_from_account_one_into_account_two
```

## Independent review and what it changed

Two read-only agents (a code reviewer and a security reviewer) were given the spec and the committed diff, not the author's conclusions, and asked for verified, evidence-backed findings. Neither found a blocker or a high-severity issue; the committed `supabase/long_forward_shadow_entries.sql` was applied twice to a throwaway PostgreSQL 16 without error and matched `public.shadow_entries`. Each finding below that was acted on has a failing test first.

| Finding (severity) | Disposition |
|---|---|
| `flat_check` printed `FLAT` (exit 0) when BingX answered 200 with `data: null`, no `data`, rows wrapped in an object, an unparsable amount, or a nonzero row naming another symbol: the client's `get_symbol_position_details` maps all of those to "no position" (medium) | Fixed. It reads the raw `get_position_info` answer itself and is strict (below). |
| `FLAT` cannot say which BingX account it checked, and nothing checked that account 2's keys differ from account 1's (medium) | `--expect-schema` on the flat checks; a runbook check that prints `different keys` / `SAME KEY` / `EMPTY KEY` (a verdict, never a key). |
| Rollback "for steps 4 to 9" renames the only `.env` before step 6 has made a backup, and offers `pm2 delete` before step 11 (medium) | Rollback split by phase; the env swap is offered only from step 6 on, `pm2 delete` only after step 11. |
| Clean-shell prefix list misses the `AI_*`, `SETUP_*`, `POSITION_*` names that account 1's file brings; a process variable overrides `.env` (medium) | The check now compares the environment with the new `.env`'s own key names, also for the pm2 app. |
| `table_sql` emitted a file with no `create table` for an upper-case `PUBLIC.t`; only create table/index were checked for `if not exists` and the header claimed more (medium) | Anchored `if not exists` (also `add column`); a statement naming `public` or a known table without a qualified name is refused; the header states what was checked. |
| Rollback after step 11 could strand a position if the entry window is open; the "outside the window" safety rested on an unchecked `ENTRY_WEEKLY_WINDOW`; "no process runs from the clone" was unchecked and not quite true (the dashboard has its directory there) | Window rule on the rollback, the effective-values check lists `entry_weekly_window` and says to stop if it differs, a `pm2 jlist` check, reworded. |
| Code-parity diff missed `src/main.py` and a dirty live checkout; requirements are unpinned (low) | Pathspec `src/bot src/main.py config requirements.txt`, a `status --porcelain` check, a `pip freeze` comparison. |
| A `Settings()` failure in the runbook one-liners prints the head of `BINGX_API_KEY` (low, reproduced) | Both one-liners set an excepthook that prints only the class name; a test shows the unprotected traceback quotes the key and the protected one does not. |
| The builder's line-based render can miss a forced key inside a multi-line quoted value or one spelled so that only the bot's parser folds it (Kelvin sign); an interrupt leaves a half-written file with copied secrets (low) | The rendered text is read back through python-dotenv and refused (key names only) unless every forced key reads back as forced; the file is removed on any interrupt. |
| Supabase system schemas were not refused as a target; a `--` comment before the closing `;` swallowed it (low) | Added to the reserved names; the terminator goes on its own line. |
| Test gaps: bare `Exception` for the no-keys-typed case; log phrases in the runbook unpinned; client kwargs only faked (low) | Exact `ValidationError` locations for three cases; a test that each phrase exists in the source; the end-to-end class builds the real `BingXFuturesClient`. |

Considered and declined, with the reason:

- A whitelist of statement shapes in `--only`. `schema.sql` contains statements (an `update ... where ... is null` backfill) whose re-runnability cannot be decided mechanically; the header says what is enforced instead of promising more.
- Same-line trailing comments after a `;` migrating to the next statement, and `public.` inside a quoted policy name. Both need a quoted-aware second view of each statement for no case `schema.sql` has; the first can only cause a refusal, never a wrong file.
- An atomic temp-file-and-link write for the builder. The mode is 0600 from the first byte, the interrupt cleanup removes the file, and `kill -9` cannot be handled by any design.
- Looking at pending orders in `flat_check`. The position read is the authority the bot itself uses; the runbook says to look at the venue app for resting orders, because they also make `set_margin_mode` fail.

### Evidence for the `flat_check` fix

The reviewer's offline harness (the real client and store over a mock transport, no network) against the unchanged code, then the new code:

```text
                                         before                  after
c0 data=[]                               FLAT                    FLAT
c1 data=null                             FLAT                    UNREADABLE VenueAnswerError
c2 no data key                           FLAT                    UNREADABLE VenueAnswerError
c3 data wrapping a LONG 0.01             FLAT                    UNREADABLE VenueAnswerError
c4 positionAmt='abc'                     FLAT                    UNREADABLE InvalidOperation
c5 symbol 'BTCUSDT', LONG 0.01           FLAT                    OPEN venue BTCUSDT side=LONG size=0.01
c6 real open position                    OPEN                    OPEN
c7 BingX business error                  UNREADABLE              UNREADABLE
c8 store answers 500                     UNREADABLE              UNREADABLE
c9 persisted row status=closing          OPEN                    OPEN
```

The reworked tests against the old code: `Ran 39 tests ... FAILED (failures=34, errors=10)`; the 10 errors are `SystemExit: 2` from the then-unknown `--expect-schema`.

### Evidence for the builder and generator fixes

```text
tests.test_build_forward_env_mirror.ReadbackAndCleanupTests, old builder:
  AssertionError: ForwardEnvError not raised          (a forced key inside a multi-line quoted value; the Kelvin-sign key)
  a KeyboardInterrupt during the write left the half-written file behind
  Ran 4 tests in 0.051s / FAILED (failures=5, errors=1)

tests.test_forward_schema_only, generator before the hardening:
  13 Supabase system schemas accepted, 'if not exists' inside a default accepted, add column without it accepted,
  upper-case / quoted / unqualified statements silently dropped
  Ran 37 tests in 3.584s / FAILED (failures=18)
  then, for the header wording and the swallowed terminator: Ran 40 tests / FAILED (failures=2)
```

## GREEN implementation

- `src/build_forward_env.py`: `--mirror-live` selects a different forced-key dict (`MIRROR_OVERRIDES`: `SUPABASE_SCHEMA=long_forward`, `ENTRY_GATE_DIRECTION=long`, `SYMBOLS=BTC/USDT`, `TELEMETRY_PORT=3919`) plus both start stamps (`VOLUME_CYCLE_STARTED_AT`, `SETUP_LEDGER_SINCE`, each set to `--cycle-start`) and the same three blanked secrets. Rendering, the exclusive mode-600 write, the source checks and the key-names-only output are the same code paths as the legacy build: the mirror is a different dict and report, not a second renderer. Both modes now read the rendered text back through python-dotenv before writing, and remove the file on any interrupt. Legacy mode is the default; its 28 tests and the golden file stay green.
- `src/forward_schema.py`: `table_sql(schema, table, base_sql)` and `--only TABLE`. A comment- and quote-aware splitter cuts `schema.sql` into statements (three comment lines in the base hold a `;`, one holds apostrophes); a statement belongs to the first `public.` name in its code, so a foreign key or a comment naming another table moves nothing. It fails closed: unknown table (exit 2, one line naming the known tables), a create or add column without `if not exists` right after the keywords, a statement it cannot place, dollar quoting, block comments, an unterminated string or statement, a `public` left after the rewrite, a reserved or Supabase system schema as the target. A test pins that all 43 statements of `schema.sql` reach exactly one `--only`. The full-schema output is byte-identical (the existing drift test passes; the three grant lines moved into a shared helper).
- `supabase/long_forward_shadow_entries.sql`: the generator's output for `shadow_entries`, pinned by a drift test and by a CLI-equality test.
- `src/flat_check.py`: `check_flat(settings, client, store)` returns `FLAT` only when every symbol's raw positions answer is a list of rows whose `positionAmt` are all numeric zeros and the store holds no row that is not `closed`; `OPEN venue ...`, `OPEN persisted ...` and `UNREADABLE <ExceptionName> (what)` otherwise, exit 1. A source that fails is reported and never reads as flat, and the other source is still read. `--expect-schema NAME` refuses another schema before any network call. The persisted rows are read through a small adapter over the store's own schema-aware getter, because `SupabaseTradeStore.list_open_positions()` turns every read error into an empty list: a test shows that trap and that the adapter raises where the store's reader returns nothing. Only exception class names are printed and the client is built without a logger or `debug_http`. An end-to-end test runs the real client and store over a mock transport and asserts that exactly two GET requests are made.
- `docs/runbooks/long-forward-test.md`: section 7 (preconditions, 13 ordered steps, what to read afterwards, rollback by phase); the status paragraph and section 6 point to it; sections 1 to 5 are untouched. `tests/test_runbook_account2_mirror.py` executes its five inline commands against files built by the real builder and pins its paths, flags, tag names, rollback order and the log phrases it names.
- `CLAUDE.md`: one Commands line after the `supervisor.py` line and one bullet in the "LONG AI forward test" section. Nothing else.

The package touches no trading code: `src/bot/` is unchanged.

## Verification

```text
Full suite on Python 3.10.20 (the local .venv), the command CI runs. Coverage on 3.10 cannot read
pyproject.toml without tomli, so an rc file with the same settings was used (CI runs 3.11):
  Ran 3134 tests ... OK
    baseline on c0b63e5: Ran 3016 tests ... OK, so 118 are new:
    test_build_forward_env_mirror 23, test_forward_schema_only 40, test_flat_check 39, test_runbook_account2_mirror 16
    (test_build_forward_env 28 and test_forward_schema 12 are unchanged and green)
  Line coverage 92% (gate 80%, baseline 92%):
    src/flat_check.py 100%, src/forward_schema.py 100% (59% before), src/build_forward_env.py 95%
compileall src tests: passed
git diff --check: passed (index, working tree and origin/main..HEAD)
pip check: not run, the local .venv has no pip module; requirements.txt is untouched
secret hygiene test: passed, with this document and every new file tracked
src/bot/ unchanged: 11 files differ from origin/main, plus this document
```

The first full run, before the review fixes, was `Ran 3096 tests ... OK`, coverage 92%.

## Commit trail

- `5fb14a4` — `feat: build_forward_env --mirror-live, account 1's settings with LONG for account 2`
- `780ca3a` — `feat: forward_schema --only TABLE and the generated shadow_entries SQL for long_forward`
- `8be468d` — `feat: flat_check.py, a read-only flat check for one bot directory`
- `da059d3` — `docs: runbook section 7, a second start on account 1's code with LONG`
- `7c89ff5` — `fix: flat_check reads the raw positions answer strictly and takes --expect-schema`
- `5c1c872` — `fix: build_forward_env reads the rendered file back and cleans up an interrupted write`
- `e13cde9` — `fix: forward_schema --only fails closed on what it cannot place and says what it checked`
- `e0ceb57` — `docs: runbook section 7 after review, and a test that runs its commands`
- the commit that adds this document

## Deployment gate

Before account 2 runs anything from this change, in this order (runbook section 7 has the commands):

1. The manager's new decision to start account 2 again (section 6). Until then nothing past the `shadow_entries` table is done on the VPS.
2. This pull request merged by the user, and the tag `long-forward-v2` created and pushed by the operator on the merge commit. The code-parity `git diff` and the `status --porcelain` check against the live checkout print nothing.
3. `supabase/long_forward_shadow_entries.sql` run in the Supabase SQL editor, and the REST check printing `schema long_forward | shadow_entries answers 200`.
4. Account 2 flat: `src/flat_check.py --expect-schema long_forward` run from `/home/ubuntu/ai-perp-long` prints `FLAT`, once after the checkout and once more with the new `.env` and the keys typed in; no resting order in the BingX VST app.
5. The env: the environment-override check prints `[]`; the BingX key check prints `different keys`; the names-only review is done; the key policy for `XAI_API_KEY` and the sizing check are decided and recorded; the effective-values check shows `long_forward`, `long`, `map`, `lean`, `volume`, ledger `shadow`, exits `0`, `('BTC/USDT',)`, port `3919`, `entry_weekly_window = mon 07:00-sat 00:00` and equal start stamps.
6. First start in the foreground between Saturday 00:00 and Monday 07:00 WIB with no `shadow_entries` warning in the log; then `pm2 start`, the after-start environment check printing `[]`, and `pm2 save`.
7. The supervisor cron line uncommented after a `--dry-run`, with a crontab backup taken first.
