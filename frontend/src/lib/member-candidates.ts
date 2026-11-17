/** Where the member area's pages live. */
export const memberCandidateHref = (id: string) => `/member/candidates/${id}`;

/**
 * A member's job page. Links on a candidate's page pass `fromCandidate`: the job page reads
 * `?candidate=<id>` to lead back to that candidate even when the job cannot be shown (withdrawn, 410).
 */
export const memberJobHref = (assignmentId: string, fromCandidate?: string) =>
  fromCandidate ? `/member/jobs/${assignmentId}?candidate=${fromCandidate}` : `/member/jobs/${assignmentId}`;
