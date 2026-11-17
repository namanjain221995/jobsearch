"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
} from "recharts";

import {
  AXIS_TICK,
  ChartCanvas,
  ChartLegend,
  ChartPanel,
  ChartTooltipCard,
  GRID_STROKE,
  SERIES,
  useHiddenSeries,
  type SeriesKey,
  type SeriesSpec,
} from "@/components/charts/chart-kit";
import { formatDate, formatDayShort } from "@/lib/format";
import type { SeriesPoint } from "@/lib/types";

const KEYS: SeriesKey[] = ["assigned", "link_opens", "applied", "interviews", "offers"];
const ALL_SERIES: SeriesSpec[] = KEYS.map((key) => ({ key, ...SERIES[key] }));

function hasData(points: SeriesPoint[] | undefined, keys: SeriesKey[]): boolean {
  return Boolean(points?.some((point) => keys.some((key) => point[key] > 0)));
}

function SeriesTooltip({
  active,
  payload,
  label,
  series,
  formatLabel,
}: TooltipContentProps & { series: SeriesSpec[]; formatLabel: (label: string) => string }) {
  if (!active || !payload?.length) return null;
  const point = payload[0]?.payload as SeriesPoint | undefined;
  if (!point) return null;
  return (
    <ChartTooltipCard
      title={formatLabel(String(label ?? point.label))}
      rows={series.map((item) => ({ ...item, value: point[item.key as SeriesKey].toLocaleString() }))}
    />
  );
}

function toTable(points: SeriesPoint[], series: SeriesSpec[], firstColumn: string, formatLabel: (label: string) => string) {
  return {
    columns: [firstColumn, ...series.map((item) => item.label)],
    rows: points.map((point) => [formatLabel(point.label), ...series.map((item) => point[item.key as SeriesKey])]),
  };
}

interface ChartProps {
  data: SeriesPoint[] | undefined;
  loading?: boolean;
  fetching?: boolean;
  error?: unknown;
  onRetry?: () => void;
  className?: string;
}

/**
 * Daily counts as lines. Days are UTC, as the API buckets them. Each line shows,
 * for that day: jobs assigned, applications confirmed, job links opened,
 * interviews started and offers recorded.
 */
export function ApplicationsOverTimeChart({
  data,
  loading,
  fetching,
  error,
  onRetry,
  title = "Applications over time",
  className,
}: ChartProps & { title?: string }) {
  const [hidden, toggle] = useHiddenSeries(KEYS);
  const visible = ALL_SERIES.filter((item) => !hidden.has(item.key));
  const points = data ?? [];
  const empty = !hasData(data, KEYS);

  return (
    <ChartPanel
      title={title}
      description="Per day (UTC): jobs assigned, applications confirmed, job links opened."
      loading={loading}
      fetching={fetching}
      error={error}
      onRetry={onRetry}
      empty={empty}
      emptyTitle="No activity in this period"
      emptyDescription="Assignments, confirmed applications and job link opens appear here day by day."
      table={toTable(points, ALL_SERIES, "Day", formatDate)}
      className={className}
    >
      <ChartLegend series={ALL_SERIES} hidden={hidden} onToggle={toggle} className="mb-2 -ml-1.5" />
      <ChartCanvas
        height={240}
        label={`${title}. Line chart with ${visible.length} series over ${points.length} days. Use the Table button for exact values.`}
      >
        {(width) => (
          <LineChart width={width} height={240} data={points} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
            <CartesianGrid vertical={false} stroke={GRID_STROKE} />
            <XAxis
              dataKey="label"
              tickFormatter={formatDayShort}
              tick={AXIS_TICK}
              tickLine={false}
              axisLine={{ stroke: "var(--border-strong)" }}
              minTickGap={28}
              tickMargin={8}
              interval="preserveStartEnd"
            />
            <YAxis allowDecimals={false} tick={AXIS_TICK} tickLine={false} axisLine={false} width={34} />
            <Tooltip
              isAnimationActive={false}
              cursor={{ stroke: "var(--border-strong)", strokeWidth: 1 }}
              content={(props) => <SeriesTooltip {...props} series={visible} formatLabel={formatDate} />}
            />
            {visible.map((item) => (
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
    </ChartPanel>
  );
}

const WEEK_KEYS: SeriesKey[] = ["assigned", "applied", "interviews", "offers"];
const WEEK_SERIES: SeriesSpec[] = WEEK_KEYS.map((key) => ({ key, ...SERIES[key] }));
const weekLabel = (label: string) => `Week of ${formatDate(label)}`;

/** The last twelve weeks as grouped columns: assigned beside applied, with interviews and offers on request. */
export function WeeklyActivityChart({ data, loading, fetching, error, onRetry, className }: ChartProps) {
  const [hidden, toggle] = useHiddenSeries(WEEK_KEYS, ["interviews", "offers"]);
  const visible = WEEK_SERIES.filter((item) => !hidden.has(item.key));
  const points = data ?? [];
  const empty = !hasData(data, WEEK_KEYS);

  return (
    <ChartPanel
      title="Weekly activity"
      description="Per week, last 12 weeks. Weeks start on Monday (UTC)."
      loading={loading}
      fetching={fetching}
      error={error}
      onRetry={onRetry}
      empty={empty}
      emptyTitle="No activity in the last 12 weeks"
      emptyDescription="Each week's assigned and applied jobs are compared here."
      table={toTable(points, WEEK_SERIES, "Week starting", formatDate)}
      className={className}
    >
      <ChartLegend series={WEEK_SERIES} hidden={hidden} onToggle={toggle} mark="bar" className="mb-2 -ml-1.5" />
      <ChartCanvas
        height={240}
        label={`Weekly activity. Column chart with ${visible.length} series over ${points.length} weeks. Use the Table button for exact values.`}
      >
        {(width) => (
          <BarChart
            width={width}
            height={240}
            data={points}
            margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
            barGap={2}
            barCategoryGap="22%"
          >
            <CartesianGrid vertical={false} stroke={GRID_STROKE} />
            <XAxis
              dataKey="label"
              tickFormatter={formatDayShort}
              tick={AXIS_TICK}
              tickLine={false}
              axisLine={{ stroke: "var(--border-strong)" }}
              minTickGap={16}
              tickMargin={8}
            />
            <YAxis allowDecimals={false} tick={AXIS_TICK} tickLine={false} axisLine={false} width={34} />
            <Tooltip
              isAnimationActive={false}
              cursor={{ fill: "var(--muted)", opacity: 0.7 }}
              content={(props) => <SeriesTooltip {...props} series={visible} formatLabel={weekLabel} />}
            />
            {visible.map((item) => (
              <Bar
                key={item.key}
                dataKey={item.key}
                name={item.label}
                fill={item.color}
                maxBarSize={24}
                radius={[4, 4, 0, 0]}
                isAnimationActive={false}
              />
            ))}
          </BarChart>
        )}
      </ChartCanvas>
    </ChartPanel>
  );
}
