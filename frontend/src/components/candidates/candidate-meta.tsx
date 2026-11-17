import Link from "next/link";
import { FileCheck2, FileWarning } from "lucide-react";

import { Tag } from "@/components/shared/status-badge";
import type { AssignmentStats, Candidate, UserRef } from "@/lib/types";
import { cn } from "@/lib/utils";

export const EMPTY_STATS: AssignmentStats = {
  assigned: 0,
  not_applied: 0,
  applied: 0,
  interviews: 0,
  offers: 0,
  closed: 0,
  viewed: 0,
  link_opens: 0,
  completion_rate: 0,
};

/** Whether a candidate's resume is in. Searches match jobs against it, so a missing one is a warning. */
export function ResumeTag({ hasResume, short = false }: { hasResume: boolean; short?: boolean }) {
  return hasResume ? (
    <Tag tone="success">
      <FileCheck2 aria-hidden="true" />
      {short ? "Resume" : "Resume uploaded"}
    </Tag>
  ) : (
    <Tag tone="warning">
      <FileWarning aria-hidden="true" />
      No resume
    </Tag>
  );
}

/** Shown beside the name of a candidate nobody is working for any more. */
export function CandidateInactiveTag({ candidate }: { candidate: Pick<Candidate, "is_active"> }) {
  return candidate.is_active ? null : <Tag>Deactivated</Tag>;
}

/**
 * The member who applies on the candidate's behalf today. A candidate without one is
 * "Unassigned": its jobs are waiting for somebody to take them.
 */
export function ResponsibleMember({
  member,
  href,
  className,
}: {
  member: UserRef | null | undefined;
  /** Makes the name a link to the member's page (managers). */
  href?: string;
  className?: string;
}) {
  if (!member) return <span className={cn("text-warning", className)}>Unassigned</span>;
  if (href) {
    return (
      <Link href={href} className={cn("rounded-sm underline-offset-4 hover:underline", className)}>
        {member.name}
      </Link>
    );
  }
  return <span className={className}>{member.name}</span>;
}

/** "Jordan Lee" or "nobody yet", for sentences. */
export const responsibleName = (member: UserRef | null | undefined): string => member?.name ?? "nobody yet";
