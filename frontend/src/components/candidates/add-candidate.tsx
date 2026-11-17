"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { CloudDownload, Plus } from "lucide-react";

import { AddCandidateDialog } from "@/components/candidates/candidate-dialog";
import { AddFromSalesforceDialog } from "@/components/candidates/salesforce-dialog";
import { useArea } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { useSalesforceStatus } from "@/lib/salesforce";

/**
 * The buttons that add a candidate, with their dialogs. Where the portal is connected to Salesforce
 * the main one is "Add from Salesforce" and typing a candidate in by hand is the second choice;
 * without Salesforce there is only "Add Candidate", as before. A new candidate opens on their own
 * page, where the profile is checked; one that was in the portal already opens as it is.
 */
export function useAddCandidate({ teamId, disabledReason }: { teamId?: string; disabledReason?: string } = {}) {
  const area = useArea();
  const router = useRouter();
  const status = useSalesforceStatus();
  // While the answer is on its way the Salesforce button is shown: that is what a connected portal settles on.
  const salesforce = status.isPending || status.data?.configured === true;
  const [open, setOpen] = useState<"salesforce" | "manual" | null>(null);
  const disabled = Boolean(disabledReason);

  const buttons = salesforce ? (
    <>
      <Button variant="outline" onClick={() => setOpen("manual")} disabled={disabled} title={disabledReason}>
        <Plus data-icon="inline-start" />
        Add Manually
      </Button>
      <Button onClick={() => setOpen("salesforce")} disabled={disabled} title={disabledReason}>
        <CloudDownload data-icon="inline-start" />
        Add from Salesforce
      </Button>
    </>
  ) : (
    <Button onClick={() => setOpen("manual")} disabled={disabled} title={disabledReason}>
      <Plus data-icon="inline-start" />
      Add Candidate
    </Button>
  );

  const dialogs = (
    <>
      <AddFromSalesforceDialog
        open={open === "salesforce"}
        onOpenChange={(next) => setOpen(next ? "salesforce" : null)}
        teamId={teamId}
        onManual={() => setOpen("manual")}
        onChosen={(candidate, how) => router.push(`${area}/candidates/${candidate.id}${how === "imported" ? "?tab=profile" : ""}`)}
      />
      <AddCandidateDialog
        open={open === "manual"}
        onOpenChange={(next) => setOpen(next ? "manual" : null)}
        teamId={teamId}
        onCreated={(candidate) => router.push(`${area}/candidates/${candidate.id}?tab=profile`)}
      />
    </>
  );

  return { buttons, dialogs, salesforce: status.data?.configured === true };
}
