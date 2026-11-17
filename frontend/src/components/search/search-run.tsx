import type { ReactNode } from "react";
import { Ban, CircleCheck, CircleX, Hourglass, LoaderCircle, SkipForward, TriangleAlert, type LucideIcon } from "lucide-react";

import { InfoPopover } from "@/components/shared/info-popover";
import { Tag } from "@/components/shared/status-badge";
import { humanize, plural } from "@/lib/format";
import type { SearchMode, SearchRun, SearchSkipped, SearchTarget } from "@/lib/types";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ labels */

export const MODE_LABELS: Record<SearchMode, string> = {
  auto_assign: "Find & Assign Matching Jobs",
  review: "Search first, review, then assign",
};

export const MODE_SHORT: Record<SearchMode, string> = { auto_assign: "Auto-assign", review: "Review first" };

type Tone = "neutral" | "primary" | "success" | "warning" | "danger";

const STATUS_VIEW: Record<string, { label: string; tone: Tone; icon: LucideIcon; spin?: boolean }> = {
  queued: { label: "Queued", tone: "neutral", icon: Hourglass },
  running: { label: "Running", tone: "primary", icon: LoaderCircle, spin: true },
  completed: { label: "Completed", tone: "success", icon: CircleCheck },
  partial: { label: "Finished with problems", tone: "warning", icon: TriangleAlert },
  failed: { label: "Failed", tone: "danger", icon: CircleX },
  cancelled: { label: "Cancelled", tone: "neutral", icon: Ban },
  skipped: { label: "Skipped", tone: "neutral", icon: SkipForward },
};

/** Status of a whole search run, or of one member inside it. */
export function SearchStatusTag({ status, cancelling = false }: { status: string; cancelling?: boolean }) {
  const view = STATUS_VIEW[status];
  if (!view) return <Tag>{humanize(status)}</Tag>;
  return (
    <Tag tone={view.tone} data-status={status}>
      <view.icon aria-hidden="true" className={view.spin ? "motion-safe:animate-spin" : undefined} />
      {cancelling && (status === "running" || status === "queued") ? "Cancelling" : view.label}
    </Tag>
  );
}

export function searchStatusLabel(status: string): string {
  return STATUS_VIEW[status]?.label ?? humanize(status);
}

/* ------------------------------------------------------------------ counters */

function Counter({ label, value, definition, className }: { label: string; value: ReactNode; definition?: string; className?: string }) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1 bg-card px-3 py-2.5", className)}>
      <dt className="flex items-center justify-between gap-1 text-xs text-muted-foreground">
        <span className="leading-tight">{label}</span>
        {definition && (
          <InfoPopover label={`About: ${label}`} title={label} className="-my-1 -mr-1.5">
            <p>{definition}</p>
          </InfoPopover>
        )}
      </dt>
      <dd className="font-figure text-2xl leading-none">{value}</dd>
    </div>
  );
}

/**
 * The run's real counters, exactly as the API reports them. Nothing is
 * estimated and there is no percentage: a search does not know in advance how
 * many postings it will find.
 */
export function RunCounters({ run, className }: { run: SearchRun; className?: string }) {
  const requested = run.target_count * Math.max(1, run.member_count);
  return (
    <dl className={cn("grid grid-cols-2 gap-px overflow-hidden rounded-lg border bg-border sm:grid-cols-3 lg:grid-cols-6", className)}>
      <Counter
        label="Requested"
        value={requested.toLocaleString()}
        definition={`${run.target_count} per member, for ${plural(run.member_count, "member")}. This is an upper limit, not a promise: only real postings that match are returned.`}
      />
      <Counter
        label="Discovered"
        value={run.discovered_count.toLocaleString()}
        definition="Postings examined. With several members this is the largest figure of any one member."
      />
      <Counter
        label="Verified"
        value={run.verified_count.toLocaleString()}
        definition="Postings confirmed as live, in the United States and not a duplicate. The largest figure of any one member."
      />
      <Counter
        label="Duplicates removed"
        value={run.duplicate_count.toLocaleString()}
        definition="Copies of a posting already found from another source."
      />
      <Counter label="Matched" value={run.matched_count.toLocaleString()} definition="Results kept, added up over all members." />
      <Counter
        label="Assigned"
        value={run.assigned_count.toLocaleString()}
        definition={
          run.mode === "review"
            ? "Assignments created, added up over all members. In review mode nothing is assigned until you choose jobs on the results page."
            : "Assignments created, added up over all members. Jobs below the minimum match score are kept as results but not assigned."
        }
      />
    </dl>
  );
}

/* ------------------------------------------------------------------ sources */

interface SourceCounts {
  boards_ok?: number;
  boards_failed?: number;
  pending?: number;
  postings?: number;
}

export function RunSources({ sources }: { sources: SearchRun["sources"] }) {
  const entries = Object.entries(sources as Record<string, SourceCounts>);
  if (entries.length === 0) {
    return <p className="text-sm text-muted-foreground">No source has reported yet.</p>;
  }
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">Job sources read by this search</caption>
        <thead>
          <tr className="border-b text-left text-2sm text-muted-foreground">
            <th scope="col" className="px-3 py-1.5 font-label">Source</th>
            <th scope="col" className="px-3 py-1.5 text-right font-label">Read</th>
            <th scope="col" className="px-3 py-1.5 text-right font-label">Failed</th>
            <th scope="col" className="px-3 py-1.5 text-right font-label">Still loading</th>
            <th scope="col" className="px-3 py-1.5 text-right font-label">Postings</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([name, counts]) => (
            <tr key={name} className="border-b last:border-b-0">
              <th scope="row" className="px-3 py-1.5 text-left font-normal">{name}</th>
              <td className="px-3 py-1.5 text-right tabular-nums">{(counts.boards_ok ?? 0).toLocaleString()}</td>
              <td className={cn("px-3 py-1.5 text-right tabular-nums", (counts.boards_failed ?? 0) > 0 && "font-semibold text-warning")}>
                {(counts.boards_failed ?? 0).toLocaleString()}
              </td>
              <td className="px-3 py-1.5 text-right tabular-nums">{(counts.pending ?? 0).toLocaleString()}</td>
              <td className="px-3 py-1.5 text-right tabular-nums">{(counts.postings ?? 0).toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------------------------------------------ members */

function TargetFigures({ target }: { target: SearchTarget }) {
  const cells: Array<[string, number]> = [
    ["Discovered", target.discovered],
    ["Verified", target.verified],
    ["Duplicates", target.duplicates],
    ["Matched", target.matched],
    ["Assigned", target.assigned],
  ];
  return (
    <dl className="grid grid-cols-5 gap-2">
      {cells.map(([label, value]) => (
        <div key={label} className="flex min-w-0 flex-col">
          <dt className="truncate text-xs text-muted-foreground">{label}</dt>
          <dd className="font-figure text-lg leading-tight">{value.toLocaleString()}</dd>
        </div>
      ))}
    </dl>
  );
}

/** One block per member in the run: where their search is, what it found, and any warnings. */
export function RunTargets({ targets, renderAction }: { targets: SearchTarget[]; renderAction?: (target: SearchTarget) => ReactNode }) {
  if (targets.length === 0) return <p className="text-sm text-muted-foreground">No members in this search.</p>;
  return (
    <ul className="flex flex-col divide-y rounded-lg border">
      {targets.map((target) => (
        <li key={target.member.id} className="flex flex-col gap-2.5 px-3 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex min-w-0 flex-col">
              <span className="truncate text-sm font-medium">{target.member.name}</span>
              <span className="truncate text-2sm text-muted-foreground">
                {target.message || target.stage || (target.status === "queued" ? "Waiting to start" : "")}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <SearchStatusTag status={target.status} />
              {renderAction?.(target)}
            </div>
          </div>
          <TargetFigures target={target} />
          {target.warnings.length > 0 && (
            <ul className="flex flex-col gap-1">
              {target.warnings.map((warning) => (
                <li key={warning} className="flex items-start gap-1.5 text-xs text-muted-foreground">
                  <TriangleAlert className="mt-px size-3.5 shrink-0 text-warning" aria-hidden="true" />
                  {warning}
                </li>
              ))}
            </ul>
          )}
        </li>
      ))}
    </ul>
  );
}

/* ------------------------------------------------------------------ warnings, errors, skipped */

export function RunWarnings({ run }: { run: SearchRun }) {
  if (run.warnings.length === 0 && !run.error_summary) return null;
  return (
    <div className="flex flex-col gap-2">
      {run.error_summary && (
        <div role="alert" className="flex items-start gap-2 rounded-md border border-destructive/35 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          <CircleX className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          {run.error_summary}
        </div>
      )}
      {run.warnings.length > 0 && (
        <ul className="flex flex-col gap-1.5 rounded-md border border-warning/40 bg-warning/8 px-3 py-2">
          {run.warnings.map((warning) => (
            <li key={warning} className="flex items-start gap-2 text-sm">
              <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
              {warning}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Members that were selected but cannot be searched, each with the API's reason. */
export function SkippedList({ skipped }: { skipped: SearchSkipped[] }) {
  if (skipped.length === 0) return null;
  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-warning/40 bg-warning/8 px-3 py-2">
      <p className="text-sm font-medium">
        {plural(skipped.length, "selected member")} {skipped.length === 1 ? "was" : "were"} left out of this search
      </p>
      <ul className="flex flex-col gap-1">
        {skipped.map((item) => (
          <li key={item.member.id} className="text-sm">
            <span className="font-medium">{item.member.name}:</span> <span className="text-muted-foreground">{item.reason}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* ------------------------------------------------------------------ settings used */

const FILTER_LABELS: Record<string, string> = {
  job_titles: "Job titles",
  years_experience: "Years of experience",
  work_authorization: "Work authorization",
  sponsorship_needed: "Sponsorship needed",
  exclude_citizenship_required: "Leave out jobs requiring US citizenship",
  work_mode: "Work arrangement",
  preferred_states: "Preferred states",
  remote_only: "Remote only",
  excluded_companies: "Companies left out",
};

function filterValue(value: unknown): string {
  if (Array.isArray(value)) return value.length ? value.join(", ") : "None";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (value === null || value === undefined || value === "") return "Not set";
  return String(value);
}

/** The settings a search ran with. An override the launcher left empty used each member's saved value. */
export function RunSettings({ run }: { run: SearchRun }) {
  const overrides = Object.entries(run.filters as Record<string, unknown>).filter(([, value]) => value !== null && value !== undefined);
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1.5 text-sm">
      <dt className="text-muted-foreground">Mode</dt>
      <dd>{MODE_LABELS[run.mode]}</dd>
      <dt className="text-muted-foreground">Results per member</dt>
      <dd className="tabular-nums">{run.target_count}</dd>
      <dt className="text-muted-foreground">Minimum match score</dt>
      <dd className="tabular-nums">{run.min_match_score}</dd>
      <dt className="text-muted-foreground">Deep search</dt>
      <dd>{run.deep_search ? "On" : "Off"}</dd>
      {overrides.length === 0 ? (
        <>
          <dt className="text-muted-foreground">Overrides</dt>
          <dd>None. Every member was searched with their own saved profile.</dd>
        </>
      ) : (
        overrides.map(([key, value]) => (
          <div key={key} className="contents">
            <dt className="text-muted-foreground">{FILTER_LABELS[key] ?? humanize(key)}</dt>
            <dd className="break-words">{filterValue(value)}</dd>
          </div>
        ))
      )}
    </dl>
  );
}
