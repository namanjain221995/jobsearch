"use client";

import { CircleDashed, PieChart, Users } from "lucide-react";

import { ChartPanel } from "@/components/charts/chart-kit";
import { InfoPopover } from "@/components/shared/info-popover";
import { formatPercent, humanize } from "@/lib/format";
import { isAssignmentStatus, METRIC_DEFINITIONS, STATUS_META } from "@/lib/status";
import type { NameValue } from "@/lib/types";

interface PanelProps {
  data: NameValue[] | undefined;
  loading?: boolean;
  fetching?: boolean;
  error?: unknown;
  onRetry?: () => void;
  className?: string;
}

interface BarRow {
  key: string;
  label: string;
  value: number;
  color: string;
  icon?: typeof CircleDashed;
}

/** Horizontal bars on one baseline: a name, a bar, a figure per row. Lengths compare exactly. */
function BarList({ rows, total, unit }: { rows: BarRow[]; total: number; unit: string }) {
  const top = Math.max(1, ...rows.map((row) => row.value));
  return (
    <ul className="flex flex-col gap-2.5">
      {rows.map((row) => {
        const share = total > 0 ? (100 * row.value) / total : 0;
        return (
          <li key={row.key} className="grid grid-cols-[minmax(6.5rem,auto)_1fr_auto] items-center gap-x-3 text-sm">
            <span className="flex items-center gap-1.5 truncate">
              {row.icon && <row.icon className="size-3.5 shrink-0" style={{ color: row.color }} aria-hidden="true" />}
              <span className="truncate">{row.label}</span>
            </span>
            <span className="h-2.5 overflow-hidden rounded-[3px] bg-foreground/6" aria-hidden="true">
              <span
                className="block h-full rounded-[3px]"
                style={{ width: `${Math.max(row.value > 0 ? 2 : 0, (100 * row.value) / top)}%`, backgroundColor: row.color }}
              />
            </span>
            <span className="min-w-16 text-right tabular-nums">
              <span className="font-semibold">{row.value.toLocaleString()}</span>
              <span className="ml-1.5 text-xs text-muted-foreground">
                {formatPercent(share)}
                <span className="sr-only"> of {unit}</span>
              </span>
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** Where every active assignment stands right now, one bar per status, in pipeline order. */
export function StatusDistributionChart({ data, loading, fetching, error, onRetry, className }: PanelProps) {
  const rows: BarRow[] = (data ?? []).map((item) => {
    const meta = isAssignmentStatus(item.name) ? STATUS_META[item.name] : null;
    return {
      key: item.name,
      label: meta?.label ?? humanize(item.name),
      value: item.value,
      color: meta?.color ?? "var(--status-closed)",
      icon: meta?.icon ?? CircleDashed,
    };
  });
  const total = rows.reduce((sum, row) => sum + row.value, 0);

  return (
    <ChartPanel
      title="Status distribution"
      description="Active assignments by their current status."
      info={
        <InfoPopover label="About the statuses" title="Statuses">
          {rows.map((row) => (
            <p key={row.key}>
              <span className="font-medium text-foreground">{row.label}:</span>{" "}
              {isAssignmentStatus(row.key) ? STATUS_META[row.key].meaning : ""}
            </p>
          ))}
        </InfoPopover>
      }
      loading={loading}
      fetching={fetching}
      error={error}
      onRetry={onRetry}
      empty={total === 0}
      emptyIcon={PieChart}
      emptyTitle="No assignments to break down"
      emptyDescription="Each status gets a bar here as soon as jobs are assigned."
      table={{
        columns: ["Status", "Assignments", "Share"],
        rows: rows.map((row) => [row.label, row.value, formatPercent(total ? (100 * row.value) / total : 0, 1)]),
      }}
      className={className}
    >
      <BarList rows={rows} total={total} unit="all assignments" />
    </ChartPanel>
  );
}

/**
 * One ratio, so one meter: the share of open assignments that have a confirmed
 * application. Two slices of a pie would say the same with less precision.
 */
export function PendingVsAppliedChart({ data, loading, fetching, error, onRetry, className }: PanelProps) {
  const applied = data?.find((item) => item.name === "Applied")?.value ?? 0;
  const pending = data?.find((item) => item.name === "Not applied")?.value ?? 0;
  const total = applied + pending;
  const share = total > 0 ? (100 * applied) / total : 0;

  return (
    <ChartPanel
      title="Pending vs applied"
      description="Assignments waiting for an application beside those with one confirmed."
      info={
        <InfoPopover label="About pending and applied" title="Pending vs applied">
          <p>
            <span className="font-medium text-foreground">Not applied:</span> {METRIC_DEFINITIONS.not_applied.definition}
          </p>
          <p>
            <span className="font-medium text-foreground">Applied:</span> {METRIC_DEFINITIONS.applied.definition}
          </p>
        </InfoPopover>
      }
      loading={loading}
      fetching={fetching}
      error={error}
      onRetry={onRetry}
      empty={total === 0}
      emptyIcon={PieChart}
      emptyTitle="Nothing pending and nothing applied"
      emptyDescription="This fills in once jobs are assigned."
      table={{
        columns: ["", "Assignments", "Share"],
        rows: [
          ["Applied", applied, formatPercent(share, 1)],
          ["Not applied", pending, formatPercent(100 - share, 1)],
        ],
      }}
      className={className}
    >
      <div className="flex flex-col gap-4">
        <p className="flex items-baseline gap-2">
          <span className="font-figure text-4xl leading-none">{formatPercent(share)}</span>
          <span className="text-sm text-muted-foreground">applied</span>
        </p>
        <div
          role="img"
          aria-label={`${applied} applied and ${pending} not applied, out of ${total}.`}
          className="flex h-3 gap-0.5 overflow-hidden rounded-[3px]"
        >
          {applied > 0 && <span style={{ flexGrow: applied, backgroundColor: "var(--status-applied)" }} />}
          {pending > 0 && <span style={{ flexGrow: pending, backgroundColor: "var(--status-assigned)" }} />}
        </div>
        <dl className="grid grid-cols-2 gap-3 text-sm">
          <div className="flex flex-col gap-0.5">
            <dt className="flex items-center gap-1.5 text-muted-foreground">
              <span aria-hidden="true" className="size-2.5 rounded-[2px] bg-status-applied" />
              Applied
            </dt>
            <dd className="font-figure text-2xl leading-none">{applied.toLocaleString()}</dd>
          </div>
          <div className="flex flex-col gap-0.5">
            <dt className="flex items-center gap-1.5 text-muted-foreground">
              <span aria-hidden="true" className="size-2.5 rounded-[2px] bg-status-assigned" />
              Not applied
            </dt>
            <dd className="font-figure text-2xl leading-none">{pending.toLocaleString()}</dd>
          </div>
        </dl>
      </div>
    </ChartPanel>
  );
}

const OUTCOME_COLOR: Record<string, string> = {
  "Interviewing now": "var(--status-interviewing)",
  Offer: "var(--status-offer)",
  "Rejected after interview": "var(--status-rejected)",
  "Rejected before interview": "var(--status-closed)",
};

/** How applications ended, or where they stand, once employers responded. Rendered only when there is data. */
export function InterviewOutcomesChart({ data, loading, fetching, error, onRetry, className }: PanelProps) {
  const rows: BarRow[] = (data ?? []).map((item) => ({
    key: item.name,
    label: item.name,
    value: item.value,
    color: OUTCOME_COLOR[item.name] ?? "var(--status-closed)",
  }));
  const total = rows.reduce((sum, row) => sum + row.value, 0);
  return (
    <ChartPanel
      title="Interview outcomes"
      description="What happened after applying."
      loading={loading}
      fetching={fetching}
      error={error}
      onRetry={onRetry}
      empty={total === 0}
      emptyIcon={Users}
      emptyTitle="No interview outcomes yet"
      emptyDescription="Interviews, offers and rejections appear here as they are recorded."
      table={{ columns: ["Outcome", "Assignments"], rows: rows.map((row) => [row.label, row.value]) }}
      className={className}
    >
      <BarList rows={rows} total={total} unit="these outcomes" />
    </ChartPanel>
  );
}
