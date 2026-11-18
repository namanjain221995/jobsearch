"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ChevronDown, ExternalLink, Pencil, Send, ShieldAlert, Sparkles, X } from "lucide-react";
import { toast } from "sonner";

import { AnswerSourceBadge, RunStatusBadge } from "@/components/auto-apply/parts";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import { AssignmentDrawer } from "@/components/views/assignment-detail";
import { errorMessage } from "@/lib/api";
import {
  approveErrorText,
  atsLabel,
  blockerText,
  markPreviewed,
  needsInputReason,
  OPEN_STATUSES,
  useApproveRun,
  useAutoApplyRun,
  useCancelRun,
  useEditAnswers,
  type AutoApplyRun,
  type RunAnswer,
} from "@/lib/auto-apply";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Where a missing answer is fixed: `application_answers.<name>` or `profile.<name>` on the Profile tab. */
function fillInHref(candidateId: string | undefined, fillIn: string): string | null {
  if (!candidateId) return null;
  const [where, name] = fillIn.split(".");
  if (where === "application_answers") return `/admin/candidates/${candidateId}?tab=profile#answer-${name ?? ""}`;
  if (where === "profile") return `/admin/candidates/${candidateId}?tab=profile`;
  return null;
}

function AnswerRow({ answer, run }: { answer: RunAnswer; run: AutoApplyRun }) {
  const edit = useEditAnswers(run.id);
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(answer.value);
  // A file is the stored resume, and certifications are only ever ticked by the candidate's consent: the API refuses both.
  const editable =
    (run.status === "needs_input" || run.status === "ready_for_review") &&
    answer.type !== "file" &&
    !answer.classification.startsWith("certification:");
  const ai = answer.source === "ai_draft";
  const missing = answer.status === "missing";

  return (
    <li
      id={`run-answer-${answer.field_key}`}
      className={cn("flex scroll-mt-24 flex-col gap-1.5 py-3 first:pt-0 last:pb-0", (ai || missing) && "-mx-2 rounded-md bg-warning/8 px-2 py-2")}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-2sm font-medium break-words">{answer.label}</span>
        {answer.required && <span className="text-2sm text-muted-foreground">(required)</span>}
        {answer.source && <AnswerSourceBadge source={answer.source} />}
        {missing && <Tag tone="warning">Missing</Tag>}
        {answer.status === "blocked" && <Tag tone="danger">Blocks the run</Tag>}
        {editable && !editing && (
          <Button variant="ghost" size="xs" className="ml-auto" onClick={() => setEditing(true)} aria-label={`Edit ${answer.label}`}>
            <Pencil data-icon="inline-start" />
            Edit
          </Button>
        )}
      </div>
      {editing ? (
        <div className="flex flex-col gap-2">
          {answer.options.length > 0 ? (
            <select
              value={value}
              onChange={(e) => setValue(e.target.value)}
              aria-label={answer.label}
              className="h-9 w-full rounded-md border bg-background px-2 text-sm"
            >
              <option value="">Choose</option>
              {answer.options.map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
          ) : (
            <Textarea value={value} onChange={(e) => setValue(e.target.value)} rows={answer.type === "textarea" ? 5 : 2} aria-label={answer.label} />
          )}
          <p className="text-2sm text-muted-foreground">Saving queues a new dry run; the preview is then prepared again.</p>
          <div className="flex gap-2">
            <Button
              size="sm"
              disabled={edit.isPending}
              onClick={() =>
                edit.mutate([{ field_key: answer.field_key, value }], {
                  onSuccess: () => {
                    toast.success("Answer saved");
                    setEditing(false);
                  },
                  onError: (error) => toast.error(errorMessage(error)),
                })
              }
            >
              Save
            </Button>
            <Button size="sm" variant="outline" onClick={() => (setEditing(false), setValue(answer.value))}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <p className="text-sm break-words whitespace-pre-wrap">{answer.value || <span className="text-muted-foreground">Left empty</span>}</p>
      )}
      {ai && (
        <p className="flex items-center gap-1 text-2sm text-muted-foreground">
          <Sparkles className="size-3.5 text-warning" aria-hidden="true" />
          Written by AI. Read it before approving.
        </p>
      )}
      {answer.note && <p className="text-2sm break-words text-muted-foreground">{answer.note}</p>}
      {answer.evidence.length > 0 && (
        <Collapsible>
          <CollapsibleTrigger className="group flex items-center gap-1 text-2sm text-muted-foreground underline-offset-4 hover:underline">
            <ChevronDown className="size-3.5 transition-transform group-data-[state=open]:rotate-180" aria-hidden="true" />
            Where this comes from
          </CollapsibleTrigger>
          <CollapsibleContent>
            <ul className="mt-1 flex flex-col gap-1 border-l-2 pl-3 text-2sm break-words whitespace-pre-wrap text-muted-foreground">
              {answer.evidence.map((line, i) => (
                <li key={i}>{line}</li>
              ))}
            </ul>
          </CollapsibleContent>
        </Collapsible>
      )}
    </li>
  );
}

/** A screenshot from the API. It may have been deleted by retention (404): then a line says so. */
function Shot({ src, alt }: { src: string; alt: string }) {
  const [gone, setGone] = useState(false);
  if (gone) return <p className="text-2sm text-muted-foreground">This screenshot is no longer kept.</p>;
  return (
    <a href={src} target="_blank" rel="noreferrer" className="block overflow-hidden rounded-md border">
      {/* eslint-disable-next-line @next/next/no-img-element -- private screenshots are served by the API, not optimised */}
      <img src={src} alt={alt} className="h-auto w-full" loading="lazy" onError={() => setGone(true)} />
    </a>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2 border-b px-4 py-4 last:border-b-0 sm:px-6">
      <h3 className="font-label text-sm">{title}</h3>
      {children}
    </section>
  );
}

function RunBody({ run }: { run: AutoApplyRun }) {
  const approve = useApproveRun(run.id);
  const cancel = useCancelRun(run.id);
  const [confirm, setConfirm] = useState<"approve" | "cancel" | null>(null);
  const [assignment, setAssignment] = useState<string | null>(null);
  const candidateName = run.candidate?.name || "the candidate";
  const aiCount = run.answers.filter((a) => a.source === "ai_draft").length;
  const previews = run.screenshots.filter((s) => s.kind !== "confirmation");
  const confirmShots = run.screenshots.filter((s) => s.kind === "confirmation");
  const open = OPEN_STATUSES.includes(run.status) && run.status !== "submitting" && run.status !== "approved";

  return (
    <>
      {previews.length > 0 && (
        <Section title="Preview of the filled form">
          <p className="text-2sm text-muted-foreground">
            Taken during the dry run, in which every form submission is blocked
            {run.submission_guard ? ` (${run.submission_guard.blocked_requests} blocked, ${run.submission_guard.allowed_submissions} allowed)` : ""}.
          </p>
          <div className="flex flex-col gap-3">
            {previews.map((shot, i) => (
              <Shot key={shot.id} src={shot.url} alt={`Screenshot ${i + 1} of the filled application form`} />
            ))}
          </div>
        </Section>
      )}

      {run.blocked_reasons.length > 0 && (
        <Section title="Why it cannot be submitted">
          <ul className="flex list-disc flex-col gap-1 pl-5 text-sm">
            {run.blocked_reasons.map((b) => (
              <li key={b.code + b.detail}>
                {blockerText(b)}
                {b.detail && blockerText(b) !== b.detail && <span className="block text-2sm text-muted-foreground">{b.detail}</span>}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {run.outcome_unknown && (
        <div className="px-4 pt-4 sm:px-6">
          <Alert variant="destructive">
            <ShieldAlert />
            <AlertTitle>It is not known whether this application went through</AlertTitle>
            <AlertDescription>
              The submit button was pressed but the result could not be read. It is never retried. Check the employer&apos;s site or
              the candidate&apos;s mailbox, then record the outcome on the job assignment by hand.
              {run.assignment_id && (
                <Button size="sm" variant="outline" className="mt-2" onClick={() => setAssignment(run.assignment_id)}>
                  Open the job assignment
                </Button>
              )}
            </AlertDescription>
          </Alert>
        </div>
      )}

      {run.error && (
        <Section title="What went wrong">
          <p className="text-sm break-words text-destructive">{run.error}</p>
        </Section>
      )}

      {run.needs_input.length > 0 && (
        <Section title="Needs input">
          <p className="text-2sm text-muted-foreground">
            The portal never guesses these. Record them in the candidate&apos;s answers or profile, or type the answer for this run.
          </p>
          <ul className="flex flex-col gap-1.5 text-sm">
            {run.needs_input.map((m) => {
              const href = fillInHref(run.candidate?.id, m.fill_in);
              return (
                <li key={m.field_key} className="break-words">
                  {href ? (
                    <Link href={href} className="underline underline-offset-4">
                      {m.label}
                    </Link>
                  ) : (
                    <a href={`#run-answer-${m.field_key}`} className="underline underline-offset-4">
                      {m.label}
                    </a>
                  )}
                  <span className="text-muted-foreground">: {needsInputReason(m.reason)}</span>
                </li>
              );
            })}
          </ul>
        </Section>
      )}

      {run.status === "submitted" && (
        <Section title="Submitted">
          <p className="text-sm">
            Submitted {formatDateTime(run.submitted_at)}
            {run.approved_by ? `, approved by ${run.approved_by.name}` : ""}.
            {run.assignment_id && (
              <>
                {" "}
                The job is marked <Tag tone="success">Applied</Tag>.{" "}
                <button type="button" onClick={() => setAssignment(run.assignment_id)} className="underline underline-offset-4">
                  Open the job assignment
                </button>
              </>
            )}
          </p>
          {run.confirmation && (
            <p className="text-2sm text-muted-foreground">
              {run.confirmation.detected ? "The employer's confirmation page was detected." : "No confirmation page was detected."}
              {run.confirmation.reference ? ` Reference: ${run.confirmation.reference}.` : ""}
            </p>
          )}
          {confirmShots.map((shot) => (
            <Shot key={shot.id} src={shot.url} alt="The employer's page after submitting" />
          ))}
        </Section>
      )}

      <Section title={`Answers (${run.answers.length})`}>
        {aiCount > 0 && (
          <p className="text-2sm text-muted-foreground">
            {aiCount} answer{aiCount === 1 ? " was" : "s were"} drafted by AI and {aiCount === 1 ? "is" : "are"} highlighted.
          </p>
        )}
        {run.answers.length === 0 ? (
          <p className="text-sm text-muted-foreground">No answers yet.</p>
        ) : (
          <ul className="flex flex-col divide-y">
            {run.answers.map((a) => (
              <AnswerRow key={a.field_key} answer={a} run={run} />
            ))}
          </ul>
        )}
      </Section>

      <Section title="History">
        <p className="text-2sm text-muted-foreground">
          Started {formatDateTime(run.created_at)}
          {run.created_by ? ` by ${run.created_by.name}` : ""}. Last change {formatDateTime(run.updated_at)}.
        </p>
      </Section>

      {open && (
        <div className="sticky bottom-0 flex flex-wrap justify-end gap-2 border-t bg-background px-4 py-3 sm:px-6">
          {open && (
            <Button variant="outline" onClick={() => setConfirm("cancel")}>
              <X data-icon="inline-start" />
              Cancel run
            </Button>
          )}
          {run.status === "ready_for_review" && (
            <Button onClick={() => setConfirm("approve")} disabled={!run.answers_hash}>
              <Send data-icon="inline-start" />
              Approve and submit
            </Button>
          )}
        </div>
      )}

      <ConfirmDialog
        open={confirm === "approve"}
        onOpenChange={(open) => !open && setConfirm(null)}
        title="Submit this application?"
        description={
          <div className="flex flex-col gap-2">
            <p>
              The application for <strong className="text-foreground">{run.job.title}</strong>
              {run.job.company ? ` at ${run.job.company}` : ""} is submitted in <strong className="text-foreground">{candidateName}</strong>&apos;s
              name to {run.job_url ? <span className="break-all">{run.job_url}</span> : "the employer"}, with the {run.answers.length} answers listed
              {aiCount > 0 ? `, ${aiCount} of them drafted by AI` : ""}, exactly as in the preview you are looking at. If anything changed
              since, the submission is refused.
            </p>
            <p className="font-medium text-foreground">A submitted application cannot be undone or withdrawn from here.</p>
          </div>
        }
        confirmLabel="Submit application"
        pending={approve.isPending}
        onConfirm={() =>
          run.answers_hash &&
          approve.mutate(run.answers_hash, {
            onSuccess: () => {
              toast.success("Application approved and being submitted");
              setConfirm(null);
            },
            // 409: the answers changed, consent ended, the job was applied meanwhile... the detail is reloaded.
            onError: (error) => (toast.error(approveErrorText(error) ?? errorMessage(error)), setConfirm(null)),
          })
        }
      />
      <AssignmentDrawer assignmentId={assignment} onOpenChange={(open) => !open && setAssignment(null)} />
      <ConfirmDialog
        open={confirm === "cancel"}
        onOpenChange={(open) => !open && setConfirm(null)}
        title="Cancel this run?"
        description="Nothing is submitted. The job stays open and you can start a new run later."
        confirmLabel="Cancel run"
        cancelLabel="Keep"
        destructive
        pending={cancel.isPending}
        onConfirm={() =>
          cancel.mutate(undefined, {
            onSuccess: () => (toast.success("Run cancelled"), setConfirm(null)),
            onError: (error) => (toast.error(approveErrorText(error) ?? errorMessage(error)), setConfirm(null)),
          })
        }
      />
    </>
  );
}

/** One auto-apply run: job, preview, answers, problems and the review actions. Opening it counts as "previewed". */
export function RunSheet({ runId, onOpenChange }: { runId: string | null; onOpenChange: (open: boolean) => void }) {
  const query = useAutoApplyRun(runId);
  const run = query.data;

  // A ready run counts as previewed once its detail, with that exact answers_hash, has been shown.
  useEffect(() => {
    if (run && run.id === runId && run.status === "ready_for_review" && run.answers_hash) markPreviewed(run.id, run.answers_hash);
  }, [run, runId]);

  return (
    <Sheet open={Boolean(runId)} onOpenChange={onOpenChange}>
      <SheetContent className="w-full! gap-0 overflow-y-auto sm:max-w-2xl!">
        <SheetHeader className="border-b pr-12">
          {run ? (
            <>
              <SheetTitle className="text-lg break-words">{run.job.title}</SheetTitle>
              <SheetDescription asChild>
                <div className="flex flex-col gap-2">
                  <span className="break-words">
                    {run.job.company}
                    {run.candidate?.name ? ` · for ${run.candidate.name}` : ""}
                  </span>
                  <span className="flex flex-wrap items-center gap-2">
                    <RunStatusBadge status={run.status} />
                    <Tag>{atsLabel(run.ats)}</Tag>
                    {run.job_url && (
                      <a href={run.job_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-2sm underline underline-offset-4">
                        Application form
                        <ExternalLink className="size-3.5" aria-hidden="true" />
                      </a>
                    )}
                  </span>
                </div>
              </SheetDescription>
            </>
          ) : (
            <SheetTitle>Application run</SheetTitle>
          )}
        </SheetHeader>
        {query.isPending ? (
          <LoadingSkeleton variant="detail" count={3} className="p-4" />
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={() => query.refetch()} />
        ) : run ? (
          <>
            {run.status === "blocked" && (
              <div className="px-4 pt-4 sm:px-6">
                <Alert variant="destructive">
                  <ShieldAlert />
                  <AlertTitle>A person has to finish this application</AlertTitle>
                  <AlertDescription>The reasons are listed below. Nothing was submitted.</AlertDescription>
                </Alert>
              </div>
            )}
            <RunBody run={run} />
          </>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}
