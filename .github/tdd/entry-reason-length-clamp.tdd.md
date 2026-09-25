# Clamp the entry decision's free text instead of failing the batch

## User journey

The operator switched the live model from `mimo-v2.5` to `mimo-v2.6-pro` on 2026-09-25 (11:16 WIB) and asked why the bot kept spending a second provider call on almost every decision. `ai_call_metrics` showed **4 of the first 5 entry calls at `attempts=2`**, on a token plan that had just been exhausted for 5.6 days — every repair doubles tokens and latency for the same decision.

The cause was not the token cap. Tokens in `ai_call_metrics` accumulate across attempts, so the 3,652 output tokens of a two-attempt call are ~1,800 per attempt, far below the 4,096 cap. The log named the real failure:

```
2026-09-25 12:21:33 | WARNING | volume_bot | Invalid AI provider structured response on attempt 1:
ValidationError: 1 validation error for EntryDecisionBatch
decisions.0.reason
  String should have at most 600 characters [type=string_too_long, input_value="Trend factors all LONG b... only CMC context used.", input_type=str]
```

`mimo-v2.6-pro` writes longer prose than `v2.5`, and **the prompt never stated the 600-character limit**, so the model could not respect it. `RiskReview.confirmations`/`invalidation_trigger` and `ExitDecision.reason` already clamp verbose text instead of raising — with the comment that a verbose but otherwise valid response must not be discarded on a length technicality — and the entry decision's own free text was the one place left unguarded.

## RED

Constants added first, no behaviour change, so the tests fail for the real reason:

```
$ PYTHONPATH=src python -m unittest tests.test_ai_brain_contract.EntryTextLengthClampTests tests.test_compact_context.OutputRulesDeclareTextLimitsTests
ERROR: test_overlong_reason_is_clamped_not_rejected
pydantic_core._pydantic_core.ValidationError: 1 validation error for EntryDecisionBatch
decisions.0.reason
  String should have at most 600 characters [type=string_too_long, input_value='RRRR...RRRR', input_type=str]
ERROR: test_overlong_learning_note_is_clamped
  String should have at most 500 characters [type=string_too_long, ...]
ERROR: test_entry_output_rules_state_the_free_text_limits
KeyError: 'reason_max_characters'
Ran 4 tests — FAILED (errors=3)
```

`test_structure_stays_strict` passed from the start: it guards that the fix does not loosen structure.

## GREEN

- `ai/compact_context.py` owns the contract numbers — `ENTRY_REASON_MAX_CHARS = 600`, `ENTRY_LEARNING_NOTE_MAX_CHARS = 500` — because this module builds the prompt that states them. `ai/brain.py` imports them for the `Field` limits and the validator, so prompt and validator cannot drift. The direction matters: `brain` already imports `compact_context`, never the reverse.
- `EntryDecision._clamp_free_text` (`mode="before"`, on `reason` and `learning_note`) trims over-long strings, mirroring `RiskReview._clamp_length` and `ExitDecision._clamp_reason`. Non-string values pass through untouched, so type errors still raise.
- `output_rules` now carries `reason_max_characters` and `learning_note_max_characters`: the model is finally told the limit it has to meet.
- `COMPACT_PROMPT_VERSION` moves `okx-context-parity-v2` → `v3`. It is stamped on every `ai_call_metrics` row as `prompt_version`, so decisions made under the new contract are separable in analysis.

Structure stays strict on purpose: an empty `reason`, a missing `symbol`, a bad `action` or an out-of-range `confidence` still fail and still trigger the single repair request. Only free text is forgiven.

## Verification

| Check | Result |
|---|---|
| `python -m compileall -q src tests` | clean |
| `python -m unittest discover -s tests` | **571 tests, OK** |
| `python -m coverage report` | **81%** (gate 80%) |
| `git diff --check` | clean |
| `pip check` | not run — no `pip` module on this uv-managed interpreter; no dependency was added or changed |
| `tests.test_secret_hygiene` | OK |

New tests: `tests/test_ai_brain_contract.py::EntryTextLengthClampTests` (3), `tests/test_compact_context.py::OutputRulesDeclareTextLimitsTests` (1).

## Deployment gate

Config-free change; no `.env` key added. Deploy is `git pull` plus a restart, and the restart must follow the usual rule: **verify the venue position is flat first** (BingX rejects `set_margin_mode` while a position is open, which turns into a pm2 crash loop). Expected effect afterwards: `attempts=2` on entry calls becomes rare, so tokens and latency per decision roughly halve. Watch `ai_call_metrics` for `attempts` and `error_category='schema'`, and check that stored `reason` values are trimmed at 600 characters rather than missing.
