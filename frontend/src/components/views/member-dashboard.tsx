"use client";

import { InterviewOutcomesChart, PendingVsAppliedChart, StatusDistributionChart } from "@/components/charts/distribution";
import { PipelineFunnel } from "@/components/charts/pipeline-funnel";
import { ApplicationsOverTimeChart, WeeklyActivityChart } from "@/components/charts/time-series";
import { DateRangeFilter } from "@/components/shared/date-range-filter";
import { FilterBar } from "@/components/shared/filters";
import { PageHeader } from "@/components/shared/page-header";
import { ErrorState } from "@/components/shared/states";
import { useMe } from "@/components/shell/me-context";
import { AssignmentKpis, MetricDefinitions, useDashboardRange } from "@/components/views/dashboard-kit";
import { useMemberDashboard } from "@/lib/queries";

/**
 * Everything one member's dashboard shows comes from GET /api/users/{id}/dashboard.
 * Used by the member for themselves and, with `embedded`, inside a manager's view of a member.
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
        label={embedded ? "Assignment figures for this member" : "Your assignment figures"}
        stats={data?.stats}
        loading={loading}
        hrefs={
          own
            ? {
                assigned: "/member/jobs",
                not_applied: "/member/jobs?applied=false",
                applied: "/member/applications",
                interviews: "/member/applications?status=INTERVIEWING",
                offers: "/member/applications?status=OFFER",
              }
            : undefined
        }
      />

      <PipelineFunnel data={data?.funnel} {...chart} />

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
        description="Where your assigned jobs stand: what is waiting, what you have applied to, and how employers have answered."
      />
      <MemberAnalyticsPanels memberId={me.id} />
    </>
  );
}
