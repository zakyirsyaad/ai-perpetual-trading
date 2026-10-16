"use client";

import { useEffect, useState, type Dispatch, type SetStateAction } from "react";

// Lives for the page session, outside any component, so a panel that remounts
// (the layout switches trees when the window crosses the desktop breakpoint)
// keeps its tab, toggle or interval.
const store = new Map<string, unknown>();

/**
 * useState whose value survives a remount of the component, keyed by `key`.
 * `initial` (a value or, like useState, a function) is used only when the key
 * has nothing stored yet.
 */
export function useSessionState<T>(key: string, initial: T | (() => T)): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    if (store.has(key)) return store.get(key) as T;
    return typeof initial === "function" ? (initial as () => T)() : initial;
  });
  // Mirror the committed value only: a render React discards never reaches the store.
  useEffect(() => {
    store.set(key, value);
  }, [key, value]);
  return [value, setValue];
}
