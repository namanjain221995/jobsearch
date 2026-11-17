import { useSyncExternalStore } from "react";

/**
 * The current time, refreshed on a shared timer. Returns null while rendering on
 * the server and during hydration, so anything derived from "now" (relative
 * times, the watermark timestamp) can never disagree with the server HTML.
 */
const listeners = new Set<() => void>();
let now = 0;
let timer: ReturnType<typeof setInterval> | null = null;
const TICK_MS = 30_000;

function tick() {
  now = Date.now();
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (timer === null) {
    now = Date.now();
    timer = setInterval(tick, TICK_MS);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

function getSnapshot(): number | null {
  if (now === 0) now = Date.now();
  return now;
}

function getServerSnapshot(): number | null {
  return null;
}

export function useNow(): number | null {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}

/** Today's date in the viewer's calendar as YYYY-MM-DD, or null on the server and during hydration. */
export function useToday(): string | null {
  const current = useNow();
  if (current === null) return null;
  const date = new Date(current);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}
