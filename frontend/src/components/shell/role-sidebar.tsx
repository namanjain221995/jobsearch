"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { Fragment, Suspense } from "react";

import { BrandMark } from "@/components/brand";
import { useMe } from "@/components/shell/me-context";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  SidebarSeparator,
  useSidebar,
} from "@/components/ui/sidebar";
import { roleLabel } from "@/lib/format";
import { activeNavItem, NAVIGATION, type NavItem } from "@/lib/nav";
import { useMemberDashboard, useTeam } from "@/lib/queries";
import { cn } from "@/lib/utils";

/** Live counts beside a few entries: jobs waiting in the team queue, jobs a member has not applied to yet. */
function useNavBadges(): Record<NonNullable<NavItem["badge"]>, number | undefined> {
  const me = useMe();
  const team = useTeam(me.role === "TEAM_LEAD" ? me.team?.id : undefined, { refetchInterval: 25_000 });
  const dashboard = useMemberDashboard(me.role === "MEMBER" ? me.id : undefined);
  return {
    "team-queue": team.data?.queue_pending,
    "my-open-jobs": dashboard.data?.stats.not_applied,
  };
}

function NavList({ activeHref }: { activeHref: string | null }) {
  const me = useMe();
  const badges = useNavBadges();
  const { isMobile, setOpenMobile } = useSidebar();
  const groups = NAVIGATION[me.role];

  return (
    <>
      {groups.map((group, index) => (
        <Fragment key={group.label ?? index}>
          {index > 0 && !group.label && <SidebarSeparator />}
          <SidebarGroup>
            {group.label && <SidebarGroupLabel>{group.label}</SidebarGroupLabel>}
            <SidebarGroupContent>
              <SidebarMenu className="gap-0.5">
                {group.items.map((item) => {
                  const active = item.href === activeHref;
                  const count = item.badge ? badges[item.badge] : undefined;
                  return (
                    <SidebarMenuItem key={item.href}>
                      <SidebarMenuButton
                        asChild
                        isActive={active}
                        tooltip={item.label}
                        className={cn(
                          "relative text-sidebar-foreground/80 data-active:text-sidebar-foreground",
                          // The active entry carries a short ink rail, the same mark the pipeline uses.
                          "data-active:before:absolute data-active:before:inset-y-1.5 data-active:before:left-0 data-active:before:w-[3px] data-active:before:rounded-full data-active:before:bg-sidebar-primary",
                        )}
                      >
                        <Link
                          href={item.href}
                          aria-current={active ? "page" : undefined}
                          onClick={() => {
                            if (isMobile) setOpenMobile(false);
                          }}
                        >
                          <item.icon aria-hidden="true" />
                          <span>{item.label}</span>
                        </Link>
                      </SidebarMenuButton>
                      {count !== undefined && count > 0 && (
                        <SidebarMenuBadge
                          className="bg-sidebar-primary/12 text-sidebar-foreground"
                          aria-label={`${count} waiting`}
                        >
                          {count > 99 ? "99+" : count}
                        </SidebarMenuBadge>
                      )}
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </Fragment>
      ))}
    </>
  );
}

/** Reads the URL, so it lives behind its own Suspense boundary (see RoleSidebar). */
function ActiveNavList() {
  const me = useMe();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const active = activeNavItem(me.role, pathname, new URLSearchParams(searchParams.toString()));
  return <NavList activeHref={active?.href ?? null} />;
}

export function RoleSidebar() {
  const me = useMe();
  return (
    <Sidebar collapsible="icon" aria-label="Main navigation">
      <SidebarHeader className="h-13 justify-center border-b border-sidebar-border px-3">
        <Link
          href={me.home}
          className="flex items-center gap-2.5 rounded-sm group-data-[collapsible=icon]:justify-center"
          aria-label="AI Job Finder, go to dashboard"
        >
          <BrandMark />
          <span className="font-display text-lg leading-none whitespace-nowrap group-data-[collapsible=icon]:hidden">
            AI Job Finder
          </span>
        </Link>
      </SidebarHeader>
      <SidebarContent>
        <div className="px-4 pt-4 pb-1 group-data-[collapsible=icon]:hidden">
          <p className="truncate font-label text-sm" title={me.team?.name ?? "All teams"}>
            {me.team?.name ?? (me.role === "ADMIN" ? "All teams" : "No team yet")}
          </p>
          <p className="text-xs text-muted-foreground">{roleLabel(me.role)}</p>
        </div>
        <nav aria-label="Sections">
          <Suspense fallback={<NavList activeHref={null} />}>
            <ActiveNavList />
          </Suspense>
        </nav>
      </SidebarContent>
      <SidebarRail />
    </Sidebar>
  );
}
