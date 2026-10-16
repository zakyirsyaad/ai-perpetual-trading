# Dashboard Display Symbols — TDD Evidence

## User journey

The live bot trades `SYMBOLS=BTC/USDT` only, and the dashboard shows only BTC because the telemetry runtime builds its sleeves, `MarketDataPoller` and `BookTickerFeed` from `settings.symbols`. The operator wants to watch ETH as well. Adding ETH to `SYMBOLS` would make the AI trade it, which is not allowed (ETH failed the research gate). The dashboard therefore needs its own display-only symbol list that nothing on the trading path reads.

## RED evidence

`tests/test_dashboard_display_symbols.py` was written first and run against `origin/main` 9b189cf:

```text
ERROR: test_a_traded_symbol_never_disappears_from_the_dashboard
AttributeError: 'Settings' object has no attribute 'telemetry_display_symbols'
ERROR: test_entries_are_parsed_like_symbols
AttributeError: 'Settings' object has no attribute 'telemetry_symbols'
ERROR: test_an_ai_hold_is_shown_only_on_traded_symbols            (hub has no ETH sleeve)
FAIL: test_the_meta_lists_a_sleeve_for_every_display_symbol_with_the_traded_one_first
AssertionError: Lists differ: [('BTC', 'BTC-USDT')] != [('BTC', 'BTC-USDT'), ('ETH', 'ETH-USDT')]
FAIL: test_the_poller_reads_prices_and_candles_for_the_display_symbol
AssertionError: Lists differ: ['BTC/USDT'] != ['BTC/USDT', 'ETH/USDT']
FAIL: test_the_price_stream_subscribes_to_the_display_pairs
AssertionError: Lists differ: ['BTC-USDT'] != ['BTC-USDT', 'ETH-USDT']
Ran 15 tests
FAILED (failures=4, errors=6)
```

The five other tests passed on RED: `test_display_symbols_never_change_the_traded_symbols` and the four trading-path tests (orchestrator rejects ETH as unsupported, `MarketIntelligence` builds context for BTC only, `VolumeStrategy` rotates BTC only, no trading module reads the display list). They are regression guards: they pin today's behaviour so a later change cannot route the display list into trading.

## GREEN implementation

- `TELEMETRY_SYMBOLS` (`Settings.telemetry_symbols`), default empty, parsed by the same `parse_symbols` validator as `SYMBOLS` (comma list, trimmed, upper-cased, `-` to `/`, blanks dropped).
- `Settings.telemetry_display_symbols` = `SYMBOLS` followed by `TELEMETRY_SYMBOLS`, de-duplicated and order-preserving. An unchanged `.env` yields exactly `SYMBOLS`, and a traded symbol can never drop off the dashboard.
- `telemetry/runtime.py`: `build_meta` sleeves, `MarketDataPoller` and `BookTickerFeed` use the display list. The first traded symbol stays the default market.
- `EngineTelemetry` keeps `settings.symbols`. A display-only coin gets price and candles but never an AI hold block. Two reasons: the model never looked at it, and the dashboard's header sums `totals.realizedUsd` over every coin's latest block, so a block on ETH would double the realized figure.
- Engine, orchestrator, market intelligence, risk and strategy are unchanged and keep reading `settings.symbols`. A structural test fails if any module outside `bot/telemetry` and `config.py` (or `main.py`) mentions the display list.
- Dashboard (`dashboard/src`) unchanged: it already renders one card per `meta.sleeves` entry, and a sleeve with no blocks gets a flat stub from its price marks (`useFeed.paintLatest` → `stubLatest`). `DecisionPanel`, `Feed`, `Book` and `lastMeaningfulCall` are null-safe for an empty sleeve. Wire shape untouched.
- Docs: `.env.example` (`TELEMETRY_SYMBOLS=`), CLAUDE.md "Live dashboard feed" bullet, README "Show a symbol the bot does not trade".

## Verification

```text
Focused tests (display symbols + telemetry runtime + telemetry config): 43 passed
Full test suite: 1544 passed
Line coverage: 89% (required minimum: 80%); runtime.py 99%, config.py 94%
compileall: passed
git diff --check: passed
tests/test_dashboard_contract.py: passed (wire unchanged)
secret hygiene test: passed
pip check: not run (the local interpreter has no pip module)
Dashboard type check: not run (no TypeScript changed; dashboard/node_modules absent)
```

## Commit trail

- `9ff1bcc` — `test: pin display-only dashboard symbols (TELEMETRY_SYMBOLS)`
- `2ace74c` — `feat: display-only dashboard symbols (TELEMETRY_SYMBOLS)`

## Deployment gate

Requires an env change on the VPS (`TELEMETRY_SYMBOLS=BTC/USDT,ETH/USDT`, with `SYMBOLS=BTC/USDT` unchanged) and a pm2 restart, which is only allowed with a flat position (see CLAUDE.md "Never restart with an open position"). After restart, confirm `curl -s http://127.0.0.1:<TELEMETRY_PORT>/` lists both sleeves and that the next entry log still offers the AI only BTC. The extra symbol adds one public order-book read per poll interval and two kline reads per minute on the shared client.
