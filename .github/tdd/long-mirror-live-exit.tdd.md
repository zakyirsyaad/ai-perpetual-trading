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
