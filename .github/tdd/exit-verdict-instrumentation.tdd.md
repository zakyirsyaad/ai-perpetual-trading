# Exit Verdict & Pacing Instrumentation — TDD Evidence

## User journey

Two questions cannot be answered from the data the bot currently keeps:

1. **How often does the engine overrule the model's exit verdict, and at what cost?**
   `_monitor_position` honours `close_now` only when `gross >= fee` or the loss has
   breached `AI_EXIT_ADVERSE_CUT_USDT`. Everything else is dropped at DEBUG level.
2. **How far behind the daily volume target was each entry taken?**
   The model is told to "open a directional trade every tick" and is never shown
   intraday volume progress, so pacing cannot even be evaluated after the fact.

This change records both. It is instrumentation only — no trading behaviour changes,
and the payload the model receives stays byte-identical.

## Evidence that motivated the change

Measured 2026-09-11 on 203 BTC trades matched to the `ai_decisions` row that opened
them (fixed entries, 1m-candle exit simulation reproducing 94% of live deterministic
exit reasons):

- Handing stop/target back to the model changes almost nothing: gross/1M live −384,
  model's own SL/TP −408, with the neutral band −345. Net stays −740…−910 per 1M.
  Exit *geometry* is not the lever, so the open question is exit *timing* — which is
  exactly what is not recorded.
- `ai_decisions` holds entry decisions only. `save_learning_note` is a no-op (there is
  no `learning_notes` table). The pm2 error log records the exit trigger, never the
  verdict. So the `gross >= fee` gate has never been evaluated against outcomes.
- BTC sat near its mean 40% of the time, yet the model entered on 6.3% of near-mean
  ticks against 10.8% of stretched ticks. Any pacing prompt has to be validated
  against history first, and that history does not exist yet.

## RED evidence

`tests.test_trade_engine_execution.AIExitVerdictRecordingTests` — the signal carried no
provenance, so the engine could not tell a model verdict from the policy's fallback:

```
  File "tests/test_trade_engine_execution.py", line 1370, in exit_signal
    return ExitSignal(
TypeError: ExitSignal.__init__() got an unexpected keyword argument 'ai_called'
```

`tests.test_supabase_trade_store.StoreTests.test_exit_verdict_is_persisted_with_the_exchange`
— no store method, and no table able to hold a verdict (`ai_decisions.systemic_risk` is a
NOT NULL 1..5 check an exit decision cannot fill):

```
    await _store(http).save_exit_verdict(
AttributeError: 'SupabaseTradeStore' object has no attribute 'save_exit_verdict'
```

`tests.test_ai_orchestration.EntryPacingRecordTests` — nothing recorded volume pacing:

```
    pacing = store.decisions[-1]["payload"]["pacing"]
KeyError: 'pacing'
```

## GREEN implementation

- `strategy/base.py` — `ExitSignal` gains `ai_called` and `confidence`. The exit policy's
  deterministic fallbacks leave `ai_called` false, so only real verdicts are recorded and
  the ~1-per-5s gated holds never reach storage.
- `ai/orchestration.py` — `exit_signal` stamps model verdicts. A verdict rejected by the
  confidence gate is now labelled `hold_low_confidence` instead of being indistinguishable
  from the fallback `hold`.
- `core/trade_engine.py` — the honour/overrule decision is computed once, recorded through
  `_record_exit_verdict` (symbol, direction, close_now, reason, confidence, honored,
  skip_reason, elapsed, entry/current price, gross, fee, net after fee), and only then
  acted on. Honouring rules are unchanged. Storage failures are logged and swallowed:
  instrumentation must never break the loop protecting an open position.
- `storage/supabase_trade_store.py` + `storage/interfaces.py` + `supabase/schema.sql` —
  new `ai_exit_verdicts` table, stamped with the exchange like every other write.
- `ai/orchestration.py` — `_pacing_state` snapshots volume today against
  `volume_profile.daily_volume_usdt`, completion %, hours left in the **trading-timezone**
  day (the same day boundary the volume accounting uses), trades today and day PnL. It is
  stored under `ai_decisions.payload_json.pacing` and logged at INFO. It is deliberately
  **not** part of the model request; a test asserts the request never gains the key.

## Verification

```
PYTHONPATH=src python -m compileall -q src tests          # OK
PYTHONPATH=src python -m coverage run -m unittest discover -s tests
Ran 532 tests in 5.247s
OK
PYTHONPATH=src python -m coverage report                  # TOTAL 6588 1265 81%
```

Secret hygiene: `tests/test_secret_hygiene.py` runs inside that suite. No credentials were
read or written; the new env key is a table name (`SUPABASE_AI_EXIT_VERDICTS_TABLE`).

## Deployment gate

1. Apply the `ai_exit_verdicts` block from `supabase/schema.sql` in the Supabase SQL editor
   **before** deploying. Without the table every insert fails; trading is unaffected (the
   failure is caught and logged) but no verdict is recorded.
2. Flip the position flat before restarting — `set_margin_mode` fails with `104103` while a
   position is open and pm2 turns that into a crash loop.
3. After the first trades, confirm rows exist:
   `select honored, count(*) from ai_exit_verdicts group by 1;`
