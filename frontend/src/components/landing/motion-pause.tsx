"use client";

import { useSyncExternalStore } from "react";
import { Pause, Play } from "lucide-react";

import { useReducedMotion } from "./use-reduced-motion";

/**
 * One switch that stops every moving part of the landing page (WCAG 2.2.2: moving
 * content that runs longer than five seconds can be paused). It sets `data-paused`
 * on the landing root, which the CSS uses to pause every animation, and the hero
 * reads the same state to stop cycling its stages. Not shown with reduced motion,
 * because then nothing moves.
 */

let paused = false;
const listeners = new Set<() => void>();

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function setPaused(next: boolean) {
  paused = next;
  document.querySelector(".lp-root")?.toggleAttribute("data-paused", next);
  for (const listener of listeners) listener();
}

export function useMotionPaused(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => paused,
    () => false,
  );
}

export function MotionPauseButton() {
  const isPaused = useMotionPaused();
  const reduced = useReducedMotion();
  if (reduced) return null;
  return (
    <button type="button" className="lp-pause" aria-pressed={isPaused} onClick={() => setPaused(!isPaused)}>
      {isPaused ? <Play className="size-3.5" aria-hidden="true" /> : <Pause className="size-3.5" aria-hidden="true" />}
      {isPaused ? "Play animations" : "Pause animations"}
    </button>
  );
}
