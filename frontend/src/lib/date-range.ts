import type { DateRange } from "./types";

export type RangePreset = "all" | "7d" | "30d" | "90d" | "custom";

export const RANGE_PRESETS: Array<{ value: RangePreset; label: string; days?: number }> = [
  { value: "all", label: "All time" },
  { value: "7d", label: "Last 7 days", days: 7 },
  { value: "30d", label: "Last 30 days", days: 30 },
  { value: "90d", label: "Last 90 days", days: 90 },
  { value: "custom", label: "Custom range" },
];

function shiftDay(day: string, deltaDays: number): string {
  const [year, month, date] = day.split("-").map(Number);
  const shifted = new Date(year, month - 1, date + deltaDays);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${shifted.getFullYear()}-${pad(shifted.getMonth() + 1)}-${pad(shifted.getDate())}`;
}

/** The API accepts date filters from 2000-01-01 to 2100-12-31 and answers 422 for anything else. */
export const DAY_MIN = "2000-01-01";
export const DAY_MAX = "2100-12-31";

/** A real calendar day (YYYY-MM-DD) that the API accepts as a date filter. */
export function isApiDay(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value < DAY_MIN || value > DAY_MAX) return false;
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

/**
 * The API parameters for a chosen range. `today` is null until the browser has
 * hydrated; a rolling preset is "not ready" until then, so no request is made
 * with the wrong dates.
 */
export function resolveRange(
  preset: string,
  from: string,
  to: string,
  today: string | null,
): { range: DateRange; ready: boolean } {
  if (preset === "custom") {
    return { range: { date_from: isApiDay(from) ? from : undefined, date_to: isApiDay(to) ? to : undefined }, ready: true };
  }
  const days = RANGE_PRESETS.find((item) => item.value === preset)?.days;
  if (!days) return { range: {}, ready: true };
  if (!today) return { range: {}, ready: false };
  return { range: { date_from: shiftDay(today, -(days - 1)) }, ready: true };
}
