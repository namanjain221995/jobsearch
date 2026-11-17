import {
  Award,
  CircleX,
  Eye,
  Inbox,
  MessagesSquare,
  PenLine,
  Send,
  SkipForward,
  Undo2,
  type LucideIcon,
} from "lucide-react";

import type { AssignmentStats, AssignmentStatus } from "./types";

/** Pipeline order. The same order is used by filters, charts and the status menu. */
export const STATUS_ORDER: AssignmentStatus[] = [
  "ASSIGNED",
  "VIEWED",
  "APPLYING",
  "APPLIED",
  "INTERVIEWING",
  "OFFER",
  "REJECTED",
  "WITHDRAWN",
  "SKIPPED",
];

export type StatusGroup = "pending" | "applied" | "closed";

export interface StatusMeta {
  label: string;
  /** CSS colour (a design token). Badges, rails and chart series all read this one value. */
  color: string;
  icon: LucideIcon;
  group: StatusGroup;
  /** What the status means, for menus and tooltips. */
  meaning: string;
}

export const STATUS_META: Record<AssignmentStatus, StatusMeta> = {
  ASSIGNED: {
    label: "Assigned",
    color: "var(--status-assigned)",
    icon: Inbox,
    group: "pending",
    meaning: "Waiting in the list. Not opened yet.",
  },
  VIEWED: {
    label: "Viewed",
    color: "var(--status-viewed)",
    icon: Eye,
    group: "pending",
    meaning: "The job details were opened. No application yet.",
  },
  APPLYING: {
    label: "Applying",
    color: "var(--status-applying)",
    icon: PenLine,
    group: "pending",
    meaning: "The application is being prepared.",
  },
  APPLIED: {
    label: "Applied",
    color: "var(--status-applied)",
    icon: Send,
    group: "applied",
    meaning: "The application was submitted and confirmed.",
  },
  INTERVIEWING: {
    label: "Interviewing",
    color: "var(--status-interviewing)",
    icon: MessagesSquare,
    group: "applied",
    meaning: "The employer has started interviews.",
  },
  OFFER: {
    label: "Offer",
    color: "var(--status-offer)",
    icon: Award,
    group: "applied",
    meaning: "The employer made an offer.",
  },
  REJECTED: {
    label: "Rejected",
    color: "var(--status-rejected)",
    icon: CircleX,
    group: "closed",
    meaning: "The employer turned the application down.",
  },
  WITHDRAWN: {
    label: "Withdrawn",
    color: "var(--status-closed)",
    icon: Undo2,
    group: "closed",
    meaning: "The application was pulled back.",
  },
  SKIPPED: {
    label: "Skipped",
    color: "var(--status-closed)",
    icon: SkipForward,
    group: "closed",
    meaning: "Decided not to apply for this job.",
  },
};

export function isAssignmentStatus(value: string): value is AssignmentStatus {
  return value in STATUS_META;
}

export function statusLabel(value: string | null | undefined): string {
  if (!value) return "";
  return isAssignmentStatus(value) ? STATUS_META[value].label : value;
}

export function statusColor(value: string): string {
  return isAssignmentStatus(value) ? STATUS_META[value].color : "var(--status-closed)";
}

/** "Applied and later": the stages that only exist once a submission was confirmed. */
export const APPLIED_STATUSES: AssignmentStatus[] = ["APPLIED", "INTERVIEWING", "OFFER", "REJECTED"];
export const PRE_APPLICATION_STATUSES: AssignmentStatus[] = ["ASSIGNED", "VIEWED", "APPLYING"];

/* ------------------------------------------------------------------ stage rail */

export const RAIL_STAGES = [
  { key: "assigned", label: "Assigned", color: "var(--status-assigned)" },
  { key: "viewed", label: "Viewed", color: "var(--status-viewed)" },
  { key: "applied", label: "Applied", color: "var(--status-applied)" },
  { key: "interview", label: "Interview", color: "var(--status-interviewing)" },
  { key: "offer", label: "Offer", color: "var(--status-offer)" },
] as const;

export interface RailState {
  /** Index of the furthest stage reached (0 = assigned). */
  reached: number;
  /** True when the application ended without an offer. */
  closed: boolean;
}

/**
 * Where an assignment sits on the five-stage rail. For closed assignments only
 * what the record proves is shown: an applied date means "Applied" was reached,
 * a viewed date means "Viewed".
 */
export function railState(input: {
  status: AssignmentStatus;
  viewed_at?: string | null;
  applied_at?: string | null;
  interviewed_at?: string | null;
}): RailState {
  switch (input.status) {
    case "ASSIGNED":
      return { reached: 0, closed: false };
    case "VIEWED":
    case "APPLYING":
      return { reached: 1, closed: false };
    case "APPLIED":
      return { reached: 2, closed: false };
    case "INTERVIEWING":
      return { reached: 3, closed: false };
    case "OFFER":
      return { reached: 4, closed: false };
    default: {
      const reached = input.interviewed_at ? 3 : input.applied_at ? 2 : input.viewed_at ? 1 : 0;
      return { reached, closed: true };
    }
  }
}

/* ------------------------------------------------------------------ metric definitions */

export type MetricKey = keyof AssignmentStats;

/** The definitions the API states for every number on a dashboard (AssignmentStats). */
export const METRIC_DEFINITIONS: Record<MetricKey, { label: string; definition: string }> = {
  assigned: { label: "Assigned", definition: "Active assignments." },
  not_applied: {
    label: "Not applied",
    definition: "Active assignments without a confirmed submission. Skipped and withdrawn jobs are left out.",
  },
  applied: {
    label: "Applied",
    definition: "Assignments with a confirmed submission (an applied date is set), whatever happened later.",
  },
  interviews: { label: "Interviews", definition: "Assignments that reached the interview stage." },
  offers: { label: "Offers", definition: "Assignments that reached the offer stage." },
  closed: { label: "Closed", definition: "Rejected, withdrawn and skipped assignments together." },
  viewed: { label: "Viewed", definition: "Assignments whose job details were opened by the candidate's responsible member." },
  link_opens: {
    label: "Job link opens",
    definition: "How many times Open Job was used. Opening a job is never counted as applying.",
  },
  completion_rate: {
    label: "Completion rate",
    definition: "Applied divided by assigned, leaving out skipped jobs and jobs withdrawn before applying.",
  },
};

export const DATE_RANGE_NOTE =
  "A date range selects assignments by the day they were assigned (UTC). Cards, tables and charts all use the same range.";
