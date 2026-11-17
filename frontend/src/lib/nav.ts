import {
  Activity,
  BriefcaseBusiness,
  ChartColumn,
  ClipboardList,
  Contact,
  History,
  Inbox,
  LayoutDashboard,
  MailPlus,
  ScrollText,
  Search,
  SendHorizontal,
  Settings,
  UserCog,
  Users,
  UsersRound,
  type LucideIcon,
} from "lucide-react";

import type { Role } from "./types";

export const ROLE_AREA: Record<Role, string> = { ADMIN: "/admin", TEAM_LEAD: "/tl", MEMBER: "/member" };

export interface NavItem {
  label: string;
  /** Path, optionally with a query string that selects a filter on that screen. */
  href: string;
  icon: LucideIcon;
  /**
   * When two entries share a path, the query parameters that make this one the
   * current page ("applied=true"), or the ones that must be absent ("!applied").
   */
  when?: string;
  /** A live count shown beside the label. */
  badge?: "team-queue" | "my-open-jobs";
}

export interface NavGroup {
  /** Shown above the group. Left out when the group needs no name. */
  label?: string;
  items: NavItem[];
}

export const NAVIGATION: Record<Role, NavGroup[]> = {
  ADMIN: [
    {
      items: [
        { label: "Dashboard", href: "/admin/dashboard", icon: LayoutDashboard },
        { label: "Analytics", href: "/admin/analytics", icon: ChartColumn },
      ],
    },
    {
      label: "People",
      items: [
        { label: "Teams", href: "/admin/teams", icon: UsersRound },
        { label: "Team Leads", href: "/admin/team-leads", icon: UserCog },
        { label: "Members", href: "/admin/members", icon: Users },
        { label: "Candidates", href: "/admin/candidates", icon: Contact },
        { label: "Invitations", href: "/admin/invitations", icon: MailPlus },
      ],
    },
    {
      label: "Jobs",
      items: [
        { label: "Jobs", href: "/admin/jobs", icon: BriefcaseBusiness },
        { label: "Find Jobs", href: "/admin/search", icon: Search },
        { label: "Search History", href: "/admin/searches", icon: History },
        { label: "Assignments", href: "/admin/assignments", icon: ClipboardList },
      ],
    },
    {
      label: "System",
      items: [
        { label: "Activity Log", href: "/admin/activity", icon: ScrollText },
        { label: "Settings", href: "/admin/settings", icon: Settings },
      ],
    },
  ],
  /*
   * The team works for its candidates: the people who need a job. Members are the
   * employees who apply on a candidate's behalf.
   * A job reaches a candidate in three steps, and the middle of this menu follows them:
   * Pending Jobs  - delegated by the Admin, waiting in the team queue to be handed out;
   * Assigned Jobs - every job a candidate of the team has been given;
   * Applications  - the assigned jobs that have been applied to.
   */
  TEAM_LEAD: [
    {
      items: [
        { label: "Dashboard", href: "/tl/dashboard", icon: LayoutDashboard },
        { label: "Candidates", href: "/tl/candidates", icon: Contact },
        { label: "Team Members", href: "/tl/members", icon: Users },
      ],
    },
    {
      items: [
        { label: "Find Jobs", href: "/tl/search", icon: Search },
        { label: "Assigned Jobs", href: "/tl/assignments", icon: ClipboardList, when: "!applied" },
        { label: "Pending Jobs", href: "/tl/jobs", icon: Inbox, badge: "team-queue" },
        { label: "Applications", href: "/tl/assignments?applied=true", icon: SendHorizontal, when: "applied=true" },
        { label: "Search History", href: "/tl/searches", icon: History },
      ],
    },
    {
      items: [
        { label: "Analytics", href: "/tl/analytics", icon: ChartColumn },
        { label: "Invitations", href: "/tl/invitations", icon: MailPlus },
        { label: "Settings", href: "/tl/settings", icon: Settings },
      ],
    },
  ],
  MEMBER: [
    {
      items: [
        { label: "Dashboard", href: "/member/dashboard", icon: LayoutDashboard },
        { label: "My Candidates", href: "/member/candidates", icon: Contact },
        { label: "All Jobs", href: "/member/jobs", icon: BriefcaseBusiness, badge: "my-open-jobs" },
        { label: "Applications", href: "/member/applications", icon: SendHorizontal },
        { label: "Activity", href: "/member/activity", icon: Activity },
      ],
    },
    {
      items: [{ label: "Settings", href: "/member/settings", icon: Settings }],
    },
  ],
};

export function pathOf(href: string): string {
  const index = href.indexOf("?");
  return index === -1 ? href : href.slice(0, index);
}

function conditionHolds(when: string | undefined, search: URLSearchParams): boolean {
  if (!when) return true;
  if (when.startsWith("!")) return !search.has(when.slice(1));
  const [key, value] = when.split("=");
  return search.get(key) === value;
}

/** The entry for the current URL: the longest matching path whose query condition holds. */
export function activeNavItem(role: Role, pathname: string, search: URLSearchParams): NavItem | null {
  let best: NavItem | null = null;
  for (const group of NAVIGATION[role]) {
    for (const item of group.items) {
      const path = pathOf(item.href);
      const matches = pathname === path || pathname.startsWith(`${path}/`);
      if (!matches || !conditionHolds(item.when, search)) continue;
      if (!best || path.length > pathOf(best.href).length) best = item;
    }
  }
  return best;
}

/** Where a person's own account settings live. */
export const ACCOUNT_PATH: Record<Role, string> = {
  ADMIN: "/admin/settings",
  TEAM_LEAD: "/tl/settings",
  MEMBER: "/member/settings",
};
