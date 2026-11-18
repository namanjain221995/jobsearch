"use client";

import { useState } from "react";
import { toast } from "sonner";

import { resultKeys, SalesforceImport } from "@/components/candidates/salesforce-import";
import { FormMessage } from "@/components/shared/form-fields";
import { SearchInput } from "@/components/shared/search-input";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { syncRecruiterSentence, syncResumeSentence, useSalesforceStatus } from "@/lib/salesforce";
import type { Candidate } from "@/lib/types";

function Body({
  teamId,
  onChosen,
  onManual,
  onClose,
}: {
  teamId?: string;
  onChosen: (candidate: Candidate, how: "imported" | "existing") => void;
  onManual?: () => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const status = useSalesforceStatus();
  const disconnected = status.isSuccess && !status.data.configured;

  return (
    <>
      {disconnected ? (
        <FormMessage tone="info">This portal is not connected to Salesforce. Add the candidate manually instead.</FormMessage>
      ) : (
        <div className="flex min-w-0 flex-col gap-3" onKeyDown={resultKeys}>
          <SearchInput
            label="Search Salesforce"
            placeholder="Search name, e-mail or phone"
            value={query}
            onChange={setQuery}
            className="sm:w-full"
          />
          <SalesforceImport
            query={query}
            defaultTeamId={teamId}
            withMember
            existingAction="Open"
            onChosen={(candidate, how, sync) => {
              if (how === "imported") {
                toast.success(`${candidate.name} brought in from Salesforce`, {
                  description: [syncResumeSentence(sync?.resume), syncRecruiterSentence(sync?.recruiter)].filter(Boolean).join(" ") || undefined,
                });
              }
              onChosen(candidate, how);
            }}
          />
        </div>
      )}
      <DialogFooter className="sm:justify-between">
        {onManual ? (
          <Button type="button" variant="ghost" onClick={onManual}>
            Add Manually Instead
          </Button>
        ) : (
          <span />
        )}
        <Button type="button" variant="outline" onClick={onClose}>
          Close
        </Button>
      </DialogFooter>
    </>
  );
}

/**
 * Add a candidate by finding them in Salesforce: search, choose the team (Admin) and optionally the
 * responsible member, and choose the person. The API reads details, profile and resume from Salesforce.
 * Someone who is already in the portal is opened instead of being brought in twice.
 */
export function AddFromSalesforceDialog({
  open,
  onOpenChange,
  teamId,
  onChosen,
  onManual,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Admin: start with this team chosen. */
  teamId?: string;
  /** The candidate that was brought in ("imported") or was in the portal already ("existing"). */
  onChosen: (candidate: Candidate, how: "imported" | "existing") => void;
  /** Offers the form for typing a candidate in by hand. */
  onManual?: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92svh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="text-lg">Add from Salesforce</DialogTitle>
          <DialogDescription>
            Find the candidate in Salesforce and choose them. Their details, job-search profile and newest resume are
            brought into the portal. Nothing is changed in Salesforce.
          </DialogDescription>
        </DialogHeader>
        {open && (
          <Body
            teamId={teamId}
            onManual={onManual}
            onClose={() => onOpenChange(false)}
            onChosen={(candidate, how) => {
              onChosen(candidate, how);
              onOpenChange(false);
            }}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}
