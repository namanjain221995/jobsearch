"use client";

import { useAddCandidate } from "@/components/candidates/add-candidate";
import { CANDIDATE_LIST_DEFAULTS, CandidateTable } from "@/components/candidates/candidate-table";
import { PageHeader } from "@/components/shared/page-header";
import { ErrorState } from "@/components/shared/states";
import { useMe } from "@/components/shell/me-context";
import { useUrlState } from "@/lib/url-state";

/** Team Lead: the candidates of their own team. */
export function TeamCandidatesView() {
  const me = useMe();
  const listState = useUrlState(CANDIDATE_LIST_DEFAULTS);
  const add = useAddCandidate();
  return (
    <>
      <PageHeader
        title="Candidates"
        description={
          add.salesforce
            ? "The people your team is finding jobs for: the candidates brought in from Salesforce and worked on here. Choose the member who handles their jobs, then run a search for them."
            : "The people your team is finding jobs for. Add a candidate, upload the resume, choose the member who handles their jobs, then run a search for them."
        }
        actions={me.team ? add.buttons : undefined}
      />
      {me.team ? (
        <CandidateTable listState={listState} emptyAction={add.buttons} />
      ) : (
        <div className="rounded-lg border bg-card">
          <ErrorState error={new Error("You are not assigned to a team yet. Ask an Admin to make you the Team Lead of a team.")} />
        </div>
      )}
      {add.dialogs}
    </>
  );
}

/** Admin: every candidate, across teams. */
export function AdminCandidatesView() {
  const listState = useUrlState(CANDIDATE_LIST_DEFAULTS);
  const add = useAddCandidate({ teamId: listState[0].team || undefined });
  return (
    <>
      <PageHeader
        title="Candidates"
        description="The people the teams are finding jobs for, in every team: who is responsible for each, whether the resume is in, and how far their jobs have got."
        actions={add.buttons}
      />
      <CandidateTable listState={listState} emptyAction={add.buttons} />
      {add.dialogs}
    </>
  );
}
