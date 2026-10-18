# LONG Mirror with Live Exits (mode L) — TDD Evidence

Offline backtest of a LONG mirror of the live SHORT bot, with the live bot's exits, on the Phase 1
harness. Spec: `docs/superpowers/specs/2026-10-18-long-mirror-live-exit-design.md`. Each task below
records its RED output and GREEN result.

## Task 1: Live-exit config file and loader

Files: `config/modes_live_exit.yaml` (spec section 7, verbatim), `src/bot/strategy/modes/config.py`
(`LiveExitModeConfig`, `CostConfig`, `LiveExitConfig`, `parse_live_exit_config`,
`load_live_exit_config_with_sha256`, `check_live_trigger_matches`), `tests/test_modes_live_exit_config.py`.
`config/modes.yaml` is unchanged (`git diff --quiet config/modes.yaml`).

### RED

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_live_exit_config -v
ERROR: test_modes_live_exit_config (unittest.loader._FailedTest)
ImportError: Failed to import test module: test_modes_live_exit_config
  File ".../tests/test_modes_live_exit_config.py", line 17, in <module>
    from bot.strategy.modes.config import (
ImportError: cannot import name 'CostConfig' from 'bot.strategy.modes.config'
Ran 1 test in 0.000s
FAILED (errors=1)
```

Expected: none of the new names exist yet.

### GREEN

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_live_exit_config tests.test_modes_config
Ran 85 tests in 2.5s
OK
```

`load_modes_config_with_sha256` and the new loader now share `_load_with_sha256` (one read, hashed
and parsed from the same bytes); `tests.test_modes_config` passes unchanged. Full suite result is in
the task report.

Full suite before commit: `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests` -> `Ran 2306 tests in 196.763s, OK`.

## Task 2: Exit-level formulas and a side-neutral near-mean trigger

Files: `src/bot/strategy/modes/live_exit.py` (`LiveExitLevels`, `stop_pct`, `net_target_price`,
`live_exit_levels`), `src/bot/strategy/modes/near_mean.py` (`near_mean`; `near_mean_short` is the same
function under Mode D's name), `tests/test_modes_live_exit.py`, `tests/test_modes_near_mean.py`.
Numbers only, no bars and no I/O; the expected prices in the tests are worked out by hand from spec section 4.

### RED

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_live_exit tests.test_modes_near_mean
  File ".../tests/test_modes_near_mean.py", line 15, in <module>
    from bot.strategy.modes.near_mean import near_mean, near_mean_short, stretch_from_window
ImportError: cannot import name 'near_mean' from 'bot.strategy.modes.near_mean'
Ran 2 tests in 0.000s
FAILED (errors=2)
```

(`tests.test_modes_live_exit` fails the same way on `bot.strategy.modes.live_exit`, which did not exist.)

### GREEN

```
$ PYTHONPATH=src .venv/bin/python -m unittest tests.test_modes_live_exit tests.test_modes_near_mean
Ran 28 tests in 0.004s
OK
```

Reference values reproduced: stop pct 0.28 / 0.30 / 0.40 for ATR 0.10 / 0.20 / 0.30; TP ratio
1.0032416208 (LONG) and 0.9967616192 (SHORT) at 25,000; entry 100.003 LONG gives sl 99.722992,
tp 100.327172, cut 99.902997; entry 99.997 SHORT gives sl 100.276992, tp 99.673172, cut 100.096997.
The net-reaches-N property is also checked from gross minus both fee legs.

Full suite: `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests` -> `Ran 2322 tests, OK`;
`compileall` and `git diff --check` clean.
