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

const isDay = (value: string) => /^\d{4}-\d{2}-\d{2}$/.test(value);

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
    return { range: { date_from: isDay(from) ? from : undefined, date_to: isDay(to) ? to : undefined }, ready: true };
  }
  const days = RANGE_PRESETS.find((item) => item.value === preset)?.days;
  if (!days) return { range: {}, ready: true };
  if (!today) return { range: {}, ready: false };
  return { range: { date_from: shiftDay(today, -(days - 1)) }, ready: true };
}
