"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { BriefcaseBusiness, Contact, SearchX } from "lucide-react";

import { JobFactLine, MatchScore } from "@/components/jobs/job-meta";
import { FilterBar, FilterSelect, SegmentedFilter } from "@/components/shared/filters";
import { Pagination } from "@/components/shared/pagination";
import { SearchInput } from "@/components/shared/search-input";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { ApplicationStatusBadge, StageRail } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { formatDate } from "@/lib/format";
import type { ListState } from "@/lib/list-state";
import { memberCandidateHref, memberJobHref } from "@/lib/member-candidates";
import { useAssignments, useCandidates } from "@/lib/queries";
import { isAssignmentStatus, STATUS_META, STATUS_ORDER } from "@/lib/status";
import type { Assignment, AssignmentListParams } from "@/lib/types";
import { toPositiveInt } from "@/lib/url-state";
import { cn } from "@/lib/utils";

/** The filters of a member's job list. `candidate` is only used where the list spans candidates. */
export const MEMBER_JOB_DEFAULTS = {
  candidate: "",
  q: "",
  status: "",
  applied: "all",
  sort: "recent",
  page: "1",
  page_size: "12",
};
export type MemberJobState = typeof MEMBER_JOB_DEFAULTS;

export const MEMBER_JOB_SORTS = [
  { value: "recent", label: "Newest first" },
  { value: "oldest", label: "Oldest first" },
  { value: "score", label: "Best match first" },
  { value: "applied", label: "Recently applied first" },
  { value: "status", label: "By status" },
];

const CLEARED = { candidate: "", q: "", status: "", applied: "all" };

/**
 * Query parameters for GET /api/assignments. The API itself limits a member to the candidates
 * assigned to them today, so no member id is sent; `candidate_id` only narrows that.
 */
export function memberJobParams(
  state: Pick<MemberJobState, "candidate" | "q" | "status" | "applied" | "sort" | "page" | "page_size">,
  candidateId?: string,
): AssignmentListParams {
  return {
    candidate_id: candidateId ?? (state.candidate || undefined),
    q: state.q || undefined,
    status: isAssignmentStatus(state.status) ? state.status : undefined,
    applied: state.applied === "true" ? true : state.applied === "false" ? false : undefined,
    sort: state.sort,
    page: toPositiveInt(state.page, 1),
    page_size: toPositiveInt(state.page_size, 12),
  };
}

/** The member's candidates as filter options (everything GET /api/candidates returns for a member is theirs). */
export function useCandidateOptions(enabled = true) {
  const candidates = useCandidates({ sort: "name", page: 1, page_size: 200 }, { enabled, live: false });
  return {
    options: (candidates.data?.items ?? []).map((candidate) => ({ value: candidate.id, label: candidate.name })),
    total: candidates.data?.total,
    loaded: candidates.isSuccess,
  };
}

/**
 * One assigned job as a card, for the member who applies on a candidate's behalf. The title opens
 * the job; where the list spans candidates the card also names the candidate, linked to their page.
 */
export function MemberJobCard({
  assignment,
  href,
  showCandidate,
}: {
  assignment: Assignment;
  href: string;
  showCandidate: boolean;
}) {
  const { job, candidate } = assignment;
  return (
    <article
      data-slot="job-card"
      className="group relative flex h-full flex-col gap-3 rounded-lg border bg-card p-4 transition-colors hover:border-border-strong has-[h3_a:focus-visible]:border-ring has-[h3_a:focus-visible]:ring-3 has-[h3_a:focus-visible]:ring-ring/40"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <p className="truncate text-2sm text-muted-foreground">{job.company}</p>
          <h3 className="font-label text-md leading-snug">
            <Link href={href} className="outline-none after:absolute after:inset-0 after:rounded-lg">
              <span className="line-clamp-2">{job.title}</span>
            </Link>
          </h3>
        </div>
        <MatchScore score={assignment.match_score} className="shrink-0 pt-0.5" />
      </div>
      {showCandidate && (
        <p className="flex min-w-0 items-center gap-1.5 text-2sm">
          <Contact className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className="shrink-0 text-muted-foreground">For</span>
          {/* Above the card-wide job link, so the candidate's name is a link of its own. */}
          <Link
            href={memberCandidateHref(candidate.id)}
            className="relative z-[1] min-w-0 truncate rounded-sm font-medium underline-offset-4 hover:underline"
          >
            {candidate.name}
          </Link>
        </p>
      )}
      <JobFactLine job={job} />
      <div className="mt-auto flex flex-col gap-2.5 pt-1">
        <StageRail status={assignment.status} viewedAt={assignment.viewed_at} appliedAt={assignment.applied_at} />
        <div className="flex flex-wrap items-center justify-between gap-2">
          <ApplicationStatusBadge status={assignment.status} />
          <span className="text-xs text-muted-foreground">
            {assignment.applied_at
              ? `Applied ${formatDate(assignment.applied_at)}`
              : `Assigned ${formatDate(assignment.assigned_at)}`}
          </span>
        </div>
      </div>
    </article>
  );
}

/**
 * Assigned jobs as cards with their filters: every job of the member's candidates, or with
 * `candidateId` the jobs of one candidate. Opening a card leads to the job page, where the
 * posting is opened, the status is changed and notes and history are kept.
 */
export function MemberAssignmentList({
  listState,
  candidateId,
  empty,
}: {
  listState: ListState<MemberJobState>;
  /** Fixes the list to one candidate: no candidate filter, no candidate on the cards. */
  candidateId?: string;
  /** What to show when there is no job at all (nothing filtered). */
  empty: ReactNode;
}) {
  const [state, setState] = listState;
  const across = candidateId === undefined;
  const candidates = useCandidateOptions(across);
  const list = useAssignments(memberJobParams(state, candidateId));
  const filtered = Boolean(state.q || state.status || state.applied !== "all" || (across && state.candidate));
  const clear = () => setState(across ? CLEARED : { q: "", status: "", applied: "all" });

  return (
    <>
      <FilterBar canClear={filtered} onClear={clear}>
        <SegmentedFilter
          label="Show"
          value={state.applied}
          onChange={(applied) => setState({ applied, status: "" })}
          options={[
            { value: "all", label: "All" },
            { value: "false", label: "Not applied" },
            { value: "true", label: "Applied" },
          ]}
        />
        <SearchInput label="Search jobs" placeholder="Search title or company" value={state.q} onChange={(q) => setState({ q })} />
        {across && (
          <FilterSelect
            label="Candidate"
            allLabel="All candidates"
            value={state.candidate}
            onChange={(candidate) => setState({ candidate })}
            options={candidates.options}
            className="sm:max-w-56"
          />
        )}
        <FilterSelect
          label="Status"
          allLabel="All statuses"
          value={state.status}
          onChange={(status) => setState({ status })}
          options={STATUS_ORDER.map((status) => ({ value: status, label: STATUS_META[status].label }))}
        />
        <FilterSelect label="Sort" value={state.sort} onChange={(sort) => setState({ sort })} options={MEMBER_JOB_SORTS} />
      </FilterBar>

      {list.isPending ? (
        <LoadingSkeleton variant="cards" count={6} />
      ) : list.isError ? (
        <div className="rounded-lg border bg-card">
          <ErrorState error={list.error} onRetry={() => list.refetch()} />
        </div>
      ) : list.data.items.length === 0 ? (
        <div className="rounded-lg border bg-card">
          {filtered ? (
            <EmptyState
              icon={SearchX}
              title="No jobs match these filters"
              description="Try a different status or clear the search."
              action={
                <Button variant="outline" size="sm" onClick={clear}>
                  Clear filters
                </Button>
              }
            />
          ) : (
            empty
          )}
        </div>
      ) : (
        <>
          <ul
            aria-label="Assigned jobs"
            className={cn("grid gap-3 sm:grid-cols-2 xl:grid-cols-3", list.isPlaceholderData && "opacity-60 transition-opacity")}
          >
            {list.data.items.map((assignment) => (
              <li key={assignment.id} className="min-w-0">
                <MemberJobCard
                  assignment={assignment}
                  href={memberJobHref(assignment.id, candidateId)}
                  showCandidate={across}
                />
              </li>
            ))}
          </ul>
          <Pagination
            className="mt-4"
            noun="jobs"
            page={list.data.page}
            pageSize={list.data.page_size}
            total={list.data.total}
            pageSizes={[12, 24, 48, 96]}
            onPageChange={(page) => setState({ page: String(page) })}
            onPageSizeChange={(size) => setState({ page_size: String(size) })}
          />
        </>
      )}
    </>
  );
}

/** The empty state of a job list that has nothing in it yet. */
export function NoJobsYet({ description, action }: { description: string; action?: ReactNode }) {
  return <EmptyState icon={BriefcaseBusiness} title="No jobs assigned yet" description={description} action={action} />;
}
