"use client";

import { ChartPanel } from "@/components/charts/chart-kit";
import { InfoPopover } from "@/components/shared/info-popover";
import { formatPercent } from "@/lib/format";
import { METRIC_DEFINITIONS } from "@/lib/status";
import type { NameValue } from "@/lib/types";
import { cn } from "@/lib/utils";

/** The funnel stages the API reports, with the colour and the definition each one carries everywhere else. */
const STAGE_STYLE: Record<string, { color: string; definition: string }> = {
  Assigned: { color: "var(--status-assigned)", definition: METRIC_DEFINITIONS.assigned.definition },
  Viewed: { color: "var(--status-viewed)", definition: METRIC_DEFINITIONS.viewed.definition },
  Applied: { color: "var(--status-applied)", definition: METRIC_DEFINITIONS.applied.definition },
  Interview: { color: "var(--status-interviewing)", definition: METRIC_DEFINITIONS.interviews.definition },
  Offer: { color: "var(--status-offer)", definition: METRIC_DEFINITIONS.offers.definition },
};

/**
 * Assigned, Viewed, Applied, Interview, Offer as one rail. Every stage is a bar
 * whose length is its count against the first stage, so the narrowing of the
 * pipeline is the picture. The share of the stage before it is printed beside
 * each count.
 */
export function PipelineFunnel({
  data,
  loading,
  fetching,
  error,
  onRetry,
  title = "Pipeline",
  className,
}: {
  data: NameValue[] | undefined;
  loading?: boolean;
  fetching?: boolean;
  error?: unknown;
  onRetry?: () => void;
  title?: string;
  className?: string;
}) {
  const stages = data ?? [];
  const top = Math.max(0, ...stages.map((stage) => stage.value));
  const empty = top === 0;

  return (
    <ChartPanel
      title={title}
      description="How far the assigned jobs have travelled."
      info={
        <InfoPopover label="About the pipeline stages" title="Pipeline stages">
          {stages.map((stage) => (
            <p key={stage.name}>
              <span className="font-medium text-foreground">{stage.name}:</span>{" "}
              {STAGE_STYLE[stage.name]?.definition ?? ""}
            </p>
          ))}
          <p>The percentage beside a stage is its share of the stage before it.</p>
        </InfoPopover>
      }
      loading={loading}
      fetching={fetching}
      error={error}
      onRetry={onRetry}
      empty={empty}
      emptyTitle="No jobs in the pipeline yet"
      emptyDescription="Once jobs are assigned, this shows how many were viewed, applied to, interviewed for and offered."
      table={{
        columns: ["Stage", "Assignments", "Share of previous stage"],
        rows: stages.map((stage, index) => [
          stage.name,
          stage.value,
          index === 0 || stages[index - 1].value === 0
            ? "—"
            : formatPercent((100 * stage.value) / stages[index - 1].value),
        ]),
      }}
      className={className}
    >
      <ol className="grid gap-x-5 gap-y-4 sm:grid-cols-5">
        {stages.map((stage, index) => {
          const previous = index > 0 ? stages[index - 1].value : 0;
          const share = index > 0 && previous > 0 ? (100 * stage.value) / previous : null;
          const width = top > 0 ? Math.max(stage.value > 0 ? 3 : 0, (100 * stage.value) / top) : 0;
          const color = STAGE_STYLE[stage.name]?.color ?? "var(--status-closed)";
          return (
            <li key={stage.name} className="flex min-w-0 flex-col gap-2">
              <div className="flex items-baseline justify-between gap-2 sm:flex-col sm:items-start sm:gap-1">
                <span className="text-2sm text-muted-foreground">{stage.name}</span>
                <span className="flex items-baseline gap-2">
                  <span className="font-figure text-3xl leading-none sm:text-4xl">{stage.value.toLocaleString()}</span>
                  {share !== null && (
                    <span className="text-xs text-muted-foreground tabular-nums">
                      {formatPercent(share)}
                      <span className="sr-only"> of {stages[index - 1].name}</span>
                    </span>
                  )}
                </span>
              </div>
              <div className="h-2.5 overflow-hidden rounded-[3px] bg-foreground/8" aria-hidden="true">
                <div
                  className={cn("h-full origin-left rounded-[3px] motion-safe:animate-rail-fill")}
                  style={{ width: `${width}%`, backgroundColor: color, animationDelay: `${index * 70}ms` }}
                />
              </div>
            </li>
          );
        })}
      </ol>
    </ChartPanel>
  );
}
