"use client";

import { usePathname, useRouter } from "next/navigation";
import { Component, Suspense, useEffect, type ErrorInfo, type ReactNode } from "react";
import { QueryErrorResetBoundary } from "@tanstack/react-query";
import { TriangleAlert } from "lucide-react";

import { BrandMark } from "@/components/brand";
import { SearchProgressHost, SearchProgressProvider } from "@/components/search/search-progress";
import { Header } from "@/components/shell/header";
import { useMe } from "@/components/shell/me-context";
import { RoleSidebar } from "@/components/shell/role-sidebar";
import { ErrorState } from "@/components/shared/states";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";
import { Skeleton } from "@/components/ui/skeleton";
import { ROLE_AREA } from "@/lib/nav";
import { useTeam } from "@/lib/queries";

/** The sidebar before the signed-in user is known. Same footprint as the real one, so nothing shifts. */
function SidebarSkeleton() {
  return (
    <div
      className="hidden w-64 shrink-0 flex-col border-r bg-sidebar md:flex"
      role="status"
      aria-label="Loading navigation"
      aria-busy="true"
    >
      <div className="flex h-13 items-center gap-2.5 border-b px-3">
        <BrandMark />
        <span className="font-display text-lg leading-none">AI Job Finder</span>
      </div>
      <div className="flex flex-col gap-3 p-4">
        {[72, 56, 64, 48, 60, 52, 66].map((width, index) => (
          <Skeleton key={index} className="h-5" style={{ width: `${width}%` }} />
        ))}
      </div>
    </div>
  );
}

/**
 * Second line of route protection, after the proxy: if someone lands in another
 * role's area through a cached client navigation, send them home. The API would
 * refuse the data anyway; this only spares them a screen of "access denied".
 */
function RoleAreaGuard() {
  const me = useMe();
  const pathname = usePathname();
  const router = useRouter();
  const foreign = Object.values(ROLE_AREA).some(
    (area) => area !== ROLE_AREA[me.role] && (pathname === area || pathname.startsWith(`${area}/`)),
  );
  useEffect(() => {
    if (foreign) router.replace(me.home);
  }, [foreign, me.home, router]);
  return null;
}

/**
 * A Team Lead whose team was deactivated can still look at everything, but the
 * API refuses every change (409). Say so up front, in the API's own words,
 * instead of letting them find out one failed action at a time.
 */
function TeamNotice() {
  const me = useMe();
  const team = useTeam(me.role === "TEAM_LEAD" ? me.team?.id : undefined);
  if (!team.data || team.data.is_active) return null;
  return (
    <div
      role="status"
      data-slot="team-notice"
      className="mb-5 flex items-start gap-2 rounded-md border border-warning/40 bg-warning/8 px-3 py-2 text-sm"
    >
      <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
      <span>
        <span className="font-medium">{team.data.name} is deactivated.</span> You can still see everything, but an
        administrator has to reactivate the team before you can make changes.
      </span>
    </div>
  );
}

/**
 * Catches a failure to load the signed-in user (the API is down, or the session
 * request failed for another reason) and anything else that breaks while
 * rendering, and offers a way to try again instead of a blank screen.
 */
class ShellErrorBoundary extends Component<
  { children: ReactNode; onReset: () => void },
  { error: unknown; failed: boolean }
> {
  state = { error: null as unknown, failed: false };

  static getDerivedStateFromError(error: unknown) {
    return { error, failed: true };
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error("The dashboard could not be rendered", error, info.componentStack);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <main className="grid min-h-svh place-items-center px-6">
        <ErrorState
          error={this.state.error}
          onRetry={() => {
            this.props.onReset();
            this.setState({ error: null, failed: false });
          }}
        />
      </main>
    );
  }
}

/**
 * The frame every signed-in screen lives in: collapsible sidebar (a drawer on
 * small screens), top bar, and the page. The page is always rendered; whatever
 * needs the signed-in user waits behind its own Suspense boundary.
 */
export function DashboardShell({ children }: { children: ReactNode }) {
  return (
    <QueryErrorResetBoundary>
      {({ reset }) => (
        <ShellErrorBoundary onReset={reset}>
          <a
            href="#main"
            className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded-md focus:bg-primary focus:px-3 focus:py-2 focus:text-sm focus:text-primary-foreground"
          >
            Skip to content
          </a>
          <SearchProgressProvider>
            <SidebarProvider>
              <Suspense fallback={<SidebarSkeleton />}>
                <RoleSidebar />
              </Suspense>
              <SidebarInset className="min-w-0">
                <Header />
                <main id="main" tabIndex={-1} className="flex-1 px-4 py-6 outline-none md:px-8 md:py-8">
                  <div className="mx-auto w-full max-w-[84rem]">
                    <Suspense fallback={null}>
                      <TeamNotice />
                    </Suspense>
                    {children}
                  </div>
                </main>
              </SidebarInset>
              <Suspense fallback={null}>
                <RoleAreaGuard />
                <SearchProgressHost />
              </Suspense>
            </SidebarProvider>
          </SearchProgressProvider>
        </ShellErrorBoundary>
      )}
    </QueryErrorResetBoundary>
  );
}
