# Reconciliation Supervisor — TDD Evidence

## User journey

Stop a correct safety halt from turning into hours of silent downtime, without
weakening the halt itself.

## Evidence that motivated the change

On 2026-09-07 an entry order returned an ambiguous outcome:

```
03:09:39 UTC  Execution guard passed for BTC/USDT: spread=0.36bps depth_bid=3,409,885 ...
03:09:39 UTC  Opening BTC/USDT SHORT | margin=180.00 | leverage=100x | notional=18000.00
03:09:42 UTC  Entries blocked: Entry order aptc7c179895cba4fa0a38238e4 for BTC/USDT
              could not be confirmed accepted or rejected; manual reconciliation
              is required before restart.
```

`TradeEngine` behaved correctly: retrying an order POST can duplicate a position,
so it latched entries and waited for a human. But nothing tells anyone. The bot
sat idle for **1h21m across 28 ticks** until it happened to be checked.

Manual reconciliation took three read-only calls and found no exposure at all:

```
position BTC/USDT : size=0.0  side=None
lookup clientOrderId aptc7c179895cba4fa0a38238e4 -> ('absent', None)
closed trades after 02:56 UTC : none
```

The order never became a position. The 81 minutes bought nothing.

## RED evidence

`tests.test_supervisor` — module did not exist:

```text
ModuleNotFoundError: No module named 'supervisor'
```

## GREEN implementation

`src/supervisor.py`, run from cron. Two pure functions carry the safety logic and
are exhaustively tested; the IO around them is deliberately thin.

`parse_block` finds the most recent latch and classifies it. The *latest* line
wins so a stale uncertain-order halt can never mask a later, more serious
unmatched-exposure halt.

`decide` clears the latch in exactly one situation — an ambiguous order that
provably produced nothing — and holds in every other:

| Situation | Action |
|---|---|
| not blocked | none |
| **uncertain order + position 0 + lookup `absent`** | **restart** |
| uncertain order + position > 0 | hold |
| uncertain order + lookup `found` or `unknown` | hold |
| position unreadable | hold |
| `Unmatched exchange exposure` (any state) | hold |
| same order id already recovered once | hold |

Two of those deserve their own note:

- **Unmatched exposure is never cleared.** That halt means a real position the
  bot could not model; no amount of reconciliation makes it safe to resume.
- **An unreadable position is not "flat".** A failed read returns `None`, which
  holds — the opposite of the default a bare `except` would have produced.
- **The same order is recovered at most once.** A repeat halt on one order means
  the latch was not the underlying problem, and auto-restarting would hide it.

The supervisor never places, cancels, or closes an order. Its only side effect is
`pm2 restart`.

Validated against the real incident's log lines before merge:

```text
parsed : BlockedEntry(kind='uncertain_order', order_id='aptc7c179895cba4fa0a38238e4', symbol='BTC/USDT')
flat+absent -> Decision(action='restart', ...)
position 0.23 -> Decision(action='hold', reason='exchange still reports 0.23 exposure on BTC/USDT')
```

## Verification

```text
Focused (tests.test_supervisor): 21 passed
Full test suite: 524 passed (was 503)
Line coverage: 81% (required minimum: 80%), coverage report exit 0
compileall: passed
git diff --check: passed
secret hygiene test: passed
```

`src/supervisor.py` sits at 76%; the uncovered lines are `_reconcile` (needs a
live venue) and the `pm2 restart` branch (needs pm2). An earlier draft left total
coverage at exactly 80% — on the gate — so tests were added for the state file
and for `main()`'s no-exchange paths rather than shipping that close to the edge.

## Deployment

```bash
*/5 * * * * cd /home/ubuntu/ai-perpetual-trading && PYTHONPATH=src \
    .venv/bin/python src/supervisor.py >> logs/supervisor.log 2>&1
```

`--dry-run` decides and reports without restarting; use it to confirm behaviour
before arming the cron.

## Not solved by this

The supervisor recovers from the latch; it does not stop orders from returning an
ambiguous outcome in the first place. That root cause is still unknown — the
entry POST failed 3 seconds after being sent, which is too fast for the configured
request timeout.
