"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { FileCheck2, FileWarning, SearchX, Users } from "lucide-react";

import { UserActions, UserStatusTag } from "@/components/people/user-actions";
import { DataTable, type Column } from "@/components/shared/data-table";
import { FilterBar, FilterSelect } from "@/components/shared/filters";
import { SearchInput } from "@/components/shared/search-input";
import { EmptyState } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { useNow } from "@/hooks/use-now";
import { formatPercent, formatRelative } from "@/lib/format";
import type { ListState } from "@/lib/list-state";
import { useTeams, useUsers } from "@/lib/queries";
import type { AssignmentStats, MemberRow, TeamRow, User, UserListParams } from "@/lib/types";
import { toPositiveInt } from "@/lib/url-state";
import { cn } from "@/lib/utils";

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

const EMPTY_STATS: AssignmentStats = {
  assigned: 0,
  not_applied: 0,
  applied: 0,
  interviews: 0,
  offers: 0,
  closed: 0,
  viewed: 0,
  link_opens: 0,
  completion_rate: 0,
};

/** Applied as a share of what could have been applied to. A bar and the figure, so rows compare at a glance. */
export function CompletionMeter({ rate, className }: { rate: number; className?: string }) {
  const value = Math.max(0, Math.min(100, rate));
  return (
    <span className={cn("inline-flex items-center gap-2", className)}>
      <span aria-hidden="true" className="h-1.5 w-14 overflow-hidden rounded-full bg-foreground/8">
        <span className="block h-full rounded-full bg-status-applied" style={{ width: `${value}%` }} />
      </span>
      <span className="min-w-9 text-right tabular-nums">{formatPercent(value)}</span>
    </span>
  );
}

export function ResumeTag({ hasResume }: { hasResume: boolean }) {
  return hasResume ? (
    <Tag tone="success">
      <FileCheck2 aria-hidden="true" />
      Resume uploaded
    </Tag>
  ) : (
    <Tag tone="warning">
      <FileWarning aria-hidden="true" />
      No resume
    </Tag>
  );
}

function LastActivity({ value }: { value: string | null | undefined }) {
  const now = useNow();
  return <span className="whitespace-nowrap text-muted-foreground">{value ? formatRelative(value, now) : "No activity yet"}</span>;
}

function StatGrid({ stats }: { stats: AssignmentStats }) {
  const cells: Array<[string, number]> = [
    ["Assigned", stats.assigned],
    ["Not applied", stats.not_applied],
    ["Applied", stats.applied],
    ["Interviews", stats.interviews],
  ];
  return (
    <dl className="grid grid-cols-4 gap-2">
      {cells.map(([label, value]) => (
        <div key={label} className="flex flex-col">
          <dt className="text-xs text-muted-foreground">{label}</dt>
          <dd className="font-figure text-xl leading-tight">{value.toLocaleString()}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * Members with their resume state and application figures. Searching, sorting
 * and paging are done by the API (GET /api/users).
 */
export function MemberTable({
  listState,
  teamId,
  showTeam = false,
  memberHref,
  actions,
  emptyAction,
}: {
  listState: ListState<MemberListState>;
  /** Limit the table to one team (a Team Lead's own team, or a team page). */
  teamId?: string;
  /** Show the team column and filter (Admin, across teams). */
  showTeam?: boolean;
  memberHref: (id: string) => string;
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
      id: "resume",
      header: "Resume",
      cell: (user) => (
        <div className="flex flex-col items-start gap-1">
          <ResumeTag hasResume={user.has_resume} />
          <span className="text-xs text-muted-foreground">Profile {user.profile_completion}% complete</span>
        </div>
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
              description="Invite the first member. They appear here as soon as the invitation is created."
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
              <ResumeTag hasResume={user.has_resume} />
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

/** Members of one team with their figures for the chosen date range (TeamAnalytics.members). */
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
          {!row.has_resume && <span className="text-xs text-warning">No resume uploaded</span>}
        </div>
      ),
    },
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
      empty={<EmptyState compact icon={Users} title="No members in this team yet" description="Invite members to see how each of them is doing." />}
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
            <ResumeTag hasResume={row.has_resume} />
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
              {row.team_lead ? `Led by ${row.team_lead.name}` : "No Team Lead"}, {row.members} member{row.members === 1 ? "" : "s"}
            </span>
          </div>
          <StatGrid stats={row.stats} />
          <CompletionMeter rate={row.stats.completion_rate} className="text-sm" />
        </div>
      )}
    />
  );
}
