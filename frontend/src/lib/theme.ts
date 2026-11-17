"use client";

import { useSyncExternalStore } from "react";

import { DARK_QUERY, THEME_STORAGE_KEY } from "./theme-script";

/**
 * Light / dark / system theme without a provider library.
 *
 * The choice lives in localStorage. An inline script in <head> (see
 * theme-script.ts) applies it before first paint; this module keeps the <html>
 * class in step afterwards and lets components read and change the choice.
 */
export type ThemeChoice = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

const listeners = new Set<() => void>();

function readChoice(): ThemeChoice {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

function systemTheme(): ResolvedTheme {
  return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
}

/** Put the stored choice on <html>. Safe to call any number of times. */
export function applyTheme(): void {
  const choice = readChoice();
  const resolved = choice === "system" ? systemTheme() : choice;
  document.documentElement.classList.toggle("dark", resolved === "dark");
}

export function setTheme(choice: ThemeChoice): void {
  try {
    if (choice === "system") window.localStorage.removeItem(THEME_STORAGE_KEY);
    else window.localStorage.setItem(THEME_STORAGE_KEY, choice);
  } catch {
    // Storage is unavailable (private mode): the choice still applies to this page.
  }
  applyTheme();
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  const onExternalChange = () => {
    applyTheme();
    listener();
  };
  listeners.add(listener);
  const mql = window.matchMedia(DARK_QUERY);
  mql.addEventListener("change", onExternalChange);
  window.addEventListener("storage", onExternalChange);
  return () => {
    listeners.delete(listener);
    mql.removeEventListener("change", onExternalChange);
    window.removeEventListener("storage", onExternalChange);
  };
}

const getSnapshot = () => `${readChoice()}:${systemTheme()}`;
const getServerSnapshot = () => "system:light";

export function useTheme(): { theme: ThemeChoice; resolvedTheme: ResolvedTheme; setTheme: (choice: ThemeChoice) => void } {
  const [theme, system] = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot).split(":") as [
    ThemeChoice,
    ResolvedTheme,
  ];
  return { theme, resolvedTheme: theme === "system" ? system : theme, setTheme };
}
