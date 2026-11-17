"use client";

import type { ReactNode } from "react";

import { Info } from "lucide-react";

import { KPIStatCard, KpiStrip } from "@/components/shared/kpi";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useToday } from "@/hooks/use-now";
import { resolveRange } from "@/lib/date-range";
import { formatPercent, plural } from "@/lib/format";
import { DATE_RANGE_NOTE, METRIC_DEFINITIONS } from "@/lib/status";
import type { AssignmentStats, DateRange } from "@/lib/types";
import { useUrlState } from "@/lib/url-state";

export const RANGE_DEFAULTS = { range: "all", from: "", to: "" };

/** The date range of a dashboard, kept in the URL, resolved to API parameters. */
export function useDashboardRange() {
  const today = useToday();
  const [state, setState] = useUrlState(RANGE_DEFAULTS);
  const { range, ready } = resolveRange(state.range, state.from, state.to, today);
  return { state, setState, range, ready, today };
}

export function rangeQuery(range: DateRange): string {
  const params = new URLSearchParams();
  if (range.date_from) params.set("date_from", range.date_from);
  if (range.date_to) params.set("date_to", range.date_to);
  const text = params.toString();
  return text ? `&${text}` : "";
}

/** Explains, once per dashboard, what every figure on it counts (the definitions are the API's own). */
export function MetricDefinitions() {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="ghost" className="text-muted-foreground">
          <Info data-icon="inline-start" />
          How these are counted
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[min(26rem,calc(100vw-2rem))] gap-2 p-3.5 text-2sm">
        <p className="font-label text-sm">How these numbers are counted</p>
        <dl className="flex flex-col gap-1.5">
          {(["assigned", "not_applied", "applied", "interviews", "offers", "closed", "completion_rate", "link_opens"] as const).map(
            (key) => (
              <div key={key}>
                <dt className="inline font-medium text-foreground">{METRIC_DEFINITIONS[key].label}: </dt>
                <dd className="inline text-muted-foreground">{METRIC_DEFINITIONS[key].definition}</dd>
              </div>
            ),
          )}
        </dl>
        <p className="border-t pt-2 text-muted-foreground">{DATE_RANGE_NOTE}</p>
      </PopoverContent>
    </Popover>
  );
}

/**
 * The six figures every role's dashboard leads with, in pipeline order:
 * Total Assigned, Not Applied, Applied, Interviews, Offers, Rejected/Closed.
 */
export function AssignmentKpis({
  stats,
  loading,
  hrefs,
  label,
}: {
  stats: AssignmentStats | undefined;
  loading: boolean;
  /** Where each figure leads, when there is a list behind it. */
  hrefs?: Partial<Record<"assigned" | "not_applied" | "applied" | "interviews" | "offers" | "closed", string>>;
  label: string;
}) {
  const s = stats;
  const cells: Array<{
    key: keyof NonNullable<typeof hrefs>;
    label: string;
    color: string;
    definition: string;
    detail?: ReactNode;
  }> = [
    {
      key: "assigned",
      label: "Total Assigned",
      color: "var(--status-assigned)",
      definition: METRIC_DEFINITIONS.assigned.definition,
      detail: s ? `${plural(s.viewed, "job")} viewed` : undefined,
    },
    {
      key: "not_applied",
      label: "Not Applied",
      color: "var(--status-viewed)",
      definition: METRIC_DEFINITIONS.not_applied.definition,
      detail: s ? `${plural(s.link_opens, "job link open")}` : undefined,
    },
    {
      key: "applied",
      label: "Applied",
      color: "var(--status-applied)",
      definition: `${METRIC_DEFINITIONS.applied.definition} Completion rate: ${METRIC_DEFINITIONS.completion_rate.definition}`,
      detail: s ? `${formatPercent(s.completion_rate, 1)} completion rate` : undefined,
    },
    {
      key: "interviews",
      label: "Interviews",
      color: "var(--status-interviewing)",
      definition: METRIC_DEFINITIONS.interviews.definition,
    },
    { key: "offers", label: "Offers", color: "var(--status-offer)", definition: METRIC_DEFINITIONS.offers.definition },
    {
      key: "closed",
      label: "Rejected/Closed",
      color: "var(--status-rejected)",
      definition: METRIC_DEFINITIONS.closed.definition,
    },
  ];
  return (
    <KpiStrip label={label}>
      {cells.map((cell) => (
        <KPIStatCard
          key={cell.key}
          label={cell.label}
          value={s ? s[cell.key] : undefined}
          color={cell.color}
          definition={cell.definition}
          detail={cell.detail}
          href={hrefs?.[cell.key]}
          loading={loading}
        />
      ))}
    </KpiStrip>
  );
}
