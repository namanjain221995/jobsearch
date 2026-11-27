"use client";

import { useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { ShieldPlus, UserRoundCheck } from "lucide-react";
import { z } from "@/lib/zod";

import { AdminAccessWarning, InvitationResult } from "@/components/people/invitation-dialog";
import { UserActions, UserStatusTag } from "@/components/people/user-actions";
import { DataTable, type Column } from "@/components/shared/data-table";
import { FormMessage, TextField } from "@/components/shared/form-fields";
import { Panel } from "@/components/shared/page-header";
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
import { Spinner } from "@/components/ui/spinner";
import { InvitationRowActions, InvitationStatusTag } from "@/components/views/invitations";
import { errorMessage } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { applyServerErrors, isGeneralError, personNameSchema } from "@/lib/forms";
import { useCreateInvitation, useInvitations, useUsers } from "@/lib/queries";
import type { Invitation, InvitationCreated, User } from "@/lib/types";

const schema = z.object({ name: personNameSchema, email: z.email("Enter a valid e-mail address.") });
type Values = z.infer<typeof schema>;

/** Name and e-mail of the person to invite as an Admin, under a plain statement of what an Admin can do. */
function InviteAdminForm() {
  const create = useCreateInvitation();
  const [created, setCreated] = useState<InvitationCreated | null>(null);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { name: "", email: "" } });
  const { errors } = form.formState;

  const onSubmit = form.handleSubmit((values) =>
    // The role is all this form sends besides the person: an Admin has no team. The API fixes the role on
    // the invitation; nothing in the link or in the page that accepts it can change it.
    create.mutate(
      { name: values.name, email: values.email, role: "ADMIN", team_description: "" },
      {
        onSuccess: (invitation) => {
          setCreated(invitation);
          form.reset();
        },
        onError: (error) => {
          applyServerErrors(form, error);
        },
      },
    ),
  );
  const failure = isGeneralError(form, create.error);

  if (created) {
    return (
      <div className="flex flex-col gap-4">
        <InvitationResult invitation={created} />
        <Button variant="outline" className="self-start" onClick={() => setCreated(null)}>
          Invite Another Admin
        </Button>
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
      <AdminAccessWarning />
      {failure && <FormMessage>{errorMessage(create.error)}</FormMessage>}
      <div className="grid gap-4 sm:grid-cols-2">
        <TextField label="Full name" autoComplete="off" error={errors.name?.message} {...form.register("name")} />
        <TextField
          label="E-mail"
          type="email"
          inputMode="email"
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          error={errors.email?.message}
          {...form.register("email")}
        />
      </div>
      <p className="text-2sm text-muted-foreground">
        They get a link to choose their password. The account does not work until they do, and the link stops working if you revoke it,
        when it expires, or once it was used.
      </p>
      <Button type="submit" disabled={create.isPending} className="self-start">
        {create.isPending ? <Spinner data-icon="inline-start" /> : <ShieldPlus data-icon="inline-start" />}
        Invite Admin
      </Button>
    </form>
  );
}

/**
 * The invitations of Admins that have not led to an account: for each invited address the newest
 * invitation, unless it was accepted. (Every Resend makes a new invitation and ends the one before it.)
 */
function openInvitations(invitations: Invitation[]): Invitation[] {
  const newest = new Map<string, Invitation>();
  for (const invitation of invitations) {
    const seen = newest.get(invitation.email);
    if (!seen || invitation.created_at > seen.created_at) newest.set(invitation.email, invitation);
  }
  return [...newest.values()].filter((invitation) => invitation.status !== "accepted");
}

function validity(invitation: Invitation): string {
  if (invitation.status === "pending") return `Valid until ${formatDateTime(invitation.expires_at)}`;
  if (invitation.status === "expired") return `Expired ${formatDateTime(invitation.expires_at)}`;
  return "Link no longer valid";
}

/**
 * Admin Settings, "Admins": who has an Admin account, which Admin invitations are still open, and the
 * form that invites a further Admin. Only an Admin reaches Settings, and the API gives the two lists
 * to an Admin only.
 */
export function AdminAccountsPanels() {
  const me = useMe();
  const [resent, setResent] = useState<InvitationCreated | null>(null);
  const users = useUsers({ role: "ADMIN", sort: "name", page: 1, page_size: 100 });
  const invitations = useInvitations({ role: "ADMIN", page: 1, page_size: 100 });
  // An invited Admin has a row among the users too, but no account yet: those are listed with the invitations.
  const accounts = useMemo(() => users.data?.items.filter((user) => user.status !== "invited"), [users.data]);
  const open = useMemo(() => (invitations.data ? openInvitations(invitations.data.items) : undefined), [invitations.data]);

  const accountColumns: Column<User>[] = [
    {
      id: "name",
      header: "Name",
      cell: (row) => (
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{row.name}</span>
          {row.id === me.id && <Tag tone="primary">You</Tag>}
        </span>
      ),
    },
    { id: "email", header: "E-mail", cell: (row) => <span className="break-all">{row.email}</span> },
    { id: "status", header: "Status", cell: (row) => <UserStatusTag status={row.status} /> },
    {
      id: "last",
      header: "Last sign-in",
      cell: (row) => (row.last_login_at ? formatDateTime(row.last_login_at) : <span className="text-muted-foreground">Never signed in</span>),
    },
    {
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      align: "right",
      cell: (row) => <UserActions user={row} />,
    },
  ];

  const invitationColumns: Column<Invitation>[] = [
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
    { id: "status", header: "Status", cell: (row) => <InvitationStatusTag status={row.status} /> },
    {
      id: "sent",
      header: "Sent",
      cell: (row) => (
        <div className="flex flex-col gap-0.5">
          <span>
            {formatDateTime(row.created_at)}
            {row.invited_by ? ` by ${row.invited_by.name}` : ""}
          </span>
          <span className="text-2sm text-muted-foreground">
            {validity(row)}. {row.delivery === "email" ? "E-mail sent." : "Link shared by hand."}
          </span>
        </div>
      ),
    },
    {
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      align: "right",
      cell: (row) => <InvitationRowActions invitation={row} onResent={setResent} />,
    },
  ];

  return (
    <div className="flex flex-col gap-5">
      <Panel
        title="Admin accounts"
        description="Everyone who can sign in as an Admin, or could before the account was deactivated. You cannot deactivate your own account, and the last Admin who can sign in cannot be deactivated."
        flush
      >
        <DataTable
          caption="Admin accounts"
          columns={accountColumns}
          rows={accounts}
          rowKey={(row) => row.id}
          loading={users.isPending}
          fetching={users.isPlaceholderData}
          error={users.error}
          onRetry={() => users.refetch()}
          className="border-0"
          empty={<EmptyState compact icon={UserRoundCheck} title="No Admin accounts" description="There is no Admin account to show." />}
          mobileCard={(row) => (
            <div className="flex flex-col gap-2">
              <div className="flex items-start justify-between gap-2">
                <div className="flex min-w-0 flex-col gap-0.5">
                  <span className="flex flex-wrap items-center gap-2 font-medium">
                    {row.name}
                    {row.id === me.id && <Tag tone="primary">You</Tag>}
                  </span>
                  <span className="text-2sm break-all text-muted-foreground">{row.email}</span>
                </div>
                <UserActions user={row} />
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <UserStatusTag status={row.status} />
                <span className="text-xs text-muted-foreground">
                  {row.last_login_at ? `Last sign-in ${formatDateTime(row.last_login_at)}` : "Never signed in"}
                </span>
              </div>
            </div>
          )}
        />
      </Panel>

      <Panel
        title="Pending Admin invitations"
        description="Invitations that have not led to an account. Resend makes a new link and ends the old one; Revoke ends the link at once."
        flush
      >
        <DataTable
          caption="Pending Admin invitations"
          columns={invitationColumns}
          rows={open}
          rowKey={(row) => row.id}
          loading={invitations.isPending}
          fetching={invitations.isPlaceholderData}
          error={invitations.error}
          onRetry={() => invitations.refetch()}
          className="border-0"
          empty={
            <EmptyState
              compact
              icon={ShieldPlus}
              title="No Admin invitation is open"
              description="When you invite an Admin, the invitation is listed here until they have chosen a password."
            />
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
                Sent {formatDateTime(row.created_at)}
                {row.invited_by ? ` by ${row.invited_by.name}` : ""}. {validity(row)}.
              </p>
              <InvitationRowActions invitation={row} onResent={setResent} />
            </div>
          )}
        />
      </Panel>

      <Panel title="Invite Admin" description="Give another person an Admin account. Accounts only ever start with an invitation.">
        <InviteAdminForm />
      </Panel>

      <Dialog open={resent !== null} onOpenChange={(next) => !next && setResent(null)}>
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
    </div>
  );
}
