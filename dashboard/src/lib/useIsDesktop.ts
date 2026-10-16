"use client";

import { useSyncExternalStore } from "react";

/** The breakpoint where the dashboard switches from the stacked layout to the two-column one. */
export const DESKTOP_QUERY = "(min-width: 900px)";

function subscribe(onChange: () => void): () => void {
  const list = window.matchMedia(DESKTOP_QUERY);
  list.addEventListener("change", onChange);
  return () => list.removeEventListener("change", onChange);
}

function getSnapshot(): boolean {
  return window.matchMedia(DESKTOP_QUERY).matches;
}

// The page is prerendered as the desktop tree; the mobile CSS (display: contents
// plus order) already paints that tree in the stacked order until hydration
// swaps in the mobile DOM order.
function getServerSnapshot(): boolean {
  return true;
}

/** True at the desktop breakpoint; picks the DOM order that matches what is on screen. */
export function useIsDesktop(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
