"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { Contact, SearchX } from "lucide-react";

import { CandidateInactiveTag, ResumeTag } from "@/components/candidates/candidate-meta";
import { ProfileFacts } from "@/components/candidates/profile-form";
import { ResumeView } from "@/components/candidates/resume-upload";
import { Watermarked } from "@/components/jobs/watermark";
import { MEMBER_JOB_DEFAULTS, MemberAssignmentList, NoJobsYet } from "@/components/member/assignment-list";
import { CandidateCard, CandidateContact } from "@/components/member/candidate-card";
import { FilterBar, FilterSelect } from "@/components/shared/filters";
import { PageHeader, Panel } from "@/components/shared/page-header";
import { SearchInput } from "@/components/shared/search-input";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ActivityFeed } from "@/components/views/activity-feed";
import { AssignmentKpis } from "@/components/views/dashboard-kit";
import { formatDate, formatDateTime } from "@/lib/format";
import { memberCandidateHref, memberJobHref } from "@/lib/member-candidates";
import { useCandidate, useCandidates } from "@/lib/queries";
import type { Candidate } from "@/lib/types";
import { useUrlState } from "@/lib/url-state";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ My Candidates */

const LIST_DEFAULTS = { q: "", sort: "newest_job" };
/** The orders GET /api/candidates offers that matter to a member; the API sorts, ties go by name. */
const LIST_SORTS = [
  { value: "newest_job", label: "Newest job first" },
  { value: "not_applied", label: "Most not applied first" },
  { value: "name", label: "Name, A to Z" },
];
/** The most GET /api/candidates returns in one page. */
const LIST_LIMIT = 200;

/** The candidates the Team Lead has assigned to this member, as cards. */
export function MemberCandidatesView() {
  const [state, setState] = useUrlState(LIST_DEFAULTS);
  const sort = LIST_SORTS.some((option) => option.value === state.sort) ? state.sort : LIST_DEFAULTS.sort;
  // Whose candidates these are is decided by the API: a member receives the ones assigned to them today.
  const list = useCandidates({ q: state.q || undefined, sort, page: 1, page_size: LIST_LIMIT });
  const items = list.data?.items ?? [];
  const searching = Boolean(state.q);
  const sorted = items;
  const total = list.data?.total ?? 0;

  return (
    <>
      <PageHeader
        title="My Candidates"
        description="The people you apply for. Open a candidate to see their jobs, profile and resume."
      />
      <FilterBar canClear={searching} onClear={() => setState({ q: "" })}>
        <SearchInput
          label="Search candidates"
          placeholder="Search name, e-mail or phone"
          value={state.q}
          onChange={(q) => setState({ q })}
        />
        <FilterSelect label="Sort" value={sort} onChange={(next) => setState({ sort: next })} options={LIST_SORTS} />
      </FilterBar>

      {list.isPending ? (
        <LoadingSkeleton variant="cards" count={3} />
      ) : list.isError ? (
        <div className="rounded-lg border bg-card">
          <ErrorState error={list.error} onRetry={() => list.refetch()} />
        </div>
      ) : sorted.length === 0 ? (
        <div className="rounded-lg border bg-card">
          {searching ? (
            <EmptyState
              icon={SearchX}
              title="No candidate matches this search"
              description="The search looks at the name, e-mail and phone of your candidates."
              action={
                <Button variant="outline" size="sm" onClick={() => setState({ q: "" })}>
                  Clear search
                </Button>
              }
            />
          ) : (
            <EmptyState
              icon={Contact}
              title="Your Team Lead has not assigned a candidate to you yet"
              description="As soon as a candidate is assigned to you, they appear here with their profile, resume and jobs. You do not need to sign in again."
            />
          )}
        </div>
      ) : (
        <>
          <ul
            aria-label="Your candidates"
            className={cn("grid gap-3 sm:grid-cols-2 xl:grid-cols-3", list.isPlaceholderData && "opacity-60 transition-opacity")}
          >
            {sorted.map((candidate) => (
              <li key={candidate.id} className="min-w-0">
                <CandidateCard candidate={candidate} />
              </li>
            ))}
          </ul>
          <p aria-live="polite" className="mt-4 text-2sm text-muted-foreground tabular-nums">
            {total > sorted.length
              ? `Showing ${sorted.length.toLocaleString()} of ${total.toLocaleString()} candidates. Search by name to find the others.`
              : `${sorted.length.toLocaleString()} ${sorted.length === 1 ? "candidate" : "candidates"}`}
          </p>
        </>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ one candidate */

const DETAIL_DEFAULTS = { tab: "jobs", ...MEMBER_JOB_DEFAULTS };
const TABS = [
  { value: "jobs", label: "Jobs" },
  { value: "details", label: "Details" },
  { value: "profile", label: "Profile" },
  { value: "resume", label: "Resume" },
  { value: "activity", label: "Activity" },
];
const BACK = { href: "/member/candidates", label: "My Candidates" };

function DetailFacts({ facts }: { facts: Array<{ label: string; value: ReactNode }> }) {
  return (
    <dl className="grid grid-cols-1 gap-x-8 gap-y-4 sm:grid-cols-2">
      {facts.map((fact) => (
        <div key={fact.label} className="flex min-w-0 flex-col gap-1">
          <dt className="text-2sm text-muted-foreground">{fact.label}</dt>
          <dd className="text-sm break-words">{fact.value}</dd>
        </div>
      ))}
    </dl>
  );
}

const none = (text: string) => <span className="text-muted-foreground">{text}</span>;

/** What is on record about the candidate. Read-only: the Team Lead keeps it up to date. */
function CandidateDetails({ candidate }: { candidate: Candidate }) {
  return (
    <div className="grid gap-5 xl:grid-cols-2">
      <Panel title="Details" description="Kept up to date by your Team Lead.">
        <DetailFacts
          facts={[
            { label: "Name", value: candidate.name },
            {
              label: "E-mail",
              value: candidate.email ? (
                <a href={`mailto:${candidate.email}`} className="rounded-sm break-all underline-offset-4 hover:underline">
                  {candidate.email}
                </a>
              ) : (
                none("Not on record")
              ),
            },
            {
              label: "Phone",
              value: candidate.phone ? (
                <a href={`tel:${candidate.phone.replace(/[^\d+]/g, "")}`} className="rounded-sm underline-offset-4 hover:underline">
                  {candidate.phone}
                </a>
              ) : (
                none("Not on record")
              ),
            },
            { label: "Team", value: candidate.team?.name ?? none("No team") },
            { label: "Responsible member", value: candidate.assigned_member?.name ?? none("Nobody") },
            {
              label: "Added",
              value: `${formatDate(candidate.created_at)}${candidate.created_by ? `, by ${candidate.created_by.name}` : ""}`,
            },
            { label: "Details last changed", value: formatDateTime(candidate.updated_at) },
          ]}
        />
      </Panel>
      <Panel title="Notes" description="Written by your Team Lead about this candidate.">
        {candidate.notes.trim() ? (
          <p className="text-sm leading-relaxed break-words whitespace-pre-wrap">{candidate.notes}</p>
        ) : (
          <p className="text-sm text-muted-foreground">No notes about this candidate.</p>
        )}
      </Panel>
    </div>
  );
}

/**
 * One candidate as the responsible member sees them: their jobs to work on, and their details,
 * job-search profile, resume and activity to read. Nothing about the candidate can be changed here.
 */
export function MemberCandidateView({ candidateId }: { candidateId: string }) {
  const candidate = useCandidate(candidateId);
  const listState = useUrlState(DETAIL_DEFAULTS);
  const [state, setState] = listState;
  const tab = TABS.some((item) => item.value === state.tab) ? state.tab : "jobs";

  if (candidate.isPending) {
    return (
      <>
        <PageHeader title="Candidate" back={BACK} />
        <LoadingSkeleton variant="detail" count={4} />
      </>
    );
  }
  if (candidate.isError) {
    return (
      <>
        <PageHeader title="Candidate" back={BACK} />
        <div className="rounded-lg border bg-card">
          <ErrorState
            error={candidate.error}
            onRetry={() => candidate.refetch()}
            unavailable={{
              title: "This candidate is not assigned to you",
              description:
                "The candidate may have been assigned to another member, or the link is wrong. The candidates you are responsible for are under My Candidates.",
            }}
            action={
              <Button asChild variant="outline" size="sm">
                <Link href={BACK.href}>Go to My Candidates</Link>
              </Button>
            }
          />
        </div>
      </>
    );
  }

  const data = candidate.data;
  const here = memberCandidateHref(data.id);
  return (
    <>
      <PageHeader
        back={BACK}
        title={data.name}
        meta={
          <>
            <CandidateInactiveTag candidate={data} />
            <ResumeTag hasResume={data.has_resume} />
          </>
        }
        className="mb-4"
      />
      <CandidateContact candidate={data} className="mb-6 text-sm sm:flex-row sm:flex-wrap sm:gap-x-6" />

      <AssignmentKpis
        label={`Job figures for ${data.name}`}
        stats={data.stats}
        loading={false}
        hrefs={{
          assigned: here,
          not_applied: `${here}?applied=false`,
          applied: `${here}?applied=true`,
          interviews: `${here}?status=INTERVIEWING`,
          offers: `${here}?status=OFFER`,
        }}
      />

      <Tabs value={tab} onValueChange={(next) => setState({ tab: next })} className="mt-6 gap-5">
        <TabsList variant="line" className="w-full justify-start overflow-x-auto border-b">
          {TABS.map((item) => (
            <TabsTrigger key={item.value} value={item.value} className="flex-none">
              {item.label}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="jobs">
          <Watermarked>
            <MemberAssignmentList
              listState={listState}
              candidateId={data.id}
              empty={
                <NoJobsYet
                  description={`No job has been assigned to ${data.name} yet. Jobs appear here as soon as your Team Lead assigns them.`}
                />
              }
            />
          </Watermarked>
        </TabsContent>

        <TabsContent value="details">
          <CandidateDetails candidate={data} />
        </TabsContent>

        <TabsContent value="profile">
          <Panel
            title="Job-search profile"
            description="What searches for this candidate look for. Use it to answer application questions the way the candidate would."
          >
            <ProfileFacts candidateId={data.id} />
          </Panel>
        </TabsContent>

        <TabsContent value="resume">
          <Panel title="Resume" description="The file to attach when you apply, and its text to copy from.">
            <ResumeView candidateId={data.id} />
          </Panel>
        </TabsContent>

        <TabsContent value="activity">
          <Panel title="Activity" description="What happened to this candidate's jobs: assignments, views, opened postings and every status change.">
            <ActivityFeed candidateId={data.id} own jobHref={(assignmentId) => memberJobHref(assignmentId, data.id)} />
          </Panel>
        </TabsContent>
      </Tabs>
    </>
  );
}
