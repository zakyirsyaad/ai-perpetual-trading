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
