"use client";

import { useSearchParams } from "next/navigation";
import { useCallback, useMemo } from "react";

/**
 * Filters, sorting and the page number live in the query string, so a filtered
 * list can be reloaded, bookmarked and reached from the sidebar. Values equal to
 * their default are left out of the URL.
 *
 * `defaults` must be a constant (declare it outside the component). Components
 * using this hook read the URL, so they render inside a <Suspense> boundary.
 */
export function useUrlState<T extends Record<string, string>>(
  defaults: T,
): [T, (patch: Partial<Record<keyof T, string>>) => void] {
  const searchParams = useSearchParams();

  const state = useMemo(() => {
    const next: Record<string, string> = { ...defaults };
    for (const key of Object.keys(defaults)) {
      const value = searchParams.get(key);
      if (value !== null) next[key] = value;
    }
    return next as T;
  }, [searchParams, defaults]);

  const update = useCallback(
    (patch: Partial<Record<keyof T, string>>) => {
      const params = new URLSearchParams(window.location.search);
      for (const [key, value] of Object.entries(patch) as Array<[string, string | undefined]>) {
        if (value === undefined || value === "" || value === defaults[key]) params.delete(key);
        else params.set(key, value);
      }
      // Any change to a filter starts again from the first page.
      if (!("page" in patch)) params.delete("page");
      const query = params.toString();
      // Next.js keeps useSearchParams() in step with history.replaceState.
      window.history.replaceState(null, "", query ? `?${query}` : window.location.pathname);
    },
    [defaults],
  );

  return [state, update];
}

export function toPositiveInt(value: string, fallback: number): number {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}
