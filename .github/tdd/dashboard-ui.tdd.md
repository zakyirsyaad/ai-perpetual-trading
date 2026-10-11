# Dashboard UI — TDD Evidence

## User journey

The operator wants to watch the BingX bot live. The bot already publishes the feed (wire contract, state, read-only server, poller, engine hooks). This change adds the screen: the dashboard from aowang-ai/jev-trade (MIT, itself based on jev-trader by Jarrod Watts, MIT), copied into `dashboard/` and adapted to this bot.

It runs on the operator's laptop and reads the bot through an SSH tunnel; nothing is opened to the internet.

## Design decisions

- **Same repo, own directory.** `dashboard/` is a self-contained Bun and Next.js app with its own lockfile. Nothing in `src/` imports it.
- **The bot's contract is the source of truth.** The dashboard keeps the upstream wire shape, so the bot emits it rather than the screen being rewritten. A new test keeps the two sides from drifting.
- **Honest labels.** The upstream panel drew five bars of per-action probabilities. This bot has no such distribution (the wire carries a one-hot value), so the panel now shows the model's `confidence` as one bar plus the `reason` text, and a close shows its exit reason.
- **Nothing public.** OpenGraph, JSON-LD, sitemap, `llms.txt` and the indexable robots rule were removed; the page is `noindex` and `robots.txt` disallows everything.
- **No third-party art or links.** The two token PNGs (DOGE, BNB), the Hyperliquid explorer fallback, the account-balance score (the bot publishes none) and the venue and brand strings were removed or replaced.
- **License kept.** `dashboard/THIRD_PARTY_NOTICES.md` reproduces the MIT notices of both upstream projects, taken from their repositories, and lists what changed.

## RED evidence

`tests/test_dashboard_contract.py` parses the TypeScript interfaces in `dashboard/src/lib/bot-types.ts` and compares their keys, nested keys and optionality with the pydantic wire models. Against the copied upstream types it failed on exactly the two keys the bot adds:

```text
AssertionError: Items in the second set but not the first:
'reason'
'confidence' : Decision: TypeScript vs Decision
Ran 5 tests ... FAILED (failures=1, errors=1)
```

Every other interface (`BlockEvent`, `Quote`, `Fill`, `Position`, `Totals`, `Meta`, `SleeveMeta`, `PricePoint` and the nested `resting`, `probabilities` and tape `fill`) already matched the Python models, which is independent confirmation that the bot emits the upstream shape. The first version of the parser did not handle a one-line inline object (`resting`); that was fixed in the test.

## GREEN implementation

- `bot-types.ts`: `Decision.confidence?` and `Decision.reason?`; header comment now points at `wire.py` and the contract test.
- `DecisionPanel`: confidence bar and reason text; hold and close read correctly.
- `Header`: balance score removed; brand and tagline updated. `page.tsx` and `pnl.ts` drop `portfolioBalance`.
- `Feed`: a close no longer shows a `0ms` latency.
- `layout.tsx`, `robots.ts`: noindex, no public metadata. `sitemap.ts`, `site.ts`, `public/` removed.
- `format.ts`, `useFeed.ts`, `FlowChart.tsx`: no Hyperliquid fallback, defaults are `bingx` and `BTC-USDT`.
- `package.json`, `.env.example`, `next.config.ts`: renamed, tunnel instructions, repo-root comment.

## Verification

```text
Contract tests: 5 passed
Full test suite: 767 passed (762 before this change)
Line coverage: 84% (required minimum: 80%)
compileall: passed
git diff --check: passed
secret hygiene test: passed with dashboard/ staged (bun.lock included)
bun install --frozen-lockfile: passed, 30 packages
tsc --noEmit: clean
next build: succeeded (/, /icon.svg, /robots.txt)
pip check: not run, `pip` is not available in this interpreter
```

Live check in the browser pane: the real feed server (BingX VST public prices, read only, plus a simulated trade cycle through the observer, no orders) on port 3010 and the dashboard dev server on port 3011. The page showed Live, the real BTC and ETH prices, per-symbol tabs, the chart with open and close marks, CALL with `OPEN LONG 100x`, 82% confidence and the reason text, a CALLS list of OPEN, HOLD and CLOSE rows, a Positions table with unrealized PnL and ROE, and realized PnL accumulating across cycles. The browser console had no errors, and a close read `CLOSE LONG`, `take_profit`. Test processes were stopped afterwards.

Not covered: CI does not build the dashboard (the workflow runs Python only), and there are no component tests; the UI is verified by type check, build and the live run above.

## Commit trail

Branch `feat/dashboard-wiring`. Committed as `feat: add the live dashboard adapted from jev-trade`.

## Deployment gate

- The dashboard is never deployed to the VPS. Run it on the laptop: `ssh -L 3000:localhost:3000 0xAlvary2`, then `cd dashboard && bun install && bun run dev` and open `http://localhost:3001`.
- The bot side stays off until `TELEMETRY_ENABLED=true` is set on the VPS, which needs a restart with a flat position (see CLAUDE.md).
- `TELEMETRY_ALLOWED_ORIGIN` on the bot must name the origin the dashboard is opened from (default `http://localhost:3001`).
