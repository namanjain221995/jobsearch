"use client";

import Link from "next/link";
import { useState } from "react";
import { MailPlus, SearchX, UserCog } from "lucide-react";

import { InvitationDialog } from "@/components/people/invitation-dialog";
import { UserActions, UserStatusTag } from "@/components/people/user-actions";
import { DataTable, type Column } from "@/components/shared/data-table";
import { FilterBar, FilterSelect } from "@/components/shared/filters";
import { PageHeader } from "@/components/shared/page-header";
import { SearchInput } from "@/components/shared/search-input";
import { EmptyState } from "@/components/shared/states";
import { Button } from "@/components/ui/button";
import { formatDate, formatDateTime } from "@/lib/format";
import { useUsers } from "@/lib/queries";
import type { User } from "@/lib/types";
import { toPositiveInt, useUrlState } from "@/lib/url-state";

const DEFAULTS = { q: "", status: "", sort: "name", page: "1", page_size: "25" };

/** Admin: every Team Lead account, the team each one leads, and when they last signed in. */
export function TeamLeadsView() {
  const [state, setState] = useUrlState(DEFAULTS);
  const [inviting, setInviting] = useState(false);
  const leads = useUsers({
    role: "TEAM_LEAD",
    q: state.q || undefined,
    status: state.status || undefined,
    sort: state.sort,
    page: toPositiveInt(state.page, 1),
    page_size: toPositiveInt(state.page_size, 25),
  });
  const filtered = Boolean(state.q || state.status);
  const create = (
    <Button onClick={() => setInviting(true)}>
      <MailPlus data-icon="inline-start" />
      Create Team Lead
    </Button>
  );

  const nameCell = (user: User) => (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="font-medium">{user.name}</span>
      <span className="text-2sm break-all text-muted-foreground">{user.email}</span>
    </div>
  );
  const teamCell = (user: User) =>
    user.team ? (
      <Link href={`/admin/teams/${user.team.id}`} className="rounded-sm underline-offset-4 hover:underline">
        {user.team.name}
      </Link>
    ) : (
      <span className="text-muted-foreground">Leads no team</span>
    );

  const columns: Column<User>[] = [
    { id: "name", header: "Team Lead", cell: nameCell },
    { id: "team", header: "Team", cell: teamCell },
    { id: "status", header: "Account", cell: (user) => <UserStatusTag status={user.status} /> },
    {
      id: "login",
      header: "Last sign-in",
      cell: (user) => <span className="whitespace-nowrap">{user.last_login_at ? formatDateTime(user.last_login_at) : "Never"}</span>,
    },
    { id: "created", header: "Created", cell: (user) => <span className="whitespace-nowrap">{formatDate(user.created_at)}</span> },
    { id: "actions", header: <span className="sr-only">Actions</span>, align: "right", cell: (user) => <UserActions user={user} /> },
  ];

  return (
    <>
      <PageHeader
        title="Team Leads"
        description="The people who run teams: they invite members, start searches and hand out jobs. To change which team someone leads, open Teams."
        actions={create}
      />
      <FilterBar canClear={filtered} onClear={() => setState({ q: "", status: "" })}>
        <SearchInput label="Search Team Leads" placeholder="Search name or e-mail" value={state.q} onChange={(q) => setState({ q })} />
        <FilterSelect
          label="Account status"
          allLabel="All accounts"
          value={state.status}
          onChange={(status) => setState({ status })}
          options={[
            { value: "active", label: "Active" },
            { value: "invited", label: "Invited" },
            { value: "deactivated", label: "Deactivated" },
          ]}
        />
        <FilterSelect
          label="Sort"
          value={state.sort}
          onChange={(sort) => setState({ sort })}
          options={[
            { value: "name", label: "Name, A to Z" },
            { value: "recent", label: "Newest account first" },
          ]}
        />
      </FilterBar>
      <DataTable
        caption="Team Leads"
        columns={columns}
        rows={leads.data?.items}
        rowKey={(user) => user.id}
        loading={leads.isPending}
        fetching={leads.isPlaceholderData}
        error={leads.error}
        onRetry={() => leads.refetch()}
        empty={
          filtered ? (
            <EmptyState icon={SearchX} title="No Team Leads match these filters" description="Check the spelling or clear the filters." />
          ) : (
            <EmptyState icon={UserCog} title="No Team Leads yet" description="Create a Team Lead to give a team someone to run it." action={create} />
          )
        }
        mobileCard={(user) => (
          <div className="flex flex-col gap-2">
            <div className="flex items-start justify-between gap-2">
              {nameCell(user)}
              <UserActions user={user} />
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
              {teamCell(user)}
              <UserStatusTag status={user.status} />
            </div>
            <p className="text-xs text-muted-foreground">
              Last sign-in: {user.last_login_at ? formatDateTime(user.last_login_at) : "never"}
            </p>
          </div>
        )}
        pagination={
          leads.data && {
            page: leads.data.page,
            pageSize: leads.data.page_size,
            total: leads.data.total,
            noun: "Team Leads",
            onPageChange: (page) => setState({ page: String(page) }),
            onPageSizeChange: (size) => setState({ page_size: String(size) }),
          }
        }
      />
      <InvitationDialog open={inviting} onOpenChange={setInviting} preset={{ role: "TEAM_LEAD" }} />
    </>
  );
}
