"use client";

import { useId, useRef, useState } from "react";
import { Contact, Plus, Upload } from "lucide-react";
import { toast } from "sonner";

import { ResumeTag } from "@/components/candidates/candidate-meta";
import { MemberSelect } from "@/components/candidates/member-select";
import { FormMessage } from "@/components/shared/form-fields";
import { SearchInput } from "@/components/shared/search-input";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Spinner } from "@/components/ui/spinner";
import { errorMessage } from "@/lib/api";
import { useCandidates, useUpdateCandidate, useUploadCandidateResume } from "@/lib/queries";
import { RESUME_ACCEPT, resumeFileProblem } from "@/lib/resume-file";
import type { Candidate } from "@/lib/types";
import { cn } from "@/lib/utils";

/** What the rest of a form needs to know about a chosen candidate. */
export interface PickedCandidate {
  id: string;
  name: string;
  teamId: string | null;
  teamName: string | null;
  hasResume: boolean;
  member: { id: string; name: string } | null;
}

export const toPicked = (candidate: Candidate): PickedCandidate => ({
  id: candidate.id,
  name: candidate.name,
  teamId: candidate.team?.id ?? null,
  teamName: candidate.team?.name ?? null,
  hasResume: candidate.has_resume,
  member: candidate.assigned_member ? { id: candidate.assigned_member.id, name: candidate.assigned_member.name } : null,
});

const PAGE_STEP = 50;
const MAX_PAGE_SIZE = 200;

/**
 * A searchable checklist of active candidates. The list is searched by the API, so it
 * works the same for five candidates or five thousand; the selection is kept separately,
 * so a chosen candidate stays chosen when the search text changes.
 * A Team Lead only ever receives their own team's candidates from the API.
 */
export function CandidatePicker({
  selected,
  onChange,
  teamId,
  showTeam = false,
  max,
  onAdd,
  className,
}: {
  selected: ReadonlyMap<string, PickedCandidate>;
  onChange: (next: Map<string, PickedCandidate>) => void;
  /** Only list candidates of this team. */
  teamId?: string;
  showTeam?: boolean;
  /** Most candidates that may be chosen. */
  max?: number;
  /** Shows "Add Candidate" beside the search box. */
  onAdd?: () => void;
  className?: string;
}) {
  const groupId = useId();
  const [query, setQuery] = useState("");
  const [pageSize, setPageSize] = useState(PAGE_STEP);
  const candidates = useCandidates(
    { team_id: teamId, active: true, q: query || undefined, sort: "name", page: 1, page_size: pageSize },
    { live: false },
  );
  const atLimit = max !== undefined && selected.size >= max;

  const toggle = (candidate: Candidate) => {
    const next = new Map(selected);
    if (next.has(candidate.id)) next.delete(candidate.id);
    else if (!atLimit) next.set(candidate.id, toPicked(candidate));
    onChange(next);
  };

  const items = candidates.data?.items ?? [];
  const allShownSelected = items.length > 0 && items.every((candidate) => selected.has(candidate.id));

  const selectShown = () => {
    const next = new Map(selected);
    for (const candidate of items) {
      if (allShownSelected) next.delete(candidate.id);
      else if (max === undefined || next.size < max) next.set(candidate.id, toPicked(candidate));
    }
    onChange(next);
  };

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <div className="flex flex-wrap items-center gap-2">
        <SearchInput label="Search candidates" placeholder="Search name, e-mail or phone" value={query} onChange={setQuery} className="sm:w-72" />
        <span className="text-2sm text-muted-foreground tabular-nums" aria-live="polite">
          {selected.size} selected{max !== undefined ? ` of ${max} allowed` : ""}
        </span>
        <div className="ml-auto flex flex-wrap gap-1.5">
          <Button type="button" variant="ghost" size="sm" disabled={items.length === 0} onClick={selectShown}>
            {allShownSelected ? "Clear shown" : "Select all shown"}
          </Button>
          {selected.size > 0 && (
            <Button type="button" variant="ghost" size="sm" onClick={() => onChange(new Map())}>
              Clear all
            </Button>
          )}
          {onAdd && (
            <Button type="button" variant="outline" size="sm" onClick={onAdd}>
              <Plus data-icon="inline-start" />
              Add Candidate
            </Button>
          )}
        </div>
      </div>

      <div role="group" aria-label="Candidates" className="max-h-72 overflow-y-auto overscroll-contain rounded-lg border bg-card">
        {candidates.isPending ? (
          <LoadingSkeleton variant="rows" count={4} />
        ) : candidates.isError ? (
          <ErrorState compact error={candidates.error} onRetry={() => candidates.refetch()} />
        ) : items.length === 0 ? (
          <EmptyState
            compact
            icon={Contact}
            title={query ? "No candidates match this search" : "No candidates to choose from"}
            description={query ? "Check the spelling or clear the search." : "Add a candidate first: the person you are finding a job for."}
            action={
              !query && onAdd ? (
                <Button type="button" size="sm" onClick={onAdd}>
                  <Plus data-icon="inline-start" />
                  Add Candidate
                </Button>
              ) : undefined
            }
          />
        ) : (
          <ul className={cn("divide-y", candidates.isPlaceholderData && "opacity-60")}>
            {items.map((candidate) => {
              const id = `${groupId}-${candidate.id}`;
              const checked = selected.has(candidate.id);
              const disabled = !checked && atLimit;
              return (
                <li key={candidate.id}>
                  <label
                    htmlFor={id}
                    className={cn(
                      "flex cursor-pointer items-center gap-3 px-3 py-2.5 hover:bg-muted/60",
                      checked && "bg-accent/60",
                      disabled && "cursor-not-allowed opacity-60",
                    )}
                  >
                    <Checkbox id={id} checked={checked} disabled={disabled} onCheckedChange={() => toggle(candidate)} />
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="truncate text-sm font-medium">{candidate.name}</span>
                      <span className="truncate text-2sm text-muted-foreground">
                        {candidate.assigned_member ? `Handled by ${candidate.assigned_member.name}` : "No responsible member"}
                        {showTeam && candidate.team ? `, ${candidate.team.name}` : ""}
                      </span>
                    </span>
                    <span className="flex shrink-0 flex-wrap justify-end gap-1.5">
                      <ResumeTag hasResume={candidate.has_resume} short />
                    </span>
                  </label>
                </li>
              );
            })}
          </ul>
        )}
      </div>
      {candidates.data && candidates.data.total > items.length && (
        <div className="flex flex-wrap items-center justify-between gap-2 text-2sm text-muted-foreground">
          <span>
            Showing {items.length} of {candidates.data.total.toLocaleString()}. Search to narrow the list.
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
          You have reached the limit of {max} candidates. Remove one to choose another.
        </p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ per-candidate settings in the search form */

function SelectedCandidate({
  candidate,
  onUpdate,
  onRemove,
}: {
  candidate: PickedCandidate;
  onUpdate: (next: PickedCandidate) => void;
  onRemove: () => void;
}) {
  const inputId = useId();
  const selectId = useId();
  const input = useRef<HTMLInputElement>(null);
  const upload = useUploadCandidateResume();
  const update = useUpdateCandidate();
  const [problem, setProblem] = useState<string | null>(null);

  const send = (file: File | undefined) => {
    if (!file) return;
    const issue = resumeFileProblem(file);
    setProblem(issue);
    if (issue) return;
    upload.mutate(
      { id: candidate.id, file },
      {
        onSuccess: (saved) => {
          toast.success(`${saved.filename} uploaded for ${candidate.name} (version ${saved.version})`);
          onUpdate({ ...candidate, hasResume: true });
        },
        onError: (error) => setProblem(errorMessage(error)),
      },
    );
  };

  const setMember = (memberId: string) => {
    setProblem(null);
    update.mutate(
      { id: candidate.id, body: { assigned_member_id: memberId || null } },
      {
        onSuccess: (saved) => {
          onUpdate(toPicked(saved));
          toast.success(
            saved.assigned_member ? `${saved.assigned_member.name} is now responsible for ${saved.name}` : `${saved.name} is unassigned`,
          );
        },
        onError: (error) => setProblem(errorMessage(error)),
      },
    );
  };

  return (
    <li className="flex flex-col gap-2.5 px-3 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="min-w-0 text-sm font-medium break-words">{candidate.name}</span>
          <ResumeTag hasResume={candidate.hasResume} />
        </span>
        <Button type="button" variant="ghost" size="sm" className="text-muted-foreground" onClick={onRemove}>
          Remove<span className="sr-only"> {candidate.name} from this search</span>
        </Button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex min-w-0 flex-col gap-1.5">
          <span className="text-xs text-muted-foreground">Resume</span>
          <Button type="button" variant="outline" className="w-full justify-start sm:w-fit" disabled={upload.isPending} onClick={() => input.current?.click()}>
            {upload.isPending ? <Spinner data-icon="inline-start" /> : <Upload data-icon="inline-start" />}
            {upload.isPending ? "Uploading and reading" : candidate.hasResume ? "Replace Resume" : "Upload Resume"}
            <span className="sr-only"> for {candidate.name}</span>
          </Button>
          <input
            id={inputId}
            ref={input}
            type="file"
            accept={RESUME_ACCEPT}
            className="sr-only"
            tabIndex={-1}
            aria-label={`Resume file for ${candidate.name}`}
            onChange={(event) => {
              send(event.target.files?.[0]);
              event.target.value = "";
            }}
          />
        </div>
        <div className="flex min-w-0 flex-col gap-1.5">
          <label htmlFor={selectId} className="text-xs text-muted-foreground">
            Responsible member, who handles the jobs found
          </label>
          <MemberSelect
            id={selectId}
            teamId={candidate.teamId ?? undefined}
            value={candidate.member?.id ?? ""}
            current={candidate.member}
            disabled={update.isPending}
            onChange={setMember}
          />
        </div>
      </div>
      {problem && <FormMessage>{problem}</FormMessage>}
    </li>
  );
}

/**
 * The candidates chosen for a search, each with the two things a Team Lead settles before
 * starting it: the resume the search will use, and the member who will handle the jobs found.
 * Both are saved to the candidate at once; they are not settings of this one search.
 */
export function SelectedCandidates({
  selected,
  onChange,
}: {
  selected: ReadonlyMap<string, PickedCandidate>;
  onChange: (next: Map<string, PickedCandidate>) => void;
}) {
  if (selected.size === 0) return null;
  return (
    <section className="flex flex-col gap-2" aria-label="Selected candidates">
      <h4 className="font-label text-sm">
        Selected candidates <span className="font-normal text-muted-foreground tabular-nums">({selected.size})</span>
      </h4>
      <p className="text-2sm text-muted-foreground">
        A search uses the candidate&apos;s current resume; upload a new one here to use it for this search and later
        ones. A change of resume or responsible member is saved to the candidate right away.
      </p>
      <ul className="flex flex-col divide-y rounded-lg border bg-card">
        {[...selected.values()].map((candidate) => (
          <SelectedCandidate
            key={candidate.id}
            candidate={candidate}
            onUpdate={(next) => onChange(new Map(selected).set(next.id, next))}
            onRemove={() => {
              const next = new Map(selected);
              next.delete(candidate.id);
              onChange(next);
            }}
          />
        ))}
      </ul>
    </section>
  );
}
