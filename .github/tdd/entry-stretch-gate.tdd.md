# Entry Stretch Gate — TDD Evidence

## User journey

Stop opening entries where the account has measurably lost money — price far from its
own 21-EMA — without abandoning the 1M/day volume mandate. The gate ships **off**, has a
**shadow** mode that records what enforcement would have cost while changing nothing, and
relaxes itself when the trading day falls behind its volume schedule.

## Evidence that motivated the change

Replay over all 342 stored BTC decisions (one position at a time, both exit models that
bracket live results):

| Gate | volume/day | gross/1M | net/1M | pre-BX3 | BX3 |
|---|---|---|---|---|---|
| none (live behaviour) | 0.98M | −2 | −502 | +87 | −132 |
| near-mean only | 0.43M | **+170** | −330 | +224 | +50 |
| confidence ≥ 0.80 | 0.44M | +8 | −492 | +54 | −109 |

Near-mean-only is the single filter positive in **both phases and both exit models**; the
confidence filter flips sign between exit models (+8 vs −370) and was dropped.

A schedule-aware relaxation traces the trade-off: threshold 1.4 entries/hour → 0.61M/day at
+46 gross/1M; threshold 2.0 → 0.52M/day at +89.

Prompt text cannot do this job. Replaying 150 ticks through mimo with a rewritten pacing
objective (123 calls, ~1.5M tokens) halved the entry rate — 25% of asked ticks down to 11% —
but did **not** change which ticks were picked: near-mean propensity fell with everything
else (8.3% → 4.3%) and entries stayed concentrated on stretched ticks. Selection belongs in
code; the prompt is a frequency dial.

## RED evidence

`tests.test_ai_orchestration.EntryStretchGateTests` — the orchestrator had no such concept:

```
    verdict = orchestrator._stretch_gate_verdict(self._decision(), context, None)
AttributeError: 'AIOrchestrator' object has no attribute '_stretch_gate_verdict'
Ran 5 tests — FAILED (errors=5)
```

After the first implementation the behind-schedule case still failed:

```
FAIL: test_a_day_behind_schedule_relaxes_the_gate
AssertionError: False is not true
```

The cause was a test fake missing `volume_profile`, which production always exposes in
`ai_volume` mode. Production was left strict on purpose — an unmeasurable schedule never
counts as "behind" — and the fake was completed instead.

## GREEN implementation

- `config.py` — `ENTRY_STRETCH_GATE_MODE` (`off` default, validated against
  off/shadow/enforce), `ENTRY_STRETCH_GATE_MAX_ATR` (0.75), and
  `ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR` (0 = never relax).
- `ai/orchestration.py:_stretch_gate_verdict` — builds the setup profile for the decision's
  symbol and direction, compares |stretch_atr| against the limit, and returns a verdict dict
  carrying mode, stretch, bucket, the limit, `behind_schedule`, and `entries_needed_per_hour`.
  A stretch that cannot be measured is never treated as a bad entry.
- `_entries_needed_per_hour` derives the remaining schedule from the pacing snapshot and the
  volume profile; when it cannot be computed the gate simply stays strict.
- `_build_entry_candidate` gained an optional verdict argument: `enforce` + `block` rejects
  with its own log line, `shadow` + `block` logs what it *would* have done and lets the entry
  through. Every other path is byte-identical to before.
- The verdict is stored on every decision at `ai_decisions.payload_json.gate`, so shadow mode
  produces a queryable record instead of log-only evidence.

## Verification

```
PYTHONPATH=src python -m compileall -q src tests          # OK
PYTHONPATH=src python -m coverage run -m unittest discover -s tests
Ran 537 tests in 7.130s
OK
PYTHONPATH=src python -m coverage report                  # TOTAL 6640 1269 81%
```

## Deployment gate

1. Ship with `ENTRY_STRETCH_GATE_MODE=shadow`. Nothing about live trading changes; the
   verdicts accumulate in Supabase.
2. After a few days, read what enforcement would have cost:
   ```sql
   select payload_json->'gate'->>'verdict' as verdict,
          payload_json->'gate'->>'bucket'  as bucket,
          count(*)
   from ai_decisions
   where action <> 'HOLD' and payload_json->'gate' is not null
   group by 1, 2 order by 3 desc;
   ```
3. Only then switch to `enforce`, and set
   `ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR` from the measured frontier (1.4–2.0)
   so the volume mandate still wins on a day that falls behind.
4. Restart only with the position flat (`104103` crash loop otherwise).
