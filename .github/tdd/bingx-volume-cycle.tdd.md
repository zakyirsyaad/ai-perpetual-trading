# BingX Volume Cycle — TDD Evidence

## User journey

Start a new BingX VST trading-volume cycle from zero at an explicitly configured UTC timestamp, without deleting or counting historical OKX trades. Keep the existing daily risk controls active and stop new entries when the configured BingX volume target is reached.

## RED evidence

The first focused run failed with `FFEF` because closed-trade volume queries had no BingX exchange filter, the cycle-start setting did not exist, and timezone-naive timestamps were accepted.

The expanded store tests then failed with `FFF` because daily volume, daily risk, and recent-trade reads were not consistently scoped to BingX.

A security regression test also failed before the final guard was implemented:

```text
test_settings_rejects_volume_cycle_start_far_in_the_future
AssertionError: ValidationError not raised
```

## GREEN implementation

- Added the optional `VOLUME_CYCLE_STARTED_AT` setting.
- Required timezone-aware input and normalized it to UTC.
- Rejected timestamps more than five minutes in the future.
- Scoped operational closed-trade reads to `exchange=bingx`.
- Applied the cycle cutoff only to volume accounting.
- Kept daily PnL and stop-loss risk counters scoped to BingX for the full local day.
- Preserved all historical OKX rows.

## Verification

```text
Focused config/store tests: 38 passed
Full test suite before final audit: 428 passed
Line coverage: 82% (required minimum: 80%)
compileall: passed
git diff --check: passed
pip check: passed
secret hygiene test: passed
```

## Final audit correction

The first Sol audit found that a failed fill lookup could accept an unrelated exchange position. Two negative tests were added first and failed because no exception was raised for either readback:

- an opposite-side position;
- a position 50% larger than the submitted entry size.

The engine now requires a compatible venue side and rejects readback size above the submitted size plus the existing 2% matching tolerance before persisting or managing it.

```text
Focused fallback tests: 3 passed
Full test suite after correction: 431 passed
Line coverage after correction: 82%
compileall: passed
git diff --check: passed
pip check: passed
secret hygiene test: passed
```

## Commit trail

- `165f10f` — `test: add BingX volume cycle regression coverage`
- `2dfde9e` — `fix: scope volume targets to the BingX cycle`
- `28dd166` — `test: initialize BingX scope in timezone fixture`
- `e313df1` — `test: reject future volume cycle timestamps`
- `ba0fd6d` — `fix: fail closed on future volume cycle timestamps`

## Deployment gate

Before activation, verify without exposing secrets that the VPS uses `VOLUME_TARGET_USDT=10000000`, set `VOLUME_CYCLE_STARTED_AT` to the current UTC time, confirm BingX VST has no pre-existing position or open order, and prove the scoped cycle volume is zero before restarting PM2.
