"use client";

import { useState } from "react";
import { Bot, ShieldAlert } from "lucide-react";
import { toast } from "sonner";

import { AtsBadge, RunStatusBadge } from "@/components/auto-apply/parts";
import { RecordConsentDialog } from "@/components/auto-apply/consent";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { errorMessage } from "@/lib/api";
import {
  activeConsent,
  atsInfo,
  eligibilityReason,
  skipReason,
  useAutoApplyStatus,
  useConsents,
  useEligibility,
  useStartRuns,
  type Eligibility,
} from "@/lib/auto-apply";
import type { Candidate } from "@/lib/types";

/** One open job, as the eligibility endpoint reports it: its application system and whether a run can start. */
function JobRow({
  item,
  allowed,
  checked,
  full,
  onToggle,
}: {
  item: Eligibility;
  allowed: boolean;
  checked: boolean;
  full: boolean;
  onToggle: (on: boolean) => void;
}) {
  const ats = atsInfo(item);
  const can = item.eligible && allowed && !full;
  const inputId = `aa-${item.assignment_id}`;
  return (
    <li className="flex items-start gap-3 p-3" data-eligible={item.eligible ? "yes" : "no"}>
      <Checkbox id={inputId} className="mt-0.5" disabled={!can} checked={checked && can} onCheckedChange={(v) => onToggle(v === true)} />
      <label htmlFor={inputId} className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="text-sm font-medium break-words">{item.job.title}</span>
        <span className="text-2sm break-words text-muted-foreground">{item.job.company}</span>
        <AtsBadge ats={ats} />
        {!item.eligible && <span className="text-2sm text-muted-foreground">{eligibilityReason(item)}</span>}
        {item.open_run_status && (
          <span className="flex items-center gap-1.5 text-2sm text-muted-foreground">
            Current run: <RunStatusBadge status={item.open_run_status} />
          </span>
        )}
      </label>
    </li>
  );
}

/**
 * Admin: choose which of a candidate's open jobs the portal should prepare applications for.
 * Nothing is submitted here: each run stops for review.
 */
export function AutoApplyDialog({ candidate, onClose }: { candidate: Candidate; onClose: () => void }) {
  const consents = useConsents(candidate.id);
  const statusQuery = useAutoApplyStatus();
  const status = statusQuery.data;
  const eligibility = useEligibility(candidate.id);
  const start = useStartRuns();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [recording, setRecording] = useState(false);

  const consent = activeConsent(consents.data);
  const items = eligibility.data?.items ?? [];

  const toggle = (id: string, on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });

  const submit = () =>
    start.mutate(
      { candidate_id: candidate.id, assignment_ids: [...selected] },
      {
        onSuccess: ({ runs: created, skipped }) => {
          if (created.length > 0) {
            toast.success(`${created.length} application${created.length === 1 ? " is" : "s are"} being prepared for your review`);
          }
          for (const skip of skipped) {
            const job = items.find((i) => i.assignment_id === skip.assignment_id)?.job.title ?? "A job";
            toast.warning(`${job} was skipped: ${skipReason(skip.reason)}`);
          }
          onClose();
        },
        onError: (error) => toast.error(errorMessage(error)),
      },
    );

  const disabledFeature = status?.enabled === false;
  // The API takes at most 20 assignments per request.
  const MAX = 20;

  return (
    <>
      <Dialog open onOpenChange={(open) => !open && !start.isPending && onClose()}>
        <DialogContent className="max-h-[92svh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle className="text-lg">Auto Apply for {candidate.name}</DialogTitle>
            <DialogDescription>
              The portal fills in each application from the candidate&apos;s recorded answers and stops before submitting. You
              review every application and approve it yourself.
            </DialogDescription>
          </DialogHeader>

          {disabledFeature && (
            <Alert>
              <ShieldAlert />
              <AlertTitle>Auto-apply is switched off on the server</AlertTitle>
              <AlertDescription>No run can be started until AUTO_APPLY_ENABLED is set on the server.</AlertDescription>
            </Alert>
          )}

          {consents.isPending ? (
            <LoadingSkeleton variant="list" count={1} />
          ) : consents.isError ? (
            <ErrorState compact error={consents.error} onRetry={() => consents.refetch()} />
          ) : consent ? (
            <p className="flex flex-wrap items-center gap-2 text-sm">
              <Tag tone="success">Consent in force</Tag>
              {consent.may_attest ? "May attest to accuracy." : "May not attest to accuracy."}
            </p>
          ) : (
            <Alert variant="destructive">
              <ShieldAlert />
              <AlertTitle>No consent recorded</AlertTitle>
              <AlertDescription>
                <p>Applications cannot be prepared or submitted in {candidate.name}&apos;s name until their consent is recorded.</p>
                <Button size="sm" className="mt-2" onClick={() => setRecording(true)}>
                  Record consent
                </Button>
              </AlertDescription>
            </Alert>
          )}

          {eligibility.data && !eligibility.data.has_resume_file && (
            <Alert>
              <ShieldAlert />
              <AlertTitle>No resume file is stored</AlertTitle>
              <AlertDescription>Runs can be started, but each one stops as blocked until a resume file is uploaded.</AlertDescription>
            </Alert>
          )}

          {eligibility.isPending ? (
            <LoadingSkeleton variant="list" count={3} />
          ) : eligibility.isError ? (
            <ErrorState compact error={eligibility.error} onRetry={() => eligibility.refetch()} />
          ) : items.length === 0 ? (
            <EmptyState compact icon={Bot} title="No open jobs" description="Every active job of this candidate has been applied to, or none is assigned." />
          ) : (
            <ul className="flex flex-col divide-y rounded-lg border" aria-label="Jobs that can be applied to">
              {items.map((item) => (
                <JobRow
                  key={item.assignment_id}
                  item={item}
                  allowed={Boolean(consent) && !disabledFeature}
                  checked={selected.has(item.assignment_id)}
                  full={selected.size >= MAX && !selected.has(item.assignment_id)}
                  onToggle={(on) => toggle(item.assignment_id, on)}
                />
              ))}
            </ul>
          )}

          <DialogFooter>
            <Button variant="outline" disabled={start.isPending} onClick={onClose}>
              Close
            </Button>
            <Button disabled={selected.size === 0 || !consent || start.isPending || disabledFeature} onClick={submit}>
              {start.isPending && <Spinner data-icon="inline-start" />}
              Prepare {selected.size || ""} application{selected.size === 1 ? "" : "s"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <RecordConsentDialog candidateId={candidate.id} candidateName={candidate.name} open={recording} onOpenChange={setRecording} />
    </>
  );
}
