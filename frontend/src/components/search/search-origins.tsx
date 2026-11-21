"use client";

import { useId, useState } from "react";
import { TriangleAlert } from "lucide-react";

import { Tag } from "@/components/shared/status-badge";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useIsMobile } from "@/hooks/use-mobile";
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
  rank_seconds?: number | null;
  /** Model answers of earlier searches for the same candidate and posting text that were used again, per kind. */
  rank_reused?: Record<string, number>;
}

/* Each label follows a count: "8 outside the United States, 2 work authorization did not match". */
const REASON_LABELS: Record<string, string> = {
  not_relevant: "title did not match",
  non_us: "outside the United States",
  work_mode: "work arrangement did not match",
  eligibility: "work authorization did not match",
  ai_audit: "set aside by the AI check",
};

function seconds(value: number | null | undefined): string {
  if (typeof value !== "number") return "—";
  // The API reports tenths of a second; a search answered from the index at once is not "0 s".
  if (value < 0.1) return "under 0.1 s";
  if (value < 60) return `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })} s`;
  return `${Math.floor(value / 60)} min ${Math.round(value % 60)} s`;
}

const count = (value: number | null | undefined) => (typeof value === "number" ? value.toLocaleString() : "—");

/** "37 AI judgements and 828 relevance scores", or "" when nothing was taken from earlier searches. */
function reusedText(reused: Record<string, number> | undefined): string {
  const judged = reused?.judge ?? 0;
  const relevance = reused?.rerank ?? 0;
  const parts = [
    judged > 0 ? `${judged.toLocaleString()} AI ${judged === 1 ? "judgement" : "judgements"}` : "",
    relevance > 0 ? `${relevance.toLocaleString()} relevance ${relevance === 1 ? "score" : "scores"}` : "",
  ].filter(Boolean);
  return parts.join(" and ");
}

function reasonsText(reasons: Record<string, number> | undefined): string {
  return Object.entries(reasons ?? {})
    .filter(([, n]) => n > 0)
    .map(([reason, n]) => `${n.toLocaleString()} ${REASON_LABELS[reason] ?? reason.replaceAll("_", " ")}`)
    .join(", ");
}

const cell = "px-3 py-1.5 text-right tabular-nums";
const head = "px-3 py-1.5 text-right font-label";

interface Figure {
  label: string;
  value: string;
  strong?: boolean;
}

/**
 * The figures of one row side by side, each under its label: what a table row becomes on a narrow screen, where
 * a table of five columns would hide its last ones (the candidates of a run show their figures the same way).
 */
function Figures({ items }: { items: Figure[] }) {
  return (
    <dl className="grid grid-cols-4 items-end gap-2">
      {items.map((item) => (
        <div key={item.label} className="flex min-w-0 flex-col">
          <dt className="text-xs text-muted-foreground">{item.label}</dt>
          <dd data-slot="origin-figure" className="font-figure text-lg leading-tight">
            {item.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

const platformFigures = (row: PlatformRow): Figure[] => [
  { label: "Unique jobs", value: count(row.unique), strong: true },
  { label: "Verified", value: count(row.verified) },
  { label: "Rejected", value: count(row.rejected) },
  { label: "In results", value: count(row.matched) },
];

const providerFigures = (row: ProviderRow): Figure[] => [
  { label: "Queries", value: count(row.queries) },
  { label: "Leads", value: count(row.leads) },
  { label: "New boards", value: count(row.new_boards) },
  { label: "Jobs from them", value: ((row.verified ?? 0) + (row.new_board_postings ?? 0)).toLocaleString() },
];

function PlatformName({ id, row }: { id: string; row: PlatformRow }) {
  const reasons = reasonsText(row.reasons);
  return (
    <span data-slot="origin-name" className="flex min-w-32 flex-col gap-1">
      <span className="flex flex-wrap items-center gap-1.5">
        {row.label ?? id}
        {row.coverage === "limited" && <Tag tone="warning">Limited coverage</Tag>}
        {row.coverage === "none" && <Tag tone="warning">Not searched</Tag>}
      </span>
      {reasons && <span className="text-xs font-normal text-muted-foreground">Rejected: {reasons}</span>}
    </span>
  );
}

function ProviderName({ name, row }: { name: string; row: ProviderRow }) {
  return (
    <span data-slot="origin-name" className="flex min-w-32 flex-wrap items-center gap-1.5">
      {name}
      <Tag>{row.kind === "ai" ? "AI provider" : "Search engine"}</Tag>
    </span>
  );
}

const PLATFORM_CAPTION = "Jobs found per platform";
const PROVIDER_CAPTION = "Search engines and AI providers that contributed";

function PlatformTable({ rows, stacked }: { rows: Array<[string, PlatformRow]>; stacked: boolean }) {
  if (stacked) {
    return (
      <ul aria-label={PLATFORM_CAPTION} className="flex flex-col divide-y rounded-lg border text-sm">
        {rows.map(([id, row]) => (
          <li key={id} data-platform={id} className="flex flex-col gap-2 px-3 py-2.5">
            <div className="font-medium">
              <PlatformName id={id} row={row} />
            </div>
            <Figures items={platformFigures(row)} />
          </li>
        ))}
      </ul>
    );
  }
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">{PLATFORM_CAPTION}</caption>
        <thead>
          <tr className="border-b text-left text-2sm text-muted-foreground">
            <th scope="col" className="px-3 py-1.5 font-label">Platform</th>
            {platformFigures({}).map((figure) => (
              <th key={figure.label} scope="col" className={head}>{figure.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(([id, row]) => (
            <tr key={id} data-platform={id} className="border-b align-top last:border-b-0">
              <th scope="row" className="px-3 py-1.5 text-left font-normal">
                <PlatformName id={id} row={row} />
              </th>
              {platformFigures(row).map((figure) => (
                <td key={figure.label} data-slot="origin-figure" className={cn(cell, figure.strong && "font-semibold")}>
                  {figure.value}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ProviderTable({ rows, stacked }: { rows: Array<[string, ProviderRow]>; stacked: boolean }) {
  if (stacked) {
    return (
      <ul aria-label={PROVIDER_CAPTION} className="flex flex-col divide-y rounded-lg border text-sm">
        {rows.map(([name, row]) => (
          <li key={name} data-provider={name} className="flex flex-col gap-2 px-3 py-2.5">
            <div className="font-medium">
              <ProviderName name={name} row={row} />
            </div>
            <Figures items={providerFigures(row)} />
          </li>
        ))}
      </ul>
    );
  }
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">{PROVIDER_CAPTION}</caption>
        <thead>
          <tr className="border-b text-left text-2sm text-muted-foreground">
            <th scope="col" className="px-3 py-1.5 font-label">Search engine or AI provider</th>
            {providerFigures({}).map((figure) => (
              <th key={figure.label} scope="col" className={head}>{figure.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, row]) => (
            <tr key={name} data-provider={name} className="border-b last:border-b-0">
              <th scope="row" className="px-3 py-1.5 text-left font-normal">
                <ProviderName name={name} row={row} />
              </th>
              {providerFigures(row).map((figure) => (
                <td key={figure.label} data-slot="origin-figure" className={cell}>
                  {figure.value}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TargetOrigins({ target }: { target: SearchTarget }) {
  const stacked = useIsMobile();
  const data = target.discovery as Origins;
  // What is still open is worded as "yet" only while this candidate's search goes on.
  const running = target.status === "queued" || target.status === "running";
  if (!data.platforms) {
    return (
      <p className="text-sm text-muted-foreground">
        {running
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
  const found = platforms.some(([, row]) => (row.unique ?? 0) > 0);
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
        <dd className="tabular-nums">
          {typeof data.seconds_to_first_verified === "number"
            ? seconds(data.seconds_to_first_verified)
            : running
              ? "No verified result yet"
              : "No verified result"}
        </dd>
        <dt className="text-muted-foreground">Total time</dt>
        <dd className="tabular-nums">{typeof data.total_seconds === "number" ? seconds(data.total_seconds) : running ? "Still running" : "Not recorded"}</dd>
        {typeof data.rank_seconds === "number" && (
          <>
            <dt className="text-muted-foreground">Ranking</dt>
            <dd>
              <span className="tabular-nums">{seconds(data.rank_seconds)}</span>
              {reusedText(data.rank_reused) &&
                `. ${reusedText(data.rank_reused)} from earlier searches of this candidate were used again; the posting text had not changed.`}
            </dd>
          </>
        )}
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
        <p className="text-sm text-muted-foreground">
          {running ? "No posting with a matching title has been found yet." : "No posting with a matching title was found."}
        </p>
      ) : (
        <PlatformTable rows={platforms} stacked={stacked} />
      )}
      {providers.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {running
            ? "No search engine or AI provider has contributed so far."
            : found
              ? "No search engine or AI provider contributed to this search: every job came from the employer boards read directly."
              : "No search engine or AI provider contributed to this search."}
        </p>
      ) : (
        <ProviderTable rows={providers} stacked={stacked} />
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
