"use client";

import { usePathname, useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { useMe } from "@/components/shell/me-context";
import { NotificationCenter } from "@/components/shell/notification-center";
import { ProfileMenu } from "@/components/shell/profile-menu";
import { ThemeToggle } from "@/components/shell/theme-toggle";
import { Separator } from "@/components/ui/separator";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { Skeleton } from "@/components/ui/skeleton";
import { activeNavItem } from "@/lib/nav";

/** Names the section the person is in. Reads the URL and the signed-in user, so it waits behind Suspense. */
function SectionName() {
  const me = useMe();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const active = activeNavItem(me.role, pathname, new URLSearchParams(searchParams.toString()));
  return <span className="truncate font-label text-sm text-muted-foreground">{active?.label ?? ""}</span>;
}

export function Header() {
  return (
    <header className="sticky top-0 z-20 flex h-13 shrink-0 items-center gap-2 border-b bg-background/92 px-3 backdrop-blur-sm md:px-5">
      <SidebarTrigger aria-label="Toggle navigation" />
      <Separator orientation="vertical" className="mx-1 h-4! self-center!" />
      <div className="flex min-w-0 flex-1 items-center">
        <Suspense fallback={null}>
          <SectionName />
        </Suspense>
      </div>
      <div className="flex items-center gap-0.5">
        <Suspense fallback={<Skeleton className="h-7 w-28" />}>
          <NotificationCenter />
          <ThemeToggle />
          <ProfileMenu />
        </Suspense>
      </div>
    </header>
  );
}
