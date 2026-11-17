"use client";

import Link from "next/link";
import { SearchX, SendHorizontal } from "lucide-react";

import { JobFactLine, MatchScore } from "@/components/jobs/job-meta";
import { Watermarked } from "@/components/jobs/watermark";
import {
  MEMBER_JOB_DEFAULTS,
  MEMBER_JOB_SORTS,
  MemberAssignmentList,
  memberJobParams,
  NoJobsYet,
  useCandidateOptions,
} from "@/components/member/assignment-list";
import { DataTable, type Column } from "@/components/shared/data-table";
import { FilterBar, FilterSelect } from "@/components/shared/filters";
import { PageHeader } from "@/components/shared/page-header";
import { SearchInput } from "@/components/shared/search-input";
import { EmptyState } from "@/components/shared/states";
import { ApplicationStatusBadge, StageRail } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { formatDate } from "@/lib/format";
import { memberCandidateHref, memberJobHref } from "@/lib/member-candidates";
import { useAssignments } from "@/lib/queries";
import { STATUS_META, STATUS_ORDER } from "@/lib/status";
import type { Assignment, AssignmentListParams } from "@/lib/types";
import { toPositiveInt, useUrlState } from "@/lib/url-state";

const APPLICATION_DEFAULTS = { candidate: "", q: "", status: "", applied: "true", sort: "applied", page: "1", page_size: "25" };

const APPLICATION_STATUSES = STATUS_ORDER.filter((status) => !["ASSIGNED", "VIEWED", "APPLYING", "SKIPPED"].includes(status));

/** Every job assigned to any of the member's candidates, as cards that name the candidate. */
export function MemberJobsView() {
  const listState = useUrlState(MEMBER_JOB_DEFAULTS);
  return (
    <Watermarked>
      <PageHeader
        title="All Jobs"
        description="Every job assigned to your candidates. Open one to read the posting, apply on the candidate's behalf on the employer's site, then mark it as applied."
      />
      <MemberAssignmentList
        listState={listState}
        empty={
          <NoJobsYet
            description="Your Team Lead assigns jobs to your candidates. They appear here as soon as they are assigned, without signing in again."
            action={
              <Button asChild variant="outline" size="sm">
                <Link href="/member/candidates">Go to My Candidates</Link>
              </Button>
            }
          />
        }
      />
    </Watermarked>
  );
}

function CandidateLink({ row, className }: { row: Assignment; className?: string }) {
  return (
    <Link href={memberCandidateHref(row.candidate.id)} className={className ?? "rounded-sm underline-offset-4 hover:underline"}>
      {row.candidate.name}
    </Link>
  );
}

const APPLICATION_COLUMNS: Column<Assignment>[] = [
  {
    id: "job",
    header: "Job",
    cell: (row) => (
      <div className="flex min-w-0 flex-col gap-0.5">
        <Link href={memberJobHref(row.id)} className="w-fit rounded-sm font-medium underline-offset-4 hover:underline">
          {row.job.title}
        </Link>
        <span className="text-2sm text-muted-foreground">{row.job.company}</span>
      </div>
    ),
  },
  { id: "candidate", header: "Candidate", cell: (row) => <CandidateLink row={row} /> },
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

/** Applications: the jobs of the member's candidates that have a confirmed submission, and everything that happened after. */
export function MemberApplicationsView() {
  const [state, setState] = useUrlState(APPLICATION_DEFAULTS);
  const candidates = useCandidateOptions();
  const params: AssignmentListParams = { ...memberJobParams(state), applied: true, page_size: toPositiveInt(state.page_size, 25) };
  const list = useAssignments(params);
  const filtered = Boolean(state.q || state.status || state.candidate);

  return (
    <Watermarked>
      <PageHeader
        title="Applications"
        description="Jobs you have applied to for your candidates, and where each application stands now."
      />
      <FilterBar canClear={filtered} onClear={() => setState({ q: "", status: "", candidate: "" })}>
        <SearchInput label="Search applications" placeholder="Search title or company" value={state.q} onChange={(q) => setState({ q })} />
        <FilterSelect
          label="Candidate"
          allLabel="All candidates"
          value={state.candidate}
          onChange={(candidate) => setState({ candidate })}
          options={candidates.options}
          className="sm:max-w-56"
        />
        <FilterSelect
          label="Status"
          allLabel="All statuses"
          value={state.status}
          onChange={(status) => setState({ status })}
          options={APPLICATION_STATUSES.map((status) => ({ value: status, label: STATUS_META[status].label }))}
        />
        <FilterSelect label="Sort" value={state.sort} onChange={(sort) => setState({ sort })} options={MEMBER_JOB_SORTS} />
      </FilterBar>
      <DataTable
        caption="Applications for your candidates"
        columns={APPLICATION_COLUMNS}
        rows={list.data?.items}
        rowKey={(row) => row.id}
        loading={list.isPending}
        fetching={list.isPlaceholderData}
        error={list.error}
        onRetry={() => list.refetch()}
        empty={
          filtered ? (
            <EmptyState icon={SearchX} title="No applications match these filters" description="Try a different candidate or status, or clear the search." />
          ) : (
            <EmptyState
              icon={SendHorizontal}
              title="No applications yet"
              description="When you apply for a job on a candidate's behalf and mark it as applied, it is listed here."
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
                <Link href={memberJobHref(row.id)} className="rounded-sm font-medium underline-offset-4 hover:underline">
                  {row.job.title}
                </Link>
                <span className="text-2sm text-muted-foreground">{row.job.company}</span>
              </div>
              <MatchScore score={row.match_score} className="shrink-0" />
            </div>
            <p className="min-w-0 text-2sm break-words">
              <span className="text-muted-foreground">For </span>
              <CandidateLink row={row} className="rounded-sm font-medium underline-offset-4 hover:underline" />
            </p>
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
