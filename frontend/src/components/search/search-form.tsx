"use client";

import { useSearchParams } from "next/navigation";
import { useId, useState } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { Bookmark, Play, Trash2, TriangleAlert } from "lucide-react";
import { toast } from "sonner";
import { z } from "zod";

import { MemberPicker, toPicked, type PickedMember } from "@/components/search/member-picker";
import { useSearchProgress } from "@/components/search/search-progress";
import { MODE_LABELS } from "@/components/search/search-run";
import { FormField, FormMessage, TextField } from "@/components/shared/form-fields";
import { PageHeader, Panel } from "@/components/shared/page-header";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { TagInput } from "@/components/shared/tag-input";
import { useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { ApiError, errorMessage } from "@/lib/api";
import { plural } from "@/lib/format";
import { applyServerErrors } from "@/lib/forms";
import { normalizeState, SPONSORSHIP, SPONSORSHIP_LABELS, WORK_MODE_LABELS } from "@/lib/options";
import {
  useCreatePreset,
  useCreateSearch,
  useDeletePreset,
  usePresets,
  useSearchOptions,
  useTeams,
  useUser,
} from "@/lib/queries";
import type { Preset, SearchCreate, SearchOptions } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Value of a select that means "do not override: use what each member saved". */
const SAVED = "__saved__";
const SAVED_LABEL = "Use each member's saved value";

function buildSchema(limits: SearchOptions["limits"]) {
  const numberText = (min: number, max: number, message: string, integer: boolean) =>
    z
      .string()
      .trim()
      .refine((value) => {
        if (value === "") return true;
        const number = Number(value);
        return Number.isFinite(number) && number >= min && number <= max && (!integer || Number.isInteger(number));
      }, message);
  return z.object({
    mode: z.enum(["auto_assign", "review"]),
    target_count: numberText(1, 500, "Enter a whole number between 1 and 500.", true).refine((value) => value !== "", "Enter how many jobs to look for per member."),
    min_match_score: numberText(0, 100, "Enter a whole number between 0 and 100.", true),
    deep_search: z.boolean(),
    job_titles: z.array(z.string()).max(8, "Keep it to 8 job titles."),
    years_experience: numberText(0, 40, "Enter a number of years between 0 and 40.", false),
    work_authorization: z.string(),
    sponsorship_needed: z.string(),
    exclude_citizenship_required: z.string(),
    work_mode: z.string(),
    preferred_states: z.array(z.string()).max(30, "Keep it to 30 states."),
    excluded_companies: z.array(z.string()).max(100, "Keep it to 100 companies."),
  }).superRefine((values, context) => {
    // Kept here so the message sits on the field; the totals check lives beside the member list.
    if (values.target_count !== "" && Number(values.target_count) > limits.max_results_per_search_run) {
      context.addIssue({
        code: "custom",
        path: ["target_count"],
        message: `This portal allows at most ${limits.max_results_per_search_run} results per search.`,
      });
    }
  });
}
type Values = z.infer<ReturnType<typeof buildSchema>>;

const defaultsFor = (limits: SearchOptions["limits"]): Values => ({
  mode: "auto_assign",
  target_count: String(limits.default_target_count),
  min_match_score: String(limits.default_min_match_score),
  deep_search: true,
  job_titles: [],
  years_experience: "",
  work_authorization: SAVED,
  sponsorship_needed: SAVED,
  exclude_citizenship_required: SAVED,
  work_mode: SAVED,
  preferred_states: [],
  excluded_companies: [],
});

type Filters = SearchCreate["filters"];

function toRequest(values: Values, memberIds: string[], teamId: string): SearchCreate {
  const picked = <T extends string>(value: string) => (value === SAVED ? null : (value as T));
  const filters: Filters = {
    job_titles: values.job_titles.length ? values.job_titles : null,
    years_experience: values.years_experience === "" ? null : Number(values.years_experience),
    work_authorization: picked<NonNullable<Filters["work_authorization"]>>(values.work_authorization),
    sponsorship_needed: picked<NonNullable<Filters["sponsorship_needed"]>>(values.sponsorship_needed),
    exclude_citizenship_required: values.exclude_citizenship_required === SAVED ? null : values.exclude_citizenship_required === "true",
    work_mode: picked<NonNullable<Filters["work_mode"]>>(values.work_mode),
    preferred_states: values.preferred_states.length ? values.preferred_states : null,
    excluded_companies: values.excluded_companies.length ? values.excluded_companies : null,
  };
  return {
    member_ids: memberIds,
    team_id: teamId || null,
    mode: values.mode,
    target_count: Number(values.target_count),
    deep_search: values.deep_search,
    min_match_score: values.min_match_score === "" ? null : Number(values.min_match_score),
    filters,
  };
}

/** A saved preset is the request without its members. Unknown or missing parts fall back to the current values. */
function fromPreset(preset: Preset, current: Values): Values {
  const data = preset.filters as Record<string, unknown>;
  const filters = (data.filters ?? {}) as Record<string, unknown>;
  const list = (value: unknown) => (Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []);
  const choice = (value: unknown) => (typeof value === "string" && value ? value : SAVED);
  return {
    mode: data.mode === "review" ? "review" : data.mode === "auto_assign" ? "auto_assign" : current.mode,
    target_count: typeof data.target_count === "number" ? String(data.target_count) : current.target_count,
    min_match_score: typeof data.min_match_score === "number" ? String(data.min_match_score) : "",
    deep_search: typeof data.deep_search === "boolean" ? data.deep_search : current.deep_search,
    job_titles: list(filters.job_titles),
    years_experience: typeof filters.years_experience === "number" ? String(filters.years_experience) : "",
    work_authorization: choice(filters.work_authorization),
    sponsorship_needed: choice(filters.sponsorship_needed),
    exclude_citizenship_required: typeof filters.exclude_citizenship_required === "boolean" ? String(filters.exclude_citizenship_required) : SAVED,
    work_mode: choice(filters.work_mode),
    preferred_states: list(filters.preferred_states),
    excluded_companies: list(filters.excluded_companies),
  };
}

function OverrideSelect({
  label,
  value,
  onChange,
  options,
  description,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: Array<{ value: string; label: string }>;
  description?: string;
}) {
  return (
    <FormField label={label} description={description}>
      {({ id, describedBy }) => (
        <Select value={value} onValueChange={onChange}>
          <SelectTrigger id={id} aria-describedby={describedBy} className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={SAVED}>{SAVED_LABEL}</SelectItem>
            {options.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
    </FormField>
  );
}

function Presets({ getValues, onApply }: { getValues: () => Values; onApply: (preset: Preset) => void }) {
  const nameId = useId();
  const presets = usePresets();
  const create = useCreatePreset();
  const remove = useDeletePreset();
  const [name, setName] = useState("");
  const [open, setOpen] = useState(false);

  const save = () => {
    // A preset is the search request without its members.
    const { mode, target_count, deep_search, min_match_score, filters } = toRequest(getValues(), [], "");
    create.mutate(
      { name: name.trim(), filters: { mode, target_count, deep_search, min_match_score, filters } },
      {
        onSuccess: (preset) => {
          setName("");
          toast.success(`Preset "${preset.name}" saved`);
        },
        onError: (error) => toast.error(errorMessage(error)),
      },
    );
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button type="button" variant="outline">
          <Bookmark data-icon="inline-start" />
          Presets
          {presets.data && presets.data.length > 0 && <span className="text-muted-foreground tabular-nums">({presets.data.length})</span>}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[min(22rem,calc(100vw-2rem))] gap-3 p-3">
        <p className="font-label text-sm">Saved search settings</p>
        {presets.isPending ? (
          <LoadingSkeleton variant="list" count={2} />
        ) : presets.isError ? (
          <ErrorState compact error={presets.error} onRetry={() => presets.refetch()} />
        ) : presets.data.length === 0 ? (
          <p className="text-2sm text-muted-foreground">
            No presets yet. Set the search up the way you like, then save it here to reuse it.
          </p>
        ) : (
          <ul className="flex max-h-56 flex-col divide-y overflow-y-auto rounded-md border">
            {presets.data.map((preset) => (
              <li key={preset.id} className="flex items-center gap-1 py-1 pr-1 pl-2.5">
                <span className="min-w-0 flex-1 truncate text-sm">{preset.name}</span>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    onApply(preset);
                    setOpen(false);
                  }}
                >
                  Apply
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Delete preset ${preset.name}`}
                  disabled={remove.isPending}
                  onClick={() =>
                    remove.mutate(preset.id, {
                      onSuccess: () => toast.success("Preset deleted"),
                      onError: (error) => toast.error(errorMessage(error)),
                    })
                  }
                >
                  <Trash2 />
                </Button>
              </li>
            ))}
          </ul>
        )}
        <div className="flex flex-col gap-2 border-t pt-3">
          <Label htmlFor={nameId}>Save the current settings as</Label>
          <div className="flex gap-2">
            <Input
              id={nameId}
              value={name}
              maxLength={120}
              placeholder="Remote ML roles"
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && name.trim().length >= 2) {
                  event.preventDefault();
                  save();
                }
              }}
            />
            <Button type="button" disabled={name.trim().length < 2 || create.isPending} onClick={save}>
              {create.isPending && <Spinner data-icon="inline-start" />}
              Save
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">Members are not part of a preset.</p>
        </div>
      </PopoverContent>
    </Popover>
  );
}

function Form({ options }: { options: SearchOptions }) {
  const me = useMe();
  const isAdmin = me.role === "ADMIN";
  const limits = options.limits;
  const progress = useSearchProgress();
  const create = useCreateSearch();
  const teams = useTeams(false, { enabled: isAdmin });
  const deepId = useId();
  const teamSelectId = useId();

  const [selected, setSelected] = useState<ReadonlyMap<string, PickedMember>>(() => new Map());
  const [teamId, setTeamId] = useState("");
  const [membersError, setMembersError] = useState<string | null>(null);

  // "Find jobs for this member" links arrive with ?member=<id>: start with that member ticked.
  const presetMemberId = useSearchParams().get("member") ?? undefined;
  const presetMember = useUser(presetMemberId);
  const [appliedPreset, setAppliedPreset] = useState<string | null>(null);
  if (presetMember.data && presetMember.data.role === "MEMBER" && appliedPreset !== presetMember.data.id) {
    setAppliedPreset(presetMember.data.id);
    setSelected(new Map([[presetMember.data.id, toPicked(presetMember.data)]]));
  }

  const form = useForm<Values>({ resolver: zodResolver(buildSchema(limits)), defaultValues: defaultsFor(limits) });
  const { errors } = form.formState;
  const mode = useWatch({ control: form.control, name: "mode" });
  const targetText = useWatch({ control: form.control, name: "target_count" });

  const team = (teams.data ?? []).find((item) => item.id === teamId);
  const memberCount = selected.size + (team?.member_count ?? 0);
  const perMember = Number(targetText) || 0;
  const totalRequested = memberCount * perMember;
  const tooManyMembers = memberCount > limits.max_members_per_search;
  const tooManyResults = totalRequested > limits.max_results_per_search_run;
  const withoutResume = [...selected.values()].filter((member) => !member.hasResume);

  const aiConfigured = Object.values(options.ai_providers as Record<string, { configured?: boolean }>).some((provider) => provider.configured);
  const sources = (options.sources as Array<{ name: string; label: string; enabled?: boolean }>).filter((source) => source.enabled !== false);

  const onSubmit = form.handleSubmit((values) => {
    if (memberCount === 0) {
      setMembersError(isAdmin ? "Choose at least one member, or a whole team." : "Choose at least one member.");
      return;
    }
    setMembersError(null);
    create.mutate(toRequest(values, [...selected.keys()], teamId), {
      onSuccess: (created) => progress.start(created),
      onError: (error) => {
        applyServerErrors(form, error);
      },
    });
  });
  const failure = create.error && !(create.error instanceof ApiError && Object.keys(create.error.fields).length > 0);

  return (
    <form onSubmit={onSubmit} noValidate>
      <PageHeader
        title="Find Jobs"
        description="Search live job postings for one or more members and match them against each member's resume and profile."
        actions={<Presets getValues={form.getValues} onApply={(preset) => form.reset(fromPreset(preset, form.getValues()))} />}
      />

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_21rem]">
        <div className="flex min-w-0 flex-col gap-5">
          <Panel
            title="Who to search for"
            description={
              isAdmin
                ? "Choose a whole team, individual members, or both."
                : `Members of ${me.team?.name ?? "your team"}. Each one is searched with their own resume and profile.`
            }
          >
            <div className="flex flex-col gap-4">
              {isAdmin && (
                <div className="flex flex-col gap-2">
                  <Label htmlFor={teamSelectId}>Whole team</Label>
                  <Select value={teamId || SAVED} onValueChange={(value) => setTeamId(value === SAVED ? "" : value)}>
                    <SelectTrigger id={teamSelectId} className="w-full sm:w-80">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={SAVED}>No whole team</SelectItem>
                      {(teams.data ?? []).map((item) => (
                        <SelectItem key={item.id} value={item.id}>
                          {item.name} ({plural(item.member_count, "active member")})
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  {team && (
                    <p className="text-2sm text-muted-foreground">
                      Every active member of {team.name} is included. You can add members of other teams below.
                    </p>
                  )}
                </div>
              )}
              <MemberPicker
                selected={selected}
                onChange={(next) => {
                  setSelected(next);
                  setMembersError(null);
                }}
                showTeam={isAdmin}
                max={limits.max_members_per_search}
              />
              {membersError && <FormMessage>{membersError}</FormMessage>}
              {tooManyMembers && (
                <FormMessage>
                  {memberCount} members are selected. One search covers at most {limits.max_members_per_search}.
                </FormMessage>
              )}
              {withoutResume.length > 0 && (
                <FormMessage tone="info">
                  <span className="flex items-start gap-2">
                    <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
                    <span>
                      {withoutResume.map((member) => member.name).join(", ")} {withoutResume.length === 1 ? "has" : "have"} no
                      resume. The search then relies on the skills in the profile, and leaves the member out if there
                      are none.
                    </span>
                  </span>
                </FormMessage>
              )}
            </div>
          </Panel>

          <Panel
            title="What to search for"
            description="These settings apply to every selected member. Leave a field empty to use each member's own saved value."
          >
            <div className="flex flex-col gap-5">
              <Controller
                control={form.control}
                name="job_titles"
                render={({ field }) => (
                  <TagInput
                    label="Job titles"
                    optional
                    value={field.value}
                    onChange={field.onChange}
                    placeholder="Empty: each member's saved titles"
                    description="Press Enter or type a comma after each title."
                    max={8}
                    error={errors.job_titles?.message}
                  />
                )}
              />
              <div className="grid gap-5 sm:grid-cols-2">
                <TextField
                  label="Years of experience"
                  optional
                  inputMode="decimal"
                  autoComplete="off"
                  placeholder="Empty: each member's saved value"
                  error={errors.years_experience?.message}
                  {...form.register("years_experience")}
                />
                <Controller
                  control={form.control}
                  name="work_mode"
                  render={({ field }) => (
                    <OverrideSelect
                      label="Work arrangement"
                      value={field.value}
                      onChange={field.onChange}
                      options={options.work_modes.map((value) => ({
                        value,
                        label: WORK_MODE_LABELS[value as keyof typeof WORK_MODE_LABELS] ?? value,
                      }))}
                    />
                  )}
                />
                <Controller
                  control={form.control}
                  name="work_authorization"
                  render={({ field }) => (
                    <OverrideSelect
                      label="Work authorization"
                      value={field.value}
                      onChange={field.onChange}
                      options={options.authorizations.map((value) => ({ value, label: value }))}
                    />
                  )}
                />
                <Controller
                  control={form.control}
                  name="sponsorship_needed"
                  render={({ field }) => (
                    <OverrideSelect
                      label="Visa sponsorship needed"
                      value={field.value}
                      onChange={field.onChange}
                      options={SPONSORSHIP.map((value) => ({ value, label: SPONSORSHIP_LABELS[value] }))}
                    />
                  )}
                />
                <Controller
                  control={form.control}
                  name="exclude_citizenship_required"
                  render={({ field }) => (
                    <OverrideSelect
                      label="Jobs that require US citizenship"
                      value={field.value}
                      onChange={field.onChange}
                      options={[
                        { value: "true", label: "Leave them out" },
                        { value: "false", label: "Keep them in" },
                      ]}
                    />
                  )}
                />
              </div>
              <Controller
                control={form.control}
                name="preferred_states"
                render={({ field }) => (
                  <TagInput
                    label="Preferred states"
                    optional
                    value={field.value}
                    onChange={field.onChange}
                    placeholder="Empty: each member's saved states"
                    description="Two-letter codes or full names. Remote jobs are always kept."
                    max={30}
                    transform={normalizeState}
                    error={errors.preferred_states?.message}
                  />
                )}
              />
              <Controller
                control={form.control}
                name="excluded_companies"
                render={({ field }) => (
                  <TagInput
                    label="Companies to leave out"
                    optional
                    value={field.value}
                    onChange={field.onChange}
                    placeholder="Empty: each member's saved list"
                    max={100}
                    error={errors.excluded_companies?.message}
                  />
                )}
              />
            </div>
          </Panel>

          <Panel title="How much, and how thoroughly">
            <div className="flex flex-col gap-5">
              <div className="grid gap-5 sm:grid-cols-2">
                <TextField
                  label="Results per member"
                  inputMode="numeric"
                  autoComplete="off"
                  description={`The number of jobs to look for, for each member separately. At most ${limits.max_results_per_search_run} in total per search.`}
                  error={errors.target_count?.message}
                  {...form.register("target_count")}
                />
                <TextField
                  label="Minimum match score"
                  optional
                  inputMode="numeric"
                  autoComplete="off"
                  description={`0 to 100. Only jobs scoring at least this are assigned automatically. Empty uses the portal default of ${limits.default_min_match_score}.`}
                  error={errors.min_match_score?.message}
                  {...form.register("min_match_score")}
                />
              </div>
              <div className="flex items-start gap-3">
                <Controller
                  control={form.control}
                  name="deep_search"
                  render={({ field }) => (
                    <Switch id={deepId} checked={field.value} onCheckedChange={field.onChange} ref={field.ref} className="mt-0.5" />
                  )}
                />
                <div className="flex flex-col gap-1">
                  <Label htmlFor={deepId}>Deep search</Label>
                  <p className="text-2sm text-muted-foreground">
                    Adds AI web search and a browser crawl to the employer job boards: wider coverage, and noticeably
                    slower. Turn it off for the fastest result.
                  </p>
                  {!aiConfigured && (
                    <p className="text-2sm text-warning">
                      No AI provider is configured on this server, so jobs are scored by keywords and no AI web search
                      runs.
                    </p>
                  )}
                </div>
              </div>
            </div>
          </Panel>

          <Panel title="What happens with the results">
            <Controller
              control={form.control}
              name="mode"
              render={({ field }) => (
                <RadioGroup value={field.value} onValueChange={field.onChange} aria-label="What happens with the results" className="gap-3 sm:grid-cols-2">
                  {(
                    [
                      {
                        value: "auto_assign",
                        text: "Jobs at or above the minimum match score are assigned to each member as soon as they are found.",
                      },
                      {
                        value: "review",
                        text: "Nothing is assigned yet. You look through the results for each member and assign the ones you choose.",
                      },
                    ] as const
                  ).map((option) => {
                    const id = `${deepId}-${option.value}`;
                    return (
                      <label
                        key={option.value}
                        htmlFor={id}
                        className={cn(
                          "flex cursor-pointer items-start gap-3 rounded-lg border bg-card p-3.5 transition-colors hover:bg-muted/50 has-[button:focus-visible]:border-ring has-[button:focus-visible]:ring-3 has-[button:focus-visible]:ring-ring/50",
                          field.value === option.value && "border-primary bg-accent/50",
                        )}
                      >
                        <RadioGroupItem id={id} value={option.value} className="mt-0.5" />
                        <span className="flex flex-col gap-1">
                          <span className="text-sm font-medium">{MODE_LABELS[option.value]}</span>
                          <span className="text-2sm text-muted-foreground">{option.text}</span>
                        </span>
                      </label>
                    );
                  })}
                </RadioGroup>
              )}
            />
          </Panel>
        </div>

        <aside className="min-w-0">
          <Panel title="This search" className="xl:sticky xl:top-18">
            <div className="flex flex-col gap-4">
              <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-2 text-sm">
                <dt className="text-muted-foreground">Members</dt>
                <dd className="text-right tabular-nums">
                  {team && selected.size > 0 ? "up to " : ""}
                  {memberCount}
                </dd>
                <dt className="text-muted-foreground">Results per member</dt>
                <dd className="text-right tabular-nums">{perMember || "—"}</dd>
                <dt className="text-muted-foreground">Requested in total</dt>
                <dd className="text-right font-semibold tabular-nums">up to {totalRequested.toLocaleString()}</dd>
              </dl>
              <p className="text-2sm text-muted-foreground">
                The count is per member: {memberCount || "no"} {memberCount === 1 ? "member" : "members"} times {perMember || 0} jobs. Only real,
                currently listed postings are returned, so a search may find fewer.
              </p>
              {tooManyResults && (
                <FormMessage>
                  {totalRequested.toLocaleString()} results is over this portal&apos;s limit of{" "}
                  {limits.max_results_per_search_run.toLocaleString()} per search. Lower the number per member, or select
                  fewer members.
                </FormMessage>
              )}
              <div className="flex flex-col gap-1.5 border-t pt-3">
                <p className="text-2sm text-muted-foreground">Result handling</p>
                <p className="text-sm font-medium">{MODE_LABELS[mode]}</p>
              </div>
              <div className="flex flex-col gap-2 border-t pt-3">
                <p className="text-2sm text-muted-foreground">Sources that will be read ({sources.length})</p>
                <ul className="flex flex-wrap gap-1.5">
                  {sources.map((source) => (
                    <li key={source.name}>
                      <Tag>{source.label}</Tag>
                    </li>
                  ))}
                </ul>
              </div>
              {!options.queue_available && (
                <FormMessage>
                  The background worker queue is not reachable, so a search cannot be started right now. Ask an Admin
                  to check the worker.
                </FormMessage>
              )}
              {failure && <FormMessage>{errorMessage(create.error)}</FormMessage>}
              <Button
                type="submit"
                size="lg"
                disabled={create.isPending || tooManyMembers || tooManyResults || !options.queue_available}
              >
                {create.isPending ? <Spinner data-icon="inline-start" /> : <Play data-icon="inline-start" />}
                {create.isPending ? "Starting search" : "Start Search"}
              </Button>
              <p className="text-xs text-muted-foreground">
                The search runs in the background. You can keep working, and it continues if you close this page.
              </p>
            </div>
          </Panel>
        </aside>
      </div>
    </form>
  );
}

/**
 * Start an AI job search for chosen members: who, with which overrides, how
 * many results per member, and whether matches are assigned at once or reviewed
 * first. Limits and choices come from GET /api/searches/options.
 */
export function SearchForm() {
  const options = useSearchOptions();
  if (options.isPending) {
    return (
      <>
        <PageHeader title="Find Jobs" />
        <LoadingSkeleton variant="form" count={5} />
      </>
    );
  }
  if (options.isError) {
    return (
      <>
        <PageHeader title="Find Jobs" />
        <div className="rounded-lg border bg-card">
          <ErrorState error={options.error} onRetry={() => options.refetch()} />
        </div>
      </>
    );
  }
  return <Form options={options.data} />;
}
