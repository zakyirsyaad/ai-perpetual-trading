# Entry direction filter (`ENTRY_GATE_DIRECTION`)

## User journey

The operator accepts lower volume in exchange for trading only the bucket that has shown
positive gross: **SHORT entries within 0.75 ATR of the 21-EMA** ("K2"). Over 700 stored BTC
decisions (31 Aug - 6 Oct 2026) K2 carried +162 gross per 1M volume (15 m forward return
+3.2 bps, SE 2.3) in both phases (+154, +171), against -104 for all decisions and -158 for
everything outside K2. K2 supplies about 4 entries a day, roughly 0.2M of the 1M/day target.
The stretch gate could already drop stretched entries but let LONG near-mean through, which is
about zero gross; this adds the missing side restriction.

Evidence is a post-hoc bucket from several, SE is wide, and the volume cost is accepted
knowingly - so the first live weeks are also a forward read of K2.

## RED evidence

New tests in `EntryDirectionFilterTests` / `EntryGateDirectionSettingTests` failed before the
code existed:

```
ERROR: test_default_is_any_and_value_is_normalised   (Settings has no entry_gate_direction)
ERROR: test_unknown_value_is_rejected
FAIL:  test_other_side_is_blocked_even_when_it_sits_near_the_mean   'allow' != 'block'
FAIL:  test_a_day_behind_schedule_cannot_bypass_the_side_filter     'allow' != 'block'
FAIL:  test_shadow_mode_records_the_side_block_without_stopping_the_entry   'allow' != 'block'
Ran 13 tests ... FAILED (failures=3, errors=2)
```

The other new tests (chosen side allowed, stretch still blocks the chosen side, `off` ignores
the filter) passed before the change by design: they pin existing behaviour that must not move.

## GREEN implementation

- `config.py` - `ENTRY_GATE_DIRECTION` (`any` default, `long`, `short`), validated and lower-cased.
- `ai/orchestration.py` - `_stretch_gate_verdict` blocks the other side first, with
  `reason="direction"`; the check runs *before* the behind-schedule relaxation, so a day that is
  short on volume can never admit the side with no edge. Stretch blocks now carry
  `reason="stretch"`. `_build_entry_candidate` logs and honours the direction block in `enforce`,
  and only records it in `shadow`. Verdicts land in `ai_decisions.payload_json.gate` as before.
- `.env.example` - documents the setting; default `any` keeps every existing deployment unchanged.

## Verification

| Check | Result |
|---|---|
| Orchestration tests | 38 passed |
| Full suite | 589 passed |
| Coverage | 81% (gate 80%) |
| `compileall` | clean |
| `git diff --check` | clean |
| Secret hygiene | `test_secret_hygiene` passes inside the suite |
| `pip check` | not run - the interpreter is uv-managed and has no pip; no dependency changed |

## Deployment gate

1. Merge only on the operator's approval.
2. VPS `.env`: `ENTRY_GATE_DIRECTION=short` and `ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR=0`
   (otherwise a day that is behind schedule - always the case at 0.2M/day - relaxes the stretch limit).
3. Restart **only when the venue is flat** (104103 crash loop with an open position).
4. After start, expect `Entry candidate ... rejected: the gate lets through short entries only` for LONG
   proposals and no change to exits. `TAKER_FEE_PCT` stays 0.05 by operator decision, even though
   fills are charged 0.035% on the current account.

## Known consequence

Model calls still happen every tick, so token cost per accepted trade rises about fivefold.
A pre-model skip when price is stretched is a possible follow-up, not part of this change.
