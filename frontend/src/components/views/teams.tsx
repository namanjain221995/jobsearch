"use client";

import Link from "next/link";
import { useState } from "react";
import { Ellipsis, MailPlus, Pencil, Plus, Power, PowerOff, UserCog, UserMinus, UsersRound } from "lucide-react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { toast } from "sonner";
import { z } from "zod";

import { InvitationDialog, type InvitationPreset } from "@/components/people/invitation-dialog";
import { CompletionMeter, MEMBER_LIST_DEFAULTS, MemberTable } from "@/components/people/member-table";
import { UserStatusTag } from "@/components/people/user-actions";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { DataTable, type Column } from "@/components/shared/data-table";
import { FormField, FormMessage, TextareaField, TextField } from "@/components/shared/form-fields";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ASSIGNMENT_LIST_DEFAULTS, AssignmentsTable } from "@/components/views/assignments";
import { TeamQueue } from "@/components/views/jobs";
import { SEARCH_LIST_DEFAULTS, SearchHistoryTable } from "@/components/views/search-history";
import { TeamAnalyticsPanels } from "@/components/views/team-analytics";
import { ApiError, errorMessage } from "@/lib/api";
import { formatDate, plural } from "@/lib/format";
import { applyServerErrors } from "@/lib/forms";
import { useLocalListState } from "@/lib/list-state";
import { useCreateTeam, useTeam, useTeams, useUpdateTeam, useUsers } from "@/lib/queries";
import type { Team } from "@/lib/types";
import { useUrlState } from "@/lib/url-state";

const teamSchema = z.object({
  name: z.string().trim().min(2, "Enter at least 2 characters.").max(120, "Use at most 120 characters."),
  description: z.string().trim().max(2000, "Use at most 2,000 characters."),
});
type TeamValues = z.infer<typeof teamSchema>;

/** Create a team, or rename one and edit its description. */
function TeamFormDialog({ team, open, onOpenChange }: { team?: Team; open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">{open && <TeamForm team={team} onClose={() => onOpenChange(false)} />}</DialogContent>
    </Dialog>
  );
}

function TeamForm({ team, onClose }: { team?: Team; onClose: () => void }) {
  const create = useCreateTeam();
  const update = useUpdateTeam();
  const mutation = team ? update : create;
  const form = useForm<TeamValues>({
    resolver: zodResolver(teamSchema),
    defaultValues: { name: team?.name ?? "", description: team?.description ?? "" },
  });
  const onError = (error: unknown) => {
    // 409: a team with this name already exists.
    if (error instanceof ApiError && error.status === 409) form.setError("name", { type: "server", message: error.message });
    else applyServerErrors(form, error);
  };
  const onSubmit = form.handleSubmit((values) => {
    if (team) {
      update.mutate(
        { id: team.id, body: values },
        {
          onSuccess: () => {
            toast.success("Team saved");
            onClose();
          },
          onError,
        },
      );
    } else {
      create.mutate(values, {
        onSuccess: (saved) => {
          toast.success(`Team ${saved.name} created`, { description: "It has no Team Lead yet. Create one from the team's menu." });
          onClose();
        },
        onError,
      });
    }
  });
  const failure =
    mutation.error && !(mutation.error instanceof ApiError && (mutation.error.status === 409 || Object.keys(mutation.error.fields).length > 0));

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
      <DialogHeader>
        <DialogTitle className="text-lg">{team ? "Edit Team" : "Create Team"}</DialogTitle>
        <DialogDescription>
          {team ? "Change the name or the description. Members and history are not affected." : "A team groups members under one Team Lead."}
        </DialogDescription>
      </DialogHeader>
      {failure && <FormMessage>{errorMessage(mutation.error)}</FormMessage>}
      <TextField label="Team name" autoComplete="off" error={form.formState.errors.name?.message} {...form.register("name")} />
      <TextareaField label="Description" optional rows={3} error={form.formState.errors.description?.message} {...form.register("description")} />
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose} disabled={mutation.isPending}>
          Cancel
        </Button>
        <Button type="submit" disabled={mutation.isPending}>
          {mutation.isPending && <Spinner data-icon="inline-start" />}
          {team ? "Save Team" : "Create Team"}
        </Button>
      </DialogFooter>
    </form>
  );
}

/** Make an existing Team Lead the lead of this team (they must not be leading another one). */
function ChangeLeadDialog({ team, onClose }: { team: Team; onClose: () => void }) {
  const leads = useUsers({ role: "TEAM_LEAD", sort: "name", page: 1, page_size: 200 }, { refetchInterval: false });
  const update = useUpdateTeam();
  const [leadId, setLeadId] = useState("");
  const options = (leads.data?.items ?? []).filter((lead) => lead.is_active && lead.id !== team.team_lead?.id);

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-lg">{team.team_lead ? "Change the Team Lead" : "Assign a Team Lead"}</DialogTitle>
          <DialogDescription>
            {team.team_lead
              ? `${team.team_lead.name} stops leading ${team.name} and keeps their account. The new Team Lead manages the team's members, queue and searches.`
              : `Choose who leads ${team.name}.`}
          </DialogDescription>
        </DialogHeader>
        {update.error && <FormMessage>{errorMessage(update.error)}</FormMessage>}
        <FormField
          label="Team Lead"
          description={
            leads.isSuccess && options.length === 0
              ? "There is no other Team Lead account. Create one with Create Team Lead."
              : "A Team Lead can lead one team. Someone who already leads another team has to be removed from it first."
          }
        >
          {({ id, describedBy }) => (
            <Select value={leadId} onValueChange={setLeadId} disabled={leads.isPending}>
              <SelectTrigger id={id} aria-describedby={describedBy} className="w-full">
                <SelectValue placeholder={leads.isPending ? "Loading Team Leads" : "Choose a Team Lead"} />
              </SelectTrigger>
              <SelectContent>
                {options.map((lead) => (
                  <SelectItem key={lead.id} value={lead.id}>
                    {lead.name}
                    {lead.team ? ` (leads ${lead.team.name})` : " (no team)"}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </FormField>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={update.isPending}>
            Cancel
          </Button>
          <Button
            disabled={!leadId || update.isPending}
            onClick={() =>
              update.mutate(
                { id: team.id, body: { team_lead_id: leadId } },
                {
                  onSuccess: (saved) => {
                    toast.success(`${saved.team_lead?.name ?? "The Team Lead"} now leads ${saved.name}`);
                    onClose();
                  },
                },
              )
            }
          >
            {update.isPending && <Spinner data-icon="inline-start" />}
            {team.team_lead ? "Change Team Lead" : "Assign Team Lead"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

type TeamAction = "edit" | "lead" | "remove-lead" | "deactivate" | "activate" | "invite-lead" | null;

/** Everything an Admin can do to a team, from its row in the list or its own page. */
export function TeamActions({ team, variant = "menu" }: { team: Team; variant?: "menu" | "buttons" }) {
  const update = useUpdateTeam();
  const [action, setAction] = useState<TeamAction>(null);
  const close = () => setAction(null);

  const patch = (body: Parameters<typeof update.mutate>[0]["body"], message: string) =>
    update.mutate(
      { id: team.id, body },
      {
        onSuccess: () => {
          toast.success(message);
          close();
        },
        onError: (error) => toast.error(errorMessage(error)),
      },
    );

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          {variant === "menu" ? (
            <Button variant="ghost" size="icon-sm" aria-label={`Actions for ${team.name}`}>
              <Ellipsis />
            </Button>
          ) : (
            <Button variant="outline">
              Manage Team
              <Ellipsis data-icon="inline-end" />
            </Button>
          )}
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="min-w-56">
          <DropdownMenuItem onSelect={() => setAction("edit")}>
            <Pencil />
            Rename or Edit
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          {!team.team_lead && (
            <DropdownMenuItem onSelect={() => setAction("invite-lead")}>
              <MailPlus />
              Create Team Lead
            </DropdownMenuItem>
          )}
          <DropdownMenuItem onSelect={() => setAction("lead")}>
            <UserCog />
            {team.team_lead ? "Change Team Lead" : "Assign Existing Team Lead"}
          </DropdownMenuItem>
          {team.team_lead && (
            <DropdownMenuItem variant="destructive" onSelect={() => setAction("remove-lead")}>
              <UserMinus />
              Remove Team Lead
            </DropdownMenuItem>
          )}
          <DropdownMenuSeparator />
          {team.is_active ? (
            <DropdownMenuItem variant="destructive" onSelect={() => setAction("deactivate")}>
              <PowerOff />
              Deactivate Team
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem onSelect={() => setAction("activate")}>
              <Power />
              Reactivate Team
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>

      <TeamFormDialog team={team} open={action === "edit"} onOpenChange={(open) => !open && close()} />
      {action === "lead" && <ChangeLeadDialog team={team} onClose={close} />}
      <InvitationDialog
        open={action === "invite-lead"}
        onOpenChange={(open) => !open && close()}
        preset={{ role: "TEAM_LEAD", teamId: team.id }}
        title={`Create Team Lead for ${team.name}`}
      />
      <ConfirmDialog
        open={action === "remove-lead"}
        onOpenChange={(open) => !open && close()}
        title={`Remove ${team.team_lead?.name ?? "the Team Lead"} from ${team.name}?`}
        description="They keep their account but no longer see this team. The team has no Team Lead until you assign one, so its queue is not distributed in the meantime."
        confirmLabel="Remove Team Lead"
        destructive
        pending={update.isPending}
        onConfirm={() => patch({ remove_team_lead: true }, "Team Lead removed")}
      />
      <ConfirmDialog
        open={action === "deactivate"}
        onOpenChange={(open) => !open && close()}
        title={`Deactivate ${team.name}?`}
        description="No new members can be invited into it and no jobs can be sent to its queue. Members, assignments and history stay as they are, and you can reactivate the team at any time."
        confirmLabel="Deactivate team"
        destructive
        pending={update.isPending}
        onConfirm={() => patch({ is_active: false }, `${team.name} deactivated`)}
      />
      <ConfirmDialog
        open={action === "activate"}
        onOpenChange={(open) => !open && close()}
        title={`Reactivate ${team.name}?`}
        description="Members can be invited into it and jobs can be sent to its queue again."
        confirmLabel="Reactivate team"
        pending={update.isPending}
        onConfirm={() => patch({ is_active: true }, `${team.name} reactivated`)}
      />
    </>
  );
}

function LeadCell({ team }: { team: Team }) {
  if (!team.team_lead) return <span className="text-muted-foreground">No Team Lead</span>;
  return (
    <div className="flex flex-col gap-0.5">
      <span className="flex flex-wrap items-center gap-2">
        {team.team_lead.name}
        {team.team_lead_status && team.team_lead_status !== "active" && <UserStatusTag status={team.team_lead_status} />}
      </span>
      <span className="text-2sm break-all text-muted-foreground">{team.team_lead.email}</span>
    </div>
  );
}

/** Admin: every team, with its lead, size and figures, and the ways to change them. */
export function TeamsView() {
  const teams = useTeams(true);
  const [creating, setCreating] = useState(false);
  const [inviting, setInviting] = useState<InvitationPreset | null>(null);

  const columns: Column<Team>[] = [
    {
      id: "team",
      header: "Team",
      sortValue: (team) => team.name,
      cell: (team) => (
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="flex flex-wrap items-center gap-2">
            <Link href={`/admin/teams/${team.id}`} className="rounded-sm font-medium underline-offset-4 hover:underline">
              {team.name}
            </Link>
            {!team.is_active && <Tag>Deactivated</Tag>}
          </span>
          {team.description && <span className="line-clamp-1 max-w-72 text-2sm text-muted-foreground">{team.description}</span>}
        </div>
      ),
    },
    { id: "lead", header: "Team Lead", sortValue: (team) => team.team_lead?.name ?? "", cell: (team) => <LeadCell team={team} /> },
    { id: "members", header: "Members", align: "right", sortValue: (team) => team.member_count, cell: (team) => team.member_count.toLocaleString() },
    { id: "queue", header: "In queue", align: "right", sortValue: (team) => team.queue_pending, cell: (team) => team.queue_pending.toLocaleString() },
    { id: "assigned", header: "Assigned", align: "right", sortValue: (team) => team.stats.assigned, cell: (team) => team.stats.assigned.toLocaleString() },
    { id: "applied", header: "Applied", align: "right", sortValue: (team) => team.stats.applied, cell: (team) => team.stats.applied.toLocaleString() },
    { id: "completion", header: "Completion rate", sortValue: (team) => team.stats.completion_rate, cell: (team) => <CompletionMeter rate={team.stats.completion_rate} /> },
    { id: "created", header: "Created", sortValue: (team) => team.created_at, cell: (team) => <span className="whitespace-nowrap">{formatDate(team.created_at)}</span> },
    { id: "actions", header: <span className="sr-only">Actions</span>, align: "right", cell: (team) => <TeamActions team={team} /> },
  ];

  return (
    <>
      <PageHeader
        title="Teams"
        description="Each team has one Team Lead and any number of members. Create teams, give them a lead, and open one to see its people and pipeline."
        actions={
          <>
            <Button variant="outline" onClick={() => setInviting({ role: "TEAM_LEAD" })}>
              <MailPlus data-icon="inline-start" />
              Create Team Lead
            </Button>
            <Button onClick={() => setCreating(true)}>
              <Plus data-icon="inline-start" />
              Create Team
            </Button>
          </>
        }
      />
      <DataTable
        caption="Teams"
        columns={columns}
        rows={teams.data}
        rowKey={(team) => team.id}
        loading={teams.isPending}
        error={teams.error}
        onRetry={() => teams.refetch()}
        defaultSort={{ id: "team", direction: "asc" }}
        rowClassName={(team) => (team.is_active ? undefined : "text-muted-foreground")}
        empty={
          <EmptyState
            icon={UsersRound}
            title="No teams yet"
            description="Create the first team, then give it a Team Lead."
            action={
              <Button size="sm" onClick={() => setCreating(true)}>
                Create Team
              </Button>
            }
          />
        }
        mobileCard={(team) => (
          <div className="flex flex-col gap-2.5">
            <div className="flex items-start justify-between gap-2">
              <div className="flex min-w-0 flex-col gap-0.5">
                <span className="flex flex-wrap items-center gap-2">
                  <Link href={`/admin/teams/${team.id}`} className="rounded-sm font-medium underline-offset-4 hover:underline">
                    {team.name}
                  </Link>
                  {!team.is_active && <Tag>Deactivated</Tag>}
                </span>
                <span className="text-2sm text-muted-foreground">
                  {team.team_lead ? `Led by ${team.team_lead.name}` : "No Team Lead"}, {plural(team.member_count, "member")}
                </span>
              </div>
              <TeamActions team={team} />
            </div>
            <p className="text-2sm text-muted-foreground tabular-nums">
              {team.stats.assigned} assigned, {team.stats.applied} applied, {team.queue_pending} waiting in the queue
            </p>
            <CompletionMeter rate={team.stats.completion_rate} className="text-sm" />
          </div>
        )}
      />
      <TeamFormDialog open={creating} onOpenChange={setCreating} />
      <InvitationDialog open={inviting !== null} onOpenChange={(open) => !open && setInviting(null)} preset={inviting ?? {}} />
    </>
  );
}

/* ------------------------------------------------------------------ one team */

const TEAM_TABS = [
  { value: "overview", label: "Overview" },
  { value: "members", label: "Members" },
  { value: "assignments", label: "Assignments" },
  { value: "queue", label: "Queue" },
  { value: "searches", label: "Searches" },
];
const TEAM_TAB_DEFAULTS = { tab: "overview" };

function TeamMembersTab({ team }: { team: Team }) {
  const listState = useLocalListState(MEMBER_LIST_DEFAULTS);
  const [inviting, setInviting] = useState(false);
  const add = (
    <Button onClick={() => setInviting(true)} disabled={!team.is_active} title={team.is_active ? undefined : "This team is deactivated"}>
      <MailPlus data-icon="inline-start" />
      Add Member
    </Button>
  );
  return (
    <>
      <MemberTable listState={listState} teamId={team.id} memberHref={(id) => `/admin/members/${id}`} actions={add} emptyAction={add} />
      <InvitationDialog open={inviting} onOpenChange={setInviting} preset={{ role: "MEMBER", teamId: team.id }} title={`Add Member to ${team.name}`} />
    </>
  );
}

function TeamAssignmentsTab({ teamId }: { teamId: string }) {
  const listState = useLocalListState(ASSIGNMENT_LIST_DEFAULTS);
  return <AssignmentsTable listState={listState} teamId={teamId} />;
}

function TeamSearchesTab({ teamId }: { teamId: string }) {
  const listState = useLocalListState(SEARCH_LIST_DEFAULTS);
  return <SearchHistoryTable listState={listState} teamId={teamId} />;
}

/** Admin: one team's lead, members, assignments, queue, searches and analytics. */
export function TeamDetailView({ teamId }: { teamId: string }) {
  const team = useTeam(teamId);
  const [state, setState] = useUrlState(TEAM_TAB_DEFAULTS);
  const tab = TEAM_TABS.some((item) => item.value === state.tab) ? state.tab : "overview";
  const back = { href: "/admin/teams", label: "Teams" };

  if (team.isPending) {
    return (
      <>
        <PageHeader title="Team" back={back} />
        <LoadingSkeleton variant="detail" count={4} />
      </>
    );
  }
  if (team.isError) {
    return (
      <>
        <PageHeader title="Team" back={back} />
        <div className="rounded-lg border bg-card">
          <ErrorState error={team.error} onRetry={() => team.refetch()} />
        </div>
      </>
    );
  }
  const data = team.data;

  return (
    <>
      <PageHeader
        back={back}
        title={data.name}
        meta={!data.is_active && <Tag>Deactivated</Tag>}
        description={
          <>
            {data.team_lead ? (
              <>
                Led by <span className="text-foreground">{data.team_lead.name}</span>
                {data.team_lead_status && data.team_lead_status !== "active" ? ` (${data.team_lead_status})` : ""}.
              </>
            ) : (
              <span className="text-warning">No Team Lead. Jobs sent to this team wait in its queue.</span>
            )}{" "}
            {plural(data.member_count, "active member")}, {plural(data.queue_pending, "job")} waiting in the queue.
            {data.description ? ` ${data.description}` : ""}
          </>
        }
        actions={<TeamActions team={data} variant="buttons" />}
      />
      <Tabs value={tab} onValueChange={(next) => setState({ tab: next })} className="gap-5">
        <TabsList variant="line" className="w-full justify-start overflow-x-auto border-b">
          {TEAM_TABS.map((item) => (
            <TabsTrigger key={item.value} value={item.value} className="flex-none">
              {item.label}
              {item.value === "queue" && data.queue_pending > 0 && (
                <span className="rounded-sm bg-primary/12 px-1 text-xs tabular-nums">{data.queue_pending}</span>
              )}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="overview">
          <TeamAnalyticsPanels teamId={data.id} variant="analytics" />
        </TabsContent>
        <TabsContent value="members">
          <TeamMembersTab team={data} />
        </TabsContent>
        <TabsContent value="assignments">
          <TeamAssignmentsTab teamId={data.id} />
        </TabsContent>
        <TabsContent value="queue">
          <TeamQueue teamId={data.id} />
        </TabsContent>
        <TabsContent value="searches">
          <TeamSearchesTab teamId={data.id} />
        </TabsContent>
      </Tabs>
    </>
  );
}
