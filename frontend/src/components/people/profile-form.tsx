"use client";

import { useId } from "react";
import { Controller, useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { CircleCheck, CircleDashed } from "lucide-react";
import { toast } from "sonner";
import { z } from "zod";

import { FormField, FormMessage, TextField } from "@/components/shared/form-fields";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { TagInput } from "@/components/shared/tag-input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { ApiError, errorMessage } from "@/lib/api";
import { applyServerErrors } from "@/lib/forms";
import {
  AUTHORIZATIONS,
  normalizeState,
  SPONSORSHIP,
  SPONSORSHIP_LABELS,
  WORK_MODE_LABELS,
  WORK_MODES,
} from "@/lib/options";
import { useProfile, useUpdateProfile } from "@/lib/queries";
import type { Profile, ProfileIn } from "@/lib/types";

const numberText = (max: number, message: string, integer = false) =>
  z
    .string()
    .trim()
    .refine((value) => {
      if (value === "") return true;
      const number = Number(value);
      return Number.isFinite(number) && number >= 0 && number <= max && (!integer || Number.isInteger(number));
    }, message);

const schema = z.object({
  desired_titles: z.array(z.string()).max(8, "Keep it to 8 job titles."),
  years_experience: numberText(40, "Enter a number of years between 0 and 40."),
  seniority: z.string().trim().max(40, "Use at most 40 characters."),
  skills: z.array(z.string()).max(120, "Keep it to 120 skills."),
  required_keywords: z.array(z.string()).max(30, "Keep it to 30 keywords."),
  work_mode: z.enum(WORK_MODES),
  preferred_states: z.array(z.string()).max(30, "Keep it to 30 states."),
  preferred_cities: z.array(z.string()).max(30, "Keep it to 30 cities."),
  work_authorization: z.enum(AUTHORIZATIONS),
  sponsorship_needed: z.enum(SPONSORSHIP),
  exclude_citizenship_required: z.boolean(),
  salary_min: numberText(5_000_000, "Enter a whole amount between 0 and 5,000,000.", true),
  excluded_companies: z.array(z.string()).max(100, "Keep it to 100 companies."),
});
type Values = z.infer<typeof schema>;

const oneOf = <T extends string>(options: readonly T[], value: string, fallback: T): T =>
  (options as readonly string[]).includes(value) ? (value as T) : fallback;

function toValues(profile: Profile): Values {
  return {
    desired_titles: profile.desired_titles,
    years_experience: profile.years_experience === null || profile.years_experience === undefined ? "" : String(profile.years_experience),
    seniority: profile.seniority,
    skills: profile.skills,
    required_keywords: profile.required_keywords,
    work_mode: oneOf(WORK_MODES, profile.work_mode, "All"),
    preferred_states: profile.preferred_states,
    preferred_cities: profile.preferred_cities,
    work_authorization: oneOf(AUTHORIZATIONS, profile.work_authorization, "Other / Prefer Not to Say"),
    sponsorship_needed: oneOf(SPONSORSHIP, profile.sponsorship_needed, "Unknown"),
    exclude_citizenship_required: profile.exclude_citizenship_required,
    salary_min: profile.salary_min === null || profile.salary_min === undefined ? "" : String(profile.salary_min),
    excluded_companies: profile.excluded_companies,
  };
}

function toBody(values: Values): ProfileIn {
  return {
    ...values,
    years_experience: values.years_experience === "" ? null : Number(values.years_experience),
    salary_min: values.salary_min === "" ? null : Number(values.salary_min),
  };
}

/** How complete the profile is, with the API's own list of what is still missing. */
export function ProfileCompletion({ profile }: { profile: Profile }) {
  const done = profile.completion >= 100;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-end justify-between gap-3">
        <p className="flex items-baseline gap-2">
          <span className="font-figure text-4xl leading-none">{profile.completion}%</span>
          <span className="text-sm text-muted-foreground">complete</span>
        </p>
      </div>
      <div
        role="progressbar"
        aria-label="Profile completion"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={profile.completion}
        className="h-2.5 overflow-hidden rounded-[3px] bg-foreground/8"
      >
        <div className="h-full rounded-[3px] bg-primary transition-[width] duration-300" style={{ width: `${profile.completion}%` }} />
      </div>
      {done ? (
        <p className="flex items-center gap-2 text-sm">
          <CircleCheck className="size-4 text-success" aria-hidden="true" />
          Everything a search needs is here.
        </p>
      ) : (
        <div className="flex flex-col gap-1.5">
          <p className="text-2sm text-muted-foreground">Still missing</p>
          <ul className="flex flex-col gap-1">
            {profile.missing.map((hint) => (
              <li key={hint} className="flex items-start gap-2 text-sm">
                <CircleDashed className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                {hint}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function Group({ title, description, children }: { title: string; description?: string; children: React.ReactNode }) {
  return (
    <fieldset className="grid gap-x-8 gap-y-5 border-t pt-6 first:border-t-0 first:pt-0 lg:grid-cols-[14rem_1fr]">
      <legend className="sr-only">{title}</legend>
      <div aria-hidden="true" className="flex flex-col gap-1">
        <h3 className="font-label text-md">{title}</h3>
        {description && <p className="text-2sm text-muted-foreground">{description}</p>}
      </div>
      <div className="flex min-w-0 flex-col gap-5">{children}</div>
    </fieldset>
  );
}

function Editor({ profile, memberId, onBehalfOf }: { profile: Profile; memberId: string; onBehalfOf?: string }) {
  const switchId = useId();
  const save = useUpdateProfile(memberId);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: toValues(profile) });
  const { errors, isDirty } = form.formState;

  const onSubmit = form.handleSubmit((values) => {
    save.mutate(toBody(values), {
      onSuccess: (saved) => {
        form.reset(toValues(saved));
        toast.success("Profile saved");
      },
      onError: (error) => {
        applyServerErrors(form, error);
      },
    });
  });
  const failure = save.error && !(save.error instanceof ApiError && Object.keys(save.error.fields).length > 0);
  const extra = Object.entries(profile.extra ?? {}).filter(([, value]) => value !== null && value !== "");

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col gap-6">
      <Group title="What to look for" description="Searches use these to find and rank jobs.">
        <Controller
          control={form.control}
          name="desired_titles"
          render={({ field }) => (
            <TagInput
              label="Desired job titles"
              value={field.value}
              onChange={field.onChange}
              placeholder="Machine Learning Engineer"
              description="Press Enter or type a comma after each title."
              max={8}
              error={errors.desired_titles?.message}
            />
          )}
        />
        <div className="grid gap-5 sm:grid-cols-2">
          <TextField
            label="Years of experience"
            inputMode="decimal"
            autoComplete="off"
            error={errors.years_experience?.message}
            {...form.register("years_experience")}
          />
          <TextField
            label="Seniority"
            optional
            placeholder="Senior"
            autoComplete="off"
            error={errors.seniority?.message}
            {...form.register("seniority")}
          />
        </div>
        <Controller
          control={form.control}
          name="skills"
          render={({ field }) => (
            <TagInput
              label="Skills"
              value={field.value}
              onChange={field.onChange}
              placeholder="Python, SQL, PyTorch"
              description="Paste a comma separated list to add several at once."
              max={120}
              error={errors.skills?.message}
            />
          )}
        />
        <Controller
          control={form.control}
          name="required_keywords"
          render={({ field }) => (
            <TagInput
              label="Required keywords"
              optional
              value={field.value}
              onChange={field.onChange}
              description="Words a posting should contain."
              max={30}
              error={errors.required_keywords?.message}
            />
          )}
        />
      </Group>

      <Group title="Where" description="Leave the locations empty to consider anywhere in the United States.">
        <Controller
          control={form.control}
          name="work_mode"
          render={({ field }) => (
            <FormField label="Work arrangement" error={errors.work_mode?.message}>
              {({ id }) => (
                <Select value={field.value} onValueChange={field.onChange}>
                  <SelectTrigger id={id} className="w-full sm:w-64">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {WORK_MODES.map((mode) => (
                      <SelectItem key={mode} value={mode}>
                        {WORK_MODE_LABELS[mode]}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </FormField>
          )}
        />
        <Controller
          control={form.control}
          name="preferred_states"
          render={({ field }) => (
            <TagInput
              label="Preferred states"
              optional
              value={field.value}
              onChange={field.onChange}
              placeholder="NY, WA"
              description="Two-letter codes or full names."
              max={30}
              transform={normalizeState}
              error={errors.preferred_states?.message}
            />
          )}
        />
        <Controller
          control={form.control}
          name="preferred_cities"
          render={({ field }) => (
            <TagInput
              label="Preferred cities"
              optional
              value={field.value}
              onChange={field.onChange}
              placeholder="Seattle"
              max={30}
              error={errors.preferred_cities?.message}
            />
          )}
        />
      </Group>

      <Group
        title="Work authorization"
        description="Used only to screen postings that state a requirement. It is never used to score a job."
      >
        <Controller
          control={form.control}
          name="work_authorization"
          render={({ field }) => (
            <FormField label="Work authorization in the United States" error={errors.work_authorization?.message}>
              {({ id }) => (
                <Select value={field.value} onValueChange={field.onChange}>
                  <SelectTrigger id={id} className="w-full sm:w-96">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {AUTHORIZATIONS.map((option) => (
                      <SelectItem key={option} value={option}>
                        {option}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </FormField>
          )}
        />
        <Controller
          control={form.control}
          name="sponsorship_needed"
          render={({ field }) => (
            <FormField label="Visa sponsorship" error={errors.sponsorship_needed?.message}>
              {({ id }) => (
                <Select value={field.value} onValueChange={field.onChange}>
                  <SelectTrigger id={id} className="w-full sm:w-64">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {SPONSORSHIP.map((option) => (
                      <SelectItem key={option} value={option}>
                        {SPONSORSHIP_LABELS[option]}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </FormField>
          )}
        />
        <div className="flex items-start gap-3">
          <Controller
            control={form.control}
            name="exclude_citizenship_required"
            render={({ field }) => (
              <Switch id={switchId} checked={field.value} onCheckedChange={field.onChange} ref={field.ref} className="mt-0.5" />
            )}
          />
          <div className="flex flex-col gap-1">
            <Label htmlFor={switchId}>Leave out jobs that require US citizenship</Label>
            <p className="text-2sm text-muted-foreground">
              Postings that state a citizenship or clearance requirement are removed from search results.
            </p>
          </div>
        </div>
      </Group>

      <Group title="Pay and exclusions">
        <TextField
          label="Minimum salary (per year)"
          optional
          inputMode="numeric"
          autoComplete="off"
          placeholder="120000"
          className="sm:max-w-64"
          error={errors.salary_min?.message}
          {...form.register("salary_min")}
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
              description="Jobs at these employers are removed from search results."
              max={100}
              error={errors.excluded_companies?.message}
            />
          )}
        />
        {extra.length > 0 && (
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 rounded-md border bg-muted/40 px-3 py-2.5 text-sm">
            {extra.map(([key, value]) => (
              <div key={key} className="contents">
                <dt className="text-muted-foreground">{key}</dt>
                <dd className="break-words">{String(value)}</dd>
              </div>
            ))}
          </dl>
        )}
      </Group>

      <div className="flex flex-wrap items-center justify-end gap-3 border-t pt-4">
        {failure && <FormMessage className="mr-auto">{errorMessage(save.error)}</FormMessage>}
        {onBehalfOf && <span className="mr-auto text-2sm text-muted-foreground">You are editing {onBehalfOf}&apos;s profile.</span>}
        {isDirty && (
          <Button type="button" variant="ghost" onClick={() => form.reset()} disabled={save.isPending}>
            Discard changes
          </Button>
        )}
        <Button type="submit" disabled={save.isPending || !isDirty}>
          {save.isPending && <Spinner data-icon="inline-start" />}
          Save Profile
        </Button>
      </div>
    </form>
  );
}

/** A member's search profile: what jobs to look for, where, and under which work authorization. */
export function ProfileForm({ memberId, onBehalfOf }: { memberId: string; onBehalfOf?: string }) {
  const profile = useProfile(memberId);
  if (profile.isPending) return <LoadingSkeleton variant="form" count={5} />;
  if (profile.isError) return <ErrorState compact error={profile.error} onRetry={() => profile.refetch()} />;
  return <Editor key={memberId} profile={profile.data} memberId={memberId} onBehalfOf={onBehalfOf} />;
}
