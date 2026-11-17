"use client";

import { useId, useRef, useState } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { FileText, Upload, X } from "lucide-react";
import { toast } from "sonner";
import { z } from "@/lib/zod";

import { MemberSelect } from "@/components/candidates/member-select";
import { ResumeAiNotice } from "@/components/candidates/resume-upload";
import { FormField, FormMessage, OptionalMark, TextareaField, TextField } from "@/components/shared/form-fields";
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
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { errorMessage } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import { applyServerErrors, isGeneralError, personNameSchema } from "@/lib/forms";
import { useCreateCandidate, useTeams, useUpdateCandidate, useUploadCandidateResume } from "@/lib/queries";
import { RESUME_ACCEPT, RESUME_FORMATS, resumeFileProblem } from "@/lib/resume-file";
import type { Candidate, CandidateUpdate } from "@/lib/types";

/** The same rules the API applies (CandidateCreate): an optional address, a phone number of plain characters, free notes. */
const PHONE = /^[0-9+()\-./# xXeEtT]{0,40}$/;
const detailsShape = {
  name: personNameSchema,
  email: z
    .string()
    .trim()
    .max(254, "Use at most 254 characters.")
    .refine((value) => value === "" || z.email().safeParse(value).success, "Enter a valid e-mail address, or leave it empty."),
  phone: z
    .string()
    .trim()
    .refine((value) => PHONE.test(value), "Use at most 40 characters: digits, spaces and + ( ) - . / # x only."),
  notes: z.string().max(10000, "Use at most 10,000 characters."),
};

/* ------------------------------------------------------------------ add */

const createSchema = (needsTeam: boolean) =>
  z
    .object({ ...detailsShape, team_id: z.string(), assigned_member_id: z.string() })
    .superRefine((values, context) => {
      if (needsTeam && !values.team_id) {
        context.addIssue({ code: "custom", path: ["team_id"], message: "Choose the team this candidate belongs to." });
      }
    });
type CreateValues = z.infer<ReturnType<typeof createSchema>>;

function AddCandidateForm({
  teamId,
  onCreated,
  onCancel,
}: {
  /** Start with this team chosen (an Admin looking at one team). A Team Lead always adds to their own team. */
  teamId?: string;
  onCreated: (candidate: Candidate) => void;
  onCancel: () => void;
}) {
  const me = useMe();
  const isAdmin = me.role === "ADMIN";
  const fileInputId = useId();
  const fileInput = useRef<HTMLInputElement>(null);
  const teams = useTeams(false, { enabled: isAdmin });
  const create = useCreateCandidate();
  const upload = useUploadCandidateResume();
  const [file, setFile] = useState<File | null>(null);
  const [fileProblem, setFileProblem] = useState<string | null>(null);

  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema(isAdmin)),
    defaultValues: {
      name: "",
      email: "",
      phone: "",
      notes: "",
      team_id: isAdmin ? (teamId ?? "") : (me.team?.id ?? ""),
      assigned_member_id: "",
    },
  });
  const { errors } = form.formState;
  const chosenTeam = useWatch({ control: form.control, name: "team_id" });
  const pending = create.isPending || upload.isPending;

  const chooseFile = (next: File | undefined) => {
    if (!next) return;
    const problem = resumeFileProblem(next);
    setFileProblem(problem);
    setFile(problem ? null : next);
  };

  const onSubmit = form.handleSubmit((values) => {
    create.mutate(
      {
        name: values.name,
        email: values.email || null,
        phone: values.phone,
        notes: values.notes.trim(),
        team_id: isAdmin ? values.team_id : null,
        assigned_member_id: values.assigned_member_id || null,
      },
      {
        onSuccess: (candidate) => {
          if (!file) {
            toast.success(`${candidate.name} added`);
            onCreated(candidate);
            return;
          }
          // The candidate exists from here on. A resume that fails to upload does not undo that:
          // say so, and the resume can be uploaded again from the candidate's page.
          upload.mutate(
            { id: candidate.id, file },
            {
              onSuccess: (resume) => {
                toast.success(`${candidate.name} added`, { description: `${resume.filename} uploaded.` });
                onCreated({ ...candidate, has_resume: true, resume_has_file: resume.has_file });
              },
              onError: (error) => {
                toast.warning(`${candidate.name} was added, but the resume was not uploaded`, {
                  description: `${errorMessage(error)} Upload it again from the candidate's page.`,
                });
                onCreated(candidate);
              },
            },
          );
        },
        onError: (error) => {
          applyServerErrors(form, error);
        },
      },
    );
  });
  const failure = isGeneralError(form, create.error);

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
      {failure && <FormMessage>{errorMessage(create.error)}</FormMessage>}
      <TextField label="Full name" autoComplete="off" error={errors.name?.message} {...form.register("name")} />
      <div className="grid gap-4 sm:grid-cols-2">
        <TextField
          label="E-mail"
          optional
          type="email"
          autoComplete="off"
          error={errors.email?.message}
          {...form.register("email")}
        />
        <TextField label="Phone" optional type="tel" autoComplete="off" error={errors.phone?.message} {...form.register("phone")} />
      </div>
      <TextareaField
        label="Notes"
        optional
        rows={3}
        description="Anything the team should know: availability, preferences, how to reach them. The responsible member sees this."
        error={errors.notes?.message}
        {...form.register("notes")}
      />

      {isAdmin && (
        <Controller
          control={form.control}
          name="team_id"
          render={({ field }) => (
            <FormField
              label="Team"
              error={errors.team_id?.message}
              description={teams.isSuccess && teams.data.length === 0 ? "There are no active teams yet. Create a team first." : undefined}
            >
              {({ id, invalid, describedBy }) => (
                <Select
                  value={field.value}
                  onValueChange={(value) => {
                    field.onChange(value);
                    // A member belongs to one team: the choice made for another team does not carry over.
                    form.setValue("assigned_member_id", "");
                  }}
                  disabled={teams.isPending}
                >
                  <SelectTrigger id={id} aria-invalid={invalid || undefined} aria-describedby={describedBy} className="w-full">
                    <SelectValue placeholder={teams.isPending ? "Loading teams" : "Choose a team"} />
                  </SelectTrigger>
                  <SelectContent>
                    {(teams.data ?? []).map((team) => (
                      <SelectItem key={team.id} value={team.id}>
                        {team.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </FormField>
          )}
        />
      )}

      <Controller
        control={form.control}
        name="assigned_member_id"
        render={({ field }) => (
          <FormField
            label="Responsible member"
            optional
            error={errors.assigned_member_id?.message}
            description="The member who applies to jobs on this candidate's behalf. You can choose or change this later."
          >
            {({ id, invalid, describedBy }) => (
              <MemberSelect
                id={id}
                invalid={invalid}
                describedBy={describedBy}
                teamId={chosenTeam || undefined}
                value={field.value}
                onChange={field.onChange}
              />
            )}
          </FormField>
        )}
      />

      <div className="flex flex-col gap-2">
        <Label htmlFor={fileInputId}>
          Resume <OptionalMark />
        </Label>
        {file ? (
          <div className="flex items-center justify-between gap-3 rounded-md border bg-muted/40 px-3 py-2">
            <span className="flex min-w-0 items-center gap-2">
              <FileText className="size-4 shrink-0 text-primary" aria-hidden="true" />
              <span className="min-w-0 truncate text-sm">{file.name}</span>
              <span className="shrink-0 text-xs text-muted-foreground">{formatBytes(file.size)}</span>
            </span>
            <Button type="button" variant="ghost" size="icon-sm" aria-label={`Remove ${file.name}`} disabled={pending} onClick={() => setFile(null)}>
              <X />
            </Button>
          </div>
        ) : (
          <Button type="button" variant="outline" className="w-fit" onClick={() => fileInput.current?.click()}>
            <Upload data-icon="inline-start" />
            Choose a File
          </Button>
        )}
        <input
          id={fileInputId}
          ref={fileInput}
          type="file"
          accept={RESUME_ACCEPT}
          className="sr-only"
          tabIndex={-1}
          onChange={(event) => {
            chooseFile(event.target.files?.[0]);
            event.target.value = "";
          }}
        />
        {fileProblem ? (
          <FormMessage>{fileProblem}</FormMessage>
        ) : (
          <p className="text-2sm text-muted-foreground">{RESUME_FORMATS}. It is uploaded once the candidate is saved; you can also add it later.</p>
        )}
        {file && <ResumeAiNotice />}
      </div>

      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel} disabled={pending}>
          Cancel
        </Button>
        <Button type="submit" disabled={pending}>
          {pending && <Spinner data-icon="inline-start" />}
          {upload.isPending ? "Uploading resume" : "Add Candidate"}
        </Button>
      </DialogFooter>
    </form>
  );
}

/**
 * Add a candidate: the person the team finds jobs for. A candidate is a record, never an
 * account, so nothing is sent to them. The resume can be uploaded in the same step.
 */
export function AddCandidateDialog({
  open,
  onOpenChange,
  teamId,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  teamId?: string;
  onCreated?: (candidate: Candidate) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92svh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="text-lg">Add Candidate</DialogTitle>
          <DialogDescription>
            The person you are finding a job for. A candidate has no account and receives nothing from the portal.
          </DialogDescription>
        </DialogHeader>
        {open && (
          <AddCandidateForm
            teamId={teamId}
            onCancel={() => onOpenChange(false)}
            onCreated={(candidate) => {
              onCreated?.(candidate);
              onOpenChange(false);
            }}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ edit details */

const editSchema = z.object(detailsShape);
type EditValues = z.infer<typeof editSchema>;

export function EditCandidateDialog({ candidate, onClose }: { candidate: Candidate; onClose: () => void }) {
  const update = useUpdateCandidate();
  const form = useForm<EditValues>({
    resolver: zodResolver(editSchema),
    defaultValues: { name: candidate.name, email: candidate.email, phone: candidate.phone, notes: candidate.notes },
  });
  const { errors, isDirty } = form.formState;

  const onSubmit = form.handleSubmit((values) => {
    // Only what changed is sent; an emptied e-mail field removes the address (null).
    const body: Partial<CandidateUpdate> = {};
    if (values.name !== candidate.name) body.name = values.name;
    if (values.email !== candidate.email) body.email = values.email || null;
    if (values.phone !== candidate.phone) body.phone = values.phone;
    if (values.notes.trim() !== candidate.notes) body.notes = values.notes.trim();
    if (Object.keys(body).length === 0) {
      onClose();
      return;
    }
    update.mutate(
      { id: candidate.id, body },
      {
        onSuccess: () => {
          toast.success("Details saved");
          onClose();
        },
        onError: (error) => {
          applyServerErrors(form, error);
        },
      },
    );
  });
  const failure = isGeneralError(form, update.error);

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[92svh] overflow-y-auto sm:max-w-lg">
        <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle className="text-lg">Edit Details</DialogTitle>
            <DialogDescription>Name, contact details and notes of {candidate.name}.</DialogDescription>
          </DialogHeader>
          {failure && <FormMessage>{errorMessage(update.error)}</FormMessage>}
          <TextField label="Full name" autoComplete="off" error={errors.name?.message} {...form.register("name")} />
          <div className="grid gap-4 sm:grid-cols-2">
            <TextField label="E-mail" optional type="email" autoComplete="off" error={errors.email?.message} {...form.register("email")} />
            <TextField label="Phone" optional type="tel" autoComplete="off" error={errors.phone?.message} {...form.register("phone")} />
          </div>
          <TextareaField
            label="Notes"
            optional
            rows={5}
            description="The responsible member sees these notes."
            error={errors.notes?.message}
            {...form.register("notes")}
          />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={update.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={update.isPending || !isDirty}>
              {update.isPending && <Spinner data-icon="inline-start" />}
              Save Details
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ assign or move to a member */

/**
 * Choose who is responsible for a candidate, move the candidate to another member, or take
 * it back (unassigned). The candidate's jobs and history stay with the candidate either way.
 */
export function AssignMemberDialog({ candidate, onClose }: { candidate: Candidate; onClose: () => void }) {
  const update = useUpdateCandidate();
  const current = candidate.assigned_member ?? null;
  const [memberId, setMemberId] = useState(current?.id ?? "");
  const unchanged = memberId === (current?.id ?? "");

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-md">
        <form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (unchanged) return;
            update.mutate(
              { id: candidate.id, body: { assigned_member_id: memberId || null } },
              {
                onSuccess: (saved) => {
                  toast.success(
                    saved.assigned_member
                      ? `${saved.assigned_member.name} is now responsible for ${saved.name}`
                      : `${saved.name} is unassigned`,
                  );
                  onClose();
                },
              },
            );
          }}
        >
          <DialogHeader>
            <DialogTitle className="text-lg">{current ? "Move to Another Member" : "Assign to a Member"}</DialogTitle>
            <DialogDescription>
              {current
                ? `${current.name} is responsible for ${candidate.name} now. `
                : `Nobody is responsible for ${candidate.name} yet. `}
              The candidate&apos;s jobs, notes and history stay with the candidate, and the new member is notified.
            </DialogDescription>
          </DialogHeader>
          {update.error && <FormMessage>{errorMessage(update.error)}</FormMessage>}
          <FormField
            label="Responsible member"
            description={
              candidate.team
                ? `Active members of ${candidate.team.name}.`
                : "This candidate has no team, so there is no member to choose. An Admin can move the candidate to a team."
            }
          >
            {({ id, describedBy }) => (
              <MemberSelect
                id={id}
                describedBy={describedBy}
                teamId={candidate.team?.id}
                value={memberId}
                current={current}
                onChange={(next) => {
                  setMemberId(next);
                  if (update.error) update.reset();
                }}
              />
            )}
          </FormField>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={update.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={unchanged || update.isPending}>
              {update.isPending && <Spinner data-icon="inline-start" />}
              {memberId ? (current ? "Move Candidate" : "Assign Candidate") : "Unassign Candidate"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
