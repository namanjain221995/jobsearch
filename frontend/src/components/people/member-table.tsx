"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { SearchX, Users } from "lucide-react";

import { EMPTY_STATS } from "@/components/candidates/candidate-meta";
import { CompletionMeter, StatGrid } from "@/components/candidates/candidate-table";
import { UserActions, UserStatusTag } from "@/components/people/user-actions";
import { DataTable, type Column } from "@/components/shared/data-table";
import { FilterBar, FilterSelect } from "@/components/shared/filters";
import { SearchInput } from "@/components/shared/search-input";
import { EmptyState } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { useNow } from "@/hooks/use-now";
import { formatRelative, plural } from "@/lib/format";
import type { ListState } from "@/lib/list-state";
import { useTeams, useUsers } from "@/lib/queries";
import type { MemberRow, TeamRow, User, UserListParams } from "@/lib/types";
import { toPositiveInt } from "@/lib/url-state";

export const MEMBER_LIST_DEFAULTS = { q: "", status: "", team: "", sort: "name", page: "1", page_size: "25" };
export type MemberListState = typeof MEMBER_LIST_DEFAULTS;

const SORTS = [
  { value: "name", label: "Name, A to Z" },
  { value: "activity", label: "Latest activity first" },
  { value: "recent", label: "Newest account first" },
];

const STATUSES = [
  { value: "active", label: "Active" },
  { value: "invited", label: "Invited" },
  { value: "deactivated", label: "Deactivated" },
];

function LastActivity({ value }: { value: string | null | undefined }) {
  const now = useNow();
  return <span className="whitespace-nowrap text-muted-foreground">{value ? formatRelative(value, now) : "No activity yet"}</span>;
}

export { CompletionMeter };

/**
 * Members: the employees who apply to jobs on their candidates' behalf. Each row shows how many
 * candidates the member is responsible for today and the figures over those candidates' jobs. Searching, sorting
 * and paging are done by the API (GET /api/users).
 */
export function MemberTable({
  listState,
  teamId,
  showTeam = false,
  memberHref,
  candidatesHref,
  actions,
  emptyAction,
}: {
  listState: ListState<MemberListState>;
  /** Limit the table to one team (a Team Lead's own team, or a team page). */
  teamId?: string;
  /** Show the team column and filter (Admin, across teams). */
  showTeam?: boolean;
  memberHref: (id: string) => string;
  /** Where "N candidates" leads: the candidates list filtered to that member. Left out, the count is plain text. */
  candidatesHref?: (user: User) => string;
  /** Buttons shown at the end of the filter row (Add Member). */
  actions?: ReactNode;
  emptyAction?: ReactNode;
}) {
  const [state, setState] = listState;
  const teams = useTeams(true, { enabled: showTeam && !teamId });
  const params: UserListParams = {
    role: "MEMBER",
    team_id: teamId ?? (state.team || undefined),
    status: state.status || undefined,
    q: state.q || undefined,
    sort: state.sort,
    page: toPositiveInt(state.page, 1),
    page_size: toPositiveInt(state.page_size, 25),
  };
  const users = useUsers(params);
  const filtered = Boolean(state.q || state.status || state.team);

  const columns: Column<User>[] = [
    {
      id: "member",
      header: "Member",
      cell: (user) => (
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="flex flex-wrap items-center gap-2">
            <Link href={memberHref(user.id)} className="rounded-sm font-medium underline-offset-4 hover:underline">
              {user.name}
            </Link>
            {user.status !== "active" && <UserStatusTag status={user.status} />}
          </span>
          <span className="text-2sm break-all text-muted-foreground">{user.email}</span>
        </div>
      ),
    },
    ...(showTeam && !teamId
      ? [{ id: "team", header: "Team", cell: (user: User) => user.team?.name ?? <span className="text-muted-foreground">No team</span> }]
      : []),
    {
      id: "candidates",
      header: "Candidates",
      align: "right",
      cell: (user) =>
        candidatesHref && user.candidate_count > 0 ? (
          <Link href={candidatesHref(user)} className="rounded-sm underline-offset-4 hover:underline" aria-label={`${plural(user.candidate_count, "candidate")} of ${user.name}`}>
            {user.candidate_count.toLocaleString()}
          </Link>
        ) : (
          user.candidate_count.toLocaleString()
        ),
    },
    { id: "assigned", header: "Assigned", align: "right", cell: (user) => (user.stats ?? EMPTY_STATS).assigned.toLocaleString() },
    { id: "not_applied", header: "Not applied", align: "right", cell: (user) => (user.stats ?? EMPTY_STATS).not_applied.toLocaleString() },
    { id: "applied", header: "Applied", align: "right", cell: (user) => (user.stats ?? EMPTY_STATS).applied.toLocaleString() },
    { id: "interviews", header: "Interviews", align: "right", cell: (user) => (user.stats ?? EMPTY_STATS).interviews.toLocaleString() },
    { id: "completion", header: "Completion rate", cell: (user) => <CompletionMeter rate={(user.stats ?? EMPTY_STATS).completion_rate} /> },
    { id: "activity", header: "Last activity", cell: (user) => <LastActivity value={user.last_activity_at} /> },
    { id: "actions", header: <span className="sr-only">Actions</span>, align: "right", cell: (user) => <UserActions user={user} /> },
  ];

  return (
    <div className="flex flex-col">
      <FilterBar canClear={filtered} onClear={() => setState({ q: "", status: "", team: "" })}>
        <SearchInput label="Search members" placeholder="Search name or e-mail" value={state.q} onChange={(q) => setState({ q })} />
        {showTeam && !teamId && (
          <FilterSelect
            label="Team"
            allLabel="All teams"
            value={state.team}
            onChange={(team) => setState({ team })}
            options={(teams.data ?? []).map((team) => ({ value: team.id, label: team.name }))}
          />
        )}
        <FilterSelect label="Account status" allLabel="All accounts" value={state.status} onChange={(status) => setState({ status })} options={STATUSES} />
        <FilterSelect label="Sort" value={state.sort} onChange={(sort) => setState({ sort })} options={SORTS} />
        {actions && <div className="ml-auto flex flex-wrap gap-2">{actions}</div>}
      </FilterBar>
      <DataTable
        caption="Team members"
        columns={columns}
        rows={users.data?.items}
        rowKey={(user) => user.id}
        loading={users.isPending}
        fetching={users.isPlaceholderData}
        error={users.error}
        onRetry={() => users.refetch()}
        empty={
          filtered ? (
            <EmptyState icon={SearchX} title="No members match these filters" description="Check the spelling or clear the filters." />
          ) : (
            <EmptyState
              icon={Users}
              title="No members yet"
              description="Invite the first member: an employee who applies to jobs for the candidates you assign to them. They appear here as soon as the invitation is created."
              action={emptyAction}
            />
          )
        }
        mobileCard={(user) => (
          <div className="flex flex-col gap-3">
            <div className="flex items-start justify-between gap-2">
              <div className="flex min-w-0 flex-col gap-0.5">
                <span className="flex flex-wrap items-center gap-2">
                  <Link href={memberHref(user.id)} className="rounded-sm font-medium underline-offset-4 hover:underline">
                    {user.name}
                  </Link>
                  {user.status !== "active" && <UserStatusTag status={user.status} />}
                </span>
                <span className="text-2sm break-all text-muted-foreground">{user.email}</span>
                {showTeam && user.team && <span className="text-2sm text-muted-foreground">{user.team.name}</span>}
              </div>
              <UserActions user={user} />
            </div>
            <StatGrid stats={user.stats ?? EMPTY_STATS} />
            <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span className="text-2sm text-muted-foreground">{plural(user.candidate_count, "candidate")}</span>
              <CompletionMeter rate={(user.stats ?? EMPTY_STATS).completion_rate} />
            </div>
            <LastActivity value={user.last_activity_at} />
          </div>
        )}
        pagination={
          users.data && {
            page: users.data.page,
            pageSize: users.data.page_size,
            total: users.data.total,
            noun: "members",
            onPageChange: (page) => setState({ page: String(page) }),
            onPageSizeChange: (size) => setState({ page_size: String(size) }),
          }
        }
      />
    </div>
  );
}

/* ------------------------------------------------------------------ analytics tables (every row is loaded, so they sort in the browser) */

/** The members of one team as the people responsible for candidates: how many each holds today, and the figures over those candidates' jobs (TeamAnalytics.members). */
export function MemberPerformanceTable({
  rows,
  loading,
  fetching,
  memberHref,
}: {
  rows: MemberRow[] | undefined;
  loading?: boolean;
  fetching?: boolean;
  memberHref: (id: string) => string;
}) {
  const columns: Column<MemberRow>[] = [
    {
      id: "member",
      header: "Member",
      sortValue: (row) => row.member.name,
      cell: (row) => (
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="flex flex-wrap items-center gap-2">
            <Link href={memberHref(row.member.id)} className="rounded-sm font-medium underline-offset-4 hover:underline">
              {row.member.name}
            </Link>
            {row.status !== "active" && <UserStatusTag status={row.status} />}
          </span>
        </div>
      ),
    },
    { id: "candidates", header: "Candidates", align: "right", sortValue: (row) => row.candidates, cell: (row) => row.candidates.toLocaleString() },
    { id: "assigned", header: "Assigned", align: "right", sortValue: (row) => row.stats.assigned, cell: (row) => row.stats.assigned.toLocaleString() },
    { id: "not_applied", header: "Not applied", align: "right", sortValue: (row) => row.stats.not_applied, cell: (row) => row.stats.not_applied.toLocaleString() },
    { id: "applied", header: "Applied", align: "right", sortValue: (row) => row.stats.applied, cell: (row) => row.stats.applied.toLocaleString() },
    { id: "interviews", header: "Interviews", align: "right", sortValue: (row) => row.stats.interviews, cell: (row) => row.stats.interviews.toLocaleString() },
    { id: "offers", header: "Offers", align: "right", sortValue: (row) => row.stats.offers, cell: (row) => row.stats.offers.toLocaleString() },
    { id: "completion", header: "Completion rate", sortValue: (row) => row.stats.completion_rate, cell: (row) => <CompletionMeter rate={row.stats.completion_rate} /> },
    { id: "activity", header: "Last activity", sortValue: (row) => row.last_activity_at ?? "", cell: (row) => <LastActivity value={row.last_activity_at} /> },
  ];
  return (
    <DataTable
      caption="Member performance"
      columns={columns}
      rows={rows}
      rowKey={(row) => row.member.id}
      loading={loading}
      fetching={fetching}
      defaultSort={{ id: "not_applied", direction: "desc" }}
      empty={<EmptyState compact icon={Users} title="No members in this team yet" description="Invite members, then assign candidates to them." />}
      mobileCard={(row) => (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Link href={memberHref(row.member.id)} className="rounded-sm font-medium underline-offset-4 hover:underline">
              {row.member.name}
            </Link>
            {row.status !== "active" && <UserStatusTag status={row.status} />}
          </div>
          <StatGrid stats={row.stats} />
          <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
            <span className="text-2sm text-muted-foreground">{plural(row.candidates, "candidate")}</span>
            <CompletionMeter rate={row.stats.completion_rate} />
          </div>
        </div>
      )}
    />
  );
}

/** One row per team, linking to the team's page (AdminAnalytics.teams). */
export function TeamPerformanceTable({ rows, loading, fetching }: { rows: TeamRow[] | undefined; loading?: boolean; fetching?: boolean }) {
  const columns: Column<TeamRow>[] = [
    {
      id: "team",
      header: "Team",
      sortValue: (row) => row.team.name,
      cell: (row) => (
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="flex flex-wrap items-center gap-2">
            <Link href={`/admin/teams/${row.team.id}`} className="rounded-sm font-medium underline-offset-4 hover:underline">
              {row.team.name}
            </Link>
            {!row.is_active && <Tag>Deactivated</Tag>}
          </span>
          <span className="text-2sm text-muted-foreground">{row.team_lead ? `Led by ${row.team_lead.name}` : "No Team Lead"}</span>
        </div>
      ),
    },
    { id: "members", header: "Members", align: "right", sortValue: (row) => row.members, cell: (row) => row.members.toLocaleString() },
    { id: "candidates", header: "Candidates", align: "right", sortValue: (row) => row.candidates, cell: (row) => row.candidates.toLocaleString() },
    { id: "assigned", header: "Assigned", align: "right", sortValue: (row) => row.stats.assigned, cell: (row) => row.stats.assigned.toLocaleString() },
    { id: "not_applied", header: "Not applied", align: "right", sortValue: (row) => row.stats.not_applied, cell: (row) => row.stats.not_applied.toLocaleString() },
    { id: "applied", header: "Applied", align: "right", sortValue: (row) => row.stats.applied, cell: (row) => row.stats.applied.toLocaleString() },
    { id: "interviews", header: "Interviews", align: "right", sortValue: (row) => row.stats.interviews, cell: (row) => row.stats.interviews.toLocaleString() },
    { id: "offers", header: "Offers", align: "right", sortValue: (row) => row.stats.offers, cell: (row) => row.stats.offers.toLocaleString() },
    { id: "completion", header: "Completion rate", sortValue: (row) => row.stats.completion_rate, cell: (row) => <CompletionMeter rate={row.stats.completion_rate} /> },
  ];
  return (
    <DataTable
      caption="Team performance"
      columns={columns}
      rows={rows}
      rowKey={(row) => row.team.id}
      loading={loading}
      fetching={fetching}
      defaultSort={{ id: "assigned", direction: "desc" }}
      empty={<EmptyState compact icon={Users} title="No teams match these filters" description="Create a team, or widen the filters." />}
      mobileCard={(row) => (
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-0.5">
            <span className="flex flex-wrap items-center gap-2">
              <Link href={`/admin/teams/${row.team.id}`} className="rounded-sm font-medium underline-offset-4 hover:underline">
                {row.team.name}
              </Link>
              {!row.is_active && <Tag>Deactivated</Tag>}
            </span>
            <span className="text-2sm text-muted-foreground">
              {row.team_lead ? `Led by ${row.team_lead.name}` : "No Team Lead"}, {plural(row.members, "member")}, {plural(row.candidates, "candidate")}
            </span>
          </div>
          <StatGrid stats={row.stats} />
          <CompletionMeter rate={row.stats.completion_rate} className="text-sm" />
        </div>
      )}
    />
  );
}
