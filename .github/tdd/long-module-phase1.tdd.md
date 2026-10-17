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

`src/research/mode_backtest/frames.py` starts here; the bot never imports it. `HourlyFrame.build(pair, sel)` joins the native 1h and 4h warm-ups to the candles aggregated from the 1m bars (`features.htf_bars`), computes the selector's series once (SMA fast and slow of the 1h closes, ATR14 (1h), the median of the last 720 ATR14 values, the 4h EMA) and keeps them as tuples. `context_at(t_ms)` only looks them up with `last_closed_index`, so at `t` the candles with `open + interval <= t` are visible and nothing else (D2). It returns a frozen `HourlyContext(sma20_1h, sma50_1h, atr14_1h, atr_median, close_4h, ema50_4h, kondisi)`, where `kondisi` is `pilih_mode` of the five selector inputs. `naik_days(start_ms, end_ms, window)` returns the WIB days that have a NAIK hour inside the weekly entry window.

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
