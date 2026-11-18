"use client";

import { useState } from "react";
import { Bot, Eye, Send } from "lucide-react";
import { toast } from "sonner";

import { RunStatusBadge } from "@/components/auto-apply/parts";
import { RunSheet } from "@/components/auto-apply/run-sheet";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { FilterBar, FilterSelect } from "@/components/shared/filters";
import { Pagination } from "@/components/shared/pagination";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { errorMessage } from "@/lib/api";
import {
  ATTENTION_STATUSES,
  RUN_STATUS_META,
  refusalText,
  RUN_STATUSES,
  useApproveBatch,
  useAutoApplyRuns,
  usePreviewed,
  type AutoApplyRun,
  type RunStatus,
} from "@/lib/auto-apply";
import { formatDateTime } from "@/lib/format";

const STATUS_OPTIONS = RUN_STATUSES.map((s) => ({ value: s, label: RUN_STATUS_META[s].label }));

function RunRow({
  run,
  showCandidate,
  selectable,
  selected,
  previewed,
  onSelect,
  onOpen,
}: {
  run: AutoApplyRun;
  showCandidate: boolean;
  selectable: boolean;
  selected: boolean;
  previewed: boolean;
  onSelect: (on: boolean) => void;
  onOpen: () => void;
}) {
  const ready = run.status === "ready_for_review";
  return (
    <li className="flex items-start gap-3 px-3 py-3 sm:px-4">
      {selectable && (
        <Checkbox
          className="mt-1"
          disabled={!ready || !previewed}
          checked={selected}
          onCheckedChange={(v) => onSelect(v === true)}
          aria-label={`Select ${run.job.title} for batch approval`}
          title={ready && !previewed ? "Open the preview first" : undefined}
        />
      )}
      <button type="button" onClick={onOpen} className="flex min-w-0 flex-1 flex-col gap-1 rounded-sm text-left hover:underline-offset-4 focus-visible:outline-2">
        <span className="text-sm font-medium break-words hover:underline">{run.job.title}</span>
        <span className="text-2sm break-words text-muted-foreground">
          {run.job.company}
          {showCandidate && run.candidate?.name ? ` · ${run.candidate.name}` : ""}
          {` · ${formatDateTime(run.updated_at ?? run.created_at)}`}
        </span>
        <span className="flex flex-wrap items-center gap-2">
          <RunStatusBadge status={run.status} />
          {run.counts.needs_input > 0 && <span className="text-2sm text-muted-foreground">{run.counts.needs_input} need input</span>}
          {run.counts.ai_drafted > 0 && <span className="text-2sm text-muted-foreground">{run.counts.ai_drafted} AI drafted</span>}
          {run.outcome_unknown && <span className="text-2sm text-destructive">Outcome unknown: check by hand</span>}
          {selectable && ready && !previewed && <span className="text-2sm text-muted-foreground">Open to preview before batch approval</span>}
        </span>
      </button>
      <Button variant="ghost" size="icon-sm" onClick={onOpen} aria-label={`Open ${run.job.title}`}>
        <Eye />
      </Button>
    </li>
  );
}

/**
 * A list of auto-apply runs with a status filter and the run sheet. With `queue`, it lists what needs
 * attention across candidates and offers batch approval of ready runs previewed in this session.
 */
export function RunsList({ candidateId, queue = false, fast = false }: { candidateId?: string; queue?: boolean; fast?: boolean }) {
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [opened, setOpened] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const previewed = usePreviewed();
  const batch = useApproveBatch();

  const runs = useAutoApplyRuns({
    candidate_id: candidateId,
    status: status ? [status as RunStatus] : queue ? ATTENTION_STATUSES : undefined,
    page,
    page_size: pageSize,
  }, true, fast);
  const items = runs.data?.items ?? [];
  // Only ready, previewed runs that are still listed can stay selected.
  const eligible = new Set(items.filter((r) => r.status === "ready_for_review" && previewed.has(r.id)).map((r) => r.id));
  const chosen = [...selected].filter((id) => eligible.has(id));
  const chosenRuns = items.filter((r) => chosen.includes(r.id));

  const statusOptions = queue ? STATUS_OPTIONS.filter((o) => ATTENTION_STATUSES.includes(o.value)) : STATUS_OPTIONS;

  return (
    <div className="flex flex-col gap-3">
      <FilterBar className="mb-0" canClear={status !== ""} onClear={() => (setStatus(""), setPage(1))}>
        <FilterSelect
          label="Status"
          allLabel={queue ? "Everything that needs attention" : "All statuses"}
          value={status}
          onChange={(v) => (setStatus(v), setPage(1))}
          options={statusOptions}
        />
        {queue && (
          <Button className="sm:ml-auto" disabled={chosen.length === 0} onClick={() => setConfirming(true)}>
            <Send data-icon="inline-start" />
            Approve and submit {chosen.length || ""} selected
          </Button>
        )}
      </FilterBar>

      <div className="rounded-lg border bg-card">
        {runs.isPending ? (
          <LoadingSkeleton variant="list" count={4} className="p-3" />
        ) : runs.isError ? (
          <ErrorState error={runs.error} onRetry={() => runs.refetch()} />
        ) : items.length === 0 ? (
          <EmptyState
            icon={Bot}
            title={queue ? "Nothing needs attention" : "No auto-apply runs"}
            description={queue ? "Runs that need input, review, or have failed or been blocked show up here." : "Start one from the candidate's menu: Auto Apply."}
          />
        ) : (
          <ul className="flex flex-col divide-y">
            {items.map((run) => (
              <RunRow
                key={run.id}
                run={run}
                showCandidate={!candidateId}
                selectable={queue}
                selected={chosen.includes(run.id)}
                previewed={previewed.has(run.id)}
                onSelect={(on) =>
                  setSelected((prev) => {
                    const next = new Set(prev);
                    if (on) next.add(run.id);
                    else next.delete(run.id);
                    return next;
                  })
                }
                onOpen={() => setOpened(run.id)}
              />
            ))}
          </ul>
        )}
      </div>

      {runs.data && runs.data.total > 0 && (
        <Pagination
          page={page}
          pageSize={pageSize}
          total={runs.data.total}
          onPageChange={setPage}
          onPageSizeChange={(size) => (setPageSize(size), setPage(1))}
          noun="runs"
        />
      )}

      <RunSheet runId={opened} onOpenChange={(open) => !open && setOpened(null)} />

      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Submit ${chosenRuns.length} application${chosenRuns.length === 1 ? "" : "s"}?`}
        description={
          <div className="flex flex-col gap-2">
            <p>Each application below is submitted in the named candidate&apos;s name, exactly as in the preview you opened. A run whose answers changed since is refused.</p>
            <ul className="max-h-48 list-disc overflow-y-auto pl-5">
              {chosenRuns.map((r) => (
                <li key={r.id} className="break-words">
                  {r.job.title}
                  {r.job.company ? ` at ${r.job.company}` : ""} for <strong className="text-foreground">{r.candidate?.name || "the candidate"}</strong>
                </li>
              ))}
            </ul>
            <p className="font-medium text-foreground">Submitted applications cannot be undone.</p>
          </div>
        }
        confirmLabel="Submit applications"
        pending={batch.isPending}
        onConfirm={() =>
          batch.mutate(
            chosen.map((run_id) => ({ run_id, answers_hash: previewed.get(run_id) ?? "" })),
            {
            onSuccess: ({ approved, refused }) => {
              if (approved.length > 0) toast.success(`${approved.length} application${approved.length === 1 ? "" : "s"} approved and being submitted`);
              for (const r of refused) {
                const job = items.find((i) => i.id === r.run_id)?.job.title ?? "A run";
                toast.error(`${job} was not approved: ${refusalText(r.reason, r.detail)}`);
              }
              setSelected(new Set());
              setConfirming(false);
            },
            onError: (error) => (toast.error(errorMessage(error)), setConfirming(false)),
            },
          )
        }
      />
    </div>
  );
}
