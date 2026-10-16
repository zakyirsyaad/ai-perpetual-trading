"use client";

import { useCallback, useState } from "react";

// Lives for the page session, outside any component, so a panel that remounts
// (the layout switches trees when the window crosses the desktop breakpoint)
// keeps its tab, toggle or interval.
const store = new Map<string, unknown>();

type Next<T> = T | ((prev: T) => T);

/** useState whose value survives a remount of the component, keyed by `key`. */
export function useSessionState<T>(key: string, initial: T): [T, (next: Next<T>) => void] {
  const [value, setValue] = useState<T>(() => (store.has(key) ? (store.get(key) as T) : initial));
  const set = useCallback(
    (next: Next<T>) => {
      setValue((prev) => {
        const resolved = typeof next === "function" ? (next as (p: T) => T)(prev) : next;
        store.set(key, resolved);
        return resolved;
      });
    },
    [key],
  );
  return [value, set];
}
