"use client";

import { Bar, BarChart, CartesianGrid, Line, LineChart, Tooltip, XAxis, YAxis, type TooltipContentProps } from "recharts";
import { Search, Users } from "lucide-react";

import {
  AXIS_TICK,
  ChartCanvas,
  ChartLegend,
  ChartPanel,
  ChartTooltipCard,
  GRID_STROKE,
  type SeriesSpec,
} from "@/components/charts/chart-kit";
import { formatDate, formatDayShort, plural } from "@/lib/format";
import type { NameValue, SearchUsagePoint } from "@/lib/types";

interface BaseProps {
  loading?: boolean;
  fetching?: boolean;
  error?: unknown;
  onRetry?: () => void;
  className?: string;
}

const RUNS: SeriesSpec = { key: "runs", label: "Searches started", color: "var(--status-applied)" };
const RESULT_SERIES: SeriesSpec[] = [
  { key: "matched", label: "Jobs matched", color: "var(--status-viewed)" },
  { key: "assigned", label: "Jobs assigned", color: "var(--status-interviewing)" },
];

function UsageTooltip({ active, payload, series }: TooltipContentProps & { series: SeriesSpec[] }) {
  if (!active || !payload?.length) return null;
  const point = payload[0]?.payload as SearchUsagePoint | undefined;
  if (!point) return null;
  return (
    <ChartTooltipCard
      title={formatDate(point.label)}
      rows={series.map((item) => ({ ...item, value: point[item.key as "runs" | "matched" | "assigned"].toLocaleString() }))}
    />
  );
}

const axis = {
  tick: AXIS_TICK,
  tickLine: false,
} as const;

/**
 * Searches per day, and what they produced. Runs and jobs differ by an order of
 * magnitude, so they are two charts on a shared time axis and not two scales
 * forced onto one plot.
 */
export function SearchUsageChart({ data, ...rest }: BaseProps & { data: SearchUsagePoint[] | undefined }) {
  const points = data ?? [];
  const empty = !points.some((point) => point.runs > 0 || point.matched > 0 || point.assigned > 0);
  const totalRuns = points.reduce((sum, point) => sum + point.runs, 0);

  return (
    <ChartPanel
      title="AI search usage"
      description={`Last 30 days (UTC). ${empty ? "" : plural(totalRuns, "search", "searches") + " started."}`}
      empty={empty}
      emptyIcon={Search}
      emptyTitle="No searches in the last 30 days"
      emptyDescription="Each search a Team Lead or Admin starts is counted here, with the jobs it matched and assigned."
      table={{
        columns: ["Day", "Searches started", "Jobs matched", "Jobs assigned"],
        rows: points.filter((p) => p.runs || p.matched || p.assigned).map((p) => [formatDate(p.label), p.runs, p.matched, p.assigned]),
      }}
      {...rest}
    >
      <div className="grid gap-5 lg:grid-cols-2">
        <figure className="flex min-w-0 flex-col gap-2">
          <figcaption className="text-2sm text-muted-foreground">Searches started per day</figcaption>
          <ChartCanvas height={190} label={`Searches started per day over ${points.length} days. Column chart.`}>
            {(width) => (
              <BarChart width={width} height={190} data={points} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid vertical={false} stroke={GRID_STROKE} />
                <XAxis
                  dataKey="label"
                  tickFormatter={formatDayShort}
                  axisLine={{ stroke: "var(--border-strong)" }}
                  minTickGap={28}
                  tickMargin={8}
                  interval="preserveStartEnd"
                  {...axis}
                />
                <YAxis allowDecimals={false} axisLine={false} width={30} {...axis} />
                <Tooltip
                  isAnimationActive={false}
                  cursor={{ fill: "var(--muted)", opacity: 0.7 }}
                  content={(props) => <UsageTooltip {...props} series={[RUNS]} />}
                />
                <Bar dataKey="runs" name={RUNS.label} fill={RUNS.color} maxBarSize={18} radius={[4, 4, 0, 0]} isAnimationActive={false} />
              </BarChart>
            )}
          </ChartCanvas>
        </figure>
        <figure className="flex min-w-0 flex-col gap-2">
          <figcaption className="flex flex-wrap items-center justify-between gap-2 text-2sm text-muted-foreground">
            Jobs those searches produced
            <ChartLegend series={RESULT_SERIES} />
          </figcaption>
          <ChartCanvas height={190} label={`Jobs matched and assigned per day over ${points.length} days. Line chart.`}>
            {(width) => (
              <LineChart width={width} height={190} data={points} margin={{ top: 6, right: 12, bottom: 0, left: 0 }}>
                <CartesianGrid vertical={false} stroke={GRID_STROKE} />
                <XAxis
                  dataKey="label"
                  tickFormatter={formatDayShort}
                  axisLine={{ stroke: "var(--border-strong)" }}
                  minTickGap={28}
                  tickMargin={8}
                  interval="preserveStartEnd"
                  {...axis}
                />
                <YAxis allowDecimals={false} axisLine={false} width={34} {...axis} />
                <Tooltip
                  isAnimationActive={false}
                  cursor={{ stroke: "var(--border-strong)", strokeWidth: 1 }}
                  content={(props) => <UsageTooltip {...props} series={RESULT_SERIES} />}
                />
                {RESULT_SERIES.map((item) => (
                  <Line
                    key={item.key}
                    type="linear"
                    dataKey={item.key}
                    name={item.label}
                    stroke={item.color}
                    strokeWidth={2}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    dot={false}
                    activeDot={{ r: 4, stroke: "var(--card)", strokeWidth: 2 }}
                    isAnimationActive={false}
                  />
                ))}
              </LineChart>
            )}
          </ChartCanvas>
        </figure>
      </div>
    </ChartPanel>
  );
}

function BandTooltip({ active, payload, noun }: TooltipContentProps & { noun: string }) {
  if (!active || !payload?.length) return null;
  const point = payload[0]?.payload as NameValue | undefined;
  if (!point) return null;
  return (
    <ChartTooltipCard
      title={`Completion rate ${point.name}`}
      rows={[{ key: "count", label: noun, color: "var(--status-applied)", value: point.value.toLocaleString() }]}
    />
  );
}

/** How many people sit in each completion-rate band. One series, so one colour and no legend. */
function CompletionBands({
  data,
  noun,
  description,
  emptyDescription,
  ...rest
}: BaseProps & { data: NameValue[] | undefined; noun: "Members" | "Candidates"; description: string; emptyDescription: string }) {
  const points = data ?? [];
  const total = points.reduce((sum, point) => sum + point.value, 0);
  const lower = noun.toLowerCase();
  return (
    <ChartPanel
      title={`${noun} by completion rate`}
      description={description}
      empty={total === 0}
      emptyIcon={Users}
      emptyTitle={`No ${lower} with assignments`}
      emptyDescription={emptyDescription}
      table={{ columns: ["Completion rate", noun], rows: points.map((point) => [point.name, point.value]) }}
      {...rest}
    >
      <ChartCanvas height={220} label={`${noun} by completion rate band. Column chart, ${total} ${lower} in ${points.length} bands.`}>
        {(width) => (
          <BarChart width={width} height={220} data={points} margin={{ top: 6, right: 8, bottom: 0, left: 0 }} barCategoryGap="30%">
            <CartesianGrid vertical={false} stroke={GRID_STROKE} />
            <XAxis dataKey="name" axisLine={{ stroke: "var(--border-strong)" }} tickMargin={8} {...axis} />
            <YAxis allowDecimals={false} axisLine={false} width={30} {...axis} />
            <Tooltip isAnimationActive={false} cursor={{ fill: "var(--muted)", opacity: 0.7 }} content={(props) => <BandTooltip {...props} noun={noun} />} />
            <Bar dataKey="value" name={noun} fill="var(--status-applied)" maxBarSize={24} radius={[4, 4, 0, 0]} isAnimationActive={false} />
          </BarChart>
        )}
      </ChartCanvas>
    </ChartPanel>
  );
}

/** Responsible members per completion-rate band, over the jobs of the candidates each holds today. */
export function MemberDistributionChart(props: BaseProps & { data: NameValue[] | undefined }) {
  return (
    <CompletionBands
      noun="Members"
      description="Responsible members whose candidates have at least one assignment, grouped by how much of it has been applied to."
      emptyDescription="Members are grouped by completion rate once their candidates have jobs assigned."
      {...props}
    />
  );
}

/** Candidates per completion-rate band. Candidates without a responsible member are included. */
export function CandidateDistributionChart(props: BaseProps & { data: NameValue[] | undefined }) {
  return (
    <CompletionBands
      noun="Candidates"
      description="Candidates with at least one assignment, grouped by how much of it has been applied to."
      emptyDescription="Candidates are grouped by completion rate once they have jobs assigned."
      {...props}
    />
  );
}
