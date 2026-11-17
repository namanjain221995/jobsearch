"use client";

import Link from "next/link";
import { useState } from "react";
import {
  Activity,
  ArrowRightLeft,
  CircleDashed,
  ExternalLink,
  Eye,
  Inbox,
  RotateCcw,
  StickyNote,
  Undo2,
  type LucideIcon,
} from "lucide-react";

import { Pagination } from "@/components/shared/pagination";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { useMe } from "@/components/shell/me-context";
import { useNow } from "@/hooks/use-now";
import { formatDateTime, formatRelative, humanize } from "@/lib/format";
import { useCandidateActivity, useMemberActivity } from "@/lib/queries";
import { statusLabel } from "@/lib/status";
import type { ActivityItem } from "@/lib/types";
import { cn } from "@/lib/utils";

function describe(item: ActivityItem, own: boolean, viewerId: string): { icon: LucideIcon; text: string } {
  const actor = item.actor?.name ?? "Someone";
  // On a member's own page their own actions read "You ..."; everybody else is named.
  const subject = own && item.actor !== null && item.actor?.id === viewerId ? "You" : actor;
  switch (item.event_type) {
    case "assigned":
      return { icon: Inbox, text: `${actor} assigned this job` };
    case "reassigned":
      return { icon: RotateCcw, text: `${actor} assigned this job again` };
    case "viewed":
      return { icon: Eye, text: `${subject} viewed the job details` };
    case "link_opened":
      return { icon: ExternalLink, text: `${subject} opened the job posting` };
    case "link_opened_by_manager":
      return { icon: ExternalLink, text: `${actor} opened the job posting (manager)` };
    case "status_changed":
      return {
        icon: ArrowRightLeft,
        text:
          item.old_status === item.new_status
            ? `${subject} updated the ${statusLabel(item.new_status)} details`
            : `${subject} changed the status from ${statusLabel(item.old_status) || "new"} to ${statusLabel(item.new_status)}`,
      };
    case "note_added":
      return { icon: StickyNote, text: `${subject} added a note` };
    case "unassigned":
      return { icon: Undo2, text: `${actor} withdrew the assignment` };
    default:
      return { icon: CircleDashed, text: humanize(item.event_type) };
  }
}

/**
 * Activity on jobs, newest first: of one candidate (GET /api/candidates/{id}/activity) or of
 * all candidates a member is responsible for today (GET /api/users/{id}/activity).
 * `jobHref` turns each entry's job into a link (a member's job page); `onOpenAssignment` opens a manager's drawer.
 */
export function ActivityFeed({
  memberId,
  candidateId,
  own = false,
  showCandidate = !candidateId,
  jobHref,
  onOpenAssignment,
  pageSize = 25,
  className,
}: {
  /** The responsible member whose candidates' activity is shown. Give this or `candidateId`. */
  memberId?: string;
  candidateId?: string;
  /** True on the member's own pages ("You opened ..."). */
  own?: boolean;
  /** Name the candidate on every entry. On by default for a member's feed, which spans several candidates. */
  showCandidate?: boolean;
  jobHref?: (assignmentId: string, candidateId: string) => string;
  onOpenAssignment?: (assignmentId: string) => void;
  pageSize?: number;
  className?: string;
}) {
  const now = useNow();
  const me = useMe();
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(pageSize);
  const ofCandidate = useCandidateActivity(candidateId, { page, page_size: size });
  const ofMember = useMemberActivity(candidateId ? undefined : memberId, { page, page_size: size });
  const activity = candidateId ? ofCandidate : ofMember;

  if (activity.isPending) return <LoadingSkeleton variant="list" count={6} className={className} />;
  if (activity.isError) return <ErrorState compact error={activity.error} onRetry={() => activity.refetch()} className={className} />;
  const { items, total } = activity.data;
  if (items.length === 0) {
    return (
      <EmptyState
        compact
        icon={Activity}
        title="No activity yet"
        description="Viewing a job, opening its posting, applying and every status change are recorded here."
        className={className}
      />
    );
  }

  return (
    <div className={cn("flex flex-col", className)}>
      <ol className={cn("flex flex-col divide-y", activity.isPlaceholderData && "opacity-60 transition-opacity")} aria-label="Activity, newest first">
        {items.map((item) => {
          const view = describe(item, own, me.id);
          const title = `${item.job_title}, ${item.company}`;
          const forCandidate = showCandidate ? `For ${item.candidate.name}` : null;
          return (
            <li key={item.id} className="flex items-start gap-3 py-3 first:pt-0">
              <span aria-hidden="true" className="mt-0.5 grid size-6 shrink-0 place-items-center rounded-full border bg-card text-muted-foreground">
                <view.icon className="size-3" />
              </span>
              <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                <p className="text-sm">{view.text}</p>
                {jobHref ? (
                  <Link href={jobHref(item.assignment_id, item.candidate.id)} className="w-fit max-w-full truncate rounded-sm text-2sm text-primary underline-offset-4 hover:underline">
                    {title}
                  </Link>
                ) : onOpenAssignment ? (
                  <button
                    type="button"
                    onClick={() => onOpenAssignment(item.assignment_id)}
                    className="w-fit max-w-full truncate rounded-sm text-left text-2sm text-primary underline-offset-4 hover:underline"
                  >
                    {title}
                  </button>
                ) : (
                  <p className="truncate text-2sm text-muted-foreground">{title}</p>
                )}
                {forCandidate && <p className="truncate text-xs text-muted-foreground">{forCandidate}</p>}
              </div>
              <time dateTime={item.created_at} title={formatDateTime(item.created_at)} className="shrink-0 pt-0.5 text-xs text-muted-foreground">
                {formatRelative(item.created_at, now)}
              </time>
            </li>
          );
        })}
      </ol>
      <Pagination
        className="mt-2 border-t pt-3"
        noun="events"
        page={activity.data.page}
        pageSize={activity.data.page_size}
        total={total}
        onPageChange={setPage}
        onPageSizeChange={(next) => {
          setSize(next);
          setPage(1);
        }}
      />
    </div>
  );
}
