"use client";

import { Fragment, useMemo } from "react";
import type { BlockEvent, Meta, PricePoint, SleeveMeta } from "@/lib/types";
import { closedLots, dayKey, groupByDay, lotVolume, tapeFills } from "@/lib/fills";
import { roePct } from "@/lib/pnl";
import {
  displayCoin,
  fmtClock,
  fmtCoin,
  fmtCompact,
  fmtDay,
  fmtPct,
  fmtPrice,
  fmtSignedUsd,
  fmtUsd,
  shortTx,
  txUrl,
} from "@/lib/format";
import { Bone } from "@/components/Skeleton/Skeleton";
import { cx } from "@/lib/cx";
import { useSessionState } from "@/lib/useSessionState";
import styles from "./Book.module.css";

type Tab = "positions" | "trades" | "history";

const TRADE_CAP = 200;

function fmtSize(size: number): string {
  if (size > 0 && size < 0.01) {
    return size.toLocaleString("en-US", { maximumFractionDigits: 5, minimumFractionDigits: 3 });
  }
  return size.toLocaleString("en-US", { maximumFractionDigits: 4 });
}

export default function Book({
  sleeves,
  latestByCoin,
  tapeByCoin,
  selected,
  meta,
  onSelect,
  onNeedMoreTape,
  waiting = false,
  variant = "stack",
}: {
  sleeves: SleeveMeta[];
  latestByCoin: Record<string, BlockEvent | null>;
  tapeByCoin: Record<string, PricePoint[]>;
  selected: string;
  meta?: Meta | null;
  onSelect: (coin: string) => void;
  onNeedMoreTape?: () => void;
  waiting?: boolean;
  /** "fill": on desktop the book fills its grid row instead of its fixed height. */
  variant?: "stack" | "fill";
}) {
  const [tab, setTab] = useSessionState<Tab>("book.tab", "positions");
  const [allMarkets, setAllMarkets] = useSessionState("book.allMarkets", false);

  const trades = useMemo(() => {
    const rows = [];
    for (const sleeve of sleeves) {
      if (!allMarkets && sleeve.coin !== selected) continue;
      for (const fill of tapeFills(tapeByCoin[sleeve.coin] ?? [])) {
        rows.push({ ...fill, coin: sleeve.coin });
      }
    }
    rows.sort((a, b) => b.ts - a.ts);
    return rows.slice(0, TRADE_CAP);
  }, [allMarkets, selected, sleeves, tapeByCoin]);

  const history = useMemo(() => {
    const rows = [];
    for (const sleeve of sleeves) {
      if (!allMarkets && sleeve.coin !== selected) continue;
      for (const lot of closedLots(tapeFills(tapeByCoin[sleeve.coin] ?? []))) {
        rows.push({ ...lot, coin: sleeve.coin });
      }
    }
    rows.sort((a, b) => b.ts - a.ts);
    return rows.slice(0, TRADE_CAP);
  }, [allMarkets, selected, sleeves, tapeByCoin]);

  const historyDays = useMemo(() => groupByDay(history), [history]);
  const tradeDays = useMemo(() => groupByDay(trades), [trades]);
  const today = dayKey(Date.now());

  return (
    <section className={cx(styles.wrap, variant === "fill" && styles.fill)}>
      <div className={styles.tabs} role="tablist" aria-label="Account book">
        <button
          type="button"
          className={tab === "positions" ? styles.tabOn : styles.tab}
          role="tab"
          aria-selected={tab === "positions"}
          onClick={() => setTab("positions")}
        >
          Positions
        </button>
        <button
          type="button"
          className={tab === "trades" ? styles.tabOn : styles.tab}
          role="tab"
          aria-selected={tab === "trades"}
          onClick={() => {
            setTab("trades");
            onNeedMoreTape?.();
          }}
        >
          Trades
        </button>
        <button
          type="button"
          className={tab === "history" ? styles.tabOn : styles.tab}
          role="tab"
          aria-selected={tab === "history"}
          onClick={() => {
            setTab("history");
            onNeedMoreTape?.();
          }}
        >
          History
        </button>
        {tab === "trades" || tab === "history" ? (
          <span className={styles.scope}>
            <button
              type="button"
              className={allMarkets ? styles.scopeBtn : styles.scopeOn}
              onClick={() => setAllMarkets(false)}
            >
              {displayCoin(selected)}
            </button>
            <button
              type="button"
              className={allMarkets ? styles.scopeOn : styles.scopeBtn}
              onClick={() => setAllMarkets(true)}
            >
              All
            </button>
          </span>
        ) : null}
      </div>
      {tab === "positions" ? (
        <div className={styles.scroller}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Market</th>
                <th>Side</th>
                <th>Size</th>
                <th>Entry</th>
                <th>Mark</th>
                <th>Value</th>
                <th>Unrealized</th>
                <th>ROE</th>
                <th>Lev</th>
              </tr>
            </thead>
            <tbody>
              {waiting && sleeves.length === 0
                ? Array.from({ length: 5 }, (_, i) => (
                    <tr key={i} className={styles.skelRow}>
                      <td><Bone w={36} h={10} /></td>
                      <td><Bone w={40} h={10} /></td>
                      <td><Bone w={72} h={10} /></td>
                      <td><Bone w={56} h={10} /></td>
                      <td><Bone w={56} h={10} /></td>
                      <td><Bone w={52} h={10} /></td>
                      <td><Bone w={56} h={10} /></td>
                      <td><Bone w={28} h={10} /></td>
                      <td><Bone w={28} h={10} /></td>
                    </tr>
                  ))
                : sleeves.map((sleeve) => {
                const latest = latestByCoin[sleeve.coin] ?? null;
                const pos = latest?.position;
                const open = Boolean(pos && pos.side !== "flat" && pos.size > 0);
                const mark = latest?.mid ?? null;
                const value = open && mark != null ? pos!.size * mark : null;
                const u = open ? pos!.unrealizedUsd : 0;
                const roe = roePct(pos);
                const side = (pos?.side ?? "flat").toUpperCase();
                const sideColor =
                  pos?.side === "long" ? "var(--buy-ink)" : pos?.side === "short" ? "var(--sell-ink)" : undefined;
                const pnlColor = open ? (u >= 0 ? "var(--pnl-pos)" : "var(--pnl-neg)") : undefined;
                const on = sleeve.coin === selected;
                return (
                  <tr
                    key={sleeve.coin}
                    className={on ? styles.rowOn : undefined}
                    onClick={() => onSelect(sleeve.coin)}
                  >
                    <td>{displayCoin(sleeve.coin)}</td>
                    <td style={sideColor ? { color: sideColor } : undefined}>{side}</td>
                    <td>{open ? fmtCoin(pos!.size, sleeve.coin, 4) : "-"}</td>
                    <td>{open && pos?.entryPrice != null ? fmtPrice(pos.entryPrice) : "-"}</td>
                    <td>{mark != null ? fmtPrice(mark) : "-"}</td>
                    <td>{value != null ? fmtPrice(value) : "-"}</td>
                    <td style={pnlColor ? { color: pnlColor } : undefined}>
                      {open ? fmtSignedUsd(u, 2) : "-"}
                    </td>
                    <td style={pnlColor ? { color: pnlColor } : undefined}>
                      {roe != null ? fmtPct(roe) : "-"}
                    </td>
                    <td>{pos?.leverage != null ? `${pos.leverage}x` : "-"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : tab === "history" ? (
        waiting && history.length === 0 ? (
          <div className={styles.scroller} aria-busy="true" aria-label="Loading history">
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Market</th>
                  <th>Side</th>
                  <th>Size</th>
                  <th>Entry</th>
                  <th>Exit</th>
                  <th>PnL</th>
                  <th>Fee</th>
                </tr>
              </thead>
              <tbody>
                {Array.from({ length: 6 }, (_, i) => (
                  <tr key={i} className={styles.skelRow}>
                    <td><Bone w={64} h={10} /></td>
                    <td><Bone w={36} h={10} /></td>
                    <td><Bone w={40} h={10} /></td>
                    <td><Bone w={48} h={10} /></td>
                    <td><Bone w={56} h={10} /></td>
                    <td><Bone w={56} h={10} /></td>
                    <td><Bone w={56} h={10} /></td>
                    <td><Bone w={40} h={10} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : history.length === 0 ? (
          <div className={styles.empty}>no closed lots yet</div>
        ) : (
          <div className={styles.scroller}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Market</th>
                  <th>Side</th>
                  <th>Size</th>
                  <th>Entry</th>
                  <th>Exit</th>
                  <th>PnL</th>
                  <th>Fee</th>
                </tr>
              </thead>
              <tbody>
                {historyDays.map((group) => {
                  const net = group.rows.reduce((sum, lot) => sum + lot.pnl, 0);
                  const fees = group.rows.reduce((sum, lot) => sum + lot.fee, 0);
                  const volume = group.rows.reduce((sum, lot) => sum + lotVolume(lot), 0);
                  return (
                    <Fragment key={group.day}>
                      <tr className={styles.dayRow}>
                        <td colSpan={8}>
                          <span className={styles.dayName}>{fmtDay(group.day)}</span>
                          {group.day === today ? <span className={styles.dayTag}>today</span> : null}
                          <span className={styles.daySum}>
                            {group.rows.length} {group.rows.length === 1 ? "trade" : "trades"} · net{" "}
                            <span style={{ color: net >= 0 ? "var(--pnl-pos)" : "var(--pnl-neg)" }}>{fmtSignedUsd(net, 2)}</span>
                            {" "}· fees {fmtUsd(fees, 2)} · volume {fmtCompact(volume)}
                          </span>
                        </td>
                      </tr>
                      {group.rows.map((lot) => {
                        const sideColor = lot.side === "long" ? "var(--buy-ink)" : "var(--sell-ink)";
                        const pnlColor = lot.pnl >= 0 ? "var(--pnl-pos)" : "var(--pnl-neg)";
                        return (
                          <tr key={`${lot.coin}|${lot.key}`} onClick={() => onSelect(lot.coin)}>
                            <td>{fmtClock(lot.ts, true)}</td>
                            <td>{displayCoin(lot.coin)}</td>
                            <td style={{ color: sideColor }}>{lot.side.toUpperCase()}</td>
                            <td>{fmtSize(lot.size)}</td>
                            <td>{lot.entry != null ? fmtPrice(lot.entry) : "-"}</td>
                            <td>{fmtPrice(lot.exit)}</td>
                            <td style={{ color: pnlColor }}>{fmtSignedUsd(lot.pnl, 2)}</td>
                            <td>{lot.fee ? fmtUsd(lot.fee, 2) : "-"}</td>
                          </tr>
                        );
                      })}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )
      ) : waiting && trades.length === 0 ? (
        <div className={styles.scroller} aria-busy="true" aria-label="Loading trades">
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Time</th>
                <th>Market</th>
                <th>Side</th>
                <th>Action</th>
                <th>Price</th>
                <th>Size</th>
                <th>Tx</th>
              </tr>
            </thead>
            <tbody>
              {Array.from({ length: 6 }, (_, i) => (
                <tr key={i} className={styles.skelRow}>
                  <td><Bone w={64} h={10} /></td>
                  <td><Bone w={36} h={10} /></td>
                  <td><Bone w={36} h={10} /></td>
                  <td><Bone w={44} h={10} /></td>
                  <td><Bone w={56} h={10} /></td>
                  <td><Bone w={48} h={10} /></td>
                  <td><Bone w={52} h={10} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : trades.length === 0 ? (
        <div className={styles.empty}>no trades yet</div>
      ) : (
        <div className={styles.scroller}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Time</th>
                <th>Market</th>
                <th>Side</th>
                <th>Action</th>
                <th>Price</th>
                <th>Size</th>
                <th>Tx</th>
              </tr>
            </thead>
            <tbody>
              {tradeDays.map((group) => {
                const volume = group.rows.reduce((sum, fill) => sum + fill.size * fill.price, 0);
                return (
                  <Fragment key={group.day}>
                    <tr className={styles.dayRow}>
                      <td colSpan={7}>
                        <span className={styles.dayName}>{fmtDay(group.day)}</span>
                        {group.day === today ? <span className={styles.dayTag}>today</span> : null}
                        <span className={styles.daySum}>
                          {group.rows.length} {group.rows.length === 1 ? "fill" : "fills"} · volume {fmtCompact(volume)}
                        </span>
                      </td>
                    </tr>
                    {group.rows.map((fill) => {
                      const sideColor = fill.side === "buy" ? "var(--buy-ink)" : "var(--sell-ink)";
                      const action = fill.dir === "open" ? "OPEN" : fill.dir === "close" ? "CLOSE" : fill.dir === "flip" ? "FLIP" : "FILL";
                      return (
                        <tr key={`${fill.coin}|${fill.key}`} onClick={() => onSelect(fill.coin)}>
                          <td>{fmtClock(fill.ts, true)}</td>
                          <td>{displayCoin(fill.coin)}</td>
                          <td style={{ color: sideColor }}>{fill.side.toUpperCase()}</td>
                          <td style={{ color: sideColor }}>{action}</td>
                          <td>{fmtPrice(fill.price)}</td>
                          <td>{fmtSize(fill.size)}</td>
                          <td>
                            {fill.hash ? (
                              <a href={txUrl(fill.hash, meta?.explorerTx)} target="_blank" rel="noreferrer">
                                {shortTx(fill.hash)}
                              </a>
                            ) : (
                              "-"
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
