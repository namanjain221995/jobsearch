"use client";

import { PageHeader, Panel } from "@/components/shared/page-header";
import { useMe } from "@/components/shell/me-context";
import { ActivityFeed } from "@/components/views/activity-feed";
import { memberJobHref } from "@/lib/member-candidates";

export function MemberActivityView() {
  const me = useMe();
  return (
    <>
      <PageHeader
        title="Activity"
        description="A record of what happened to the jobs of your candidates: assignments, views, opened postings and every status change."
      />
      <Panel>
        <ActivityFeed memberId={me.id} own jobHref={(assignmentId) => memberJobHref(assignmentId)} />
      </Panel>
    </>
  );
}
