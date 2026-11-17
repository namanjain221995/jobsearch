"use client";

import { useId, useRef, useState, type DragEvent } from "react";
import { FileText, Trash2, Upload } from "lucide-react";
import { toast } from "sonner";

import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { FormMessage } from "@/components/shared/form-fields";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { errorMessage } from "@/lib/api";
import { formatBytes, formatDateTime } from "@/lib/format";
import { useDeleteResume, useProfile, useResume, useUpdateProfile, useUploadResume } from "@/lib/queries";
import type { Resume } from "@/lib/types";
import { cn } from "@/lib/utils";

const MAX_BYTES = 10 * 1024 * 1024;
const EXTENSIONS = [".pdf", ".docx", ".txt"];

function validate(file: File): string | null {
  const name = file.name.toLowerCase();
  if (!EXTENSIONS.some((extension) => name.endsWith(extension))) return "Choose a PDF, DOCX or TXT file.";
  if (file.size > MAX_BYTES) return `This file is ${formatBytes(file.size)}. The limit is 10 MB.`;
  if (file.size === 0) return "This file is empty.";
  return null;
}

const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.trim() !== "") : [];

/** What was read from the resume text, shown for review. Skills can be copied into the profile. */
function ExtractedDetails({ resume, memberId }: { resume: Resume; memberId: string }) {
  const profile = useProfile(memberId);
  const save = useUpdateProfile(memberId);
  const parsed = resume.parsed_profile as Record<string, unknown>;
  const skills = strings(parsed.skills);
  const titles = strings(parsed.job_titles);
  const education = strings(parsed.education);
  const current = new Set((profile.data?.skills ?? []).map((skill) => skill.toLowerCase()));
  const missing = skills.filter((skill) => !current.has(skill.toLowerCase()));

  if (skills.length + titles.length + education.length === 0) {
    return <p className="text-sm text-muted-foreground">Nothing could be picked out of this resume automatically.</p>;
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h4 className="text-2sm text-muted-foreground">Skills found in the resume ({skills.length})</h4>
          {profile.data && missing.length > 0 && (
            <Button
              variant="outline"
              size="sm"
              disabled={save.isPending}
              onClick={() =>
                save.mutate(
                  { skills: [...(profile.data?.skills ?? []), ...missing].slice(0, 120) },
                  {
                    onSuccess: () => toast.success(`${missing.length} skill${missing.length === 1 ? "" : "s"} added to the profile`),
                    onError: (error) => toast.error(errorMessage(error)),
                  },
                )
              }
            >
              {save.isPending && <Spinner data-icon="inline-start" />}
              Add {missing.length} missing to profile
            </Button>
          )}
        </div>
        {skills.length === 0 ? (
          <p className="text-sm text-muted-foreground">None found.</p>
        ) : (
          <ul className="flex flex-wrap gap-1.5">
            {skills.map((skill) => (
              <li key={skill}>
                <Tag tone={current.has(skill.toLowerCase()) ? "primary" : "neutral"}>{skill}</Tag>
              </li>
            ))}
          </ul>
        )}
        {profile.data && skills.length > 0 && (
          <p className="text-xs text-muted-foreground">
            {missing.length === 0
              ? "All of these are already in the profile."
              : "Highlighted skills are already in the profile. Check the list before adding the rest: it was read automatically and can be wrong."}
          </p>
        )}
      </div>
      {titles.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <h4 className="text-2sm text-muted-foreground">Job titles found</h4>
          <p className="text-sm">{titles.join(", ")}</p>
        </div>
      )}
      {education.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <h4 className="text-2sm text-muted-foreground">Education found</h4>
          <p className="text-sm">{education.join(", ")}</p>
        </div>
      )}
    </div>
  );
}

/**
 * Upload, replace, review and delete a member's resume. Members use it for
 * themselves; a Team Lead or Admin can upload on a member's behalf.
 */
export function ResumeUpload({ memberId, onBehalfOf }: { memberId: string; onBehalfOf?: string }) {
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const resume = useResume(memberId);
  const upload = useUploadResume(memberId, Boolean(onBehalfOf));
  const remove = useDeleteResume(memberId);
  const [dragging, setDragging] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const send = (file: File | undefined) => {
    if (!file) return;
    const issue = validate(file);
    setProblem(issue);
    if (issue) return;
    upload.mutate(file, {
      onSuccess: (saved) => toast.success(`${saved.filename} uploaded (version ${saved.version})`),
      onError: (error) => setProblem(errorMessage(error)),
    });
  };

  const onDrop = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    setDragging(false);
    if (!upload.isPending) send(event.dataTransfer.files?.[0]);
  };

  if (resume.isPending) return <LoadingSkeleton variant="list" count={2} />;
  if (resume.isError) return <ErrorState compact error={resume.error} onRetry={() => resume.refetch()} />;
  const current = resume.data;

  return (
    <div className="flex flex-col gap-5">
      {current && (
        <div className="flex flex-col gap-3 rounded-md border bg-muted/40 p-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="flex min-w-0 items-start gap-2.5">
              <FileText className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden="true" />
              <div className="flex min-w-0 flex-col gap-0.5">
                <p className="text-sm font-medium break-all">{current.filename}</p>
                <p className="text-xs text-muted-foreground">
                  Version {current.version}, {formatBytes(current.size_bytes)}, uploaded {formatDateTime(current.created_at)}
                  {current.uploaded_by ? ` by ${current.uploaded_by.name}` : ""}
                </p>
              </div>
            </div>
            <Button variant="destructive" size="sm" onClick={() => setConfirmDelete(true)}>
              <Trash2 data-icon="inline-start" />
              Delete
            </Button>
          </div>
          {current.truncated && (
            <FormMessage tone="info">
              This resume was longer than the limit, so only the first part of its text was kept.
            </FormMessage>
          )}
        </div>
      )}

      <div className="flex flex-col gap-2">
        <label
          htmlFor={inputId}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={cn(
            "flex cursor-pointer flex-col items-center gap-2 rounded-lg border border-dashed border-input bg-card px-4 py-7 text-center transition-colors hover:bg-muted/50 has-[input:focus-visible]:border-ring has-[input:focus-visible]:ring-3 has-[input:focus-visible]:ring-ring/50",
            dragging && "border-primary bg-accent",
            upload.isPending && "pointer-events-none opacity-70",
          )}
        >
          {upload.isPending ? (
            <Spinner className="size-5" />
          ) : (
            <Upload className="size-5 text-muted-foreground" aria-hidden="true" />
          )}
          <span className="text-sm font-medium">
            {upload.isPending
              ? "Uploading and reading the resume"
              : current
                ? "Drop a new file here to replace the resume, or choose a file"
                : "Drop the resume here, or choose a file"}
          </span>
          <span className="text-xs text-muted-foreground">PDF, DOCX or TXT, up to 10 MB</span>
          <input
            id={inputId}
            ref={inputRef}
            type="file"
            accept=".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain"
            className="sr-only"
            disabled={upload.isPending}
            onChange={(event) => {
              send(event.target.files?.[0]);
              event.target.value = "";
            }}
          />
        </label>
        {problem && <FormMessage>{problem}</FormMessage>}
        {onBehalfOf && <p className="text-xs text-muted-foreground">You are uploading on behalf of {onBehalfOf}.</p>}
        <p className="text-xs text-muted-foreground">
          {current?.ai_processing_notice ??
            "Only the text of the file is kept. It is used to match jobs to the member when a search runs."}
        </p>
      </div>

      {current && (
        <div className="flex flex-col gap-3 border-t pt-4">
          <h3 className="font-label text-sm">Read from the resume</h3>
          <ExtractedDetails resume={current} memberId={memberId} />
        </div>
      )}

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="Delete this resume?"
        description="The stored resume text is removed. Searches for this member then rely on the skills in the profile until a new resume is uploaded."
        confirmLabel="Delete resume"
        destructive
        pending={remove.isPending}
        onConfirm={() =>
          current &&
          remove.mutate(current.id, {
            onSuccess: (result) => {
              toast.success(result.message);
              setConfirmDelete(false);
            },
            onError: (error) => toast.error(errorMessage(error)),
          })
        }
      />
    </div>
  );
}
