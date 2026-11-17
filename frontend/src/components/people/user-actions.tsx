"use client";

import { useState } from "react";
import { Ellipsis, KeyRound, MailWarning, MailCheck, Pencil, Repeat2, UserCheck, UserX } from "lucide-react";
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
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { errorMessage } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
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
          <div className="flex flex-col gap-4">
            <FormMessage tone={link.email_sent ? "success" : "info"}>
              <span className="flex items-start gap-2">
                {link.email_sent ? (
                  <MailCheck className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                ) : (
                  <MailWarning className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
                )}
                <span>
                  {link.email_sent
                    ? `The link was e-mailed to ${user.email}.`
                    : `No e-mail was sent. Copy the link and give it to ${user.name} yourself. It will not be shown again.`}
                </span>
              </span>
            </FormMessage>
            <CopyField label="Reset link" value={link.reset_url} />
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
  const valid = trimmed.length >= 2 && trimmed.length <= 160;
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
          {update.error && <FormMessage>{errorMessage(update.error)}</FormMessage>}
          <FormField label="Name" error={!valid && name !== user.name ? "Enter between 2 and 160 characters." : undefined}>
            {({ id, invalid }) => (
              <Input id={id} value={name} aria-invalid={invalid || undefined} onChange={(event) => setName(event.target.value)} autoComplete="off" />
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
 * What a manager can do to an account: rename, deactivate or reactivate, create
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
  const close = () => setAction(null);

  const setActive = (active: boolean) =>
    update.mutate(
      { id: user.id, body: { is_active: active } },
      {
        onSuccess: (saved) => {
          toast.success(active ? `${saved.name} can sign in again` : `${saved.name} was deactivated`);
          close();
        },
        onError: (error) => toast.error(errorMessage(error)),
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
        <DropdownMenuContent align="end" className="min-w-52">
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
        description="They are signed out everywhere and cannot sign in until you reactivate them. Their assignments and history stay as they are."
        confirmLabel="Deactivate account"
        destructive
        pending={update.isPending}
        onConfirm={() => setActive(false)}
      />
      <ConfirmDialog
        open={action === "activate"}
        onOpenChange={(open) => !open && close()}
        title={`Reactivate ${user.name}?`}
        description="They will be able to sign in again with their existing password."
        confirmLabel="Reactivate account"
        pending={update.isPending}
        onConfirm={() => setActive(true)}
      />
    </>
  );
}
