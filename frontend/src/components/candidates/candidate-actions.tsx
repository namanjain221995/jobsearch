"use client";

import Link from "next/link";
import { useState } from "react";
import { ArrowLeftRight, Ellipsis, Pencil, Search, UserCheck, UserRoundCheck, UserX } from "lucide-react";
import { toast } from "sonner";

import { AssignMemberDialog, EditCandidateDialog } from "@/components/candidates/candidate-dialog";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { useArea } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { errorMessage } from "@/lib/api";
import { useUpdateCandidate } from "@/lib/queries";
import type { Candidate } from "@/lib/types";

type Action = "edit" | "assign" | "deactivate" | "activate" | null;

/**
 * What a Team Lead or Admin can do with a candidate: edit the details, choose or change
 * the responsible member, deactivate or reactivate, and start a search for them.
 * The API decides what is actually allowed; a member is never shown this.
 */
export function CandidateActions({ candidate, variant = "menu" }: { candidate: Candidate; variant?: "menu" | "buttons" }) {
  const area = useArea();
  const update = useUpdateCandidate();
  const [action, setAction] = useState<Action>(null);
  const close = () => setAction(null);

  const setActive = (active: boolean) =>
    update.mutate(
      { id: candidate.id, body: { is_active: active } },
      {
        onSuccess: (saved) => {
          toast.success(active ? `${saved.name} is active again` : `${saved.name} was deactivated`);
          close();
        },
        onError: (error) => {
          toast.error(errorMessage(error));
          close();
        },
      },
    );

  const assignLabel = candidate.assigned_member ? "Move to Another Member" : "Assign to a Member";
  const AssignIcon = candidate.assigned_member ? ArrowLeftRight : UserRoundCheck;

  const items = (
    <>
      {variant === "menu" && candidate.is_active && (
        <>
          <DropdownMenuItem asChild>
            <Link href={`${area}/search?candidate=${candidate.id}`}>
              <Search />
              Find Jobs
            </Link>
          </DropdownMenuItem>
          <DropdownMenuSeparator />
        </>
      )}
      <DropdownMenuItem onSelect={() => setAction("edit")}>
        <Pencil />
        Edit Details
      </DropdownMenuItem>
      {variant === "menu" && (
        <DropdownMenuItem onSelect={() => setAction("assign")}>
          <AssignIcon />
          {assignLabel}
        </DropdownMenuItem>
      )}
      <DropdownMenuSeparator />
      {candidate.is_active ? (
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
  );

  return (
    <>
      {variant === "buttons" && (
        <Button variant="outline" onClick={() => setAction("assign")}>
          <AssignIcon data-icon="inline-start" />
          {assignLabel}
        </Button>
      )}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          {variant === "menu" ? (
            <Button variant="ghost" size="icon-sm" aria-label={`Actions for ${candidate.name}`}>
              <Ellipsis />
            </Button>
          ) : (
            <Button variant="outline">
              Manage Candidate
              <Ellipsis data-icon="inline-end" />
            </Button>
          )}
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="min-w-56">
          {items}
        </DropdownMenuContent>
      </DropdownMenu>

      {action === "edit" && <EditCandidateDialog candidate={candidate} onClose={close} />}
      {action === "assign" && <AssignMemberDialog candidate={candidate} onClose={close} />}
      <ConfirmDialog
        open={action === "deactivate"}
        onOpenChange={(open) => !open && close()}
        title={`Deactivate ${candidate.name}?`}
        description="No new search can be run and no new job can be assigned for a deactivated candidate. The jobs already assigned, the searches and the history stay as they are, and you can reactivate the candidate at any time."
        confirmLabel="Deactivate candidate"
        destructive
        pending={update.isPending}
        onConfirm={() => setActive(false)}
      />
      <ConfirmDialog
        open={action === "activate"}
        onOpenChange={(open) => !open && close()}
        title={`Reactivate ${candidate.name}?`}
        description="Searches can be run and jobs assigned for this candidate again."
        confirmLabel="Reactivate candidate"
        pending={update.isPending}
        onConfirm={() => setActive(true)}
      />
    </>
  );
}
