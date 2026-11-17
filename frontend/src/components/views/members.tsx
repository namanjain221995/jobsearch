"use client";

import { useState } from "react";
import { MailPlus } from "lucide-react";

import { InvitationDialog } from "@/components/people/invitation-dialog";
import { MEMBER_LIST_DEFAULTS, MemberTable } from "@/components/people/member-table";
import { PageHeader } from "@/components/shared/page-header";
import { ErrorState } from "@/components/shared/states";
import { useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { useUrlState } from "@/lib/url-state";

/** Team Lead: the members of their own team. */
export function TeamMembersView() {
  const me = useMe();
  const listState = useUrlState(MEMBER_LIST_DEFAULTS);
  const [inviting, setInviting] = useState(false);
  const add = (
    <Button onClick={() => setInviting(true)}>
      <MailPlus data-icon="inline-start" />
      Add Member
    </Button>
  );
  return (
    <>
      <PageHeader
        title="Team Members"
        description="The members of your team: how many candidates each is responsible for, and how far they are with those candidates' jobs."
        actions={add}
      />
      {me.team ? (
        <MemberTable
          listState={listState}
          teamId={me.team.id}
          memberHref={(id) => `/tl/members/${id}`}
          candidatesHref={(user) => `/tl/candidates?member=${user.id}`}
          emptyAction={add}
        />
      ) : (
        <div className="rounded-lg border bg-card">
          <ErrorState error={new Error("You are not assigned to a team yet. Ask an Admin to make you the Team Lead of a team.")} />
        </div>
      )}
      <InvitationDialog open={inviting} onOpenChange={setInviting} preset={{ role: "MEMBER" }} />
    </>
  );
}

/** Admin: every member, across teams. */
export function AdminMembersView() {
  const listState = useUrlState(MEMBER_LIST_DEFAULTS);
  const [inviting, setInviting] = useState(false);
  const add = (
    <Button onClick={() => setInviting(true)}>
      <MailPlus data-icon="inline-start" />
      Add Member
    </Button>
  );
  return (
    <>
      <PageHeader
        title="Members"
        description="Every member in every team: the candidates each is responsible for, and the figures over those candidates' jobs."
        actions={add}
      />
      <MemberTable
        listState={listState}
        showTeam
        memberHref={(id) => `/admin/members/${id}`}
        candidatesHref={(user) => `/admin/candidates?${user.team ? `team=${user.team.id}&` : ""}member=${user.id}`}
        emptyAction={add}
      />
      <InvitationDialog open={inviting} onOpenChange={setInviting} preset={{ role: "MEMBER" }} />
    </>
  );
}
