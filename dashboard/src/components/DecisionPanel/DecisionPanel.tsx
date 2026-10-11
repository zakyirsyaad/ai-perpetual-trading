"use client";

import type { BlockEvent, Meta } from "@/lib/types";
import { fmtCall, fmtPct } from "@/lib/format";
import { Bone } from "@/components/Skeleton/Skeleton";
import styles from "./DecisionPanel.module.css";

export interface DecisionPanelProps {
  latest: BlockEvent | null;
  meta?: Meta | null;
  waiting?: boolean;
}

interface BarRowProps {
  label: string;
  active: boolean;
  value: number;
  fill: string;
  pct: string;
}

function BarRow({ label, active, value, fill, pct }: BarRowProps) {
  return (
    <div className={styles.row}>
      <span className={styles.label} style={{ color: "var(--ink)", opacity: active ? 1 : 0.38 }}>
        {label}
      </span>
      <div className={styles.track}>
        <div
          className={styles.fill}
          style={{
            width: `${Math.max(0, Math.min(1, value)) * 100}%`,
            background: fill,
          }}
        />
      </div>
      <span className={styles.pct}>{pct}</span>
    </div>
  );
}

export default function DecisionPanel({ latest, waiting = false }: DecisionPanelProps) {
  if (waiting && !latest) {
    return (
      <div className={styles.panel} aria-busy="true" aria-label="Loading call">
        <section className={styles.section}>
          <div className={styles.railHead}>CALL</div>
          <div className={styles.body}>
            <div className={styles.headline}>
              <Bone w={72} h={22} />
              <span className={styles.metaLine}>
                <Bone w={40} h={10} />
              </span>
            </div>
            <div className={styles.bars}>
              <div className={styles.row}>
                <span className={styles.label} style={{ opacity: 0.38 }}>
                  conf
                </span>
                <div className={styles.track} />
                <span className={styles.pct}>
                  <Bone w={28} h={10} />
                </span>
              </div>
            </div>
          </div>
        </section>
      </div>
    );
  }

  const decision = latest?.decision ?? null;
  const held = decision?.intent === "hold";
  const chosen = decision && !held && decision.action !== "hold" ? (decision.bias ?? decision.action) : null;
  const decided = decision !== null && (chosen !== null || held);

  const headline = decided ? fmtCall(decision) || "-" : "-";
  const headlineColor = held
    ? "var(--ink-2)"
    : chosen
      ? (decision?.bias ?? decision?.action) === "short" || decision?.action === "sell"
        ? "var(--sell-ink)"
        : "var(--buy-ink)"
      : "var(--late-ink)";

  // The bot publishes how sure the model said it was, not a probability per action.
  const confidence = decision?.confidence;
  const reason = decision?.reason;

  return (
    <div className={styles.panel}>
      <section className={styles.section}>
        <div className={styles.railHead}>CALL</div>
        <div className={styles.body}>
          <div className={styles.headline}>
            <span className={styles.headlineWord} style={{ color: headlineColor }}>
              {headline}
            </span>
            {decided && decision && decision.latencyMs > 0 ? (
              <span className={styles.metaLine}>{decision.latencyMs} ms</span>
            ) : null}
          </div>

          <div className={styles.bars}>
            <BarRow
              label="conf"
              active={confidence != null}
              value={confidence ?? 0}
              fill={decision?.bias === "short" || decision?.action === "sell" ? "var(--sell-bar)" : "var(--buy-bar)"}
              pct={confidence != null ? fmtPct(confidence) : "-"}
            />
          </div>
          {reason ? (
            <p className={styles.reason} title={reason}>
              {reason}
            </p>
          ) : null}
        </div>
      </section>
    </div>
  );
}
