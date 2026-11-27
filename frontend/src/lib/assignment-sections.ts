import { keepPreviousData, useMutation, useQuery, useQueryClient, type QueryClient, type QueryKey } from "@tanstack/react-query";

import { api, type QueryParams } from "./api";
import { invalidateWork, LIVE_INTERVAL, qk } from "./queries";
import { PRE_APPLICATION_STATUSES, sectionOf } from "./status";
import type {
  Assignment,
  AssignmentDetail,
  AssignmentListParams,
  AssignmentSectionParams,
  AssignmentSections,
  AssignmentStatus,
  Page,
} from "./types";

/*
 * The sections of a member's job list ("To apply", "Applied", "In progress", "Closed"): their sizes, and
 * the one change a member makes straight from a card, marking a job as applied (and taking that back).
 *
 * Both keys start with qk.assignments, so everything that already refreshes assignment lists after a
 * change (invalidateWork) refreshes these as well.
 */
const LISTS = [...qk.assignments, "list"] as const;
const SECTIONS = [...qk.assignments, "sections"] as const;
const MOVE = ["assignment-move"] as const;

/** How many jobs each section holds, for the same candidate and search text as the list beside it. */
export function useAssignmentSections(params: AssignmentSectionParams, enabled = true) {
  return useQuery({
    queryKey: [...SECTIONS, params],
    queryFn: ({ signal }) => api.get<AssignmentSections>("/api/assignments/sections", params as QueryParams, signal),
    placeholderData: keepPreviousData,
    refetchInterval: LIVE_INTERVAL,
    enabled,
  });
}

/** Would a list asked for with these parameters hold an assignment of this status? */
function listTakes(params: AssignmentListParams, status: AssignmentStatus): boolean {
  if (params.status && !params.status.split(",").includes(status)) return false;
  const waiting = PRE_APPLICATION_STATUSES.includes(status);
  if (params.applied === true && waiting) return false;
  if (params.applied === false && !waiting) return false;
  return true;
}

/** Is this assignment among the ones a section count with these parameters counts? */
function countTakes(params: AssignmentSectionParams, assignment: Assignment): boolean {
  if (params.candidate_id && params.candidate_id !== assignment.candidate.id) return false;
  if (params.member_id && params.member_id !== assignment.member?.id) return false;
  if (params.team_id && params.team_id !== assignment.team?.id) return false;
  const text = (params.q ?? "").trim().toLowerCase();
  return !text || assignment.job.title.toLowerCase().includes(text) || assignment.job.company.toLowerCase().includes(text);
}

export interface AssignmentMove {
  /** The assignment as the screen shows it now (its `status` is where it moves from). */
  assignment: Assignment;
  to: AssignmentStatus;
  /**
   * Filled in when the move is shown: for each list the job left, the place it had there. Hand the same
   * map to the move that takes it back, and the job returns to those places.
   */
  places: Map<string, number>;
}

/** Show a status change in every list and count that is on screen or remembered, before the API has answered. */
function showMove(client: QueryClient, { assignment, to, places }: AssignmentMove, returning?: Map<string, number>): void {
  const moved: Assignment = {
    ...assignment,
    status: to,
    applied_at: PRE_APPLICATION_STATUSES.includes(to) ? null : (assignment.applied_at ?? new Date().toISOString()),
  };
  for (const [key, page] of client.getQueriesData<Page<Assignment>>({ queryKey: LISTS })) {
    if (!page) continue;
    const takes = listTakes((key[key.length - 1] ?? {}) as AssignmentListParams, to);
    const name = JSON.stringify(key);
    const index = page.items.findIndex((item) => item.id === assignment.id);
    if (index >= 0 && !takes) {
      places.set(name, index);
      client.setQueryData<Page<Assignment>>(key, {
        ...page,
        items: page.items.filter((item) => item.id !== assignment.id),
        total: Math.max(0, page.total - 1),
      });
    } else if (index >= 0) {
      client.setQueryData<Page<Assignment>>(key, { ...page, items: page.items.map((item) => (item.id === assignment.id ? moved : item)) });
    } else if (takes && returning?.has(name)) {
      const items = [...page.items];
      items.splice(Math.min(returning.get(name) ?? 0, items.length), 0, moved);
      client.setQueryData<Page<Assignment>>(key, { ...page, items, total: page.total + 1 });
    }
  }
  const from = sectionOf(assignment.status);
  const into = sectionOf(to);
  if (from === into) return;
  for (const [key, counts] of client.getQueriesData<AssignmentSections>({ queryKey: SECTIONS })) {
    if (!counts || !countTakes((key[key.length - 1] ?? {}) as AssignmentSectionParams, assignment)) continue;
    client.setQueryData<AssignmentSections>(key, { ...counts, [from]: Math.max(0, counts[from] - 1), [into]: counts[into] + 1 });
  }
}

/**
 * Change an assignment's status from a list. The card moves at once: it leaves the lists that no longer
 * hold it and the section counts follow. If the API refuses, everything is put back exactly as it was
 * and the caller is told (the promise rejects). When the API has answered, the lists are read again, so
 * what stays on screen is what is stored.
 */
export function useMoveAssignment() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: MOVE,
    mutationFn: ({ assignment, to }: AssignmentMove & { returning?: Map<string, number> }) =>
      api.patch<AssignmentDetail>(`/api/assignments/${assignment.id}/status`, { status: to }),
    onMutate: async (move) => {
      // A list that is being read right now would arrive after the change below and undo it on screen.
      await Promise.all([client.cancelQueries({ queryKey: LISTS }), client.cancelQueries({ queryKey: SECTIONS })]);
      const before: Array<[QueryKey, unknown]> = [
        ...client.getQueriesData({ queryKey: LISTS }),
        ...client.getQueriesData({ queryKey: SECTIONS }),
      ];
      showMove(client, move, move.returning);
      return { before };
    },
    onError: (_error, _move, context) => {
      for (const [key, data] of context?.before ?? []) client.setQueryData(key, data);
    },
    onSuccess: (detail, { assignment }) => {
      client.setQueryData(qk.assignment(assignment.id), detail);
    },
    // While another move is still on its way, reading the lists now could show it as not made yet.
    onSettled: () => (client.isMutating({ mutationKey: MOVE }) <= 1 ? invalidateWork(client) : undefined),
  });
}
