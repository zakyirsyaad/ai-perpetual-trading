"use client";

import { fmtSignedUsd, fmtUsd } from "@/lib/format";
import { Bone } from "@/components/Skeleton/Skeleton";
import styles from "./Header.module.css";

export interface HeaderProps {
  connection: "connecting" | "live" | "reconnecting";
  unrealized: number | null;
  realized: number | null;
}

function Score({
  label,
  value,
  signed = true,
}: {
  label: string;
  value: number | null;
  signed?: boolean;
}) {
  const color = !signed || value == null ? undefined : value >= 0 ? "var(--pnl-pos)" : "var(--pnl-neg)";
  return (
    <span className={styles.score}>
      <span className={styles.scoreKey}>{label}</span>
      <span className={styles.scoreVal} style={color ? { color } : undefined}>
        {value == null ? <Bone w={64} h={16} /> : signed ? fmtSignedUsd(value, 2) : fmtUsd(value, 2)}
      </span>
    </span>
  );
}

export default function Header({ connection, unrealized, realized }: HeaderProps) {
  const live = connection === "live";

  return (
    <div className={styles.header}>
      <span className={styles.brandLockup}>
        <h1 className={styles.brand}>AI Perpetual</h1>
        <p className={styles.tagline}>BingX VST volume bot</p>
      </span>
      <span className={styles.status} data-live={live ? "true" : "false"}>
        <span className={styles.dot} aria-hidden="true" />
        <span>{live ? "Live" : "Offline"}</span>
      </span>
      <span className={styles.scores}>
        <Score label="unrealized" value={unrealized} />
        <Score label="realized" value={realized} />
      </span>
    </div>
  );
}
