"use client";

import Link from "next/link";
import { useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ExternalLink, Send, Undo2 } from "lucide-react";
import { toast } from "sonner";

import { ApplicationTimeline } from "@/components/jobs/application-timeline";
import { AssignmentNotes } from "@/components/jobs/assignment-notes";
import { JobDescription } from "@/components/jobs/job-description";
import {
  jobLocation,
  jobSalary,
  MatchScore,
  NOT_STATED,
  ORIGIN_LABELS,
  SOURCE_LABELS,
  stated,
  VerificationTag,
} from "@/components/jobs/job-meta";
import { MatchPanel } from "@/components/jobs/match-panel";
import { StatusDialog } from "@/components/jobs/status-dialog";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Panel } from "@/components/shared/page-header";
import { ApplicationStatusBadge, StageRail, Tag } from "@/components/shared/status-badge";
import { useArea, useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { errorMessage } from "@/lib/api";
import { formatDate, formatDateTime, humanize, plural } from "@/lib/format";
import { invalidateWork, useWithdrawAssignment } from "@/lib/queries";
import { STATUS_META, STATUS_ORDER } from "@/lib/status";
import type { AssignmentDetail, AssignmentStatus, JobAssignee, JobDetail } from "@/lib/types";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ overview */

function FactGrid({ facts }: { facts: Array<{ label: string; value: ReactNode }> }) {
  return (
    <dl className="grid grid-cols-1 gap-x-8 gap-y-4 sm:grid-cols-2 xl:grid-cols-3">
      {facts.map((fact) => (
        <div key={fact.label} className="flex min-w-0 flex-col gap-1">
          <dt className="text-2sm text-muted-foreground">{fact.label}</dt>
          <dd className="text-sm break-words">{fact.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** The facts of a posting. Anything the posting does not state is shown as "Not stated". */
export function JobOverview({ job }: { job: JobDetail }) {
  const facts: Array<{ label: string; value: ReactNode }> = [
    { label: "Company", value: job.company },
    { label: "Location", value: jobLocation(job) },
    { label: "Work arrangement", value: stated(job.work_mode) },
    { label: "Employment type", value: stated(job.employment_type) },
    { label: "Seniority", value: stated(job.seniority) },
    { label: "Salary", value: jobSalary(job) },
    { label: "Experience asked for", value: stated(job.experience_text) },
    { label: "Posted", value: job.posted_at ? formatDate(job.posted_at) : NOT_STATED },
    { label: "Citizenship requirement", value: stated(job.citizenship_requirement) },
    { label: "Visa sponsorship", value: stated(job.sponsorship_status) },
    { label: "Security clearance", value: job.clearance_required ? "Required" : NOT_STATED },
    {
      label: "Source",
      value: [stated(job.source_label), ORIGIN_LABELS[job.origin] ?? humanize(job.origin)]
        .filter((part) => part && part !== NOT_STATED)
        .join(", ") || NOT_STATED,
    },
  ];
  return (
    <div className="flex flex-col gap-6">
      <FactGrid facts={facts} />
      <div className="flex flex-col gap-2">
        <h4 className="text-2sm text-muted-foreground">Skills named in the posting</h4>
        {job.requirements.length === 0 ? (
          <p className="text-sm">{NOT_STATED}</p>
        ) : (
          <ul className="flex flex-wrap gap-1.5">
            {job.requirements.map((skill) => (
              <li key={skill}>
                <Tag>{skill}</Tag>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="flex flex-col gap-2 border-t pt-4">
        <h4 className="text-2sm text-muted-foreground">Verification</h4>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-sm">
          <VerificationTag status={job.verification_status} />
          <span className="text-muted-foreground">
            {job.last_verified_at ? `Last checked ${formatDateTime(job.last_verified_at)}` : "Never checked against the posting"}
          </span>
        </div>
        {job.verification_note && <p className="text-sm text-muted-foreground">{job.verification_note}</p>}
        {job.sources.length > 1 && <p className="text-xs text-muted-foreground">Seen on: {job.sources.join(", ")}</p>}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ tracking */

function OtherAssignees({ assignees }: { assignees: JobAssignee[] }) {
  const area = useArea();
  if (assignees.length === 0) return <p className="text-sm text-muted-foreground">No other candidate has this job.</p>;
  return (
    <ul className="flex flex-col divide-y">
      {assignees.map((assignee) => (
        <li key={assignee.assignment_id} className="flex flex-wrap items-center justify-between gap-2 py-2 first:pt-0 last:pb-0">
          <span className="min-w-0">
            <Link href={`${area}/candidates/${assignee.candidate.id}`} className="rounded-sm text-sm font-medium underline-offset-4 hover:underline">
              {assignee.candidate.name}
            </Link>
            <span className="block text-xs text-muted-foreground">
              {assignee.member ? `Handled by ${assignee.member.name}` : "No responsible member"}
              {assignee.team ? `, ${assignee.team.name}` : ""}
            </span>
          </span>
          <ApplicationStatusBadge status={assignee.status} />
        </li>
      ))}
    </ul>
  );
}

function PostingLinks({ urls }: { urls: Record<string, unknown> }) {
  const entries = [
    { label: "Posting", url: urls.canonical_url },
    { label: "Application form", url: urls.apply_url },
  ].filter((entry): entry is { label: string; url: string } => typeof entry.url === "string" && /^https?:\/\//i.test(entry.url));
  const unique = entries.filter((entry, index) => entries.findIndex((other) => other.url === entry.url) === index);
  if (unique.length === 0) return <p className="text-sm text-muted-foreground">This job has no stored link.</p>;
  return (
    <ul className="flex flex-col gap-2">
      {unique.map((entry) => (
        <li key={entry.url} className="flex flex-col gap-0.5">
          <span className="text-xs text-muted-foreground">{entry.label}</span>
          <a
            href={entry.url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-start gap-1.5 rounded-sm text-sm break-all text-primary underline-offset-4 hover:underline"
          >
            <ExternalLink className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
            {entry.url}
            <span className="sr-only"> (opens in a new tab)</span>
          </a>
        </li>
      ))}
    </ul>
  );
}

/**
 * The buttons that move an application forward.
 * "Open Job / Apply" only opens the posting, through the audited redirect, in a
 * new tab. It never changes the status. "Mark as Applied" is a separate step
 * that asks for confirmation.
 */
export function AssignmentActions({ assignment, className }: { assignment: AssignmentDetail; className?: string }) {
  const me = useMe();
  const client = useQueryClient();
  const isMember = me.role === "MEMBER";
  const [target, setTarget] = useState<AssignmentStatus | null>(null);
  const [withdrawing, setWithdrawing] = useState(false);
  const withdraw = useWithdrawAssignment();

  const canMarkApplied = assignment.is_active && !assignment.applied_at && assignment.allowed_statuses.includes("APPLIED");
  const others = STATUS_ORDER.filter(
    (status) => assignment.allowed_statuses.includes(status) && !(canMarkApplied && status === "APPLIED"),
  );

  const openJob = () => {
    // The API logs the visit and redirects to the posting. The address of the posting is never put on this page.
    window.open(`/api/assignments/${assignment.id}/open-job`, "_blank", "noopener,noreferrer");
    // The visit is recorded by the time the new tab has loaded: refresh the open count, status and history
    // (this assignment is among the queries that invalidateWork refreshes).
    window.setTimeout(() => {
      void invalidateWork(client);
    }, 1500);
  };

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      {!assignment.is_active && (
        <p className="rounded-md border border-border-strong bg-muted px-3 py-2 text-sm">
          This assignment was withdrawn from the candidate&apos;s list. Its history is kept.
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        {isMember && (
          <Button
            variant={canMarkApplied ? "outline" : "default"}
            size="lg"
            className="flex-1"
            disabled={!assignment.job.has_apply_link}
            onClick={openJob}
          >
            <ExternalLink data-icon="inline-start" />
            Open Job / Apply
            <span className="sr-only"> (opens in a new tab)</span>
          </Button>
        )}
        {canMarkApplied && (
          <Button size="lg" className="flex-1" onClick={() => setTarget("APPLIED")}>
            <Send data-icon="inline-start" />
            Mark as Applied
          </Button>
        )}
      </div>
      {isMember && !assignment.job.has_apply_link && (
        <p className="text-xs text-muted-foreground">This job has no link to open. Ask your Team Lead to check the posting.</p>
      )}
      {isMember && assignment.job.has_apply_link && (
        <p className="text-xs text-muted-foreground">Opening the job is recorded, and is never counted as applying.</p>
      )}
      {assignment.is_active && (
        <div className="flex flex-wrap gap-2">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" className="flex-1" disabled={others.length === 0}>
                Change Status
                <ChevronDown data-icon="inline-end" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="min-w-64">
              <DropdownMenuLabel>Change status to</DropdownMenuLabel>
              {others.map((status) => {
                const meta = STATUS_META[status];
                return (
                  <DropdownMenuItem key={status} onSelect={() => setTarget(status)} className="items-start">
                    <meta.icon className="mt-0.5" style={{ color: meta.color }} />
                    <span className="flex flex-col">
                      <span>{meta.label}</span>
                      <span className="text-xs text-muted-foreground">{meta.meaning}</span>
                    </span>
                  </DropdownMenuItem>
                );
              })}
            </DropdownMenuContent>
          </DropdownMenu>
          {!isMember && (
            <Button variant="destructive" onClick={() => setWithdrawing(true)}>
              <Undo2 data-icon="inline-start" />
              Withdraw
            </Button>
          )}
        </div>
      )}

      <StatusDialog
        assignment={assignment}
        target={target}
        onOpenChange={(open) => !open && setTarget(null)}
        onBehalfOf={!isMember ? assignment.candidate.name : undefined}
      />
      <ConfirmDialog
        open={withdrawing}
        onOpenChange={setWithdrawing}
        title="Withdraw this assignment?"
        description={
          <>
            <span className="font-medium text-foreground">{assignment.job.title}</span> disappears from{" "}
            {assignment.candidate.name}&apos;s list and from the counts. Its history is kept, and assigning the job
            again restores it.
          </>
        }
        confirmLabel="Withdraw assignment"
        destructive
        pending={withdraw.isPending}
        onConfirm={() =>
          withdraw.mutate(assignment.id, {
            onSuccess: (result) => {
              // The hook has already refreshed everything assignments feed, this one included.
              toast.success(result.message);
              setWithdrawing(false);
            },
            onError: (error) => toast.error(errorMessage(error)),
          })
        }
      />
    </div>
  );
}

function TrackingFacts({ assignment }: { assignment: AssignmentDetail }) {
  const rows: Array<{ label: string; value: ReactNode }> = [
    {
      label: "Assigned",
      value: `${formatDateTime(assignment.assigned_at)}${assignment.assigned_by ? `, by ${assignment.assigned_by.name}` : ""}`,
    },
    { label: "How it was assigned", value: SOURCE_LABELS[assignment.source] ?? humanize(assignment.source) },
    { label: "Viewed", value: assignment.viewed_at ? formatDateTime(assignment.viewed_at) : "Not yet" },
    {
      label: "Job link opened",
      value:
        assignment.link_opens > 0
          ? `${plural(assignment.link_opens, "time")}, last ${formatDateTime(assignment.last_link_open_at)}`
          : "Not yet",
    },
    { label: "Applied", value: assignment.applied_at ? formatDateTime(assignment.applied_at) : "Not yet" },
  ];
  if (assignment.application_reference) rows.push({ label: "Reference number", value: assignment.application_reference });
  if (assignment.interviewed_at) rows.push({ label: "Interview stage reached", value: formatDateTime(assignment.interviewed_at) });
  if (assignment.offer_at) rows.push({ label: "Offer", value: formatDateTime(assignment.offer_at) });
  if (assignment.closed_at) rows.push({ label: "Closed", value: formatDateTime(assignment.closed_at) });
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
      {rows.map((row) => (
        <div key={row.label} className="contents">
          <dt className="text-muted-foreground">{row.label}</dt>
          <dd className="min-w-0 break-words">{row.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Where the application stands and what can be done next. */
export function TrackingSummary({ assignment }: { assignment: AssignmentDetail }) {
  const me = useMe();
  const area = useArea();
  const isManager = me.role !== "MEMBER";
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <ApplicationStatusBadge status={assignment.status} size="lg" />
          {isManager && (
            <span className="flex flex-col items-end text-sm">
              <span>
                <span className="text-muted-foreground">For </span>
                <Link href={`${area}/candidates/${assignment.candidate.id}`} className="rounded-sm font-medium underline-offset-4 hover:underline">
                  {assignment.candidate.name}
                </Link>
              </span>
              <span className="text-2sm text-muted-foreground">
                {assignment.member ? (
                  <>
                    Handled by{" "}
                    <Link href={`${area}/members/${assignment.member.id}`} className="rounded-sm underline-offset-4 hover:underline">
                      {assignment.member.name}
                    </Link>
                  </>
                ) : (
                  <span className="text-warning">No responsible member</span>
                )}
                {assignment.team ? `, ${assignment.team.name}` : ""}
              </span>
            </span>
          )}
        </div>
        <StageRail
          status={assignment.status}
          viewedAt={assignment.viewed_at}
          appliedAt={assignment.applied_at}
          interviewedAt={assignment.interviewed_at}
          size="lg"
          showLabels
        />
      </div>
      <AssignmentActions assignment={assignment} />
      <TrackingFacts assignment={assignment} />
      {isManager && assignment.job_urls && (
        <div className="flex flex-col gap-2 border-t pt-4">
          <h4 className="font-label text-sm">Posting links</h4>
          <PostingLinks urls={assignment.job_urls as Record<string, unknown>} />
        </div>
      )}
      {isManager && (
        <div className="flex flex-col gap-2 border-t pt-4">
          <h4 className="font-label text-sm">Other candidates with this job</h4>
          <OtherAssignees assignees={assignment.other_assignees} />
        </div>
      )}
    </div>
  );
}

/** The notes and the full history of an assignment. */
export function TrackingLog({ assignment }: { assignment: AssignmentDetail }) {
  const isManager = useMe().role !== "MEMBER";
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-3">
        <h4 className="font-label text-sm">Notes</h4>
        <AssignmentNotes assignment={assignment} canWritePrivate={isManager} />
      </div>
      <div className="flex flex-col gap-3 border-t pt-4">
        <h4 className="font-label text-sm">History</h4>
        <ApplicationTimeline events={assignment.events} />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ composition */

export function JobTitleBlock({
  job,
  status,
  score,
  heading = "h2",
}: {
  job: JobDetail;
  status?: AssignmentStatus;
  score?: number | null;
  heading?: "h1" | "h2";
}) {
  const Heading = heading;
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm text-muted-foreground">{job.company}</p>
      <Heading className={cn("font-display leading-tight break-words", heading === "h1" ? "text-2xl md:text-3xl" : "text-2xl")}>
        {job.title}
      </Heading>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 pt-0.5">
        {status && <ApplicationStatusBadge status={status} />}
        {score !== undefined && <MatchScore score={score} />}
        <VerificationTag status={job.verification_status} />
      </div>
    </div>
  );
}

/**
 * One job, in full: overview, description, match and tracking.
 * As a page the sections are laid out side by side with tracking in its own
 * column; in a drawer (and on small screens) they become tabs.
 */
export function JobDetails({
  job,
  assignment,
  layout,
  managerPanel,
}: {
  job: JobDetail;
  /** Adds the match and tracking sections. */
  assignment?: AssignmentDetail;
  layout: "page" | "drawer";
  /** Extra section for managers looking at a job outside of one assignment (who has it, queues, links). */
  managerPanel?: ReactNode;
}) {
  const overview = <JobOverview job={job} />;
  const description = <JobDescription text={job.description} />;
  const match = assignment ? <MatchPanel match={assignment.match} score={assignment.match_score} /> : null;

  if (layout === "drawer") {
    return (
      <Tabs defaultValue={assignment ? "tracking" : "overview"} className="gap-4">
        <TabsList variant="line" className="w-full justify-start overflow-x-auto border-b">
          {assignment && <TabsTrigger value="tracking" className="flex-none">Tracking</TabsTrigger>}
          <TabsTrigger value="overview" className="flex-none">Overview</TabsTrigger>
          <TabsTrigger value="description" className="flex-none">Description</TabsTrigger>
          {match && <TabsTrigger value="match" className="flex-none">Match</TabsTrigger>}
          {managerPanel && <TabsTrigger value="manage" className="flex-none">Assignments</TabsTrigger>}
        </TabsList>
        {assignment && (
          <TabsContent value="tracking" className="flex flex-col gap-6">
            <TrackingSummary assignment={assignment} />
            <div className="border-t pt-5">
              <TrackingLog assignment={assignment} />
            </div>
          </TabsContent>
        )}
        <TabsContent value="overview">{overview}</TabsContent>
        <TabsContent value="description">{description}</TabsContent>
        {match && <TabsContent value="match">{match}</TabsContent>}
        {managerPanel && <TabsContent value="manage">{managerPanel}</TabsContent>}
      </Tabs>
    );
  }

  const hasAside = Boolean(assignment || managerPanel);
  // Small screens read top to bottom: what to do next, the job itself, then the notes and history.
  // Wide screens keep the job on the left and everything about tracking in a column on the right.
  return (
    <div className={cn("grid gap-5", hasAside && "xl:grid-cols-[minmax(0,1fr)_24rem] xl:grid-rows-[auto_1fr]")}>
      {assignment && (
        <Panel title="Tracking" className="xl:col-start-2 xl:row-start-1">
          <TrackingSummary assignment={assignment} />
        </Panel>
      )}
      <div className={cn("flex min-w-0 flex-col gap-5", hasAside && "xl:col-start-1 xl:row-span-2 xl:row-start-1")}>
        <Panel title="Overview">{overview}</Panel>
        {match && <Panel title="Match">{match}</Panel>}
        <Panel title="Full description">{description}</Panel>
      </div>
      {hasAside && (
        <div className={cn("flex min-w-0 flex-col gap-5 xl:col-start-2", assignment ? "xl:row-start-2" : "xl:row-span-2 xl:row-start-1")}>
          {managerPanel}
          {assignment && (
            <Panel title="Notes and history">
              <TrackingLog assignment={assignment} />
            </Panel>
          )}
        </div>
      )}
    </div>
  );
}
