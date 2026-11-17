"use client";

import { useId } from "react";
import { ScrollText, SearchX } from "lucide-react";

import { DayInput } from "@/components/shared/day-input";
import { DataTable, type Column } from "@/components/shared/data-table";
import { FilterBar, FilterSelect } from "@/components/shared/filters";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/states";
import { useToday } from "@/hooks/use-now";
import { formatDateTime, humanize } from "@/lib/format";
import { useAuditLogs, useTeams, useUsers } from "@/lib/queries";
import { statusLabel } from "@/lib/status";
import type { AuditEntry, AuditListParams } from "@/lib/types";
import { isApiDay } from "@/lib/date-range";
import { toPositiveInt, useUrlState } from "@/lib/url-state";

const DEFAULTS = { action: "", actor: "", team: "", from: "", to: "", page: "1", page_size: "50" };

/** The audit trail is filtered by the start of the action code. */
const ACTION_GROUPS = [
  { value: "auth", label: "Sign-in and passwords" },
  { value: "invitation", label: "Invitations" },
  { value: "team", label: "Teams" },
  { value: "user", label: "Accounts" },
  { value: "member", label: "Member transfers" },
  { value: "candidate", label: "Candidates" },
  { value: "profile", label: "Candidate profiles" },
  { value: "resume", label: "Candidate resumes" },
  { value: "job", label: "Jobs" },
  { value: "search", label: "Searches" },
  { value: "assignment", label: "Assignments" },
  { value: "settings", label: "Settings" },
];

const ACTION_LABELS: Record<string, string> = {
  "auth.login": "Signed in",
  "auth.login_failed": "Sign-in failed",
  "auth.login_throttled": "Sign-in blocked after too many failures",
  "auth.logout": "Signed out",
  "auth.password_reset_requested": "Asked for a password reset",
  "auth.password_reset": "Reset their password",
  "auth.password_changed": "Changed their password",
  "auth.reset_link_created": "Created a password reset link",
  "invitation.created": "Created an invitation",
  "invitation.resent": "Sent a new invitation link",
  "invitation.revoked": "Revoked an invitation",
  "invitation.accepted": "Accepted an invitation",
  "team.created": "Created a team",
  "team.updated": "Changed a team",
  "team.lead_assigned": "Assigned a Team Lead",
  "team.lead_removed": "Removed a Team Lead",
  "user.renamed": "Renamed an account",
  "user.activated": "Reactivated an account",
  "user.deactivated": "Deactivated an account",
  "member.transferred": "Moved a member to another team",
  "candidate.created": "Added a candidate",
  "candidate.updated": "Edited a candidate's details",
  "candidate.assigned": "Assigned a candidate to a member",
  "candidate.unassigned": "Took a candidate from their member",
  "candidate.activated": "Reactivated a candidate",
  "candidate.deactivated": "Deactivated a candidate",
  "candidate.transferred": "Moved a candidate to another team",
  "candidate.imported_salesforce": "Brought a candidate in from Salesforce",
  "candidate.salesforce_refreshed": "Refreshed a candidate from Salesforce",
  "profile.updated": "Updated a candidate's profile",
  "resume.uploaded": "Uploaded a candidate's resume",
  "resume.deleted": "Deleted a candidate's resume",
  "resume.imported_salesforce": "Brought a candidate's resume in from Salesforce",
  "search.interrupted": "Search interrupted",
  "job.created": "Added a job",
  "job.updated": "Edited a job",
  "job.verified": "Verified a job",
  "jobs.delegated": "Sent jobs to a team queue",
  "search.created": "Started a search",
  "search.cancel_requested": "Cancelled a search",
  "search.finished": "Search finished",
  "assignments.created": "Assigned jobs to candidates",
  "assignment.status_changed": "Changed an application status",
  "assignment.withdrawn": "Withdrew an assignment",
  "settings.updated": "Changed portal settings",
  "settings.extra_answers_deleted": "Deleted answers to changed candidate profile fields",
};


function detailValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return "none";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (Array.isArray(value)) return value.join(", ") || "none";
  if (typeof value === "object") return JSON.stringify(value);
  if ((key === "old" || key === "new" || key === "status") && typeof value === "string") return statusLabel(value) || value;
  return String(value);
}

function Details({ entry }: { entry: AuditEntry }) {
  const pairs = Object.entries(entry.metadata as Record<string, unknown>);
  if (pairs.length === 0) return <span className="text-muted-foreground">No details</span>;
  return (
    <dl className="flex max-w-80 flex-wrap gap-x-3 gap-y-0.5 text-2sm">
      {pairs.map(([key, value]) => (
        <div key={key} className="flex min-w-0 gap-1">
          <dt className="text-muted-foreground">{humanize(key).toLowerCase()}:</dt>
          <dd className="break-all">{detailValue(key, value)}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Admin: the audit trail (GET /api/audit-logs), newest first. */
export function AuditLogView() {
  const fieldId = useId();
  const today = useToday();
  const [state, setState] = useUrlState(DEFAULTS);
  const teams = useTeams(true);
  const people = useUsers({ sort: "name", page: 1, page_size: 200 }, { refetchInterval: false });
  const params: AuditListParams = {
    action: state.action || undefined,
    actor_id: state.actor || undefined,
    team_id: state.team || undefined,
    date_from: isApiDay(state.from) ? state.from : undefined,
    date_to: isApiDay(state.to) ? state.to : undefined,
    page: toPositiveInt(state.page, 1),
    page_size: toPositiveInt(state.page_size, 50),
  };
  const logs = useAuditLogs(params);
  const filtered = Boolean(state.action || state.actor || state.team || state.from || state.to);

  const actionCell = (entry: AuditEntry) => (
    <div className="flex flex-col gap-0.5">
      <span>{ACTION_LABELS[entry.action] ?? humanize(entry.action)}</span>
      <code className="text-xs text-muted-foreground">{entry.action}</code>
    </div>
  );
  const whoCell = (entry: AuditEntry) =>
    entry.actor ? (
      <div className="flex flex-col gap-0.5">
        <span>{entry.actor.name}</span>
        <span className="text-2sm break-all text-muted-foreground">{entry.actor.email}</span>
      </div>
    ) : (
      <span className="text-muted-foreground">Not signed in</span>
    );

  const columns: Column<AuditEntry>[] = [
    { id: "when", header: "When", cell: (entry) => <time dateTime={entry.created_at} className="whitespace-nowrap">{formatDateTime(entry.created_at)}</time> },
    { id: "who", header: "Who", cell: whoCell },
    { id: "action", header: "What", cell: actionCell },
    { id: "team", header: "Team", cell: (entry) => entry.team?.name ?? <span className="text-muted-foreground">None</span> },
    { id: "details", header: "Details", cell: (entry) => <Details entry={entry} /> },
    { id: "ip", header: "Address", cell: (entry) => <span className="whitespace-nowrap tabular-nums">{entry.ip || "Unknown"}</span> },
  ];

  return (
    <>
      <PageHeader
        title="Activity Log"
        description="Who did what, and when: sign-ins, invitations, team changes, searches, assignments and settings."
      />
      <FilterBar canClear={filtered} onClear={() => setState({ action: "", actor: "", team: "", from: "", to: "" })}>
        <FilterSelect label="Kind of activity" allLabel="All activity" value={state.action} onChange={(action) => setState({ action })} options={ACTION_GROUPS} />
        <FilterSelect
          label="Person"
          allLabel="Everyone"
          value={state.actor}
          onChange={(actor) => setState({ actor })}
          options={(people.data?.items ?? []).map((user) => ({ value: user.id, label: user.name }))}
        />
        <FilterSelect
          label="Team"
          allLabel="All teams"
          value={state.team}
          onChange={(team) => setState({ team })}
          options={(teams.data ?? []).map((team) => ({ value: team.id, label: team.name }))}
        />
        <DayInput id={`${fieldId}-from`} label="From" value={state.from} max={state.to || today || undefined} onChange={(from) => setState({ from })} />
        <DayInput id={`${fieldId}-to`} label="To" value={state.to} min={state.from || undefined} max={today || undefined} onChange={(to) => setState({ to })} />
      </FilterBar>
      <DataTable
        caption="Activity log"
        columns={columns}
        rows={logs.data?.items}
        rowKey={(entry) => entry.id}
        loading={logs.isPending}
        fetching={logs.isPlaceholderData}
        error={logs.error}
        onRetry={() => logs.refetch()}
        empty={
          filtered ? (
            <EmptyState icon={SearchX} title="No activity matches these filters" description="Widen the date range or clear the filters." />
          ) : (
            <EmptyState icon={ScrollText} title="No activity recorded yet" description="Actions are recorded here as people use the portal." />
          )
        }
        mobileCard={(entry) => (
          <div className="flex flex-col gap-1.5">
            <div className="flex items-start justify-between gap-3">
              {actionCell(entry)}
              <time dateTime={entry.created_at} className="shrink-0 text-xs text-muted-foreground">
                {formatDateTime(entry.created_at)}
              </time>
            </div>
            <p className="text-2sm">
              {entry.actor?.name ?? "Not signed in"}
              {entry.team ? <span className="text-muted-foreground">, {entry.team.name}</span> : null}
            </p>
            <Details entry={entry} />
          </div>
        )}
        pagination={
          logs.data && {
            page: logs.data.page,
            pageSize: logs.data.page_size,
            total: logs.data.total,
            noun: "entries",
            onPageChange: (page) => setState({ page: String(page) }),
            onPageSizeChange: (size) => setState({ page_size: String(size) }),
          }
        }
      />
    </>
  );
}
