# LONG Module Phase 1 — TDD Evidence

Offline backtest of the manager's LONG module for the BingX VST bot. The rule core will live in `src/bot/strategy/modes/` and the harness in `src/research/mode_backtest/`; neither is wired into the live bot. Spec: `docs/superpowers/specs/2026-10-17-long-module-phase1-design.md`. Plan: `docs/superpowers/plans/2026-10-17-long-module-phase1.md`.

## User journey

The manager's document "Modul LONG — Spesifikasi & Rencana Uji" (17 October 2026) adds rule-based modes (A and B for up-trends, C for sideways markets, D2 as a mirrored pullback SHORT) and ETH to a bot that only shorts BTC. Each mode is tested on its own and passes only if its result per 1,000,000 USDT of volume is better than −150. The document fixes the order: step 0 first (split the 16 October Pine holdout by side and trend condition, no new test), then Mode A on BTC. Phase 1 covers exactly those two. It backtests Mode A on production 1m data (training, a lock file, then a one-shot test) and reports in Indonesian. The live bot, `.env` and the VPS are not touched.

Each task below appends its RED output and GREEN result.

## Task 1 — SMA, population stdev and rolling median

`bot/strategy/pine_indicators.py` gains `sma`, `stdev` and `rolling_median`. They are the base of the hourly SMA20/SMA50 and of the 720-hour median of ATR14 that the mode selector and the volatility guard read. Each takes a list that may hold `None` and returns a list of the same length. A value is `None` until `n` values exist and while the last `n` values hold a `None` (ATR warm-up values, for instance). `stdev` is the population deviation, as TradingView's `ta.stdev`.

RED, before any implementation, with `PYTHONPATH=src .venv/bin/python -m unittest tests.test_pine_indicators -v` (paths shortened). The error names `rolling_median` because the import list is alphabetical; `sma` and `stdev` were missing as well.

```text
test_pine_indicators (unittest.loader._FailedTest) ... ERROR

======================================================================
ERROR: test_pine_indicators (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_pine_indicators
Traceback (most recent call last):
  ...
  File ".../tests/test_pine_indicators.py", line 6, in <module>
    from bot.strategy.pine_indicators import (
ImportError: cannot import name 'rolling_median' from 'bot.strategy.pine_indicators' (.../src/bot/strategy/pine_indicators.py)

----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (errors=1)
```

GREEN:

- `_rolling_complete(values, n, pick)` wraps the existing `_rolling`, so the period check and the window slicing stay in one place; a window that holds a `None` gives `None`. `sma`, `stdev` and `rolling_median` are one line each over `statistics.fmean`, `pstdev` and `median`.
- `_rolling` is annotated `Sequence[Optional[float]]`. This is an annotation only: `rolling_max` and `rolling_min` behave as before.
- The module docstring now describes the window statistics.

Nine new tests in `WindowStatisticTests`: the five from the plan (`sma` None-then-mean, a window holding a `None`, population stdev, odd and even medians, prefix stability), plus the `None` rule for all three functions on one series with warm-up and a later gap, an exactly zero deviation for a flat window, short and empty input, and a period below 1. The prefix test uses a series with a gap every 25th value, so about a third of the windows hold a `None`; its seeded run compares both `None` and numeric values for each function.

Verification:

```text
PYTHONPATH=src .venv/bin/python -m unittest tests.test_pine_indicators -v
Ran 25 tests in 0.5s — OK                     (16 existing + 9 new)
pine_indicators.py line coverage: 100% (120 statements, 0 missed; focused run under Python 3.11, because .venv has no coverage module)
Mutation check (scratch script, not committed): 8 plausible bugs patched in one at a time, all caught —
  sample deviation, None not blanked, None dropped from the window, short windows computed,
  look-ahead window, no period check, unsorted median, lower-middle median
Full suite (PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v)
Ran 1585 tests in 36.0s — OK                  (Python 3.10.20)
compileall: passed
git diff --check: passed
secret hygiene test (tests.test_secret_hygiene): passed, with this document tracked
```

Two earlier full runs were made while unrelated jobs held the machine's load average between 50 and 160. Both stalled at `test_telemetry_poller.RunLoopTests.test_run_keeps_polling_until_cancelled`, the known Python 3.10 hang (a bare `asyncio.wait_for` in the poller can swallow a cancellation, so the poller loop never stops; telemetry code this task does not touch). Each was stopped after more than 2 minutes with 0 failures among the 1,349 tests that ran before it; the 235 tests after it also passed on their own (`Ran 235 tests`, OK), and the stalled test passes alone in about 0.03 s (three runs out of three). No `bot` module imports `pine_indicators` (`test_pine_gate_structure` enforces it), so the poller cannot see this change. The third run, on a quiet machine, was clean.

Note: `pine_indicators.py` is inside the Pine gate's code fingerprint (`research.pine_gate.lock`), so the Pine selection file's `code_sha256` no longer matches current code. That was already so after PR #36; the Pine holdout ran once and its run marker refuses a second test regardless.

## Task 2 — Modes configuration (YAML)

`config/modes.yaml` is the block of spec §6.3 copied byte for byte (a `diff` against spec lines 256-340 prints nothing; 85 lines, UTF-8, ends in a newline). `bot.strategy.modes.config` loads it into frozen dataclasses (`GeneralConfig`, `SelectorConfig`, `PairConfig`, `PullbackConfig`, `BreakoutConfig`, `SidewaysConfig`, `NearMeanConfig` and `TestConfig`, held by `ModesConfig`) and is the one source of every number the later tasks read. The lock file will bind `config_sha256(path)`, the sha256 of the file's bytes, so the loader refuses instead of guessing. Every refusal is a `ModesConfigError` whose message starts with the full YAML key path (`umum.extra`, `mode.A_pullback_long.sl_persen`), and all unknown and missing keys of a block are named in one message. PyYAML (`PyYAML>=6.0`, used through `yaml.safe_load` only) is added to `requirements.txt` and `pyproject.toml`.

What the loader refuses:

- an unknown or missing key at any level, a block that is not a mapping, a key written twice in one mapping (`safe_load` would silently keep the last; the check walks the composed nodes, so objects are still built by `safe_load` alone, and an alias loop cannot hang it);
- the wrong type: a `bool` is never a number, a float is never a whole number, `null` is never a value; a whole number is accepted for a float field and comes back as a float; `NaN`, infinities and ints beyond the float range are refused;
- percents and multipliers that are not greater than 0 (the two `lolos_hasil_*` thresholds may be negative); counts and periods below 1 (`jeda_menit` and `lolos_buang_posisi_teratas` may be 0);
- `tf_pemicu` other than `15m`/`5m` (A, D2) or `3m` (D); `faktor_k` below 1.0 or any text but `otomatis` (returned as `None`); `harus_sama_kondisi_dengan` other than a listed pair; `zona_waktu` other than `Asia/Jakarta` (controller ruling: the harness hard-codes WIB); `ai_boleh_buka_tutup: true`; a weekly window that is not a string or does not parse non-empty with `bot.core.entry_window.parse_weekly_window`;
- `uji` dates that are not quoted strings of the exact shape `YYYY-MM-DDTHH:MM:SS+HH:MM`, that are not midnight in Asia/Jakarta, or that do not satisfy `latihan_mulai < latihan_selesai < ujian_selesai`. They come back as aware datetimes in the +07:00 zone, whatever offset the file used. The shape is pinned because `datetime.fromisoformat` accepts more on 3.11 (CI) than on 3.10 (the `.venv`): a trailing `Z` or `+0700` would validate in one and not in the other.

Choices the brief left open: the pair names are fixed (`BTC-USDT` and `ETH-USDT`, both required); `pair` is a `MappingProxyType`, so it is read-only (`dataclasses.asdict` and `copy.deepcopy` cannot copy it; the docstring says so); mode B keeps its YAML key `maks_jarak_di_atas_sma20_1h_kali_atr` as its field name, since only modes A and D2 (which share `PullbackConfig`) map their two keys onto `maks_jarak_sma20_1h_kali_atr`.

RED, before any implementation, with `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_config -v` (paths shortened):

```text
test_modes_config (unittest.loader._FailedTest) ... ERROR

======================================================================
ERROR: test_modes_config (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_modes_config
Traceback (most recent call last):
  ...
  File ".../tests/test_modes_config.py", line 19, in <module>
    from bot.strategy.modes.config import (
ModuleNotFoundError: No module named 'bot.strategy.modes'

----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (errors=1)
exit code: 1
```

GREEN, the same command after implementing: the first run passed 52 of 53 tests. The failing test was mine (it used a YAML alias before its anchor, which YAML rightly calls undefined) and was fixed in the test, not in the loader. After two more tests, one more case in an existing test and a faster fixture the module has 55 tests:

```text
Ran 55 tests in 1.1s

OK
```

The tests include the brief's six named cases (the first with its assertions verbatim) and are data-driven over a rule table that must cover every leaf of the repo file (`test_the_rule_tables_cover_every_leaf_of_the_repo_file`), so adding a key to the YAML without a rule fails a test.

Verification:

- `.venv` was built by `uv` and has no `pip`, so `uv pip install --python .venv/bin/python -r requirements.txt` stood in for `.venv/bin/pip install -r requirements.txt`. It installed PyYAML 6.0.3 and `coverage` 7.16.2 (listed in `requirements.txt` but missing from the venv).
- Focused: 55 tests OK on Python 3.10.20 (`.venv`) and on 3.11.15 (a scratch venv, because CI runs 3.11).
- Line coverage of `bot/strategy/modes/`: 100 % (254 statements, 0 missed). Under 3.10 `coverage` cannot read `pyproject.toml` without `tomli`, so this focused run used a scratch rcfile; CI's 3.11 reads the real one.
- Mutation check (scratch script, not committed): 55 plausible bugs planted one at a time in a scratch copy of the final `src`, all caught (bool taken as a number, unknown or missing keys ignored, `ai_boleh_buka_tutup` allowed, naive or non-midnight dates, a repeated key allowed, the alias guard dropped, mode A reading D2's key, a writable `pair`, a hash of stripped text, and so on). One mutant survived an earlier pass: without the `isinstance(str)` check on the weekly window, an object whose `str()` looks like a window was accepted, because `parse_weekly_window` calls `str()` on its argument. A test for it was added.
- The existing guards cover the new package: planting `from bot.strategy import pine_indicators` and `import research.pine_gate` into a scratch copy of `config.py` made `test_no_live_module_imports_pine_indicators` and `test_no_bot_module_or_main_imports_research` fail.
- Full suite, exact command `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`: `Ran 1643 tests in 308.039s` / `OK` (1,588 existing + 55 new), exit 0, with the machine's load average at 40-107 and no stall.
- `compileall -q src tests` OK; `git diff --check` clean (new files marked intent-to-add); `tests.test_secret_hygiene` OK with the new files and this document tracked.

### Fix round 1 (task review)

The review found that "every content problem raises `ModesConfigError`" was not true. `yaml.safe_load` raised a bare `ValueError` for an unquoted date that no calendar has (`latihan_mulai: 2025-13-01T00:00:00+07:00`, `2025-08-32`, `jeda_menit: 2025-02-30`), and `astimezone` sat outside the `try`, so the quoted `"0001-01-01T00:00:00+07:00"` raised a bare `OverflowError`. Nothing wrong ever loaded, but a CLI that maps `ModesConfigError` to a one-line `error:` would have printed a traceback. Reproducing it showed the family is wider: `!!bool maybe` (`KeyError`), `!!timestamp abc` (`AttributeError`), `!!int abc`, `!!float abc`, an hour 25, an offset of +25:00 and year 0000 (`ValueError`), `9999-12-31T23:59:59-23:59` (`OverflowError`) and 1,500-deep nesting (`RecursionError`) all escaped too. A hex literal of more than 4,300 digits broke the message code itself, because CPython will not print so long an int.

RED, the new tests against the unfixed loader: `Ran 59 tests` / `FAILED (errors=23)`, every error the bare library exception (`ValueError: month must be in 1..12`, `OverflowError: date value out of range`, `KeyError: 'maybe'`, `AttributeError: 'NoneType' object has no attribute 'groupdict'`, `RecursionError: maximum recursion depth exceeded`). The tests for the unprintable int came second: `Ran 61 tests` / `FAILED (errors=8)`, all `ValueError: Exceeds the limit (4300) for integer string conversion`.

The fix, all in `config.py`:

- `_wib_midnight`: `astimezone` moves inside the `try`, and `OverflowError` is caught with `ValueError`.
- `_parse_yaml`: each PyYAML call (`compose`, `safe_load`) sits in its own `try` that turns any `Exception` into `ModesConfigError("cannot read as YAML: ...")`. `Exception` rather than `ValueError` because `KeyError`, `AttributeError` and `RecursionError` escaped as well. The repeated-key refusal stays outside both, so it is not wrapped a second time (`ModesConfigError` is itself a `ValueError`).
- `load_modes_config` now only handles bad UTF-8 and prefixes the file name, since YAML failures are converted earlier.
- `_show` replaces `repr` and `str` in `_describe`, `_join` and the "at least N" message, so naming a value or a key can never raise.

GREEN: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_config` gives `Ran 61 tests` / `OK` (Python 3.10.20 and 3.11.15). Six tests are new and two existing ones gained cases. These numbers supersede the counts above: line coverage of `bot/strategy/modes/` 100 % (267 statements); 66 planted bugs, all caught; full suite `Ran 1649 tests` / `OK`; a fuzz of 25,034 hostile documents through `parse_modes_config` raised nothing but `ModesConfigError`. A message for an unquoted impossible date carries the file name and PyYAML's text (`month must be in 1..12`) but no key path, because PyYAML's constructor error carries no position.

## Task 3 — Mode selector

`bot.strategy.modes.regime` holds the manager's `pilih_mode` (spec §2.2) as a pure function of the five numbers and the band multiple. It keeps the document's order and operators: SIDEWAYS first and strict (`gap < sideways_jarak_kali_atr * atr14_1h`), then NAIK (`sma20_1h > sma50_1h and close_4h > ema50_4h`), then TURUN (`sma20_1h < sma50_1h`, with no 4h filter, on purpose), else TIDAK_ENTRY. The one addition is the guard the brief asks for: a `None` in any of the five numeric inputs returns TIDAK_ENTRY before anything is computed. The spec's literal `0.2` is the `sideways_jarak_kali_atr` parameter, which is `pemilih_mode.sideways_jarak_kali_atr: 0.2` in `config/modes.yaml`. The module imports only `typing`, and no live module imports it.

Choices the brief left open: the guard is an explicit `or` chain of `is None` tests, so a `0.0` is a value and not a missing input; only the five inputs are guarded, and `sideways_jarak_kali_atr` is a configured float that is not checked. A NaN (not `None`) is not guarded. A NaN ATR makes `gap < ...` False and lets NAIK or TURUN decide, while a NaN SMA falls through to TIDAK_ENTRY (probed: `pilih_mode(101.0, 100.0, nan, 3.0, 2.0, 0.2)` returns NAIK). The brief limits the guard to `None` and the module to `typing`, so the producer (Task 5) must keep NaN out of these inputs.

RED, before any implementation, with `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_regime -v` (paths shortened). The test file already held the brief's five tests and the precedence test described below:

```text
test_modes_regime (unittest.loader._FailedTest) ... ERROR

======================================================================
ERROR: test_modes_regime (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_modes_regime
Traceback (most recent call last):
  File ".../unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File ".../tests/test_modes_regime.py", line 5, in <module>
    from bot.strategy.modes.regime import NAIK, SIDEWAYS, TIDAK_ENTRY, TURUN, pilih_mode
ModuleNotFoundError: No module named 'bot.strategy.modes.regime'


----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (errors=1)
exit code: 1
```

GREEN: after `regime.py` was written, the same file ran six tests and passed. A band test was added afterwards, and the final focused run is:

```text
PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_regime -v
test_any_missing_input_is_tidak_entry ... ok
test_gap_equal_to_the_band_is_not_sideways ... ok
test_naik_needs_the_4h_close_above_its_ema ... ok
test_sideways_is_checked_before_both_trends ... ok
test_sideways_when_gap_is_below_the_band ... ok
test_the_band_is_the_given_multiple_of_atr ... ok
test_turun_ignores_the_4h_filter ... ok

Ran 7 tests in 0.000s

OK
```

The five brief tests keep their exact numbers. The `None` test runs the five inputs through `subTest`, and first checks that its base case is a NAIK case, so the `None` is the only cause of the result. Two tests go beyond the brief's list. The precedence test was written from the brief's own comment ("checked before the trend"). The band test was added after GREEN, because every brief test passes `0.2`. The mutation check below then showed that the brief's five alone let three plausible bugs through, and that these two catch them:

- `test_sideways_is_checked_before_both_trends`: the brief's `test_sideways_when_gap_is_below_the_band` has `close_4h` under its EMA, so NAIK does not hold there and the test cannot tell the order apart. This one has NAIK and TURUN each holding on one of its inputs.
- `test_the_band_is_the_given_multiple_of_atr`: every brief test passes `0.2`, and an ATR of 1.0 or 5.0 cannot show the scaling.

Mutation check (scratch copies of `regime.py`, not committed): 14 planted bugs, each run against the seven-test file and against the brief's five tests alone.

- Caught by both sets (10): no `None` guard (the five `subTest`s error), guard returns SIDEWAYS, guard misses ATR, band with `<=`, NAIK without the 4h filter, NAIK with `>=` on the 4h close, NAIK with the SMA direction reversed, TURUN with the 4h filter, TURUN with `>`, gap without `abs`.
- Caught only by the two added tests (3): SIDEWAYS checked after NAIK, band hard-coded at `0.2`, band ignoring ATR.
- Caught by neither (1): a truthiness guard (`not x`) in place of `is None`. It reads a `0.0` input as missing and returns TIDAK_ENTRY, which fails closed. No test pins it.

Verification:

- Focused: `Ran 7 tests` / OK on Python 3.10.20 (`.venv`).
- Line coverage of `bot/strategy/modes/regime.py`: 100 % (16 statements, 0 missed), measured with a scratch rcfile because coverage cannot read `pyproject.toml` on 3.10 without `tomli`, as in Task 2.
- Full suite, `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`: `Ran 1656 tests in 35.176s` / OK (1,649 existing plus 7 new), exit 0.
- `compileall -q src tests` OK; `git diff --check` clean (new files marked intent-to-add).

## Task 4 — Backtest package, data loading and copy check

`src/research/mode_backtest/` starts here, and the bot never imports it. `data.py` is the one place the harness reads bar files. `load_pair` returns a `PairData` (the 1m bars of an inclusive WIB day range and the native 1h and 4h warm-ups), `stage_files` names exactly the files such a load opens, `manifest` and `manifest_sha256` hash them (a later task's lock binds that hash), and `verify_copy` checks a local copy of the data against the VPS's `sha256sum` output. It uses only public names of `research.pine_gate` (`bars`, and `cli_common.file_sha256`) and imports nothing but research modules and the standard library. The rule behind every choice below: a missing, unreadable or altered file stops a run with an error and never shrinks the sample.

Choices the brief left open:

- `load_pair` checks the days first. `load_days` names every missing day before it reads any file, so a missing day wins over a missing warm-up and nothing is read. Then both warm-ups are checked together, and one `FileNotFoundError` names each missing one. A corrupt file or an invalid bar raises what `bars` raises (`UnreadableDayError`, `DayShapeError`, `BarGapError`); nothing is caught or replaced by an empty series. Once both warm-ups are read, one that adds nothing is refused (Fix round 1).
- `stage_files` only builds names, so nothing has to exist yet. It refuses a reversed range, because an empty range would give a manifest of the two warm-ups alone. A test records every `gzip.open` of a `load_pair` call and requires the sorted list to equal `stage_files`, so the manifest cannot drift from what the load reads.
- `manifest` lines are `relpath:sha256`, relative to `data_dir` with `/` separators, so a copy of the data has the manifest of the original. A path outside `data_dir` or given twice raises `ValueError`. `manifest_sha256` sorts the lines itself, so the hash does not depend on the order in which a caller joined two symbols, and it refuses a bare string, which would be sorted letter by letter.
- `verify_copy` compares both ways (Fix round 1) and reports one problem for each of these:
  - the hash differs (both hashes are printed), the file is missing, or it cannot be read (a directory in its place is a problem, not a crash);
  - the line is not `<64 hex>  <path>` (two spaces, as `sha256sum` prints in text mode; the binary-mode `*` is malformed), or the path is not a file inside `prod/`: absolute, with `..`, outside `prod/` or holding a NUL. Such a path is never read, so a bad listing cannot make the check hash another file;
  - the same path appears twice, because two entries for one file could otherwise cancel out;
  - a regular file under `prod/` that the listing does not name (a stray file, or a listing cut short).

  Blank lines, CRLF line ends and upper-case hex are tolerated. A listing with no file entry at all is one problem, so an empty file cannot pass, and a lone malformed line is one problem, not two. `prod/<symbol>/funding.json` (three path components, not four) is a normal entry although `stage_files` does not list it, since this harness does not read funding.

`tests/mode_backtest_helpers.py` (not `test_*`; tests import it as `from tests.mode_backtest_helpers import ...`, as `tests/test_telemetry_book_ticker.py` does) writes synthetic data that looks like the real data:

- `minute_rows` and `write_days` write valid 1,440-bar WIB day files of flat bars; `price(i)` counts minutes from 00:00 WIB of the earliest day written, so one function gives a continuous series, also across a day that is left out.
- `write_warmups` writes the 1h and 4h warm-ups so that the last bar is the one that contains the minute before the first day starts, on the UTC grid. Both open at 16:00 UTC when the day starts at 00:00 WIB, as on the real files, and a test joins them to the aggregated bars with `features.htf_bars`, which raises on a hole at the seam.
- `sha256sum_text` prints what `find prod -type f | sort | xargs sha256sum` prints on the VPS.

`tests/test_mode_backtest_structure.py` runs its rules over every `*.py` of the package, so a module added by a later task is covered without editing it:

- no import of a private name of another module (relative, `research.*` and `bot.*` imports; also a private module, and a private name reached through an imported module such as `bars._empty`);
- none of `httpx`, `socket`, `urllib`, `requests`, `bingx_client`, `_signed_request`, `X-BX-APIKEY` and `listenKey` anywhere in the text, docstrings and comments included;
- no `write_text`, `write_bytes` or `open`-style call (`open`, `fdopen`, `GzipFile`) with a mode that can write; a non-constant `mode=` counts too.

Each detector also runs on small synthetic sources that must and must not trip it, so a rule cannot pass just because it looks at nothing.

RED, before the package existed, with `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_data tests.test_mode_backtest_structure -v` (paths shortened). The first run, in the repo, used the first version of the tests and failed the same way; the output below is the same command on the final tests in a scratch copy of the tree without `src/research/mode_backtest`, so it matches what is committed:

```text
test_mode_backtest_data (unittest.loader._FailedTest) ... ERROR
test_files_are_written_only_through_fileio (structure.FileWriteTests) ... ok
test_the_write_guard_lets_reading_through (structure.FileWriteTests) ... ok
test_the_write_guard_sees_every_way_to_write_a_file (structure.FileWriteTests) ... ok
test_no_network_or_venue_code (structure.NetworkAndVenueTests) ... ok
test_the_word_guard_sees_every_word_in_code_comments_and_docstrings (structure.NetworkAndVenueTests) ... ok
test_the_rules_below_look_at_real_modules (structure.PackageLayoutTests) ... FAIL
test_no_module_imports_a_private_name_of_another (structure.PrivateImportTests) ... ok
test_no_module_reaches_a_private_name_through_an_imported_module (structure.PrivateImportTests) ... ok
test_the_attribute_guard_lets_public_names_and_own_privates_through (structure.PrivateImportTests) ... ok
test_the_attribute_guard_sees_a_private_name_behind_an_import (structure.PrivateImportTests) ... ok
test_the_import_guard_lets_public_names_and_other_packages_through (structure.PrivateImportTests) ... ok
test_the_import_guard_sees_every_form_of_a_private_import (structure.PrivateImportTests) ... ok

======================================================================
ERROR: test_mode_backtest_data (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_mode_backtest_data
Traceback (most recent call last):
  File "<py>/unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File "<repo>/tests/test_mode_backtest_data.py", line 14, in <module>
    from research.mode_backtest import data as backtest_data
ModuleNotFoundError: No module named 'research.mode_backtest'


======================================================================
FAIL: test_the_rules_below_look_at_real_modules (structure.PackageLayoutTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "<repo>/tests/test_mode_backtest_structure.py", line 144, in test_the_rules_below_look_at_real_modules
    self.assertIn("__init__.py", names)
AssertionError: '__init__.py' not found in []

----------------------------------------------------------------------
Ran 13 tests in 0.001s

FAILED (failures=1, errors=1)
```

This is the expected failure. The data tests cannot import `research.mode_backtest`. Three structure rules (private imports, forbidden words, file writes) pass on an empty package because there is nothing to violate; `test_the_rules_below_look_at_real_modules` is the one that fails, and it is there so that these rules cannot pass vacuously once the package is gone or moved. The mutation check below shows that they trip on a violating module.

GREEN after `__init__.py` and `data.py`:

```text
PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_data tests.test_mode_backtest_structure
Ran 63 tests in 0.5s

OK
```

The first run passed all 60 tests then in the two files, which is why the mutation check came next (below). Two tests were added afterwards (a very long listing line is cut in its message; a string `data_dir` works like a path) and one was split in two.

Verification:

- Focused: `Ran 63 tests` / OK (51 in `test_mode_backtest_data`, 12 in `test_mode_backtest_structure`) on Python 3.10.20 (`.venv`) and on 3.11.15 (a scratch venv with `requirements.txt`, because CI runs 3.11).
- Line coverage of `research/mode_backtest/`: 100 % (`data.py` 96 statements and 26 branches on 3.10 with a scratch rcfile, since coverage cannot read `pyproject.toml` on 3.10 without `tomli`; 95 statements, 0 missed, on 3.11 with the repo's own `pyproject.toml`).
- Mutation check (scratch scripts, not committed): 38 plausible bugs planted one at a time in `data.py`, 10 violating modules dropped into the package for the structure rules, and 8 bugs in the test helpers; all 56 are caught. Examples: a reversed range allowed, the last day dropped, `..` or a NUL allowed in a listing path, one space accepted as separator, a short hash accepted, a duplicate allowed, an empty listing passing, an unreadable file crashing, a manifest with absolute paths, an unsorted manifest hash, a warm-up that ends on the first minute instead of before it. The first pass had two survivors (one space accepted, a short hash accepted): the malformed lines all named `funding.json`, which the base listing already holds, so a mis-parsed line still produced a "listed twice" problem. Each shape is now its own listing with a path that is not in the local copy.
- Realistic scale (scratch, 439 synthetic days for two symbols plus 4,344 1h and 1,086 4h warm-ups each): `load_pair` 1.3 s and both warm-up seams join; the manifest of 441 staged files 0.02 s; `verify_copy` of 882 listed files 0.03 s.
- Existing guards stay green with the new package: `tests.test_pine_gate_structure` scans every research source for the venue words and every research import of a `bot` module.
- Full suite, exact command `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`: `Ran 1719 tests in 35.538s` / OK (1,656 existing + 63 new), exit 0.
- `compileall -q src tests` OK; `git diff --check` clean (new files marked intent-to-add); `tests.test_secret_hygiene` OK with the new files and this document tracked.

### Fix round 1 (task review)

The review found two ways to a wrong result with no error. The controller ruled that spec §7 (a copy is accepted only when every file matches the VPS) outranks the plan's "the listing is the authority".

- `verify_copy` could return `[]` without comparing every file the harness reads. A listing cut after its first line returned `[]`, and so did a day file that exists locally but is absent from the listing: `load_pair` then loads it and `manifest(stage_files(...))` binds its hash into the training manifest of a one-shot lock. After the loop, every regular file under `<data_dir>/prod` that the listing does not name is now a problem (`<path>: local file is not in the listing`), in path order. Skipped: names starting with a dot (`.DS_Store`, `._name`), directories, and everything outside `prod/` (the run markers live in `holdout_runs/`). An empty listing stays exactly one problem; the stray check runs only when at least one file is listed, so a listing with no valid line gets only its own line problems. `[]` now means every local file under `prod/` was compared with the VPS and matched.
- `load_pair` accepted an empty warm-up, or one that starts at or after the first aggregated bar, and `htf_bars` joins it silently. The 4h EMA50 and the 720-hour ATR median then stay `None` for weeks, the selector answers TIDAK_ENTRY and trades drop with no error. The Pine loader treats exactly this as a missing warm-up (`dataset.py`) and its pipeline refuses the run. Once both warm-ups are read, `load_pair` raises `ValueError` naming every file for which `features.warmup_unused` holds. A corrupt warm-up still raises its own error first, and one bar before the first aggregated bar is enough.

RED, the final tests against the previous `data.py` (a scratch tree; excerpt, paths shortened), `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_data tests.test_mode_backtest_structure`:

```text
ERROR: test_a_stray_local_file_is_a_problem (VerifyCopyTests)
ValueError: not enough values to unpack (expected 1, got 0)
FAIL: test_a_truncated_listing_is_not_an_exact_copy (VerifyCopyTests)
AssertionError: 0 != 5 : []
FAIL: test_a_listing_cut_in_the_middle_of_a_line_is_not_an_exact_copy (VerifyCopyTests)
AssertionError: 1 != 6 : ["line 2: not '<64 hex digits>  <path>': '4123e3921242ce94f58b73a66c2901359f8c9a4a'"]
FAIL: test_every_stray_file_is_one_problem_in_path_order (VerifyCopyTests)
AssertionError: 0 != 3 : []
FAIL: test_verify_copy_reports_every_mismatch_and_missing_file (VerifyCopyTests)
AssertionError: 5 != 6 : ['prod/BTC-USDT/1m/2026-07-02.csv.gz: sha256 differs (local c2c8d4d2..., VPS f81fb903...
FAIL: test_an_empty_warmup_is_refused (LoadPairTests) (interval='1h')
AssertionError: ValueError not raised
FAIL: test_an_empty_warmup_is_refused (LoadPairTests) (interval='4h')
AssertionError: ValueError not raised
FAIL: test_a_warmup_that_starts_at_or_after_the_first_aggregated_bar_is_refused (LoadPairTests) (interval='1h', bars_late=0)
AssertionError: ValueError not raised
  (the same for ('1h', 5), ('4h', 0) and ('4h', 5))
FAIL: test_every_unused_warmup_is_named (LoadPairTests)
AssertionError: ValueError not raised

Ran 75 tests in 0.525s
FAILED (failures=11, errors=1)
```

The other new tests (dot names, directories and files outside `prod/` are not strays, a listed-but-missing file is not also a stray, one seam bar is enough, an unreadable warm-up is reported before an unused one) guard what must not change, and pass on the old code too. The mutation check below shows they bite. GREEN, the same command after the change: `Ran 75 tests` / OK.

Verification (these numbers supersede the Verification numbers above):

- Focused: `Ran 75 tests` / OK (63 in `test_mode_backtest_data`, 12 in `test_mode_backtest_structure`; 12 new) on Python 3.10.20 (`.venv`) and 3.11.15 (scratch venv). `test_malformed_lines_are_each_one_problem` and `test_one_malformed_line_is_one_problem_not_two` now run with a full local copy present, and the brief's named `test_verify_copy_reports_every_mismatch_and_missing_file` also carries a stray file.
- Line coverage of `research/mode_backtest/`: 100 % (`data.py` 107 statements and 30 branches on 3.10 with the scratch rcfile; 106 statements, 0 missed, on 3.11 with the repo's `pyproject.toml`).
- Mutation check (scratch, not committed): 56 bugs planted in `data.py` (the 38 of the first pass with moved patterns updated, plus 18 for this round: strays never reported, dot names or directories counted as strays, the scan covering the whole data dir or not recursing, the scan also running when nothing valid is listed, unsorted strays, only one of the two warm-ups checked, only an empty warm-up refused, another exception type, no file named, the second warm-up not read before the unused check) and 10 violating modules for the structure rules: all 66 caught; the 8 helper mutants are still caught.
- The two reproductions from the review, replayed on the fixed code: a 4-file copy with a listing cut after its first line gives 3 problems; a valid extra day file gives 1 and `[]` once removed; an empty listing with a full local copy gives exactly 1; an empty 4h warm-up and a 4h warm-up whose first bar opens at the first aggregated bar both raise `ValueError` naming `prod/BTC-USDT/4h/warmup.csv.gz`, and the real warm-ups still load.
- Realistic scale (scratch, 439 days x 2 symbols + 4,344 / 1,086 warm-ups): `load_pair` 1.5 s with the unused check (1.3 s before); `verify_copy` of 882 listed files 0.03 s with the stray scan.
- Full suite, exact command `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`: `Ran 1731 tests in 35.601s` / OK (1,719 + 12 new), exit 0.
- `compileall -q src tests` OK; `git diff --check` clean; `tests.test_secret_hygiene` OK with this document tracked.

## Task 5 — Hourly frame, condition at a time, NAIK days

`src/research/mode_backtest/frames.py` starts here; the bot never imports it. `HourlyFrame.build(pair, sel)` joins the native 1h and 4h warm-ups to the candles aggregated from the 1m bars (`features.htf_bars`), computes the selector's series once (SMA fast and slow of the 1h closes, ATR14 (1h), the median of the last 720 ATR14 values, the 4h EMA) and keeps them as tuples. `context_at(t_ms)` only looks them up with `last_closed_index`, so at `t` the candles with `open + interval <= t` are visible and nothing else (D2). It returns a frozen `HourlyContext(sma20_1h, sma50_1h, atr14_1h, atr_median, close_4h, ema50_4h, kondisi)`, where `kondisi` is `pilih_mode` of the five selector inputs. `naik_days(start_ms, end_ms, window, min_naik_hours)` returns the WIB days that have at least `min_naik_hours` NAIK hours inside the weekly entry window (the first version took no minimum; see Fix round 1).

Choices the brief left open:

- **The day of a candle is the WIB day of its close.** The range, the window and the condition are all read at the close, so the candle ending at 00:00 WIB belongs to the new day (a UTC day would put it on the old one). The range is half open on the close: a close at `start` counts, a close at `end` does not. Warm-up candles count when the range reaches them. An empty range gives no days, a reversed one raises `ValueError` (it can only be a caller bug, and the answer "no NAIK days" would shrink the sample without a word).
- **The window is checked by `bot.core.entry_window.window_is_open`** on the WIB datetime of the close, built in one small function (`_wib_moment`, exact integer arithmetic, the fixed +07:00 `WIB` of `research.pine_gate.spec`, the same zone as Asia/Jakarta and the one `wib_day` uses). `window=None` is always open. A later task routes this one conversion through the shared helper.
- **The context fields keep the names of the document's defaults** (`sma20_1h`, `sma50_1h`, `ema50_4h`) and hold the periods of `SelectorConfig`. The ATR period is the module constant `ATR_PERIOD_1H = 14` (the document's ATR14; it is not a YAML key). `ATR_MEDIAN_HOURS = 720`.
- **A missing input never reaches the selector as anything but `None`.** An index of -1 (no candle closed yet) is guarded in one place, because `column[-1]` would silently return the last value of the series, which is the future. The median is not a selector input: `kondisi` is defined while `atr_median` is still `None`.
- **No NaN can reach `pilih_mode`** (it guards `None` only): the bars are validated finite when they are read, and the series are plain averages and medians of them. `frames.py` adds no division of its own. A test sweeps 2,140 contexts, from before the first candle to the end of the data, and requires every number to be finite or `None`.
- `HourlyFrame(bars_1h, bars_4h, sel)` takes the joined series and refuses any interval but 1h and 4h (a swapped pair would put every "closed" boundary in the wrong place without an error); `build` is the entry point. A hole at a warm-up seam raises `BarGapError` from `htf_bars` and stops the build.
- `tests/test_pine_gate_structure.py`: `_bot_sources_imported_by_research` now walks `PACKAGE` (`research/pine_gate`) and its docstring says so. It guards the Pine lock's code fingerprint, which was never meant to cover the `bot/strategy/modes` imports of the mode backtest.
- `tests/mode_backtest_helpers.py`: `write_warmups(..., price=...)` now prices a bar by the minutes from 00:00 WIB of `first_day` to the minute it opens (negative for a warm-up bar), the axis `write_days` already used, so one price function runs on across the seam. Before, it counted bars from the first bar of each file. A new helper test pins this; the old ones never passed a price to the warm-ups and stay green.

Test design: every price is a function of time (flat inside each UTC hour, or each UTC 4h bucket where a test reads 4h candles), written through the helper to the day files and warm-ups, loaded with `load_pair`, and every expected number is computed in the test from that function (`HourlyReference`, plus `pine_indicators.atr`/`ema` over the generated closes), never from the frame. Beyond the six tests named in the brief:

- the hour boundary is read at -1 ms, 0 and +1 ms around a close, and the six 4h closes are each read at 0 and 1 ms before (13 times a day) on both the seam day (Monday, whose 03:00 close is the candle that straddles the seam) and a normal day, with `close_4h` and `ema50_4h` decoded back to the candle they belong to;
- a poisoning test rebuilds the frame with every 1m bar from `t` on priced 50 times higher: the context at `t` must not change, at an hour close, a 4h close and inside an hour; poisoning the candle that just closed must change it;
- the ATR median is checked at 719 and 720 values, and against windows that are one short, one long, or without the latest value, on a volatility that rises every hour so that those windows give different numbers;
- `naik_days` has the brief's window case (Monday 06:00 out, 07:00 in, Saturday out) with the condition stubbed per close, the WIB day roll at 00:00, the half-open range, each non-NAIK condition, `None` window, and real rising and falling weeks.

RED, before `frames.py` existed, with `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_frames tests.test_pine_gate_structure -v` (paths shortened; the 13 Pine structure tests all pass at this point and are left out):

```text
test_mode_backtest_frames (unittest.loader._FailedTest) ... ERROR

======================================================================
ERROR: test_mode_backtest_frames (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_mode_backtest_frames
Traceback (most recent call last):
  File "<py>/unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File "<repo>/tests/test_mode_backtest_frames.py", line 24, in <module>
    from research.mode_backtest import frames
ImportError: cannot import name 'frames' from 'research.mode_backtest' (<repo>/src/research/mode_backtest/__init__.py)

----------------------------------------------------------------------
Ran 14 tests in 0.622s

FAILED (errors=1)
```

This is the expected failure. The brief expects the Pine fingerprint test to fail on `bot/strategy/modes/regime.py` too, but nothing under `research/` imports that module until `frames.py` exists, so it fails only at the next step: with `frames.py` written and the scope change not yet made, `PYTHONPATH=src .venv/bin/python -m unittest tests.test_pine_gate_structure`:

```text
FAIL: test_fingerprint_covers_every_bot_module_the_research_imports (tests.test_pine_gate_structure.CodeFingerprintCoverageTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "<repo>/tests/test_pine_gate_structure.py", line 64, in test_fingerprint_covers_every_bot_module_the_research_imports
    self.assertEqual(sorted(imported - covered), [])
AssertionError: Lists differ: ['bot/core/entry_window.py', 'bot/strategy[45 chars].py'] != []

First list contains 3 additional elements.
First extra element 0:
'bot/core/entry_window.py'

- ['bot/core/entry_window.py',
-  'bot/strategy/modes/config.py',
-  'bot/strategy/modes/regime.py']

Ran 13 tests in 0.568s

FAILED (failures=1)
```

The helper's price axis had its own RED (a new test in `HelperTests`, run against the previous helper): `test_the_price_axis_is_shared_by_the_warmups_and_the_days` failed with `Lists differ: [1000.0, 1000.0166666666667, 1000.0333333333333] != [997.0, 998.0, 999.0]`, the warm-up having been priced by bar number. GREEN after the helper change: `tests.test_mode_backtest_data tests.test_mode_backtest_structure`, 76 tests / OK.

GREEN, the brief's command after `frames.py` and the scope change: `Ran 42 tests in 1.105s` / OK (29 frames tests and the 13 Pine structure tests). All 27 frames tests of the first version passed at once, which is why the mutation check came next. Two tests were added after it: a hole at the warm-up seam stops the build (it passed at once, the behaviour being `htf_bars`'s), and series of the wrong interval are refused (RED first: `AssertionError: ValueError not raised` for all three cases, GREEN with the guard in the constructor).

Verification:

- Focused: `tests.test_mode_backtest_frames tests.test_pine_gate_structure tests.test_mode_backtest_structure tests.test_mode_backtest_data` `Ran 118 tests` / OK on Python 3.10.20 (`.venv`): 29 frames, 13 Pine structure, 12 mode-backtest structure, 64 data. Python 3.11 was not run: it needs a scratch venv, and the compiled `pydantic_core` of `.venv` does not load there.
- Line coverage of `research/mode_backtest/frames.py`: 100 % (68 statements and 8 branches, 0 missed), measured with a scratch rcfile (coverage cannot read `pyproject.toml` on 3.10 without `tomli`).
- Mutation check (scratch scripts, not committed): 46 bugs planted one at a time in a copy of the tree, all caught: 43 in `frames.py`, 3 in the helper. Examples: the forming candle visible by 1 ms (1h and 4h), the just-closed candle invisible, a candle one behind, no guard for index -1, SMA periods swapped or hard-coded, ATR period 20, the 4h EMA period or the sideways multiple hard-coded, a median window of 719 or 721 or without the latest value, a median of the closes, `close_4h` and `ema50_4h` swapped, a warm-up not joined, a gap at the seam swallowed, the NAIK day taken in UTC or from the candle's open, the window read in UTC, at UTC+8 or at the open, a window `None` treated as closed, the range end included or the start excluded, the condition read at the open, any condition but TURUN counted, a reversed range accepted, the interval guard dropped or half of it. The first pass had one survivor, an early exit that skipped the check for a day already found; it changed nothing in the result, so it was removed.
- Realistic scale (scratch, 441 synthetic days, 4,344 1h and 1,086 4h warm-up candles, a random walk with all four conditions present): `HourlyFrame.build` 0.78 s; `naik_days` over the 441 days 22 ms; `context_at` 1.9 microseconds a call (211,680 calls in 0.40 s), so the simulation loop of Task 12 will not wait on it.
- Full suite, exact command `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`: `Ran 1761 tests in 36.967s` / OK (1,731 + 29 frames tests + 1 helper test), exit 0.
- `compileall -q src tests` OK; `git diff --check` clean (new files marked intent-to-add); `tests.test_secret_hygiene` OK with this document tracked; `ruff check --select F,E,W,B --line-length 100` clean on the five changed Python files (ruff is not configured in the repo; used as a lint only).

### Fix round 1 (task review)

The review found that `uji.hari_naik_minimal_jam_naik` was accepted by the config loader (any whole number of at least 1) and then ignored: `naik_days` fixed "one NAIK hour makes a NAIK day", so a value of 2 would have loaded without an error while "Entry per hari aktif" and the 5m switch kept counting a day with one NAIK hour. The plan gave `naik_days` no place for the number and no task read it. The controller ruled that the function takes it.

- `HourlyFrame.naik_days(start_ms, end_ms, window, min_naik_hours)`. The new parameter has no default, so a caller cannot leave the config value out (callers pass `cfg.uji.hari_naik_minimal_jam_naik`). Which closes count is unchanged (close in `[start, end)`, window open at the close, condition NAIK); they are now counted per WIB day of the close, and a day is kept when it has at least `min_naik_hours` of them. A value below 1 raises `ValueError`. The docstring says all of this.
- At 1, the value in `config/modes.yaml`, the results are unchanged. Checked against the committed version of the function (scratch script): 180 random ranges over 441 synthetic days, with no window, with `mon 07:00-sat 00:00` and with `tue 00:00-thu 12:00`, give identical sets, and the number of days only falls as the minimum rises (206 days at 1, 199 at 2, 178 at 6 and 0 at 25 for the weekly window), so an ignored key would have moved the count.
- Tests: the existing `naik_days` tests pass 1. New (`NaikDaysTests`): a day with exactly one NAIK window hour is kept at 1 and dropped at 2; a day with two (not next to each other) is kept at 2 and dropped at 3; an hour before the window opens (Monday 06:00) does not count; hours are counted per WIB day (23:00 and 00:00 are two days although one UTC day; one hour on each of two days is not two); closes outside `[start, end)` do not count; 0 and -1 raise; the parameter has no default; and a real rising week with the weekly window (Monday has 17 window closes, Tuesday to Friday 24) kept or dropped at 17, 18, 24 and 25, and without a window at 24 and 25.

RED, `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_frames tests.test_pine_gate_structure -v` against the three-argument function (excerpt, paths shortened):

```text
ERROR: test_naik_days_count_only_window_hours (tests.test_mode_backtest_frames.NaikDaysTests) [Monday 07:00 is its first hour]
  File "<repo>/tests/test_mode_backtest_frames.py", line 496, in week_days
    return self.week.frame.naik_days(*self.WEEK_RANGE, window, min_naik_hours)
TypeError: HourlyFrame.naik_days() takes 4 positional arguments but 5 were given
FAIL: test_the_minimum_has_no_default_so_no_caller_can_leave_it_out (tests.test_mode_backtest_frames.NaikDaysTests)
AssertionError: TypeError not raised

Ran 50 tests in 1.358s
FAILED (failures=1, errors=41)
```

That RED is about the signature. To show that the new tests also catch the reported bug itself, I accepted the parameter and ignored it in a scratch copy (what the review described): 12 failures with the numbers wrong, for example `AssertionError: frozenset({datetime.date(2026, 7, 1)}) != set()` for a day with one NAIK hour at a minimum of 2, and `ValueError not raised` for 0 and -1.

GREEN, the same command after the change: `Ran 50 tests in 1.257s` / OK (37 frames tests, 13 Pine structure tests).

Verification (these numbers supersede the Verification numbers above):

- Focused: 50 tests / OK on Python 3.10.20 (`.venv`). Line and branch coverage of `frames.py`: 100 % (73 statements, 12 branches, 0 missed).
- Mutation check (scratch, not committed): 56 bugs, the 46 of the first round plus 10 for the minimum, all caught: the minimum ignored (the reported bug), `>` for `>=`, needing one more hour, comparing it with the number of days or with the hours of the whole range, counting hours outside the window toward it (while requiring one inside), the check removed, 1 refused, 0 accepted, a default of 1.
- Full suite, exact command `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`: `Ran 1769 tests in 153.990s` / OK (1,761 + 8 new), exit 0. The time is the machine's, which had a load average of 18 from other applications; the earlier quiet run took 37 s.
- `compileall -q src tests` OK; `git diff --check` clean; `tests.test_secret_hygiene` OK with this document tracked; `ruff check --select F,E,W,B --line-length 100` clean on the two changed Python files.

## Task 6 — Step 0

`src/research/mode_backtest/step0.py`, `report.py` (the step-0 table), `cli.py` and `__main__.py` start here; the bot never imports them. Step 0 is the manager's first request: the Pine Phase 1 holdout of 16 October, split by side and by the new selector's condition, with no new test. The Pine test stage must never run again (its run marker forbids it), so step 0 never calls it and never touches a marker. `replay_main_runs(data_dir, pine_dir)` rebuilds the stage's inputs with public names of `research.pine_gate`, assembled as the stage's `_context` assembled them (`holdout_context`): `load_dataset(data_dir, None)`, `holdout_markets`, `HoldoutPeriod.spec()`, `load_calibration_file`, seed 0, `days`, `last20_days`, `purge_tick_minutes(calib, exec_cfg)` and the spec `ExecConfig()`, `GuardConfig()`, `GateConfig()` (`SPEC_CONFIGS`, the same objects `check_inputs` compares). It then runs `h.run(c.signal)` for every contender of `build_contenders`. `run_step0` compares `symbol_metrics` of the replay with the holdout file, and only when every value matches does it label each trade (`pilih_mode` at its signal time, on `HourlyFrame`s from `load_pair` over 2025-08-01 to 2026-10-13), split the trades (`split_rows`) and write `step0_<YYYYMMDD>.json` and `.md`.

Choices the brief left open:

- **`check_inputs` checks more than the brief's three inputs.** Besides the selection file's sha256, `configs.exec/guard/gate` against `asdict` of the defaults and `data_sha256`, it checks the seed (0), the period (`HoldoutPeriod.spec().to_dict()`) and the calibration file: s*, stop count and latency count against the holdout file's `calibration` record, and the latency sample against the selection's `latencies_sha256`, the chain the test stage's `check_calibration` used. All are inputs of the `Holdout`; without these checks a wrong calibration file would surface only after the replay (minutes on the real data), as a list of metric differences. Every problem is listed in one `ReproductionError`. The check runs inside `replay_main_runs`, after `load_dataset` (it needs `data_sha256`) and before any run.
- **Values are compared as JSON text** (`json.dumps(sort_keys=True)`): floats are compared bit for bit (JSON keeps every bit, through `repr`), an int never equals a float (`2 != 2.0`), `null` never equals `0`, and NaN never equals a stored number. Every key `symbol_metrics` returns is compared; the stored file's other keys (robustness, source, verdict, ...) are not. A key, symbol or contender on one side only is a difference, and so is comparing nothing at all (`[]` from two empty sides never passes). Lines read `c1 BTC-USDT wr: 48.1 != 48.2` (replay first).
- **`ReproductionError(message, problems)`** keeps every difference; the CLI prints each on stdout and then the one `error:` line on stderr, so the operator sees all of them while stderr keeps exactly one line.
- **The label days are derived, not typed:** `LABEL_FIRST_DAY = wib_day(ms(TRAIN_START))`, `LABEL_LAST_DAY = wib_day(ms(HOLDOUT_END) - 1)`, the day files the Pine load read; a test pins them to 2025-08-01 and 2026-10-13. `condition_lookup(pairs, selector)` refuses a time outside a pair's 1m bars (`[first open, last open + 1 min)`), because past the data the last closed candle is stale and the label would be wrong without a word; it also refuses an unknown symbol, a pair without bars and a pair given twice. `label_lookup(data_dir, selector)` loads both pairs over the label days and calls it.
- **`split_rows` keeps only groups with trades**, ordered by contender (the mapping's order), symbol (BTC first), side (LONG first) and condition (NAIK, TURUN, SIDEWAYS, TIDAK_ENTRY). Menang is Pine's `stats.win_rate` (100 × TP / (TP + SL), `None` when TP + SL = 0), gross is `stats.mean_gross_bps`, so step 0 reads the trades as the 16 October report did. The notional is keyword-only, defaults to the spec `ExecConfig().notional_usdt` (no literal 25,000), and `run_step0` passes the replay's `h.exec_cfg.notional_usdt`. An unknown symbol, side or condition and a notional that is not positive and finite raise `ValueError` naming the value.
- **`run_step0` refuses before the replay whenever it can:** a naive clock, an existing output (either file), a missing Pine file (`FileNotFoundError` naming every missing one), a config that does not load, a holdout file that is not a JSON object. A failed second write removes the first file, as Pine's `write_report` does, so a run leaves both files or neither; the JSON is rendered (`fileio.json_text`, NaN refused) before anything is written. After the reproduction check the `Holdout` is dropped before the labels load, so the Pine frames and the label pairs are not in memory together.
- **Provenance in the JSON:** sha256 of the three Pine files, `data_sha256` (the one `check_inputs` accepted), the config's sha256, the current `lock.code_sha256()` beside the holdout file's `code_sha256`, the Python version, the period, the label days, `asdict(exec_cfg)`, the reproduction summary (contenders, symbols, the 15 compared fields, 0 differences) and the rows.
- **The Markdown** is Indonesian with the decimal comma and a dot between thousands (ruling 4: `48,1%`, `-3,25`, `-1.450`; a value that rounds to zero has no sign; missing is `–`). It names the holdout file, the period and the seed, and carries the spec §8 caveat with the numbers taken from `exec_cfg` (TP 0,20% dan SL 0,20%, 60 menit, tick clock with AI latency, the Pine guards, not the new modes' exits). `format_number` and `format_percent` are public in `report.py` for Task 13.
- **CLI:** every option is required (no default directories), `~` is expanded, exit codes 0 / 1 (one `error:` line, `cli_common.one_line`) / 2 (argparse), `MODE_BACKTEST_DEBUG=1` prints the traceback first, an unexpected exception is not turned into an error line. `verify-copy` prints every problem and fails with `error: N problems: ...`, or prints `0 problems: every file matches the VPS listing`.

Test design: the real data is outside the repository, so the replay and step 0 run on a synthetic Pine data directory (23 random-walk days at the end of the holdout, both symbols, 1,000 1h and 500 4h warm-up bars, funding every 8 h), loaded once with `start_day`. The tests patch `step0.load_dataset` to return it and `step0.load_pair` to read its days, and both patches record what they were asked for, so the tests also check `load_dataset(data_dir, None)` and the label range 2025-08-01 to 2026-10-13. The reference trades are simulated directly with `simulate_holdout` and explicit spec settings and seed 0, not through the code under test: 742 trades (c1 258, c2 330, c3 154), on both sides and in all four conditions, and some days reach the 1M volume guard, so the guard config matters. The replay must equal them trade for trade, and `run_step0` must pass the reproduction check against a holdout document built from them. The brief's six tests are there (`test_identical_metrics_have_no_mismatch`, `test_one_changed_field_is_reported_and_stops_step0` with `replay_main_runs` patched and no file in `out_dir`, `test_split_rows_groups_by_side_and_condition`, `test_check_inputs_refuses_a_different_selection_file`, `test_cli_unknown_command_exits_2`, `test_cli_missing_pine_file_exits_1_with_one_error_line`), plus a perturbation of each of the 15 `symbol_metrics` fields one at a time, the type and NaN cases, the one-sided contender and symbol cases, each input check, the replay's `Holdout` fields, the refusals before the replay, WIB file dating (18:30 UTC is the next WIB day, 16:59 UTC is not), the rollback, the label guard, the table formatting and the CLI paths, including one `python -m research.mode_backtest` subprocess.

RED, before the four modules existed, `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_step0 tests.test_mode_backtest_cli -v` (interpreter path shortened):

```text
test_mode_backtest_step0 (unittest.loader._FailedTest) ... ERROR
test_mode_backtest_cli (unittest.loader._FailedTest) ... ERROR

======================================================================
ERROR: test_mode_backtest_step0 (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_mode_backtest_step0
Traceback (most recent call last):
  File "<py>/unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File "<repo>/tests/test_mode_backtest_step0.py", line 28, in <module>
    from research.mode_backtest import report, step0
ImportError: cannot import name 'report' from 'research.mode_backtest' (<repo>/src/research/mode_backtest/__init__.py)


======================================================================
ERROR: test_mode_backtest_cli (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_mode_backtest_cli
Traceback (most recent call last):
  File "<py>/unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File "<repo>/tests/test_mode_backtest_cli.py", line 14, in <module>
    from research.mode_backtest import cli, step0
ImportError: cannot import name 'cli' from 'research.mode_backtest' (<repo>/src/research/mode_backtest/__init__.py)


----------------------------------------------------------------------
Ran 2 tests in 0.000s

FAILED (errors=2)
```

This is the expected failure (the brief's step 2: an import failure). The first GREEN run had 3 errors, all in the test: `ConditionLookupTests` kept the lookup function as a class attribute, which Python bound as a method (`kondisi_at() takes 2 positional arguments but 3 were given`); wrapped in `staticmethod`.

GREEN, the same command after the mutation-check additions: `Ran 74 tests in 5.737s` / OK (59 step-0 tests, 15 CLI tests).

Verification:

- Focused: `tests.test_mode_backtest_step0 tests.test_mode_backtest_cli tests.test_mode_backtest_structure` `Ran 86 tests` / OK on Python 3.10.20 (`.venv`); the structure tests (no private import, no network or venue word, writes only through `fileio`) cover the four new modules. Python 3.11 was not run (the compiled `pydantic_core` of `.venv` does not load there).
- Line and branch coverage (scratch rcfile): `step0.py` 100 % (243 statements, 76 branches), `report.py` 100 % (31, 6), `cli.py` 100 % (65, 8). `__main__.py` runs only in the subprocess test, which coverage does not follow.
- Mutation check (scratch copy of the tree, not committed): 51 bugs planted one at a time, all caught. Examples: seed 1, purge one minute short, `last20_days` as `days`, ETH before BTC, guards off, `load_dataset` cut at the holdout end, the input check after the runs, c3 replayed on its primary rule, each input check removed, `==` instead of the JSON form, the last metrics key skipped, a contender only in the file ignored, "nothing compared" passing, the reproduction stop removed, labels at the entry time, one leg per trade, time exits counted as losses, LONG and SHORT or the conditions reordered, the label days moved by one, the label guard removed or made inclusive, files dated in UTC, no rollback, no early output refusal, a naive clock accepted, a hard-coded notional, the Pine files checked after the config, each `split_rows` guard removed, the selector's slow SMA at 40, differences not printed by the CLI, `ReproductionError` not mapped to exit 1, `verify-copy` passing with problems, usage errors as 1, no decimal comma, a signed zero, Menang with two decimals, a hard-coded bracket. The first pass had two survivors, both test gaps: the conditions' order was never checked within one side (SIDEWAYS moved first went unnoticed), and an unknown condition still raised `ValueError` from `tuple.index` without the explicit guard (the same held for the symbol guard). A test now orders all four conditions in one group, and the guard tests match the message naming the value.
- Full suite, exact command `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`: `Ran 1843 tests in 42.389s` / OK (1,769 + 74 new).
- `compileall -q src tests` OK; `git diff --check` clean (new files marked intent-to-add); `tests.test_secret_hygiene` OK with this document tracked; `ruff check --select F,E,W,B --line-length 100` clean on the six new Python files.

Not verified here: the real replay. It needs the copied VPS data and Pine files and runs in Task 7. A note for that run: the holdout file's `code_sha256` (`e915262…`, in the 16 October report) is the `lock.code_sha256` of commit `7d013bf` (2026-10-16 04:49 WIB), checked by hashing each commit's tree the way `lock.code_files` does. Since then the fingerprinted files changed only outside the replay path: `bars.py` (the day validation now shared with check-data; the same `Bars` for valid days), `features.py` (the check-data agreement helper; `FeatureFrame.build` untouched), `cli_data.py` (check-data) and `pine_indicators.py` (new `sma`, `stdev`, `rolling_median`). So the current code is expected to reproduce. The current `code_sha256` differs anyway, by design: it covers every file under `src/research`, `mode_backtest` included.

## Task 8 — Mode A tracker

`bot.strategy.modes.pullback` holds Mode A of the manager's module (spec §2.3) as a pure state machine over closed trigger candles: the frozen `Candle(close_ms, open, high, low, close)`, the frozen `PullbackSignal(close_ms)` and `PullbackTracker(confirm_within, max_distance_atr)`. `on_candle(candle, ema, sma20_1h, atr14_1h)` keeps the indices of the open touches in a small list. A touch is `low <= ema` (inclusive), covers candles `i … i + confirm_within − 1`, and leaves the list when the current index passes the end of its window. A candle that meets an open touch with `close > open`, `close > ema` (strict) and `close <= sma20_1h + max_distance_atr * atr14_1h` (inclusive) returns `PullbackSignal(close_ms)`. `consume()` empties the list: touches that ran out left when the signal candle arrived and none can be newer, so each touch still there holds the signal candle, which is D5's "every touch whose window contains the signal candle". The module imports `math`, `dataclasses` and `typing` only. No module imports it yet (checked with grep), and the guards that scan every source file under `src` (no bot module imports `research`, none imports `pine_indicators`) pass with it in place. Mode A is LONG-only; the mirrored D2 comes in a later phase.

D4 and D5 as implemented: every candle is compared with the `ema` passed with it, as a touch and as a confirmation; touches are recorded whatever the condition, and the caller checks NAIK at the signal; a touch ends only in `consume()` or when its window runs out. A signal the caller does not consume, and a candle that is no signal (red, not above the EMA, too high), leave the touches alone.

Choices the brief left open:

- **Exceptions.** `ValueError` for a candle whose `close_ms` is not above the previous one (equal or older). A rejected candle changes nothing: it takes no window slot, it does not move the clock back, and a pending signal stays consumable. `RuntimeError` for `consume()` when the latest candle holds no unconsumed signal: before any candle, after a candle without a signal, for the signal of an earlier candle, and a second time after a consume. A refused `consume()` leaves the touches alone. `RuntimeError` is what Python itself raises for a call made in the wrong state (releasing an unlocked lock, starting a thread twice); `ValueError` is for an argument that cannot be accepted.
- **Constructor checks beyond the brief.** `confirm_within` must be an `int` of at least 1 and `max_distance_atr` a finite number above 0; a `bool`, a string, NaN, infinity and a whole number beyond the float range are refused with `ValueError`. Without the checks a window of 0 or a NaN cap builds a tracker that never signals and says nothing. They are the config's rules (`_count`, `_positive`), so a tracker built from `config/modes.yaml` cannot trip them.
- **A too-high close is no entry and leaves the touch.** D5 says a touch is used up only by an entry, so a green close above the EMA that is more than the cap above the SMA does not end the window: a later candle inside it, within the cap, may still signal. The manager's text does not say this in so many words; it is the literal reading of D5, and the one behaviour of this task worth confirming. A test pins it.
- **A window counts candles received.** A candle with `ema=None` (warm-up) still advances it, so the caller must send every closed candle, which the module docstring says.
- **Missing and NaN inputs.** As in the brief: no `ema`, neither a touch nor a confirmation; no `sma20_1h` or `atr14_1h`, no signal, but the touch is still recorded. A NaN fails closed the same way, since every comparison with it is false. A test pins this, because writing a comparison as its negation would turn a NaN into a touch. The producer should still keep NaN out, as for Task 3.
- `Candle.high` is carried and unused by the rule. The spec's sketch names the return type `Signal`; the brief's `PullbackSignal` is used.

Test design: every candle is a named shape against an EMA of 100, an SMA (1h) of 100 and an ATR (1h) of 2 (`touch_green`, `touch_red`, `red_above`, `green_above`, `doji_above`), so a test reads as a sequence and the cap is 102. Boundaries use exact floats: a low of exactly 100.0 against one `math.nextafter` above it, a close of exactly 100.0, a close at the cap against the next float above it. A candle that stays off the EMA closes above it (its low is at most its close), so a candle that neither touches nor confirms is a red one. The brief's eight tests are there under their names, and 23 more pin:

- D4: a rising and a falling EMA between the touch and the confirmation, with both closes under the cap so that the EMA alone decides.
- D5: a too-high close leaves the touch; an entry consumes every touch whose window holds the signal candle, including an older touch whose window runs past it; a blocked touch expires on its own and a later touch enters separately.
- The window and the cap as parameters: 1, 2, 3 and 5 candles; four SMA, ATR and multiple combinations.
- Missing and NaN inputs (also on a later candle of a window, and with a cap far above any close so that "missing means zero" would signal), and a candle without an EMA still counting in the window.
- Each misuse error, a rejected candle changing nothing, the first candle at `close_ms` 0, the constructor refusals, and the frozen values.
- A differential test against a reference written differently: a stateless scan back over the window for a touch after the last consumed signal. It runs 1,500 random candles in regimes below, around and well above the EMA, with about one candle in five missing an input, for windows of 1, 2, 3 and 5 and caps of 0.5 and 1.0, with the caller taking half of the signals at random. Each run holds 43 to 91 taken and 45 to 93 blocked signals, and 12 to 29 of them (windows of 2 and up) rest on the touch of an earlier candle; the test fails unless it sees more than 30 of each, so it cannot pass by looking at nothing.

Two flaws in my own tests were found by reading them, before the first run: the rising-EMA candle's close was also above the cap, so the EMA did not decide, and a test of the candle count after `consume()` could not fail for any observable bug (indices are relative once the list is empty); it was removed.

RED, before the module existed, `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_pullback -v` (paths shortened). This is the expected failure of the brief's step 2, an import failure:

```text
test_modes_pullback (unittest.loader._FailedTest) ... ERROR

======================================================================
ERROR: test_modes_pullback (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_modes_pullback
Traceback (most recent call last):
  File "<py>/unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File "<repo>/tests/test_modes_pullback.py", line 12, in <module>
    from bot.strategy.modes.pullback import Candle, PullbackSignal, PullbackTracker
ModuleNotFoundError: No module named 'bot.strategy.modes.pullback'


----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (errors=1)
exit code: 1
```

GREEN, the same command after `pullback.py` was written, first run (the brief's eight tests shown, in the order unittest prints them; the other 23 also `ok`):

```text
test_blocked_signal_leaves_the_touch_available (tests.test_modes_pullback.RuleTests) ... ok
test_confirmation_on_the_third_candle_but_not_the_fourth (tests.test_modes_pullback.RuleTests) ... ok
test_distance_cap_is_inclusive (tests.test_modes_pullback.RuleTests) ... ok
test_low_equal_to_ema_is_a_touch_and_close_equal_to_ema_is_not_a_confirmation (tests.test_modes_pullback.RuleTests) ... ok
test_missing_inputs_never_signal (tests.test_modes_pullback.RuleTests) ... ok
test_one_entry_per_touch (tests.test_modes_pullback.RuleTests) ... ok
test_red_candle_never_confirms (tests.test_modes_pullback.RuleTests) ... ok
test_touch_and_confirmation_in_one_candle (tests.test_modes_pullback.RuleTests) ... ok
...
Ran 31 tests in 0.035s

OK
exit code: 0
```

A first-try pass is a reason to doubt the tests, so three more things followed, each closing a gap:

- **A missing input read as zero.** A scratch mutant reading a missing `sma20_1h` or `atr14_1h` as 0 survived all the explicit tests when the missing-inputs test used only the reference values: a zero there makes the cap 2 or 100, below every close, which looks like "too high". The randomized test catches the ATR one but cannot see the SMA one (its SMA is about 100). The lenient pass (SMA and ATR of 1,000) now catches both.
- **A rejected older candle moving the clock back.** A mutant that stored `close_ms` before validating survived (`Ran 31 tests` / OK): a candle between the rejected one and the last accepted one would then be taken. The test now refuses candles 3 and 4 after 5 was accepted, and the mutant fails with `FAILED (failures=1)`.
- **An overflow found by review, then RED.** `math.isfinite(10**400)` raised `OverflowError` out of the constructor instead of the documented `ValueError`. The test gained `10**400` and failed first (the label of the failing case, a 400-digit number, is shortened here):

```text
ERROR: test_the_cap_must_be_a_finite_number_above_zero (tests.test_modes_pullback.MisuseTests) (max_distance_atr=10000000...)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "<repo>/tests/test_modes_pullback.py", line 319, in test_the_cap_must_be_a_finite_number_above_zero
    PullbackTracker(confirm_within=3, max_distance_atr=bad)
  File "<repo>/src/bot/strategy/modes/pullback.py", line 77, in __init__
    if not _is_number(max_distance_atr) or not math.isfinite(max_distance_atr):
OverflowError: int too large to convert to float

----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (errors=1)
```

  The check is now one helper that treats a whole number beyond the float range as invalid.

Mutation check (scratch copies of `pullback.py` outside the repository, one planted bug each, every one run against the 31 tests): 64 bugs, all caught, and all 64 are also caught by the 30 explicit tests without the randomized one.

- Candle order (7): `<` for `<=`, the check removed, a first candle at `close_ms` 0 refused, the pending signal cleared before validating, a rejected candle taking a window slot, a rejected candle moving the clock back, the wrong exception type.
- Window (5): one short, one long, a hard-coded 3, never expiring, a candle without an EMA not counted.
- Touch (8): `<` for `<=`, reversed, on the close, on the high, only with all inputs present, never recorded, not recorded while another is open, a NaN low counting.
- Confirmation (8): no open touch needed, a doji counting, a close equal to the EMA counting, no green check, no above-EMA check, red candles confirming, the previous candle's EMA used, the open judged instead of the close.
- Cap (10): `<`, the multiple ignored, the ATR ignored, the SMA ignored, measured from the EMA, a hard-coded 1.0, removed, reversed, a NaN passing, below the SMA. Missing inputs (3): SMA read as zero, ATR read as zero, a missing input passing the cap.
- Signal (5): the wrong time, never flagged for `consume()`, consumed by itself, a too-high close consuming the touch, the flag surviving the next candle.
- `consume()` (8): keeping the touches, keeping the signal candle's own touch, dropping only the newest, dropping only the oldest, callable twice, silent without a signal, clearing before it refuses, the wrong exception type.
- Constructor (10): a window of 0, a bool window, the window check removed, a cap of 0, a negative cap, an infinite cap, a bool cap, a string cap, the overflow leaking, the cap check removed.

Verification:

- Focused: `Ran 31 tests` / OK on Python 3.10.20 (`.venv`), and also on 3.11.15 and 3.12.13 (the module and its tests use the standard library only). Line and branch coverage of `bot/strategy/modes/pullback.py`: 100 % (60 statements, 18 branches, 0 missed), measured with a scratch rcfile because coverage cannot read `pyproject.toml` on 3.10 without `tomli`.
- Full suite, exact command `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`: `Ran 1874 tests in 42.031s` / OK (1,843 + 31 new).
- `compileall -q src tests` OK; `git diff --check` clean (new files marked intent-to-add); `tests.test_secret_hygiene` OK with this document tracked; `ruff check --select F,E,W,B --line-length 100` and `ruff format --check --line-length 100` clean on the two new Python files.

## Task 9 — Trigger series and the Mode D trigger

Two pieces. `bot.strategy.modes.near_mean` is the pure rule: `stretch_from_window(highs, lows, closes, ema_period, atr_period)` returns `build_setup_profile({"last_price": closes[-1], "ema_21": ema(closes, ema_period), "atr_14": atr(highs, lows, closes, atr_period)}, "LONG")["stretch_atr"]` with the live `bot.strategy.indicators`, which is the number the live stretch gate decides on (rounded to four decimals, as the profile rounds it), and `near_mean_short(stretch_atr, max_abs)` is `stretch_atr is not None and abs(stretch_atr) <= max_abs`. `research.mode_backtest.frames` gains the series the event loop of Task 12 walks:

- `TriggerSeries(bars, ema)` and `trigger_series(bars_1m, tf, ema_period)`: the 15m or 5m candles (`aggregate`, UTC-epoch buckets, a bucket with a missing minute dropped) and `pine_indicators.ema` of their closes (mean-seeded, `None` before 8 x period candles, so the first 159 candles at a period of 20: 40 hours at 15m, 13 h 20 min at 5m).
- `stretch_series(bars_1m, window, ema_period, atr_period) -> (Bars, Column)`: the 3m candles and, for each, `stretch_from_window` over the `window` candles ending with it; `None` for the first `window - 1`. The loop has the shape of the Pine research's stretch column, and a test holds the two equal on every candle of the series. Its first value needs 39 3m bars (1 h 57 min).
- `PairFrames(symbol, bars_1m, hourly, triggers, stretch_bars, stretch)` and `build_pair_frames(pair, cfg)`: the hourly frame from `cfg.pemilih_mode`, both triggers with `cfg.mode_a.ema_pemicu`, the stretch with `cfg.mode_d.candle_jendela`, `ema_periode` and `atr_periode`.

D2 for these series: the value at index `i` is computed through the close of candle `i`, so it is known at `bars.open_ms[i] + bars.interval_ms` and not before (D4 for the trigger EMA: a candle is compared with the EMA that includes its own close). The docstrings say so, because Task 12 reads them at exactly that time.

Choices the brief left open:

- **`Column`, not `Series`.** The brief types the numbers as `Series`. The module already has `Column = Tuple[Optional[float], ...]`, which `HourlyFrame` stores, so the new fields use it: tuples, not lists. `PairFrames.triggers` is a `MappingProxyType`, as `ModesConfig.pair` is; both classes are frozen.
- **What is refused.** `trigger_series` takes `"15m"` and `"5m"` only (the config's two values) and 1m bars only; `stretch_series` takes 1m bars. Without the 1m check, 3m bars aggregate to 15m without a word (the same check as `FeatureFrame.build`). `stretch_series` also refuses a window the live maths cannot measure: the live `ema` returns the last close when it has fewer closes than its period (a stretch of 0.0, which is "near the mean" on every candle, so Mode D would short everywhere), and the live `atr` returns 0 with fewer than `period + 1` closes (no stretch at all). So a window under `max(ema_period, atr_period + 1)`, or a period under 1, raises `ValueError`. `stretch_from_window` itself keeps the live behaviour (a test pins both quirks) and refuses only bars that do not match (none, or different lengths: a high list one longer than the closes would be measured on misaligned bars without a word). The standard configuration, 39 / 21 / 14, passes with room.
- **The side `"LONG"` is a constant that cannot be tested away.** `build_setup_profile` reads the side only for its `setup_key`; the stretch is the same for `LONG`, `SHORT`, an empty side and `None` (checked), so a mutant that passes `"SHORT"` is equivalent. The constant is named and commented, as the Pine research does.
- **An unmeasured stretch is no signal.** `near_mean_short(None, ...)` is False, as is NaN or infinity (a comparison with NaN is False). The live gate lets an entry with an unreadable setup through; a trigger needs a measured value (D3: a missing input blocks the signal), and the Pine research's `contender2_signal` does the same.
- **No warm-up for the trigger candles.** They are aggregated from the 1m bars only (the brief), so the first 160 candles of the data have no EMA and Mode A cannot signal on them; Task 12 must still feed those candles to the tracker (Task 8's note).
- **Both timeframes are always built** (the 5m series costs 0.4 s per pair), so a run can pick 15m or the pre-registered 5m switch without rebuilding.
- `bot/strategy/modes/__init__.py`: its docstring says the rules "take numbers, never bar arrays"; one sentence now says `near_mean` takes one short window of highs, lows and closes and measures it with the live indicators.

Test design: the price is `wavy`, a sum of sines that moves every minute (written through the Task 4 helpers to day files and loaded with `load_pair`, three WIB days), so the high, low and close of a 3m, 5m or 15m candle all differ and the stretch crosses zero and the +-0.75 band (on the fixture: 1,402 measured values, 488 inside the band and 914 outside, 675 positive and 727 negative). Expected candle closes are computed in the test from the price function (`candle_closes`), never from the frame. The brief's four tests are there under their names, and 29 more pin:

- the stretch against an oracle written in the test from the definitions (the EMA as a weighted sum of the closes, the ATR as the mean of the last 14 true ranges), within the four decimals of the profile, besides the literal equality with the live call sequence;
- the Pine cross-check at the brief's five times (38 bars closed: none; the 39th just closed; inside the 40th bar; a bar in the middle; the end of the data), and then on every one of the 1,440 candles at the time it closes;
- look-ahead, in the way the hourly tests do it: the 1m bars from `t` on are scaled by 50, and no value up to the candle that closed at `t` may change, while the next one must; the candle's own last minute scaled must change its value (for the trigger EMA at candle 200 of the 15m series and for the stretch at candle 300 of the 3m series);
- the EMA at candle `i` against the EMA of the closes `0..i` and not `0..i-1` or `0..i+1` (the price moves every candle, so a shifted series differs), at the first valid candle (159), the next, a middle one and the last;
- partial candles dropped at either end (1m bars from 00:07 to 02:40 WIB give nine 15m candles), the UTC grid of the open times, 15m and 5m, the EMA period, the window and both periods as arguments, a flat market (all `None`), every refusal above, and the frozen and read-only types and their field order;
- `build_pair_frames` against the direct calls with the values of `config/modes.yaml`, then with every period changed (selector SMA, trigger EMA, window, both stretch periods) so that none can be a constant, and with a symbol that is not the fixture's.

RED, 1: the brief's step 2, `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_near_mean tests.test_mode_backtest_frames -v` (paths shortened). The expected import failure, in both modules:

```text
test_modes_near_mean (unittest.loader._FailedTest) ... ERROR
test_mode_backtest_frames (unittest.loader._FailedTest) ... ERROR

======================================================================
ERROR: test_modes_near_mean (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_modes_near_mean
Traceback (most recent call last):
  File "<py>/unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File "<repo>/tests/test_modes_near_mean.py", line 15, in <module>
    from bot.strategy.modes.near_mean import near_mean_short, stretch_from_window
ModuleNotFoundError: No module named 'bot.strategy.modes.near_mean'


======================================================================
ERROR: test_mode_backtest_frames (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_mode_backtest_frames
Traceback (most recent call last):
  File "<py>/unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File "<repo>/tests/test_mode_backtest_frames.py", line 22, in <module>
    from bot.strategy.modes.near_mean import stretch_from_window
ModuleNotFoundError: No module named 'bot.strategy.modes.near_mean'


----------------------------------------------------------------------
Ran 2 tests in 0.000s

FAILED (errors=2)
```

GREEN for the first module, `near_mean.py` written: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_near_mean -v` `Ran 12 tests in 0.001s` / OK.

RED, 2: with `near_mean.py` in place and `frames.py` unchanged, `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_frames` (excerpt). The 37 existing tests pass; the new ones fail on the missing names, which is the failure that shows they test the new code:

```text
.....................................EEEEEEEEEEEEEEE
======================================================================
ERROR: setUpClass (tests.test_mode_backtest_frames.PairFramesTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "<repo>/tests/test_mode_backtest_frames.py", line 940, in setUpClass
    cls.built = frames.build_pair_frames(cls.pair, cls.cfg)
AttributeError: module 'research.mode_backtest.frames' has no attribute 'build_pair_frames'

======================================================================
ERROR: test_trigger_ema_is_aligned_to_closed_candles (tests.test_mode_backtest_frames.TriggerSeriesTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "<repo>/tests/test_mode_backtest_frames.py", line 733, in test_trigger_ema_is_aligned_to_closed_candles
    ts = frames.trigger_series(self.pair.bars_1m, "15m", TRIGGER_EMA)
AttributeError: module 'research.mode_backtest.frames' has no attribute 'trigger_series'
...
----------------------------------------------------------------------
Ran 52 tests in 0.661s

FAILED (errors=29)
```

GREEN, the brief's command after the `frames.py` additions: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_near_mean tests.test_mode_backtest_frames -v` `Ran 69 tests in 1.011s` / OK (12 near-mean tests and 57 frames tests, the brief's four among them). Everything passed at once, so the mutation check came next and found a gap (below); the test added for it makes it 70 tests.

Verification:

- Focused: `tests.test_modes_near_mean tests.test_mode_backtest_frames` `Ran 70 tests` / OK on Python 3.10.20 (`.venv`), and with the structure tests (`tests.test_mode_backtest_structure`, `tests.test_pine_gate_structure`) `Ran 95 tests` / OK on 3.11.15, the Python of CI. The neighbouring modules (`test_mode_backtest_structure`, `test_pine_gate_structure`, `test_mode_backtest_data`, `test_modes_pullback`, `test_modes_regime`, `test_modes_config`) pass: 188 tests.
- Line and branch coverage (scratch rcfile): `near_mean.py` 100 % (13 statements, 2 branches), `frames.py` 100 % (120 statements, 24 branches), 0 missed.
- Mutation check (scratch copy of the tree, not committed): 66 bugs planted one at a time, run against the two test modules and the structure tests. First pass 65 caught, 1 survivor: `build_pair_frames` returning a hard-coded `'BTC-USDT'` as the symbol, because the fixture's symbol is `BTC-USDT`; `test_the_symbol_is_the_pairs_own` now builds the frames of an `ETH-USDT` pair, and the mutant fails (66 of 66). By area: `near_mean_short` (7: `<` for `<=`, no `abs` on either side, the `None` check gone or replaced by truthiness, the band hard-coded, NaN passing), `stretch_from_window` (10: the previous close as the price, each period hard-coded, the ATR fed lows and highs swapped, the EMA of the highs, the EMA key fed the last close, each length check and the empty check removed), `trigger_series` (14: 5m dropped from the timeframes, 3m accepted, either check removed, always 15m, the EMA period hard-coded, the EMA one candle late or early, of the highs or the opens, from the first candle, after 4 x period, the dataclass fields swapped, not frozen), `stretch_series` (15: the 1m or the measurable check removed, 5m candles, the first value one bar late or early, a 38- or 40-bar window, the window one bar early or one bar ahead, only the close looking ahead, the periods swapped or hard-coded, the window hard-coded, a list for a tuple), `_require_measurable` (6, each bound off by one), `build_pair_frames` and `PairFrames` (14: each config value hard-coded, Mode D2's EMA period, the selector default, a writable mapping, only 15m, the 1m bars copied, the bars and the stretch swapped, not frozen, the fields reordered).
- Realistic scale (scratch, 441 synthetic days, 635,040 1m bars, a random walk): `trigger_series` 0.13 s at 15m (42,336 candles) and 0.39 s at 5m (127,008), `stretch_series` 4 to 6 s (211,680 candles), `build_pair_frames` 4.4 s in all, peak RSS 200 MB. The stretch loop is the cost, one `stretch_from_window` per candle; the Pine research pays the same. Two pairs are about 10 s, once per run.
- Full suite, exact command `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`: `Ran 1907 tests in 43.157s` / OK (1,874 + 33 new), exit 0.
- `compileall -q src tests` OK; `git diff --check` clean; `tests.test_secret_hygiene` OK with this document tracked; `ruff check --select F,E,W,B --line-length 100` and `ruff format --check --line-length 100` clean on the five changed Python files.

Notes for later tasks:

- **Task 12** reads `triggers[tf]` (Mode A) and `stretch_bars` with `stretch` (Mode D) for the candles whose close, `open_ms[i] + interval_ms`, falls in the stage, takes `ema[i]` and `stretch[i]` at that close, and feeds every candle to the tracker, those whose EMA is still `None` included.
- **Task 14**: the code fingerprint of the mode backtest must follow `frames.py` into `bot/strategy/modes/near_mean.py` and from there into `bot/strategy/indicators.py` and `bot/arena/setup_profile.py` (the Pine fingerprint already holds the last two). `tests/test_pine_gate_structure.py` does not scan `near_mean.py`: it walks `research/pine_gate` only.
- `tests/test_mode_backtest_frames.py` is now 1,026 lines (680 before); the brief puts the new tests there. If it keeps growing, the three Task 9 classes (`TriggerSeriesTests`, `StretchSeriesTests`, `PairFramesTests`, with `WavyCase` and `wavy`) move to a module of their own.

## Task 10 — Position rules

`bot.strategy.modes.position_rules` holds the price arithmetic and the entry gates that every mode shares, as small functions of numbers:

- `LONG = 1`, `SHORT = -1`, `Bracket(tp, sl)` and `bracket_prices(entry_fill, side, tp_pct, sl_pct)`: `tp = entry x (1 + side x tp/100)` and `sl = entry x (1 - side x sl/100)`, set from the entry fill (D6).
- `adverse_fill(price, side, slippage_pct, closing)`: `price x (1 + against x s/100)`, with `against = side` for an opening fill and `-side` for a closing one. A LONG pays up on the way in and gives up on the way out; a SHORT the other way round.
- `entry_window_open(window, t_ms)`: `bot.core.entry_window.window_is_open` on the WIB datetime of `t_ms`; `None` is always open.
- `pause_over(t_ms, last_exit_ms, pause_min)`: `last_exit_ms is None or t_ms >= last_exit_ms + pause_min x 60,000`.
- `under_daily_caps(pair_today, total_today, max_pair, max_total)`: `pair_today < max_pair and total_today < max_total`, the counts being the entries already opened that WIB day.
- `volatility_ok(atr14_1h, atr_median, mult)`: False when either is `None`, else `atr14_1h <= mult x atr_median`.

`research.mode_backtest.frames` no longer converts to WIB by itself (the controller's ruling carried from Task 5): `HourlyFrame.naik_days` asks `entry_window_open(window, close_ms)`, and `_wib_moment`, `_EPOCH` and the imports only they needed (`window_is_open`, `datetime`, `timedelta`, `timezone`, `WIB`) are gone, so one WIB conversion remains. `tests/test_mode_backtest_frames.py` is unchanged (58 tests, green before and after). This is a behaviour-preserving refactor, so those tests are its safety net and it has no RED of its own; the mutation check below shows they would notice a broken delegation.

Choices the brief left open, or changed:

- **A fixed UTC+7, not `ZoneInfo`.** The brief says to convert with `datetime.fromtimestamp(t_ms / 1000, ZoneInfo("Asia/Jakarta"))`. The controller ruled for a fixed `timezone(timedelta(hours=7))`: Asia/Jakarta has been UTC+7 since 1964, the config pins `zona_waktu` to it, the research side (`wib_day`, and `_wib_moment` here) already read WIB as a fixed offset, and it needs no tzdata. The conversion is exact integer arithmetic (`epoch + timedelta(milliseconds=t_ms)`, as `_wib_moment` did) and not the float of `t_ms / 1000`. The module keeps its own `_WIB` because the rule core never imports `research` (`config.py` keeps a private one too).
- **`side` is the integer `LONG` (+1) or `SHORT` (-1)**, which is what the brief's formulas multiply by (and Task 11's `side x qty x (exit - entry)`). Anything else raises `ValueError`, a bool included.
- **What raises and what answers False.** The two price functions cannot say "no", so a `side` that is not 1 or -1, a `closing` that is not a bool, and a price or percent that is not a finite number above 0 (a slippage of 0 is allowed; `None`, text and an int too large for a float count as not finite) raise `ValueError`: each would otherwise put a level on the wrong side of the entry, or a fill in the position's favour, without a word. The gates refuse instead. `volatility_ok` is False for `None` and for NaN, because it is written as `atr <= mult x median` and a comparison with NaN is False (the negative form `not atr > limit` lets NaN through; a test holds that). `HourlyContext.atr_median` is `None` until 720 hourly values exist, so the design's "also skip while fewer than 720 values exist" needs no code of its own.
- **The caps count entries already opened.** `under_daily_caps(5, 9, 6, 10)` is True (the 6th entry on the pair and the 10th in total are allowed); `(6, 9, 6, 10)` and `(5, 10, 6, 10)` are False.
- **The pause is closed on the left.** An entry exactly 15 minutes after the exit is allowed, one millisecond earlier is not, and a signal at the exit itself or before it is inside the pause (with a pause of 0 minutes the exit instant is over). A pair that has not exited yet is never paused.
- **The window is read to the minute**, as the live `window_is_open` does, with its end excluded: Monday 06:59:59.999 is closed, 07:00:00.000 is open, Friday 23:59:59.999 is open and Saturday 00:00:00.000 is closed.
- **Plain functions, not an `EntryGate` class.** The design's module table sketches `bracket(...)` and `EntryGate.allows(...)`; the plan's Task 10 fixes the plain functions above, and Task 12's event loop composes the gates in the order of section 9.1.
- `bot/strategy/modes/__init__.py`: one sentence about `position_rules` in the package docstring.

Test design: 50 tests in ten classes, the brief's six under their names. Every expected number is written in the test from the rules of the document and never computed with the code under test; a time is built from a WIB wall-clock reading by integer arithmetic on the epoch. The others pin:

- the bracket against numbers worked out by hand (LONG from 100 at 0.40 / 0.25 gives 100.4 and 99.75; SHORT at 0.20 / 0.20 gives 99.8 and 100.2), the two percents not swapped (SHORT at 0.40 / 0.25 gives 99.6 and 100.25), the levels as a ratio of the fill at three prices, and the SHORT as the mirror of the LONG around the entry;
- the fills against a real price (63,250.5, whose basis point is 6.32505), the direction (no fill ever moves in favour of the position, for either side and either order), zero slippage as the identity, the slippage as a percent, and `closing` given by position;
- the weekly window against an oracle that uses no `datetime`: the minute of the week taken from the epoch alone (1970-01-01 was a Thursday). Every minute of two weeks is compared, the second week running across New Year, at the first and the last millisecond of each minute (40,320 checks, 6,780 open minutes a week). The four edges are held to the millisecond. WIB and not UTC is shown through UTC instants (Monday 00:00 UTC is open, Sunday 23:59 UTC is closed, Friday 17:00 UTC is Saturday 00:00 WIB and closed), and a second window shows the argument is the one applied;
- the pause at the exact boundary, a millisecond before it, at and before the exit, with no exit, in minutes (a 30-minute pause), and with a pause of 0;
- the caps as a table, each cap against its own count (7 of 6 on the pair with 3 in total is refused, 3 and 7 is allowed), other caps, and the arguments by name;
- the volatility guard at the limit (2.0 against a median of 1.0 passes), at the next float above it (`math.nextafter`, which fails) and below it, with `None`, NaN and infinity, and with another multiplier;
- the refusals above, and the module boundary: an AST scan of `position_rules.py` finds no import that reaches `research` or `pine_indicators` and finds `bot.core.entry_window.window_is_open`; the same detector is run on a synthetic source with every form of import, so the scan cannot pass by looking at nothing.

RED: the brief's step 2, `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_position_rules -v` (paths shortened). The expected import failure:

```text
test_modes_position_rules (unittest.loader._FailedTest) ... ERROR

======================================================================
ERROR: test_modes_position_rules (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_modes_position_rules
Traceback (most recent call last):
  File "<py>/unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File "<repo>/tests/test_modes_position_rules.py", line 19, in <module>
    from bot.strategy.modes import position_rules
ImportError: cannot import name 'position_rules' from 'bot.strategy.modes' (<repo>/src/bot/strategy/modes/__init__.py)


----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (errors=1)
```

GREEN, first run with `position_rules.py` written: 49 of 50 pass. The one failure was in my own test helper, not in the module: the import detector built `..pine_indicators` for `from . import pine_indicators`, and its synthetic-source test, written for exactly that, caught it (`AssertionError: Lists differ: ['..pine_indicators', ...] != ['.pine_indicators', ...]`). The helper joins the prefix correctly now, and the second run is the result: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_position_rules -v` `Ran 50 tests in 0.039s` / OK.

Then the refactor of `frames.py`: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_frames` `Ran 58 tests` / OK, with the file unchanged.

RED, 2: the self-review of the module found a gap between its docstring and its code. A price, percent or slippage that is `None`, text or an int too large for a float reached `math.isfinite` and raised `TypeError` or `OverflowError`, where the module promises `ValueError` (and the neighbouring `PullbackTracker` makes a non-number a `ValueError` too). The four refusal tests were extended with `None`, a string and `10**400` (their subtest headers use a short label, since `10**400` has 401 digits), and `PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_position_rules` gave `Ran 50 tests` / `FAILED (errors=12)`, three kinds of error four times each:

```text
TypeError: must be real number, not NoneType
TypeError: must be real number, not str
OverflowError: int too large to convert to float
```

GREEN, 2: `_is_finite` catches both errors and the two validators use it. `Ran 50 tests` / OK.

Verification:

- Focused: `tests.test_modes_position_rules tests.test_mode_backtest_frames` `Ran 108 tests` / OK, and with `tests.test_mode_backtest_structure`, `tests.test_pine_gate_structure` and `tests.test_entry_window` `Ran 145 tests` / OK, on Python 3.10.20 (`.venv`) and on 3.11.15, the Python of CI (3.11 from a local environment that has the dependencies, because the venv's compiled `pydantic_core` is a 3.10 build).
- Line and branch coverage (scratch rcfile): `position_rules.py` 100 % (55 statements, 10 branches, 0 missed) and `frames.py` 100 % (117 statements, 24 branches, 0 missed). The whole repository on the project's settings (line coverage, `omit` and `fail_under = 80` copied into a scratch rcfile, because the 3.10 `coverage` cannot read `pyproject.toml`): 90 %, 0 below the gate.
- Mutation check (scratch copy of `src`, `tests` and `config`, not committed): 77 bugs planted one at a time, 71 in `position_rules.py` against the new tests and 6 in the `naik_days` delegation of `frames.py` against the unchanged frames tests. All 77 are caught on the first pass, each by failing assertions and none by a broken import (7 to 26 failing tests for the frames ones). By area: `bracket_prices` and `Bracket` (15: either sign flipped, the two percents swapped, a percent not divided or divided by 1,000, an additive level, the side missing from a level, each check removed, the fields swapped, not frozen), `adverse_fill` (11: the closing flag inverted, or ignored in either direction, the percent not divided or divided by 10,000, the sign flipped, an additive fill, each check removed), `_is_finite` (4: a non-number or an oversized int not caught, an error counted as finite, infinity counted as finite), the validators and `LONG` / `SHORT` (10: zero accepted as a price or percent, zero refused as a slippage, infinity or a negative slippage accepted, a bool or a zero accepted as a side, SHORT refused, the constants changed), the zone and the window (9: UTC, +8, +6, -7, the window ignored or negated, the end of the minute, a millisecond early, the epoch a day late), `pause_over` (7), `under_daily_caps` (6), `volatility_ok` (9: the limit exclusive, the multiplier fixed or added, reversed or swapped operands, NaN passing, a missing value passing, only one of the two checked, both needed) and the `naik_days` call (6: the result inverted, the close minus a millisecond, the opening time instead of the close, the window ignored, the gate skipped, always closed).
- Full suite, exact command `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`: `Ran 1957 tests in 57.197s` / OK (1,907 + 50 new), exit 0.
- `compileall -q src tests` OK; `git diff --check` clean; `ruff check --select F,E,W,B --line-length 100` and `ruff format --check --line-length 100` clean on the four changed Python files; `tests.test_secret_hygiene` OK with this document tracked.

Notes for later tasks:

- **Task 11** calls `adverse_fill(price, side, slippage_pct, closing)` with `side` as `LONG` / `SHORT` and `closing` a real bool, and `bracket_prices` takes the *fill*, so the entry is `adverse_fill(open, side, s, False)` first and the bracket after it (D6).
- **Task 12** composes the gates in the order of section 9.1: `entry_window_open(window, t_ms)`, the segment edge, the condition, the open position, `pause_over(t_ms, last_exit_ms[pair], jeda_menit)`, `under_daily_caps(pair_entries, total_entries, maks_pair, maks_total)` with the counts of the WIB day of `t` (`wib_day`), then `volatility_ok(ctx.atr14_1h, ctx.atr_median, mult)`.
- **Task 14**: the code fingerprint of the mode backtest must also follow `frames.py` into `bot/strategy/modes/position_rules.py` and from there into `bot/core/entry_window.py`.

## Task 11 — Exit walk and trade money

`research.mode_backtest.simulate` (the exit part; Task 12 adds the event loop to the same file) says how a simulated position ends and what it earns. It takes numbers and bars only and reads no clock, file or setting:

- `ExitOutcome(reason, exit_idx, raw_price, gapped, both_touched)` (frozen), with the three reasons as `EXIT_TP`, `EXIT_SL` and `EXIT_WAKTU` (`"TP"`, `"SL"`, `"WAKTU"`, the words of the log), and `resolve_exit(bars_1m, entry_idx, side, tp, sl, limit_min)`. The walk starts on the entry bar itself, whose whole range counts because the entry fills at its open, and goes bar by bar. Order inside a bar: the time limit, an open at or beyond the SL, an open at or beyond the TP, an SL touch, a TP touch (LONG: `open <= sl`, `open >= tp`, `low <= sl`, `high >= tp`; SHORT mirrored). A gap exits at the bar's open (`gapped`), a touch at the level, and the time exit (D7) at the open of the bar `limit_min` minutes after the entry bar, before that bar's levels. A bar that touches both levels without opening beyond either is taken as the SL (`both_touched`). It returns `None` when the data ends first, and a bar that is not exactly one minute after the one before it (a hole, a repeated minute, a minute that goes back) is where the data ends.
- `Money(entry_fill, exit_fill, qty, fee_usdt, slippage_usdt, pnl_usdt, volume_usdt)` (frozen) and `settle(entry_open, raw_exit, side, notional, fee_pct, slippage_pct)`: both fills come from `adverse_fill` (the entry with `closing=False`, the exit with `closing=True`), `qty = notional / entry_fill`, `volume = qty x entry_fill + qty x exit_fill`, `fee = fee_pct / 100 x volume`, `pnl = side x qty x (exit_fill - entry_fill) - fee`, and `slippage_usdt = qty x |entry_fill - entry_open| + qty x |raw_exit - exit_fill|`.

Choices the brief left open, or changed:

- **`both_touched` is the stop-first tie and nothing else.** It is True only for an SL taken at the SL price from a bar whose range also reached the TP. An exit at the open (a gap, or the time exit) is False even when the same bar's range goes on to reach the other level: the open comes first, so the order inside the minute is known and no tie was broken. This is the convention of the Pine outcome walker (`research.pine_gate.outcomes`). The flag is a diagnostic: it is not among the 23 log columns and no metric reads it.
- **A level behind the entry open is a gap on the entry bar.** The walk applies the same rules to the entry bar as to any bar and is given levels, not percents, so it does not compare them with the entry open. The document's percents never put a level behind the open; a slippage larger than the stop distance would, and the answer is then the gap rule of D6 (exit at that bar's open). A test pins it.
- **The arguments are checked, and a wrong one raises `ValueError`.** Bars that are not 1m; a `side` that is not the int `LONG` or `SHORT` (a bool or `1.0` included, which the `adverse_fill` check lets through for `1.0`); an `entry_idx` that is not an int inside the bars (a negative one would index the arrays from the end); a `limit_min` that is not an int of at least 1; a level that is not a finite number above 0; levels on the wrong sides of each other (a LONG needs `sl < tp`, a SHORT `tp < sl`, equal levels have no side); a `notional` that is not above 0; a `fee_pct` below 0. A bool is no number here. Prices and the slippage are left to `adverse_fill`. Each of these would otherwise give a plausible exit or profit without a word.
- **A touch is only known to the minute.** `exit_idx` is the bar; the outcome carries no time. Task 12 decides what `exit_ms` is (see the notes).
- **The brief's take-profit figures sit on a tie.** The exact pnl is 79.9558785 and the exact fee 17.5341215, which the brief rounds up to 79.955879 and 17.534122. A float lands on either side of such a rounding, so `places=6` would pass or fail with the order of the arithmetic. The brief's figures are compared with a tolerance of one unit of the 6th decimal (`delta=1e-6`) and the exact values are pinned beside them at nine decimals. The exact values come from decimal arithmetic at 50 digits, not from the code under test.
- **The SHORT reference trades are not in the brief.** Mode D's SHORT (0.20 % each way from the fill 99.99) was worked out the same way: take-profit pnl 30.02162675, stop-loss pnl -70.02337675.
- **`slippage_usdt` is written with absolute values**, so it is positive by construction and never `-0.0` (with a slippage of 0 a SHORT would otherwise write `-0.0` into the trade log).
- **`INTERVAL_1M` is imported from `research.pine_gate.features`**, as `frames.py` does.

Test design: 77 tests in 13 classes, the brief's seven under their names (the TP half of the gap test is a test of its own, `test_bar_opening_beyond_tp_exits_at_its_open`). The bars are flat at 100.0 except the few that decide the outcome, and every SHORT case is written out by hand and not derived from the LONG one, so a helper cannot repeat a mistake of the code. The tests pin:

- the touches (TP, SL, both; the first bar to touch decides; `both_touched` belongs to the deciding bar), inclusive at the level to the last bit (`math.nextafter` one float inside and one outside, for all four comparisons), and the two levels not swapped between the sides;
- the gaps: at the stop, at the target, exactly at a level (a gap) and one float inside it (a touch), a gap beating a touch in the same bar (a bar that opens beyond the SL and then reaches the TP is a gap and not a tie), and the exit price being the open;
- the time exit: at the open of bar 90, before that bar's levels and before its gaps; bar 89 still counting; nothing after bar 90 looked at; the minutes counted from the entry bar (an entry at 10 exits at 100); a limit of 60 and of 1;
- the entry bar's range counting (TP, SL, both) and the bars before it never looked at;
- the end of the data: 90 bars is `None` and 91 is a time exit, an entry on the last bar, a level touched before the end still deciding, and a late touch past the limit not a reason for `None`;
- holes: a missing minute, a level after the hole not taken and one before it taken, a hole where the time exit bar should be (and one bar earlier) not a time exit, a hole before the entry bar outside the walk, runs that join without a hole walking as one, and a repeated or backward minute ending the walk;
- 4,000 seeded random walks on a tick grid (levels hit exactly, gaps, holes, eight limits and five level distances) compared with a plain per-side restatement of the rules written in the test, and a check that they reach every kind of outcome on both sides (33 to 782 times each; 1,060 walks hold a hole and 2,975 touch a level exactly), so the comparison cannot pass by looking at nothing;
- the money: the brief's two reference trades, their SHORT mirrors, a round trip that closes where it opened (9 bps), no fee and no slippage, the fills equal to `adverse_fill`, the quantity over the fill and not the open, and 144 generated trades (both sides, six price pairs, three slippages, two fees, two notionals) against an oracle written in `Fraction`s;
- the arguments (each refusal, with the levels of a LONG and of a SHORT for the side, so that the order of the levels cannot be what refuses it), and the composition the event loop will use: fill, bracket from the fill (D6), walk, settle, which gives the reference trades back.

RED: the brief's step 2, `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_simulate -v` before the module existed (paths shortened). The expected import failure:

```text
test_mode_backtest_simulate (unittest.loader._FailedTest) ... ERROR

======================================================================
ERROR: test_mode_backtest_simulate (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_mode_backtest_simulate
Traceback (most recent call last):
  File "<py>/unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File "<repo>/tests/test_mode_backtest_simulate.py", line 21, in <module>
    from research.mode_backtest.simulate import (
ModuleNotFoundError: No module named 'research.mode_backtest.simulate'


----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (errors=1)
```

GREEN, first run with `simulate.py` written: 72 of 73 pass. The one failure was the tolerance of my own oracle test, not the code: it asked for `abs_tol=1e-12`, but a pnl is made of the difference of two float fills (each good to about 1e-14) times a quantity of a few hundred, so a float cannot be closer than a few 1e-12 USDT to the exact value:

```text
FAIL: test_money_matches_exact_arithmetic_on_a_grid_of_trades (tests.test_mode_backtest_simulate.MoneyOracleTests) (args=(100.0, 99.75, -1, 25000.0, 0.035, 0.1), field='pnl_usdt')
AssertionError: False is not true : -4.92052364864373 != -4.920523648648653
```

The oracle now allows `abs_tol=1e-9` (a billionth of a USDT; a wrong formula is off by far more) with the reason written beside it, and `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_simulate -v` gave `Ran 73 tests` / OK.

RED, 2: the mutation check below found two survivors, and each was a gap in the tests. (1) A hole check that only looks for a forward gap (`>` for `!=`) passed: the test for a repeated or backward minute changed a single timestamp, and the bar after it then looked like a forward gap, which the mutant also catches. The test now moves every bar from bar 5 on back by whole minutes, so the only wrong step is the one into bar 5. (2) A side check that also accepts 0 passed: `side=0` was tried with the levels of a LONG only, and the level-order check refuses a 0 with those anyway. The test now tries each bad side with the levels of a LONG and of a SHORT. Against the final tests the two mutants fail (paths shortened):

```text
mutant: if i > entry_idx and open_ms[i] > open_ms[i - 1] + INTERVAL_1M:
FAIL: test_bars_that_repeat_or_go_back_in_time_end_the_walk (...HoleTests) (case='repeat')
AssertionError: ExitOutcome(reason='WAKTU', exit_idx=90, raw_price=100.0, gapped=False, both_touched=False) is not None
FAIL: test_bars_that_repeat_or_go_back_in_time_end_the_walk (...HoleTests) (case='back')
AssertionError: ExitOutcome(reason='WAKTU', exit_idx=90, raw_price=100.0, gapped=False, both_touched=False) is not None
Ran 77 tests / FAILED (failures=2)

mutant: side not in (LONG, SHORT, 0)
FAIL: test_a_side_that_is_not_long_or_short_is_refused (...WalkArgumentTests) (side=0, tp=99.6)
AssertionError: ValueError not raised
Ran 77 tests / FAILED (failures=1)
```

GREEN, 2: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_simulate` `Ran 77 tests` / OK (73 + 1 for the repeated and backward minutes + 3 for the random walks).

Verification:

- Focused: `tests.test_mode_backtest_simulate` `Ran 77 tests` / OK on Python 3.10.20 (`.venv`), and with `tests.test_mode_backtest_structure` `Ran 89 tests` / OK on 3.11.15, the Python of CI (the 3.11 from the local environment that has the dependencies, because the venv's compiled `pydantic_core` is a 3.10 build; the `features` import pulls `pydantic` in, as it does for `frames.py`).
- Line and branch coverage of `simulate.py` (scratch rcfile, because the 3.10 `coverage` cannot read `pyproject.toml`): 100 % (94 statements, 30 branches, 0 missed).
- Mutation check (scratch copy of `src` and `tests`, not committed): 122 bugs planted one at a time in `simulate.py`, run against the tests. The first pass caught 118 of the 120 that applied and 2 survived (RED, 2 above); two more patterns (the quantity over the open and over the exit fill) matched the docstring as well as the code and were rewritten to match the code only. After the two test fixes all 122 are caught, every one by failing assertions and none by a broken module. By area: the rules of a bar (12: each comparison made strict, the sides swapped or fixed to the LONG rule, the extremes swapped or fixed, the open read from the highs), holes and the end of the data (8: the check off, forward only, backward only, a wrong step, applied on the entry bar, from the start of the data, a hole or the end answered with a time exit), the loop and the limit (9: the limit bar one early or late, `limit_idx` one off or taken from the start of the data, the walk starting after the entry bar, the last bar skipped, the walk running past the data), what each exit returns (37: price, index, `gapped` and `both_touched` of the five kinds of exit, each gap check off, the touches swapped, a touch tested on the wrong extreme, the labels, and the time check, the two gap checks and the touches in six arrangements), the argument checks (22), and `settle` and the two records (34: the side, notional and fee checks off, the closing flags swapped, the quantity over the open or the exit fill, four wrong volumes, five wrong fees, six wrong pnls, six wrong slippages, the argument order and the field order of both records, neither record frozen).
- Full suite, exact command `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`: `Ran 2034 tests` / OK (1,957 + 77 new).
- `compileall -q src tests` OK; `git diff --check` clean; `ruff check --select F,E,W,B --line-length 100` and `ruff format --check --line-length 100` clean on the two new Python files; `tests.test_secret_hygiene` OK with this document tracked.

Notes for later tasks:

- **Task 12** composes the pieces in this order (D6): `entry_fill = adverse_fill(open, side, s, False)` first, then `bracket_prices(entry_fill, side, tp_pct, sl_pct)`, then `resolve_exit(pair.bars_1m, entry_idx, side, b.tp, b.sl, limit)` with `entry_idx` the 1m bar whose `open_ms == t`, then `settle(bars.open[entry_idx], outcome.raw_price, side, notional, fee_pct, s)` with the three numbers from `cfg.umum` (`notional_usdt`, `fee_taker_persen`, `slippage_persen`) and the limit from `cfg.mode_a.batas_waktu_menit` or `cfg.mode_d.batas_waktu_menit`. `None` means the data ended, or a minute is missing, before the position resolved. A `ValueError` from either function is a bug in the caller.
- **The time exit needs the bar that opens `limit_min` minutes after the entry bar.** A stage ends at 00:00 WIB and its last day file stops at 23:59 WIB, so the bar at the end instant is not in the data. D10 still allows the entry exactly `limit` minutes before the end (a 15m close at 22:30 WIB for Mode A, 60 minutes for Mode D), and for that one entry `resolve_exit` returns `None`: the time-exit bar is missing. Task 12's loop must count such a signal (it is not a gate-blocked one) and Task 14 must either read one more minute past the stage end or accept the loss of that entry; it is not an error of the walk.
- **`exit_ms` is Task 12's to decide and to pin with a test.** For a time exit and for a gap the exit is at the open of `exit_idx`, `open_ms[exit_idx]`. A touch happens somewhere inside the minute; the Pine simulation used the bar's close (`open_ms + 60,000`) for it, and that is the later, more cautious choice for the 15-minute pause. Do not copy the Pine rule `open_ms + (0 if gapped else 60,000)` as it stands: it would put a time exit, which is at the open, one minute late. It also decides the "average position length" column.
- **Task 13**: `Money.slippage_usdt` and `Money.fee_usdt` are positive, `pnl_usdt` is after the fee, and `volume_usdt` is the entry plus the exit notional, which is what `Hasil per 1 juta` divides by. `ExitOutcome.reason` is already the log word (`TP`, `SL`, `WAKTU`).
- **Task 14**: `simulate.py` imports `bot.strategy.modes.position_rules` (and through it `bot.core.entry_window`), `research.pine_gate.bars` and `research.pine_gate.features`, all of them already in the code fingerprint's path through `frames.py`.
- `tests/test_mode_backtest_simulate.py` is 1,025 lines; the plan puts Task 12's tests in the same file. The random-walk helpers (`restated_exit`, `random_rows`, `bars_with_holes`, `RandomWalkTests`) and the `Money` oracle are the part that would move to a module of their own if it keeps growing.

## Task 12 — The simulation loop for Mode A and Mode D

`research.mode_backtest.simulate` (the loop part, beside Task 11's walk and money) turns closed candles into simulated positions:

- `RunSpec(mode, pair, tf, kondisi_gate, start_ms, end_ms)` (frozen, validated in `__post_init__`), `MODE_A = "A"`, `MODE_D = "D"`.
- `Trade` (frozen), the 22 fields of the brief in its order: every §2.9 log column except `trade_id` (Task 13's row number) can be read from it, the four indicator columns through `context`.
- `RunResult(trades, blocked)`: `trades` a tuple in time order, `blocked` a read-only mapping with every key of `BLOCK_REASONS = ("window", "segment_edge", "kondisi", "position_open", "pause", "daily_cap", "volatility")` in that order, zeros included.
- `simulate_run(frames, cfg, run)`. The clock is the close `t` of each trigger candle with `start_ms <= t < end_ms` (`frames.triggers[run.tf]` for A, `frames.stretch_bars` for D). Mode A sends every one of those candles to a fresh `PullbackTracker` with `ema[i]` and the SMA20/ATR14 of `frames.hourly.context_at(t)`; Mode D signals on `near_mean_short(stretch[i], maks_jarak_ke_ema_kali_atr)`. A signal meets the seven gates in that order and the first that fails is counted. A signal that passes enters on the 1m bar with `open_ms == t` (`entry_index_for` plus an exact check), then fill, bracket from the fill, `resolve_exit`, `settle`, record, and `consume()` for Mode A only.

Controller rulings for this task (recorded as given):

1. **Segment edge (D10):** blocked when `t + limit_min x 60,000 >= end_ms`; the time-exit bar opening exactly at the end belongs to the next segment. For A (limit 90) a signal at `end - 90 min` is blocked and the previous 15m candle (`end - 105 min`) enters. This also settles Task 11's note about the entry exactly `limit` before the end: it never reaches the walk.
2. **A trade is never dropped:** `resolve_exit` returning `None` after every gate passed raises `RuntimeError` naming the signal time (WIB ISO time and ms); so does a signal with no 1m bar opening exactly at `t`.
3. **Exit time:** `open_ms[exit_idx]` for a time exit and a gap, `open_ms[exit_idx] + 60,000` for a touched TP or SL. The position is open while `t < exit_ms`, and the pause runs from `exit_ms`.
4. **Tests in a new module** `tests/test_mode_backtest_loop.py`; `tests/test_mode_backtest_simulate.py` is unchanged.
5. **`faktor_k`:** TP and SL percents are multiplied by `cfg.pair[run.pair].faktor_k` (1.0 for BTC, recorded on the trade); `otomatis` (`None`, ETH) raises `ValueError`.

Choices of mine, each pinned by a test:

- **`RunSpec` takes only the runs of Phase 1:** Mode A on `15m`/`5m` in NAIK; Mode D on `3m` in TURUN, or in NAIK (the comparator). Anything else (`mode` "B", Mode A in TURUN, Mode D in SIDEWAYS, Mode D on 15m, float or bool times, an end before the start) raises `ValueError`. A start equal to the end is an empty run.
- **One position per pair is structural**, so `umum.maks_posisi_per_pair != 1` raises `ValueError` instead of being ignored. The frames must be of the run's pair, the pair must be in the config, and the 1m bars, the trigger candles and their EMA or stretch column must have the run's interval and matching lengths (`ValueError` naming which).
- **A missing trigger candle between two candles of the run raises `RuntimeError`**: the Mode A window counts the candles it is sent, so a hole would widen it silently. `load_pair` already refuses incomplete days, so this only protects against misuse.
- **Each run starts clean**: the tracker sees only the candles of its own range (a touch closing before the start never reaches it), and no pause or daily count carries over from another segment. A test-stage signal at 00:00 WIB is therefore not paused by a training exit at 23:59, and a touch from the last training candle cannot confirm in the test; only the first minutes of a stage see a different state than a run that carried it over would (the pause lasts 15 minutes, a touch three candles), though a different first entry can shift the entries after it.
- **The condition is read only through `context_at(t)`**, at every candle close for Mode A (the tracker's cap needs SMA20 and ATR14) and at signal closes for Mode D. The stub in the tests has `context_at` and nothing else and records each time asked.
- **Daily caps:** a run trades one pair, so its count of entries on the WIB day of `t` is passed as both the pair count and the total; the smaller cap binds.

Test design: 49 tests in 14 classes (`tests/test_mode_backtest_loop.py`), the brief's nine under their names (`test_segment_edge_blocks_a_signal_whose_time_limit_passes_the_end` with ruling 1's values). The trigger candles are written by hand against an EMA of 100 (quiet, touch, confirm, touch-and-confirm, too high, or a candle with its own EMA or none), Mode D's stretch is written per 3m candle, and the 1m bars are a separate flat series at 100.0 with the few bars a test edits. Expected trades are built with the committed `adverse_fill`, `bracket_prices` and `settle` from numbers the test picks, so the tests check what the loop passes to them. Every number comes from `config/modes.yaml` (pinned by a fixture test), and variants come from `dataclasses.replace`. The tests pin:

- the entry: at the open of the 1m bar opening at the confirmation close (the bars a minute before and after carry other prices), the whole `Trade` equal field by field, each candle judged against its own EMA, candles without an EMA still counting toward the 3-candle window, the cap read from the context at that close, and `tf` choosing the 15m or 5m series;
- D5: a touch blocked by an open position and then by the pause still enters inside its window, an entry uses the touch up, a surviving touch still lapses after three candles, and a kondisi block leaves the touch;
- each gate on its own: weekend and Monday-before-07:00 signals blocked (Friday 23:45 enters and is held past the window), daily caps rolling at 00:00 WIB and not 00:00 UTC in both directions (Wednesday 00:00 WIB enters although its UTC day already holds six; Wednesday 07:15 WIB is capped although its UTC day holds none), an entry counting for the WIB day it opens on, the smaller cap binding, the volatility guard (above twice the median, exactly twice, no median yet, both modes), the segment edge for A on 15m and 5m and for D, exact to the minute (an end at 00:01), and no trade exiting at or after the end;
- the exit time of each kind of exit (time, touched TP, touched SL, a touch in the entry minute, a gap through each level) and the pause measured from it (a touch blocks 15 minutes from the end of its minute, a gap from its open, a time exit's own minute counts as `pause` and not `position_open`);
- the counting: one Mode D scenario with 23 signals over Friday evening, the weekend and Monday morning, each entered or blocked under the first of the gates it fails (signals also fail later gates: a weekend signal in TURUN with a hot ATR is `window`, an open position in a hot TURUN hour is `kondisi`, a paused signal with the cap reached is `pause`), with every count and every trade (entry, exit, reason) written out, plus a run where the window is checked before the segment edge;
- the run range: a touch before the start is never seen, a candle closing at the end is outside, the condition is asked at every close inside the run and never outside it, an empty run, and two runs over the same frames giving the same result;
- `faktor_k` scaling both levels for both modes (a bar that reaches the unscaled stop and not the scaled one does not exit) and `otomatis` refused;
- every number read from the config (fee, slippage, notional, both brackets, both limits, the pause, the volatility multiple, the weekly window, the confirmation window, the cap multiple, the Mode D band);
- the data defects (a missing minute inside the walk, data ending before the exit, a hole at the entry minute, data ending before or starting after it, bars off the minute grid, a missing trigger candle), each a `RuntimeError` naming the time;
- the argument refusals: 19 bad `RunSpec`s, and 9 frame or config mismatches, each of these matched on its message so that the intended check is the one refusing.

RED: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_loop -v` before the loop existed (paths shortened). The brief expected the import error on `simulate_run`; the import list is alphabetical, so the first missing name is `BLOCK_REASONS`:

```text
test_mode_backtest_loop (unittest.loader._FailedTest) ... ERROR

======================================================================
ERROR: test_mode_backtest_loop (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_mode_backtest_loop
Traceback (most recent call last):
  File "<py>/unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File "<repo>/tests/test_mode_backtest_loop.py", line 31, in <module>
    from research.mode_backtest.simulate import (
ImportError: cannot import name 'BLOCK_REASONS' from 'research.mode_backtest.simulate' (<repo>/src/research/mode_backtest/simulate.py)


----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (errors=1)
```

GREEN, first run with the loop written: `Ran 47 tests` / OK. A first pass that passes at once proves little, so the tests were then checked against planted bugs (below). While listing the bugs I added `test_an_entry_counts_for_the_wib_day_it_opens_on` (nothing pinned that an entry counts for its opening day rather than its exit day); it passed against the loop, `Ran 48 tests` / OK.

RED, 2: the first mutation pass (104 bugs) left one non-equivalent survivor: a segment edge one minute longer (`(limit_min + 1) * 60,000`). With every end at a midnight and every close on the candle grid, `t + limit` never lands one minute before the end, so no test could tell. `test_the_edge_is_exact_to_the_minute` ends a run at 00:01, where the 22:30 signal's time exit opens at 00:00 inside the segment. Against the final tests the mutant fails:

```text
FAIL: test_the_edge_is_exact_to_the_minute (tests.test_mode_backtest_loop.SegmentEdgeTests)
AssertionError: Lists differ: [] != [(1782833400000, 1782838800000)]
Ran 49 tests in 0.057s
FAILED (failures=1)
```

GREEN, 2: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_loop` `Ran 49 tests` / OK.

Verification:

- Focused: `tests.test_mode_backtest_loop`, `tests.test_mode_backtest_simulate`, `tests.test_mode_backtest_structure`, `tests.test_mode_backtest_frames` and `tests.test_pine_gate_structure` `Ran 209 tests` / OK on Python 3.10.20 (`.venv`); the first three `Ran 138 tests` / OK on 3.11.15 (a scratch venv with `requirements.txt`, because CI runs 3.11).
- Line and branch coverage of `simulate.py` (scratch rcfile, because the 3.10 `coverage` cannot read `pyproject.toml`): 100 % (312 statements, 88 branches, 0 missed). Global line coverage with the `pyproject.toml` settings mirrored into a scratch rcfile: 90 % (gate 80 %).
- Mutation check (scratch copy of `src`, not committed): 107 bugs planted one at a time in the loop and run against `tests.test_mode_backtest_loop`. 106 are caught, 98 of them by failing assertions and 8 by a crash that is the natural result of the bug (a skipped trade returning `None` into the book, levels swapped into `resolve_exit`'s own check, a `KeyError` once the config-pair check is gone). The one survivor is equivalent: `Candle(t_ms - 1, ...)` changes only the tracker's ordering check and its `PullbackSignal.close_ms`, which the loop does not read. By area: the order of the gates (each adjacent pair swapped, 6), each gate removed (7), gate details (21: the edge strict, one minute short or long, a fixed 90; the condition fixed to NAIK or TURUN; the position still open at its exit time; the pause fixed or one minute longer; the caps from one field only (2), one off, by UTC day, counted on the UTC day or on the exit day, counted on blocked signals; the volatility multiple fixed or its arguments swapped; the window fixed or open; the last exit not kept), the exit time (4), the tracker and its feed (17: no consume, consume on a block, skipping candles without an EMA or while a position is open, either neighbour's EMA, the context a candle late or a minute early (Mode A), a minute early (Mode D), SMA and ATR swapped, a fixed cap or window, the candle's close time, its high and low or open and close swapped, the next candle's stretch, the band from the wrong mode), the range of candles (7: the candle at the start skipped, one before it taken, the one at the end taken, the last one dropped, the open reported as the close, the hole check off or skipping the hole), the entry (17: the next or the previous 1m bar, no exact-open check, a skipped trade on each defect (3), a bracket from the open, the percents swapped, the levels swapped into the walk, fee and slippage swapped, fee, slippage or notional fixed, the notional of the record fixed, the entry or signal time wrong, `gapped` wrong), the terms of a mode (8: `faktor_k` ignored, on TP only, recorded as 1, `otomatis` let through; Mode D as LONG, Mode A as SHORT; each mode with the other's numbers), the input checks (15: the seven of `simulate_run`, the five of `RunSpec`, and Mode A in TURUN, Mode D in SIDEWAYS or on 15m let through) and the result (5: zero keys dropped, a writable mapping, a list of trades, the WIB time or the ms missing from a message).
- Real data, training period only (read-only smoke run of the copy at `~/replay/mode_backtest/data`, 2025-08-01 to 2026-08-04 WIB, 369 days, 531,360 1m bars; no test day read, nothing written): Mode A on 15m and 5m and Mode D in NAIK and in TURUN over `[latihan_mulai, latihan_selesai)` all completed (frames 4.3 s, each run 0.1 to 0.3 s) with no `RuntimeError`, and structural invariants held on every run (no overlap and the pause kept, the daily cap kept, every exit before the segment end, every entry inside the window, the gate condition and the side, the volatility guard at each entry, the recorded context equal to `context_at` at the signal, the seven keys). No result is reported here.
- Full suite, exact command `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`: `Ran 2083 tests` / OK (2,034 + 49 new).
- `compileall -q src tests` OK; `git diff --check` clean; `uv pip check` reports all installed packages compatible (the venv has no `pip`); `ruff check --select F,E,W,B --line-length 100` and `ruff format --check --line-length 100` clean on the two files; `tests.test_secret_hygiene` OK with this document tracked; `tests.test_mode_backtest_structure` OK (no private import, no network or venue word, no file write in the package).

Notes for later tasks:

- **Task 13**: `Trade.side` is the int `LONG`/`SHORT` (map it to the words), `exit_reason` is already the log word, `signal_ms == entry_ms`, and the indicator columns are `trade.context.sma20_1h`, `.sma50_1h`, `.ema50_4h` and `.atr14_1h`. "Rata-rata lama posisi" is `exit_ms - entry_ms` with ruling 3's exit time. `RunResult.blocked` is a `MappingProxyType`: use `dict(result.blocked)` before writing JSON.
- **Task 14**: one `simulate_run` per stage and run, with `start_ms`/`end_ms` the stage's WIB midnights (`spec.ms` of the `uji` datetimes). The 1m data must reach the minute before the end (the loaded days do); the trigger series and the hourly frame need warm data at the start, so the test stage still loads from `latihan_mulai` (Task 9's note), while the loop itself never looks at a candle closing before `start_ms`. The code fingerprint must cover the modules `simulate.py` now imports: `bot.strategy.modes.pullback` and `bot.strategy.modes.near_mean` (both inside `bot/strategy/modes/*.py`), `bot.core.entry_window`, `research.pine_gate.spec`, `bars` and `features`, all of them also reached through `frames.py` except `pullback`. `mode_a.aktif`, `mode_d.aktif` and `pair.*.aktif` are not read by the loop; the stages decide what runs.


## Task 13 — Metrics, trade log and the Mode A table

`research.mode_backtest.metrics`, `research.mode_backtest.tradelog` and the Mode A table in `research.mode_backtest.report` turn the trades of a run into the numbers that decide the one-shot holdout verdict and into what the manager reads:

- `StageMetrics(posisi, entry_per_hari_aktif, menang_pct, hasil_per_1_juta, tp, sl, waktu, rata_lama_menit, hasil_tanpa_teratas, hari_naik)` (frozen, the ten fields of the brief in its order) and `stage_metrics(trades, hari_naik, drop_top)`.
- `pass_status(m, uji)` returning `"LOLOS"`, `"GAGAL"` or `"BELUM CUKUP SAMPEL"` (module constants `LOLOS`, `GAGAL`, `BELUM_CUKUP_SAMPEL`), and `wants_5m(m15, uji)`.
- `tradelog.COLUMNS` (the 23 names of spec 2.9, in order), `wib_iso(t_ms)`, `trade_log_text(trades)` and `write_trade_log(path, trades)` (through `write_exclusive(..., "trade log")`).
- `report.ReportRow(mode, pair, tahap, metrics)`, `report.MODE_HEADER`, `report.NO_TRADING` and `report.mode_table(rows)`.

Controller rulings for this task (recorded as given):

1. **Entry per hari aktif** is `positions / NAIK days`; with 0 NAIK days it is `None`, never a `ZeroDivisionError`.
2. **The 5m switch fails loudly:** `wants_5m` is True iff the 15m run's entries per NAIK day is below `uji.pindah_5m_jika_entry_per_hari_naik_kurang_dari` (3); if that figure is `None` (no NAIK day) it raises `ValueError`, because the pre-registered rule cannot be evaluated.
3. **Trade log:** the 23 columns exactly as spec 2.9, in order; `trade_id` is the 1-based row number inside each CSV; `sisi` is LONG/SHORT from `side`; `kondisi` is the trade's `kondisi`; `ema50_4h`, `sma20_1h`, `sma50_1h` and `atr14_1h` come from `trade.context`; times are ISO 8601 in WIB with `+07:00`; `alasan_exit` is TP/SL/WAKTU; numbers are written so they round-trip (`repr` of floats).
4. **Every Indonesian table uses the decimal comma:** the new table reuses `format_number` and `format_percent` of `report.py`.
5. **Pass rule:** fewer than `uji.lolos_minimal_posisi` (40) positions is `BELUM CUKUP SAMPEL` (not a failure); otherwise `LOLOS` iff Hasil per 1 juta `>` -150 and the result without the top `drop_top` (3) `>` -250, both strictly; else `GAGAL`.

Choices of mine, each pinned by a test:

- **Sums are `math.fsum`**, so the same set of trades gives the same bits whatever order it is listed in. The pass rule is strict and the stage code lists trades in time order, the trimmed set in rank order; a plain float sum could differ in the last bit between the two.
- **The trim ranks by `(-pnl_usdt, entry_ms)`**: the highest profit in USDT first (not per volume), ties by the earliest entry, whatever the input order. Dropping more positions than exist, or all of them, gives `None`; dropping none equals the whole result.
- **`stage_metrics` refuses what would give a quiet wrong verdict** (`ValueError` naming the field): an `exit_reason` that is not TP, SL or WAKTU (it would be left out of `tp + sl + waktu`), a `pnl_usdt` that is not finite (NaN fails every `>` and would read as GAGAL), a `volume_usdt` that is not a finite number above 0, and a `hari_naik` or `drop_top` that is not a whole number of at least 0 (a bool is not one). `StageMetrics` itself is a plain record with no checks, so a test or a later task can build one by hand.
- **`pass_status` raises `ValueError`** when there are enough positions but `hasil_per_1_juta` or `hasil_tanpa_teratas` is `None` or not finite, instead of failing the mode. With too few positions it never looks at the results (a stage with no trades is `BELUM CUKUP SAMPEL`, not an error). `StageMetrics` has no `drop_top` field (the brief's field list is exact), so `pass_status` trusts that `hasil_tanpa_teratas` was made with `uji.lolos_buang_posisi_teratas`; the module docstring says so and Task 14 must pass that number.
- **The log is machine text:** a decimal point, no thousands separator, `repr` of the float, `\n` line ends, rows in the trades' order. A missing indicator (`None` in the context) is an empty cell and not a lost trade. `wib_iso` accepts only an `int` (not a bool or a float) that is a whole second, and raises `ValueError` instead of cutting the time. A `side` that is not `LONG` or `SHORT` (a bool is neither) raises. The text is built before the file is touched, so a trade that cannot be written leaves no file.
- **The table groups by tahap and pair without reordering:** the "Tidak trading" row follows the last row of each (tahap, pair) and shows Posisi 0, Entry per hari aktif 0,00 (or "–" when the stage has no NAIK day, as every row would), Menang "–", Hasil per 1 juta 0, "0 / 0 / 0" and "–" for the hold. Its NAIK days are those of the row it follows. Columns: Posisi and Hasil per 1 juta are whole numbers, Entry per hari aktif has two decimals, Menang one, the exit counts use the thousands dot and the hold is "45,5 menit". The Pair column shows `BTC-USDT` as the config names it (the step-0 table's "Koin" shows `BTC`). `mode_table` reads its rows once, so a generator works.
- **Test helpers:** `tests/mode_backtest_trades.py` (a `Trade` and context builder and the readers of the two layouts in spec 2.9) is a new module, because `tests/mode_backtest_helpers.py` is imported by the data tests and would otherwise pull `simulate` into them. The table has its own test module, `tests/test_mode_backtest_report.py`, next to the brief's two.

Test design: 125 tests in three modules, every expected number worked out by hand.

- `tests/test_mode_backtest_metrics.py` (65): the config numbers; `test_hasil_per_1_juta` (10 and -40 on 50,000 each is -300), pooled rather than a mean of ratios, an exact `Fraction` oracle, order independence over 60 random trades and 20 shuffles; Menang without time exits (3 TP, 1 SL, 6 time exits is 75), `None` without TP or SL, 0 for stop-losses only; exit counts; entry per day including 0 NAIK days; the hold from the entry and not the signal; the trim (ties by time with three tied at 20 and different volumes, ranking by profit and not by ratio, `drop_top` 0 to 4, nothing left, a brute-force oracle that finds the one dropped set by the rule itself); empty input, any iterable, no mutation, JSON round trip; the argument refusals; `test_statuses` as 12 tests (39 positions with good and bad numbers, 40 at -149 / -249, exactly -150 and exactly -250, a step either side of each, the limits read from the config); `wants_5m` at 2.99, 3.0 and 3.01, no NAIK day, NaN, inf and bool; and an end-to-end class with 40 trades whose sums sit exactly on both limits (-150.0 and -250.0 in binary floating point), half a USDT better and worse, three big winners, and 39 positions.
- `tests/test_mode_backtest_tradelog.py` (35): the header equal to the document twice (a copy typed in the test and the code block read out of the spec file), 23 distinct columns, WIB times (the brief's `2026-08-05T07:15:00+07:00`, midnight, the epoch, never `Z`), the refusals of `wib_iso`, one full row written out by hand and a field-by-field map with a number of its own in every field (signal 15 minutes before the entry, exit 45 after), a SHORT row, `kondisi` from the trade and not the context, the fills as the prices, missing indicators as empty cells, `trade_id` from 1 in every log, order kept, float round trips with awkward values, times parsed back to the same instant, the file written exactly, a text path, a missing folder, an existing file never overwritten (original bytes kept, label in the message) and a trade that cannot be written leaving no file.
- `tests/test_mode_backtest_report.py` (25): the header equal to the document (a copy and the line read out of the spec), a row written out in full in Indonesian notation (`1.234`, `2,50`, `48,1%`, `-1.234`, `600 / 500 / 134`, `45,5 menit`), dashes for missing values, "-0" never shown, nine cells in every line, a trailing newline; the "Tidak trading" row after each tahap, after the last row of a tahap when rows interleave, with a dash when there is no NAIK day, one per pair, with 0 positions and a result of 0; and a table made from real trades through `stage_metrics`.

RED: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_metrics tests.test_mode_backtest_tradelog -v` before any of the three modules existed (paths shortened). The brief expected an import failure; neither `metrics` nor `tradelog` existed, and the table tests failed the same way on `metrics`:

```text
test_mode_backtest_metrics (unittest.loader._FailedTest) ... ERROR
test_mode_backtest_tradelog (unittest.loader._FailedTest) ... ERROR

======================================================================
ERROR: test_mode_backtest_metrics (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_mode_backtest_metrics
Traceback (most recent call last):
  File "<py>/unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File "<repo>/tests/test_mode_backtest_metrics.py", line 21, in <module>
    from research.mode_backtest.metrics import (
ModuleNotFoundError: No module named 'research.mode_backtest.metrics'


======================================================================
ERROR: test_mode_backtest_tradelog (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_mode_backtest_tradelog
Traceback (most recent call last):
  File "<py>/unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File "<repo>/tests/test_mode_backtest_tradelog.py", line 19, in <module>
    from research.mode_backtest.tradelog import COLUMNS, trade_log_text, wib_iso, write_trade_log
ModuleNotFoundError: No module named 'research.mode_backtest.tradelog'


----------------------------------------------------------------------
Ran 2 tests in 0.000s

FAILED (errors=2)
```

```text
test_mode_backtest_report (unittest.loader._FailedTest) ... ERROR

======================================================================
ERROR: test_mode_backtest_report (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_mode_backtest_report
Traceback (most recent call last):
  File "<py>/unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File "<repo>/tests/test_mode_backtest_report.py", line 13, in <module>
    from research.mode_backtest.metrics import StageMetrics, stage_metrics
ModuleNotFoundError: No module named 'research.mode_backtest.metrics'


----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (errors=1)
```

GREEN, first run with the modules written: `Ran 123 tests` / OK. A first pass that passes at once proves little, so the tests were then checked against planted bugs and against coverage (below), and two gaps were closed.

RED, 2: the first mutation pass (111 bugs) left one survivor, the volume summed with `sum` instead of `fsum`: the order test used 40 identical volumes, which add up exactly in any order. `test_hasil_does_not_depend_on_the_order_of_the_trades` now uses 60 random profits and volumes and 20 shuffles; against the final tests the mutant fails. Coverage showed `metrics.py` line 95 (the bool guard of `_is_finite`) never run, so a bool, an int beyond the float range and text were added to the profit, volume, result and ratio refusals. Two further tests came from the self-review: `mode_table` read its rows twice, so a generator gave a header-only table (`test_any_iterable_of_rows_gives_the_same_table`; `mode_table` now takes `tuple(rows)`), and `test_the_metrics_survive_a_round_trip_through_json` pins that the metrics are JSON-ready for the stages.

GREEN, 2: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_metrics tests.test_mode_backtest_tradelog tests.test_mode_backtest_report` `Ran 125 tests` / OK.

Verification:

- Focused: the three new modules, `tests.test_mode_backtest_structure`, `tests.test_mode_backtest_step0` and `tests.test_pine_gate_structure` `Ran 209 tests` / OK on Python 3.10.20 (`.venv`) and on 3.11.15 (a scratch venv with `requirements.txt`, because CI runs 3.11). The brief's command (`tests.test_mode_backtest_metrics tests.test_mode_backtest_tradelog`) `Ran 100 tests` / OK.
- Line and branch coverage of `metrics.py`, `tradelog.py` and `report.py` (scratch rcfile, because the 3.10 `coverage` cannot read `pyproject.toml`): 100 % (176 statements, 38 branches, 0 missed).
- Mutation check (scratch copy of `src`, not committed): 112 bugs planted one at a time and run against the three new modules; all 112 are caught. By area: the pass rule (13: each comparison `>=` or `<=`, `and` to `or`, the minimum fixed or off by one, the limits and the status text, the verdict inverted, the 5m comparison and its None check, the result checks off), the numbers (24: the scale, `fsum`, the trim's rank key, its tie-break, its cut and its count, Menang's denominator and its empty and falsy cases, tp and sl swapped, entry per day with a floor of 1 or a zero for none, the hold in seconds, not averaged, or from the signal, waktu not an exit reason), the argument checks (11), the log (38: columns swapped or dropped, every column fed from a neighbouring field, `trade_id` from 0 or shifted, a rounded or fixed-precision number, `None` as a number, CRLF, UTC instead of WIB, the whole-second and type checks of `wib_iso`, the label of the write, a bool side, the header missing, the rows sorted) and the table (26: each header cell, each column's decimals and unit, the order of the exit counts, the baseline row's cells, its group key, its position after every row, after none or after the first, its label, its pair and tahap, the separator, the trailing newline, `tuple(rows)`).
- Full suite, exact command `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`: `Ran 2208 tests` / OK (2,083 + 125 new).
- `compileall -q src tests` OK; `git diff --check` clean; `ruff check --select F,E,W,B --line-length 100` clean on the six new files and `report.py`, `ruff format --check --line-length 100` clean on the six new files (`report.py` as a whole was not ruff-format clean before this task, so its step-0 part is left as it was and the additions follow its existing style); `tests.test_secret_hygiene` OK with this document tracked; `tests.test_mode_backtest_structure` OK (no private import, no network or venue word, no file write in the package, with the two new modules covered).

Notes for later tasks:

- **Task 14**: call `stage_metrics(trades, hari_naik=len(naik_days), drop_top=cfg.uji.lolos_buang_posisi_teratas)`; the trimmed result is only right with that number. `wants_5m(m15, cfg.uji)` raises `ValueError` when the training stage has no NAIK day: let it end the `train` command with a one-line error and exit 1, do not catch it into False. `dataclasses.asdict(metrics)` is JSON-ready (floats, ints and `None`). Write one CSV per run with `write_trade_log` (its `trade_id` starts at 1 in each) and build the `.md` with `mode_table`: give every row of a tahap the same `hari_naik`, list Mode A first and the comparators last, and label the comparator rows yourself (`ReportRow.mode`); "Tidak trading" is added by the table.
- **The code fingerprint** must cover the new imports: `metrics.py` imports `bot.strategy.modes.config` (already in `modes/*.py`) and `.simulate`; `tradelog.py` imports `bot.strategy.modes.position_rules`, `research.pine_gate.fileio` and `research.pine_gate.spec`; `report.py` now imports `.metrics`. `fileio` and `spec` are `research.pine_gate` modules, so `CODE_EXTRA` needs them (`step0.py` and `simulate.py` already import them).
- **The status line** the `test` stage prints (Task 16 asks for the three numbers) can read `m.posisi`, `m.hasil_per_1_juta` and `m.hasil_tanpa_teratas` from the same `StageMetrics` that `pass_status` judged. The table rounds Hasil per 1 juta to a whole number, so -149.6 shows as -150 and still passes; the status line should print the exact values.

## Task 14 — Lock and the train/test stages

`train` and `test` make the Mode A backtest a pre-registered, one-shot experiment:

- `research.mode_backtest.lock`: `LockError(message, problems=())` (a `RuntimeError`; `problems` holds one line per mismatch), `CODE_DIRS`, `CODE_EXTRA`, `code_files()`, `code_sha256()`, `python_version()` (`"3.10"`), `run_name(mode, pair)`, `lock_path(out_dir, mode, pair)` (`mode_A_BTC-USDT_lock.json`), `marker_path(data_dir, mode, pair, lock_sha)` (`holdout_runs/mode_A_BTC-USDT_<sha>.json`), `existing_markers(data_dir, mode, pair)`, `read_lock(path)` (the document and the sha256 of its bytes), `check_lock(doc, *, config_sha, code_sha, train_manifest_sha, python)` and `claim_marker(path, doc)`. Fix round 1 added `started_path(out_dir, mode, pair)` (`mode_A_BTC-USDT_test_started.json`) and `claim_started(path, doc)`.
- `research.mode_backtest.stages`: `StageError` (a `ValueError`, exit 1), `check_phase1`, `train_stage(config_path, data_dir, out_dir, mode, pair, now) -> Path` and `test_stage(...) -> Path`, each returning its result JSON.
- `research.mode_backtest.cli`: `train` and `test`, each with `--mode --pair --config --data-dir --out-dir`. They print every file written (the test also its run marker), then the status line. A `LockError` prints its problems on stdout, then one `error:` line, and exits 2.
- `bot.strategy.modes.config.load_modes_config_with_sha256(path)`: reads the file once and returns the config and the sha256 of those same bytes. `load_modes_config` now delegates to it.
- Test helpers: `tests/mode_backtest_helpers.write_minute_warmups` builds each warm-up bar from the minute prices it covers, so a moving price has no ATR jump at the seam. `tests/mode_backtest_stages.py` holds a synthetic rising week (Mon 6 to Fri 10 July 2026: training Mon to Wed, the test Thu and Fri, every day NAIK, both Mode A and the comparator trade) and `stage_config_text(**uji)`, which moves the `uji` dates of the repo YAML onto it (quoted WIB midnights) and refuses a key not written exactly once.

Controller rulings for this task (recorded as given):

1. The config is hashed and parsed from the same bytes (one read), in `train` and in `test`.
2. `code_files()` covers every `research/mode_backtest/*.py`, every `bot/strategy/modes/*.py`, and every other `bot.*` and `research.pine_gate.*` module the package imports, followed transitively within `bot` and `research`. The structure test fails when such a module is missing. Replacing `data.py`'s `cli_common.file_sha256` import was allowed, not required.
3. The training manifest is `stage_files(data_dir, "BTC-USDT", latihan_mulai, latihan_selesai - 1 day)`. `test` recomputes it by hashing only and compares it with the lock before the marker.
4. `train` reads only `latihan_mulai` to `latihan_selesai - 1 day`. `test` loads `latihan_mulai` to `ujian_selesai - 1 day`, and only after the marker is written.
5. `train` fails (exit 1) when the 4h EMA50 or the 720-hour ATR median is `None` at the first training day's 00:00 WIB.
6. NAIK days come from `frames.hourly.naik_days(start, end, window, uji.hari_naik_minimal_jam_naik)` and feed both Mode A and the comparator.
7. `stage_metrics` gets `cfg.uji.lolos_buang_posisi_teratas` at exactly one call site, and a test shows the YAML value changes the verdict.
8. The JSON and the status lines carry the exact (unrounded) Hasil per 1 juta and without-top values, plus, for `train`, the 15m entries per NAIK day next to the 5m decision.
9. Each stage runs Mode A in NAIK at the chosen timeframe and the comparator, Mode D (3m) in NAIK. `train` runs A at `tf_pemicu`, and once more at 5m when `wants_5m`; both training results are kept.
10. The lock binds the config, code and training manifest sha256s, Python major.minor, the chosen timeframe, the training summary, the stage bounds, the mode and the pair. It is written exclusively. The marker is claimed exclusively before any test day is read, and an existing marker refuses with exit 2 ("already ran").
11. Phase 1 refuses any mode but A and any pair but BTC-USDT with exit 1. Lock refusals and mismatches exit 2. Outputs: `mode_A_BTC-USDT_train_<YYYYMMDD>.json` (WIB date of `now`), one CSV per run and the lock; then `mode_A_BTC-USDT_test_<YYYYMMDD>.json`, the CSVs and a `.md` with the section 2.9 table (Latihan rows from the lock) and the status line.

Choices of mine, each pinned by a test:

- **The fingerprint is everything Python loads when the package is imported**: 43 `CODE_EXTRA` files beside the two globs (49 outside the package in all). That includes the packages around each imported module, because their `__init__` runs first, so `bot/arena/__init__.py` and the arena modules it imports (`agents`, `attribution`, `expression_engine`, `factors`, `flow_signals`, `program_trader`, `signals`, then `bot/models.py`, `bot/data/market_signals.py`, `bot/volume/profile.py`) are fingerprinted too. Two imports sit inside functions the package never calls and only read the live settings and Supabase: `bot.config` in `pine_gate.cli_common.make_settings` and `bot.storage.supabase_trade_store` in `pine_gate.calibrate`. They are left out, as `tests/test_pine_gate_structure.py` leaves them out. The structure test checks the list twice: with a static AST walk of every import statement (relative imports, `from package import submodule`, imports inside functions and `TYPE_CHECKING`, parent packages), followed transitively, and with a fresh interpreter that imports every module of the package and lists the files Python loaded. `CODE_EXTRA` must also hold no module the package does not import, no duplicate and no globbed file. I did not replace `data.py`'s `cli_common` import: `cli.py` (`CliError`, `one_line`) and `step0.py` (`load_calibration_file`) import `cli_common` too, so the change would not shrink the fingerprint.
- **The config is read once in `bot/strategy/modes/config.py`**: the only public way to parse bytes with the repeated-key refusal is a new function there, because `_parse_yaml` is private. The text is decoded as text mode reads it (line ends turned into `\n`), so `load_modes_config` behaves as before.
- **`test` checks the lock's own record before the marker**: mode, pair, both stage bounds against the config, the three training metrics records (`mode_a_15m`, `mode_a`, `pembanding`), and the locked timeframe re-derived by `wants_5m` on the locked 15m run. The last check came from mutation testing: with only a "15m or 5m" check, a lock edited from 15m to 5m by hand ran the one-shot test on 5m (RED 2 below).
- **The refusals before the claims, and only these** (as corrected in fix round 1, below):
  - any marker of the mode and pair in the data folder, whatever its lock, or the start record next to the lock. So `train` also refuses to make a new lock once the test ran ("a lock may be deleted and recreated only while no marker exists for it");
  - a test result of any date, or any output of today;
  - a day or warm-up file that is missing or not a regular file. This checks names and file types only, one `stat` each; nothing is read.

  The content of the test days is not checked before the claims. That is `verify-copy`'s job, which Task 16 runs right before `test`. A test day that fails to read after the claims ends the run with the one shot used up. A training file that has gone is a training-data mismatch (`LockError`, exit 2), not exit 1.
- **The warm-up guard checks all six hourly inputs** of the selector at 00:00 WIB of the first training day, a superset of the ruling's two. One bar short of either warm-up (830 1h bars, or 400 4h bars, because the last 4h warm-up bar is still forming at 00:00 WIB) is refused with the series named.
- **`train` refuses `mode.A_pullback_long.tf_pemicu: 5m`** (`StageError`): the procedure starts on 15m and reaches 5m only through the switch, and a 5m start would make the switch run 5m twice.
- **Outputs**: run keys `A_15m_NAIK`, `A_5m_NAIK` and `D_3m_NAIK` name the CSVs (`mode_A_BTC-USDT_train_<date>_A_15m_NAIK.csv`) and the `runs` entries. The comparator rows are "D di NAIK". Every possible output is checked before any work, and the lock is written last. The JSON keys come out sorted (`fileio.json_text`), so `blocked` lists the gates alphabetically, not in gate order. The status lines are Indonesian, like the report, with every float as `repr`, so -149.63184210526315 shows in full while the table shows -150.

Test design: 50 new tests.

- `tests/test_mode_backtest_lock.py` (33).
  - The lock helpers: spec names and refusals of bad names and shas, `check_lock` listing all four mismatches and every missing binding, `read_lock` (sha of the bytes, missing, broken, a folder), `claim_marker` never overwriting, markers of one mode and pair, and `code_files`/`code_sha256`.
  - The training stage: results, logs and an exclusive lock, and no training exit after `latihan_selesai`. `test_train_never_opens_a_test_day_file` uses a corrupt Thursday file, a folder in place of Friday, and a recorder of every `gzip.open` and `Path.read_bytes`. Also the 5m switch run once and locked, no switch, the NAIK-day minimum from the config (18 hours drops Monday, which has 17 window hours), both short warm-ups, a flat market without a NAIK day (exit 1, "5m switch cannot be evaluated"), `tf_pemicu: 5m` and a naive clock.
  - The test stage:
    - the verdict and the report table, rebuilt with `mode_table` from the lock and the result;
    - `test_marker_is_written_before_any_test_day_is_read`: every read of a test-day file happens after the marker exists, and a training file is hashed before it;
    - a second run (same day and three days later);
    - a new lock after the test;
    - no lock;
    - a hand-edited lock (tf, bounds, pair, summary, a 15m run without a NAIK day);
    - a missing test day;
    - `test_test_refuses_on_a_changed_config_code_or_training_byte`: config, a training day, a warm-up, a missing training day, code and Python, each exit 2 with nothing written, then the unchanged lock runs;
    - `test_outputs_are_never_overwritten`.
  - The verdict: `drop_top` 3 against 0 on 40 fabricated trades gives GAGAL against LOLOS, and every `stage_metrics` call got the YAML value. The exact numbers: 7 entries on 3 NAIK days is `2.3333333333333335` and switches to 5m; a test at -149.63 per 1M is LOLOS while the table shows -150. A test with no positions is BELUM CUKUP SAMPEL and shows dashes. An AST check finds a single `stage_metrics` call site, passing `cfg.uji.lolos_buang_posisi_teratas`.
- `tests/test_mode_backtest_cli.py` (+7): missing options (exit 2), help, Phase 1 refusals for both commands (exit 1, nothing written), the stage called with the paths and the clock and its outputs printed, a `LockError` printing its problems then one line (exit 2), and a `StageError` (exit 1).
- `tests/test_mode_backtest_structure.py` (+6): `test_fingerprint_covers_every_bot_and_pine_gate_module_the_package_imports`, which also asserts that the walk reaches transitive modules such as `bot/strategy/indicators.py` and `bot/arena/factors.py`. Also no stale `CODE_EXTRA` entries, the interpreter cross-check, and the walker on synthetic sources (every import form, a package's relative imports, the I/O-only and non-module names dropped).
- `tests/test_modes_config.py` (+4): the config and sha of one read, a file edited between two reads still binding the parsed bytes, CRLF files, and the errors of `load_modes_config`.

RED: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_lock tests.test_mode_backtest_cli tests.test_mode_backtest_structure -v` before `lock.py` and `stages.py` existed (paths shortened). The brief expected an import failure:

```text
test_mode_backtest_lock (unittest.loader._FailedTest) ... ERROR
test_mode_backtest_cli (unittest.loader._FailedTest) ... ERROR
test_mode_backtest_structure (unittest.loader._FailedTest) ... ERROR

======================================================================
ERROR: test_mode_backtest_lock (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_mode_backtest_lock
Traceback (most recent call last):
  File "<py>/unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File "<repo>/tests/test_mode_backtest_lock.py", line 27, in <module>
    from research.mode_backtest import cli, lock, stages
ImportError: cannot import name 'lock' from 'research.mode_backtest' (<repo>/src/research/mode_backtest/__init__.py)


======================================================================
ERROR: test_mode_backtest_cli (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_mode_backtest_cli
Traceback (most recent call last):
  File "<py>/unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File "<repo>/tests/test_mode_backtest_cli.py", line 19, in <module>
    from research.mode_backtest import cli, stages, step0
ImportError: cannot import name 'stages' from 'research.mode_backtest' (<repo>/src/research/mode_backtest/__init__.py)


======================================================================
ERROR: test_mode_backtest_structure (unittest.loader._FailedTest)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_mode_backtest_structure
Traceback (most recent call last):
  File "<py>/unittest/loader.py", line 154, in loadTestsFromName
    module = __import__(module_name)
  File "<repo>/tests/test_mode_backtest_structure.py", line 23, in <module>
    from research.mode_backtest import lock
ImportError: cannot import name 'lock' from 'research.mode_backtest' (<repo>/src/research/mode_backtest/__init__.py)


----------------------------------------------------------------------
Ran 3 tests in 0.000s

FAILED (errors=3)
```

The config tests failed the same way, on the function that did not exist yet (`PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_config`):

```text
ImportError: cannot import name 'load_modes_config_with_sha256' from 'bot.strategy.modes.config' (<repo>/src/bot/strategy/modes/config.py)

----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (errors=1)
```

GREEN, first run with the modules written: `Ran 69 tests` with 2 failures, both one assertion: `blocked` was expected in gate order, but `fileio.json_text` sorts JSON keys. The test now compares the key set. After that, the three modules and the config tests ran `Ran 134 tests` / OK.

RED, 2: mutation testing. First pass: 28 bugs were planted one at a time in a scratch copy of the repository (`src`, `tests`, `config`) and run against the lock, CLI and config tests. A first attempt had copied only `src`, so `test_code_sha256_is_the_sha256_of_the_code_file_lines`, which hashes the files next to the tests, failed for every mutant whatever it changed. The run was redone on a full copy. Three bugs survived:

- my marker mutant was wrong: it claimed a second marker while the real one was still claimed first. It was rebuilt to claim the real marker after the test days are read;
- the lock's bounds were not checked by any test;
- the lock's `tf` was not checked by any test, and a hand edit from 15m to 5m was accepted.

The fix re-derives the timeframe with `wants_5m` on a 15m record the lock now keeps (`summary.mode_a_15m`), with `test_a_hand_edited_lock_is_refused_before_the_marker` as its test. A test for a missing test day refused before the marker was added in the same pass. Here is that test against a copy that has the earlier check (the 5m edit runs the one-shot test, exit 0, and so uses it up):

```text
======================================================================
FAIL: test_a_hand_edited_lock_is_refused_before_the_marker (tests.test_mode_backtest_lock.TestStageTests) (edit='tf')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "<copy>/tests/test_mode_backtest_lock.py", line 577, in test_a_hand_edited_lock_is_refused_before_the_marker
    self.assertEqual(code, 2, err)
AssertionError: 0 != 2 :
```

GREEN, 2: all 28 runtime bugs are caught. They include train reading into the test stage, the marker claimed after the test days are read, the test loaded from its own start (cold series), `drop_top` fixed at 3, a rounded status line, `check_lock` ignoring Python or stopping at the first mismatch, no warm-up guard, train ignoring markers, the NAIK-day minimum fixed at 1, the switch always or never locking 5m, the comparator in TURUN, a config hashed from a second read, the warm-ups left out of the manifest, the CLI exiting 1 for a lock refusal, and the verdict taken from the comparator. All 9 structural bugs are caught too: an arena module, `indicators.py` (reached only through `features` and `near_mean`), `cli_common`, or a package `__init__` dropped from `CODE_EXTRA`; a module that is not imported, a duplicate or a globbed file added to it; a second `stage_metrics` call; and a new project import missing from the fingerprint. Each missing module fails both the AST walk and the interpreter check.

Coverage then showed four lines of mine never run: an unreadable lock (a folder), a naive clock, the dash for a missing result in a status line, and a locked 15m record without a NAIK day. Each now has a test. Line and branch coverage of `lock.py`, `stages.py` and `cli.py` is 100 % (scratch rcfile, because the 3.10 `coverage` cannot read `pyproject.toml`). `config.py` is at 99 %: one partial branch (a YAML key that is not a scalar) predates this task.

Verification:

- The brief's command, `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_lock tests.test_mode_backtest_cli tests.test_mode_backtest_structure -v`: `Ran 73 tests` / OK. `tests.test_modes_config`: `Ran 65 tests` / OK.
- Python 3.11.15 (the scratch venv with `requirements.txt`, because CI runs 3.11): those four modules plus `tests.test_mode_backtest_data` and `tests.test_pine_gate_structure`, `Ran 215 tests` / OK.
- Full suite, exact command `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`: `Ran 2258 tests` / OK (2,208 + 50 new).
- `compileall -q src tests` OK. `git diff --check` is clean. `ruff check --select F,E,W,B --line-length 100` is clean on every new and changed file; the long lines in `tests/test_modes_config.py` were there before (27 at HEAD, 27 now). `tests.test_secret_hygiene` OK with this document tracked. `tests.test_mode_backtest_structure` OK, with `lock.py` and `stages.py` covered by the private-import, network-word and write rules.
- End to end on the synthetic week through `python -m research.mode_backtest`, against a temporary data copy and never the real one:
  - `train`: exit 0, four files and the line `entry per hari NAIK pada 15m = 4.0 ... tetap 15m, dikunci 15m`;
  - `test`: exit 0, four files, the marker and BELUM CUKUP SAMPEL (12 positions);
  - a second `test`: exit 2, "already ran";
  - `--pair ETH-USDT`: exit 1.
- Scale: `train` on 20 synthetic days took 0.28 s at 49 MB peak RSS, about 5 s for the 370 real training days if it scales linearly.

Notes for later tasks:

- **Task 15**: document `train`/`test` in CLAUDE.md, with the lock and the start record in `--out-dir`, the marker in `--data-dir/holdout_runs/`, exit 2 for lock refusals, and the fingerprint scope (the arena modules are covered because `bot/arena/__init__.py` imports them).
- **Task 16**:
  - Run `verify-copy` right before `test`. Before its claims, `test` checks only names and file types: every day and warm-up file must be a regular file, one `stat` each, nothing read. Content integrity comes from `verify-copy` alone. A test day that is a regular file but fails to read after the claims (the start record and the marker) ends the run with the one shot used up, whatever data folder a rerun names.
  - Keep the checkout unchanged between `train` and `test`: any change to the 61 fingerprinted files (12 package modules, 6 rule-core modules, 43 in `CODE_EXTRA`), the YAML, the training files or the Python minor version makes `test` exit 2. While no marker exists, the lock may be deleted and `train` re-run.
  - Run both stages with the same `--data-dir`, because the marker lives there.

### Fix round 1 (task review)

Two review findings, raised to Important by the controller because Task 16 relies on the one-shot guarantee:

1. **The one-shot guard covered only the `--data-dir` it was given.**
   - The marker lived only in `<data_dir>/holdout_runs`, and the out-dir check matched only a finished result. The training manifest uses relative paths, so the lock validates against any copy of the data.
   - So a `test` that failed after claiming its marker (exit 1) could be run again with a fresh copy, the same out-dir and the same lock. It ran to the end (exit 0) and claimed a second marker.
   - Fix (controller ruling): `test` now also claims, exclusively, the start record `mode_A_BTC-USDT_test_started.json` next to the lock. It holds the lock sha256, the resolved data folder, the time and the bindings. It is claimed in the same step as the marker, after every refusal and before any day of the test stage is read: the start record first, then the marker.
   - When the start record exists, `test` and `train` both refuse (exit 2, "already ran"), whatever `--data-dir` names.
   - New code: `lock.started_path` and `lock.claim_started`, which shares the exclusive claim with `claim_marker`; `stages._refuse_after_the_test` now takes the out folder too.
2. **The day check before the marker used `exists()`.**
   - A folder in place of a test day passed the check, the marker was claimed, and the run died on `[Errno 21] Is a directory`, using up the shot.
   - Fix: every day and warm-up file must pass `is_file()` before the claims (one `stat` each, nothing read). A failure is refused with exit 1, naming each path relative to the data folder and why ("missing" or "no regular file").
   - The "refusals before the claims" bullet above and the Task 16 note now say exactly what is checked: names and regular files only. The content comes from `verify-copy`.

Tests (3 new, 4 extended):

- `test_a_test_that_failed_after_its_claim_refuses_a_rerun_on_a_fresh_data_copy` reproduces the probe:
  - a corrupt test day passes the checks because it is a regular file, so the first `test` claims and then fails (exit 1);
  - a second `test`, with a fresh copy in another data folder and the same out-dir, exits 2 "already ran" and writes nothing: every file under the temporary root is unchanged, and the copy has no `holdout_runs`.
- `test_a_folder_in_place_of_a_test_day_is_refused_before_the_marker`: exit 1 naming `prod/BTC-USDT/1m/2026-07-10.csv.gz`, with no marker, no start record and nothing written.
- `test_the_start_record_sits_next_to_the_lock_and_is_claimed_once`.
- Extended:
  - `test_marker_is_written_before_any_test_day_is_read`: both records exist before every read of a test day, and the record holds the lock sha256, the resolved data folder and the time;
  - `test_a_new_lock_is_refused_once_the_test_ran`: also on a fresh copy without markers;
  - `test_a_missing_test_day_is_refused_before_the_marker`: also no start record;
  - the result's `files` lists the start record.

RED: the new and extended tests against the code of `488ab42` (paths shortened):

```text
======================================================================
FAIL: test_a_test_that_failed_after_its_claim_refuses_a_rerun_on_a_fresh_data_copy (tests.test_mode_backtest_lock.TestStageTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "<repo>/tests/test_mode_backtest_lock.py", line 572, in test_a_test_that_failed_after_its_claim_refuses_a_rerun_on_a_fresh_data_copy
    self.assertEqual(code, 2, err)
AssertionError: 0 != 2 :

======================================================================
FAIL: test_a_folder_in_place_of_a_test_day_is_refused_before_the_marker (tests.test_mode_backtest_lock.TestStageTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "<repo>/tests/test_mode_backtest_lock.py", line 658, in test_a_folder_in_place_of_a_test_day_is_refused_before_the_marker
    self.assertFalse(self.marker().exists())
AssertionError: True is not false

======================================================================
FAIL: test_a_new_lock_is_refused_once_the_test_ran (tests.test_mode_backtest_lock.TestStageTests) (data_dir='fresh_copy')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "<repo>/tests/test_mode_backtest_lock.py", line 601, in test_a_new_lock_is_refused_once_the_test_ran
    self.assertEqual(code, 2, err)
AssertionError: 0 != 2 :

----------------------------------------------------------------------
Ran 3 tests in 0.760s

FAILED (failures=3)
```

The whole module failed with `FAILED (failures=5, errors=1)`. The error was that `lock.started_path` did not exist yet; the two other failures were the start record missing from `files` and from the marker-before-read probe.

GREEN:

- The covering command, `PYTHONPATH=src .venv/bin/python -m unittest tests.test_mode_backtest_lock tests.test_mode_backtest_cli tests.test_mode_backtest_structure -v`: `Ran 76 tests` / OK, and OK on 3.11.15 as well.
- Full suite: `Ran 2261 tests` / OK.
- Line and branch coverage of `lock.py`, `stages.py` and `cli.py`: 100 %.
- `compileall` OK, `ruff` clean, `git diff --check` clean.
- Mutation check on a full repo copy: all 7 bugs planted in the new guards are caught. They were: the start record ignored by the refusal, `exists()` in place of `is_file()`, the start record never claimed, the start record claimed after the test days are read, `train` looking for it in the data folder, a claim that is not exclusive, and the record written without its data folder.
- End to end through `python -m research.mode_backtest` on a temporary synthetic copy:

  | Case | Result |
  |---|---|
  | A folder in place of a test day | exit 1, nothing claimed, so the shot is not used |
  | A corrupt regular file | the claims are made, then exit 1 |
  | A fresh copy with the same out-dir | exit 2, "already ran (…/mode_A_BTC-USDT_test_started.json)" |
  | `train` on the fresh copy after deleting the lock | exit 2 |
