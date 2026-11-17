"use client";

import Link from "next/link";
import { useState } from "react";
import { Contact } from "lucide-react";

import { CANDIDATE_LIST_DEFAULTS, CandidateTable } from "@/components/candidates/candidate-table";
import { UserActions, UserStatusTag } from "@/components/people/user-actions";
import { PageHeader, Panel } from "@/components/shared/page-header";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { useArea } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ActivityFeed } from "@/components/views/activity-feed";
import { AssignmentDrawer } from "@/components/views/assignment-detail";
import { ASSIGNMENT_LIST_DEFAULTS, AssignmentsTable } from "@/components/views/assignments";
import { MemberAnalyticsPanels } from "@/components/views/member-dashboard";
import { formatDateTime, plural } from "@/lib/format";
import { useLocalListState } from "@/lib/list-state";
import { useUser } from "@/lib/queries";
import { useUrlState } from "@/lib/url-state";

const TAB_DEFAULTS = { tab: "overview" };
const TABS = [
  { value: "overview", label: "Overview" },
  { value: "candidates", label: "Candidates" },
  { value: "jobs", label: "Jobs" },
  { value: "activity", label: "Activity" },
];

function MemberCandidates({ memberId }: { memberId: string }) {
  const listState = useLocalListState(CANDIDATE_LIST_DEFAULTS);
  return <CandidateTable listState={listState} memberId={memberId} />;
}

function MemberJobs({ memberId }: { memberId: string }) {
  const listState = useLocalListState(ASSIGNMENT_LIST_DEFAULTS);
  return <AssignmentsTable listState={listState} memberId={memberId} compact />;
}

/**
 * One member as their Team Lead or an Admin sees them. A member is an employee: the page
 * shows the candidates they are responsible for today, the totals over those candidates'
 * jobs, the jobs themselves and what happened to them. Profiles and resumes belong to the
 * candidates and are on the candidates' pages.
 */
export function MemberDetailView({ memberId }: { memberId: string }) {
  const area = useArea();
  const user = useUser(memberId);
  const [state, setState] = useUrlState(TAB_DEFAULTS);
  const [opened, setOpened] = useState<string | null>(null);
  const back = { href: `${area}/members`, label: area === "/admin" ? "Members" : "Team Members" };
  const tab = TABS.some((item) => item.value === state.tab) ? state.tab : "overview";

  if (user.isPending) {
    return (
      <>
        <PageHeader title="Member" back={back} />
        <LoadingSkeleton variant="detail" count={4} />
      </>
    );
  }
  if (user.isError) {
    return (
      <>
        <PageHeader title="Member" back={back} />
        <div className="rounded-lg border bg-card">
          <ErrorState error={user.error} onRetry={() => user.refetch()} />
        </div>
      </>
    );
  }
  const member = user.data;
  if (member.role !== "MEMBER") {
    return (
      <>
        <PageHeader title={member.name} back={back} />
        <div className="rounded-lg border bg-card">
          <ErrorState error={new Error(`${member.name} is not a member, so there are no candidates to show.`)} />
        </div>
      </>
    );
  }
  const candidatesHref = `${area}/candidates?${area === "/admin" && member.team ? `team=${member.team.id}&` : ""}member=${member.id}`;

  return (
    <>
      <PageHeader
        back={back}
        title={member.name}
        meta={
          <>
            <UserStatusTag status={member.status} />
            <Tag tone={member.candidate_count > 0 ? "primary" : "neutral"}>
              <Contact aria-hidden="true" />
              {plural(member.candidate_count, "candidate")}
            </Tag>
          </>
        }
        description={
          <>
            {member.email}
            {member.team ? `, ${member.team.name}` : ", no team"}.{" "}
            {member.last_activity_at ? `Last active ${formatDateTime(member.last_activity_at)}.` : "No activity yet."}
          </>
        }
        actions={
          <>
            <UserActions user={member} variant="buttons" />
            <Button asChild>
              <Link href={candidatesHref}>
                <Contact data-icon="inline-start" />
                Candidates of {member.name.split(" ")[0]}
              </Link>
            </Button>
          </>
        }
      />

      <Tabs value={tab} onValueChange={(next) => setState({ tab: next })} className="gap-5">
        <TabsList variant="line" className="w-full justify-start overflow-x-auto border-b">
          {TABS.map((item) => (
            <TabsTrigger key={item.value} value={item.value} className="flex-none">
              {item.label}
              {item.value === "candidates" && member.candidate_count > 0 && (
                <span className="rounded-sm bg-primary/12 px-1 text-xs tabular-nums">{member.candidate_count}</span>
              )}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="overview">
          <MemberAnalyticsPanels memberId={member.id} embedded />
        </TabsContent>

        <TabsContent value="candidates">
          <MemberCandidates memberId={member.id} />
        </TabsContent>

        <TabsContent value="jobs">
          <MemberJobs memberId={member.id} />
        </TabsContent>

        <TabsContent value="activity">
          <Panel description="What happened to the jobs of the candidates this member is responsible for today.">
            <ActivityFeed memberId={member.id} onOpenAssignment={setOpened} />
          </Panel>
        </TabsContent>
      </Tabs>

      <AssignmentDrawer assignmentId={opened} onOpenChange={(open) => !open && setOpened(null)} />
    </>
  );
}
