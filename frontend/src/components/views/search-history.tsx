"use client";

import Link from "next/link";
import { History, Search, SearchX } from "lucide-react";

import { MODE_SHORT, SearchStatusTag } from "@/components/search/search-run";
import { DataTable, type Column } from "@/components/shared/data-table";
import { FilterBar, FilterSelect, SegmentedFilter } from "@/components/shared/filters";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { useArea, useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { formatDateTime, formatDuration, plural } from "@/lib/format";
import type { ListState } from "@/lib/list-state";
import { useSearches, useTeams } from "@/lib/queries";
import type { SearchListParams, SearchRun } from "@/lib/types";
import { toPositiveInt, useUrlState } from "@/lib/url-state";

export const SEARCH_LIST_DEFAULTS = { status: "", team: "", mine: "all", page: "1", page_size: "25" };
type SearchListState = typeof SEARCH_LIST_DEFAULTS;

const STATUSES = [
  { value: "active", label: "Queued or running" },
  { value: "completed", label: "Completed" },
  { value: "partial", label: "Finished with problems" },
  { value: "failed", label: "Failed" },
  { value: "cancelled", label: "Cancelled" },
];

function memberNames(run: SearchRun): string {
  const names = run.targets.map((target) => target.member.name);
  if (names.length <= 2) return names.join(", ") || "No members";
  return `${names.slice(0, 2).join(", ")} and ${names.length - 2} more`;
}

/**
 * Past and running searches (GET /api/searches). Embedded in a team or member
 * page it is limited to that team or member and keeps its own state.
 */
export function SearchHistoryTable({
  listState,
  teamId,
  memberId,
  showFilters = true,
}: {
  listState: ListState<SearchListState>;
  teamId?: string;
  memberId?: string;
  showFilters?: boolean;
}) {
  const me = useMe();
  const area = useArea();
  const isAdmin = me.role === "ADMIN";
  const [state, setState] = listState;
  const teams = useTeams(true, { enabled: isAdmin && showFilters && !teamId });
  const params: SearchListParams = {
    status: state.status || undefined,
    team_id: teamId ?? (isAdmin ? state.team || undefined : undefined),
    member_id: memberId,
    mine: state.mine === "mine" ? true : undefined,
    page: toPositiveInt(state.page, 1),
    page_size: toPositiveInt(state.page_size, 25),
  };
  const list = useSearches(params);
  const filtered = Boolean(state.status || state.team || state.mine !== "all");

  const columns: Column<SearchRun>[] = [
    {
      id: "started",
      header: "Started",
      cell: (run) => (
        <div className="flex flex-col gap-0.5">
          <Link href={`${area}/searches/${run.id}`} className="w-fit rounded-sm font-medium whitespace-nowrap underline-offset-4 hover:underline">
            {formatDateTime(run.created_at)}
          </Link>
          <span className="text-2sm text-muted-foreground">by {run.initiated_by?.name ?? "someone who left"}</span>
        </div>
      ),
    },
    {
      id: "members",
      header: "For",
      cell: (run) => (
        <div className="flex max-w-64 flex-col gap-0.5">
          <span className="truncate">{memberNames(run)}</span>
          <span className="text-2sm text-muted-foreground">
            {run.team?.name ?? "Several teams"}, {plural(run.member_count, "member")}
          </span>
        </div>
      ),
    },
    { id: "mode", header: "Results", cell: (run) => <Tag>{MODE_SHORT[run.mode]}</Tag> },
    { id: "status", header: "Status", cell: (run) => <SearchStatusTag status={run.status} cancelling={run.cancel_requested} /> },
    { id: "matched", header: "Matched", align: "right", cell: (run) => run.matched_count.toLocaleString() },
    { id: "assigned", header: "Assigned", align: "right", cell: (run) => run.assigned_count.toLocaleString() },
    {
      id: "duration",
      header: "Took",
      align: "right",
      cell: (run) => (run.completed_at ? formatDuration(run.started_at ?? run.created_at, run.completed_at) : "Still running"),
    },
  ];

  return (
    <div className="flex flex-col">
      {showFilters && (
        <FilterBar canClear={filtered} onClear={() => setState({ status: "", team: "", mine: "all" })}>
          <SegmentedFilter
            label="Whose searches"
            value={state.mine}
            onChange={(mine) => setState({ mine })}
            options={[
              { value: "all", label: isAdmin ? "Everyone's" : "Whole team" },
              { value: "mine", label: "Started by me" },
            ]}
          />
          <FilterSelect label="Status" allLabel="All statuses" value={state.status} onChange={(status) => setState({ status })} options={STATUSES} />
          {isAdmin && !teamId && (
            <FilterSelect
              label="Team"
              allLabel="All teams"
              value={state.team}
              onChange={(team) => setState({ team })}
              options={(teams.data ?? []).map((team) => ({ value: team.id, label: team.name }))}
            />
          )}
        </FilterBar>
      )}
      <DataTable
        caption="Searches"
        columns={columns}
        rows={list.data?.items}
        rowKey={(run) => run.id}
        loading={list.isPending}
        fetching={list.isPlaceholderData}
        error={list.error}
        onRetry={() => list.refetch()}
        empty={
          filtered ? (
            <EmptyState icon={SearchX} title="No searches match these filters" description="Choose another status or clear the filters." />
          ) : (
            <EmptyState
              icon={History}
              title="No searches yet"
              description="Each search you start is kept here with what it found."
              action={
                <Button asChild size="sm">
                  <Link href={`${area}/search`}>
                    <Search data-icon="inline-start" />
                    Find Jobs
                  </Link>
                </Button>
              }
            />
          )
        }
        mobileCard={(run) => (
          <div className="flex flex-col gap-2">
            <div className="flex items-start justify-between gap-2">
              <div className="flex min-w-0 flex-col gap-0.5">
                <Link href={`${area}/searches/${run.id}`} className="w-fit rounded-sm font-medium underline-offset-4 hover:underline">
                  {formatDateTime(run.created_at)}
                </Link>
                <span className="text-2sm text-muted-foreground">by {run.initiated_by?.name ?? "someone who left"}</span>
              </div>
              <SearchStatusTag status={run.status} cancelling={run.cancel_requested} />
            </div>
            <p className="text-sm">{memberNames(run)}</p>
            <p className="text-2sm text-muted-foreground tabular-nums">
              {MODE_SHORT[run.mode]}. {run.matched_count} matched, {run.assigned_count} assigned.
            </p>
          </div>
        )}
        pagination={
          list.data && {
            page: list.data.page,
            pageSize: list.data.page_size,
            total: list.data.total,
            noun: "searches",
            onPageChange: (page) => setState({ page: String(page) }),
            onPageSizeChange: (size) => setState({ page_size: String(size) }),
          }
        }
      />
    </div>
  );
}

export function SearchHistoryView() {
  const area = useArea();
  const listState = useUrlState(SEARCH_LIST_DEFAULTS);
  return (
    <>
      <PageHeader
        title="Search History"
        description="Every search, with what it was asked to find, what it found and what was assigned."
        actions={
          <Button asChild>
            <Link href={`${area}/search`}>
              <Search data-icon="inline-start" />
              Find Jobs
            </Link>
          </Button>
        }
      />
      <SearchHistoryTable listState={listState} />
    </>
  );
}
