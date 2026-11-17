"use client";

import { PageHeader, Panel } from "@/components/shared/page-header";
import { useMe } from "@/components/shell/me-context";
import { ActivityFeed } from "@/components/views/activity-feed";

export function MemberActivityView() {
  const me = useMe();
  return (
    <>
      <PageHeader
        title="Activity"
        description="A record of what happened to your jobs: assignments, views, opened postings and every status change."
      />
      <Panel>
        <ActivityFeed memberId={me.id} own jobHref={(id) => `/member/jobs/${id}`} />
      </Panel>
    </>
  );
}
