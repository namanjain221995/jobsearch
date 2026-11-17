"use client";

import Link from "next/link";
import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { JobDetails, JobTitleBlock } from "@/components/jobs/job-details";
import { JobFactLine } from "@/components/jobs/job-meta";
import { Watermarked } from "@/components/jobs/watermark";
import { PageHeader } from "@/components/shared/page-header";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { closeOnInternalLink } from "@/lib/dom";
import { useAssignment } from "@/lib/queries";

/**
 * When a member opens a job, the API marks it as viewed. The lists and
 * dashboards are refreshed so that change shows without a reload.
 */
function useViewedRefresh(assignmentId: string | null | undefined, status: string | undefined) {
  const me = useMe();
  const client = useQueryClient();
  useEffect(() => {
    if (!assignmentId || !status || me.role !== "MEMBER") return;
    void client.invalidateQueries({ queryKey: ["assignments", "list"] });
    void client.invalidateQueries({ queryKey: ["analytics"] });
    void client.invalidateQueries({ queryKey: ["activity"] });
  }, [assignmentId, status, me.role, client]);
}

/** One assigned job as a full page (the member's job detail). */
export function AssignmentDetailPage({ assignmentId, back }: { assignmentId: string; back: { href: string; label: string } }) {
  const assignment = useAssignment(assignmentId);
  useViewedRefresh(assignment.data?.id, assignment.data?.status);

  if (assignment.isPending) {
    return (
      <>
        <PageHeader title="Job" back={back} />
        <LoadingSkeleton variant="detail" count={6} />
      </>
    );
  }
  if (assignment.isError) {
    return (
      <>
        <PageHeader title="Job" back={back} />
        <div className="rounded-lg border bg-card">
          {/*
            Two different answers end here, both with the way back to the list:
            410, a job of this member's that a manager took back: the API's own sentence is shown as it is;
            403 (or 404), a job that is not theirs or does not exist: the words below.
          */}
          <ErrorState
            error={assignment.error}
            onRetry={() => assignment.refetch()}
            unavailable={{
              title: "This job is not on your list",
              description:
                "The link may belong to a job that was never assigned to you. Everything assigned to you is under My Jobs.",
            }}
            action={
              <Button asChild variant="outline" size="sm">
                <Link href={back.href}>Go to {back.label}</Link>
              </Button>
            }
          />
        </div>
      </>
    );
  }
  const data = assignment.data;
  return (
    <Watermarked>
      <PageHeader
        back={back}
        title={data.job.title}
        description={<span className="text-foreground">{data.job.company}</span>}
        className="mb-5"
      />
      <JobFactLine job={data.job} className="-mt-2 mb-6 text-sm" />
      <JobDetails job={data.job} assignment={data} layout="page" />
    </Watermarked>
  );
}

/**
 * One assigned job in a side drawer, for managers working through a list.
 * Full screen on small screens.
 */
export function AssignmentDrawer({
  assignmentId,
  onOpenChange,
}: {
  /** The drawer is open while an id is set. */
  assignmentId: string | null;
  onOpenChange: (open: boolean) => void;
}) {
  const assignment = useAssignment(assignmentId);
  const data = assignment.data;
  return (
    <Sheet open={assignmentId !== null} onOpenChange={onOpenChange}>
      <SheetContent className="w-full! gap-0 overflow-y-auto sm:max-w-2xl!" onClickCapture={closeOnInternalLink(onOpenChange)}>
        <SheetHeader className="border-b pr-12">
          {data ? (
            <>
              <SheetTitle asChild>
                <div>
                  <JobTitleBlock job={data.job} status={data.status} score={data.match_score} />
                </div>
              </SheetTitle>
              <SheetDescription className="sr-only">
                Assignment of {data.job.title} at {data.job.company} to {data.member.name}
              </SheetDescription>
              <JobFactLine job={data.job} className="pt-2" />
            </>
          ) : (
            <>
              <SheetTitle>Assignment</SheetTitle>
              <SheetDescription className="sr-only">Details of one assigned job</SheetDescription>
            </>
          )}
        </SheetHeader>
        <div className="flex-1 p-4">
          {assignment.isError ? (
            <ErrorState compact error={assignment.error} onRetry={() => assignment.refetch()} />
          ) : !data ? (
            <LoadingSkeleton variant="detail" count={4} />
          ) : (
            <JobDetails job={data.job} assignment={data} layout="drawer" />
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
