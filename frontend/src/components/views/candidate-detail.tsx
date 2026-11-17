"use client";

import Link from "next/link";
import { useState, type ReactNode } from "react";
import { Search } from "lucide-react";

import { CandidateActions } from "@/components/candidates/candidate-actions";
import { CandidateInactiveTag, ResponsibleMember, ResumeTag } from "@/components/candidates/candidate-meta";
import { CandidateProfileCompletion, CandidateProfilePanel } from "@/components/candidates/profile-form";
import { CandidateResumePanel } from "@/components/candidates/resume-upload";
import { InterviewOutcomesChart, PendingVsAppliedChart, StatusDistributionChart } from "@/components/charts/distribution";
import { PipelineFunnel } from "@/components/charts/pipeline-funnel";
import { ApplicationsOverTimeChart, WeeklyActivityChart } from "@/components/charts/time-series";
import { DateRangeFilter } from "@/components/shared/date-range-filter";
import { FilterBar } from "@/components/shared/filters";
import { PageHeader, Panel } from "@/components/shared/page-header";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { useArea } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ActivityFeed } from "@/components/views/activity-feed";
import { AssignmentDrawer } from "@/components/views/assignment-detail";
import { ASSIGNMENT_LIST_DEFAULTS, AssignmentsTable } from "@/components/views/assignments";
import { AssignmentKpis, MetricDefinitions, useDashboardRange } from "@/components/views/dashboard-kit";
import { SEARCH_LIST_DEFAULTS, SearchHistoryTable } from "@/components/views/search-history";
import { formatDate } from "@/lib/format";
import { useLocalListState } from "@/lib/list-state";
import { useCandidate, useCandidateAnalytics } from "@/lib/queries";
import type { Candidate } from "@/lib/types";
import { useUrlState } from "@/lib/url-state";

const TAB_DEFAULTS = { tab: "overview" };
const TABS = [
  { value: "overview", label: "Overview" },
  { value: "jobs", label: "Jobs" },
  { value: "profile", label: "Profile and Resume" },
  { value: "activity", label: "Activity" },
  { value: "searches", label: "Searches" },
];

/** One candidate's figures and charts, from GET /api/candidates/{id}/analytics (the shape of the member dashboard). */
export function CandidateAnalyticsPanels({ candidateId }: { candidateId: string }) {
  const { state, setState, range, ready, today } = useDashboardRange();
  const analytics = useCandidateAnalytics(ready ? candidateId : undefined, range);
  const data = analytics.data;
  const loading = analytics.isPending;
  const fetching = analytics.isPlaceholderData;

  if (analytics.isError && !data) return <ErrorState error={analytics.error} onRetry={() => analytics.refetch()} />;
  const chart = { loading, fetching, onRetry: () => analytics.refetch() };

  return (
    <div className="flex flex-col gap-5">
      <FilterBar className="mb-0" canClear={state.range !== "all"} onClear={() => setState({ range: "all", from: "", to: "" })}>
        <DateRangeFilter preset={state.range} from={state.from} to={state.to} today={today} onChange={setState} />
        <div className="ml-auto">
          <MetricDefinitions />
        </div>
      </FilterBar>
      <AssignmentKpis label="Assignment figures for this candidate" stats={data?.stats} loading={loading} />
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

/** Who the candidate is and how to reach them. The same facts the responsible member sees. */
export function CandidateFacts({ candidate, memberHref }: { candidate: Candidate; memberHref?: string }) {
  const none = (text: string) => <span className="text-muted-foreground">{text}</span>;
  const facts: Array<{ label: string; value: ReactNode }> = [
    {
      label: "E-mail",
      value: candidate.email ? (
        <a href={`mailto:${candidate.email}`} className="rounded-sm break-all underline-offset-4 hover:underline">
          {candidate.email}
        </a>
      ) : (
        none("None entered")
      ),
    },
    {
      label: "Phone",
      value: candidate.phone ? (
        <a href={`tel:${candidate.phone.replace(/[^0-9+]/g, "")}`} className="rounded-sm underline-offset-4 hover:underline">
          {candidate.phone}
        </a>
      ) : (
        none("None entered")
      ),
    },
    { label: "Team", value: candidate.team?.name ?? none("No team") },
    { label: "Responsible member", value: <ResponsibleMember member={candidate.assigned_member} href={memberHref} /> },
    {
      label: "Added",
      value: `${formatDate(candidate.created_at)}${candidate.created_by ? `, by ${candidate.created_by.name}` : ""}`,
    },
  ];
  return (
    <div className="flex flex-col gap-4">
      <dl className="grid grid-cols-1 gap-x-8 gap-y-4 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-5">
        {facts.map((fact) => (
          <div key={fact.label} className="flex min-w-0 flex-col gap-1">
            <dt className="text-2sm text-muted-foreground">{fact.label}</dt>
            <dd className="text-sm break-words">{fact.value}</dd>
          </div>
        ))}
      </dl>
      <div className="flex flex-col gap-1 border-t pt-3">
        <h4 className="text-2sm text-muted-foreground">Notes</h4>
        {candidate.notes ? (
          <p className="max-w-[80ch] text-sm break-words whitespace-pre-wrap">{candidate.notes}</p>
        ) : (
          <p className="text-sm text-muted-foreground">No notes.</p>
        )}
      </div>
    </div>
  );
}

function CandidateJobs({ candidateId }: { candidateId: string }) {
  const listState = useLocalListState(ASSIGNMENT_LIST_DEFAULTS);
  return <AssignmentsTable listState={listState} candidateId={candidateId} compact />;
}

function CandidateSearches({ candidateId }: { candidateId: string }) {
  const listState = useLocalListState(SEARCH_LIST_DEFAULTS);
  return <SearchHistoryTable listState={listState} candidateId={candidateId} showFilters={false} />;
}

/**
 * One candidate as their Team Lead or an Admin sees them: details, figures and charts, the jobs
 * assigned to them, the profile and resume (both edited here), the activity timeline and the
 * searches run for them.
 */
export function CandidateDetailView({ candidateId }: { candidateId: string }) {
  const area = useArea();
  const query = useCandidate(candidateId);
  const [state, setState] = useUrlState(TAB_DEFAULTS);
  const [opened, setOpened] = useState<string | null>(null);
  const back = { href: `${area}/candidates`, label: "Candidates" };
  const tab = TABS.some((item) => item.value === state.tab) ? state.tab : "overview";

  if (query.isPending) {
    return (
      <>
        <PageHeader title="Candidate" back={back} />
        <LoadingSkeleton variant="detail" count={4} />
      </>
    );
  }
  if (query.isError) {
    return (
      <>
        <PageHeader title="Candidate" back={back} />
        <div className="rounded-lg border bg-card">
          <ErrorState
            error={query.error}
            onRetry={() => query.refetch()}
            unavailable={{
              title: "This candidate is not available to you",
              description: "The candidate does not exist, or belongs to a team you do not manage.",
            }}
            action={
              <Button asChild variant="outline" size="sm">
                <Link href={back.href}>Go to Candidates</Link>
              </Button>
            }
          />
        </div>
      </>
    );
  }
  const candidate = query.data;
  const firstName = candidate.name.split(" ")[0];
  const memberHref = candidate.assigned_member ? `${area}/members/${candidate.assigned_member.id}` : undefined;

  return (
    <>
      <PageHeader
        back={back}
        title={candidate.name}
        meta={
          <>
            <CandidateInactiveTag candidate={candidate} />
            <ResumeTag hasResume={candidate.has_resume} />
          </>
        }
        description={
          <>
            Candidate{candidate.team ? ` of ${candidate.team.name}` : ""}. Jobs handled by{" "}
            {candidate.assigned_member ? (
              <ResponsibleMember member={candidate.assigned_member} href={memberHref} className="text-foreground" />
            ) : (
              <span className="text-warning">nobody yet: choose a responsible member</span>
            )}
            .
          </>
        }
        actions={
          <>
            <CandidateActions candidate={candidate} variant="buttons" />
            {candidate.is_active ? (
              <Button asChild>
                <Link href={`${area}/search?candidate=${candidate.id}`}>
                  <Search data-icon="inline-start" />
                  Find Jobs for {firstName}
                </Link>
              </Button>
            ) : (
              <Button disabled title="A deactivated candidate cannot be searched for">
                <Search data-icon="inline-start" />
                Find Jobs for {firstName}
              </Button>
            )}
          </>
        }
      />

      <Tabs value={tab} onValueChange={(next) => setState({ tab: next })} className="gap-5">
        <TabsList variant="line" className="w-full justify-start overflow-x-auto border-b">
          {TABS.map((item) => (
            <TabsTrigger key={item.value} value={item.value} className="flex-none">
              {item.label}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="overview" className="flex flex-col gap-5">
          <Panel title="Details">
            <CandidateFacts candidate={candidate} memberHref={memberHref} />
          </Panel>
          <CandidateAnalyticsPanels candidateId={candidate.id} />
        </TabsContent>

        <TabsContent value="jobs">
          <CandidateJobs candidateId={candidate.id} />
        </TabsContent>

        <TabsContent value="profile">
          <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_22rem]">
            <div className="flex min-w-0 flex-col gap-5">
              <Panel title="Job preferences" description="What searches for this candidate look for.">
                <CandidateProfilePanel candidateId={candidate.id} candidateName={candidate.name} />
              </Panel>
              <Panel title="Resume" description="PDF, DOCX or TXT. Uploading a new file replaces the current one.">
                <CandidateResumePanel candidateId={candidate.id} candidateName={candidate.name} />
              </Panel>
            </div>
            <div className="flex min-w-0 flex-col gap-5 max-xl:order-first">
              <Panel title="Profile completion">
                <CandidateProfileCompletion candidateId={candidate.id} />
              </Panel>
            </div>
          </div>
        </TabsContent>

        <TabsContent value="activity">
          <Panel>
            <ActivityFeed candidateId={candidate.id} onOpenAssignment={setOpened} />
          </Panel>
        </TabsContent>

        <TabsContent value="searches">
          <CandidateSearches candidateId={candidate.id} />
        </TabsContent>
      </Tabs>

      <AssignmentDrawer assignmentId={opened} onOpenChange={(open) => !open && setOpened(null)} />
    </>
  );
}
