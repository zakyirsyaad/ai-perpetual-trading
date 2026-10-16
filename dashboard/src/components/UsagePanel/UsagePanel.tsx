"use client";

import { useEffect, useState } from "react";
import { Bone } from "@/components/Skeleton/Skeleton";
import type { UsageDay, UsageSummary } from "@/lib/bot-types";
import { fmtCompact, fmtDay, fmtInt } from "@/lib/format";
import { share, sumDays, todayOf, tokenMix, tokensPerTrade, type TokenMix } from "@/lib/usage";
import styles from "./UsagePanel.module.css";

export interface UsagePanelProps {
  usage: UsageSummary | null;
  /** "rail": compact layout for the desktop side rail (mobile is unchanged). */
  variant?: "band" | "rail";
}

const MAX_DAYS = 14;
const STORAGE_KEY = "usage-panel-open";
const DETAIL_ID = "usage-detail";

function mixTotal(mix: TokenMix): number {
  return mix.input + mix.reasoning + mix.answer;
}

function perTradeLabel(totalTokens: number, closedTrades: number): string {
  const perTrade = tokensPerTrade(totalTokens, closedTrades);
  return perTrade === null ? "—/trade" : `${fmtCompact(perTrade)}/trade`;
}

function mixTitle(mix: TokenMix): string {
  return `input ${fmtCompact(mix.input)} · reasoning ${fmtCompact(mix.reasoning)} · output ${fmtCompact(mix.answer)}`;
}

/** Input, reasoning and answer as one segmented bar. `widthPct` scales the whole bar inside its track. */
function MixBar({ mix, widthPct = 100 }: { mix: TokenMix; widthPct?: number }) {
  const whole = mixTotal(mix);
  return (
    <span className={styles.mix} style={{ width: `${widthPct}%` }} title={mixTitle(mix)}>
      <span className={styles.segInput} style={{ width: `${share(mix.input, whole)}%` }} />
      <span className={styles.segReasoning} style={{ width: `${share(mix.reasoning, whole)}%` }} />
      <span className={styles.segAnswer} style={{ width: `${share(mix.answer, whole)}%` }} />
    </span>
  );
}

function DayRow({ day, today, widest }: { day: UsageDay; today: boolean; widest: number }) {
  const mix = tokenMix(day);
  return (
    <li className={styles.row} data-today={today ? "true" : "false"}>
      <span className={styles.day}>{fmtDay(day.day)}</span>
      <span className={styles.rowTrack} aria-hidden="true">
        <MixBar mix={mix} widthPct={share(mixTotal(mix), widest)} />
      </span>
      <span className={styles.num}>{fmtCompact(day.totalTokens)}</span>
      <span className={styles.calls}>
        {fmtInt(day.calls)} calls{day.failed > 0 ? ` · ${fmtInt(day.failed)} failed` : ""}
      </span>
      <span className={styles.split}>
        entry {fmtCompact(day.entryTokens)} · exit {fmtCompact(day.exitTokens)}
      </span>
      <span className={styles.perTrade}>{perTradeLabel(day.totalTokens, day.closedTrades)}</span>
    </li>
  );
}

export default function UsagePanel({ usage, variant = "band" }: UsagePanelProps) {
  const [open, setOpen] = useState(false);

  // Remembered per browser; the page is correct without it (private window, blocked storage).
  useEffect(() => {
    try {
      if (window.localStorage.getItem(STORAGE_KEY) === "1") setOpen(true);
    } catch {
      /* storage unavailable: stay closed */
    }
  }, []);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next ? "1" : "0");
    } catch {
      /* not remembered, still works */
    }
  };

  const today = todayOf(usage);
  const days = usage ? [...usage.days].reverse().slice(0, MAX_DAYS) : [];
  const widest = days.reduce((max, d) => Math.max(max, mixTotal(tokenMix(d))), 0);
  const todayMix = today ? tokenMix(today) : null;
  const totals = usage ? sumDays(usage.days) : null;
  const since = usage?.days[0]?.day;

  return (
    <section className={variant === "rail" ? `${styles.panel} ${styles.rail}` : styles.panel} aria-label="MiMo token usage">
      <div className={styles.strip}>
        <span className={styles.label}>MiMo tokens</span>
        {today && todayMix ? (
          <>
            <span className={styles.big}>
              {fmtCompact(today.totalTokens)}
              <span className={styles.unit}> today</span>
            </span>
            <span className={styles.facts}>
              {today.calls > 0 ? (
                <>
                  {fmtInt(today.calls)} calls · <span data-failed={today.failed > 0 ? "true" : "false"}>{fmtInt(today.failed)} failed</span> ·{" "}
                  {perTradeLabel(today.totalTokens, today.closedTrades)}
                </>
              ) : (
                "no calls yet"
              )}
            </span>
            <span className={styles.stripMix} aria-hidden="true">
              <MixBar mix={todayMix} />
            </span>
          </>
        ) : (
          <>
            <Bone w={110} h={16} />
            <Bone w={200} h={10} />
          </>
        )}
        <button
          type="button"
          className={styles.toggle}
          aria-expanded={open}
          aria-controls={DETAIL_ID}
          onClick={toggle}
          disabled={!usage}
        >
          Detail <span aria-hidden="true">{open ? "▴" : "▾"}</span>
        </button>
      </div>

      <div id={DETAIL_ID} className={styles.detail} hidden={!open}>
        {usage && todayMix ? (
          <>
            <div className={styles.legend}>
              <span className={styles.legendTitle}>Today</span>
              <span className={styles.legendItem}>
                <span className={styles.swatchInput} aria-hidden="true" /> input {fmtCompact(todayMix.input)}
              </span>
              <span className={styles.legendItem}>
                <span className={styles.swatchReasoning} aria-hidden="true" /> reasoning {fmtCompact(todayMix.reasoning)}
              </span>
              <span className={styles.legendItem}>
                <span className={styles.swatchAnswer} aria-hidden="true" /> output {fmtCompact(todayMix.answer)}
              </span>
            </div>
            <ul className={styles.rows}>
              {days.map((d) => (
                <DayRow key={d.day} day={d} today={d.day === usage.today} widest={widest} />
              ))}
            </ul>
            {totals && since ? (
              <span className={styles.sum}>
                Σ {fmtCompact(totals.totalTokens)} since {fmtDay(since)} · {fmtInt(totals.calls)} calls · {fmtInt(totals.failed)} failed ·{" "}
                {perTradeLabel(totals.totalTokens, totals.closedTrades)}
              </span>
            ) : null}
          </>
        ) : null}
      </div>
    </section>
  );
}
