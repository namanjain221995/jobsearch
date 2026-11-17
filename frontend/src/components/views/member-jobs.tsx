"use client";

import Link from "next/link";
import { BriefcaseBusiness, SearchX, SendHorizontal } from "lucide-react";

import { JobCard } from "@/components/jobs/job-card";
import { JobFactLine, MatchScore } from "@/components/jobs/job-meta";
import { Watermarked } from "@/components/jobs/watermark";
import { DataTable, type Column } from "@/components/shared/data-table";
import { FilterBar, FilterSelect, SegmentedFilter } from "@/components/shared/filters";
import { PageHeader } from "@/components/shared/page-header";
import { Pagination } from "@/components/shared/pagination";
import { SearchInput } from "@/components/shared/search-input";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { ApplicationStatusBadge, StageRail } from "@/components/shared/status-badge";
import { useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { formatDate } from "@/lib/format";
import { useAssignments } from "@/lib/queries";
import { isAssignmentStatus, STATUS_META, STATUS_ORDER } from "@/lib/status";
import type { Assignment, AssignmentListParams } from "@/lib/types";
import { toPositiveInt, useUrlState } from "@/lib/url-state";
import { cn } from "@/lib/utils";

const JOB_DEFAULTS = { q: "", status: "", applied: "all", sort: "recent", page: "1", page_size: "12" };
const APPLICATION_DEFAULTS = { q: "", status: "", applied: "true", sort: "applied", page: "1", page_size: "25" };

const SORTS = [
  { value: "recent", label: "Newest first" },
  { value: "oldest", label: "Oldest first" },
  { value: "score", label: "Best match first" },
  { value: "applied", label: "Recently applied first" },
  { value: "status", label: "By status" },
];

const APPLICATION_STATUSES = STATUS_ORDER.filter((status) => !["ASSIGNED", "VIEWED", "APPLYING", "SKIPPED"].includes(status));

function toParams(state: typeof JOB_DEFAULTS, memberId: string): AssignmentListParams {
  return {
    member_id: memberId,
    q: state.q || undefined,
    status: isAssignmentStatus(state.status) ? state.status : undefined,
    applied: state.applied === "true" ? true : state.applied === "false" ? false : undefined,
    sort: state.sort,
    page: toPositiveInt(state.page, 1),
    page_size: toPositiveInt(state.page_size, 12),
  };
}

/** The member's own job list: every job assigned to them, as cards. */
export function MemberJobsView() {
  const me = useMe();
  const [state, setState] = useUrlState(JOB_DEFAULTS);
  const params = toParams(state, me.id);
  const list = useAssignments(params);
  const filtered = Boolean(state.q || state.status || state.applied !== "all");

  return (
    <Watermarked>
      <PageHeader
        title="My Jobs"
        description="Every job assigned to you. Open one to read the posting, apply on the employer's site, then mark it as applied."
      />
      <FilterBar canClear={filtered} onClear={() => setState({ q: "", status: "", applied: "all" })}>
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
        <FilterSelect
          label="Status"
          allLabel="All statuses"
          value={state.status}
          onChange={(status) => setState({ status })}
          options={STATUS_ORDER.map((status) => ({ value: status, label: STATUS_META[status].label }))}
        />
        <FilterSelect label="Sort" value={state.sort} onChange={(sort) => setState({ sort })} options={SORTS} />
      </FilterBar>

      {list.isPending ? (
        <LoadingSkeleton variant="cards" count={6} />
      ) : list.isError ? (
        <ErrorState error={list.error} onRetry={() => list.refetch()} />
      ) : list.data.items.length === 0 ? (
        <div className="rounded-lg border bg-card">
          {filtered ? (
            <EmptyState
              icon={SearchX}
              title="No jobs match these filters"
              description="Try a different status or clear the search."
              action={
                <Button variant="outline" size="sm" onClick={() => setState({ q: "", status: "", applied: "all" })}>
                  Clear filters
                </Button>
              }
            />
          ) : (
            <EmptyState
              icon={BriefcaseBusiness}
              title="No jobs assigned yet"
              description="Your Team Lead assigns jobs to you. They appear here as soon as they are assigned, without signing in again."
              action={
                <Button asChild variant="outline" size="sm">
                  <Link href="/member/profile">Complete your profile</Link>
                </Button>
              }
            />
          )}
        </div>
      ) : (
        <>
          <ul
            aria-label="Assigned jobs"
            className={cn("grid gap-3 sm:grid-cols-2 xl:grid-cols-3", list.isPlaceholderData && "opacity-60 transition-opacity")}
          >
            {list.data.items.map((assignment) => (
              <li key={assignment.id}>
                <JobCard assignment={assignment} href={`/member/jobs/${assignment.id}`} />
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
    </Watermarked>
  );
}

const APPLICATION_COLUMNS: Column<Assignment>[] = [
  {
    id: "job",
    header: "Job",
    cell: (row) => (
      <div className="flex min-w-0 flex-col gap-0.5">
        <Link href={`/member/jobs/${row.id}`} className="w-fit rounded-sm font-medium underline-offset-4 hover:underline">
          {row.job.title}
        </Link>
        <span className="text-2sm text-muted-foreground">{row.job.company}</span>
      </div>
    ),
  },
  { id: "status", header: "Status", cell: (row) => <ApplicationStatusBadge status={row.status} /> },
  {
    id: "stage",
    header: "Stage",
    className: "min-w-32",
    cell: (row) => <StageRail status={row.status} viewedAt={row.viewed_at} appliedAt={row.applied_at} />,
  },
  { id: "applied", header: "Applied on", cell: (row) => formatDate(row.applied_at) },
  { id: "assigned", header: "Assigned on", cell: (row) => formatDate(row.assigned_at) },
  { id: "score", header: "Match", cell: (row) => <MatchScore score={row.match_score} /> },
];

/** Applications: the assigned jobs that have a confirmed submission, and everything that happened after. */
export function MemberApplicationsView() {
  const me = useMe();
  const [state, setState] = useUrlState(APPLICATION_DEFAULTS);
  const params: AssignmentListParams = { ...toParams(state, me.id), applied: true, page_size: toPositiveInt(state.page_size, 25) };
  const list = useAssignments(params);
  const filtered = Boolean(state.q || state.status);

  return (
    <Watermarked>
      <PageHeader
        title="Applications"
        description="Jobs you have applied to, and where each application stands now."
      />
      <FilterBar canClear={filtered} onClear={() => setState({ q: "", status: "" })}>
        <SearchInput label="Search applications" placeholder="Search title or company" value={state.q} onChange={(q) => setState({ q })} />
        <FilterSelect
          label="Status"
          allLabel="All statuses"
          value={state.status}
          onChange={(status) => setState({ status })}
          options={APPLICATION_STATUSES.map((status) => ({ value: status, label: STATUS_META[status].label }))}
        />
        <FilterSelect label="Sort" value={state.sort} onChange={(sort) => setState({ sort })} options={SORTS} />
      </FilterBar>
      <DataTable
        caption="Your applications"
        columns={APPLICATION_COLUMNS}
        rows={list.data?.items}
        rowKey={(row) => row.id}
        loading={list.isPending}
        fetching={list.isPlaceholderData}
        error={list.error}
        onRetry={() => list.refetch()}
        empty={
          filtered ? (
            <EmptyState icon={SearchX} title="No applications match these filters" description="Try a different status or clear the search." />
          ) : (
            <EmptyState
              icon={SendHorizontal}
              title="No applications yet"
              description="When you apply for a job and mark it as applied, it is listed here."
              action={
                <Button asChild variant="outline" size="sm">
                  <Link href="/member/jobs?applied=false">See jobs to apply for</Link>
                </Button>
              }
            />
          )
        }
        mobileCard={(row) => (
          <div className="flex flex-col gap-2">
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 flex-col gap-0.5">
                <Link href={`/member/jobs/${row.id}`} className="rounded-sm font-medium underline-offset-4 hover:underline">
                  {row.job.title}
                </Link>
                <span className="text-2sm text-muted-foreground">{row.job.company}</span>
              </div>
              <MatchScore score={row.match_score} className="shrink-0" />
            </div>
            <JobFactLine job={row.job} />
            <StageRail status={row.status} viewedAt={row.viewed_at} appliedAt={row.applied_at} />
            <div className="flex flex-wrap items-center justify-between gap-2">
              <ApplicationStatusBadge status={row.status} />
              <span className="text-xs text-muted-foreground">Applied {formatDate(row.applied_at)}</span>
            </div>
          </div>
        )}
        pagination={
          list.data && {
            page: list.data.page,
            pageSize: list.data.page_size,
            total: list.data.total,
            noun: "applications",
            onPageChange: (page) => setState({ page: String(page) }),
            onPageSizeChange: (size) => setState({ page_size: String(size) }),
          }
        }
      />
    </Watermarked>
  );
}
