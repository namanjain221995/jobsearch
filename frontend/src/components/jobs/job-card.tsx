import Link from "next/link";
import type { ReactNode } from "react";

import { JobFactLine, MatchScore } from "@/components/jobs/job-meta";
import { ApplicationStatusBadge, StageRail } from "@/components/shared/status-badge";
import { formatDate } from "@/lib/format";
import type { Assignment } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * One assigned job as a card: what it is, how well it fits, and how far the
 * application has travelled. The whole card opens the job; the posting's own
 * address is never shown here.
 */
export function JobCard({
  assignment,
  href,
  footer,
  className,
}: {
  assignment: Assignment;
  href: string;
  /** Replaces the default "Assigned on ..." line. */
  footer?: ReactNode;
  className?: string;
}) {
  const { job } = assignment;
  return (
    <article
      data-slot="job-card"
      className={cn(
        "group relative flex h-full flex-col gap-3 rounded-lg border bg-card p-4 transition-colors hover:border-border-strong has-[a:focus-visible]:border-ring has-[a:focus-visible]:ring-3 has-[a:focus-visible]:ring-ring/40",
        className,
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <p className="truncate text-2sm text-muted-foreground">{job.company}</p>
          <h3 className="font-label text-md leading-snug">
            <Link href={href} className="outline-none after:absolute after:inset-0 after:rounded-lg">
              <span className="line-clamp-2">{job.title}</span>
            </Link>
          </h3>
        </div>
        <MatchScore score={assignment.match_score} className="shrink-0 pt-0.5" />
      </div>
      <JobFactLine job={job} />
      <div className="mt-auto flex flex-col gap-2.5 pt-1">
        <StageRail status={assignment.status} viewedAt={assignment.viewed_at} appliedAt={assignment.applied_at} />
        <div className="flex flex-wrap items-center justify-between gap-2">
          <ApplicationStatusBadge status={assignment.status} />
          <span className="text-xs text-muted-foreground">
            {footer ??
              (assignment.applied_at
                ? `Applied ${formatDate(assignment.applied_at)}`
                : `Assigned ${formatDate(assignment.assigned_at)}`)}
          </span>
        </div>
      </div>
    </article>
  );
}
