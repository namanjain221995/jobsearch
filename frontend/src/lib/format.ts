/**
 * Formatting for dates, numbers and labels. Everything that depends on the
 * viewer's locale or time zone is only ever called with API data in the browser,
 * so server-rendered HTML never contains a formatted date.
 */

const DATE: Intl.DateTimeFormatOptions = { year: "numeric", month: "short", day: "numeric" };
const DATE_TIME: Intl.DateTimeFormatOptions = { ...DATE, hour: "numeric", minute: "2-digit" };
const TIME: Intl.DateTimeFormatOptions = { hour: "numeric", minute: "2-digit" };

function toDate(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** A calendar day the API sends as "YYYY-MM-DD" is shown as that day, whatever the viewer's time zone. */
function parseDay(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return toDate(value);
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
}

export function formatDate(value: string | Date | null | undefined, fallback = "—"): string {
  const date = typeof value === "string" ? parseDay(value) : toDate(value);
  return date ? date.toLocaleDateString(undefined, DATE) : fallback;
}

export function formatDateTime(value: string | Date | null | undefined, fallback = "—"): string {
  const date = toDate(value);
  return date ? date.toLocaleString(undefined, DATE_TIME) : fallback;
}

export function formatTime(value: string | Date | null | undefined, fallback = "—"): string {
  const date = toDate(value);
  return date ? date.toLocaleTimeString(undefined, TIME) : fallback;
}

/** Short axis label for a chart day ("Nov 3"). */
export function formatDayShort(value: string): string {
  const date = parseDay(value);
  return date ? date.toLocaleDateString(undefined, { month: "short", day: "numeric" }) : value;
}

const RELATIVE_STEPS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
];

/** "3 days ago". `now` comes from useNow() so the text stays current without a refetch. */
export function formatRelative(value: string | Date | null | undefined, now: number | null, fallback = "—"): string {
  const date = toDate(value);
  if (!date) return fallback;
  if (now === null) return formatDate(date);
  const seconds = Math.round((date.getTime() - now) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 45) return "just now";
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, size] of RELATIVE_STEPS) {
    if (abs >= size) return formatter.format(Math.round(seconds / size), unit);
  }
  return formatter.format(Math.round(seconds / 60), "minute");
}

export function formatNumber(value: number | null | undefined, fallback = "—"): string {
  if (value === null || value === undefined || Number.isNaN(value)) return fallback;
  return value.toLocaleString();
}

/** 1,284 stays in full; 12,900 becomes 12.9K. For figures on stat tiles. */
export function formatCompact(value: number | null | undefined, fallback = "—"): string {
  if (value === null || value === undefined || Number.isNaN(value)) return fallback;
  if (Math.abs(value) < 10_000) return value.toLocaleString();
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

export function formatPercent(value: number | null | undefined, digits = 0, fallback = "—"): string {
  if (value === null || value === undefined || Number.isNaN(value)) return fallback;
  return `${value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 })}%`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatDuration(startIso: string | null | undefined, endIso: string | null | undefined): string {
  const start = toDate(startIso);
  const end = toDate(endIso);
  if (!start || !end) return "—";
  const seconds = Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function initials(name: string | null | undefined): string {
  const parts = (name ?? "").trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

/** Value for <input type="date"> from an ISO timestamp, in the viewer's own calendar. */
export function toDateInput(value: string | Date | null | undefined): string {
  const date = toDate(value);
  if (!date) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** Value for <input type="datetime-local"> from a Date, in the viewer's own time zone. */
export function toDateTimeInput(value: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${toDateInput(value)}T${pad(value.getHours())}:${pad(value.getMinutes())}`;
}

export function daysAgoInput(days: number, from = new Date()): string {
  const date = new Date(from.getFullYear(), from.getMonth(), from.getDate() - days);
  return toDateInput(date);
}

export const ROLE_LABELS = { ADMIN: "Admin", TEAM_LEAD: "Team Lead", MEMBER: "Member" } as const;

export function roleLabel(role: string | null | undefined): string {
  return role && role in ROLE_LABELS ? ROLE_LABELS[role as keyof typeof ROLE_LABELS] : (role ?? "");
}

/** "status_changed" -> "Status changed". For event and audit names the UI has no better words for. */
export function humanize(value: string | null | undefined): string {
  const text = (value ?? "").replace(/[._]+/g, " ").trim();
  return text ? text[0].toUpperCase() + text.slice(1) : "";
}
