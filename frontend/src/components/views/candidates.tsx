"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Plus } from "lucide-react";

import { AddCandidateDialog } from "@/components/candidates/candidate-dialog";
import { CANDIDATE_LIST_DEFAULTS, CandidateTable } from "@/components/candidates/candidate-table";
import { PageHeader } from "@/components/shared/page-header";
import { ErrorState } from "@/components/shared/states";
import { useArea, useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { useUrlState } from "@/lib/url-state";

/** The Add Candidate button with its dialog. A new candidate opens on their own page, where the profile is filled in. */
function useAddCandidate(teamId?: string) {
  const area = useArea();
  const router = useRouter();
  const [adding, setAdding] = useState(false);
  const button = (
    <Button onClick={() => setAdding(true)}>
      <Plus data-icon="inline-start" />
      Add Candidate
    </Button>
  );
  const dialog = (
    <AddCandidateDialog
      open={adding}
      onOpenChange={setAdding}
      teamId={teamId}
      onCreated={(candidate) => router.push(`${area}/candidates/${candidate.id}?tab=profile`)}
    />
  );
  return { button, dialog };
}

/** Team Lead: the candidates of their own team. */
export function TeamCandidatesView() {
  const me = useMe();
  const listState = useUrlState(CANDIDATE_LIST_DEFAULTS);
  const add = useAddCandidate();
  return (
    <>
      <PageHeader
        title="Candidates"
        description="The people your team is finding jobs for. Add a candidate, upload the resume, choose the member who handles their jobs, then run a search for them."
        actions={me.team ? add.button : undefined}
      />
      {me.team ? (
        <CandidateTable listState={listState} emptyAction={add.button} />
      ) : (
        <div className="rounded-lg border bg-card">
          <ErrorState error={new Error("You are not assigned to a team yet. Ask an Admin to make you the Team Lead of a team.")} />
        </div>
      )}
      {add.dialog}
    </>
  );
}

/** Admin: every candidate, across teams. */
export function AdminCandidatesView() {
  const listState = useUrlState(CANDIDATE_LIST_DEFAULTS);
  const add = useAddCandidate(listState[0].team || undefined);
  return (
    <>
      <PageHeader
        title="Candidates"
        description="The people the teams are finding jobs for, in every team: who is responsible for each, whether the resume is in, and how far their jobs have got."
        actions={add.button}
      />
      <CandidateTable listState={listState} emptyAction={add.button} />
      {add.dialog}
    </>
  );
}
