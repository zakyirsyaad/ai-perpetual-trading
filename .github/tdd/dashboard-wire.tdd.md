# Dashboard Wire Contract and Bus — TDD Evidence

## User journey

The operator wants a live dashboard for the BingX bot, adapted from the dashboard in aowang-ai/jev-trade (MIT). That dashboard is a 2,700-line Next.js app written against one fixed JSON shape: `snapshot`, `block`, `quote`, `fill` and `price` events plus a candle tape. Rather than rewrite it, this bot emits the same shape.

This change lands only the foundation: a whitelisted wire contract and an in-memory fan-out bus. Nothing is wired into `TradeEngine`, no server exists and no dependency was added, so the live trading path is untouched.

Constraints taken from CLAUDE.md:

- Telemetry is observability, never a safety path. `publish` must not raise or block whatever the message or subscriber does.
- No secret, wallet or account address may reach the stream. Every model forbids unknown fields, so a payload carries only what is declared.
- One open position total is structural, so the contract is used with a single symbol per event.

## Design decisions

- **Same wire format as jev-trade.** The contract tests round-trip real samples (`tests/fixtures/jev_wire_samples.json`: a hold, an open with a quote, a fill, and the meta object) captured from a running jev-trade bot. The Python models must reproduce them exactly after JSON parsing.
- **Optional versus nullable keys.** TypeScript `x?:` keys are omitted when unset. Required-but-nullable keys (`txHash`, `orderId`, `accountValue`, `quote`, `fill`) are always emitted as explicit `null`, as the dashboard expects.
- **Honest confidence.** The upstream `probabilities` come from a calibrated model. This bot's LLM reports a self-assessed confidence, so it travels in its own optional `confidence` field (plus `reason`) instead of being dressed up as a probability.
- **Dashboard key names kept.** `ai_cost_usd`, `fee_usd` and `fee_sz` dump as `jevUsd`, `gasUsd` and `gasSz`, so the dashboard needs no change for them.
- **Access.** The server will bind to 127.0.0.1 and be reached through an SSH tunnel. Cloudflare Quick Tunnels were considered and rejected: their documentation states they do not support Server-Sent Events and are for testing only.

## RED evidence

The tests were written before the package existed:

```text
ModuleNotFoundError: No module named 'bot.telemetry'
Ran 2 tests in 0.000s
FAILED (errors=2)
```

## GREEN implementation

- `src/bot/telemetry/wire.py`: pydantic models (`Meta`, `BlockEvent`, `QuoteMessage`, `FillMessage`, `PriceMessage`, `Decision`, `Quote`, `FillData`, `Position`, `Totals`, `Resting`) with `extra="forbid"`, `frozen=True`, camelCase aliases and an omit-when-None serializer. `to_wire` and `sse_frame` produce the dashboard's payloads.
- `src/bot/telemetry/bus.py`: `TelemetryBus` with per-subscriber bounded queues that drop their oldest item when slow. A broken subscriber is logged and skipped, a foreign object is dropped, and any unexpected failure is logged and never reaches the caller.

## Verification

```text
Telemetry tests: 26 passed
Full test suite: 615 passed
Line coverage: 82% (required minimum: 80%); telemetry package 100%
compileall: passed
git diff --check: passed
secret hygiene test: passed (part of the full suite)
pip check: not run, `pip` is not available in this interpreter; no dependency was added
```

## Commit trail

Branch `feat/dashboard-wire`, commit `feat: add dashboard wire contract and telemetry bus`.

## Deployment gate

None for this change: no module imports the telemetry package yet, so a deploy is behaviourally identical.

The gate belongs to the next change (SSE server and engine hooks): the position must be flat before any redeploy (see the restart crash-loop note in CLAUDE.md), `TELEMETRY_ENABLED` stays `false` on the VPS until the SSH tunnel is in place, and the server binds to 127.0.0.1 only.
