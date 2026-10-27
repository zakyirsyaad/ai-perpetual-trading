// Checks `headerPnl`, the header's choice between the newest block and the bot's `/pnl` counters.
// Run with bun: `bun dashboard/scripts/header_pnl.check.mjs` (tests/test_dashboard_header_pnl.py does it when bun is installed).
// The dashboard has no JS test runner; this is a plain script that exits non-zero on the first failed assertion.
import assert from "node:assert/strict";
import { headerPnl } from "../src/lib/pnl.ts";

const totals = (realizedUsd) => ({
  blocks: 0, decisions: 0, quotes: 0, fills: 0, reverted: 0, lateBlocks: 0,
  jevUsd: 0, gasSz: 0, gasUsd: 0, realizedUsd, pnlUsd: realizedUsd, pnlSz: 0, pnlPct: 0,
});

const block = (number, realizedUsd, unrealizedUsd = 0) => ({
  coin: "BTC", block: number, ts: 1, mid: 67000, bestBid: 66999, bestAsk: 67001, spreadBps: 0.3,
  decision: null, quote: null, fill: null, resting: { bidSz: 0, askSz: 0 },
  position: {
    side: unrealizedUsd ? "long" : "flat", size: unrealizedUsd ? 1 : 0, entryPrice: null, leverage: null,
    unrealizedUsd, unrealizedSz: 0,
  },
  totals: totals(realizedUsd),
});

// What useFeed paints from a price mark alone when a sleeve has no block yet (`stubLatest`): block 0, flat, totals 0.
const stub = block(0, 0);
const summary = { realizedUsd: -190.67, unrealizedUsd: 0 };

// Nothing known yet: the header stays a skeleton.
assert.equal(headerPnl({ BTC: null }, null), null);
// No block: the bot's own figures from /pnl stand in.
assert.deepEqual(headerPnl({ BTC: null }, summary), { unrealized: 0, realized: -190.67 });
// A price-only stub is not a block. It must not hide /pnl, and it must not make the header read zero.
assert.deepEqual(headerPnl({ BTC: stub }, summary), { unrealized: 0, realized: -190.67 });
assert.equal(headerPnl({ BTC: stub }, null), null);
// A block the bot published wins: it carries the live mark of an open position. The bot's first block is number 1,
// so the boundary is pinned with block 1, not with a later one.
assert.deepEqual(headerPnl({ BTC: block(1, -150, 12.5) }, summary), { unrealized: 12.5, realized: -150 });
// Display-only coins carry a stub; a real block on another coin still counts.
assert.deepEqual(headerPnl({ BTC: block(3, -150), ETH: stub }, summary), { unrealized: 0, realized: -150 });

console.log("headerPnl: ok");
