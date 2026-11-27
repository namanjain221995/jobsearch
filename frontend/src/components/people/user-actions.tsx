"use client";

import { useId, useState } from "react";
import { Ellipsis, KeyRound, MailWarning, MailCheck, Pencil, Repeat2, ShieldAlert, UserCheck, UserX } from "lucide-react";
import { toast } from "sonner";

import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { CopyField } from "@/components/shared/copy-field";
import { FormField, FormMessage } from "@/components/shared/form-fields";
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { ApiError, errorMessage } from "@/lib/api";
import { formatDateTime, plural } from "@/lib/format";
import { isPersonName, PERSON_NAME_RULE } from "@/lib/forms";
import { useCreateResetLink, useTeams, useUpdateUser } from "@/lib/queries";
import type { ResetLink, User, UserStatus } from "@/lib/types";

const STATUS_VIEW: Record<UserStatus, { label: string; tone: "success" | "warning" | "neutral" }> = {
  active: { label: "Active", tone: "success" },
  invited: { label: "Invited", tone: "warning" },
  deactivated: { label: "Deactivated", tone: "neutral" },
};

/** Account state of a person: invited (no password yet), active, or deactivated. */
export function UserStatusTag({ status }: { status: UserStatus | string }) {
  const view = STATUS_VIEW[status as UserStatus];
  return <Tag tone={view?.tone ?? "neutral"}>{view?.label ?? status}</Tag>;
}

type Action = "deactivate" | "activate" | "reset" | "transfer" | "rename" | null;

function ResetLinkDialog({ user, onClose }: { user: User; onClose: () => void }) {
  const create = useCreateResetLink();
  const [link, setLink] = useState<ResetLink | null>(null);
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-lg">Password reset link</DialogTitle>
          <DialogDescription>
            A one-time link that lets {user.name} choose a new password. Using it signs them out everywhere else.
          </DialogDescription>
        </DialogHeader>
        {link ? (
          // A link that went out by e-mail is not shown here as well (`reset_url` is empty then):
          // whoever holds it can set the password.
          <div className="flex flex-col gap-4">
            {link.email_sent ? (
              <FormMessage tone="success">
                <span className="flex items-start gap-2">
                  <MailCheck className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                  <span>
                    The reset link was sent by e-mail to <span className="font-medium break-all">{user.email}</span>.
                    {link.reset_url === "" && " The link is in that e-mail only and is not shown here."}
                  </span>
                </span>
              </FormMessage>
            ) : link.reset_url !== "" ? (
              <FormMessage tone="info">
                <span className="flex items-start gap-2">
                  <MailWarning className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
                  <span>
                    No e-mail was sent. Copy the link and give it to {user.name} yourself. It will not be shown again.
                  </span>
                </span>
              </FormMessage>
            ) : (
              <FormMessage>No e-mail was sent and the server returned no link. Create the reset link again.</FormMessage>
            )}
            {link.reset_url !== "" && <CopyField label="Reset link" value={link.reset_url} />}
            <p className="text-2sm text-muted-foreground">Valid until {formatDateTime(link.expires_at)}.</p>
          </div>
        ) : (
          create.error && <FormMessage>{errorMessage(create.error)}</FormMessage>
        )}
        <DialogFooter>
          {link ? (
            <Button onClick={onClose}>Done</Button>
          ) : (
            <>
              <Button variant="outline" onClick={onClose} disabled={create.isPending}>
                Cancel
              </Button>
              <Button disabled={create.isPending} onClick={() => create.mutate(user.id, { onSuccess: setLink })}>
                {create.isPending && <Spinner data-icon="inline-start" />}
                Create Reset Link
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function TransferDialog({ user, onClose }: { user: User; onClose: () => void }) {
  const teams = useTeams(false);
  const update = useUpdateUser();
  const [teamId, setTeamId] = useState("");
  const options = (teams.data ?? []).filter((team) => team.id !== user.team?.id);
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-lg">Move {user.name} to another team</DialogTitle>
          <DialogDescription>
            Their assignments move with them: the new Team Lead manages the whole pipeline and the current one no
            longer sees it. Application history is untouched.
          </DialogDescription>
        </DialogHeader>
        {update.error && <FormMessage>{errorMessage(update.error)}</FormMessage>}
        <FormField label="New team" description={user.team ? `Currently in ${user.team.name}.` : "Currently in no team."}>
          {({ id }) => (
            <Select value={teamId} onValueChange={setTeamId} disabled={teams.isPending}>
              <SelectTrigger id={id} className="w-full">
                <SelectValue placeholder={teams.isPending ? "Loading teams" : "Choose a team"} />
              </SelectTrigger>
              <SelectContent>
                {options.map((team) => (
                  <SelectItem key={team.id} value={team.id}>
                    {team.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </FormField>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={update.isPending}>
            Cancel
          </Button>
          <Button
            disabled={!teamId || update.isPending}
            onClick={() =>
              update.mutate(
                { id: user.id, body: { team_id: teamId } },
                {
                  onSuccess: (saved) => {
                    toast.success(`${saved.name} moved to ${saved.team?.name ?? "the new team"}`);
                    onClose();
                  },
                },
              )
            }
          >
            {update.isPending && <Spinner data-icon="inline-start" />}
            Move Member
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RenameDialog({ user, onClose }: { user: User; onClose: () => void }) {
  const update = useUpdateUser();
  const [name, setName] = useState(user.name);
  const trimmed = name.trim();
  const valid = isPersonName(trimmed);
  // The API's own verdict on the name (422 with fields.name) belongs on the field, not above the form.
  const refused = update.error instanceof ApiError ? update.error.fields.name : undefined;
  const failure = update.error && !refused;
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-md">
        <form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!valid) return;
            update.mutate(
              { id: user.id, body: { name: trimmed } },
              {
                onSuccess: () => {
                  toast.success("Name saved");
                  onClose();
                },
              },
            );
          }}
        >
          <DialogHeader>
            <DialogTitle className="text-lg">Rename</DialogTitle>
            <DialogDescription>The name shown across the portal for {user.email}.</DialogDescription>
          </DialogHeader>
          {failure && <FormMessage>{errorMessage(update.error)}</FormMessage>}
          <FormField label="Name" error={!valid && name !== user.name ? PERSON_NAME_RULE : refused}>
            {({ id, invalid, describedBy }) => (
              <Input
                id={id}
                value={name}
                maxLength={100}
                aria-invalid={invalid || undefined}
                aria-describedby={describedBy}
                onChange={(event) => {
                  setName(event.target.value);
                  if (update.error) update.reset();
                }}
                autoComplete="off"
              />
            )}
          </FormField>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={update.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={!valid || trimmed === user.name || update.isPending}>
              {update.isPending && <Spinner data-icon="inline-start" />}
              Save Name
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/**
 * What a manager can do to an account (an Admin, a Team Lead or a member; candidates have no account): rename, deactivate or reactivate, create
 * a password reset link, and (Admin, members only) move to another team.
 * The API decides what is actually allowed; this only offers what can work.
 */
export function UserActions({ user, variant = "menu" }: { user: User; variant?: "menu" | "buttons" }) {
  const me = useMe();
  const update = useUpdateUser();
  const [action, setAction] = useState<Action>(null);
  const isSelf = user.id === me.id;
  const canReset = user.status === "active";
  const canTransfer = me.role === "ADMIN" && user.role === "MEMBER";
  // An account an Admin deactivated can only be reactivated by an Admin (the API answers a Team Lead 403).
  const lockedByAdmin = me.role === "TEAM_LEAD" && !user.is_active && user.deactivated_by_admin;
  const lockedNoteId = useId();
  const close = () => setAction(null);

  const setActive = (active: boolean) =>
    update.mutate(
      { id: user.id, body: { is_active: active } },
      {
        onSuccess: (saved) => {
          toast.success(active ? `${saved.name} can sign in again` : `${saved.name} was deactivated`);
          close();
        },
        // For example 403: a Team Lead cannot reactivate an account an Admin deactivated. That is not offered
        // (see `lockedByAdmin`), but this screen may not have heard of the Admin's decision yet. Asking again would not help.
        onError: (error) => {
          toast.error(errorMessage(error));
          close();
        },
      },
    );

  const items = (
    <>
      <DropdownMenuItem onSelect={() => setAction("rename")}>
        <Pencil />
        Rename
      </DropdownMenuItem>
      {canReset && (
        <DropdownMenuItem onSelect={() => setAction("reset")}>
          <KeyRound />
          Create Reset Link
        </DropdownMenuItem>
      )}
      {canTransfer && (
        <DropdownMenuItem onSelect={() => setAction("transfer")}>
          <Repeat2 />
          Move to Another Team
        </DropdownMenuItem>
      )}
      {!isSelf && (
        <>
          <DropdownMenuSeparator />
          {user.is_active ? (
            <DropdownMenuItem variant="destructive" onSelect={() => setAction("deactivate")}>
              <UserX />
              Deactivate
            </DropdownMenuItem>
          ) : lockedByAdmin ? (
            // Said in place of the action, not offered and then refused. It is no menu item, so the menu
            // is described by it: a screen reader says it when the menu opens.
            <DropdownMenuLabel id={lockedNoteId} className="flex items-start gap-1.5 text-sm font-normal">
              <ShieldAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              <span className="flex flex-col gap-0.5">
                <span className="text-foreground">Deactivated by an administrator</span>
                <span className="text-xs">Only an administrator can reactivate this account.</span>
              </span>
            </DropdownMenuLabel>
          ) : (
            <DropdownMenuItem onSelect={() => setAction("activate")}>
              <UserCheck />
              Reactivate
            </DropdownMenuItem>
          )}
        </>
      )}
    </>
  );

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          {variant === "menu" ? (
            <Button variant="ghost" size="icon-sm" aria-label={`Actions for ${user.name}`}>
              <Ellipsis />
            </Button>
          ) : (
            <Button variant="outline">
              Manage Account
              <Ellipsis data-icon="inline-end" />
            </Button>
          )}
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          className={lockedByAdmin ? "min-w-64" : "min-w-52"}
          aria-describedby={lockedByAdmin ? lockedNoteId : undefined}
        >
          {items}
        </DropdownMenuContent>
      </DropdownMenu>

      {action === "reset" && <ResetLinkDialog user={user} onClose={close} />}
      {action === "transfer" && <TransferDialog user={user} onClose={close} />}
      {action === "rename" && <RenameDialog user={user} onClose={close} />}
      <ConfirmDialog
        open={action === "deactivate"}
        onOpenChange={(open) => !open && close()}
        title={`Deactivate ${user.name}?`}
        description={
          user.role === "MEMBER"
            ? `They are signed out everywhere and cannot sign in until you reactivate them. ${
                user.candidate_count > 0
                  ? `Their ${plural(user.candidate_count, "candidate")} ${user.candidate_count === 1 ? "becomes" : "become"} unassigned: the candidates keep their jobs and history, and wait for you to choose another member.`
                  : "They are responsible for no candidates at the moment."
              }`
            : user.role === "ADMIN"
              ? "They are signed out everywhere and cannot sign in until an Admin reactivates them. Invitations they sent that were not accepted yet stop working."
              : "They are signed out everywhere and cannot sign in until you reactivate them. Their team and its history stay as they are."
        }
        confirmLabel="Deactivate account"
        destructive
        pending={update.isPending}
        onConfirm={() => setActive(false)}
      />
      <ConfirmDialog
        open={action === "activate"}
        onOpenChange={(open) => !open && close()}
        title={`Reactivate ${user.name}?`}
        description={
          user.role === "MEMBER"
            ? "They will be able to sign in again with their existing password. Candidates are not given back automatically: assign them again."
            : "They will be able to sign in again with their existing password."
        }
        confirmLabel="Reactivate account"
        pending={update.isPending}
        onConfirm={() => setActive(true)}
      />
    </>
  );
}
