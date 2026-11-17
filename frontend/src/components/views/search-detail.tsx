"use client";

import { useState } from "react";
import { ClipboardCheck, ListChecks, SearchX } from "lucide-react";
import { toast } from "sonner";

import { assignResultText } from "@/components/jobs/assignment-dialog";
import { JobDrawer } from "@/components/jobs/job-manager";
import { JobFactLine, MatchScore } from "@/components/jobs/job-meta";
import { useSearchProgress } from "@/components/search/search-progress";
import {
  MODE_SHORT,
  RunAttempts,
  RunCounters,
  RunSettings,
  RunSources,
  RunTargets,
  RunWarnings,
  SearchStatusTag,
} from "@/components/search/search-run";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { DataTable, type Column } from "@/components/shared/data-table";
import { FilterBar, FilterSelect, SegmentedFilter } from "@/components/shared/filters";
import { PageHeader, Panel } from "@/components/shared/page-header";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { ApplicationStatusBadge, Tag } from "@/components/shared/status-badge";
import { useArea } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import { formatDateTime, formatDuration, plural } from "@/lib/format";
import { isSearchActive, useBulkAssign, useCancelSearch, useSearch, useSearchResults } from "@/lib/queries";
import type { SearchResult, SearchRun } from "@/lib/types";

const SCORE_OPTIONS = [50, 60, 70, 80, 90].map((score) => ({ value: String(score), label: `Score ${score} or higher` }));

interface Picked {
  resultId: string;
  jobId: string;
  memberId: string;
  memberName: string;
  title: string;
  company: string;
}

/**
 * The jobs a search found, per member, with their match. Jobs a member does not
 * have yet can be ticked and assigned in one step, after a confirmation.
 * In review mode this is where every assignment of the run is made.
 */
function SearchResults({ run }: { run: SearchRun }) {
  const [memberId, setMemberId] = useState(run.targets.length === 1 ? run.targets[0].member.id : "");
  const [minScore, setMinScore] = useState("");
  const [show, setShow] = useState(run.mode === "review" ? "unassigned" : "all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [picked, setPicked] = useState<ReadonlyMap<string, Picked>>(() => new Map());
  const [confirming, setConfirming] = useState(false);
  const [opened, setOpened] = useState<SearchResult | null>(null);
  const assign = useBulkAssign();

  const active = isSearchActive(run.status);
  const results = useSearchResults(
    run.id,
    {
      member_id: memberId || undefined,
      min_score: minScore ? Number(minScore) : undefined,
      unassigned_only: show === "unassigned" ? true : undefined,
      page,
      page_size: pageSize,
    },
    active,
  );
  const showMember = !memberId && run.targets.length > 1;

  const changeFilter = (apply: () => void) => {
    apply();
    setPage(1);
  };

  const columns: Column<SearchResult>[] = [
    { id: "rank", header: "Rank", align: "right", className: "w-14", cell: (row) => row.rank },
    {
      id: "job",
      header: "Job",
      cell: (row) => (
        <div className="flex min-w-0 flex-col gap-1">
          <button
            type="button"
            onClick={() => setOpened(row)}
            className="w-fit rounded-sm text-left font-medium underline-offset-4 hover:underline"
          >
            {row.job.title}
          </button>
          <JobFactLine job={row.job} company />
        </div>
      ),
    },
    ...(showMember ? [{ id: "member", header: "For", cell: (row: SearchResult) => row.member.name }] : []),
    { id: "score", header: "Match", cell: (row) => <MatchScore score={row.match_score} /> },
    {
      id: "skills",
      header: "Skills",
      cell: (row) => (
        <span className="whitespace-nowrap tabular-nums">
          {row.match.matching_skills.length} matching
          <span className="text-muted-foreground">, {row.match.missing_skills.length} missing</span>
        </span>
      ),
    },
    {
      id: "assignment",
      header: "Assignment",
      cell: (row) =>
        row.assignment_status ? <ApplicationStatusBadge status={row.assignment_status} /> : <Tag>Not assigned</Tag>,
    },
  ];

  const selection = {
    selected: new Set(picked.keys()),
    isSelectable: (row: SearchResult) => !row.assignment_id,
    label: (row: SearchResult) => `Select ${row.job.title} at ${row.job.company} for ${row.member.name}`,
    onChange: (next: Set<string>) => {
      const byId = new Map((results.data?.items ?? []).map((row) => [row.id, row]));
      const updated = new Map<string, Picked>();
      for (const id of next) {
        const existing = picked.get(id);
        const row = byId.get(id);
        if (existing) updated.set(id, existing);
        else if (row) {
          updated.set(id, {
            resultId: row.id,
            jobId: row.job.id,
            memberId: row.member.id,
            memberName: row.member.name,
            title: row.job.title,
            company: row.job.company,
          });
        }
      }
      setPicked(updated);
    },
  };

  const pickedList = [...picked.values()];
  const perMember = new Map<string, { name: string; count: number }>();
  for (const item of pickedList) {
    const entry = perMember.get(item.memberId) ?? { name: item.memberName, count: 0 };
    entry.count += 1;
    perMember.set(item.memberId, entry);
  }

  const submit = () =>
    assign.mutate(
      {
        items: pickedList.map((item) => ({ job_id: item.jobId, member_id: item.memberId, search_run_id: run.id })),
        search_run_id: run.id,
      },
      {
        onSuccess: (result) => {
          toast.success("Jobs assigned", { description: assignResultText(result) });
          setPicked(new Map());
          setConfirming(false);
        },
        onError: (error) => toast.error(errorMessage(error)),
      },
    );

  return (
    <Panel
      title="Results"
      description={
        run.mode === "review"
          ? "Nothing from this search is assigned until you choose it here."
          : "Jobs at or above the minimum match score were assigned automatically. The others can be assigned by hand."
      }
      flush
    >
      <div className="flex flex-col gap-3 p-4 pt-3">
        <FilterBar className="mb-0">
          {run.targets.length > 1 && (
            <FilterSelect
              label="Member"
              allLabel="All members"
              value={memberId}
              onChange={(value) => changeFilter(() => setMemberId(value))}
              options={run.targets.map((target) => ({
                value: target.member.id,
                label: `${target.member.name} (${target.matched})`,
              }))}
            />
          )}
          <SegmentedFilter
            label="Show"
            value={show}
            onChange={(value) => changeFilter(() => setShow(value))}
            options={[
              { value: "all", label: "All results" },
              { value: "unassigned", label: "Not assigned yet" },
            ]}
          />
          <FilterSelect
            label="Minimum match score"
            allLabel="Any match score"
            value={minScore}
            onChange={(value) => changeFilter(() => setMinScore(value))}
            options={SCORE_OPTIONS}
          />
          <div className="ml-auto flex items-center gap-2">
            {picked.size > 0 && (
              <Button variant="ghost" onClick={() => setPicked(new Map())}>
                Clear selection
              </Button>
            )}
            <Button disabled={picked.size === 0} onClick={() => setConfirming(true)}>
              <ClipboardCheck data-icon="inline-start" />
              Assign Selected{picked.size > 0 ? ` (${picked.size})` : ""}
            </Button>
          </div>
        </FilterBar>
        <DataTable
          caption="Search results"
          columns={columns}
          rows={results.data?.items}
          rowKey={(row) => row.id}
          loading={results.isPending}
          fetching={results.isPlaceholderData}
          error={results.error}
          onRetry={() => results.refetch()}
          selection={selection}
          empty={
            active ? (
              <EmptyState icon={ListChecks} title="No results yet" description="Results are saved as each member's search finishes. This list fills in on its own." />
            ) : show === "unassigned" || minScore ? (
              <EmptyState
                icon={SearchX}
                title="Nothing left to show with these filters"
                description={
                  show === "unassigned"
                    ? "Every result on this page is already assigned. Go to another page, or switch to All results."
                    : "Lower the minimum match score to see more."
                }
              />
            ) : (
              <EmptyState icon={SearchX} title="This search found no matching jobs" description="Try different job titles, more results per member, or deep search." />
            )
          }
          mobileCard={(row, select) => (
            <div className="flex gap-3">
              <div className="pt-0.5">{select}</div>
              <div className="flex min-w-0 flex-1 flex-col gap-2">
                <div className="flex items-start justify-between gap-3">
                  <button type="button" onClick={() => setOpened(row)} className="rounded-sm text-left font-medium underline-offset-4 hover:underline">
                    {row.job.title}
                  </button>
                  <MatchScore score={row.match_score} className="shrink-0" />
                </div>
                <JobFactLine job={row.job} company />
                <div className="flex flex-wrap items-center justify-between gap-2 text-2sm">
                  <span className="text-muted-foreground">
                    {showMember ? `${row.member.name}. ` : ""}Rank {row.rank}
                  </span>
                  {row.assignment_status ? <ApplicationStatusBadge status={row.assignment_status} /> : <Tag>Not assigned</Tag>}
                </div>
              </div>
            </div>
          )}
          pagination={
            results.data && {
              page: results.data.page,
              pageSize: results.data.page_size,
              total: results.data.total,
              noun: "results",
              // The API applies "not assigned yet" after paging, so a page can hold fewer rows than its range.
              summary:
                show === "unassigned"
                  ? ({ first, last, total }) =>
                      `${results.data.items.length} not assigned yet, among results ${first}–${last} of ${total}`
                  : undefined,
              onPageChange: setPage,
              onPageSizeChange: (size) => {
                setPageSize(size);
                setPage(1);
              },
            }
          }
          className="rounded-md"
        />
      </div>

      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Assign ${plural(pickedList.length, "job")}?`}
        description={
          <>
            Each selected job is assigned to the member it was matched for. They are notified, and the jobs appear in
            their list right away.
          </>
        }
        confirmLabel={`Assign ${plural(pickedList.length, "Job")}`}
        pending={assign.isPending}
        onConfirm={submit}
      >
        <ul className="flex max-h-48 flex-col gap-1 overflow-y-auto rounded-md border bg-muted/40 px-3 py-2 text-sm">
          {[...perMember.entries()].map(([id, entry]) => (
            <li key={id} className="flex justify-between gap-3">
              <span className="font-medium">{entry.name}</span>
              <span className="text-muted-foreground tabular-nums">{plural(entry.count, "job")}</span>
            </li>
          ))}
        </ul>
      </ConfirmDialog>

      <JobDrawer
        jobId={opened?.job.id ?? null}
        onOpenChange={(open) => !open && setOpened(null)}
        match={opened ? { explanation: opened.match, score: opened.match_score, memberName: opened.member.name } : undefined}
      />
    </Panel>
  );
}

/** One search run: its progress or outcome, the settings it used, and its results. */
export function SearchDetailView({ searchId }: { searchId: string }) {
  const area = useArea();
  const search = useSearch(searchId);
  const cancel = useCancelSearch();
  const progress = useSearchProgress();
  const [confirming, setConfirming] = useState(false);
  const back = { href: `${area}/searches`, label: "Search History" };

  if (search.isPending) {
    return (
      <>
        <PageHeader title="Search" back={back} />
        <LoadingSkeleton variant="detail" count={4} />
      </>
    );
  }
  if (search.isError) {
    return (
      <>
        <PageHeader title="Search" back={back} />
        <div className="rounded-lg border bg-card">
          <ErrorState error={search.error} onRetry={() => search.refetch()} />
        </div>
      </>
    );
  }

  const run = search.data;
  const active = isSearchActive(run.status);

  return (
    <>
      <PageHeader
        back={back}
        title={`Search of ${formatDateTime(run.created_at)}`}
        meta={
          <>
            <SearchStatusTag status={run.status} cancelling={run.cancel_requested} />
            <Tag>{MODE_SHORT[run.mode]}</Tag>
            <RunAttempts run={run} />
          </>
        }
        description={
          <>
            Started by {run.initiated_by?.name ?? "someone who left"}
            {run.team ? ` for ${run.team.name}` : ""}.{" "}
            {active
              ? run.stage || "Waiting for the background worker."
              : `Finished ${formatDateTime(run.completed_at)}, after ${formatDuration(run.started_at ?? run.created_at, run.completed_at)}.`}
          </>
        }
        actions={
          active && (
            <>
              <Button variant="outline" onClick={() => progress.follow(run.id, "modal")}>
                Show Progress
              </Button>
              <Button variant="destructive" disabled={run.cancel_requested} onClick={() => setConfirming(true)}>
                {run.cancel_requested ? "Cancelling" : "Cancel Search"}
              </Button>
            </>
          )
        }
      />

      <div className="flex flex-col gap-5">
        <RunWarnings run={run} />
        <RunCounters run={run} />
        <div className="grid gap-5 lg:grid-cols-2">
          <Panel title="Members" description="Each member is searched separately, with their own resume and profile.">
            <RunTargets targets={run.targets} />
          </Panel>
          <div className="flex min-w-0 flex-col gap-5">
            <Panel title="Settings used">
              <RunSettings run={run} />
            </Panel>
            <Panel title="Sources">
              <RunSources sources={run.sources} />
            </Panel>
          </div>
        </div>
        <SearchResults key={run.id} run={run} />
      </div>

      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="Cancel this search?"
        description="The search stops within a few seconds. Jobs already found and assigned stay as they are; the rest is not searched."
        confirmLabel="Cancel search"
        cancelLabel="Keep searching"
        destructive
        pending={cancel.isPending}
        onConfirm={() =>
          cancel.mutate(run.id, {
            onSuccess: () => {
              setConfirming(false);
              toast.message("Cancelling the search");
            },
            onError: (error) => toast.error(errorMessage(error)),
          })
        }
      />
    </>
  );
}
