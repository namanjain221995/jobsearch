"use client";

import { useState } from "react";
import { BadgeCheck, ClipboardCheck, FolderInput, Pencil } from "lucide-react";
import { toast } from "sonner";

import { AssignmentDialog, DelegateDialog } from "@/components/jobs/assignment-dialog";
import { JobDetails } from "@/components/jobs/job-details";
import { EditJobDialog } from "@/components/jobs/job-form";
import { ManagerPanel } from "@/components/jobs/job-manager";
import { JobFactLine, VerificationTag } from "@/components/jobs/job-meta";
import { PageHeader } from "@/components/shared/page-header";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { useArea, useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { AssignmentDrawer } from "@/components/views/assignment-detail";
import { errorMessage } from "@/lib/api";
import { humanize } from "@/lib/format";
import { useJob, useVerifyJob } from "@/lib/queries";

/**
 * One posting as managers see it: the full job, its links, who has it and which
 * queues it is waiting in. An Admin can also verify and edit it and send it to a team.
 */
export function JobManagerDetailView({ jobId }: { jobId: string }) {
  const me = useMe();
  const area = useArea();
  const isAdmin = me.role === "ADMIN";
  const job = useJob(jobId);
  const verify = useVerifyJob();
  const [dialog, setDialog] = useState<"members" | "team" | "edit" | null>(null);
  const [opened, setOpened] = useState<string | null>(null);
  const back = { href: `${area}/jobs`, label: isAdmin ? "Jobs" : "Pending Jobs" };

  if (job.isPending) {
    return (
      <>
        <PageHeader title="Job" back={back} />
        <LoadingSkeleton variant="detail" count={6} />
      </>
    );
  }
  if (job.isError) {
    return (
      <>
        <PageHeader title="Job" back={back} />
        <div className="rounded-lg border bg-card">
          <ErrorState error={job.error} onRetry={() => job.refetch()} />
        </div>
      </>
    );
  }

  const data = job.data;
  const ref = [{ id: data.id, title: data.title, company: data.company }];

  return (
    <>
      <PageHeader
        back={back}
        title={data.title}
        meta={<VerificationTag status={data.verification_status} />}
        description={<span className="text-foreground">{data.company}</span>}
        className="mb-5"
        actions={
          <>
            {isAdmin && (
              <>
                <Button
                  variant="outline"
                  disabled={verify.isPending}
                  onClick={() =>
                    verify.mutate(data.id, {
                      onSuccess: (saved) =>
                        toast.message(`Checked: ${humanize(saved.verification_status)}`, { description: saved.verification_note || undefined }),
                      onError: (error) => toast.error(errorMessage(error)),
                    })
                  }
                >
                  {verify.isPending ? <Spinner data-icon="inline-start" /> : <BadgeCheck data-icon="inline-start" />}
                  {verify.isPending ? "Checking" : "Verify Now"}
                </Button>
                <Button variant="outline" onClick={() => setDialog("edit")}>
                  <Pencil data-icon="inline-start" />
                  Edit
                </Button>
                <Button variant="outline" onClick={() => setDialog("team")}>
                  <FolderInput data-icon="inline-start" />
                  Assign to Team Lead
                </Button>
              </>
            )}
            <Button onClick={() => setDialog("members")}>
              <ClipboardCheck data-icon="inline-start" />
              Assign to Members
            </Button>
          </>
        }
      />
      <JobFactLine job={data} className="-mt-2 mb-6 text-sm" />
      <JobDetails job={data} layout="page" managerPanel={<ManagerPanel job={data} onOpenAssignment={setOpened} />} />

      <AssignmentDialog
        open={dialog === "members"}
        onOpenChange={(open) => !open && setDialog(null)}
        jobs={ref}
        teamId={me.role === "TEAM_LEAD" ? me.team?.id : undefined}
      />
      {isAdmin && (
        <>
          <DelegateDialog open={dialog === "team"} onOpenChange={(open) => !open && setDialog(null)} jobs={ref} />
          <EditJobDialog job={data} open={dialog === "edit"} onOpenChange={(open) => !open && setDialog(null)} />
        </>
      )}
      <AssignmentDrawer assignmentId={opened} onOpenChange={(open) => !open && setOpened(null)} />
    </>
  );
}
