"use client";

import Link from "next/link";
import { Contact } from "lucide-react";

import { CandidatePerformanceTable } from "@/components/candidates/candidate-table";
import { InterviewOutcomesChart, PendingVsAppliedChart, StatusDistributionChart } from "@/components/charts/distribution";
import { PipelineFunnel } from "@/components/charts/pipeline-funnel";
import { ApplicationsOverTimeChart, WeeklyActivityChart } from "@/components/charts/time-series";
import { DateRangeFilter } from "@/components/shared/date-range-filter";
import { FilterBar } from "@/components/shared/filters";
import { PageHeader, Section } from "@/components/shared/page-header";
import { EmptyState, ErrorState } from "@/components/shared/states";
import { useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { AssignmentKpis, MetricDefinitions, useDashboardRange } from "@/components/views/dashboard-kit";
import { memberCandidateHref } from "@/lib/member-candidates";
import { useMemberDashboard } from "@/lib/queries";

/**
 * Everything one member's dashboard shows comes from GET /api/users/{id}/dashboard: the totals over
 * the candidates assigned to the member today, the pipeline, and one row per candidate.
 * Used by the member for themselves and, with `embedded`, inside a manager's view of a member
 * (which brings its own list of the member's candidates, so the breakdown is left out there).
 */
export function MemberAnalyticsPanels({ memberId, embedded = false }: { memberId: string; embedded?: boolean }) {
  const me = useMe();
  const { state, setState, range, ready, today } = useDashboardRange();
  const dashboard = useMemberDashboard(ready ? memberId : undefined, range);
  const data = dashboard.data;
  const loading = dashboard.isPending;
  const fetching = dashboard.isPlaceholderData;
  const own = me.role === "MEMBER";

  if (dashboard.isError && !data) {
    return <ErrorState error={dashboard.error} onRetry={() => dashboard.refetch()} />;
  }
  const chart = { loading, fetching, onRetry: () => dashboard.refetch() };

  return (
    <div className="flex flex-col gap-5">
      <FilterBar
        className="mb-0"
        canClear={state.range !== "all"}
        onClear={() => setState({ range: "all", from: "", to: "" })}
      >
        <DateRangeFilter preset={state.range} from={state.from} to={state.to} today={today} onChange={setState} />
        <div className="ml-auto">
          <MetricDefinitions />
        </div>
      </FilterBar>

      <AssignmentKpis
        label={embedded ? "Job figures for this member's candidates" : "Job figures for your candidates"}
        stats={data?.stats}
        loading={loading}
        hrefs={
          own
            ? {
                // The figures and the lists behind them: "Not Applied" is the "To apply" section of Jobs (the same
                // count), and Applications holds every job with a confirmed submission, which is what "Applied" counts.
                assigned: "/member/jobs?section=all",
                not_applied: "/member/jobs",
                applied: "/member/applications",
                interviews: "/member/applications?status=INTERVIEWING",
                offers: "/member/applications?status=OFFER",
                closed: "/member/jobs?section=closed",
              }
            : undefined
        }
      />

      <PipelineFunnel data={data?.funnel} {...chart} />

      {own && !embedded && (
        <Section
          title="By candidate"
          description="The same figures for each of your candidates. The rows add up to the totals above."
          actions={
            <Button asChild variant="outline" size="sm">
              <Link href="/member/candidates">My Candidates</Link>
            </Button>
          }
        >
          <CandidatePerformanceTable
            rows={data?.candidates}
            loading={loading}
            fetching={fetching}
            showMember={false}
            candidateHref={memberCandidateHref}
            empty={
              <EmptyState
                compact
                icon={Contact}
                title="Your Team Lead has not assigned a candidate to you yet"
                description="Once a candidate is assigned to you, their figures are listed here."
              />
            }
          />
        </Section>
      )}

      <div className="grid gap-5 lg:grid-cols-3">
        <ApplicationsOverTimeChart data={data?.applications_over_time} className="lg:col-span-2" {...chart} />
        <PendingVsAppliedChart data={data?.pending_vs_applied} {...chart} />
      </div>
      <div className="grid gap-5 lg:grid-cols-2">
        <StatusDistributionChart data={data?.status_distribution} {...chart} />
        <WeeklyActivityChart data={data?.weekly_activity} {...chart} />
      </div>
      {/* Interview outcomes only exist once an employer has answered, so the panel appears with the data. */}
      {data && data.interview_outcomes.length > 0 && <InterviewOutcomesChart data={data.interview_outcomes} {...chart} />}
    </div>
  );
}

export function MemberDashboardView() {
  const me = useMe();
  return (
    <>
      <PageHeader
        title={`Welcome back, ${me.name.split(" ")[0]}`}
        description="Where the jobs of your candidates stand: what is waiting, what you have applied to on their behalf, and how employers have answered."
      />
      <MemberAnalyticsPanels memberId={me.id} />
    </>
  );
}
