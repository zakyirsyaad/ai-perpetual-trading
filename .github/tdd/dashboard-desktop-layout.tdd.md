# TDD Evidence: dashboard desktop layout

## User journey

The operator opens the live dashboard on a laptop (1280-1440 px wide) or an external monitor. What they need without scrolling is the day's volume against the 1M target and the Positions / Trades / History tables; the chart and the AI calls must stay readable. On a 13-14" laptop the old desktop layout stacked four full-width bands (header, volume today + by day, MiMo tokens, coin cards) above the chart, so at 1280x720 the chart was 158 px tall and the CALLS list showed only its title bar. The page was locked to the viewport height, so nothing could scroll into view. Mobile must not change.

## Before (main, measured in the browser against the live feed)

1280x720, element top / height in px:

| Element | Top | Height | Width |
|---|---|---|---|
| Header | 0 | 51 | 1280 |
| Volume band | 51 | 156 | 1280 |
| MiMo token strip | 208 | 33 | 1280 |
| Coin cards (2 of 5 columns used) | 241 | 102 | 1280 |
| Chart | 342 | 158 | 900 |
| Latest call | 342 | 130 | 379 |
| Calls feed | 473 | 27 (title bar only) | 379 |
| Book | 500 | 220 | 1280 |

The chart also had a latent crash: `lightweight-charts` throws `Cannot update oldest data` when `CandlePane` calls `series.update()` with a newest bar older than the one already drawn (a rebuilt or trimmed tape with an unchanged first bar). Reproduced on main at 1024x768 on a fresh load; React 19 shows the error overlay, and an uncaught effect error can take the whole page down.

## Design (approved in chat, option A)

- Desktop (>= 900 px): header on top, then a two-column grid. The work column holds the coin tabs (`SleeveStrip variant="tabs"`, a two-line tab per coin), the chart (`minmax(240px, 1fr)`) and the Book (`minmax(200px, 40%)`, filling its row). The rail (360 px, 400 px from 1440, 440 px from 1800) holds `VolumePanel variant="rail"` (today, then the daily list), `UsagePanel variant="rail"` (total and toggle on one line, the detail trimmed to four columns), the latest call, and the calls feed (at least 180 px; the rail scrolls on its own if it runs out of room).
- A desktop window shorter than 680 px scrolls the page at 680 px instead of squeezing the panels.
- Mobile (< 900 px): the wrappers are `display: contents` and `order` restores the original stacked sequence (header, volume, tokens, coins, chart, call, feed, book). The variants only change styles inside `@media (min-width: 900px)`.
- No data or wire change: `bot-types.ts`, the telemetry feed and the bot are untouched.

## GREEN

- `src/app/page.tsx`: slots for header, work column (sleeves, chart, book) and rail (volume, usage, call, feed).
- `src/app/page.module.css`: the grid, rail widths, the book/feed fill rules (desktop only), and the mobile `display: contents` + `order` block.
- `src/app/globals.css`: the short-viewport scroll rule.
- `SleeveStrip`, `VolumePanel`, `UsagePanel`: an optional `variant` prop (default keeps the old look) whose styles live in a desktop media query.
- `FlowChart/CandlePane.tsx`: tracks the newest bar time drawn and redraws with `setData` when the next newest bar is older; the live bar still goes through `update()` on every tick.

A code review (react-reviewer) found no HIGH or MEDIUM issue; its three LOW notes were fixed before commit: the desktop tab row scrolls sideways when more coins (or the five-card loading skeleton) do not fit, the rail hides horizontal overflow so the 320 px volume skeleton never adds a sideways scrollbar, and `CandlePane` also redraws when more than one bar was appended since the last draw (a throttled background tab), since `update()` would skip the bars in between.

One regression was caught during the work and fixed before commit: the "book fills its row" rule first applied on mobile too and cut the Book from 260 px to 119 px; it is now scoped to the desktop media query.

## After (same feed, same viewport)

1280x720:

| Element | Top | Height | Width |
|---|---|---|---|
| Header | 0 | 51 | 1280 |
| Coin tabs | 51 | 55 | 920 |
| Chart | 107 | 346 | 920 |
| Book | 453 | 267 | 920 |
| Rail: volume | 51 | 290 | 359 |
| Rail: MiMo tokens | 342 | 56 | 359 |
| Rail: latest call | 398 | 130 | 359 |
| Rail: calls feed | 529 | 191 | 359 |

Chart 158 → 346 px, calls feed 27 → 191 px, book 220 → 267 px, everything above the chart 291 → 55 px. Also checked at 1024x768, 1440x900, 1920x1080 and 1280x600 (page scrolls to 680 px). Interactions checked: coin tabs (BTC/ETH switch the chart and the call panel), the token detail toggle inside the rail, and the Positions / Trades / History tabs with real trades.

Mobile 375x812, element top / height / width, main vs this branch: Header 0/76/375, Volume 76/295/375, Tokens 371/58/375, Coins 429/72/375, Chart 501/309/375, Call 810/90/375, Feed 901/248/375, Book 1149/260/375 — identical.

After the `CandlePane` guard, three fresh loads at 1024x768 showed no `Cannot update oldest data` error and no Next.js issue badge.

## Round 2 (code review of PR #38)

Findings and fixes:
- The `CandlePane` guard still allowed `update()` when the series shrank (`added <= 1` held for a negative count) or the coin/interval changed with the same first bar, so 1m -> 5m could leave 1m bars under one 5m bar. `update()` now needs the same coin and interval (`rangeKey`), the same first bar, a newest time that does not go back, and exactly zero or one new bar; everything else redraws with `setData`.
- After a multi-bar catch-up (a throttled background tab) the redraw did not keep the view on the live edge. It now calls `showLatest` when the view was at the last bar before the redraw (`atLiveEdge`), and leaves a scrolled-back view alone.
- Mobile reading and focus order followed the DOM (header, coins, chart, book, volume, ...) while CSS `order` painted another sequence. `useIsDesktop` (matchMedia via `useSyncExternalStore`, server snapshot = desktop) now renders the slots flat in the stacked order below 900 px; the `display: contents` + `order` rules stay as the paint for the prerendered desktop tree until hydration.
- The rail is 380 px below 1440 px again, the width `DecisionPanel` and `Feed` were laid out for.
- `Book` takes `variant="fill"` (styled in `Book.module.css`) instead of the page stylesheet overriding its root.
- The rail's token detail keeps the per-day calls and input/output split on a second line instead of hiding them.
- Variant names are uniform (default `stack`; `tabs`, `rail`, `fill`), and a `lib/cx.ts` helper joins the class names.
- `pip check` is now run and recorded.

Verified after round 2: 1280x720 chart 346 px, calls feed 191 px, book 267 px, rail 380 px; mobile 375x812 DOM order header, volume, tokens, coins, chart, call, feed, book, with focus order Detail, BTC, ETH, chart intervals, Positions/Trades/History, and every panel at the same top/height/width as main; interval switches 5m -> 1m -> 15m -> 5m redraw cleanly with no console error and no hydration warning.

## Round 3 (second code review of PR #38)

Findings and fixes:
- Switching trees remounted every panel: on a phone the prerendered desktop tree was mounted and then replaced, creating the chart twice, and crossing 900 px reset the Book tab, the all-markets toggle and the chart interval. `useIsDesktop` now reports `null` while prerendering and hydrating, so the page renders only its header until the browser reports its width and each panel mounts once, in the right tree (the prerendered HTML now holds only the header). The `display: contents` + `order` fallback that painted the desktop tree in stacked order is gone with it. `lib/useSessionState` keeps the Book tab, the all-markets toggle and the chart interval across a remount; FlowChart resets the interval only when the coin actually changes, not on every mount. (`feed.loadTape` was already guarded by its idle/loading/done status, so the second mount never sent a second request.)
- After the tape reached `TAPE_CAP`, every new candle changed the first bar and snapped the view to the right edge. A redraw now follows the live edge only for a new coin or interval or when the view was already on the last bar (`atLiveEdge`); a view scrolled back to an earlier entry stays put.
- The Feed is sized by its own `variant="fill"`, like Book; the page stylesheet no longer reaches into it.
- `useIsDesktop` reuses one module-level `MediaQueryList` instead of creating one per snapshot.
- `CandlePane` converts only the newest candle on the `update()` path; the full `toBars` runs only on a redraw. The always-true `Boolean(stem)` check is gone.

Verified after round 3: with Trades selected, the token detail open and the chart on 15m, resizing 1280 -> 375 -> 1280 keeps all three; switching BTC -> ETH still resets the interval to 5m. Desktop 1280x720 and mobile 375x812 panel geometry unchanged from round 2 (the token detail open adds its own height). No hydration warning or chart error in the console.

## Verification

- `bunx tsc --noEmit -p .` (dashboard): clean.
- `bun run build` (dashboard): compiled, 4 static routes.
- `PYTHONPATH=src python -m unittest tests.test_dashboard_contract tests.test_dashboard_display_symbols tests.test_secret_hygiene`: 21 tests OK (wire contract unchanged).
- `PYTHONPATH=src python -m compileall -q src tests`: clean. `git diff --check`: clean.
- Full Python suite under coverage: 1,576 tests OK, 89% total line coverage (no Python file changed).
- `pip check`: `uv pip check --python .venv/bin/python` reports all installed packages compatible (no dependency changed in this PR).

## Commit trail

- 4c0d5b5 `feat: give the dashboard a two-column desktop layout`
- 9519f2f `fix: redraw the candle series instead of updating an older bar`
- 779f123 `docs: TDD evidence for the dashboard desktop layout`
- 1edac7d `fix: address PR #38 review on chart redraws, mobile order and the rail` (round 2)
- 926d296 `docs: round 2 of the dashboard layout TDD evidence`
- c34a28d `fix: mount dashboard panels once and keep their state across the breakpoint` (round 3)
- the docs-only commit that records the round-3 hash makes no code change

## Deployment gate

Dashboard-only change. The screen runs on the operator's laptop (`bun run dev` / `bun run build && bun run start` in `dashboard/`, reading the feed through `ssh -L 3000:localhost:3917`). The trading bot `ai-perp-volume` is not touched and needs no restart. If the VPS pm2 app `ai-perp-dashboard` serves this screen, updating it is a separate step that needs the user's approval; it never restarts the bot.
