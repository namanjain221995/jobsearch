"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { Contact, SearchX } from "lucide-react";

import { CandidateActions } from "@/components/candidates/candidate-actions";
import { CandidateInactiveTag, ResponsibleMember, ResumeTag } from "@/components/candidates/candidate-meta";
import { DataTable, type Column } from "@/components/shared/data-table";
import { FilterBar, FilterSelect } from "@/components/shared/filters";
import { SearchInput } from "@/components/shared/search-input";
import { EmptyState } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { useArea, useMe } from "@/components/shell/me-context";
import { useNow } from "@/hooks/use-now";
import { formatPercent, formatRelative } from "@/lib/format";
import type { ListState } from "@/lib/list-state";
import { useCandidates, useTeams, useUsers } from "@/lib/queries";
import type { AssignmentStats, Candidate, CandidateListParams, CandidateRow } from "@/lib/types";
import { toPositiveInt } from "@/lib/url-state";
import { cn } from "@/lib/utils";

export const CANDIDATE_LIST_DEFAULTS = {
  q: "",
  team: "",
  member: "",
  resume: "",
  active: "true",
  sort: "name",
  page: "1",
  page_size: "25",
};
export type CandidateListState = typeof CANDIDATE_LIST_DEFAULTS;

/** The value of the member filter that means "candidates nobody is responsible for". */
export const UNASSIGNED = "unassigned";

const SORTS = [
  { value: "name", label: "Name, A to Z" },
  { value: "newest_job", label: "Newest job first" },
  { value: "not_applied", label: "Most not applied first" },
  { value: "recent", label: "Newest candidate first" },
  { value: "updated", label: "Details recently changed first" },
];

const ACTIVE_OPTIONS = [
  { value: "true", label: "Active candidates" },
  { value: "false", label: "Deactivated candidates" },
  { value: "all", label: "Active and deactivated" },
];

const RESUME_OPTIONS = [
  { value: "yes", label: "Resume uploaded" },
  { value: "no", label: "No resume" },
];

/** When the candidate last received a job (their newest active assignment). */
function NewestJob({ value }: { value: string | null | undefined }) {
  const now = useNow();
  return <span className="whitespace-nowrap text-muted-foreground">{value ? formatRelative(value, now) : "No jobs yet"}</span>;
}

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

/** The figures of one row, for the card layout on small screens. */
export function StatGrid({ stats, offers = false }: { stats: AssignmentStats; offers?: boolean }) {
  const cells: Array<[string, number]> = [
    ["Assigned", stats.assigned],
    ["Not applied", stats.not_applied],
    ["Applied", stats.applied],
    ["Interviews", stats.interviews],
    ...(offers ? ([["Offers", stats.offers]] as Array<[string, number]>) : []),
  ];
  return (
    <dl className={cn("grid gap-2", offers ? "grid-cols-5" : "grid-cols-4")}>
      {cells.map(([label, value]) => (
        <div key={label} className="flex min-w-0 flex-col">
          <dt className="truncate text-xs text-muted-foreground">{label}</dt>
          <dd className="font-figure text-xl leading-tight">{value.toLocaleString()}</dd>
        </div>
      ))}
    </dl>
  );
}

function Contacts({ candidate }: { candidate: Candidate }) {
  if (!candidate.email && !candidate.phone) return <span className="text-2sm text-muted-foreground">No contact details</span>;
  return (
    <span className="text-2sm break-all text-muted-foreground">
      {[candidate.email, candidate.phone].filter(Boolean).join(", ")}
    </span>
  );
}

/**
 * Candidates with their responsible member, resume state and application figures.
 * Searching, filtering, sorting and paging are done by the API (GET /api/candidates),
 * which also decides whose candidates the signed-in person receives.
 */
export function CandidateTable({
  listState,
  teamId,
  memberId,
  actions,
  emptyAction,
}: {
  listState: ListState<CandidateListState>;
  /** Limit the table to one team (a team page). A Team Lead is always limited to their own team by the API. */
  teamId?: string;
  /** Limit the table to the candidates of one responsible member (a member page). */
  memberId?: string;
  /** Buttons shown at the end of the filter row (Add Candidate). */
  actions?: ReactNode;
  emptyAction?: ReactNode;
}) {
  const me = useMe();
  const area = useArea();
  const isAdmin = me.role === "ADMIN";
  const [state, setState] = listState;
  const showTeam = isAdmin && !teamId && !memberId;
  const scopeTeamId = teamId ?? (isAdmin ? state.team || undefined : me.team?.id);
  const teams = useTeams(true, { enabled: showTeam });
  // The member filter lists the members of the team in view (for an Admin, of the chosen team).
  const members = useUsers(
    { role: "MEMBER", team_id: scopeTeamId, sort: "name", page: 1, page_size: 200 },
    { enabled: !memberId && Boolean(scopeTeamId), refetchInterval: false },
  );

  const unassigned = !memberId && state.member === UNASSIGNED;
  const params: CandidateListParams = {
    team_id: teamId ?? (isAdmin ? state.team || undefined : undefined),
    assigned_member_id: memberId ?? (unassigned ? undefined : state.member || undefined),
    unassigned: unassigned ? true : undefined,
    active: state.active === "all" ? undefined : state.active !== "false",
    has_resume: state.resume === "yes" ? true : state.resume === "no" ? false : undefined,
    q: state.q || undefined,
    sort: state.sort,
    page: toPositiveInt(state.page, 1),
    page_size: toPositiveInt(state.page_size, 25),
  };
  const candidates = useCandidates(params);
  const filtered = Boolean(state.q || state.team || state.member || state.resume || state.active !== "true");
  const clear = () => setState({ q: "", team: "", member: "", resume: "", active: "true" });
  const href = (id: string) => `${area}/candidates/${id}`;

  const nameCell = (candidate: Candidate) => (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="flex flex-wrap items-center gap-2">
        <Link href={href(candidate.id)} className="rounded-sm font-medium underline-offset-4 hover:underline">
          {candidate.name}
        </Link>
        <CandidateInactiveTag candidate={candidate} />
      </span>
      <Contacts candidate={candidate} />
    </div>
  );

  const columns: Column<Candidate>[] = [
    { id: "candidate", header: "Candidate", cell: nameCell },
    ...(showTeam
      ? [{ id: "team", header: "Team", cell: (candidate: Candidate) => candidate.team?.name ?? <span className="text-muted-foreground">No team</span> }]
      : []),
    ...(memberId
      ? []
      : [
          {
            id: "member",
            header: "Responsible member",
            cell: (candidate: Candidate) => (
              <ResponsibleMember
                member={candidate.assigned_member}
                href={candidate.assigned_member ? `${area}/members/${candidate.assigned_member.id}` : undefined}
              />
            ),
          },
        ]),
    {
      id: "resume",
      header: "Resume",
      cell: (candidate) => (
        <div className="flex flex-col items-start gap-1">
          <ResumeTag hasResume={candidate.has_resume} />
          <span className="text-xs text-muted-foreground">Profile {candidate.profile_completion}% complete</span>
        </div>
      ),
    },
    { id: "assigned", header: "Jobs assigned", align: "right", cell: (candidate) => candidate.stats.assigned.toLocaleString() },
    { id: "applied", header: "Applied", align: "right", cell: (candidate) => candidate.stats.applied.toLocaleString() },
    { id: "interviews", header: "Interviews", align: "right", cell: (candidate) => candidate.stats.interviews.toLocaleString() },
    { id: "newest_job", header: "Newest job", cell: (candidate) => <NewestJob value={candidate.last_assigned_at} /> },
    {
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      align: "right",
      cell: (candidate) => <CandidateActions candidate={candidate} />,
    },
  ];

  return (
    <div className="flex flex-col">
      <FilterBar canClear={filtered} onClear={clear}>
        <SearchInput
          label="Search candidates"
          placeholder="Search name, e-mail or phone"
          value={state.q}
          onChange={(q) => setState({ q })}
        />
        {showTeam && (
          <FilterSelect
            label="Team"
            allLabel="All teams"
            value={state.team}
            onChange={(team) => setState({ team, member: state.member === UNASSIGNED ? UNASSIGNED : "" })}
            options={(teams.data ?? []).map((team) => ({ value: team.id, label: team.name }))}
          />
        )}
        {!memberId && (
          <FilterSelect
            label="Responsible member"
            allLabel="Any responsible member"
            value={state.member}
            onChange={(member) => setState({ member })}
            options={[
              { value: UNASSIGNED, label: "Unassigned" },
              ...(members.data?.items ?? []).map((user) => ({ value: user.id, label: user.name })),
            ]}
          />
        )}
        <FilterSelect label="Resume" allLabel="With or without resume" value={state.resume} onChange={(resume) => setState({ resume })} options={RESUME_OPTIONS} />
        <FilterSelect label="Active or deactivated" value={state.active} onChange={(active) => setState({ active })} options={ACTIVE_OPTIONS} />
        <FilterSelect label="Sort" value={state.sort} onChange={(sort) => setState({ sort })} options={SORTS} />
        {actions && <div className="ml-auto flex flex-wrap gap-2">{actions}</div>}
      </FilterBar>
      <DataTable
        caption="Candidates"
        columns={columns}
        rows={candidates.data?.items}
        rowKey={(candidate) => candidate.id}
        loading={candidates.isPending}
        fetching={candidates.isPlaceholderData}
        error={candidates.error}
        onRetry={() => candidates.refetch()}
        rowClassName={(candidate) => (candidate.is_active ? undefined : "opacity-70")}
        empty={
          filtered ? (
            <EmptyState icon={SearchX} title="No candidates match these filters" description="Check the spelling or clear the filters." />
          ) : memberId ? (
            <EmptyState
              icon={Contact}
              title="No candidates assigned to this member"
              description="Open a candidate and choose this member as the responsible member."
            />
          ) : (
            <EmptyState
              icon={Contact}
              title="No candidates yet"
              description="Add the first candidate: the person you are finding a job for. Then upload their resume and run a search."
              action={emptyAction}
            />
          )
        }
        mobileCard={(candidate) => (
          <div className="flex flex-col gap-3">
            <div className="flex items-start justify-between gap-2">
              <div className="flex min-w-0 flex-col gap-0.5">
                {nameCell(candidate)}
                {showTeam && candidate.team && <span className="text-2sm text-muted-foreground">{candidate.team.name}</span>}
              </div>
              <CandidateActions candidate={candidate} />
            </div>
            <StatGrid stats={candidate.stats} />
            <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <ResumeTag hasResume={candidate.has_resume} />
              {!memberId && (
                <span className="text-2sm">
                  <span className="text-muted-foreground">Responsible: </span>
                  <ResponsibleMember member={candidate.assigned_member} />
                </span>
              )}
            </div>
          </div>
        )}
        pagination={
          candidates.data && {
            page: candidates.data.page,
            pageSize: candidates.data.page_size,
            total: candidates.data.total,
            noun: "candidates",
            onPageChange: (page) => setState({ page: String(page) }),
            onPageSizeChange: (size) => setState({ page_size: String(size) }),
          }
        }
      />
    </div>
  );
}

/* ------------------------------------------------------------------ analytics table (every row is loaded, so it sorts in the browser) */

/**
 * One row per candidate with their figures (the `candidates` breakdown of a member's or a
 * team's analytics). The rows add up to the totals shown above them.
 */
export function CandidatePerformanceTable({
  rows,
  loading,
  fetching,
  candidateHref,
  memberHref,
  showMember = true,
  empty,
}: {
  rows: CandidateRow[] | undefined;
  loading?: boolean;
  fetching?: boolean;
  candidateHref: (id: string) => string;
  /** Makes the responsible member a link (managers). */
  memberHref?: (id: string) => string;
  /** Leave the member column out where every row has the same member (a member's own dashboard). */
  showMember?: boolean;
  empty?: ReactNode;
}) {
  const nameCell = (row: CandidateRow) => (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="flex flex-wrap items-center gap-2">
        <Link href={candidateHref(row.candidate.id)} className="rounded-sm font-medium underline-offset-4 hover:underline">
          {row.candidate.name}
        </Link>
        {!row.is_active && <Tag>Deactivated</Tag>}
      </span>
      {!row.has_resume && <span className="text-xs text-warning">No resume uploaded</span>}
    </div>
  );
  const columns: Column<CandidateRow>[] = [
    { id: "candidate", header: "Candidate", sortValue: (row) => row.candidate.name, cell: nameCell },
    ...(showMember
      ? [
          {
            id: "member",
            header: "Responsible member",
            sortValue: (row: CandidateRow) => row.member?.name ?? "",
            cell: (row: CandidateRow) => (
              <ResponsibleMember member={row.member} href={row.member && memberHref ? memberHref(row.member.id) : undefined} />
            ),
          },
        ]
      : []),
    { id: "assigned", header: "Assigned", align: "right", sortValue: (row) => row.stats.assigned, cell: (row) => row.stats.assigned.toLocaleString() },
    { id: "not_applied", header: "Not applied", align: "right", sortValue: (row) => row.stats.not_applied, cell: (row) => row.stats.not_applied.toLocaleString() },
    { id: "applied", header: "Applied", align: "right", sortValue: (row) => row.stats.applied, cell: (row) => row.stats.applied.toLocaleString() },
    { id: "interviews", header: "Interviews", align: "right", sortValue: (row) => row.stats.interviews, cell: (row) => row.stats.interviews.toLocaleString() },
    { id: "offers", header: "Offers", align: "right", sortValue: (row) => row.stats.offers, cell: (row) => row.stats.offers.toLocaleString() },
    { id: "completion", header: "Completion rate", sortValue: (row) => row.stats.completion_rate, cell: (row) => <CompletionMeter rate={row.stats.completion_rate} /> },
  ];
  return (
    <DataTable
      caption="Figures per candidate"
      columns={columns}
      rows={rows}
      rowKey={(row) => row.candidate.id}
      loading={loading}
      fetching={fetching}
      defaultSort={{ id: "not_applied", direction: "desc" }}
      empty={
        empty ?? (
          <EmptyState compact icon={Contact} title="No candidates yet" description="Add candidates to see how the work for each of them is going." />
        )
      }
      mobileCard={(row) => (
        <div className="flex flex-col gap-3">
          {nameCell(row)}
          <StatGrid stats={row.stats} offers />
          <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
            {showMember ? (
              <span className="text-2sm">
                <span className="text-muted-foreground">Responsible: </span>
                <ResponsibleMember member={row.member} />
              </span>
            ) : (
              <span />
            )}
            <CompletionMeter rate={row.stats.completion_rate} />
          </div>
        </div>
      )}
    />
  );
}
