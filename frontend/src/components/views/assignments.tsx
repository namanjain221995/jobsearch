"use client";

import { useId, useState } from "react";
import { ClipboardList, Download, SearchX } from "lucide-react";

import { JobFactLine, MatchScore } from "@/components/jobs/job-meta";
import { DataTable, type Column } from "@/components/shared/data-table";
import { FilterBar, FilterSelect, SegmentedFilter } from "@/components/shared/filters";
import { PageHeader } from "@/components/shared/page-header";
import { SearchInput } from "@/components/shared/search-input";
import { EmptyState } from "@/components/shared/states";
import { ApplicationStatusBadge, StageRail, Tag } from "@/components/shared/status-badge";
import { useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { AssignmentDrawer } from "@/components/views/assignment-detail";
import { buildUrl } from "@/lib/api";
import { useToday } from "@/hooks/use-now";
import { formatDate } from "@/lib/format";
import type { ListState } from "@/lib/list-state";
import { useAssignments, useTeams, useUsers } from "@/lib/queries";
import { isAssignmentStatus, STATUS_META, STATUS_ORDER } from "@/lib/status";
import type { Assignment, AssignmentListParams } from "@/lib/types";
import { toPositiveInt, useUrlState } from "@/lib/url-state";

export const ASSIGNMENT_LIST_DEFAULTS = {
  q: "",
  status: "",
  applied: "all",
  team: "",
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

const isDay = (value: string) => /^\d{4}-\d{2}-\d{2}$/.test(value);

function toParams(state: AssignmentListState, fixed: { teamId?: string; memberId?: string; searchRunId?: string }): AssignmentListParams {
  return {
    member_id: fixed.memberId ?? (state.member || undefined),
    team_id: fixed.teamId ?? (state.team || undefined),
    status: isAssignmentStatus(state.status) ? state.status : undefined,
    applied: state.applied === "true" ? true : state.applied === "false" ? false : undefined,
    q: state.q || undefined,
    company: state.company || undefined,
    date_from: isDay(state.from) ? state.from : undefined,
    date_to: isDay(state.to) ? state.to : undefined,
    search_run_id: fixed.searchRunId,
    include_inactive: state.withdrawn === "1" ? true : undefined,
    sort: state.sort,
    page: toPositiveInt(state.page, 1),
    page_size: toPositiveInt(state.page_size, 25),
  };
}

/**
 * Assignments as managers see them: who has which job and how far it has got.
 * Filtering, sorting and paging are done by GET /api/assignments. A row opens
 * the assignment in a drawer, where its status can be changed on the member's behalf.
 */
export function AssignmentsTable({
  listState,
  teamId,
  memberId,
  compact = false,
}: {
  listState: ListState<AssignmentListState>;
  /** Limit to one team (a team page). A Team Lead is always limited to their own team by the API. */
  teamId?: string;
  /** Limit to one member (a member page). */
  memberId?: string;
  /** Fewer filters, for lists embedded in another screen. */
  compact?: boolean;
}) {
  const me = useMe();
  const today = useToday();
  const isAdmin = me.role === "ADMIN";
  const fieldId = useId();
  const [state, setState] = listState;
  const [opened, setOpened] = useState<string | null>(null);
  const params = toParams(state, { teamId, memberId });
  const list = useAssignments(params);

  const scopeTeamId = teamId ?? (isAdmin ? state.team || undefined : me.team?.id);
  const teams = useTeams(true, { enabled: isAdmin && !teamId && !memberId && !compact });
  // The member filter lists the members of the team in view (for an Admin, of the chosen team).
  const members = useUsers(
    { role: "MEMBER", team_id: scopeTeamId, sort: "name", page: 1, page_size: 200 },
    { enabled: !memberId && !compact && (!isAdmin || Boolean(scopeTeamId)), refetchInterval: false },
  );

  const filtered = Boolean(
    state.q || state.status || state.applied !== "all" || state.team || state.member || state.company || state.from || state.to || state.withdrawn,
  );
  const clear = () =>
    setState({ q: "", status: "", applied: "all", team: "", member: "", company: "", from: "", to: "", withdrawn: "" });

  const exportHref = buildUrl("/api/assignments/export.csv", {
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
    ...(memberId
      ? []
      : [
          {
            id: "member",
            header: "Member",
            cell: (row: Assignment) => (
              <div className="flex flex-col gap-0.5">
                <span>{row.member.name}</span>
                {isAdmin && !teamId && <span className="text-2sm text-muted-foreground">{row.team?.name ?? "No team"}</span>}
              </div>
            ),
          },
        ]),
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
        {isAdmin && !teamId && !memberId && !compact && (
          <FilterSelect
            label="Team"
            allLabel="All teams"
            value={state.team}
            onChange={(team) => setState({ team, member: "" })}
            options={(teams.data ?? []).map((team) => ({ value: team.id, label: team.name }))}
          />
        )}
        {!memberId && !compact && (!isAdmin || scopeTeamId) && (
          <FilterSelect
            label="Member"
            allLabel="All members"
            value={state.member}
            onChange={(member) => setState({ member })}
            options={(members.data?.items ?? []).map((user) => ({ value: user.id, label: user.name }))}
          />
        )}
        {!compact && (
          <>
            <div className="flex flex-col gap-1">
              <label className="text-xs text-muted-foreground" htmlFor={`${fieldId}-from`}>
                Assigned from
              </label>
              <Input
                id={`${fieldId}-from`}
                type="date"
                value={state.from}
                max={state.to || today || undefined}
                onChange={(event) => setState({ from: event.target.value })}
                className="w-40"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs text-muted-foreground" htmlFor={`${fieldId}-to`}>
                Assigned to
              </label>
              <Input
                id={`${fieldId}-to`}
                type="date"
                value={state.to}
                min={state.from || undefined}
                max={today || undefined}
                onChange={(event) => setState({ to: event.target.value })}
                className="w-40"
              />
            </div>
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
              description="Run a search or assign jobs by hand. Each job given to a member is listed here."
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
                {memberId ? "" : `${row.member.name}. `}
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
            ? "Jobs your members have applied to, and where each application stands."
            : isLead
              ? "Every job given to a member of your team, and how far each one has got."
              : "One row for each job given to a member, across all teams."
        }
      />
      <AssignmentsTable listState={listState} />
    </>
  );
}
