# Entry-Shape Memory and Factor Family — TDD Evidence

## User journey

Make the AI's trade memory describe which **entries** lose rather than which
exits fired, and record the factor-category mix of each entry so a hypothesis
about entry quality can be tested prospectively instead of retrospectively.

## Evidence that motivated the change

### The memory taught a tautology

`build_trade_decision_memory` derived `winning_patterns` / `losing_patterns`
from the **exit reason**. With live data that produced literally:

```
winning: neutral_exit   losing: stop_loss
```

An exit reason is a consequence, not a decision. This is why the same LONG at
78,293 / 78,309 / 78,301 could be repeated three times with the memory present
in every prompt.

### A hypothesis that did not survive testing

Comparing the four OKX days with positive net (26 trades, +378/1M) against the
rest suggested momentum/volume factors led the good entries and microstructure
factors led the bad ones. Arena factors already carry a `category`, and it is
persisted in `factor_metadata.snapshots`, so the hypothesis was tested across
all 363 closed trades rather than the 26 that were selected by outcome:

| venue | micro < 40% | micro ≥ 40% | verdict |
|---|---:|---:|---|
| OKX (n=281) | gross **+109**/1M | gross −260/1M | supports |
| BingX (n=81) | gross −604/1M | gross **+593**/1M (n=5) | contradicts |

The BingX contradicting bucket holds five trades, so the honest reading is not
"refuted" but "BingX has no sample to test it": 76 of 81 BingX entries were
micro < 40%. Worse, BingX's `momentum`-dominant bucket (n=11) returned gross
−1,410/1M at 0% win rate, while on OKX `momentum` was the *best* category
(+285/1M).

This was the eighth dimension tested — after symbol, direction, confidence,
hold time, hour, dominant category and microstructure share — and like the other
seven it **inverts between venues**. No measured axis holds its sign on both.

The axis is therefore *recorded*, not acted on. Building a gate on a
retrospective pattern this fragile would be curve-fitting.

## RED evidence

`tests.test_setup_profile.FactorFamilyTests` — 7 errors:

```text
TypeError: build_setup_profile() takes 2 positional arguments but 3 were given
```

`tests.test_trade_decision_memory.EntryShapeMemoryTests` — 3 failures, 1 error:

```text
AssertionError: 'LONG|stretch_up|atr_mid' not found in ['stop_loss']
KeyError: 'exit_performance'
KeyError: 'family_performance'
```

## GREEN implementation

- `build_setup_profile` gains an optional `factor_snapshots` argument and returns
  `micro_share`, `factor_family` (`micro_led` / `mixed` / `trend_led` /
  `unknown`) and `family_key`. The family is deliberately **not** folded into
  `setup_key`: a fourth axis would triple the bucket count from 18 to 54 and
  starve each one of sample. A test pins that separation.

- `build_trade_decision_memory` keys `winning_patterns` / `losing_patterns` on
  `factor_metadata.setup.setup_key`, and adds `family_performance` and
  `exit_performance`. Rows written before instrumentation produce no entry-shape
  patterns and an explicit lesson saying so, rather than being dressed up as
  entry evidence.

- `_lessons` now emits entry-shape lessons carrying their own numbers, so they
  stay accurate as buckets change, while the hand-written exit-hygiene advice
  keys on the exit reason, which is what it is actually about.

### One pre-existing test was intentionally rewritten

`test_summarizes_winning_and_losing_exit_patterns` asserted
`winning_patterns[0]["pattern"] == "neutral_exit"` — it pinned the tautology
being removed. Its contract was updated to assert that patterns are empty for
rows with no setup, and that exit reasons appear under `exit_performance`. This
is a deliberate contract change, not a test relaxed to fit the code.

## Verification

```text
Focused (setup_profile + SetupMetadataTests + trade_decision_memory): 22 passed
Full test suite: 500 passed (was 491)
Line coverage: 81% (required minimum: 80%), coverage report exit 0
compileall: passed
git diff --check: passed
secret hygiene test: passed
```

## Deliberately out of scope

- **No gate.** Nothing blocks an entry on family or setup. The one hypothesis
  worth gating on failed its cross-venue test.
- No backfill is possible for `setup_key`, but `factor_family` *could* be
  derived retroactively from stored snapshot categories if a future analysis
  needs it.

## Deployment gate

Recording plus prompt-content change; no execution behaviour changes. Flatten
any open position before restarting — BingX rejects `set_margin_mode` while a
position is open and the fail-closed verification then crash-loops.
