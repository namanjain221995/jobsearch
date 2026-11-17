"use client";

import Link from "next/link";
import { useId, useState } from "react";
import { ClipboardList, Download, SearchX } from "lucide-react";

import { JobFactLine, MatchScore } from "@/components/jobs/job-meta";
import { DayInput } from "@/components/shared/day-input";
import { DataTable, type Column } from "@/components/shared/data-table";
import { FilterBar, FilterSelect, SegmentedFilter } from "@/components/shared/filters";
import { PageHeader } from "@/components/shared/page-header";
import { SearchInput } from "@/components/shared/search-input";
import { EmptyState } from "@/components/shared/states";
import { ApplicationStatusBadge, StageRail, Tag } from "@/components/shared/status-badge";
import { ResponsibleMember } from "@/components/candidates/candidate-meta";
import { useArea, useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { AssignmentDrawer } from "@/components/views/assignment-detail";
import { buildUrl } from "@/lib/api";
import { useToday } from "@/hooks/use-now";
import { formatDate } from "@/lib/format";
import type { ListState } from "@/lib/list-state";
import { useAssignments, useCandidates, useTeams, useUsers } from "@/lib/queries";
import { isAssignmentStatus, STATUS_META, STATUS_ORDER } from "@/lib/status";
import type { Assignment, AssignmentListParams } from "@/lib/types";
import { isApiDay } from "@/lib/date-range";
import { toPositiveInt, useUrlState } from "@/lib/url-state";

export const ASSIGNMENT_LIST_DEFAULTS = {
  q: "",
  status: "",
  applied: "all",
  team: "",
  candidate: "",
  member: "",
  company: "",
  from: "",
  to: "",
  withdrawn: "",
  sort: "recent",
  page: "1",
  page_size: "25",
};
export type AssignmentListState = typeof ASSIGNMENT_LIST_DEFAULTS;

const SORTS = [
  { value: "recent", label: "Newest first" },
  { value: "oldest", label: "Oldest first" },
  { value: "score", label: "Best match first" },
  { value: "applied", label: "Recently applied first" },
  { value: "status", label: "By status" },
];


function toParams(
  state: AssignmentListState,
  fixed: { teamId?: string; candidateId?: string; memberId?: string; searchRunId?: string },
): AssignmentListParams {
  return {
    candidate_id: fixed.candidateId ?? (state.candidate || undefined),
    member_id: fixed.memberId ?? (state.member || undefined),
    team_id: fixed.teamId ?? (state.team || undefined),
    status: isAssignmentStatus(state.status) ? state.status : undefined,
    applied: state.applied === "true" ? true : state.applied === "false" ? false : undefined,
    q: state.q || undefined,
    company: state.company || undefined,
    date_from: isApiDay(state.from) ? state.from : undefined,
    date_to: isApiDay(state.to) ? state.to : undefined,
    search_run_id: fixed.searchRunId,
    include_inactive: state.withdrawn === "1" ? true : undefined,
    sort: state.sort,
    page: toPositiveInt(state.page, 1),
    page_size: toPositiveInt(state.page_size, 25),
  };
}

/**
 * Assignments as managers see them: which candidate has which job, who handles it
 * and how far it has got. Filtering, sorting and paging are done by GET /api/assignments.
 * A row opens the assignment in a drawer, where its status can be changed in place of
 * the responsible member.
 */
export function AssignmentsTable({
  listState,
  teamId,
  candidateId,
  memberId,
  compact = false,
}: {
  listState: ListState<AssignmentListState>;
  /** Limit to one team (a team page). A Team Lead is always limited to their own team by the API. */
  teamId?: string;
  /** Limit to one candidate (a candidate page). */
  candidateId?: string;
  /** Limit to the candidates one member is responsible for today (a member page). */
  memberId?: string;
  /** Fewer filters, for lists embedded in another screen. */
  compact?: boolean;
}) {
  const me = useMe();
  const area = useArea();
  const today = useToday();
  const isAdmin = me.role === "ADMIN";
  const fieldId = useId();
  const [state, setState] = listState;
  const [opened, setOpened] = useState<string | null>(null);
  const params = toParams(state, { teamId, candidateId, memberId });
  const list = useAssignments(params);

  const scopeTeamId = teamId ?? (isAdmin ? state.team || undefined : me.team?.id);
  const scoped = Boolean(candidateId || memberId);
  const teams = useTeams(true, { enabled: isAdmin && !teamId && !scoped && !compact });
  // The candidate and member filters list the people of the team in view (for an Admin, of the chosen team).
  const pickPeople = !scoped && !compact && (!isAdmin || Boolean(scopeTeamId));
  const members = useUsers(
    { role: "MEMBER", team_id: scopeTeamId, sort: "name", page: 1, page_size: 200 },
    { enabled: pickPeople, refetchInterval: false },
  );
  const candidates = useCandidates(
    { team_id: scopeTeamId, assigned_member_id: state.member || undefined, sort: "name", page: 1, page_size: 200 },
    { enabled: pickPeople, live: false },
  );

  const filtered = Boolean(
    state.q || state.status || state.applied !== "all" || state.team || state.candidate || state.member || state.company || state.from || state.to || state.withdrawn,
  );
  const clear = () =>
    setState({ q: "", status: "", applied: "all", team: "", candidate: "", member: "", company: "", from: "", to: "", withdrawn: "" });

  const exportHref = buildUrl("/api/assignments/export.csv", {
    candidate_id: params.candidate_id,
    member_id: params.member_id,
    team_id: params.team_id,
    status: params.status,
    applied: params.applied,
    date_from: params.date_from,
    date_to: params.date_to,
  });

  const jobCell = (row: Assignment) => (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setOpened(row.id)}
          className="w-fit rounded-sm text-left font-medium underline-offset-4 hover:underline"
        >
          {row.job.title}
        </button>
        {!row.is_active && <Tag>Withdrawn from list</Tag>}
      </span>
      <span className="text-2sm text-muted-foreground">{row.job.company}</span>
    </div>
  );

  const columns: Column<Assignment>[] = [
    { id: "job", header: "Job", cell: jobCell },
    ...(candidateId
      ? []
      : [
          {
            id: "candidate",
            header: "Candidate",
            cell: (row: Assignment) => (
              <div className="flex flex-col gap-0.5">
                <Link href={`${area}/candidates/${row.candidate.id}`} className="w-fit rounded-sm underline-offset-4 hover:underline">
                  {row.candidate.name}
                </Link>
                {isAdmin && !teamId && <span className="text-2sm text-muted-foreground">{row.team?.name ?? "No team"}</span>}
              </div>
            ),
          },
        ]),
    ...(memberId
      ? []
      : [{ id: "member", header: "Responsible member", cell: (row: Assignment) => <ResponsibleMember member={row.member} /> }]),
    { id: "status", header: "Status", cell: (row) => <ApplicationStatusBadge status={row.status} /> },
    {
      id: "stage",
      header: "Stage",
      className: "min-w-28",
      cell: (row) => <StageRail status={row.status} viewedAt={row.viewed_at} appliedAt={row.applied_at} />,
    },
    { id: "score", header: "Match", cell: (row) => <MatchScore score={row.match_score} /> },
    {
      id: "assigned",
      header: "Assigned",
      cell: (row) => (
        <div className="flex flex-col gap-0.5 whitespace-nowrap">
          <span>{formatDate(row.assigned_at)}</span>
          {row.assigned_by && <span className="text-2sm text-muted-foreground">by {row.assigned_by.name}</span>}
        </div>
      ),
    },
    { id: "applied", header: "Applied", cell: (row) => <span className="whitespace-nowrap">{row.applied_at ? formatDate(row.applied_at) : "Not yet"}</span> },
    { id: "opens", header: "Link opens", align: "right", cell: (row) => row.link_opens.toLocaleString() },
    { id: "notes", header: "Notes", align: "right", cell: (row) => row.note_count.toLocaleString() },
  ];

  return (
    <div className="flex flex-col">
      <FilterBar canClear={filtered} onClear={clear}>
        <SegmentedFilter
          label="Show"
          value={state.applied}
          onChange={(applied) => setState({ applied })}
          options={[
            { value: "all", label: "All" },
            { value: "false", label: "Not applied" },
            { value: "true", label: "Applied" },
          ]}
        />
        <SearchInput label="Search assignments" placeholder="Search title or company" value={state.q} onChange={(q) => setState({ q })} />
        <FilterSelect
          label="Status"
          allLabel="All statuses"
          value={state.status}
          onChange={(status) => setState({ status })}
          options={STATUS_ORDER.map((status) => ({ value: status, label: STATUS_META[status].label }))}
        />
        {isAdmin && !teamId && !scoped && !compact && (
          <FilterSelect
            label="Team"
            allLabel="All teams"
            value={state.team}
            onChange={(team) => setState({ team, candidate: "", member: "" })}
            options={(teams.data ?? []).map((team) => ({ value: team.id, label: team.name }))}
          />
        )}
        {pickPeople && (
          <>
            <FilterSelect
              label="Candidate"
              allLabel="All candidates"
              value={state.candidate}
              onChange={(candidate) => setState({ candidate })}
              options={(candidates.data?.items ?? []).map((candidate) => ({ value: candidate.id, label: candidate.name }))}
            />
            <FilterSelect
              label="Responsible member"
              allLabel="Any responsible member"
              value={state.member}
              onChange={(member) => setState({ member, candidate: "" })}
              options={(members.data?.items ?? []).map((user) => ({ value: user.id, label: user.name }))}
            />
          </>
        )}
        {!compact && (
          <>
            <DayInput id={`${fieldId}-from`} label="Assigned from" value={state.from} max={state.to || today || undefined} onChange={(from) => setState({ from })} />
            <DayInput id={`${fieldId}-to`} label="Assigned to" value={state.to} min={state.from || undefined} max={today || undefined} onChange={(to) => setState({ to })} />
            <FilterSelect
              label="Withdrawn assignments"
              value={state.withdrawn === "1" ? "1" : "0"}
              onChange={(value) => setState({ withdrawn: value === "1" ? "1" : "" })}
              options={[
                { value: "0", label: "Active only" },
                { value: "1", label: "Include withdrawn" },
              ]}
            />
          </>
        )}
        <FilterSelect label="Sort" value={state.sort} onChange={(sort) => setState({ sort })} options={SORTS} />
        {!compact && (
          <Button asChild variant="outline" className="ml-auto">
            <a href={exportHref} download="assignments.csv">
              <Download data-icon="inline-start" />
              Export CSV
            </a>
          </Button>
        )}
      </FilterBar>
      <DataTable
        caption="Assignments"
        columns={columns}
        rows={list.data?.items}
        rowKey={(row) => row.id}
        loading={list.isPending}
        fetching={list.isPlaceholderData}
        error={list.error}
        onRetry={() => list.refetch()}
        rowClassName={(row) => (row.is_active ? undefined : "opacity-70")}
        empty={
          filtered ? (
            <EmptyState
              icon={SearchX}
              title="No assignments match these filters"
              description="Widen the filters or clear them."
              action={
                <Button variant="outline" size="sm" onClick={clear}>
                  Clear filters
                </Button>
              }
            />
          ) : (
            <EmptyState
              icon={ClipboardList}
              title="No jobs assigned yet"
              description={candidateId ? "Run a search for this candidate or assign jobs by hand. Each job given to the candidate is listed here." : "Run a search or assign jobs by hand. Each job given to a candidate is listed here."}
            />
          )
        }
        mobileCard={(row) => (
          <div className="flex flex-col gap-2">
            <div className="flex items-start justify-between gap-3">
              {jobCell(row)}
              <MatchScore score={row.match_score} className="shrink-0" />
            </div>
            <JobFactLine job={row.job} />
            <StageRail status={row.status} viewedAt={row.viewed_at} appliedAt={row.applied_at} />
            <div className="flex flex-wrap items-center justify-between gap-2">
              <ApplicationStatusBadge status={row.status} />
              <span className="text-xs text-muted-foreground">
                {candidateId ? "" : `${row.candidate.name}. `}
                {row.applied_at ? `Applied ${formatDate(row.applied_at)}` : `Assigned ${formatDate(row.assigned_at)}`}
              </span>
            </div>
          </div>
        )}
        pagination={
          list.data && {
            page: list.data.page,
            pageSize: list.data.page_size,
            total: list.data.total,
            noun: "assignments",
            onPageChange: (page) => setState({ page: String(page) }),
            onPageSizeChange: (size) => setState({ page_size: String(size) }),
          }
        }
      />
      <AssignmentDrawer assignmentId={opened} onOpenChange={(open) => !open && setOpened(null)} />
    </div>
  );
}

/** The assignments screen of a Team Lead ("Assigned Jobs", "Applications") and of an Admin. */
export function AssignmentsView() {
  const me = useMe();
  const listState = useUrlState(ASSIGNMENT_LIST_DEFAULTS);
  const appliedOnly = listState[0].applied === "true";
  const isLead = me.role === "TEAM_LEAD";
  return (
    <>
      <PageHeader
        title={isLead ? (appliedOnly ? "Applications" : "Assigned Jobs") : "Assignments"}
        description={
          appliedOnly
            ? "Jobs that were applied to for your candidates, and where each application stands."
            : isLead
              ? "Every job given to a candidate of your team, who handles it, and how far it has got."
              : "One row for each job given to a candidate, across all teams."
        }
      />
      <AssignmentsTable listState={listState} />
    </>
  );
}
