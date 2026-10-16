"use client";

import { useSyncExternalStore } from "react";

/** The breakpoint where the dashboard switches from the stacked layout to the two-column one. */
export const DESKTOP_QUERY = "(min-width: 900px)";

let list: MediaQueryList | null = null;

function query(): MediaQueryList {
  list ??= window.matchMedia(DESKTOP_QUERY);
  return list;
}

function subscribe(onChange: () => void): () => void {
  const mql = query();
  mql.addEventListener("change", onChange);
  return () => mql.removeEventListener("change", onChange);
}

function getSnapshot(): boolean {
  return query().matches;
}

// Unknown while prerendering and hydrating: the page renders only its shell
// until the browser reports the width, so each panel mounts once, in the tree
// that matches the screen.
function getServerSnapshot(): null {
  return null;
}

/** True at the desktop breakpoint, false below it, null before the width is known. */
export function useIsDesktop(): boolean | null {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
