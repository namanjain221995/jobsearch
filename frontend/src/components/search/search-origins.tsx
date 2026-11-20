"use client";

import { useId, useState } from "react";
import { TriangleAlert } from "lucide-react";

import { Tag } from "@/components/shared/status-badge";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { SearchRun, SearchTarget } from "@/lib/types";
import { cn } from "@/lib/utils";

/* The statistics arrive inside each candidate's `discovery` (see SearchTargetOut in the API): counts only. */

interface PlatformRow {
  label?: string;
  startup?: boolean;
  coverage?: string;
  considered?: number;
  verified?: number;
  unique?: number;
  rejected?: number;
  reasons?: Record<string, number>;
  duplicates?: number;
  matched?: number;
}

interface ProviderRow {
  kind?: string;
  queries?: number | null;
  leads?: number;
  new_boards?: number;
  new_board_postings?: number;
  verified?: number;
}

interface Origins {
  platforms?: Record<string, PlatformRow>;
  providers?: Record<string, ProviderRow>;
  platform_filter?: string[];
  startup_focus?: boolean;
  filtered_out?: number;
  coverage_warnings?: string[];
  seconds_to_first_verified?: number | null;
  total_seconds?: number | null;
}

const REASON_LABELS: Record<string, string> = {
  not_relevant: "title did not match",
  non_us: "outside the United States",
  work_mode: "work arrangement",
  eligibility: "work authorization",
  ai_audit: "set aside by the AI check",
};

function seconds(value: number | null | undefined): string {
  if (typeof value !== "number") return "—";
  if (value < 60) return `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })} s`;
  return `${Math.floor(value / 60)} min ${Math.round(value % 60)} s`;
}

const count = (value: number | null | undefined) => (typeof value === "number" ? value.toLocaleString() : "—");

function reasonsText(reasons: Record<string, number> | undefined): string {
  return Object.entries(reasons ?? {})
    .filter(([, n]) => n > 0)
    .map(([reason, n]) => `${n.toLocaleString()} ${REASON_LABELS[reason] ?? reason.replaceAll("_", " ")}`)
    .join(", ");
}

const cell = "px-3 py-1.5 text-right tabular-nums";
const head = "px-3 py-1.5 text-right font-label";

function PlatformTable({ rows }: { rows: Array<[string, PlatformRow]> }) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">Jobs found per platform</caption>
        <thead>
          <tr className="border-b text-left text-2sm text-muted-foreground">
            <th scope="col" className="px-3 py-1.5 font-label">Platform</th>
            <th scope="col" className={head}>Unique jobs</th>
            <th scope="col" className={head}>Verified</th>
            <th scope="col" className={head}>Rejected</th>
            <th scope="col" className={head}>In results</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([id, row]) => {
            const reasons = reasonsText(row.reasons);
            return (
              <tr key={id} data-platform={id} className="border-b align-top last:border-b-0">
                <th scope="row" className="px-3 py-1.5 text-left font-normal">
                  <span className="flex min-w-32 flex-col gap-1">
                    <span className="flex flex-wrap items-center gap-1.5">
                      {row.label ?? id}
                      {row.coverage === "limited" && <Tag tone="warning">Limited coverage</Tag>}
                      {row.coverage === "none" && <Tag tone="warning">Not searched</Tag>}
                    </span>
                    {reasons && <span className="text-xs text-muted-foreground">Rejected: {reasons}</span>}
                  </span>
                </th>
                <td className={cn(cell, "font-semibold")}>{count(row.unique)}</td>
                <td className={cell}>{count(row.verified)}</td>
                <td className={cell}>{count(row.rejected)}</td>
                <td className={cell}>{count(row.matched)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ProviderTable({ rows }: { rows: Array<[string, ProviderRow]> }) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">Search engines and AI providers that contributed</caption>
        <thead>
          <tr className="border-b text-left text-2sm text-muted-foreground">
            <th scope="col" className="px-3 py-1.5 font-label">Search engine or AI provider</th>
            <th scope="col" className={head}>Queries</th>
            <th scope="col" className={head}>Leads</th>
            <th scope="col" className={head}>New boards</th>
            <th scope="col" className={head}>Jobs from them</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, row]) => (
            <tr key={name} data-provider={name} className="border-b last:border-b-0">
              <th scope="row" className="px-3 py-1.5 text-left font-normal">
                <span className="flex min-w-32 flex-wrap items-center gap-1.5">
                  {name}
                  <Tag>{row.kind === "ai" ? "AI provider" : "Search engine"}</Tag>
                </span>
              </th>
              <td className={cell}>{count(row.queries)}</td>
              <td className={cell}>{count(row.leads)}</td>
              <td className={cell}>{count(row.new_boards)}</td>
              <td className={cell}>{((row.verified ?? 0) + (row.new_board_postings ?? 0)).toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TargetOrigins({ target }: { target: SearchTarget }) {
  const data = target.discovery as Origins;
  if (!data.platforms) {
    return (
      <p className="text-sm text-muted-foreground">
        {target.status === "queued" || target.status === "running"
          ? "The figures appear once the first sources have been read."
          : "This search has no figures per platform: it did not get that far, or it ran before they were recorded."}
      </p>
    );
  }
  // Most unique jobs first; chosen platforms that found nothing stay in the list.
  const platforms = Object.entries(data.platforms).sort(([, a], [, b]) => (b.unique ?? 0) - (a.unique ?? 0));
  const providers = Object.entries(data.providers ?? {});
  const warnings = data.coverage_warnings ?? [];
  const filteredOut = data.filtered_out ?? 0;
  return (
    <div className="flex flex-col gap-4">
      {warnings.length > 0 && (
        <ul data-slot="coverage-warnings" className="flex flex-col gap-1.5 rounded-md border border-warning/40 bg-warning/8 px-3 py-2">
          {warnings.map((warning) => (
            <li key={warning} className="flex items-start gap-2 text-sm">
              <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
              {warning}
            </li>
          ))}
        </ul>
      )}
      <dl className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1.5 text-sm">
        <dt className="text-muted-foreground">Time to first verified results</dt>
        <dd className="tabular-nums">{typeof data.seconds_to_first_verified === "number" ? seconds(data.seconds_to_first_verified) : "No verified result yet"}</dd>
        <dt className="text-muted-foreground">Total time</dt>
        <dd className="tabular-nums">{typeof data.total_seconds === "number" ? seconds(data.total_seconds) : "Still running"}</dd>
        {data.startup_focus && (
          <>
            <dt className="text-muted-foreground">Startup focus</dt>
            <dd>On. Startup platforms were searched first; at an equal match score their jobs are listed first.</dd>
          </>
        )}
        {filteredOut > 0 && (
          <>
            <dt className="text-muted-foreground">Left out by the platform choice</dt>
            <dd>
              <span className="tabular-nums">{filteredOut.toLocaleString()}</span> web results on other platforms or sites
            </dd>
          </>
        )}
      </dl>
      {platforms.length === 0 ? (
        <p className="text-sm text-muted-foreground">No posting with a matching title has been found yet.</p>
      ) : (
        <PlatformTable rows={platforms} />
      )}
      {providers.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No search engine or AI provider contributed to this search: every job came from the employer boards read
          directly.
        </p>
      ) : (
        <ProviderTable rows={providers} />
      )}
      <p className="text-xs text-muted-foreground">
        Verified means live, in the United States and passing the search&apos;s filters. Unique jobs are the verified
        ones after copies of the same posting were removed. Jobs from a search engine or AI provider are the postings
        it named plus the postings of employer boards it was the first to find.
      </p>
    </div>
  );
}

/**
 * Where the jobs of a search came from: per platform, per search engine and AI
 * provider, and how long it took. Each candidate is searched separately, so the
 * figures are shown for one candidate at a time.
 */
export function RunOrigins({ run }: { run: SearchRun }) {
  const selectId = useId();
  const [candidateId, setCandidateId] = useState("");
  const target = run.targets.find((item) => item.candidate.id === candidateId) ?? run.targets[0];
  if (!target) return <p className="text-sm text-muted-foreground">No candidates in this search.</p>;
  return (
    <div className="flex flex-col gap-4">
      {run.targets.length > 1 && (
        <div className="flex flex-col gap-2">
          <Label htmlFor={selectId}>Candidate</Label>
          <Select value={target.candidate.id} onValueChange={setCandidateId}>
            <SelectTrigger id={selectId} className="w-full sm:max-w-80">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {run.targets.map((item) => (
                <SelectItem key={item.candidate.id} value={item.candidate.id}>
                  {item.candidate.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}
      <TargetOrigins target={target} />
    </div>
  );
}
