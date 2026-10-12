"use client";

import { useEffect, useState } from "react";
import type { VolumeSummary } from "@/lib/bot-types";

/**
 * Polls `/volume`. It is a separate route from the event stream because the series is
 * only worth sending when it changes, and because it must stay readable while the bot is
 * idle (weekends): the feed publishes no block events then. A failed poll keeps the last figures.
 */
export function useVolume(base: string, everyMs = 30_000): VolumeSummary | null {
  const [volume, setVolume] = useState<VolumeSummary | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await fetch(`${base}/volume`, { cache: "no-store" });
        if (!r.ok) return;
        const body = await r.json();
        if (alive && body && Array.isArray(body.days)) setVolume(body as VolumeSummary);
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

  return volume;
}
