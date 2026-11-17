"use client";

import { useState } from "react";
import { Cloud, ExternalLink, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { FormMessage } from "@/components/shared/form-fields";
import { Tag } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { useNow } from "@/hooks/use-now";
import { errorMessage } from "@/lib/api";
import { formatDateTime, formatRelative } from "@/lib/format";
import { candidateOrigin, syncSummary, useRefreshSalesforceCandidate } from "@/lib/salesforce";
import type { Candidate } from "@/lib/types";

/** Marks a candidate that was brought in from Salesforce. A candidate added by hand has no marker. */
export function SourceTag({ candidate }: { candidate: Candidate }) {
  if (candidateOrigin(candidate).source !== "salesforce") return null;
  return (
    <Tag data-slot="source-tag">
      <Cloud aria-hidden="true" />
      <span className="sr-only">Source: </span>
      Salesforce
    </Tag>
  );
}

/**
 * For the Team Lead and Admins, on a candidate that came from Salesforce: when the portal last
 * read the record, the way to it in Salesforce, and reading it again. The confirmation states the
 * rule the API applies (docs/salesforce-api.md, "The rule that protects local work").
 */
export function SalesforcePanel({ candidate }: { candidate: Candidate }) {
  const origin = candidateOrigin(candidate);
  const now = useNow();
  const refresh = useRefreshSalesforceCandidate();
  const [confirming, setConfirming] = useState(false);
  if (origin.source !== "salesforce") return null;

  const close = (open: boolean) => {
    setConfirming(open);
    if (!open) refresh.reset();
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
        <dl className="flex min-w-0 flex-col gap-1">
          <dt className="text-2sm text-muted-foreground">Last read from Salesforce</dt>
          <dd className="text-sm break-words">
            {origin.syncedAt ? (
              <>
                <time dateTime={origin.syncedAt}>{formatDateTime(origin.syncedAt)}</time>
                <span className="text-muted-foreground"> ({formatRelative(origin.syncedAt, now)})</span>
              </>
            ) : (
              <span className="text-muted-foreground">Not recorded</span>
            )}
          </dd>
        </dl>
        <div className="flex flex-wrap items-center gap-2">
          {origin.salesforceUrl && (
            <Button asChild variant="outline">
              <a href={origin.salesforceUrl} target="_blank" rel="noopener noreferrer">
                <ExternalLink data-icon="inline-start" />
                Open in Salesforce
                <span className="sr-only"> (opens in a new tab)</span>
              </a>
            </Button>
          )}
          <Button variant="outline" onClick={() => setConfirming(true)}>
            <RefreshCw data-icon="inline-start" />
            Refresh from Salesforce
          </Button>
        </div>
      </div>
      <p className="max-w-[80ch] text-2sm text-muted-foreground">
        The portal only reads Salesforce. Changes made here are not written back there.
      </p>

      <ConfirmDialog
        open={confirming}
        onOpenChange={close}
        title={`Refresh ${candidate.name} from Salesforce?`}
        description={
          <div className="flex flex-col gap-3">
            <p>The details, the job-search profile and the resume are read again from Salesforce.</p>
            <div className="flex flex-col gap-1.5">
              <p className="font-medium text-foreground">What is updated</p>
              <ul className="flex list-disc flex-col gap-1 pl-5">
                <li>A field that is still empty here, or still holds what Salesforce gave last time.</li>
                <li>The line in the notes that starts with &quot;Preferred job locations (from Salesforce)&quot;, if it is still as it was written.</li>
                <li>The resume, when Salesforce has a file this candidate has not received yet and the current resume also came from Salesforce, or there is none.</li>
              </ul>
            </div>
            <div className="flex flex-col gap-1.5">
              <p className="font-medium text-foreground">What is kept</p>
              <ul className="flex list-disc flex-col gap-1 pl-5">
                <li>Every field that was changed in the portal, and every value Salesforce has left empty.</li>
                <li>The rest of the notes. If the locations line was edited or removed, a changed Salesforce text is added as a new line.</li>
                <li>A resume uploaded in the portal after the Salesforce file was last changed. An older upload is replaced by the newer Salesforce file.</li>
              </ul>
            </div>
            <p>Nothing is written to Salesforce.</p>
          </div>
        }
        confirmLabel="Refresh from Salesforce"
        pending={refresh.isPending}
        onConfirm={() =>
          refresh.mutate(candidate.id, {
            onSuccess: ({ sync, name }) => {
              toast.success(`${name} was read again from Salesforce`, { description: syncSummary(sync) });
              close(false);
            },
          })
        }
      >
        {refresh.error && <FormMessage>{errorMessage(refresh.error)}</FormMessage>}
      </ConfirmDialog>
    </div>
  );
}
