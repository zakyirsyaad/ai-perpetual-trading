# Weekly entry window (`ENTRY_WEEKLY_WINDOW`)

## User journey

The operator wants no new trades on weekends: entries only from **Monday 07:00 to Friday
24:00 WIB** (the trading timezone). Positions that are already open must still be monitored
and closed normally, and the model should not be called (or paid for) while the window is
closed. The bot had no notion of trading days, and stopping the process on a schedule is unsafe:
a stopped bot orphans an open position and a restart with a position open crash-loops (104103).

## RED evidence

`tests/test_entry_window.py` was written before any implementation:

```
ModuleNotFoundError: No module named 'bot.core.entry_window'
Ran 1 test ... FAILED (errors=1)
```

## GREEN implementation

- `bot/core/entry_window.py` - pure parsing and the open/closed decision. `"mon 07:00-sat 00:00"`
  becomes minutes since Monday 00:00; `fri 24:00` equals `sat 00:00`; start must precede end
  within one Monday-Sunday week; empty means always open.
- `config.py` - `ENTRY_WEEKLY_WINDOW` (default empty), validated and normalised at startup so a
  typo refuses to boot instead of silently trading on weekends.
- `core/trade_engine.py` - `_tick` checks the window right after the reconciliation latch and
  before the risk guard, the model call and the strategy. It logs once when the window closes and
  once when it reopens, not once per tick. Open-position management (`_manage_open_position`,
  restart resume) is untouched, so a position opened late on Friday keeps being monitored into Saturday.
  `_now_in_trading_tz` uses `TRADING_TIMEZONE`.
- `.env.example` documents the setting.

## Verification

| Check | Result |
|---|---|
| New tests | 12 passed (parsing, boundaries, tick gating, once-only logging, timezone, setting validation) |
| Full suite | 601 passed |
| Coverage | 82% (gate 80%); `entry_window.py` 97% |
| `compileall` | clean |
| `git diff --check` | clean |
| Secret hygiene | `test_secret_hygiene` passes inside the suite |
| `pip check` | not run - uv-managed interpreter has no pip; no dependency changed |

## Deployment gate

1. Merge only on the operator's approval.
2. VPS `.env`: `ENTRY_WEEKLY_WINDOW=mon 07:00-sat 00:00`.
3. Restart **only when the venue is flat**.
4. Expect one `Entry window closed (...)` line at the first tick after Saturday 00:00 WIB and one
   `Entry window open (...)` line at the first tick after Monday 07:00 WIB (ticks are ~4 minutes
   apart, so the real switch can lag the boundary by one tick).

## Known limits

- The window is evaluated per tick; an entry decided in the last tick before Saturday 00:00 can
  open a few minutes before the boundary and then runs to its normal exit, as intended.
- Holidays are not modelled, only the weekly pattern.
