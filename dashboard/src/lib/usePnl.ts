"use client";

import { useEffect, useState } from "react";
import type { PnlSummary } from "@/lib/bot-types";

/**
 * Polls `/pnl`: the header's realized and unrealized figures, read from the bot's own counters. The header
 * normally reads them from the newest block, but a run has no block until its first decision or close (hours,
 * when the entry window is closed or the daily cap is reached), while the counters are seeded from the store
 * the moment the bot starts. A failed poll (offline, an error status) keeps the last figures, and so does one empty
 * answer (a bot that has just restarted has not seeded yet); a second empty answer in a row clears them.
 */
export function usePnl(base: string, everyMs = 15_000): PnlSummary | null {
  const [pnl, setPnl] = useState<PnlSummary | null>(null);

  useEffect(() => {
    let alive = true;
    let empty = 0; // consecutive answers with nothing published
    const load = async () => {
      try {
        const r = await fetch(`${base}/pnl`, { cache: "no-store" });
        if (!r.ok) return;
        const body = await r.json();
        if (!alive) return;
        if (Number.isFinite(body?.realizedUsd) && Number.isFinite(body?.unrealizedUsd)) {
          empty = 0;
          setPnl(body as PnlSummary);
        } else if (++empty >= 2) {
          // A bot that has just restarted has not seeded yet, so one empty answer keeps the last figures; a second
          // in a row means an older process's figures (possibly of an older cycle) must not stay up.
          setPnl(null);
        }
      } catch {
        /* offline or restarting: keep what is on screen */
      }
    };
    void load();
    const id = setInterval(load, everyMs);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [base, everyMs]);

  return pnl;
}
