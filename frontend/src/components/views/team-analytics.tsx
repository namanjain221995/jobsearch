"use client";

import Link from "next/link";
import { Contact, Inbox, Search, Users } from "lucide-react";

import { PendingVsAppliedChart, StatusDistributionChart } from "@/components/charts/distribution";
import { PipelineFunnel } from "@/components/charts/pipeline-funnel";
import { ApplicationsOverTimeChart, WeeklyActivityChart } from "@/components/charts/time-series";
import { CandidatePerformanceTable } from "@/components/candidates/candidate-table";
import { MemberPerformanceTable } from "@/components/people/member-table";
import { DateRangeFilter } from "@/components/shared/date-range-filter";
import { FilterBar } from "@/components/shared/filters";
import { PageHeader, Section } from "@/components/shared/page-header";
import { ErrorState } from "@/components/shared/states";
import { useArea, useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { AssignmentKpis, MetricDefinitions, useDashboardRange } from "@/components/views/dashboard-kit";
import { useTeamAnalytics } from "@/lib/queries";
import type { TeamAnalytics } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Four facts about the team right now, each a link to where it is dealt with. */
function TeamNow({ data, teamId }: { data: TeamAnalytics | undefined; teamId: string }) {
  const me = useMe();
  const area = useArea();
  const own = me.role === "TEAM_LEAD";
  const unassigned = data?.unassigned_candidates ?? 0;
  const items = [
    {
      icon: Contact,
      value: data?.candidate_count,
      label: `${data?.candidate_count === 1 ? "active candidate" : "active candidates"}${unassigned > 0 ? `, ${unassigned} unassigned` : ""}`,
      href: own
        ? unassigned > 0
          ? "/tl/candidates?member=unassigned"
          : "/tl/candidates"
        : `${area}/teams/${teamId}?tab=candidates`,
    },
    {
      icon: Users,
      value: data?.member_count,
      label: data?.member_count === 1 ? "active member" : "active members",
      href: own ? "/tl/members" : `${area}/teams/${teamId}?tab=members`,
    },
    {
      icon: Search,
      value: data?.active_searches,
      label: data?.active_searches === 1 ? "search running" : "searches running",
      href: own ? "/tl/searches?status=active" : `${area}/teams/${teamId}?tab=searches`,
    },
    {
      icon: Inbox,
      value: data?.queue_pending,
      label: data?.queue_pending === 1 ? "job waiting in the queue" : "jobs waiting in the queue",
      href: own ? "/tl/jobs" : `${area}/teams/${teamId}?tab=queue`,
    },
  ];
  return (
    <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {items.map((item) => (
        <li key={item.label}>
          <Link
            href={item.href}
            className="flex items-center gap-3 rounded-lg border bg-card px-4 py-3 transition-colors hover:border-border-strong"
          >
            <span className="grid size-8 shrink-0 place-items-center rounded-md bg-primary/8 text-primary" aria-hidden="true">
              <item.icon className="size-4" />
            </span>
            <span className="flex items-baseline gap-2">
              <span className={cn("font-figure text-3xl leading-none", item.value === undefined && "text-muted-foreground")}>
                {item.value === undefined ? "—" : item.value.toLocaleString()}
              </span>
              <span className="text-sm text-muted-foreground">{item.label}</span>
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

/**
 * Everything GET /api/teams/{id}/analytics returns, laid out for two uses:
 * "dashboard" (where the team stands now, and who needs attention) and
 * "analytics" (the same figures over a chosen date range, with every chart).
 */
export function TeamAnalyticsPanels({ teamId, variant }: { teamId: string; variant: "dashboard" | "analytics" }) {
  const area = useArea();
  const { state, setState, range, ready, today } = useDashboardRange();
  const analytics = useTeamAnalytics(ready ? teamId : undefined, variant === "analytics" ? range : {});
  const data = analytics.data;
  const loading = analytics.isPending;
  const fetching = analytics.isPlaceholderData;

  if (analytics.isError && !data) return <ErrorState error={analytics.error} onRetry={() => analytics.refetch()} />;
  const chart = { loading, fetching, onRetry: () => analytics.refetch() };
  const memberHref = (id: string) => `${area}/members/${id}`;

  return (
    <div className="flex flex-col gap-5">
      {variant === "analytics" ? (
        <FilterBar className="mb-0" canClear={state.range !== "all"} onClear={() => setState({ range: "all", from: "", to: "" })}>
          <DateRangeFilter preset={state.range} from={state.from} to={state.to} today={today} onChange={setState} />
          <div className="ml-auto">
            <MetricDefinitions />
          </div>
        </FilterBar>
      ) : (
        <TeamNow data={data} teamId={teamId} />
      )}

      <AssignmentKpis label="Assignment figures for this team" stats={data?.stats} loading={loading} />
      <PipelineFunnel data={data?.funnel} {...chart} />

      <div className="grid gap-5 lg:grid-cols-3">
        <ApplicationsOverTimeChart data={data?.applications_over_time} className="lg:col-span-2" {...chart} />
        <PendingVsAppliedChart data={data?.pending_vs_applied} {...chart} />
      </div>

      {variant === "analytics" && (
        <div className="grid gap-5 lg:grid-cols-2">
          <StatusDistributionChart data={data?.status_distribution} {...chart} />
          <WeeklyActivityChart data={data?.weekly_activity} {...chart} />
        </div>
      )}

      <Section
        title="Candidates"
        description={
          variant === "dashboard"
            ? "Every candidate of the team, sorted by jobs still waiting for an application. Select a column to sort by it."
            : "Each candidate's figures for the chosen date range."
        }
      >
        <CandidatePerformanceTable
          rows={data?.candidates}
          loading={loading}
          fetching={fetching}
          candidateHref={(id) => `${area}/candidates/${id}`}
          memberHref={memberHref}
        />
      </Section>

      <Section
        title="Members"
        description={
          variant === "dashboard"
            ? "Each member's totals over the candidates they are responsible for today. Candidates without a responsible member are in no member's row."
            : "Each member's totals over their candidates, for the chosen date range."
        }
      >
        <MemberPerformanceTable rows={data?.members} loading={loading} fetching={fetching} memberHref={memberHref} />
      </Section>
    </div>
  );
}

function NoTeam() {
  return (
    <div className="rounded-lg border bg-card">
      <ErrorState error={new Error("You are not assigned to a team yet. Ask an Admin to make you the Team Lead of a team.")} />
    </div>
  );
}

export function TeamLeadDashboardView() {
  const me = useMe();
  return (
    <>
      <PageHeader
        title={me.team ? me.team.name : "Dashboard"}
        description="Where the jobs of your team's candidates stand, and which candidates still have jobs waiting."
        actions={
          <>
            <MetricDefinitions />
            <Button asChild>
              <Link href="/tl/search">
                <Search data-icon="inline-start" />
                Find Jobs
              </Link>
            </Button>
          </>
        }
      />
      {me.team ? <TeamAnalyticsPanels teamId={me.team.id} variant="dashboard" /> : <NoTeam />}
    </>
  );
}

export function TeamLeadAnalyticsView() {
  const me = useMe();
  return (
    <>
      <PageHeader
        title="Analytics"
        description="Assignments and applications for your team's candidates over time. Choose a date range to look at one period."
      />
      {me.team ? <TeamAnalyticsPanels teamId={me.team.id} variant="analytics" /> : <NoTeam />}
    </>
  );
}
