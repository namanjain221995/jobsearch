"use client";

import Link from "next/link";
import { useState } from "react";
import { Search } from "lucide-react";

import { ResumeTag } from "@/components/people/member-table";
import { ProfileCompletion, ProfileForm } from "@/components/people/profile-form";
import { ResumeUpload } from "@/components/people/resume-upload";
import { UserActions, UserStatusTag } from "@/components/people/user-actions";
import { PageHeader, Panel } from "@/components/shared/page-header";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { useArea } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ActivityFeed } from "@/components/views/activity-feed";
import { AssignmentDrawer } from "@/components/views/assignment-detail";
import { ASSIGNMENT_LIST_DEFAULTS, AssignmentsTable } from "@/components/views/assignments";
import { MemberAnalyticsPanels } from "@/components/views/member-dashboard";
import { SEARCH_LIST_DEFAULTS, SearchHistoryTable } from "@/components/views/search-history";
import { formatDateTime } from "@/lib/format";
import { useLocalListState } from "@/lib/list-state";
import { useProfile, useUser } from "@/lib/queries";
import { useUrlState } from "@/lib/url-state";

const TAB_DEFAULTS = { tab: "overview" };
const TABS = [
  { value: "overview", label: "Overview" },
  { value: "assignments", label: "Assignments" },
  { value: "profile", label: "Profile and Resume" },
  { value: "activity", label: "Activity" },
  { value: "searches", label: "Searches" },
];

function Completion({ memberId }: { memberId: string }) {
  const profile = useProfile(memberId);
  if (profile.isPending) return <LoadingSkeleton variant="form" count={1} />;
  if (profile.isError) return <ErrorState compact error={profile.error} onRetry={() => profile.refetch()} />;
  return <ProfileCompletion profile={profile.data} />;
}

function MemberAssignments({ memberId }: { memberId: string }) {
  const listState = useLocalListState(ASSIGNMENT_LIST_DEFAULTS);
  return <AssignmentsTable listState={listState} memberId={memberId} compact />;
}

function MemberSearches({ memberId }: { memberId: string }) {
  const listState = useLocalListState(SEARCH_LIST_DEFAULTS);
  return <SearchHistoryTable listState={listState} memberId={memberId} showFilters={false} />;
}

/**
 * One member as their Team Lead or an Admin sees them: figures and charts,
 * assignments, the profile and resume (both editable on the member's behalf),
 * the activity timeline and the searches run for them.
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
          <ErrorState error={new Error(`${member.name} is not a member, so there is no job pipeline to show.`)} />
        </div>
      </>
    );
  }

  return (
    <>
      <PageHeader
        back={back}
        title={member.name}
        meta={
          <>
            <UserStatusTag status={member.status} />
            <ResumeTag hasResume={member.has_resume} />
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
            {member.is_active ? (
              <Button asChild>
                <Link href={`${area}/search?member=${member.id}`}>
                  <Search data-icon="inline-start" />
                  Find Jobs for {member.name.split(" ")[0]}
                </Link>
              </Button>
            ) : (
              <Button disabled title="A deactivated member cannot be searched for">
                <Search data-icon="inline-start" />
                Find Jobs for {member.name.split(" ")[0]}
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

        <TabsContent value="overview">
          <MemberAnalyticsPanels memberId={member.id} embedded />
        </TabsContent>

        <TabsContent value="assignments">
          <MemberAssignments memberId={member.id} />
        </TabsContent>

        <TabsContent value="profile">
          <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_22rem]">
            <div className="flex min-w-0 flex-col gap-5">
              <Panel title="Job preferences" description="What searches for this member look for.">
                <ProfileForm memberId={member.id} onBehalfOf={member.name} />
              </Panel>
              <Panel title="Resume" description="PDF, DOCX or TXT. Uploading a new file replaces the current one.">
                <ResumeUpload memberId={member.id} onBehalfOf={member.name} />
              </Panel>
            </div>
            <div className="flex min-w-0 flex-col gap-5 max-xl:order-first">
              <Panel title="Profile completion">
                <Completion memberId={member.id} />
              </Panel>
            </div>
          </div>
        </TabsContent>

        <TabsContent value="activity">
          <Panel>
            <ActivityFeed memberId={member.id} onOpenAssignment={setOpened} />
          </Panel>
        </TabsContent>

        <TabsContent value="searches">
          <MemberSearches memberId={member.id} />
        </TabsContent>
      </Tabs>

      <AssignmentDrawer assignmentId={opened} onOpenChange={(open) => !open && setOpened(null)} />
    </>
  );
}
