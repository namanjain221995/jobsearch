"use client";

import { useState } from "react";
import { Ban, MailPlus, RefreshCw, SearchX } from "lucide-react";
import { toast } from "sonner";

import { InvitationDialog, InvitationResult, type InvitationPreset } from "@/components/people/invitation-dialog";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { DataTable, type Column } from "@/components/shared/data-table";
import { FilterBar, FilterSelect } from "@/components/shared/filters";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { errorMessage } from "@/lib/api";
import { formatDateTime, roleLabel } from "@/lib/format";
import { useInvitations, useResendInvitation, useRevokeInvitation, useTeams } from "@/lib/queries";
import type { Invitation, InvitationCreated, InvitationListParams, InvitationStatus } from "@/lib/types";
import { toPositiveInt, useUrlState } from "@/lib/url-state";

const DEFAULTS = { status: "", team: "", page: "1", page_size: "25" };

const STATUS_VIEW: Record<InvitationStatus, { label: string; tone: "warning" | "success" | "neutral" | "danger" }> = {
  pending: { label: "Pending", tone: "warning" },
  accepted: { label: "Accepted", tone: "success" },
  expired: { label: "Expired", tone: "neutral" },
  revoked: { label: "Revoked", tone: "danger" },
};

export function InvitationStatusTag({ status }: { status: InvitationStatus }) {
  const view = STATUS_VIEW[status];
  return <Tag tone={view.tone}>{view.label}</Tag>;
}

function RowActions({
  invitation,
  onResent,
}: {
  invitation: Invitation;
  onResent: (created: InvitationCreated) => void;
}) {
  const resend = useResendInvitation();
  const revoke = useRevokeInvitation();
  const [confirming, setConfirming] = useState(false);
  // A new link can be issued for anything not yet accepted; only a pending link can be revoked.
  if (invitation.status === "accepted") return <span className="text-xs text-muted-foreground">No actions</span>;
  return (
    <div className="flex flex-wrap justify-end gap-1.5">
      <Button
        variant="outline"
        size="sm"
        disabled={resend.isPending}
        onClick={() =>
          resend.mutate(invitation.id, {
            onSuccess: onResent,
            onError: (error) => toast.error(errorMessage(error)),
          })
        }
      >
        <RefreshCw data-icon="inline-start" className={resend.isPending ? "animate-spin" : undefined} />
        Resend
      </Button>
      {invitation.status === "pending" && (
        <Button variant="destructive" size="sm" onClick={() => setConfirming(true)}>
          <Ban data-icon="inline-start" />
          Revoke
        </Button>
      )}
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Revoke the invitation for ${invitation.name}?`}
        description="The link stops working at once. You can send a new invitation later with Resend."
        confirmLabel="Revoke invitation"
        destructive
        pending={revoke.isPending}
        onConfirm={() =>
          revoke.mutate(invitation.id, {
            onSuccess: () => {
              toast.success("Invitation revoked");
              setConfirming(false);
            },
            onError: (error) => toast.error(errorMessage(error)),
          })
        }
      />
    </div>
  );
}

/**
 * Invitations sent by this person's scope (an Admin sees all, a Team Lead those
 * for their team), with their state and the two things that can be done to one:
 * send a fresh link, or revoke it.
 */
export function InvitationsView({ preset }: { preset?: InvitationPreset }) {
  const me = useMe();
  const isAdmin = me.role === "ADMIN";
  const [state, setState] = useUrlState(DEFAULTS);
  const [inviting, setInviting] = useState<InvitationPreset | null>(null);
  const [resent, setResent] = useState<InvitationCreated | null>(null);
  const teams = useTeams(true, { enabled: isAdmin });
  const params: InvitationListParams = {
    status: state.status || undefined,
    team_id: isAdmin ? state.team || undefined : undefined,
    page: toPositiveInt(state.page, 1),
    page_size: toPositiveInt(state.page_size, 25),
  };
  const list = useInvitations(params);
  const filtered = Boolean(state.status || state.team);

  const columns: Column<Invitation>[] = [
    {
      id: "invitee",
      header: "Invited",
      cell: (row) => (
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="font-medium">{row.name}</span>
          <span className="text-2sm break-all text-muted-foreground">{row.email}</span>
        </div>
      ),
    },
    {
      id: "role",
      header: "Joins as",
      cell: (row) => (
        <div className="flex flex-col gap-0.5">
          <span>{roleLabel(row.role)}</span>
          <span className="text-2sm text-muted-foreground">{row.team?.name ?? "No team yet"}</span>
        </div>
      ),
    },
    { id: "status", header: "Status", cell: (row) => <InvitationStatusTag status={row.status} /> },
    {
      id: "delivery",
      header: "Delivery",
      cell: (row) => (row.delivery === "email" ? "E-mail sent" : <span title="No e-mail was sent for this invitation">Link shared by hand</span>),
    },
    {
      id: "dates",
      header: "Sent",
      cell: (row) => (
        <div className="flex flex-col gap-0.5 whitespace-nowrap">
          <span>{formatDateTime(row.created_at)}</span>
          <span className="text-2sm text-muted-foreground">
            {row.status === "accepted" && row.accepted_at
              ? `Accepted ${formatDateTime(row.accepted_at)}`
              : row.status === "pending"
                ? `Valid until ${formatDateTime(row.expires_at)}`
                : row.status === "expired"
                  ? `Expired ${formatDateTime(row.expires_at)}`
                  : "Link no longer valid"}
          </span>
        </div>
      ),
    },
    ...(isAdmin ? [{ id: "by", header: "Sent by", cell: (row: Invitation) => row.invited_by?.name ?? "Unknown" }] : []),
    {
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      align: "right",
      cell: (row) => <RowActions invitation={row} onResent={setResent} />,
    },
  ];

  return (
    <>
      <PageHeader
        title="Invitations"
        description="Every account starts with an invitation. Send one, see whether it was accepted, and resend or revoke it."
        actions={
          <>
            {isAdmin && (
              <Button variant="outline" onClick={() => setInviting({ role: "TEAM_LEAD" })}>
                <MailPlus data-icon="inline-start" />
                Create Team Lead
              </Button>
            )}
            <Button onClick={() => setInviting(preset ?? { role: "MEMBER" })}>
              <MailPlus data-icon="inline-start" />
              Add Member
            </Button>
          </>
        }
      />
      <FilterBar canClear={filtered} onClear={() => setState({ status: "", team: "" })}>
        <FilterSelect
          label="Status"
          allLabel="All statuses"
          value={state.status}
          onChange={(status) => setState({ status })}
          options={(Object.keys(STATUS_VIEW) as InvitationStatus[]).map((status) => ({ value: status, label: STATUS_VIEW[status].label }))}
        />
        {isAdmin && (
          <FilterSelect
            label="Team"
            allLabel="All teams"
            value={state.team}
            onChange={(team) => setState({ team })}
            options={(teams.data ?? []).map((team) => ({ value: team.id, label: team.name }))}
          />
        )}
      </FilterBar>
      <DataTable
        caption="Invitations"
        columns={columns}
        rows={list.data?.items}
        rowKey={(row) => row.id}
        loading={list.isPending}
        fetching={list.isPlaceholderData}
        error={list.error}
        onRetry={() => list.refetch()}
        empty={
          filtered ? (
            <EmptyState icon={SearchX} title="No invitations match these filters" description="Choose another status or clear the filters." />
          ) : (
            <EmptyState
              icon={MailPlus}
              title="No invitations yet"
              description="Invite a member to give them an account."
              action={
                <Button size="sm" onClick={() => setInviting(preset ?? { role: "MEMBER" })}>
                  Add Member
                </Button>
              }
            />
          )
        }
        mobileCard={(row) => (
          <div className="flex flex-col gap-2.5">
            <div className="flex items-start justify-between gap-2">
              <div className="flex min-w-0 flex-col gap-0.5">
                <span className="font-medium">{row.name}</span>
                <span className="text-2sm break-all text-muted-foreground">{row.email}</span>
              </div>
              <InvitationStatusTag status={row.status} />
            </div>
            <p className="text-2sm text-muted-foreground">
              {roleLabel(row.role)}, {row.team?.name ?? "no team yet"}. Sent {formatDateTime(row.created_at)}
              {row.delivery === "email" ? " by e-mail." : ", link shared by hand."}
            </p>
            <RowActions invitation={row} onResent={setResent} />
          </div>
        )}
        pagination={
          list.data && {
            page: list.data.page,
            pageSize: list.data.page_size,
            total: list.data.total,
            noun: "invitations",
            onPageChange: (page) => setState({ page: String(page) }),
            onPageSizeChange: (size) => setState({ page_size: String(size) }),
          }
        }
      />

      <InvitationDialog open={inviting !== null} onOpenChange={(open) => !open && setInviting(null)} preset={inviting ?? {}} />
      <Dialog open={resent !== null} onOpenChange={(open) => !open && setResent(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle className="text-lg">New invitation link</DialogTitle>
            <DialogDescription>Earlier links for this person no longer work.</DialogDescription>
          </DialogHeader>
          {resent && <InvitationResult invitation={resent} />}
          <DialogFooter>
            <Button onClick={() => setResent(null)}>Done</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
