"use client";

import Link from "next/link";
import { Search, UserCog, Users, UsersRound, type LucideIcon } from "lucide-react";

import { MemberDistributionChart, SearchUsageChart } from "@/components/charts/admin-charts";
import { PendingVsAppliedChart, StatusDistributionChart } from "@/components/charts/distribution";
import { PipelineFunnel } from "@/components/charts/pipeline-funnel";
import { ApplicationsOverTimeChart, WeeklyActivityChart } from "@/components/charts/time-series";
import { TeamPerformanceTable } from "@/components/people/member-table";
import { DateRangeFilter } from "@/components/shared/date-range-filter";
import { FilterBar, FilterSelect } from "@/components/shared/filters";
import { KPIStatCard, KpiStrip } from "@/components/shared/kpi";
import { PageHeader, Section } from "@/components/shared/page-header";
import { SearchInput } from "@/components/shared/search-input";
import { ErrorState } from "@/components/shared/states";
import { AssignmentKpis, MetricDefinitions } from "@/components/views/dashboard-kit";
import { useToday } from "@/hooks/use-now";
import { resolveRange } from "@/lib/date-range";
import { useAdminAnalytics, useTeams, useUsers } from "@/lib/queries";
import { isAssignmentStatus, STATUS_META, STATUS_ORDER } from "@/lib/status";
import type { AdminAnalyticsParams, NameValue } from "@/lib/types";
import { useUrlState } from "@/lib/url-state";

const DEFAULTS = { range: "all", from: "", to: "", team: "", member: "", status: "", company: "", role: "" };

const number = (value: unknown): number | undefined => (typeof value === "number" && Number.isFinite(value) ? value : undefined);

function OrgTile({ icon: Icon, value, label, href }: { icon: LucideIcon; value: number | undefined; label: string; href: string }) {
  return (
    <Link href={href} className="flex items-center gap-3 rounded-lg border bg-card px-4 py-3 transition-colors hover:border-border-strong">
      <span className="grid size-8 shrink-0 place-items-center rounded-md bg-primary/8 text-primary" aria-hidden="true">
        <Icon className="size-4" />
      </span>
      <span className="flex items-baseline gap-2">
        <span className="font-figure text-3xl leading-none">{value === undefined ? "—" : value.toLocaleString()}</span>
        <span className="text-sm text-muted-foreground">{label}</span>
      </span>
    </Link>
  );
}

/**
 * The organisation as an Admin sees it, from GET /api/analytics/admin.
 * "dashboard" leads with the state of the organisation and the teams;
 * "analytics" shows every chart for the same filters.
 *
 * Job postings and assignments are different things and are shown apart: a
 * posting is one vacancy, counted once; an assignment is that posting given to
 * one member, so one posting can be many assignments.
 */
export function AdminAnalyticsView({ variant }: { variant: "dashboard" | "analytics" }) {
  const today = useToday();
  const [state, setState] = useUrlState(DEFAULTS);
  const { range, ready } = resolveRange(state.range, state.from, state.to, today);
  const params: AdminAnalyticsParams = {
    ...range,
    team_id: state.team || undefined,
    member_id: state.member || undefined,
    status: isAssignmentStatus(state.status) ? state.status : undefined,
    company: state.company || undefined,
    role: state.role || undefined,
  };
  const analytics = useAdminAnalytics(params, ready);
  const teams = useTeams(true);
  const members = useUsers({ role: "MEMBER", team_id: state.team || undefined, sort: "name", page: 1, page_size: 200 }, { refetchInterval: false });

  const data = analytics.data;
  const loading = analytics.isPending;
  const fetching = analytics.isPlaceholderData;
  const totals = (data?.totals ?? {}) as Record<string, unknown>;
  const filtered = Object.entries(state).some(([key, value]) => value !== DEFAULTS[key as keyof typeof DEFAULTS]);
  const chart = { loading, fetching, onRetry: () => analytics.refetch() };
  // The same two figures the meter needs, from the stats the API already sent.
  const pendingVsApplied: NameValue[] | undefined = data && [
    { name: "Not applied", value: data.stats.not_applied },
    { name: "Applied", value: data.stats.applied },
  ];

  return (
    <>
      <PageHeader
        title={variant === "dashboard" ? "Dashboard" : "Analytics"}
        description={
          variant === "dashboard"
            ? "The whole organisation: teams, job postings, and how far assigned jobs have got."
            : "Assignments, applications and searches across the organisation, for the filters you choose."
        }
        actions={<MetricDefinitions />}
      />

      <FilterBar canClear={filtered} onClear={() => setState({ ...DEFAULTS })}>
        <DateRangeFilter preset={state.range} from={state.from} to={state.to} today={today} onChange={setState} />
        <FilterSelect
          label="Team"
          allLabel="All teams"
          value={state.team}
          onChange={(team) => setState({ team, member: "" })}
          options={(teams.data ?? []).map((team) => ({ value: team.id, label: team.name }))}
        />
        <FilterSelect
          label="Member"
          allLabel="All members"
          value={state.member}
          onChange={(member) => setState({ member })}
          options={(members.data?.items ?? []).map((user) => ({ value: user.id, label: user.name }))}
        />
        <FilterSelect
          label="Status"
          allLabel="All statuses"
          value={state.status}
          onChange={(status) => setState({ status })}
          options={STATUS_ORDER.map((status) => ({ value: status, label: STATUS_META[status].label }))}
        />
        <SearchInput label="Filter by company" placeholder="Company" value={state.company} onChange={(company) => setState({ company })} className="sm:w-44" />
        <SearchInput label="Filter by job title" placeholder="Job title" value={state.role} onChange={(role) => setState({ role })} className="sm:w-44" />
      </FilterBar>

      {analytics.isError && !data ? (
        <div className="rounded-lg border bg-card">
          <ErrorState error={analytics.error} onRetry={() => analytics.refetch()} />
        </div>
      ) : (
        <div className="flex flex-col gap-7">
          {variant === "dashboard" && (
            <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <li>
                <OrgTile icon={UsersRound} value={number(totals.teams)} label={totals.teams === 1 ? "team" : "teams"} href="/admin/teams" />
              </li>
              <li>
                <OrgTile icon={UserCog} value={number(totals.team_leads)} label={totals.team_leads === 1 ? "Team Lead" : "Team Leads"} href="/admin/team-leads" />
              </li>
              <li>
                <OrgTile icon={Users} value={number(totals.members)} label={totals.members === 1 ? "member" : "members"} href="/admin/members" />
              </li>
              <li>
                <OrgTile
                  icon={Search}
                  value={number(totals.active_searches)}
                  label={totals.active_searches === 1 ? "search running" : "searches running"}
                  href="/admin/searches?status=active"
                />
              </li>
            </ul>
          )}

          <Section
            title="Job postings"
            description="Unique postings. Each vacancy is counted once, however many members it was given to. These three figures do not change with the filters above."
          >
            <KpiStrip label="Job postings" className="md:grid-cols-3 xl:grid-cols-3">
              <KPIStatCard
                label="Postings in the portal"
                value={number(totals.job_postings)}
                loading={loading}
                href="/admin/jobs"
                definition="Distinct job postings stored in the portal: found by a search, added by hand or imported. One row per vacancy."
              />
              <KPIStatCard
                label="Postings in the search index"
                value={number(totals.indexed_jobs)}
                loading={loading}
                definition="Postings the search engine has read from the job sources and keeps to search through. Most of them are never assigned to anyone."
              />
              <KPIStatCard
                label="Of those, read recently"
                value={number(totals.indexed_jobs_fresh)}
                loading={loading}
                definition="Index postings whose source was read recently enough to still count as fresh. The rest are re-read when a search needs them."
              />
            </KpiStrip>
          </Section>

          <Section
            title="Assignments"
            description="One assignment is one posting given to one member. A posting assigned to three members counts three times here."
          >
            <div className="flex flex-col gap-5">
              <AssignmentKpis label="Assignment figures for the organisation" stats={data?.stats} loading={loading} />
              <PipelineFunnel data={data?.funnel} {...chart} />
              {variant === "dashboard" ? (
                <div className="grid gap-5 lg:grid-cols-3">
                  <ApplicationsOverTimeChart data={data?.applications_over_time} className="lg:col-span-2" {...chart} />
                  <StatusDistributionChart data={data?.status_distribution} {...chart} />
                </div>
              ) : (
                <>
                  <div className="grid gap-5 lg:grid-cols-3">
                    <ApplicationsOverTimeChart data={data?.applications_over_time} className="lg:col-span-2" {...chart} />
                    <PendingVsAppliedChart data={pendingVsApplied} {...chart} />
                  </div>
                  <div className="grid gap-5 lg:grid-cols-2">
                    <StatusDistributionChart data={data?.status_distribution} {...chart} />
                    <WeeklyActivityChart data={data?.weekly_activity} {...chart} />
                  </div>
                  <div className="grid gap-5 lg:grid-cols-3">
                    <SearchUsageChart data={data?.search_usage} className="lg:col-span-2" {...chart} />
                    <MemberDistributionChart data={data?.member_distribution} {...chart} />
                  </div>
                </>
              )}
            </div>
          </Section>

          <Section title="Team performance" description="Each team's assignment figures for the filters above. Select a team to open its page.">
            <TeamPerformanceTable rows={data?.teams} loading={loading} fetching={fetching} />
          </Section>
        </div>
      )}
    </>
  );
}
