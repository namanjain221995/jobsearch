"use client";

import { useId, useRef, useState, type DragEvent } from "react";
import { Download, FileText, Trash2, Upload } from "lucide-react";
import { toast } from "sonner";

import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { FormMessage } from "@/components/shared/form-fields";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { errorMessage } from "@/lib/api";
import { formatBytes, formatDateTime } from "@/lib/format";
import {
  candidateResumeDownloadPath,
  useCandidateProfile,
  useCandidateResume,
  useDeleteCandidateResume,
  useUpdateCandidateProfile,
  useUploadCandidateResume,
} from "@/lib/queries";
import { RESUME_ACCEPT, RESUME_FORMATS, resumeFileProblem } from "@/lib/resume-file";
import type { Resume } from "@/lib/types";
import { cn } from "@/lib/utils";

/** "OpenAI", "OpenAI and Anthropic", "OpenAI, Anthropic and Another". */
const joinNames = (names: string[]) =>
  names.length <= 2 ? names.join(" and ") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;

const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.trim() !== "") : [];

/**
 * Where the text of a resume goes, said before anything is uploaded and from the server's actual
 * configuration (`me.settings.ai_providers`: the outside AI services a search sends resume text to).
 */
export function ResumeAiNotice({ className }: { className?: string }) {
  const aiProviders = useMe().settings.ai_providers;
  return (
    <FormMessage tone="info" className={className}>
      {aiProviders.length > 0
        ? `When a job search runs for this candidate, text from this resume is sent to ${joinNames(aiProviders)} to match and rank the jobs found. The uploaded file is kept on this server, where the candidate's responsible member can download it.`
        : "No outside AI service is configured on this server. The resume text and the uploaded file stay on this server."}
    </FormMessage>
  );
}

/** The file that was uploaded, as a download. Resumes from before files were kept have only their text. */
export function ResumeDownload({ candidateId, resume, size = "sm" }: { candidateId: string; resume: Pick<Resume, "has_file" | "filename">; size?: "sm" | "default" }) {
  if (!resume.has_file) {
    return (
      <span className="text-xs text-muted-foreground">
        Only the text of this resume was kept, so there is no file to download.
      </span>
    );
  }
  return (
    <Button asChild variant="outline" size={size}>
      <a href={candidateResumeDownloadPath(candidateId)} download={resume.filename}>
        <Download data-icon="inline-start" />
        Download File
      </a>
    </Button>
  );
}

/** The text read from the resume: what a search matches jobs against, and what a member copies from when applying. */
export function ResumeText({ resume }: { resume: Resume }) {
  if (!resume.text) return <p className="text-sm text-muted-foreground">No text could be read from this resume.</p>;
  return (
    <div
      tabIndex={0}
      role="region"
      aria-label="Resume text"
      className="max-h-[32rem] overflow-y-auto overscroll-contain rounded-md border bg-muted/40 px-3 py-2.5 text-sm leading-relaxed break-words whitespace-pre-wrap focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
    >
      {resume.text}
    </div>
  );
}

function ResumeFacts({ resume }: { resume: Resume }) {
  return (
    <div className="flex min-w-0 items-start gap-2.5">
      <FileText className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden="true" />
      <div className="flex min-w-0 flex-col gap-0.5">
        <p className="text-sm font-medium break-all">{resume.filename}</p>
        <p className="text-xs text-muted-foreground">
          Version {resume.version}, {formatBytes(resume.size_bytes)}, uploaded {formatDateTime(resume.created_at)}
          {resume.uploaded_by ? ` by ${resume.uploaded_by.name}` : ""}
        </p>
      </div>
    </div>
  );
}

/**
 * A candidate's resume for the member who applies on their behalf: the file to download and
 * the text to read. Nothing here can be changed; the Team Lead keeps the resume up to date.
 */
export function ResumeView({ candidateId }: { candidateId: string }) {
  const resume = useCandidateResume(candidateId);
  if (resume.isPending) return <LoadingSkeleton variant="list" count={2} />;
  if (resume.isError) return <ErrorState compact error={resume.error} onRetry={() => resume.refetch()} />;
  const current = resume.data;
  if (!current) {
    return (
      <EmptyState
        compact
        icon={FileText}
        title="No resume yet"
        description="Your Team Lead uploads the candidate's resume. It appears here as soon as it is in."
      />
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3 rounded-md border bg-muted/40 p-3">
        <ResumeFacts resume={current} />
        <ResumeDownload candidateId={candidateId} resume={current} />
      </div>
      {current.truncated && (
        <FormMessage tone="info">This resume was longer than the limit, so only the first part of its text was kept.</FormMessage>
      )}
      <ResumeText resume={current} />
    </div>
  );
}

/** What was read from the resume text, shown for review. Skills can be copied into the profile. */
function ExtractedDetails({ resume, candidateId }: { resume: Resume; candidateId: string }) {
  const profile = useCandidateProfile(candidateId);
  const save = useUpdateCandidateProfile(candidateId);
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
 * Upload, replace, review, download and delete a candidate's resume. For the Team Lead
 * and Admins; the responsible member reads it through ResumeView.
 */
export function ResumeUpload({ candidateId, candidateName }: { candidateId: string; candidateName: string }) {
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const resume = useCandidateResume(candidateId);
  const upload = useUploadCandidateResume();
  const remove = useDeleteCandidateResume(candidateId);
  const [dragging, setDragging] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const send = (file: File | undefined) => {
    if (!file) return;
    const issue = resumeFileProblem(file);
    setProblem(issue);
    if (issue) return;
    upload.mutate(
      { id: candidateId, file },
      {
        onSuccess: (saved) => toast.success(`${saved.filename} uploaded (version ${saved.version})`),
        onError: (error) => setProblem(errorMessage(error)),
      },
    );
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
            <ResumeFacts resume={current} />
            <div className="flex flex-wrap items-center gap-2">
              {current.has_file && <ResumeDownload candidateId={candidateId} resume={current} />}
              <Button variant="destructive" size="sm" onClick={() => setConfirmDelete(true)}>
                <Trash2 data-icon="inline-start" />
                Delete
              </Button>
            </div>
          </div>
          {!current.has_file && <ResumeDownload candidateId={candidateId} resume={current} />}
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
                ? `Drop a new file here to replace ${candidateName}'s resume, or choose a file`
                : `Drop ${candidateName}'s resume here, or choose a file`}
          </span>
          <span className="text-xs text-muted-foreground">{RESUME_FORMATS}</span>
          <input
            id={inputId}
            ref={inputRef}
            type="file"
            accept={RESUME_ACCEPT}
            className="sr-only"
            disabled={upload.isPending}
            onChange={(event) => {
              send(event.target.files?.[0]);
              event.target.value = "";
            }}
          />
        </label>
        {problem && <FormMessage>{problem}</FormMessage>}
        <ResumeAiNotice />
      </div>

      {current && (
        <div className="flex flex-col gap-3 border-t pt-4">
          <h3 className="font-label text-sm">Read from the resume</h3>
          <ExtractedDetails resume={current} candidateId={candidateId} />
        </div>
      )}

      {current && (
        <div className="flex flex-col gap-3 border-t pt-4">
          <h3 className="font-label text-sm">Resume text</h3>
          <ResumeText resume={current} />
        </div>
      )}

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="Delete this resume?"
        description={`The stored text and the uploaded file are removed. Searches for ${candidateName} then rely on the skills in the profile until a new resume is uploaded.`}
        confirmLabel="Delete resume"
        destructive
        pending={remove.isPending}
        onConfirm={() =>
          remove.mutate(undefined, {
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

/** The candidate's resume: upload, replace and delete for managers; text and download only when `readOnly` (the responsible member). */
export function CandidateResumePanel({
  candidateId,
  candidateName,
  readOnly = false,
}: {
  candidateId: string;
  candidateName: string;
  readOnly?: boolean;
}) {
  return readOnly ? <ResumeView candidateId={candidateId} /> : <ResumeUpload candidateId={candidateId} candidateName={candidateName} />;
}
