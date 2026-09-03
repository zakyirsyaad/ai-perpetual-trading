# Empty External Provider Prune — TDD Evidence

## User journey

Stop the entry prompt from penalising confidence for an external provider that
is simply not configured or is failing, without weakening the payload parity
restored in `d96d13e`.

## Evidence that motivated the change

Live VPS log, every tick:

```text
TradingView signal fetch failed: All connection attempts failed
Dune built-in signal failed for eth_dex_volume_regime: Client error '400 Bad Request'
Dune built-in signal failed for stablecoin_liquidity_flow: Client error '429 Too Many Requests'
Dune built-in signal failed for large_eth_transfer_activity: Client error '400 Bad Request'
```

All three entries in `BUILTIN_DUNE_SIGNAL_SQL` fail, so Dune produces no data at
all. The two failure modes differ in shape, and only one of them was harmless:

- `TradingViewSignalProvider.fetch()` returns `{}` — falsy.
- `DuneProvider.fetch()` returns `{"saved_queries": {}, "builtin_signals": {}}` —
  **truthy**, because the skeleton is built before the sub-requests run.

`CompactDecisionContextBuilder._compact_external_signals` would have dropped both
(`if not payload: continue`), but it is dead code: nothing calls it, and nothing
calls `_summarize_provider` except it. `d96d13e` ("restore safe BingX AI trading
parity") routed the payload through `_deduplicated_market_context`, which
deep-copies `external_signals` verbatim. Both providers therefore reached the
model as present-but-empty, which the entry prompt penalises under "reduce
confidence when a provider is empty".

Live decision mix over the same window: 658 HOLD against 342 OPEN_* (65.8% HOLD).

## RED evidence

`tests.test_compact_context.EmptyExternalProviderTests` — 4 of 8 failed:

```text
FAIL: test_unreachable_tradingview_empty_dict_is_dropped
AssertionError: 'tradingview' unexpectedly found in {'tradingview': {}}

FAIL: test_dune_skeleton_with_no_results_is_dropped
FAIL: test_deeply_nested_empty_containers_are_dropped
FAIL: test_non_provider_metadata_is_never_pruned
```

The four that passed in RED pin the behaviour that must **not** change: a
provider with data keeps byte-for-byte parity, a partially populated provider is
kept, `0`/`False` count as data rather than emptiness, and the caller's context
is never mutated.

## GREEN implementation

- Added `_has_signal_content()`: containers are transparent (only what they hold
  counts, so a skeleton of empty dicts reads as no content) while scalars count,
  including `0` and `False`, which are meaningful readings.
- `_deduplicated_market_context` now drops any of `coinmarketcap`/`dune`/
  `tradingview` with no content. It operates on the existing `deepcopy`, so the
  caller's context is untouched, and it only inspects those three keys, so
  non-provider metadata such as `collected_at` is never pruned.

Deliberately **not** done: wiring `_compact_external_signals` back in. That would
undo the parity decision made in `d96d13e` and change what the model sees for
providers that do have data. The dead code is left in place and now documented as
dead in `CLAUDE.md`; reviving it is a separate decision.

## Verification

```text
Focused (EmptyExternalProviderTests): 8 passed
Module (test_compact_context, incl. the pre-existing parity tests): 12 passed
Full test suite: 474 passed (was 466)
Line coverage: 80% (required minimum: 80%), coverage report exit 0
compileall: passed
git diff --check: passed
secret hygiene test: passed
```

## Follow-ups not in this change

- The two Dune `400 Bad Request` queries are malformed SQL and remain broken;
  the prune only stops them from harming the payload. Clearing `DUNE_API_KEY`
  additionally stops the wasted requests (`DUNE_USE_BUILTIN_SIGNALS=false` does
  not — it still returns the truthy skeleton).
- TradingView cannot work as configured: `TRADINGVIEW_WEBHOOK_URL` points at
  `127.0.0.1`, unreachable from TradingView's servers, and `bot_data/` has never
  existed on the VPS.
- Entry calls cost ~21k tokens against a documented ~4k budget, because the
  external-provider projection is dead. Reducing that is separate work.
