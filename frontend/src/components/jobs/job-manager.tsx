"use client";

import Link from "next/link";
import { ExternalLink, Users } from "lucide-react";

import { JobDetails, JobTitleBlock } from "@/components/jobs/job-details";
import { JobFactLine } from "@/components/jobs/job-meta";
import { MatchPanel } from "@/components/jobs/match-panel";
import { Panel } from "@/components/shared/page-header";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { ApplicationStatusBadge, Tag } from "@/components/shared/status-badge";
import { useArea } from "@/components/shell/me-context";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { closeOnInternalLink } from "@/lib/dom";
import { formatDate } from "@/lib/format";
import { useJob } from "@/lib/queries";
import type { JobManagerView, MatchExplanation } from "@/lib/types";

function Links({ job }: { job: JobManagerView }) {
  const entries = [
    { label: "Posting", url: job.canonical_url },
    { label: "Application form", url: job.apply_url },
    { label: "Company site", url: job.company_url },
  ].filter((entry) => /^https?:\/\//i.test(entry.url ?? ""));
  const unique = entries.filter((entry, index) => entries.findIndex((other) => other.url === entry.url) === index);
  if (unique.length === 0) return <p className="text-sm text-muted-foreground">This job has no stored link, so nobody can open it from the portal.</p>;
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
 * What only managers see about a posting: its links, where it came from, which
 * team queues it is waiting in, and who has it.
 */
export function JobManagerInfo({ job, onOpenAssignment }: { job: JobManagerView; onOpenAssignment?: (assignmentId: string) => void }) {
  const area = useArea();
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-2">
        <h4 className="font-label text-sm">Posting links</h4>
        <Links job={job} />
        {(job.external_source || job.external_job_id) && (
          <p className="text-xs text-muted-foreground">
            {job.external_source && `Source system: ${job.external_source}. `}
            {job.external_job_id && `Posting ID: ${job.external_job_id}.`}
          </p>
        )}
      </div>
      <div className="flex flex-col gap-2 border-t pt-4">
        <h4 className="font-label text-sm">Waiting in team queues</h4>
        {job.queue_teams.length === 0 ? (
          <p className="text-sm text-muted-foreground">Not waiting in any team&apos;s queue.</p>
        ) : (
          <ul className="flex flex-wrap gap-1.5">
            {job.queue_teams.map((team) => (
              <li key={team.id}>
                <Tag tone="primary">{team.name}</Tag>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="flex flex-col gap-2 border-t pt-4">
        <h4 className="font-label text-sm">
          Assigned to <span className="font-normal text-muted-foreground tabular-nums">({job.assignment_count})</span>
        </h4>
        {job.assignees.length === 0 ? (
          <EmptyState compact icon={Users} title="Nobody has this job yet" description="Assign it to candidates, or send it to a team's queue." />
        ) : (
          <ul className="flex flex-col divide-y">
            {job.assignees.map((assignee) => (
              <li key={assignee.assignment_id} className="flex flex-wrap items-center justify-between gap-2 py-2 first:pt-0 last:pb-0">
                <span className="flex min-w-0 flex-col">
                  <Link href={`${area}/candidates/${assignee.candidate.id}`} className="w-fit rounded-sm text-sm font-medium underline-offset-4 hover:underline">
                    {assignee.candidate.name}
                  </Link>
                  <span className="text-xs text-muted-foreground">
                    {assignee.member ? `Handled by ${assignee.member.name}, ` : "No responsible member, "}
                    {assignee.team ? `${assignee.team.name}, ` : ""}assigned {formatDate(assignee.assigned_at)}
                    {assignee.applied_at ? `, applied ${formatDate(assignee.applied_at)}` : ""}
                  </span>
                </span>
                {onOpenAssignment ? (
                  <button
                    type="button"
                    onClick={() => onOpenAssignment(assignee.assignment_id)}
                    className="rounded-[5px]"
                    aria-label={`Open ${assignee.candidate.name}'s assignment`}
                  >
                    <ApplicationStatusBadge status={assignee.status} />
                  </button>
                ) : (
                  <ApplicationStatusBadge status={assignee.status} />
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/**
 * A posting in a side drawer, for managers looking through a list of jobs or
 * search results. When a match is passed (a search result for one candidate) it
 * gets its own tab.
 */
export function JobDrawer({
  jobId,
  onOpenChange,
  match,
}: {
  /** The drawer is open while an id is set. */
  jobId: string | null;
  onOpenChange: (open: boolean) => void;
  match?: { explanation: MatchExplanation; score: number; candidateName: string };
}) {
  const job = useJob(jobId ?? undefined);
  const data = job.data;
  return (
    <Sheet open={jobId !== null} onOpenChange={onOpenChange}>
      <SheetContent className="w-full! gap-0 overflow-y-auto sm:max-w-2xl!" onClickCapture={closeOnInternalLink(onOpenChange)}>
        <SheetHeader className="border-b pr-12">
          {data ? (
            <>
              <SheetTitle asChild>
                <div>
                  <JobTitleBlock job={data} score={match?.score} />
                </div>
              </SheetTitle>
              <SheetDescription className="sr-only">
                Details of {data.title} at {data.company}
              </SheetDescription>
              <JobFactLine job={data} className="pt-2" />
            </>
          ) : (
            <>
              <SheetTitle>Job</SheetTitle>
              <SheetDescription className="sr-only">Details of one job posting</SheetDescription>
            </>
          )}
        </SheetHeader>
        <div className="flex-1 p-4">
          {job.isError ? (
            <ErrorState compact error={job.error} onRetry={() => job.refetch()} />
          ) : !data ? (
            <LoadingSkeleton variant="detail" count={4} />
          ) : match ? (
            <Tabs defaultValue="match" className="gap-4">
              <TabsList variant="line" className="w-full justify-start overflow-x-auto border-b">
                <TabsTrigger value="match" className="flex-none">Match for {match.candidateName}</TabsTrigger>
                <TabsTrigger value="job" className="flex-none">Job</TabsTrigger>
                <TabsTrigger value="manage" className="flex-none">Assignments</TabsTrigger>
              </TabsList>
              <TabsContent value="match">
                <MatchPanel match={match.explanation} score={match.score} />
              </TabsContent>
              <TabsContent value="job">
                <JobDetails job={data} layout="drawer" />
              </TabsContent>
              <TabsContent value="manage">
                <JobManagerInfo job={data} />
              </TabsContent>
            </Tabs>
          ) : (
            <JobDetails job={data} layout="drawer" managerPanel={<JobManagerInfo job={data} />} />
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

export function ManagerPanel({ job, onOpenAssignment }: { job: JobManagerView; onOpenAssignment?: (assignmentId: string) => void }) {
  return (
    <Panel title="Links and assignments">
      <JobManagerInfo job={job} onOpenAssignment={onOpenAssignment} />
    </Panel>
  );
}
