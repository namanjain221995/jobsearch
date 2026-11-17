"use client";

import { useCallback, useState } from "react";

/**
 * The same shape as useUrlState, kept in component state. For lists embedded in
 * another screen (a member's assignments inside the member page), where the URL
 * already belongs to that screen.
 */
export function useLocalListState<T extends Record<string, string>>(
  defaults: T,
): [T, (patch: Partial<Record<keyof T, string>>) => void] {
  const [state, setState] = useState<T>(defaults);
  const update = useCallback(
    (patch: Partial<Record<keyof T, string>>) =>
      setState((current) => {
        const next: Record<string, string> = { ...current };
        for (const [key, value] of Object.entries(patch) as Array<[string, string | undefined]>) {
          next[key] = value === undefined || value === "" ? defaults[key] : value;
        }
        // Any change to a filter starts again from the first page.
        if (!("page" in patch) && "page" in defaults) next.page = defaults.page;
        return next as T;
      }),
    [defaults],
  );
  return [state, update];
}

export type ListState<T extends Record<string, string>> = [T, (patch: Partial<Record<keyof T, string>>) => void];
