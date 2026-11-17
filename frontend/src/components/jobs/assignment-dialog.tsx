"use client";

import { useState } from "react";
import { toast } from "sonner";

import { CandidatePicker, type PickedCandidate } from "@/components/candidates/candidate-picker";
import { FormField, FormMessage } from "@/components/shared/form-fields";
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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import { errorMessage } from "@/lib/api";
import { plural } from "@/lib/format";
import { useBulkAssign, useDelegateJobs, useTeams } from "@/lib/queries";
import type { BulkAssignResult } from "@/lib/types";

export interface JobRef {
  id: string;
  title: string;
  company: string;
}

/** "3 created, 1 already assigned" in a sentence, for the toast after assigning. */
export function assignResultText(result: BulkAssignResult): string {
  const created = `${plural(result.created, "assignment")} created`;
  return result.already_assigned > 0
    ? `${created}. ${plural(result.already_assigned, "was", "were")} already assigned and left unchanged.`
    : `${created}.`;
}

function JobSummaryList({ jobs }: { jobs: JobRef[] }) {
  return (
    <ul className="max-h-32 overflow-y-auto rounded-md border bg-muted/40 px-3 py-2 text-sm">
      {jobs.map((job) => (
        <li key={job.id} className="truncate py-0.5">
          <span className="font-medium">{job.title}</span>
          <span className="text-muted-foreground"> at {job.company}</span>
        </li>
      ))}
    </ul>
  );
}

/**
 * Assign the chosen jobs to chosen candidates: every job to every candidate
 * (POST /api/assignments/bulk). The picker is step one; step two states exactly
 * what will be created and asks for confirmation.
 */
export function AssignmentDialog({
  open,
  onOpenChange,
  jobs,
  teamId,
  queueItemIds,
  onAssigned,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  jobs: JobRef[];
  /** Only offer candidates of this team (a Team Lead distributing the team queue). */
  teamId?: string;
  /** Queue entries these jobs came from; they are marked as distributed. */
  queueItemIds?: string[];
  onAssigned?: (result: BulkAssignResult) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        {open && (
          <AssignmentSteps
            jobs={jobs}
            teamId={teamId}
            queueItemIds={queueItemIds}
            onAssigned={onAssigned}
            onClose={() => onOpenChange(false)}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

function AssignmentSteps({
  jobs,
  teamId,
  queueItemIds,
  onAssigned,
  onClose,
}: {
  jobs: JobRef[];
  teamId?: string;
  queueItemIds?: string[];
  onAssigned?: (result: BulkAssignResult) => void;
  onClose: () => void;
}) {
  const me = useMe();
  const assign = useBulkAssign();
  const [selected, setSelected] = useState<ReadonlyMap<string, PickedCandidate>>(() => new Map());
  const [confirming, setConfirming] = useState(false);
  const total = jobs.length * selected.size;
  const candidates = [...selected.values()];
  const unassigned = candidates.filter((candidate) => !candidate.member);

  const submit = () =>
    assign.mutate(
      { job_ids: jobs.map((job) => job.id), candidate_ids: [...selected.keys()], queue_item_ids: queueItemIds ?? [] },
      {
        onSuccess: (result) => {
          toast.success("Jobs assigned", { description: assignResultText(result) });
          onAssigned?.(result);
          onClose();
        },
      },
    );

  if (confirming) {
    return (
      <>
        <DialogHeader>
          <DialogTitle className="text-lg">
            Assign {plural(jobs.length, "job")} to {plural(candidates.length, "candidate")}?
          </DialogTitle>
          <DialogDescription>
            This creates up to {plural(total, "assignment")}: each job for each candidate. A candidate who already has
            a job keeps it unchanged. Each candidate&apos;s responsible member is notified.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <JobSummaryList jobs={jobs} />
          <p className="text-sm">
            <span className="text-muted-foreground">To:</span>{" "}
            {candidates.map((candidate) => `${candidate.name} (${candidate.member ? candidate.member.name : "unassigned"})`).join(", ")}
          </p>
          {unassigned.length > 0 && (
            <FormMessage tone="info">
              {unassigned.map((candidate) => candidate.name).join(", ")} {unassigned.length === 1 ? "has" : "have"} no
              responsible member. The jobs are assigned to the candidate all the same and wait until you choose a
              member for them.
            </FormMessage>
          )}
          {assign.error && <FormMessage>{errorMessage(assign.error)}</FormMessage>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => setConfirming(false)} disabled={assign.isPending}>
            Back
          </Button>
          <Button onClick={submit} disabled={assign.isPending}>
            {assign.isPending && <Spinner data-icon="inline-start" />}
            Assign {plural(total, "Job")}
          </Button>
        </DialogFooter>
      </>
    );
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle className="text-lg">Assign to Candidates</DialogTitle>
        <DialogDescription>
          {jobs.length === 1 ? `${jobs[0].title} at ${jobs[0].company}` : `${jobs.length} selected jobs`}. Choose the
          candidates {jobs.length === 1 ? "it is" : "they are"} for.
        </DialogDescription>
      </DialogHeader>
      {jobs.length > 1 && <JobSummaryList jobs={jobs} />}
      <CandidatePicker selected={selected} onChange={setSelected} teamId={teamId} showTeam={me.role === "ADMIN" && !teamId} max={50} />
      <DialogFooter>
        <Button variant="outline" onClick={onClose}>
          Cancel
        </Button>
        <Button disabled={selected.size === 0} onClick={() => setConfirming(true)}>
          Review {plural(total, "Assignment")}
        </Button>
      </DialogFooter>
    </>
  );
}

/**
 * Hand jobs to a team: they land in the team's queue and its Team Lead
 * distributes them to candidates (POST /api/teams/{id}/queue). Admin only.
 */
export function DelegateDialog({
  open,
  onOpenChange,
  jobs,
  onDelegated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  jobs: JobRef[];
  onDelegated?: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        {open && <DelegateForm jobs={jobs} onDelegated={onDelegated} onClose={() => onOpenChange(false)} />}
      </DialogContent>
    </Dialog>
  );
}

function DelegateForm({ jobs, onDelegated, onClose }: { jobs: JobRef[]; onDelegated?: () => void; onClose: () => void }) {
  const teams = useTeams(false);
  const delegate = useDelegateJobs();
  const [teamId, setTeamId] = useState("");
  const [note, setNote] = useState("");
  const team = (teams.data ?? []).find((item) => item.id === teamId);

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (!teamId) return;
        delegate.mutate(
          { teamId, body: { job_ids: jobs.map((job) => job.id), note: note.trim() } },
          {
            onSuccess: () => {
              toast.success(`${plural(jobs.length, "job")} sent to ${team?.name ?? "the team"}'s queue`, {
                description: team?.team_lead ? `${team.team_lead.name} has been notified.` : "This team has no Team Lead yet, so nobody was notified.",
              });
              onDelegated?.();
              onClose();
            },
          },
        );
      }}
    >
      <DialogHeader>
        <DialogTitle className="text-lg">Assign to Team Lead</DialogTitle>
        <DialogDescription>
          The jobs go into the team&apos;s queue. Its Team Lead decides which candidates get them.
        </DialogDescription>
      </DialogHeader>
      <JobSummaryList jobs={jobs} />
      {delegate.error && <FormMessage>{errorMessage(delegate.error)}</FormMessage>}
      <FormField
        label="Team"
        description={
          team
            ? team.team_lead
              ? `Led by ${team.team_lead.name}. ${plural(team.queue_pending, "job")} already waiting in the queue.`
              : "This team has no Team Lead yet. The jobs wait in the queue until one is assigned."
            : undefined
        }
      >
        {({ id, describedBy }) => (
          <Select value={teamId} onValueChange={setTeamId} disabled={teams.isPending}>
            <SelectTrigger id={id} aria-describedby={describedBy} className="w-full">
              <SelectValue placeholder={teams.isPending ? "Loading teams" : "Choose a team"} />
            </SelectTrigger>
            <SelectContent>
              {(teams.data ?? []).map((item) => (
                <SelectItem key={item.id} value={item.id}>
                  {item.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </FormField>
      <FormField label="Note for the Team Lead" optional>
        {({ id }) => (
          <Textarea id={id} rows={2} maxLength={500} value={note} onChange={(event) => setNote(event.target.value)} placeholder="Where these came from, or what to do with them" />
        )}
      </FormField>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose} disabled={delegate.isPending}>
          Cancel
        </Button>
        <Button type="submit" disabled={!teamId || delegate.isPending}>
          {delegate.isPending && <Spinner data-icon="inline-start" />}
          Send to Team Queue
        </Button>
      </DialogFooter>
    </form>
  );
}
