"use client";

import { useEffect, useState } from "react";
import type { UsageSummary } from "@/lib/bot-types";

/**
 * Polls `/usage`. Like `/volume` it is its own route: the figures only change when a model call is
 * recorded, and they must stay readable while the bot is idle (weekends). A failed poll keeps the last figures.
 */
export function useUsage(base: string, everyMs = 30_000): UsageSummary | null {
  const [usage, setUsage] = useState<UsageSummary | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await fetch(`${base}/usage`, { cache: "no-store" });
        if (!r.ok) return;
        const body = await r.json();
        if (alive && body && Array.isArray(body.days)) setUsage(body as UsageSummary);
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

  return usage;
}
