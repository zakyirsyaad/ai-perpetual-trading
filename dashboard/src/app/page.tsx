"use client";

import { useMemo, useState } from "react";
import Book from "@/components/Book/Book";
import DecisionPanel from "@/components/DecisionPanel/DecisionPanel";
import Feed from "@/components/Feed/Feed";
import FlowChart from "@/components/FlowChart/FlowChart";
import Header from "@/components/Header/Header";
import SleeveStrip from "@/components/SleeveStrip/SleeveStrip";
import UsagePanel from "@/components/UsagePanel/UsagePanel";
import VolumePanel from "@/components/VolumePanel/VolumePanel";
import { lastMeaningfulCall } from "@/lib/format";
import { portfolioPnl } from "@/lib/pnl";
import { useFeed } from "@/lib/useFeed";
import { useUsage } from "@/lib/useUsage";
import { useIsDesktop } from "@/lib/useIsDesktop";
import { useVolume } from "@/lib/useVolume";
import type { BlockEvent, Meta, SleeveFeed } from "@/lib/types";
import styles from "./page.module.css";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:3000";

const EMPTY: SleeveFeed = { events: [], tape: [], latest: null, avgLatencyMs: 0 };

function viewMeta(meta: Meta | null, coin: string): Meta | null {
  if (!meta) return null;
  const sleeve = meta.sleeves.find((s) => s.coin === coin);
  return {
    ...meta,
    coin,
    pair: sleeve?.pair ?? meta.pair,
    market: sleeve?.pair ?? meta.market,
    wallet: sleeve?.wallet ?? meta.wallet,
  };
}

export default function Page() {
  const feed = useFeed(API_URL);
  const volume = useVolume(API_URL);
  const usage = useUsage(API_URL);
  const isDesktop = useIsDesktop();
  const [picked, setPicked] = useState<string | null>(null);

  const coins = useMemo(() => {
    if (feed.meta?.sleeves.length) return feed.meta.sleeves.map((s) => s.coin);
    return Object.keys(feed.byCoin);
  }, [feed.meta, feed.byCoin]);

  const coin = picked && coins.includes(picked) ? picked : coins[0] ?? "BTC";
  const sleeve = feed.byCoin[coin] ?? EMPTY;
  const meta = viewMeta(feed.meta, coin);

  const latestByCoin = useMemo(() => {
    const out: Record<string, BlockEvent | null> = {};
    for (const c of coins) out[c] = feed.byCoin[c]?.latest ?? null;
    return out;
  }, [coins, feed.byCoin]);

  const tapeByCoin = useMemo(() => {
    const out: Record<string, NonNullable<SleeveFeed["tape"]>> = {};
    for (const c of coins) out[c] = feed.byCoin[c]?.tape ?? [];
    return out;
  }, [coins, feed.byCoin]);

  const lastCallByCoin = useMemo(() => {
    const out: Record<string, string> = {};
    for (const c of coins) {
      const s = feed.byCoin[c];
      out[c] = lastMeaningfulCall(s?.events ?? [], s?.latest ?? null);
    }
    return out;
  }, [coins, feed.byCoin]);

  const pnl = useMemo(() => portfolioPnl(latestByCoin), [latestByCoin]);
  const hasBooks = coins.some((c) => latestByCoin[c]);
  const waiting = !feed.meta;

  const header = (
    <div className={styles.slotHeader}>
      <Header
        connection={feed.connection}
        unrealized={hasBooks ? pnl.unrealized : null}
        realized={hasBooks ? pnl.realized : null}
      />
    </div>
  );
  const sleeves = (
    <div className={styles.slotSleeves}>
      <SleeveStrip
        sleeves={feed.meta?.sleeves ?? []}
        latestByCoin={latestByCoin}
        lastCallByCoin={lastCallByCoin}
        selected={coin}
        onSelect={setPicked}
        waiting={waiting}
        variant="tabs"
      />
    </div>
  );
  const chart = (
    <div className={styles.chartWrap}>
      <FlowChart
        tape={sleeve.tape ?? []}
        events={sleeve.events}
        latest={sleeve.latest}
        meta={meta}
        onNeedMoreTape={feed.loadTape}
      />
    </div>
  );
  const book = (
    <div className={styles.slotBook}>
      <Book
        sleeves={feed.meta?.sleeves ?? []}
        latestByCoin={latestByCoin}
        tapeByCoin={tapeByCoin}
        selected={coin}
        meta={meta}
        onSelect={setPicked}
        onNeedMoreTape={feed.loadTape}
        waiting={waiting}
        variant="fill"
      />
    </div>
  );
  const volumePanel = (
    <div className={styles.slotVolume}>
      <VolumePanel volume={volume} variant="rail" />
    </div>
  );
  const usagePanel = (
    <div className={styles.slotUsage}>
      <UsagePanel usage={usage} variant="rail" />
    </div>
  );
  const decision = (
    <div className={styles.slotDecision}>
      <DecisionPanel latest={sleeve.latest} meta={meta} waiting={waiting} />
    </div>
  );
  const callFeed = (
    <div className={styles.slotFeed}>
      <Feed events={sleeve.events} meta={meta} waiting={waiting} />
    </div>
  );

  // The DOM order follows what is on screen at each breakpoint, so keyboard
  // focus and screen readers read the page in its visual order.
  if (!isDesktop) {
    return (
      <div className="shell">
        {header}
        {volumePanel}
        {usagePanel}
        {sleeves}
        {chart}
        {decision}
        {callFeed}
        {book}
      </div>
    );
  }

  return (
    <div className="shell">
      {header}
      <div className={styles.body}>
        <div className={styles.work}>
          {sleeves}
          {chart}
          {book}
        </div>
        <div className={styles.rail}>
          {volumePanel}
          {usagePanel}
          {decision}
          {callFeed}
        </div>
      </div>
    </div>
  );
}
