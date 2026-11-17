"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  createContext,
  use,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ChevronUp, Minimize2, Search, X } from "lucide-react";
import { toast } from "sonner";

import {
  MODE_LABELS,
  RunAttempts,
  RunCounters,
  RunSources,
  RunTargets,
  RunWarnings,
  searchStatusLabel,
  SearchStatusTag,
  SkippedList,
} from "@/components/search/search-run";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { useArea, useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import { plural } from "@/lib/format";
import { invalidateWork, isSearchActive, qk, useCancelSearch, useMyActiveSearches, useSearch } from "@/lib/queries";
import type { SearchCreated, SearchRun, SearchSkipped } from "@/lib/types";

/* ------------------------------------------------------------------ shared state */

type View = "modal" | "minimized";

interface Tracked {
  runId: string;
  view: View;
  skipped: SearchSkipped[];
}

interface SearchProgressContextValue {
  tracked: Tracked | null;
  /** Called with the API's 202 answer: opens the progress window for the new run. */
  start: (created: SearchCreated) => void;
  /** Follow an existing run (from the history or a detail page). */
  follow: (runId: string, view?: View) => void;
  setView: (view: View) => void;
  dismiss: () => void;
  dismissed: ReadonlySet<string>;
}

const SearchProgressContext = createContext<SearchProgressContextValue | null>(null);

export function useSearchProgress(): SearchProgressContextValue {
  const value = use(SearchProgressContext);
  if (!value) throw new Error("useSearchProgress() is only available inside the dashboard shell.");
  return value;
}

/**
 * Holds which search run the person is following. It lives in the shell, above
 * the pages, so the progress window and its minimized indicator survive every
 * navigation. The search itself runs on the server whether or not anyone watches.
 */
export function SearchProgressProvider({ children }: { children: ReactNode }) {
  const [tracked, setTracked] = useState<Tracked | null>(null);
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(() => new Set());

  const start = useCallback((created: SearchCreated) => {
    setTracked({ runId: created.search.id, view: "modal", skipped: created.skipped });
  }, []);
  const follow = useCallback((runId: string, view: View = "modal") => {
    setTracked((current) => (current?.runId === runId ? { ...current, view } : { runId, view, skipped: [] }));
  }, []);
  const setView = useCallback((view: View) => {
    setTracked((current) => (current ? { ...current, view } : current));
  }, []);
  const dismiss = useCallback(() => {
    setTracked((current) => {
      if (current) setDismissed((previous) => new Set(previous).add(current.runId));
      return null;
    });
  }, []);

  const value = useMemo(
    () => ({ tracked, start, follow, setView, dismiss, dismissed }),
    [tracked, start, follow, setView, dismiss, dismissed],
  );
  return <SearchProgressContext value={value}>{children}</SearchProgressContext>;
}

/* ------------------------------------------------------------------ progress window */

function ProgressBody({ run, skipped }: { run: SearchRun; skipped: SearchSkipped[] }) {
  const active = isSearchActive(run.status);
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <SearchStatusTag status={run.status} cancelling={run.cancel_requested} />
          <span className="text-sm" aria-live="polite">
            {active ? run.stage || "Waiting for the background worker" : `Search ${searchStatusLabel(run.status).toLowerCase()}`}
          </span>
          {active && <span aria-hidden="true" className="size-1.5 rounded-full bg-primary motion-safe:animate-pulse-dot" />}
          <RunAttempts run={run} />
        </div>
        <p className="text-2sm text-muted-foreground">
          {MODE_LABELS[run.mode]}. Up to {run.target_count} jobs per member, minimum match score {run.min_match_score}.
        </p>
      </div>

      <SkippedList skipped={skipped} />
      <RunWarnings run={run} />

      <RunCounters run={run} className="lg:grid-cols-3" />

      <section className="flex flex-col gap-2">
        <h3 className="font-label text-sm">
          Selected members <span className="font-normal text-muted-foreground tabular-nums">({run.targets.length})</span>
        </h3>
        <RunTargets targets={run.targets} />
      </section>

      <section className="flex flex-col gap-2">
        <h3 className="font-label text-sm">Sources</h3>
        <RunSources sources={run.sources} />
      </section>
    </div>
  );
}

/**
 * "AI Job Search Started": the live view of one run.
 *
 * It is a dialog that does not take over the page. There is no backdrop and
 * focus is not trapped, so the rest of the app keeps working underneath; Escape
 * or Minimize folds it into the floating indicator.
 */
export function SearchProgressModal({
  run,
  loading,
  error,
  onRetry,
  skipped,
  onMinimize,
  onClose,
}: {
  run: SearchRun | undefined;
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  skipped: SearchSkipped[];
  onMinimize: () => void;
  onClose: () => void;
}) {
  const area = useArea();
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const cancel = useCancelSearch();
  const [confirming, setConfirming] = useState(false);
  const active = run ? isSearchActive(run.status) : true;

  // Move focus into the window when it opens, so keyboard and screen-reader users land on it.
  useEffect(() => {
    panelRef.current?.focus();
  }, []);

  return (
    <div
      ref={panelRef}
      role="dialog"
      aria-modal="false"
      aria-labelledby={titleId}
      tabIndex={-1}
      data-slot="search-progress"
      onKeyDown={(event) => {
        if (event.key === "Escape" && !confirming) {
          event.stopPropagation();
          onMinimize();
        }
      }}
      className="fixed inset-x-2 bottom-2 z-40 flex max-h-[88svh] flex-col overflow-hidden rounded-xl border border-border-strong bg-popover text-popover-foreground shadow-xl outline-none motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-bottom-4 sm:inset-x-auto sm:top-[8svh] sm:bottom-auto sm:left-1/2 sm:w-[min(46rem,calc(100vw-2rem))] sm:-translate-x-1/2"
    >
      <header className="flex items-start justify-between gap-3 border-b px-4 py-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h2 id={titleId} className="font-display text-xl leading-tight">
            AI Job Search Started
          </h2>
          <p className="text-2sm text-muted-foreground">
            It keeps running in the background if you leave this page, refresh, or sign out.
          </p>
        </div>
        <Button variant="ghost" size="icon-sm" onClick={onMinimize} aria-label="Minimize">
          <Minimize2 />
        </Button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-4">
        {error && !run ? (
          <ErrorState compact error={error} onRetry={onRetry} />
        ) : loading || !run ? (
          <LoadingSkeleton variant="detail" count={3} />
        ) : (
          <ProgressBody run={run} skipped={skipped} />
        )}
      </div>

      <footer className="flex flex-wrap items-center justify-end gap-2 border-t bg-muted/50 px-4 py-3">
        {run && active && (
          <Button
            variant="destructive"
            className="mr-auto"
            disabled={run.cancel_requested || cancel.isPending}
            onClick={() => setConfirming(true)}
          >
            {run.cancel_requested ? "Cancelling" : "Cancel Search"}
          </Button>
        )}
        {run && !active && (
          <Button variant="ghost" className="mr-auto" onClick={onClose}>
            <X data-icon="inline-start" />
            Close
          </Button>
        )}
        <Button variant="outline" onClick={onMinimize}>
          Minimize
        </Button>
        {run && (
          <Button asChild>
            <Link href={`${area}/searches/${run.id}`} onClick={onMinimize}>
              {active ? "View Search" : "View Results"}
            </Link>
          </Button>
        )}
      </footer>

      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="Cancel this search?"
        description="The search stops within a few seconds. Jobs already found and assigned stay as they are; the rest is not searched."
        confirmLabel="Cancel search"
        cancelLabel="Keep searching"
        destructive
        pending={cancel.isPending}
        onConfirm={() =>
          run &&
          cancel.mutate(run.id, {
            onSuccess: () => {
              setConfirming(false);
              toast.message("Cancelling the search");
            },
            onError: (error) => toast.error(errorMessage(error)),
          })
        }
      />
    </div>
  );
}

/* ------------------------------------------------------------------ minimized indicator */

function ProgressIndicator({
  run,
  othersActive,
  onExpand,
  onDismiss,
}: {
  run: SearchRun | undefined;
  othersActive: number;
  onExpand: () => void;
  onDismiss: () => void;
}) {
  const area = useArea();
  const active = run ? isSearchActive(run.status) : true;
  return (
    <div
      role="status"
      data-slot="search-indicator"
      className="fixed right-3 bottom-3 z-40 flex max-w-[calc(100vw-1.5rem)] items-stretch overflow-hidden rounded-lg border border-border-strong bg-popover text-popover-foreground shadow-lg"
    >
      <button
        type="button"
        onClick={onExpand}
        className="flex min-w-0 items-center gap-2.5 py-2 pr-2 pl-3 text-left hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
        aria-label={`Show search progress: ${run ? searchStatusLabel(run.status) : "loading"}`}
      >
        <span className="relative grid size-7 shrink-0 place-items-center rounded-md bg-primary/10 text-primary">
          <Search className="size-4" aria-hidden="true" />
          {active && <span aria-hidden="true" className="absolute -top-0.5 -right-0.5 size-2 rounded-full bg-primary motion-safe:animate-pulse-dot" />}
        </span>
        <span className="flex min-w-0 flex-col">
          <span className="truncate text-sm font-medium">
            {!run
              ? "Search"
              : !active
                ? `Search ${searchStatusLabel(run.status).toLowerCase()}`
                : run.cancel_requested
                  ? "Cancelling search"
                  : // Queued is also where a run waits after the worker restarted: it continues by itself.
                    run.status === "queued"
                    ? "Search queued"
                    : "Search running"}
          </span>
          <span className="truncate text-xs text-muted-foreground tabular-nums">
            {run
              ? active
                ? `${run.stage || "Waiting"}. ${run.matched_count} matched`
                : `${run.matched_count} matched, ${run.assigned_count} assigned`
              : "Loading progress"}
            {othersActive > 0 ? `. ${plural(othersActive, "other search", "other searches")} running` : ""}
          </span>
        </span>
        <ChevronUp className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      </button>
      {run && !active && (
        <>
          <Link
            href={`${area}/searches/${run.id}`}
            className="flex items-center border-l px-3 text-sm font-medium text-primary hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
          >
            Results
          </Link>
          <button
            type="button"
            onClick={onDismiss}
            aria-label="Dismiss"
            className="grid w-9 place-items-center border-l text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:bg-accent focus-visible:outline-none"
          >
            <X className="size-4" aria-hidden="true" />
          </button>
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ host */

/**
 * Draws the progress window or its indicator for the run being followed, polls
 * it while it is queued or running, and announces the result when it ends.
 *
 * On load it looks for searches this person started that are still active
 * (GET /api/searches?status=active&mine=true) and restores the indicator for
 * the newest one, so a refresh or a new sign-in does not lose track of it.
 */
export function SearchProgressHost() {
  const me = useMe();
  const area = useArea();
  const client = useQueryClient();
  const router = useRouter();
  const { tracked, follow, setView, dismiss, dismissed } = useSearchProgress();
  const isManager = me.role !== "MEMBER";

  const activeMine = useMyActiveSearches(isManager);
  const restorableId = activeMine.data?.items.find((run) => !dismissed.has(run.id))?.id;
  // Nothing is being followed yet, but a search of this person is still running: pick it up, minimized.
  useEffect(() => {
    if (isManager && !tracked && restorableId) follow(restorableId, "minimized");
  }, [isManager, tracked, restorableId, follow]);

  const search = useSearch(isManager ? (tracked?.runId ?? null) : null);
  const run = search.data;

  // Announce the end of a run once, and refresh everything its results may have changed.
  const lastStatus = useRef<{ id: string; status: string } | null>(null);
  useEffect(() => {
    if (!run) return;
    const previous = lastStatus.current;
    lastStatus.current = { id: run.id, status: run.status };
    if (!previous || previous.id !== run.id || !isSearchActive(previous.status) || isSearchActive(run.status)) return;

    void invalidateWork(client);
    void client.invalidateQueries({ queryKey: qk.searchActiveMine });
    const summary = `${plural(run.matched_count, "job")} matched, ${run.assigned_count.toLocaleString()} assigned.`;
    const action = { label: "View results", onClick: () => router.push(`${area}/searches/${run.id}`) };
    if (run.status === "completed") toast.success("Search completed", { description: summary, action });
    else if (run.status === "partial") toast.warning("Search finished with problems", { description: summary, action });
    else if (run.status === "cancelled") toast.message("Search cancelled", { description: summary, action });
    else toast.error("Search failed", { description: run.error_summary || summary, action });
  }, [run, client, area, router]);

  if (!isManager || !tracked) return null;
  const othersActive = (activeMine.data?.items ?? []).filter((item) => item.id !== tracked.runId).length;

  return tracked.view === "modal" ? (
    <SearchProgressModal
      run={run}
      loading={search.isPending}
      error={search.error}
      onRetry={() => search.refetch()}
      skipped={tracked.skipped}
      onMinimize={() => setView("minimized")}
      onClose={dismiss}
    />
  ) : (
    <ProgressIndicator run={run} othersActive={othersActive} onExpand={() => setView("modal")} onDismiss={dismiss} />
  );
}
