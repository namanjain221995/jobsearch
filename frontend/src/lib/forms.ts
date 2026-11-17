import type { FieldValues, Path, UseFormReturn } from "react-hook-form";

import { ApiError } from "./api";

/**
 * Put the API's per-field validation messages (`fields` in the error body) on the
 * matching form fields. Returns true when at least one message found its field.
 */
export function applyServerErrors<T extends FieldValues>(form: UseFormReturn<T>, error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  const known = new Set(Object.keys(form.getValues()));
  let applied = false;
  for (const [name, message] of Object.entries(error.fields)) {
    const root = name.split(".")[0];
    if (!known.has(root)) continue;
    form.setError(name as Path<T>, { type: "server", message });
    applied = true;
  }
  return applied;
}

/** Turn "a, b\nc" into ["a", "b", "c"]: for the comma separated inputs (titles, states, companies). */
export function splitList(value: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const part of value.split(/[,\n]/)) {
    const item = part.trim();
    const key = item.toLowerCase();
    if (!item || seen.has(key)) continue;
    seen.add(key);
    out.push(item);
  }
  return out;
}
