"use client";

import { useId, useState, type KeyboardEvent, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Cloud, CloudOff, FileCheck2, FileWarning, Hourglass, RefreshCw, SearchX, TriangleAlert, type LucideIcon } from "lucide-react";

import { MemberSelect } from "@/components/candidates/member-select";
import { FormMessage } from "@/components/shared/form-fields";
import { LoadingSkeleton } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { api, errorMessage } from "@/lib/api";
import { plural } from "@/lib/format";
import { qk, useTeams } from "@/lib/queries";
import {
  SALESFORCE_MAX_QUERY,
  SALESFORCE_MIN_QUERY,
  SALESFORCE_PAGE,
  salesforceProblem,
  useImportSalesforceCandidate,
  useSalesforceSearch,
  useSalesforceStatus,
  type SalesforceCandidate,
  type SalesforceSync,
} from "@/lib/salesforce";
import type { Candidate } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Arrow keys for a search box with result rows under it. Put on the element that holds both:
 * Down from the box enters the results, Up and Down move through them (Up from the first row
 * goes back to the box), Home and End jump. Rows are the elements marked `data-result`.
 * Tab works as everywhere, and Enter chooses the row that has the focus.
 */
export function resultKeys(event: KeyboardEvent<HTMLElement>) {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp" && event.key !== "Home" && event.key !== "End") return;
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  const fromBox = target instanceof HTMLInputElement && target.type === "search";
  if (fromBox ? event.key !== "ArrowDown" : !target.matches("[data-result]")) return;
  const rows = [...event.currentTarget.querySelectorAll<HTMLElement>("[data-result]:not(:disabled)")];
  if (rows.length === 0) return;
  const index = rows.indexOf(target);
  const next = fromBox
    ? rows[0]
    : event.key === "ArrowDown"
      ? rows[index + 1]
      : event.key === "ArrowUp"
        ? (rows[index - 1] ?? event.currentTarget.querySelector<HTMLElement>('input[type="search"]'))
        : event.key === "Home"
          ? rows[0]
          : rows[rows.length - 1];
  if (!next) return;
  event.preventDefault();
  next.focus();
}

/** A state of the whole list that is not a list: cannot be reached, asked too fast, nothing found. */
function Notice({
  icon: Icon,
  title,
  description,
  onRetry,
  retryLabel = "Try again",
  alert = false,
}: {
  icon: LucideIcon;
  title: string;
  description?: ReactNode;
  onRetry?: () => void;
  retryLabel?: string;
  alert?: boolean;
}) {
  return (
    <div role={alert ? "alert" : undefined} data-slot="salesforce-notice" className="flex w-full flex-col items-center justify-center gap-1.5 px-4 py-6 text-center">
      <span className="mb-1 grid size-8 place-items-center rounded-md border border-dashed border-border-strong text-muted-foreground" aria-hidden="true">
        <Icon className="size-4" />
      </span>
      <p className="max-w-md font-label text-sm text-pretty text-foreground">{title}</p>
      {description && <p className="max-w-md text-2sm text-pretty text-muted-foreground">{description}</p>}
      {onRetry && (
        <Button type="button" variant="outline" size="sm" className="mt-2" onClick={onRetry}>
          <RefreshCw data-icon="inline-start" />
          {retryLabel}
        </Button>
      )}
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <span className="min-w-0 break-words">
      <span className="text-muted-foreground">{label}: </span>
      {value}
    </span>
  );
}

function experience(person: SalesforceCandidate): string {
  const years = person.years_experience == null ? "" : plural(person.years_experience, "year");
  return [years, person.experience_level].filter(Boolean).join(", ");
}

type RowState =
  | { kind: "import" }
  | { kind: "existing"; candidateId: string; chosen: boolean }
  | { kind: "elsewhere" }
  | { kind: "unavailable" };

function rowState(person: SalesforceCandidate, importedId: string | undefined, chosenIds: ReadonlySet<string> | undefined): RowState {
  const candidateId = importedId ?? person.imported_candidate_id;
  if (candidateId) return { kind: "existing", candidateId, chosen: chosenIds?.has(candidateId) ?? false };
  // "Always false for an Admin", and a Team Lead is told nothing more than this.
  if (person.imported_elsewhere) return { kind: "elsewhere" };
  return person.importable ? { kind: "import" } : { kind: "unavailable" };
}

function Row({
  person,
  state,
  busy,
  locked,
  problem,
  existingAction,
  onChoose,
}: {
  person: SalesforceCandidate;
  state: RowState;
  /** This row is being brought in or opened right now. */
  busy: boolean;
  /** Nothing can be chosen at the moment (another row is busy, or the form takes no more candidates). */
  locked: boolean;
  problem: string | undefined;
  existingAction: string;
  onChoose: () => void;
}) {
  const noteId = useId();
  const chosen = state.kind === "existing" && state.chosen;
  const available = state.kind === "import" || state.kind === "existing";
  const action = state.kind === "import" ? "Bring in" : state.kind === "existing" ? (chosen ? "Selected" : existingAction) : "";

  const body = (
    <>
      <span className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <span className="min-w-0 text-sm font-medium break-words">{person.name}</span>
        {person.candidate_status && <Tag>{person.candidate_status}</Tag>}
        {state.kind === "existing" && <Tag tone="primary">In the portal</Tag>}
        {person.has_resume ? (
          <Tag tone="success">
            <FileCheck2 aria-hidden="true" />
            Resume in Salesforce
          </Tag>
        ) : (
          <Tag tone="warning">
            <FileWarning aria-hidden="true" />
            No resume in Salesforce
          </Tag>
        )}
      </span>
      {(person.technology || person.niche) && (
        <span className="text-2sm break-words">{[person.technology, person.niche].filter(Boolean).join(", ")}</span>
      )}
      <span className="flex flex-wrap gap-x-4 gap-y-0.5 text-2sm">
        <Fact label="Visa" value={person.visa_status} />
        <Fact label="Work authorization" value={person.work_authorization} />
        <Fact label="Experience" value={experience(person)} />
        <Fact label="Job type" value={person.preferred_job_type} />
        <Fact label="Locations" value={person.preferred_locations} />
      </span>
      <span className="flex flex-wrap gap-x-4 gap-y-0.5 text-2sm">
        <Fact label="Recruiter" value={person.assigned_recruiter} />
        <Fact label="Marketing team lead" value={person.marketing_team_lead} />
        <Fact label="E-mail" value={person.email} />
        <Fact label="Phone" value={person.phone} />
      </span>
    </>
  );

  return (
    <li data-slot="salesforce-row" data-state={state.kind}>
      {available ? (
        <button
          type="button"
          data-result=""
          disabled={locked && !busy}
          aria-disabled={chosen || busy || undefined}
          aria-busy={busy || undefined}
          aria-describedby={problem ? noteId : undefined}
          onClick={chosen || busy ? undefined : onChoose}
          className={cn(
            "flex w-full flex-col gap-1.5 px-3 py-2.5 text-left outline-none hover:bg-muted/60 focus-visible:bg-muted/60 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:ring-inset disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-transparent",
            chosen && "cursor-default bg-accent/60 hover:bg-accent/60",
            busy && "cursor-progress",
          )}
        >
          {body}
          <span className="flex items-center gap-1.5 text-2sm font-medium text-primary">
            {busy && <Spinner className="size-3.5" />}
            {busy ? (state.kind === "import" ? "Bringing in details and resume" : "Opening") : action}
          </span>
        </button>
      ) : (
        <div className="flex flex-col gap-1.5 px-3 py-2.5 opacity-75">
          {body}
          <span className="text-2sm text-muted-foreground">
            {state.kind === "elsewhere"
              ? "Not available: this candidate is already in the portal with another team."
              : "Not available: you cannot bring this candidate in."}
          </span>
        </div>
      )}
      {problem && (
        <div id={noteId} className="px-3 pb-2.5">
          <FormMessage>{problem}</FormMessage>
        </div>
      )}
    </li>
  );
}

/**
 * Find a person in Salesforce and bring them into the portal. The search box belongs to the
 * screen around this (the Find Jobs candidate list shares it with the portal's own candidates);
 * everything else is here: the "Active candidates only" switch, the team an Admin brings the
 * candidate into, the results, and what happens when one is chosen.
 *
 * Choosing a person who is not in the portal yet imports them: details, profile and resume file
 * are read from Salesforce by the API. Choosing one who already is hands back that candidate
 * without importing again. Nothing is rendered when the portal is not connected to Salesforce.
 */
export function SalesforceImport({
  query,
  defaultTeamId,
  withMember = false,
  heading = false,
  chosenIds,
  full = false,
  existingAction = "Select",
  onChosen,
  className,
}: {
  /** What was typed in the search box, already debounced. */
  query: string;
  /** Admin: the team already chosen in the surrounding form, if any. Without one the team is asked for here. */
  defaultTeamId?: string;
  /** Also ask for the responsible member the candidate is brought in with. */
  withMember?: boolean;
  /** Show the "From Salesforce" title (where this is one of two groups). */
  heading?: boolean;
  /** Candidates the surrounding form already holds: their rows read "Selected". */
  chosenIds?: ReadonlySet<string>;
  /** The surrounding form takes no more candidates. */
  full?: boolean;
  /** What choosing someone who is already in the portal does, as the row says it. */
  existingAction?: string;
  /**
   * Called with the portal candidate that was brought in or that already existed.
   * A sentence returned from here is shown in the row (the form could not take the candidate).
   */
  onChosen: (candidate: Candidate, how: "imported" | "existing", sync?: SalesforceSync) => string | void;
  className?: string;
}) {
  const me = useMe();
  const isAdmin = me.role === "ADMIN";
  const client = useQueryClient();
  const switchId = useId();
  const teamSelectId = useId();
  const memberSelectId = useId();
  const titleId = useId();

  const [activeOnly, setActiveOnly] = useState(true);
  const [teamId, setTeamId] = useState(defaultTeamId ?? "");
  const [seenDefault, setSeenDefault] = useState(defaultTeamId);
  if (defaultTeamId !== seenDefault) {
    setSeenDefault(defaultTeamId);
    if (defaultTeamId) setTeamId(defaultTeamId);
  }
  const [memberId, setMemberId] = useState("");
  const [teamMissing, setTeamMissing] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [problems, setProblems] = useState<Record<string, string>>({});
  /** Brought in on this screen, before the result list has been read again. */
  const [imported, setImported] = useState<Record<string, string>>({});
  const [lastEvent, setLastEvent] = useState("");

  const status = useSalesforceStatus();
  const configured = status.data?.configured === true;
  const term = query.trim().slice(0, SALESFORCE_MAX_QUERY);
  const longEnough = term.length >= SALESFORCE_MIN_QUERY;
  const search = useSalesforceSearch({ q: term, status: activeOnly ? "active" : "all", limit: SALESFORCE_PAGE }, configured);
  const teams = useTeams(false, { enabled: isAdmin && configured });
  const importer = useImportSalesforceCandidate();

  // Not connected (or still unknown): say nothing. A portal without Salesforce looks as it did before.
  if (status.isPending || (status.isSuccess && !configured)) return null;

  const importTeamId = isAdmin ? teamId : (me.team?.id ?? "");
  const setProblem = (id: string, message: string | undefined) =>
    setProblems((current) => {
      const next = { ...current };
      if (message) next[id] = message;
      else delete next[id];
      return next;
    });

  const hand = (person: SalesforceCandidate, candidate: Candidate, how: "imported" | "existing", sync?: SalesforceSync) => {
    const refusal = onChosen(candidate, how, sync);
    setProblem(person.salesforce_id, refusal || undefined);
    setLastEvent(refusal || how === "existing" ? "" : `${candidate.name} was brought in from Salesforce.`);
  };

  const choose = (person: SalesforceCandidate, state: RowState) => {
    if (busyId) return;
    if (state.kind === "existing") {
      setBusyId(person.salesforce_id);
      client
        .fetchQuery({ queryKey: qk.candidate(state.candidateId), queryFn: () => api.get<Candidate>(`/api/candidates/${state.candidateId}`) })
        .then(
          (candidate) => hand(person, candidate, "existing"),
          (error: unknown) => setProblem(person.salesforce_id, errorMessage(error)),
        )
        .finally(() => setBusyId(null));
      return;
    }
    if (state.kind !== "import") return;
    if (isAdmin && !teamId) {
      setTeamMissing(true);
      document.getElementById(teamSelectId)?.focus();
      return;
    }
    setBusyId(person.salesforce_id);
    setProblem(person.salesforce_id, undefined);
    setLastEvent(`Bringing in details and resume for ${person.name}.`);
    importer
      .mutateAsync({
        salesforce_id: person.salesforce_id,
        team_id: isAdmin ? teamId : null,
        assigned_member_id: withMember && memberId ? memberId : null,
      })
      .then(
        ({ sync, ...candidate }) => {
          setImported((current) => ({ ...current, [person.salesforce_id]: candidate.id }));
          hand(person, candidate, "imported", sync);
        },
        (error: unknown) => {
          setLastEvent("");
          setProblem(person.salesforce_id, errorMessage(error));
        },
      )
      .finally(() => setBusyId(null));
  };

  const retry = () => {
    void status.refetch();
    if (longEnough) void search.refetch();
  };

  const people = search.data?.items ?? [];
  const failure = search.isError ? salesforceProblem(search.error) : null;
  const searching = longEnough && search.isPending && !failure;
  const announcement = !longEnough
    ? ""
    : searching
      ? "Searching Salesforce"
      : failure
        ? ""
        : people.length === 0
          ? "No candidate found in Salesforce"
          : `${plural(people.length, "candidate")} found in Salesforce`;

  let body: ReactNode;
  if (status.isError) {
    body = (
      <Notice
        alert
        icon={CloudOff}
        title="The connection to Salesforce could not be checked"
        description={errorMessage(status.error)}
        onRetry={retry}
      />
    );
  } else if (!longEnough && status.data?.connected === false) {
    body = (
      <Notice
        alert
        icon={CloudOff}
        title="Salesforce cannot be reached right now"
        description={status.data.message || "Candidates already in the portal are not affected. Try again in a moment."}
        onRetry={retry}
        retryLabel="Check again"
      />
    );
  } else if (!longEnough) {
    body = (
      <p className="px-3 py-4 text-center text-2sm text-muted-foreground">
        Type at least {SALESFORCE_MIN_QUERY} characters of a name, e-mail address or phone number in the search box to look in Salesforce.
      </p>
    );
  } else if (failure?.kind === "unreachable") {
    body = (
      <Notice
        alert
        icon={CloudOff}
        title="Salesforce cannot be reached right now"
        description={`${failure.message} Candidates already in the portal are not affected.`}
        onRetry={retry}
      />
    );
  } else if (failure?.kind === "rate") {
    body = (
      <Notice
        alert
        icon={Hourglass}
        title="Too many searches in a short time"
        description={`${failure.message} Wait a few seconds, then search again.`}
        onRetry={retry}
        retryLabel="Search again"
      />
    );
  } else if (failure) {
    body = <Notice alert icon={TriangleAlert} title="Salesforce could not be searched" description={failure.message} onRetry={retry} />;
  } else if (search.isPending) {
    body = <LoadingSkeleton variant="rows" count={3} />;
  } else if (people.length === 0) {
    body = (
      <Notice
        icon={SearchX}
        title={`Nobody in Salesforce matches "${term}"`}
        description={
          <>
            {activeOnly
              ? "Check the spelling. Only candidates whose status in Salesforce is Active are searched: switch off \"Active candidates only\" to include the others."
              : "Check the spelling, or try a shorter part of the name."}
            {!isAdmin && status.data?.team_lead_scope === "own" && " You can find the candidates whose marketing team lead in Salesforce is you."}
          </>
        }
      />
    );
  } else {
    body = (
      <ul className="divide-y">
        {people.map((person) => {
          const state = rowState(person, imported[person.salesforce_id], chosenIds);
          const busy = busyId === person.salesforce_id;
          // A candidate the form already holds stays reachable; everything else waits while the form is full.
          const locked = (busyId !== null && !busy) || (full && !(state.kind === "existing" && state.chosen));
          return (
            <Row
              key={person.salesforce_id}
              person={person}
              state={state}
              busy={busy}
              locked={locked}
              problem={problems[person.salesforce_id]}
              existingAction={existingAction}
              onChoose={() => choose(person, state)}
            />
          );
        })}
      </ul>
    );
  }

  return (
    <section data-slot="salesforce-import" aria-labelledby={heading ? titleId : undefined} aria-label={heading ? undefined : "From Salesforce"} className={cn("flex flex-col gap-2", className)}>
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        {heading ? (
          <h4 id={titleId} className="flex items-center gap-1.5 font-label text-sm">
            <Cloud className="size-4 text-muted-foreground" aria-hidden="true" />
            From Salesforce
          </h4>
        ) : (
          <span className="text-2sm text-muted-foreground">Read from Salesforce. Nothing is changed there.</span>
        )}
        <div className="flex items-center gap-2">
          <Switch id={switchId} checked={activeOnly} onCheckedChange={setActiveOnly} />
          <Label htmlFor={switchId} className="text-2sm font-normal">
            Active candidates only
          </Label>
        </div>
      </div>

      {(isAdmin || withMember) && (
        <div className={cn("grid gap-3", isAdmin && withMember && "sm:grid-cols-2")}>
          {isAdmin && (
            <div className="flex min-w-0 flex-col gap-1.5">
              <label htmlFor={teamSelectId} className="text-xs text-muted-foreground">
                Team the candidate is brought into
              </label>
              <Select
                value={teamId || undefined}
                onValueChange={(next) => {
                  setTeamId(next);
                  setTeamMissing(false);
                  // A member belongs to one team: the choice made for another team does not carry over.
                  setMemberId("");
                }}
                disabled={teams.isPending || busyId !== null}
              >
                <SelectTrigger
                  id={teamSelectId}
                  aria-invalid={teamMissing || undefined}
                  aria-describedby={teamMissing ? `${teamSelectId}-error` : undefined}
                  className={cn("w-full", !withMember && "sm:max-w-80")}
                >
                  <SelectValue placeholder={teams.isPending ? "Loading teams" : "Choose a team"} />
                </SelectTrigger>
                <SelectContent>
                  {(teams.data ?? []).map((team) => (
                    <SelectItem key={team.id} value={team.id}>
                      {team.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {teamMissing ? (
                <p id={`${teamSelectId}-error`} role="alert" className="text-2sm text-destructive">
                  Choose the team first, then choose the candidate again.
                </p>
              ) : (
                teams.isSuccess && teams.data.length === 0 && <p className="text-2sm text-muted-foreground">There are no active teams yet. Create a team first.</p>
              )}
            </div>
          )}
          {withMember && (
            <div className="flex min-w-0 flex-col gap-1.5">
              <label htmlFor={memberSelectId} className="text-xs text-muted-foreground">
                Responsible member <span className="font-normal">(optional)</span>
              </label>
              <MemberSelect id={memberSelectId} teamId={importTeamId || undefined} value={memberId} onChange={setMemberId} disabled={busyId !== null} />
            </div>
          )}
        </div>
      )}

      <p role="status" className="sr-only">
        {announcement}
      </p>
      <p role="status" className="sr-only">
        {lastEvent}
      </p>
      <div
        role="group"
        aria-label="Candidates in Salesforce"
        aria-busy={searching || undefined}
        className="max-h-96 overflow-y-auto overscroll-contain rounded-lg border bg-card"
      >
        {body}
      </div>
      {!failure && longEnough && search.data?.limit_reached && (
        <p className="text-2sm text-muted-foreground">
          Showing the first {people.length} matches, by name. There may be more: type more of the name to narrow the list.
        </p>
      )}
    </section>
  );
}
