import type { BlockEvent, PnlSummary } from "./bot-types";

export type SleevePnl = {
  coin: string;
  unrealized: number;
  realized: number;
  open: boolean;
};

export function sleevePnl(latest: BlockEvent | null | undefined): SleevePnl {
  const pos = latest?.position;
  const open = Boolean(pos && pos.side !== "flat" && pos.size > 0);
  const unrealized = open && typeof pos?.unrealizedUsd === "number" && Number.isFinite(pos.unrealizedUsd)
    ? pos.unrealizedUsd
    : 0;
  const realized = typeof latest?.totals?.realizedUsd === "number" && Number.isFinite(latest.totals.realizedUsd)
    ? latest.totals.realizedUsd
    : 0;
  return { coin: latest?.coin ?? "", unrealized, realized, open };
}

export function portfolioPnl(latestByCoin: Record<string, BlockEvent | null | undefined>): {
  unrealized: number;
  realized: number;
} {
  let unrealized = 0;
  let realized = 0;
  for (const latest of Object.values(latestByCoin)) {
    const row = sleevePnl(latest);
    unrealized += row.unrealized;
    realized += row.realized;
  }
  return { unrealized, realized };
}

/**
 * The header's figures. Blocks the bot published win while there are any (they carry the live mark of an
 * open position); before the first block of a run, the bot's own counters from `/pnl` stand in. Null while
 * neither is known, which the header draws as a skeleton.
 */
export function headerPnl(
  latestByCoin: Record<string, BlockEvent | null | undefined>,
  summary: PnlSummary | null,
): { unrealized: number; realized: number } | null {
  // `useFeed` paints a stub (block 0, flat, totals 0) for a sleeve that has a price mark but no block yet, and
  // the bot's own blocks start at 1, so only `block > 0` is a block the bot published.
  if (Object.values(latestByCoin).some((b) => b != null && b.block > 0)) return portfolioPnl(latestByCoin);
  return summary ? { unrealized: summary.unrealizedUsd, realized: summary.realizedUsd } : null;
}

/** Unrealized as a percent of initial margin, when size, entry, and leverage exist. */
export function roePct(pos: {
  side: string;
  size: number;
  entryPrice: number | null;
  leverage: number | null;
  unrealizedUsd: number;
} | null | undefined): number | null {
  if (!pos || pos.side === "flat" || !(pos.size > 0) || !(pos.entryPrice && pos.entryPrice > 0)) return null;
  const lev = pos.leverage && pos.leverage > 0 ? pos.leverage : 1;
  const margin = (pos.size * pos.entryPrice) / lev;
  if (!(margin > 0)) return null;
  return pos.unrealizedUsd / margin;
}
