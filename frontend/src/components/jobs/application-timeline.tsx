"use client";

import {
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

import { EmptyState } from "@/components/shared/states";
import { useNow } from "@/hooks/use-now";
import { formatDateTime, formatRelative, humanize } from "@/lib/format";
import { statusLabel } from "@/lib/status";
import type { AssignmentEvent } from "@/lib/types";
import { SOURCE_LABELS } from "@/components/jobs/job-meta";

interface Described {
  icon: LucideIcon;
  text: string;
  notes: string[];
}

function describe(event: AssignmentEvent): Described {
  const who = event.actor?.name ?? "Someone";
  const meta = event.metadata as Record<string, unknown>;
  const notes: string[] = [];
  switch (event.event_type) {
    case "assigned": {
      const source = typeof meta.source === "string" ? SOURCE_LABELS[meta.source] : undefined;
      if (source) notes.push(source);
      return { icon: Inbox, text: `Assigned by ${who}`, notes };
    }
    case "reassigned":
      return { icon: RotateCcw, text: `Assigned again by ${who}`, notes };
    case "viewed":
      return { icon: Eye, text: `Job details viewed by ${who}`, notes };
    case "link_opened":
      notes.push("Opening the posting is not an application.");
      return { icon: ExternalLink, text: `Job posting opened by ${who}`, notes };
    case "link_opened_by_manager":
      notes.push("Opened by a manager. Not counted as a job link open.");
      return { icon: ExternalLink, text: `Job posting opened by ${who}`, notes };
    case "status_changed": {
      if (meta.correction) notes.push("Correction: later dates were cleared.");
      if (meta.on_behalf_of_member) notes.push(`Recorded by ${who} in place of the responsible member.`);
      if (meta.applied_at_inferred) notes.push("No earlier application was recorded, so the applied date was set to this moment.");
      if (meta.reference_recorded) notes.push("A reference number was saved.");
      if (typeof meta.previous_applied_at === "string" && !meta.correction) {
        notes.push(`Applied date changed (was ${formatDateTime(meta.previous_applied_at)}).`);
      }
      const from = statusLabel(event.old_status);
      const to = statusLabel(event.new_status);
      const text =
        event.old_status === event.new_status
          ? `${to} details updated by ${who}`
          : `Status changed from ${from || "new"} to ${to} by ${who}`;
      return { icon: ArrowRightLeft, text, notes };
    }
    case "note_added":
      if (meta.private) notes.push("Private note, hidden from the responsible member.");
      return { icon: StickyNote, text: `Note added by ${who}`, notes };
    case "unassigned":
      return { icon: Undo2, text: `Removed from the candidate's list by ${who}`, notes };
    default:
      return { icon: CircleDashed, text: `${humanize(event.event_type)} (${who})`, notes };
  }
}

/** Everything that happened to one assignment, newest first, exactly as the API recorded it. */
export function ApplicationTimeline({ events }: { events: AssignmentEvent[] }) {
  const now = useNow();
  if (events.length === 0) {
    return <EmptyState compact icon={CircleDashed} title="No history yet" description="Every change to this assignment is listed here." />;
  }
  return (
    <ol className="relative flex flex-col" aria-label="Assignment history, newest first">
      {events.map((event, index) => {
        const view = describe(event);
        return (
          <li key={event.id} className="relative flex gap-3 pb-4 last:pb-0">
            {index < events.length - 1 && (
              <span aria-hidden="true" className="absolute top-6 bottom-0 left-[0.6875rem] w-px bg-border" />
            )}
            <span
              aria-hidden="true"
              className="relative z-[1] grid size-[1.4375rem] shrink-0 place-items-center rounded-full border bg-card text-muted-foreground"
            >
              <view.icon className="size-3" />
            </span>
            <div className="flex min-w-0 flex-col gap-0.5 pt-px">
              <p className="text-sm">{view.text}</p>
              {view.notes.map((note) => (
                <p key={note} className="text-xs text-muted-foreground">
                  {note}
                </p>
              ))}
              <time dateTime={event.created_at} className="text-xs text-muted-foreground" title={formatDateTime(event.created_at)}>
                {formatDateTime(event.created_at)}
                {now !== null && <span> ({formatRelative(event.created_at, now)})</span>}
              </time>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
