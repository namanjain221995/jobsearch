"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { BriefcaseBusiness, Check, CircleCheckBig, Contact, SearchX } from "lucide-react";
import { toast } from "sonner";

import { JobFactLine, MatchScore } from "@/components/jobs/job-meta";
import { FilterBar, FilterSelect } from "@/components/shared/filters";
import { Pagination } from "@/components/shared/pagination";
import { SearchInput } from "@/components/shared/search-input";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { ApplicationStatusBadge, StageRail } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { errorMessage } from "@/lib/api";
import { useAssignmentSections, useMoveAssignment, type AssignmentMove } from "@/lib/assignment-sections";
import { formatDate } from "@/lib/format";
import type { ListState } from "@/lib/list-state";
import { memberCandidateHref, memberJobHref } from "@/lib/member-candidates";
import { useAssignments, useCandidates } from "@/lib/queries";
import { isAssignmentStatus, isJobSection, JOB_SECTIONS, PRE_APPLICATION_STATUSES, STATUS_META, type JobSection } from "@/lib/status";
import type { Assignment, AssignmentListParams, AssignmentStatus } from "@/lib/types";
import { toPositiveInt } from "@/lib/url-state";
import { cn } from "@/lib/utils";

/**
 * The filters of a member's job list. `section` is the part of the list on screen: "to_apply" (the
 * default), "applied", "in_progress", "closed" or "all". `candidate` is only used where the list spans candidates.
 */
export const MEMBER_JOB_DEFAULTS = {
  section: "to_apply",
  candidate: "",
  q: "",
  status: "",
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

type SectionValue = JobSection | "all";

const TABS: Array<{ value: SectionValue; label: string; statuses: AssignmentStatus[] }> = [
  ...JOB_SECTIONS.map(({ value, label, statuses }) => ({ value, label, statuses })),
  { value: "all", label: "All", statuses: [] },
];

function sectionValue(value: string): SectionValue {
  return value === "all" || isJobSection(value) ? value : "to_apply";
}

/** The statuses a section holds; none for "all". */
function statusesOf(section: SectionValue): AssignmentStatus[] {
  return TABS.find((tab) => tab.value === section)?.statuses ?? [];
}

/**
 * Query parameters for GET /api/assignments. The API itself limits a member to the candidates
 * assigned to them today, so no member id is sent; `candidate_id` only narrows that. Lists that have
 * no sections (Applications) leave `section` out and get every status.
 */
export function memberJobParams(
  state: Pick<MemberJobState, "candidate" | "q" | "status" | "sort" | "page" | "page_size"> & { section?: string },
  candidateId?: string,
): AssignmentListParams {
  const within = state.section === undefined ? [] : statusesOf(sectionValue(state.section));
  // A status filter left over from another section does not apply in this one.
  const one = isAssignmentStatus(state.status) && (within.length === 0 || within.includes(state.status)) ? state.status : undefined;
  return {
    candidate_id: candidateId ?? (state.candidate || undefined),
    q: state.q || undefined,
    status: one ?? (within.length > 0 ? within.join(",") : undefined),
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
 * A job that is still to be applied for carries the one thing that is done to it most: marking it as applied.
 */
export function MemberJobCard({
  assignment,
  href,
  showCandidate,
  onMarkApplied,
  marking = false,
}: {
  assignment: Assignment;
  href: string;
  showCandidate: boolean;
  /** Offered on jobs that are not applied to yet. */
  onMarkApplied?: (assignment: Assignment) => void;
  marking?: boolean;
}) {
  const { job, candidate } = assignment;
  const waiting = PRE_APPLICATION_STATUSES.includes(assignment.status);
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
        {waiting && onMarkApplied && (
          // Above the card-wide job link. The card's title is in the name, so every button says which job it is for.
          <Button
            type="button"
            variant="outline"
            size="sm"
            data-slot="mark-applied"
            // On a phone the button is as wide as the card and tall enough for a thumb.
            className="relative z-[1] w-full max-sm:h-10 sm:w-fit"
            disabled={marking}
            aria-label={`Mark as applied: ${job.title} at ${job.company}${showCandidate ? `, for ${candidate.name}` : ""}`}
            onClick={() => onMarkApplied(assignment)}
          >
            {marking ? <Spinner data-icon="inline-start" /> : <Check data-icon="inline-start" />}
            Mark as applied
          </Button>
        )}
      </div>
    </article>
  );
}

const EMPTY_SECTION: Record<JobSection, { title: string; description: string }> = {
  to_apply: {
    title: "Nothing left to apply for",
    description: "Every job assigned so far has been applied to or closed. New jobs appear here as soon as your Team Lead assigns them.",
  },
  applied: {
    title: "No applications waiting for an answer",
    description: "A job moves here when you mark it as applied, and stays until the employer answers.",
  },
  in_progress: {
    title: "No interviews or offers yet",
    description: "A job moves here when you record an interview or an offer on its page.",
  },
  closed: { title: "No closed jobs", description: "Rejected, withdrawn and skipped jobs are kept here." },
};

/**
 * Assigned jobs as cards, in sections: "To apply" (what is still to do, shown first), "Applied",
 * "In progress" (interviews and offers), "Closed", and "All". Every job of the member's candidates,
 * or with `candidateId` the jobs of one candidate. Search, the candidate filter and the sort order
 * work inside the section on screen, and the counts on the tabs follow search and candidate.
 *
 * "Mark as applied" on a card moves it out of "To apply" at once; the toast that confirms it offers
 * Undo. Everything else about a job (opening the posting, the other statuses, notes, history) is on
 * its page, which the card opens.
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
  const section = sectionValue(state.section);
  const within = statusesOf(section);
  const candidates = useCandidateOptions(across);
  const params = memberJobParams(state, candidateId);
  const list = useAssignments(params);
  const counts = useAssignmentSections({ candidate_id: params.candidate_id, q: params.q });
  const move = useMoveAssignment();
  const [marking, setMarking] = useState<ReadonlySet<string>>(new Set());
  const listRef = useRef<HTMLUListElement>(null);
  const tabsRef = useRef<HTMLDivElement>(null);
  /** The card whose button was pressed and the place it had: once it has left the list, the focus goes to the card now there. */
  const leaving = useRef<{ id: string; index: number } | null>(null);

  useEffect(() => {
    const left = leaving.current;
    if (!left || !list.data || list.data.items.some((item) => item.id === left.id)) return;
    leaving.current = null;
    // Without this the focus would fall back to the top of the page when the pressed button disappears.
    const titles = listRef.current?.querySelectorAll<HTMLAnchorElement>("li h3 a");
    const next = titles?.[Math.min(left.index, (titles?.length ?? 1) - 1)];
    (next ?? tabsRef.current?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]'))?.focus();
  }, [list.data]);

  const statusFilter = isAssignmentStatus(state.status) && (within.length === 0 || within.includes(state.status)) ? state.status : "";
  const filtered = Boolean(state.q || statusFilter || (across && state.candidate));
  const clear = () => setState(across ? { candidate: "", q: "", status: "" } : { q: "", status: "" });
  const total = counts.data?.total;

  const settle = (id: string) =>
    setMarking((current) => {
      const next = new Set(current);
      next.delete(id);
      return next;
    });

  const takeBack = (applied: Assignment, to: AssignmentStatus, places: Map<string, number>) => {
    const undo: AssignmentMove & { returning: Map<string, number> } = { assignment: applied, to, places: new Map(), returning: places };
    move.mutateAsync(undo).then(
      () => toast.success("Moved back to To apply", { description: `${applied.job.title} is not marked as applied any more.` }),
      (error: unknown) => toast.error("The job is still marked as applied", { description: errorMessage(error) }),
    );
  };

  const markApplied = (assignment: Assignment) => {
    const before = assignment.status;
    const index = list.data?.items.findIndex((item) => item.id === assignment.id) ?? -1;
    const leaves = section !== "all" && section !== "applied";
    const applied: AssignmentMove = { assignment, to: "APPLIED", places: new Map() };
    setMarking((current) => new Set(current).add(assignment.id));
    if (leaves && index >= 0) leaving.current = { id: assignment.id, index };
    // mutateAsync, not mutate with callbacks: each card that is marked gets its own confirmation, also when
    // a second one is marked before the first has been answered.
    move.mutateAsync(applied).then(
      (detail) => {
        settle(assignment.id);
        toast.success("Marked as applied", {
          description: `${assignment.job.title} at ${assignment.job.company}, for ${assignment.candidate.name}.`,
          duration: 8000,
          action: {
            label: "Undo",
            onClick: () => takeBack({ ...assignment, status: "APPLIED", applied_at: detail.applied_at }, before, applied.places),
          },
        });
      },
      (error: unknown) => {
        settle(assignment.id);
        if (leaving.current?.id === assignment.id) leaving.current = null;
        toast.error("Not marked as applied", { description: `${errorMessage(error)} The job is back under To apply.` });
      },
    );
  };

  return (
    <Tabs
      value={section}
      onValueChange={(next) => setState({ section: next, status: "" })}
      className="gap-4"
      // The sections are views of one list: the panel below is the same region for each of them.
      activationMode="manual"
    >
      <TabsList ref={tabsRef} variant="line" aria-label="Job sections" className="w-full justify-start overflow-x-auto border-b">
        {TABS.map((tab) => {
          const count = tab.value === "all" ? total : counts.data?.[tab.value];
          return (
            <TabsTrigger key={tab.value} value={tab.value} data-section={tab.value} className="flex-none gap-2">
              {tab.label}
              {/* The number is read with the label ("To apply, 12 jobs"), and is shown only once it is known. */}
              {count !== undefined && (
                <span
                  data-slot="section-count"
                  className={cn(
                    "rounded-full border px-1.5 text-xs leading-5 tabular-nums",
                    tab.value === section ? "border-border-strong bg-accent text-foreground" : "text-muted-foreground",
                  )}
                >
                  <span aria-hidden="true">{count.toLocaleString()}</span>
                  <span className="sr-only">
                    , {count.toLocaleString()} {count === 1 ? "job" : "jobs"}
                  </span>
                </span>
              )}
            </TabsTrigger>
          );
        })}
      </TabsList>

      <TabsContent value={section} className="flex flex-col">
        <FilterBar canClear={filtered} onClear={clear}>
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
          {within.length !== 1 && (
            <FilterSelect
              label="Status"
              allLabel="All statuses"
              value={statusFilter}
              onChange={(status) => setState({ status })}
              options={(within.length > 0 ? within : (Object.keys(STATUS_META) as AssignmentStatus[])).map((status) => ({
                value: status,
                label: STATUS_META[status].label,
              }))}
            />
          )}
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
                description="Try a different candidate or status, or clear the search."
                action={
                  <Button variant="outline" size="sm" onClick={clear}>
                    Clear filters
                  </Button>
                }
              />
            ) : section === "all" || total === 0 ? (
              empty
            ) : (
              <EmptyState
                icon={section === "to_apply" ? CircleCheckBig : BriefcaseBusiness}
                title={EMPTY_SECTION[section].title}
                description={EMPTY_SECTION[section].description}
                action={
                  section === "to_apply" ? (
                    <Button variant="outline" size="sm" onClick={() => setState({ section: "applied", status: "" })}>
                      See applied jobs
                    </Button>
                  ) : (
                    <Button variant="outline" size="sm" onClick={() => setState({ section: "to_apply", status: "" })}>
                      See jobs to apply for
                    </Button>
                  )
                }
              />
            )}
          </div>
        ) : (
          <>
            <ul
              ref={listRef}
              aria-label={`${TABS.find((tab) => tab.value === section)?.label ?? "Assigned"} jobs`}
              className={cn("grid gap-3 sm:grid-cols-2 xl:grid-cols-3", list.isPlaceholderData && "opacity-60 transition-opacity")}
            >
              {list.data.items.map((assignment) => (
                <li key={assignment.id} className="min-w-0">
                  <MemberJobCard
                    assignment={assignment}
                    href={memberJobHref(assignment.id, candidateId)}
                    showCandidate={across}
                    onMarkApplied={markApplied}
                    marking={marking.has(assignment.id)}
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
      </TabsContent>
    </Tabs>
  );
}

/** The empty state of a job list that has nothing in it yet. */
export function NoJobsYet({ description, action }: { description: string; action?: ReactNode }) {
  return <EmptyState icon={BriefcaseBusiness} title="No jobs assigned yet" description={description} action={action} />;
}
