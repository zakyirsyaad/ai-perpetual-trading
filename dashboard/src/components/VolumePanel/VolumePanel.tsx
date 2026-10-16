"use client";

import { Bone } from "@/components/Skeleton/Skeleton";
import type { VolumeSummary } from "@/lib/bot-types";
import { fmtCompact, fmtDay, fmtInt } from "@/lib/format";
import { cx } from "@/lib/cx";
import styles from "./VolumePanel.module.css";

export interface VolumePanelProps {
  volume: VolumeSummary | null;
  /** "rail": one column for the desktop side rail (mobile is unchanged). */
  variant?: "stack" | "rail";
}

const MAX_DAYS = 14;

function pct(part: number, whole: number): number {
  return whole > 0 ? Math.max(0, (part / whole) * 100) : 0;
}

export default function VolumePanel({ volume, variant = "stack" }: VolumePanelProps) {
  const target = volume?.dailyTargetUsd ?? 0;
  const todayPct = volume ? pct(volume.todayUsd, target) : 0;
  const reached = volume != null && target > 0 && volume.todayUsd >= target;
  // Newest first; the running total is already computed per day by the bot.
  const days = volume ? [...volume.days].reverse().slice(0, MAX_DAYS) : [];
  const since = volume?.days[0]?.day;

  return (
    <section className={cx(styles.panel, variant === "rail" && styles.rail)} aria-label="Trading volume">
      <div className={styles.today}>
        <span className={styles.label}>Volume today</span>
        {volume ? (
          <>
            <span className={styles.big}>
              {fmtInt(volume.todayUsd)}
              <span className={styles.of}> / {target > 0 ? fmtInt(target) : "no target"}</span>
            </span>
            <span
              className={styles.track}
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.min(100, Math.round(todayPct))}
              aria-label="Share of the daily volume target reached"
            >
              <span className={styles.fill} data-done={reached ? "true" : "false"} style={{ width: `${Math.min(100, todayPct)}%` }} />
            </span>
            <span className={styles.sub}>
              {target > 0
                ? `${todayPct.toFixed(1)}% of target · ${fmtInt(Math.max(0, target - volume.todayUsd))} to go`
                : "daily target not set"}
            </span>
          </>
        ) : (
          <>
            <Bone w={180} h={22} />
            <Bone w={220} h={10} />
          </>
        )}
      </div>

      <div className={styles.history}>
        <div className={styles.historyHead}>
          <span className={styles.label}>Volume by day · cumulative</span>
          {volume && since ? <span className={styles.sub}>total {fmtCompact(volume.totalUsd)} since {fmtDay(since)}</span> : null}
        </div>
        {volume ? (
          <ul className={styles.rows}>
            {days.map((d) => (
              <li key={d.day} className={styles.row} data-today={d.day === volume.today ? "true" : "false"}>
                <span className={styles.day}>{fmtDay(d.day)}</span>
                <span className={styles.dayTrack} aria-hidden="true">
                  <span className={styles.dayFill} style={{ width: `${Math.min(100, pct(d.volumeUsd, target))}%` }} />
                </span>
                <span className={styles.num}>{fmtCompact(d.volumeUsd)}</span>
                <span className={styles.cum} title="Cumulative volume up to and including this day">Σ {fmtCompact(d.cumulativeUsd)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <Bone w={320} h={14} />
        )}
      </div>
    </section>
  );
}
