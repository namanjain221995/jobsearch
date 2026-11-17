"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChartNoAxesColumn, Table2, type LucideIcon } from "lucide-react";

import { Panel } from "@/components/shared/page-header";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ series */

/** One colour per pipeline stage, the same tokens the status badges and stage rails use. */
export const SERIES = {
  assigned: { label: "Assigned", color: "var(--status-assigned)" },
  link_opens: { label: "Job link opens", color: "var(--status-viewed)" },
  applied: { label: "Applied", color: "var(--status-applied)" },
  interviews: { label: "Interviews", color: "var(--status-interviewing)" },
  offers: { label: "Offers", color: "var(--status-offer)" },
} as const;

export type SeriesKey = keyof typeof SERIES;

export interface SeriesSpec {
  key: string;
  label: string;
  color: string;
}

/* ------------------------------------------------------------------ sizing */

/**
 * Width of the element a chart is drawn into. Charts are only rendered once a
 * real width is known, and a width of zero (a hidden screen) is ignored, so a
 * chart never tries to draw itself into nothing.
 */
export function useChartWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const observer = new ResizeObserver((entries) => {
      const next = Math.floor(entries[0].contentRect.width);
      if (next > 0) setWidth(next);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

export function ChartCanvas({
  height,
  children,
  label,
}: {
  height: number;
  /** Receives the measured width; return the Recharts chart. */
  children: (width: number) => ReactNode;
  /** Sentence describing the chart for screen readers. The same data is available as a table. */
  label: string;
}) {
  const [ref, width] = useChartWidth<HTMLDivElement>();
  return (
    <div ref={ref} role="img" aria-label={label} style={{ height }} className="w-full min-w-0 [&_.recharts-surface]:outline-none">
      {width > 0 ? children(width) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ legend */

/** Legend whose entries are also switches: press one to hide or show that series. */
export function ChartLegend({
  series,
  hidden,
  onToggle,
  mark = "line",
  className,
}: {
  series: SeriesSpec[];
  hidden?: ReadonlySet<string>;
  onToggle?: (key: string) => void;
  mark?: "line" | "bar";
  className?: string;
}) {
  return (
    <ul className={cn("flex flex-wrap gap-x-1 gap-y-1", className)}>
      {series.map((item) => {
        const off = hidden?.has(item.key) ?? false;
        const swatch = (
          <span
            aria-hidden="true"
            className={cn("shrink-0 rounded-[2px]", mark === "line" ? "h-0.5 w-3.5" : "size-2.5", off && "opacity-30")}
            style={{ backgroundColor: item.color }}
          />
        );
        return (
          <li key={item.key}>
            {onToggle ? (
              <button
                type="button"
                aria-pressed={!off}
                onClick={() => onToggle(item.key)}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-sm px-1.5 py-0.5 text-xs hover:bg-muted",
                  off ? "text-muted-foreground line-through" : "text-foreground",
                )}
              >
                {swatch}
                {item.label}
              </button>
            ) : (
              <span className="inline-flex items-center gap-1.5 px-1.5 py-0.5 text-xs text-foreground">
                {swatch}
                {item.label}
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** Keeps which series are switched off. At least one always stays on. */
export function useHiddenSeries(keys: string[], initiallyHidden: string[] = []) {
  const [hidden, setHidden] = useState<ReadonlySet<string>>(() => new Set(initiallyHidden));
  const toggle = (key: string) =>
    setHidden((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else if (keys.length - next.size > 1) next.add(key);
      return next;
    });
  return [hidden, toggle] as const;
}

/* ------------------------------------------------------------------ tooltip */

interface TooltipRow {
  key: string;
  label: string;
  color: string;
  value: number | string;
}

/** One tooltip for every series at the hovered position. The value leads; the name follows. */
export function ChartTooltipCard({ title, rows }: { title: string; rows: TooltipRow[] }) {
  return (
    <div className="min-w-36 rounded-md border border-border-strong bg-popover px-2.5 py-2 text-xs text-popover-foreground shadow-md">
      <p className="mb-1.5 font-label text-foreground">{title}</p>
      <ul className="flex flex-col gap-1">
        {rows.map((row) => (
          <li key={row.key} className="flex items-center gap-2">
            <span aria-hidden="true" className="h-0.5 w-3 shrink-0 rounded-full" style={{ backgroundColor: row.color }} />
            <span className="text-muted-foreground">{row.label}</span>
            <span className="ml-auto pl-3 font-semibold text-foreground tabular-nums">{row.value}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* ------------------------------------------------------------------ panel */

export interface ChartTable {
  columns: string[];
  rows: Array<Array<string | number>>;
}

/**
 * Frame for every chart: title, optional definition, the chart or an honest empty
 * state, and a switch to the same data as a table (the accessible twin, and the
 * place where exact values live).
 */
export function ChartPanel({
  title,
  description,
  info,
  actions,
  loading,
  fetching,
  error,
  onRetry,
  empty,
  emptyIcon = ChartNoAxesColumn,
  emptyTitle = "Nothing to chart yet",
  emptyDescription,
  table,
  children,
  className,
}: {
  title: string;
  description?: ReactNode;
  info?: ReactNode;
  actions?: ReactNode;
  loading?: boolean;
  /** Fresh data is being fetched: keep the current picture, slightly dimmed. */
  fetching?: boolean;
  error?: unknown;
  onRetry?: () => void;
  /** True when the series has no data. An empty state is shown instead of an empty or invented chart. */
  empty: boolean;
  emptyIcon?: LucideIcon;
  emptyTitle?: string;
  emptyDescription?: ReactNode;
  table?: ChartTable;
  children: ReactNode;
  className?: string;
}) {
  const [asTable, setAsTable] = useState(false);
  const showToggle = Boolean(table) && !loading && !error && !empty;

  let body: ReactNode;
  if (loading) body = <LoadingSkeleton variant="chart" />;
  else if (error) body = <ErrorState compact error={error} onRetry={onRetry} />;
  else if (empty) body = <EmptyState compact icon={emptyIcon} title={emptyTitle} description={emptyDescription} />;
  else if (asTable && table) {
    body = (
      <div className="max-h-72 overflow-auto rounded-md border" tabIndex={0} role="group" aria-label={`${title}, as a table`}>
        <table className="w-full border-collapse text-2sm">
          <caption className="sr-only">{title}</caption>
          <thead className="sticky top-0 bg-card">
            <tr className="border-b">
              {table.columns.map((column, index) => (
                <th
                  key={column}
                  scope="col"
                  className={cn("px-2.5 py-1.5 font-label text-muted-foreground", index === 0 ? "text-left" : "text-right")}
                >
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row, rowIndex) => (
              <tr key={rowIndex} className="border-b last:border-b-0">
                {row.map((cell, index) =>
                  index === 0 ? (
                    <th key={index} scope="row" className="px-2.5 py-1.5 text-left font-normal whitespace-nowrap">
                      {cell}
                    </th>
                  ) : (
                    <td key={index} className="px-2.5 py-1.5 text-right tabular-nums">
                      {typeof cell === "number" ? cell.toLocaleString() : cell}
                    </td>
                  ),
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  } else body = <div className={cn(fetching && "opacity-60 transition-opacity")}>{children}</div>;

  return (
    <Panel
      title={title}
      description={description}
      info={info}
      className={className}
      actions={
        <>
          {actions}
          {showToggle && (
            <Button
              variant="ghost"
              size="xs"
              aria-pressed={asTable}
              onClick={() => setAsTable((value) => !value)}
              className="text-muted-foreground"
            >
              {asTable ? <ChartNoAxesColumn data-icon="inline-start" /> : <Table2 data-icon="inline-start" />}
              {asTable ? "Chart" : "Table"}
            </Button>
          )}
        </>
      }
    >
      {body}
    </Panel>
  );
}

/* ------------------------------------------------------------------ shared axis styling */

export const AXIS_TICK = { fill: "var(--muted-foreground)", fontSize: 12 } as const;
export const GRID_STROKE = "var(--chart-grid)";
