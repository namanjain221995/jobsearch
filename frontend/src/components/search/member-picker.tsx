"use client";

import { useId, useState } from "react";
import { Users } from "lucide-react";

import { SearchInput } from "@/components/shared/search-input";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { useUsers } from "@/lib/queries";
import type { User } from "@/lib/types";
import { cn } from "@/lib/utils";

/** What the rest of a form needs to know about a chosen member. */
export interface PickedMember {
  id: string;
  name: string;
  email: string;
  teamName: string | null;
  hasResume: boolean;
}

export const toPicked = (user: User): PickedMember => ({
  id: user.id,
  name: user.name,
  email: user.email,
  teamName: user.team?.name ?? null,
  hasResume: user.has_resume,
});

const PAGE_STEP = 50;
const MAX_PAGE_SIZE = 200;

/**
 * A searchable checklist of members. The list is searched by the API, so it
 * works the same for five members or five thousand; the selection is kept
 * separately, so a chosen member stays chosen when the search text changes.
 * A Team Lead only ever receives their own team from the API.
 */
export function MemberPicker({
  selected,
  onChange,
  teamId,
  showTeam = false,
  max,
  label = "Members",
  className,
}: {
  selected: ReadonlyMap<string, PickedMember>;
  onChange: (next: Map<string, PickedMember>) => void;
  /** Only list members of this team. */
  teamId?: string;
  showTeam?: boolean;
  /** Most members that may be chosen. */
  max?: number;
  label?: string;
  className?: string;
}) {
  const groupId = useId();
  const [query, setQuery] = useState("");
  const [pageSize, setPageSize] = useState(PAGE_STEP);
  const users = useUsers(
    { role: "MEMBER", team_id: teamId, q: query || undefined, sort: "name", page: 1, page_size: pageSize },
    { refetchInterval: false },
  );
  const atLimit = max !== undefined && selected.size >= max;

  const toggle = (user: User) => {
    const next = new Map(selected);
    if (next.has(user.id)) next.delete(user.id);
    else if (!atLimit) next.set(user.id, toPicked(user));
    onChange(next);
  };

  const items = users.data?.items ?? [];
  const selectable = items.filter((user) => user.is_active);
  const allShownSelected = selectable.length > 0 && selectable.every((user) => selected.has(user.id));

  const selectShown = () => {
    const next = new Map(selected);
    for (const user of selectable) {
      if (allShownSelected) next.delete(user.id);
      else if (max === undefined || next.size < max) next.set(user.id, toPicked(user));
    }
    onChange(next);
  };

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <div className="flex flex-wrap items-center gap-2">
        <SearchInput label={`Search ${label.toLowerCase()}`} placeholder="Search name or e-mail" value={query} onChange={setQuery} className="sm:w-72" />
        <span className="text-2sm text-muted-foreground tabular-nums" aria-live="polite">
          {selected.size} selected{max !== undefined ? ` of ${max} allowed` : ""}
        </span>
        <div className="ml-auto flex gap-1.5">
          <Button type="button" variant="ghost" size="sm" disabled={selectable.length === 0} onClick={selectShown}>
            {allShownSelected ? "Clear shown" : "Select all shown"}
          </Button>
          {selected.size > 0 && (
            <Button type="button" variant="ghost" size="sm" onClick={() => onChange(new Map())}>
              Clear all
            </Button>
          )}
        </div>
      </div>

      <div
        role="group"
        aria-label={label}
        className="max-h-72 overflow-y-auto overscroll-contain rounded-lg border bg-card"
      >
        {users.isPending ? (
          <LoadingSkeleton variant="rows" count={4} />
        ) : users.isError ? (
          <ErrorState compact error={users.error} onRetry={() => users.refetch()} />
        ) : items.length === 0 ? (
          <EmptyState
            compact
            icon={Users}
            title={query ? "No members match this search" : "No members to choose from"}
            description={query ? "Check the spelling or clear the search." : "Invite members first, then come back."}
          />
        ) : (
          <ul className={cn("divide-y", users.isPlaceholderData && "opacity-60")}>
            {items.map((user) => {
              const id = `${groupId}-${user.id}`;
              const checked = selected.has(user.id);
              const disabled = !user.is_active || (!checked && atLimit);
              return (
                <li key={user.id}>
                  <label
                    htmlFor={id}
                    className={cn(
                      "flex cursor-pointer items-center gap-3 px-3 py-2.5 hover:bg-muted/60",
                      checked && "bg-accent/60",
                      disabled && "cursor-not-allowed opacity-60",
                    )}
                  >
                    <Checkbox id={id} checked={checked} disabled={disabled} onCheckedChange={() => toggle(user)} />
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="truncate text-sm font-medium">{user.name}</span>
                      <span className="truncate text-2sm text-muted-foreground">
                        {user.email}
                        {showTeam && user.team ? `, ${user.team.name}` : ""}
                      </span>
                    </span>
                    <span className="flex shrink-0 flex-wrap justify-end gap-1.5">
                      {!user.has_resume && <Tag tone="warning">No resume</Tag>}
                      {user.status === "invited" && <Tag>Invited</Tag>}
                      {user.status === "deactivated" && <Tag>Deactivated</Tag>}
                    </span>
                  </label>
                </li>
              );
            })}
          </ul>
        )}
      </div>
      {users.data && users.data.total > items.length && (
        <div className="flex flex-wrap items-center justify-between gap-2 text-2sm text-muted-foreground">
          <span>
            Showing {items.length} of {users.data.total.toLocaleString()}. Search to narrow the list.
          </span>
          {pageSize < MAX_PAGE_SIZE && (
            <Button type="button" variant="ghost" size="sm" onClick={() => setPageSize((size) => Math.min(MAX_PAGE_SIZE, size + PAGE_STEP))}>
              Show more
            </Button>
          )}
        </div>
      )}
      {atLimit && (
        <p className="text-2sm text-muted-foreground">
          You have reached the limit of {max} members. Remove one to choose another.
        </p>
      )}
    </div>
  );
}
