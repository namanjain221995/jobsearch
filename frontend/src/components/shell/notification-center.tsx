"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Bell, CheckCheck, Contact, FolderInput, Inbox, SearchCheck, UserCheck, UserRoundX, type LucideIcon } from "lucide-react";
import { toast } from "sonner";

import { useMe } from "@/components/shell/me-context";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useNow } from "@/hooks/use-now";
import { errorMessage } from "@/lib/api";
import { formatRelative, humanize, plural, roleLabel } from "@/lib/format";
import { ROLE_AREA } from "@/lib/nav";
import {
  invalidateWork,
  useNotifications,
  useReadAllNotifications,
  useReadNotification,
  useUnreadCount,
} from "@/lib/queries";
import type { Notification, Role } from "@/lib/types";
import { cn } from "@/lib/utils";

const PAGE_STEP = 20;
const MAX_PAGE_SIZE = 200;

interface Presented {
  icon: LucideIcon;
  text: string;
  href: string | null;
}

const text = (value: unknown, fallback = ""): string => (typeof value === "string" && value ? value : fallback);
const count = (value: unknown): number => (typeof value === "number" && Number.isFinite(value) ? value : 0);

/** Turns a stored notification into a sentence and the screen it points to. Unknown types still render. */
function present(notification: Notification, role: Role): Presented {
  const area = ROLE_AREA[role];
  const payload = notification.payload as Record<string, unknown>;
  switch (notification.type) {
    case "candidate_assigned": {
      // A candidate was handed to this member: from now on they apply on the candidate's behalf.
      const id = text(payload.candidate_id);
      return {
        icon: Contact,
        text: `${text(payload.assigned_by, "Your Team Lead")} made you responsible for the candidate ${text(payload.candidate_name, "(name not given)")}.`,
        href: id ? `${area}/candidates/${id}` : `${area}/candidates`,
      };
    }
    case "candidate_jobs_assigned": {
      const id = text(payload.candidate_id);
      const jobs = plural(count(payload.count), "new job");
      const candidate = text(payload.candidate_name, "one of your candidates");
      return {
        icon: Inbox,
        text:
          text(payload.source) === "search"
            ? `A search found ${jobs} for ${candidate}.`
            : `${text(payload.assigned_by, "Your Team Lead")} assigned ${jobs} to ${candidate}.`,
        href: id ? `${area}/candidates/${id}` : `${area}/candidates`,
      };
    }
    case "candidates_unassigned": {
      const reason = text(payload.reason);
      const why =
        reason === "member_deactivated"
          ? " was deactivated"
          : reason === "member_moved_to_another_team"
            ? " was moved to another team"
            : " no longer handles them";
      const teamId = text(payload.team_id);
      return {
        icon: UserRoundX,
        text: `${plural(count(payload.count), "candidate")} ${count(payload.count) === 1 ? "has" : "have"} no responsible member any more: ${text(payload.member_name, "their member")}${why}. Choose who takes over.`,
        href:
          role === "TEAM_LEAD"
            ? "/tl/candidates?member=unassigned"
            : role === "ADMIN"
              ? `/admin/candidates?${teamId ? `team=${teamId}&` : ""}member=unassigned`
              : null,
      };
    }
    case "jobs_assigned":
      // From before candidates existed: the old payload names no candidate.
      return {
        icon: Inbox,
        text: `${plural(count(payload.count), "job")} assigned.`,
        href: role === "MEMBER" ? "/member/jobs" : `${area}/assignments`,
      };
    case "jobs_delegated":
      return {
        icon: FolderInput,
        text: `${text(payload.delegated_by, "An Admin")} sent ${plural(count(payload.count), "job")} to your team's queue.`,
        href: role === "TEAM_LEAD" ? "/tl/jobs" : null,
      };
    case "invitation_accepted": {
      const invitedRole = text(payload.role);
      const userId = text(payload.user_id);
      const href =
        role === "MEMBER"
          ? null
          : invitedRole === "MEMBER" && userId
            ? `${area}/members/${userId}`
            : role === "ADMIN"
              ? "/admin/team-leads"
              : `${area}/members`;
      return {
        icon: UserCheck,
        text: `${text(payload.name, "Someone you invited")} accepted the invitation${invitedRole ? ` as ${roleLabel(invitedRole)}` : ""}.`,
        href,
      };
    }
    case "search_finished": {
      const id = text(payload.search_run_id);
      const status = text(payload.status, "finished");
      return {
        icon: SearchCheck,
        text: `Search ${status}: ${plural(count(payload.matched), "job")} matched, ${count(payload.assigned).toLocaleString()} assigned${count(payload.candidates) > 0 ? `, for ${plural(count(payload.candidates), "candidate")}` : ""}.`,
        href: role !== "MEMBER" && id ? `${area}/searches/${id}` : null,
      };
    }
    default:
      return { icon: Bell, text: humanize(notification.type), href: null };
  }
}

function NotificationList({ onNavigate }: { onNavigate: () => void }) {
  const me = useMe();
  const router = useRouter();
  const now = useNow();
  const [pageSize, setPageSize] = useState(PAGE_STEP);
  const list = useNotifications(pageSize, true);
  const read = useReadNotification();
  const readAll = useReadAllNotifications();

  if (list.isPending) return <LoadingSkeleton variant="list" count={4} className="p-3" />;
  if (list.isError) return <ErrorState compact error={list.error} onRetry={() => list.refetch()} />;

  const { items, total } = list.data;
  const unread = items.filter((item) => !item.read_at).length;

  if (items.length === 0) {
    return (
      <EmptyState
        compact
        icon={Bell}
        title="No notifications yet"
        description="Candidates assigned to you, new jobs for them, finished searches and accepted invitations show up here."
      />
    );
  }

  return (
    <>
      <div className="flex items-center justify-between gap-3 border-b px-3 py-2">
        <p className="font-label text-sm">Notifications</p>
        <Button
          variant="ghost"
          size="xs"
          disabled={unread === 0 || readAll.isPending}
          onClick={() => readAll.mutate(undefined, { onError: (error) => toast.error(errorMessage(error)) })}
        >
          <CheckCheck data-icon="inline-start" />
          Mark all as read
        </Button>
      </div>
      <ul className="max-h-[min(26rem,60svh)] overflow-y-auto overscroll-contain">
        {items.map((notification) => {
          const view = present(notification, me.role);
          const isUnread = !notification.read_at;
          return (
            <li key={notification.id} className="border-b last:border-b-0">
              <button
                type="button"
                className={cn(
                  "flex w-full items-start gap-3 px-3 py-2.5 text-left hover:bg-accent focus-visible:bg-accent focus-visible:outline-none",
                  isUnread && "bg-accent/50",
                )}
                onClick={() => {
                  if (isUnread) read.mutate(notification.id);
                  if (view.href) {
                    onNavigate();
                    router.push(view.href);
                  }
                }}
              >
                <view.icon
                  className={cn("mt-0.5 size-4 shrink-0", isUnread ? "text-primary" : "text-muted-foreground")}
                  aria-hidden="true"
                />
                <span className="min-w-0 flex-1">
                  <span className={cn("block text-sm", isUnread ? "text-foreground" : "text-muted-foreground")}>
                    {view.text}
                  </span>
                  <span className="mt-0.5 block text-xs text-muted-foreground">
                    {formatRelative(notification.created_at, now)}
                    {isUnread && <span className="sr-only">, unread</span>}
                  </span>
                </span>
                {isUnread && <span className="mt-1.5 size-2 shrink-0 rounded-full bg-primary" aria-hidden="true" />}
              </button>
            </li>
          );
        })}
      </ul>
      {total > items.length && pageSize < MAX_PAGE_SIZE && (
        <div className="border-t p-2">
          <Button
            variant="ghost"
            size="sm"
            className="w-full"
            disabled={list.isFetching}
            onClick={() => setPageSize((size) => Math.min(MAX_PAGE_SIZE, size + PAGE_STEP))}
          >
            Show older ({(total - items.length).toLocaleString()} more)
          </Button>
        </div>
      )}
    </>
  );
}

export function NotificationCenter() {
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const unread = useUnreadCount();
  const unreadCount = unread.data ?? 0;

  // A new notification means something changed for this person (a candidate or jobs assigned, a search finished):
  // refresh the lists and dashboards right away instead of waiting for their next poll.
  const previous = useRef<number | null>(null);
  useEffect(() => {
    if (unread.data === undefined) return;
    if (previous.current !== null && unread.data > previous.current) void invalidateWork(client);
    previous.current = unread.data;
  }, [unread.data, client]);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="relative"
          aria-label={unreadCount > 0 ? `Notifications, ${unreadCount} unread` : "Notifications"}
        >
          <Bell />
          {unreadCount > 0 && (
            <span
              aria-hidden="true"
              className="absolute -top-0.5 -right-0.5 grid h-4 min-w-4 place-items-center rounded-full bg-primary px-1 text-[0.625rem] leading-none font-semibold text-primary-foreground tabular-nums"
            >
              {unreadCount > 99 ? "99+" : unreadCount}
            </span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[min(24rem,calc(100vw-1.5rem))] gap-0 p-0">
        {open && <NotificationList onNavigate={() => setOpen(false)} />}
      </PopoverContent>
    </Popover>
  );
}
