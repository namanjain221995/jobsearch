"use client";

import Link from "next/link";
import { useState } from "react";
import {
  BadgeCheck,
  BriefcaseBusiness,
  ClipboardCheck,
  Ellipsis,
  FileUp,
  FolderInput,
  Inbox,
  Link2,
  Plus,
  RotateCcw,
  SearchX,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";

import { AssignmentDialog, DelegateDialog, type JobRef } from "@/components/jobs/assignment-dialog";
import { AddJobDialog, CsvImportDialog, ImportUrlDialog } from "@/components/jobs/job-form";
import { JobDrawer } from "@/components/jobs/job-manager";
import { JobFactLine, ORIGIN_LABELS, VerificationTag } from "@/components/jobs/job-meta";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { DataTable, type Column } from "@/components/shared/data-table";
import { FilterBar, FilterSelect, SegmentedFilter } from "@/components/shared/filters";
import { PageHeader } from "@/components/shared/page-header";
import { SearchInput } from "@/components/shared/search-input";
import { EmptyState, ErrorState } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { useArea, useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { errorMessage } from "@/lib/api";
import { formatDate, humanize, plural } from "@/lib/format";
import type { ListState } from "@/lib/list-state";
import { JOB_WORK_MODES } from "@/lib/options";
import { useJobs, useTeamQueue, useUpdateQueueItem, useVerifyJob } from "@/lib/queries";
import type { JobListItem, JobListParams, QueueItem } from "@/lib/types";
import { toPositiveInt, useUrlState } from "@/lib/url-state";

/* ------------------------------------------------------------------ job postings */

export const JOB_LIST_DEFAULTS = {
  q: "",
  company: "",
  origin: "",
  verification: "",
  work_mode: "",
  assigned: "all",
  page: "1",
  page_size: "25",
};
type JobListState = typeof JOB_LIST_DEFAULTS;

const ORIGINS = Object.entries(ORIGIN_LABELS).map(([value, label]) => ({ value, label }));
const VERIFICATIONS = [
  { value: "verified", label: "Verified" },
  { value: "unverified", label: "Not verified" },
  { value: "failed", label: "Verification failed" },
  { value: "closed", label: "Posting closed" },
];

function VerifyAction({ job }: { job: JobListItem }) {
  const verify = useVerifyJob();
  return (
    <DropdownMenuItem
      disabled={verify.isPending}
      onSelect={(event) => {
        event.preventDefault();
        verify.mutate(job.id, {
          onSuccess: (saved) =>
            toast.message(`Checked: ${humanize(saved.verification_status)}`, { description: saved.verification_note || undefined }),
          onError: (error) => toast.error(errorMessage(error)),
        });
      }}
    >
      <BadgeCheck />
      {verify.isPending ? "Checking the posting" : "Verify Now"}
    </DropdownMenuItem>
  );
}

/**
 * Job postings. An Admin sees every posting; a Team Lead only those their team
 * has, was sent, or found (GET /api/jobs decides). One posting is one row,
 * however many members have it.
 */
export function JobsTable({ listState }: { listState: ListState<JobListState> }) {
  const me = useMe();
  const area = useArea();
  const isAdmin = me.role === "ADMIN";
  const [state, setState] = listState;
  const [picked, setPicked] = useState<ReadonlyMap<string, JobRef>>(() => new Map());
  const [dialog, setDialog] = useState<"members" | "team" | null>(null);
  const [opened, setOpened] = useState<string | null>(null);

  const params: JobListParams = {
    q: state.q || undefined,
    company: state.company || undefined,
    origin: state.origin || undefined,
    verification: state.verification || undefined,
    work_mode: state.work_mode || undefined,
    assigned: state.assigned === "yes" ? true : state.assigned === "no" ? false : undefined,
    page: toPositiveInt(state.page, 1),
    page_size: toPositiveInt(state.page_size, 25),
  };
  const jobs = useJobs(params);
  const filtered = Boolean(state.q || state.company || state.origin || state.verification || state.work_mode || state.assigned !== "all");
  const clear = () => setState({ q: "", company: "", origin: "", verification: "", work_mode: "", assigned: "all" });
  const pickedJobs = [...picked.values()];

  const columns: Column<JobListItem>[] = [
    {
      id: "job",
      header: "Job",
      cell: (job) => (
        <div className="flex min-w-0 flex-col gap-1">
          <Link href={`${area}/jobs/${job.id}`} className="w-fit rounded-sm font-medium underline-offset-4 hover:underline">
            {job.title}
          </Link>
          <JobFactLine job={job} company />
        </div>
      ),
    },
    {
      id: "source",
      header: "Source",
      cell: (job) => (
        <div className="flex flex-col gap-0.5 whitespace-nowrap">
          <span>{ORIGIN_LABELS[job.origin] ?? humanize(job.origin)}</span>
          {job.source_label && <span className="text-2sm text-muted-foreground">{job.source_label}</span>}
        </div>
      ),
    },
    { id: "verification", header: "Verification", cell: (job) => <VerificationTag status={job.verification_status} /> },
    {
      id: "assigned",
      header: "Assignments",
      align: "right",
      cell: (job) => (
        <span className="whitespace-nowrap">
          {job.assignment_count.toLocaleString()}
          <span className="text-muted-foreground"> ({job.applied_count} applied)</span>
        </span>
      ),
    },
    { id: "added", header: "Added", cell: (job) => <span className="whitespace-nowrap">{formatDate(job.created_at)}</span> },
    {
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      align: "right",
      cell: (job) => (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon-sm" aria-label={`Actions for ${job.title} at ${job.company}`}>
              <Ellipsis />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-52">
            <DropdownMenuItem onSelect={() => setOpened(job.id)}>
              <BriefcaseBusiness />
              Quick Look
            </DropdownMenuItem>
            <DropdownMenuItem
              onSelect={() => {
                setPicked(new Map([[job.id, { id: job.id, title: job.title, company: job.company }]]));
                setDialog("members");
              }}
            >
              <ClipboardCheck />
              Assign to Members
            </DropdownMenuItem>
            {isAdmin && (
              <DropdownMenuItem
                onSelect={() => {
                  setPicked(new Map([[job.id, { id: job.id, title: job.title, company: job.company }]]));
                  setDialog("team");
                }}
              >
                <FolderInput />
                Assign to Team Lead
              </DropdownMenuItem>
            )}
            {isAdmin && <VerifyAction job={job} />}
          </DropdownMenuContent>
        </DropdownMenu>
      ),
    },
  ];

  return (
    <div className="flex flex-col">
      <FilterBar canClear={filtered} onClear={clear}>
        <SearchInput label="Search jobs" placeholder="Search title or company" value={state.q} onChange={(q) => setState({ q })} />
        <SegmentedFilter
          label="Assignment"
          value={state.assigned}
          onChange={(assigned) => setState({ assigned })}
          options={[
            { value: "all", label: "All" },
            { value: "no", label: "Unassigned" },
            { value: "yes", label: "Assigned" },
          ]}
        />
        <FilterSelect label="Source" allLabel="All sources" value={state.origin} onChange={(origin) => setState({ origin })} options={ORIGINS} />
        <FilterSelect
          label="Verification"
          allLabel="Any verification"
          value={state.verification}
          onChange={(verification) => setState({ verification })}
          options={VERIFICATIONS}
        />
        <FilterSelect
          label="Work arrangement"
          allLabel="Any arrangement"
          value={state.work_mode}
          onChange={(work_mode) => setState({ work_mode })}
          options={JOB_WORK_MODES.map((mode) => ({ value: mode, label: mode }))}
        />
      </FilterBar>

      {picked.size > 0 && dialog === null && (
        <div
          role="region"
          aria-label="Selected jobs"
          className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-primary/30 bg-accent/60 px-3 py-2"
        >
          <span className="text-sm font-medium tabular-nums">{plural(picked.size, "job")} selected</span>
          <div className="ml-auto flex flex-wrap gap-2">
            <Button variant="ghost" onClick={() => setPicked(new Map())}>
              Clear selection
            </Button>
            {isAdmin && (
              <Button variant="outline" onClick={() => setDialog("team")}>
                <FolderInput data-icon="inline-start" />
                Assign to Team Lead
              </Button>
            )}
            <Button onClick={() => setDialog("members")}>
              <ClipboardCheck data-icon="inline-start" />
              Assign to Members
            </Button>
          </div>
        </div>
      )}

      <DataTable
        caption="Job postings"
        columns={columns}
        rows={jobs.data?.items}
        rowKey={(job) => job.id}
        loading={jobs.isPending}
        fetching={jobs.isPlaceholderData}
        error={jobs.error}
        onRetry={() => jobs.refetch()}
        selection={{
          selected: new Set(picked.keys()),
          label: (job) => `Select ${job.title} at ${job.company}`,
          onChange: (next) => {
            const byId = new Map((jobs.data?.items ?? []).map((job) => [job.id, job]));
            const updated = new Map<string, JobRef>();
            for (const id of next) {
              const existing = picked.get(id);
              const job = byId.get(id);
              if (existing) updated.set(id, existing);
              else if (job) updated.set(id, { id: job.id, title: job.title, company: job.company });
            }
            setPicked(updated);
          },
        }}
        empty={
          filtered ? (
            <EmptyState
              icon={SearchX}
              title="No jobs match these filters"
              description="Widen the filters or clear them."
              action={
                <Button variant="outline" size="sm" onClick={clear}>
                  Clear filters
                </Button>
              }
            />
          ) : (
            <EmptyState
              icon={BriefcaseBusiness}
              title="No jobs yet"
              description={
                isAdmin
                  ? "Jobs arrive from searches. You can also add one by hand, import it from its address, or import a CSV file."
                  : "Jobs your team was sent, found in a search, or was assigned are listed here."
              }
            />
          )
        }
        mobileCard={(job, select) => (
          <div className="flex gap-3">
            <div className="pt-0.5">{select}</div>
            <div className="flex min-w-0 flex-1 flex-col gap-2">
              <Link href={`${area}/jobs/${job.id}`} className="w-fit rounded-sm font-medium underline-offset-4 hover:underline">
                {job.title}
              </Link>
              <JobFactLine job={job} company />
              <div className="flex flex-wrap items-center justify-between gap-2 text-2sm">
                <VerificationTag status={job.verification_status} />
                <span className="text-muted-foreground tabular-nums">
                  {plural(job.assignment_count, "assignment")}, {job.applied_count} applied
                </span>
              </div>
            </div>
          </div>
        )}
        pagination={
          jobs.data && {
            page: jobs.data.page,
            pageSize: jobs.data.page_size,
            total: jobs.data.total,
            noun: "jobs",
            onPageChange: (page) => setState({ page: String(page) }),
            onPageSizeChange: (size) => setState({ page_size: String(size) }),
          }
        }
      />

      <AssignmentDialog
        open={dialog === "members"}
        onOpenChange={(open) => !open && setDialog(null)}
        jobs={pickedJobs}
        teamId={me.role === "TEAM_LEAD" ? me.team?.id : undefined}
        onAssigned={() => setPicked(new Map())}
      />
      <DelegateDialog
        open={dialog === "team"}
        onOpenChange={(open) => !open && setDialog(null)}
        jobs={pickedJobs}
        onDelegated={() => setPicked(new Map())}
      />
      <JobDrawer jobId={opened} onOpenChange={(open) => !open && setOpened(null)} />
    </div>
  );
}

/** Admin: every job posting in the portal, and the ways to add more. */
export function AdminJobsView() {
  const listState = useUrlState(JOB_LIST_DEFAULTS);
  const [dialog, setDialog] = useState<"add" | "url" | "csv" | null>(null);
  return (
    <>
      <PageHeader
        title="Jobs"
        description="Every job posting in the portal, counted once however many members have it. Select jobs to send them to a team's queue or straight to members."
        actions={
          <>
            <Button variant="outline" onClick={() => setDialog("csv")}>
              <FileUp data-icon="inline-start" />
              Import CSV
            </Button>
            <Button variant="outline" onClick={() => setDialog("url")}>
              <Link2 data-icon="inline-start" />
              Import from URL
            </Button>
            <Button onClick={() => setDialog("add")}>
              <Plus data-icon="inline-start" />
              Add Job
            </Button>
          </>
        }
      />
      <JobsTable listState={listState} />
      <AddJobDialog open={dialog === "add"} onOpenChange={(open) => !open && setDialog(null)} />
      <ImportUrlDialog open={dialog === "url"} onOpenChange={(open) => !open && setDialog(null)} />
      <CsvImportDialog open={dialog === "csv"} onOpenChange={(open) => !open && setDialog(null)} />
    </>
  );
}

/* ------------------------------------------------------------------ team queue */

const QUEUE_STATUS: Record<QueueItem["status"], { label: string; tone: "warning" | "success" | "neutral" }> = {
  pending: { label: "Waiting", tone: "warning" },
  assigned: { label: "Distributed", tone: "success" },
  dismissed: { label: "Dismissed", tone: "neutral" },
};

const QUEUE_FILTERS = [
  { value: "pending", label: "Waiting" },
  { value: "assigned", label: "Distributed" },
  { value: "dismissed", label: "Dismissed" },
  { value: "all", label: "All" },
];

/**
 * The jobs an Admin sent to a team (GET /api/teams/{id}/queue). Waiting jobs can
 * be ticked and distributed to members, or dismissed; a dismissed job can be
 * put back.
 */
export function TeamQueue({ teamId }: { teamId: string }) {
  const [status, setStatus] = useState("pending");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [picked, setPicked] = useState<ReadonlyMap<string, QueueItem>>(() => new Map());
  const [assigning, setAssigning] = useState(false);
  const [dismissing, setDismissing] = useState<QueueItem | null>(null);
  const [opened, setOpened] = useState<string | null>(null);
  const queue = useTeamQueue(teamId, { status, page, page_size: pageSize });
  const update = useUpdateQueueItem();
  const pickedItems = [...picked.values()];

  const setItemStatus = (item: QueueItem, next: "pending" | "dismissed") =>
    update.mutate(
      { id: item.id, status: next },
      {
        onSuccess: () => {
          toast.success(next === "dismissed" ? "Job dismissed from the queue" : "Job put back in the queue");
          setDismissing(null);
          setPicked((current) => {
            const updated = new Map(current);
            updated.delete(item.id);
            return updated;
          });
        },
        onError: (error) => toast.error(errorMessage(error)),
      },
    );

  const columns: Column<QueueItem>[] = [
    {
      id: "job",
      header: "Job",
      cell: (item) => (
        <div className="flex min-w-0 flex-col gap-1">
          <button type="button" onClick={() => setOpened(item.job.id)} className="w-fit rounded-sm text-left font-medium underline-offset-4 hover:underline">
            {item.job.title}
          </button>
          <JobFactLine job={item.job} company />
        </div>
      ),
    },
    {
      id: "note",
      header: "Note from the Admin",
      className: "max-w-64",
      cell: (item) => (item.note ? <span className="break-words">{item.note}</span> : <span className="text-muted-foreground">No note</span>),
    },
    {
      id: "sent",
      header: "Sent",
      cell: (item) => (
        <div className="flex flex-col gap-0.5 whitespace-nowrap">
          <span>{formatDate(item.created_at)}</span>
          {item.delegated_by && <span className="text-2sm text-muted-foreground">by {item.delegated_by.name}</span>}
        </div>
      ),
    },
    { id: "status", header: "Status", cell: (item) => <Tag tone={QUEUE_STATUS[item.status].tone}>{QUEUE_STATUS[item.status].label}</Tag> },
    {
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      align: "right",
      cell: (item) =>
        item.status === "pending" ? (
          <Button variant="ghost" size="sm" onClick={() => setDismissing(item)}>
            <XCircle data-icon="inline-start" />
            Dismiss
          </Button>
        ) : item.status === "dismissed" ? (
          <Button variant="ghost" size="sm" disabled={update.isPending} onClick={() => setItemStatus(item, "pending")}>
            <RotateCcw data-icon="inline-start" />
            Put Back
          </Button>
        ) : (
          <span className="text-xs text-muted-foreground">Handed out</span>
        ),
    },
  ];

  return (
    <div className="flex flex-col">
      <FilterBar>
        <SegmentedFilter
          label="Queue status"
          value={status}
          onChange={(next) => {
            setStatus(next);
            setPage(1);
            setPicked(new Map());
          }}
          options={QUEUE_FILTERS}
        />
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {picked.size > 0 && (
            <Button variant="ghost" onClick={() => setPicked(new Map())}>
              Clear selection
            </Button>
          )}
          <Button disabled={picked.size === 0} onClick={() => setAssigning(true)}>
            <ClipboardCheck data-icon="inline-start" />
            Distribute to Members{picked.size > 0 ? ` (${picked.size})` : ""}
          </Button>
        </div>
      </FilterBar>
      <DataTable
        caption="Jobs in the team queue"
        columns={columns}
        rows={queue.data?.items}
        rowKey={(item) => item.id}
        loading={queue.isPending}
        fetching={queue.isPlaceholderData}
        error={queue.error}
        onRetry={() => queue.refetch()}
        selection={{
          selected: new Set(picked.keys()),
          isSelectable: (item) => item.status === "pending",
          label: (item) => `Select ${item.job.title} at ${item.job.company}`,
          onChange: (next) => {
            const byId = new Map((queue.data?.items ?? []).map((item) => [item.id, item]));
            const updated = new Map<string, QueueItem>();
            for (const id of next) {
              const item = picked.get(id) ?? byId.get(id);
              if (item) updated.set(id, item);
            }
            setPicked(updated);
          },
        }}
        empty={
          <EmptyState
            icon={Inbox}
            title={status === "pending" ? "No jobs are waiting" : "Nothing here"}
            description={
              status === "pending"
                ? "When an Admin sends jobs to this team, they wait here until they are handed to members."
                : "No queue entries with this status."
            }
          />
        }
        mobileCard={(item, select) => (
          <div className="flex gap-3">
            <div className="pt-0.5">{select}</div>
            <div className="flex min-w-0 flex-1 flex-col gap-2">
              <button type="button" onClick={() => setOpened(item.job.id)} className="w-fit rounded-sm text-left font-medium underline-offset-4 hover:underline">
                {item.job.title}
              </button>
              <JobFactLine job={item.job} company />
              {item.note && <p className="text-2sm break-words text-muted-foreground">Note: {item.note}</p>}
              <div className="flex flex-wrap items-center justify-between gap-2">
                <Tag tone={QUEUE_STATUS[item.status].tone}>{QUEUE_STATUS[item.status].label}</Tag>
                {item.status === "pending" && (
                  <Button variant="ghost" size="sm" onClick={() => setDismissing(item)}>
                    Dismiss
                  </Button>
                )}
                {item.status === "dismissed" && (
                  <Button variant="ghost" size="sm" disabled={update.isPending} onClick={() => setItemStatus(item, "pending")}>
                    Put Back
                  </Button>
                )}
              </div>
            </div>
          </div>
        )}
        pagination={
          queue.data && {
            page: queue.data.page,
            pageSize: queue.data.page_size,
            total: queue.data.total,
            noun: "jobs",
            onPageChange: setPage,
            onPageSizeChange: (size) => {
              setPageSize(size);
              setPage(1);
            },
          }
        }
      />

      <AssignmentDialog
        open={assigning}
        onOpenChange={setAssigning}
        jobs={pickedItems.map((item) => ({ id: item.job.id, title: item.job.title, company: item.job.company }))}
        teamId={teamId}
        queueItemIds={pickedItems.map((item) => item.id)}
        onAssigned={() => setPicked(new Map())}
      />
      <ConfirmDialog
        open={dismissing !== null}
        onOpenChange={(open) => !open && setDismissing(null)}
        title="Dismiss this job from the queue?"
        description={
          dismissing && (
            <>
              <span className="font-medium text-foreground">{dismissing.job.title}</span> at {dismissing.job.company} will no
              longer wait for distribution. You can put it back from the Dismissed list.
            </>
          )
        }
        confirmLabel="Dismiss job"
        destructive
        pending={update.isPending}
        onConfirm={() => dismissing && setItemStatus(dismissing, "dismissed")}
      />
      <JobDrawer jobId={opened} onOpenChange={(open) => !open && setOpened(null)} />
    </div>
  );
}

const TL_DEFAULTS = { ...JOB_LIST_DEFAULTS, tab: "queue" };

/** Team Lead "Pending Jobs": the queue of jobs sent by the Admin, and every job the team can see. */
export function TeamJobsView() {
  const me = useMe();
  const [state, setState] = useUrlState(TL_DEFAULTS);
  const tab = state.tab === "jobs" ? "jobs" : "queue";

  return (
    <>
      <PageHeader
        title="Pending Jobs"
        description="Jobs your Admin sent to your team wait here until you hand them to members. Team Jobs lists every job your team has, was sent, or found."
        actions={
          <SegmentedFilter
            label="View"
            value={tab}
            onChange={(next) => setState({ tab: next })}
            options={[
              { value: "queue", label: "Queue from Admin" },
              { value: "jobs", label: "Team Jobs" },
            ]}
          />
        }
      />
      {!me.team ? (
        <div className="rounded-lg border bg-card">
          <ErrorState error={new Error("You are not assigned to a team yet. Ask an Admin to make you the Team Lead of a team.")} />
        </div>
      ) : tab === "queue" ? (
        <TeamQueue teamId={me.team.id} />
      ) : (
        <JobsTable listState={[state, setState]} />
      )}
    </>
  );
}
