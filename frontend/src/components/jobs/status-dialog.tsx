"use client";

import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { toast } from "sonner";
import { z } from "@/lib/zod";

import { FormMessage, TextareaField, TextField } from "@/components/shared/form-fields";
import { ApplicationStatusBadge } from "@/components/shared/status-badge";
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
import { errorMessage } from "@/lib/api";
import { formatDateTime, toDateTimeInput } from "@/lib/format";
import { applyServerErrors, isGeneralError } from "@/lib/forms";
import { useUpdateStatus } from "@/lib/queries";
import { PRE_APPLICATION_STATUSES, STATUS_META } from "@/lib/status";
import type { AssignmentDetail, AssignmentStatus, StatusUpdate } from "@/lib/types";

/** The API allows a few minutes of clock difference on both ends; so does this form. */
const CLOCK_TOLERANCE_MS = 4 * 60_000;

/**
 * The application date is optional. When given it lies between the moment the
 * job was assigned and now: nobody applied to a job before it was assigned to
 * them. The API checks the same and answers 422 otherwise.
 */
const makeSchema = (assignedAt: string) =>
  z.object({
    applied_at: z
      .string()
      .refine((value) => !value || !Number.isNaN(new Date(value).getTime()), "Enter a date and a time.")
      .refine((value) => !value || new Date(value).getTime() <= Date.now() + 60_000, "The application date cannot be in the future.")
      .refine(
        (value) => !value || new Date(value).getTime() >= new Date(assignedAt).getTime() - CLOCK_TOLERANCE_MS,
        `The application date cannot be earlier than ${formatDateTime(assignedAt)}, when this job was assigned.`,
      ),
    reference: z.string().max(200, "Use at most 200 characters."),
    note: z.string().max(4000, "Use at most 4,000 characters."),
  });
type Values = z.infer<ReturnType<typeof makeSchema>>;

const SUBMITTED: AssignmentStatus[] = ["APPLIED", "INTERVIEWING", "OFFER", "REJECTED"];

/** What changing to `target` does to the record, in plain words. The API applies exactly these rules. */
function consequence(assignment: AssignmentDetail, target: AssignmentStatus): string | null {
  const wasApplied = Boolean(assignment.applied_at);
  if ((PRE_APPLICATION_STATUSES.includes(target) || target === "SKIPPED") && wasApplied) {
    return "The applied date and any interview or offer dates will be cleared. The history keeps the change as a correction.";
  }
  if (target === "APPLIED" && (assignment.status === "INTERVIEWING" || assignment.status === "OFFER")) {
    return "The interview and offer dates will be cleared. The history keeps the change as a correction.";
  }
  if (target === "INTERVIEWING" && assignment.status === "OFFER") {
    return "The offer date will be cleared. The history keeps the change as a correction.";
  }
  if (target !== "APPLIED" && SUBMITTED.includes(target) && !wasApplied) {
    return "No application has been recorded for this job yet. Unless you enter a date below, the applied date is set to now.";
  }
  if (target === "REJECTED" || target === "WITHDRAWN" || target === "SKIPPED") {
    return "This closes the assignment. You can reopen it later by changing the status again.";
  }
  return null;
}

/**
 * Confirms a status change before it is saved. "Mark as Applied" is this dialog
 * with APPLIED as the target: it asks for the reference number and the real
 * application date, both optional.
 */
export function StatusDialog({
  assignment,
  target,
  onOpenChange,
  onBehalfOf,
}: {
  assignment: AssignmentDetail;
  /** The status to change to. The dialog is open while this is set. */
  target: AssignmentStatus | null;
  onOpenChange: (open: boolean) => void;
  /** Set when a manager records the change for a member. */
  onBehalfOf?: string;
}) {
  return (
    <Dialog open={target !== null} onOpenChange={onOpenChange}>
      {target && (
        <StatusDialogBody
          key={target}
          assignment={assignment}
          target={target}
          onBehalfOf={onBehalfOf}
          onDone={() => onOpenChange(false)}
        />
      )}
    </Dialog>
  );
}

function StatusDialogBody({
  assignment,
  target,
  onBehalfOf,
  onDone,
}: {
  assignment: AssignmentDetail;
  target: AssignmentStatus;
  onBehalfOf?: string;
  onDone: () => void;
}) {
  const update = useUpdateStatus(assignment.id);
  const form = useForm<Values>({
    resolver: zodResolver(makeSchema(assignment.assigned_at)),
    defaultValues: { applied_at: "", reference: "", note: "" },
  });
  const { errors } = form.formState;

  const marking = target === "APPLIED" && !assignment.applied_at;
  const asksForSubmission = SUBMITTED.includes(target) && (target === "APPLIED" || !assignment.applied_at);
  const note = consequence(assignment, target);
  const title = marking ? "Mark as Applied" : `Change status to ${STATUS_META[target].label}`;
  const confirm = marking ? "Mark as Applied" : `Change to ${STATUS_META[target].label}`;

  const onSubmit = form.handleSubmit((values) => {
    const body: StatusUpdate = { status: target };
    // The box holds local time without a zone; the API wants the zone, so the instant is sent in UTC ("...Z").
    if (asksForSubmission && values.applied_at) body.applied_at = new Date(values.applied_at).toISOString();
    if (asksForSubmission && values.reference.trim()) body.reference = values.reference.trim();
    if (values.note.trim()) body.note = values.note.trim();
    update.mutate(body, {
      onSuccess: () => {
        toast.success(marking ? "Marked as applied" : `Status changed to ${STATUS_META[target].label}`);
        onDone();
      },
      onError: (error) => {
        applyServerErrors(form, error);
      },
    });
  });

  const failure = isGeneralError(form, update.error);

  return (
    <DialogContent className="sm:max-w-md">
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
        <DialogHeader>
          <DialogTitle className="text-lg">{title}</DialogTitle>
          <DialogDescription>
            {marking
              ? "Confirm only after you have submitted the application on the employer's site. Opening the job posting is not counted as applying."
              : `${assignment.job.title} at ${assignment.job.company}`}
          </DialogDescription>
        </DialogHeader>

        {!marking && (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <ApplicationStatusBadge status={assignment.status} />
            <span className="text-muted-foreground">becomes</span>
            <ApplicationStatusBadge status={target} />
          </div>
        )}
        {marking && (
          <p className="text-sm">
            <span className="font-medium">{assignment.job.title}</span>
            <span className="text-muted-foreground"> at {assignment.job.company}</span>
          </p>
        )}
        {note && <FormMessage tone="info">{note}</FormMessage>}
        {onBehalfOf && (
          <p className="text-2sm text-muted-foreground">
            You are recording this for {onBehalfOf}. The history will show that you made the change.
          </p>
        )}
        {failure && <FormMessage>{errorMessage(update.error)}</FormMessage>}

        {asksForSubmission && (
          <>
            <TextField
              label="Reference or confirmation number"
              optional
              autoComplete="off"
              error={errors.reference?.message}
              {...form.register("reference")}
            />
            <TextField
              label="When was the application submitted?"
              optional
              type="datetime-local"
              min={toDateTimeInput(new Date(assignment.assigned_at))}
              max={toDateTimeInput(new Date())}
              description={`Leave empty to use the current time. Not before ${formatDateTime(assignment.assigned_at)}, when this job was assigned.`}
              error={errors.applied_at?.message}
              {...form.register("applied_at")}
            />
          </>
        )}
        <TextareaField
          label="Note"
          optional
          rows={3}
          description="Saved with the assignment and visible to the member and their managers."
          error={errors.note?.message}
          {...form.register("note")}
        />

        <DialogFooter>
          <Button type="button" variant="outline" onClick={onDone} disabled={update.isPending}>
            Cancel
          </Button>
          <Button type="submit" disabled={update.isPending}>
            {update.isPending && <Spinner data-icon="inline-start" />}
            {confirm}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  );
}
