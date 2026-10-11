/**
 * Wire types for the bot's dashboard feed (src/bot/telemetry/wire.py in the bot repo).
 * tests/test_dashboard_contract.py compares these interfaces with the Python models,
 * so change both sides together.
 */
export type Action = "buy" | "sell" | "hold";
export type Side = "buy" | "sell";
export type Bias = "long" | "short";
/** `hold` posts nothing and pulls any resting quote. */
export type Intent = "open" | "close" | "hold";

export interface Book {
  block: number;
  bid: number;
  ask: number;
  mid: number;
  spreadBps: number;
  /** (bidDepth - askDepth) / (bidDepth + askDepth) within 1% of mid. -1..1 */
  imbalance: number;
  levels: { bids: [number, number][]; asks: [number, number][] };
  depthBps: { [band: string]: { bid: number; ask: number } };
}

/** This tick's order. Entries are post-only limits, exits are Ioc takers. */
export interface Quote {
  side: Side;
  price: number;
  size: number;
  txHash: string | null;
  cancel: number[];
  status: "placed" | "reverted" | "sim";
  orderId: number | null;
  capped: boolean;
  reduceOnly?: boolean;
  unchanged?: boolean;
  /** Ioc order that crossed the touch instead of resting on it. */
  taker?: boolean;
}

/** A taker hit one of our resting orders. */
export interface Fill {
  side: Side;
  size: number;
  price: number;
  txHash: string | null;
  orderId: number;
  simulated: boolean;
  feeUsd?: number;
  closedPnl?: number;
  dir?: "open" | "close" | "flip";
}

/** Compact tape print. Candles carry o/h/l/c. Fills sit on this series. */
export interface PricePoint {
  ts: number;
  mid: number;
  open?: number;
  high?: number;
  low?: number;
  close?: number;
  bar?: "1s" | "1m" | "15m";
  block?: number;
  fill?: {
    side: Side;
    price: number;
    size: number;
    dir?: "open" | "close" | "flip";
    hash?: string;
    closedPnl?: number;
    feeUsd?: number;
  };
}

export interface Decision {
  action: Action;
  intent?: Intent;
  bias?: Bias;
  leverage?: number;
  probabilities: {
    buy: number;
    sell: number;
    hold: number;
    long?: number;
    short?: number;
    open?: number;
    close?: number;
  };
  upIn10: number;
  latencyMs: number;
  late: boolean;
  /**
   * The model's self-reported certainty, 0..1. Not a calibrated probability, which is
   * why `probabilities` above is one-hot on the action taken and this travels apart.
   */
  confidence?: number;
  /** Why the bot acted: the model's reason on an open, the exit reason on a close. */
  reason?: string;
}

export interface Position {
  side: "long" | "short" | "flat";
  size: number;
  entryPrice: number | null;
  leverage: number | null;
  unrealizedUsd: number;
  unrealizedSz: number;
}

export interface Totals {
  blocks: number;
  decisions: number;
  quotes: number;
  fills: number;
  reverted: number;
  lateBlocks: number;
  jevUsd: number;
  gasSz: number;
  gasUsd: number;
  realizedUsd: number;
  pnlUsd: number;
  pnlSz: number;
  pnlPct: number;
}

export interface BlockEvent {
  coin: string;
  block: number;
  ts: number;
  mid: number;
  bestBid: number;
  bestAsk: number;
  spreadBps: number;
  decision: Decision | null;
  quote: Quote | null;
  fill: Fill | null;
  resting: { bidSz: number; askSz: number };
  position: Position;
  totals: Totals;
  /** Account equity. The BingX feed leaves it null: reading it would need a private endpoint. */
  accountValue?: number | null;
  withdrawable?: number | null;
}

export interface SleeveMeta {
  coin: string;
  pair: string;
  label: string;
  wallet: string | null;
}

export interface Meta {
  model: string;
  wallet: string | null;
  dryRun: boolean;
  market: string;
  startedAt: number;
  venue: string;
  coin: string;
  pair: string;
  explorerTx: string;
  tickMs: number;
  sleeves: SleeveMeta[];
}

export interface Timing {
  readMs: number;
  loopMs: number;
}
