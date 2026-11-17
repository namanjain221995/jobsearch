"use client";

import { useEffect, useId, useMemo } from "react";
import { Controller, useForm, type UseFormReturn } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { useQueryClient } from "@tanstack/react-query";
import { CircleCheck, CircleDashed } from "lucide-react";
import { toast } from "sonner";
import { z } from "@/lib/zod";

import { FormField, FormMessage, TextField } from "@/components/shared/form-fields";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { TagInput } from "@/components/shared/tag-input";
import { useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { Field, FieldError, FieldTitle } from "@/components/ui/field";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { ApiError, errorMessage } from "@/lib/api";
import {
  applyServerErrors,
  EXTRA_ANSWER_MAX,
  EXTRA_NO,
  EXTRA_YES,
  extraAnswerProblem,
  extraAnswerText,
  extraAnswerValue,
  isGeneralError,
} from "@/lib/forms";
import {
  AUTHORIZATIONS,
  normalizeState,
  SPONSORSHIP,
  SPONSORSHIP_LABELS,
  WORK_MODE_LABELS,
  WORK_MODES,
} from "@/lib/options";
import { qk, useProfile, useUpdateProfile } from "@/lib/queries";
import type { ExtraField, Profile, ProfileIn } from "@/lib/types";

const numberText = (max: number, message: string, integer = false) =>
  z
    .string()
    .trim()
    .refine((value) => {
      if (value === "") return true;
      const number = Number(value);
      return Number.isFinite(number) && number >= 0 && number <= max && (!integer || Number.isInteger(number));
    }, message);

/**
 * The shape of the form. What an extra answer has to be depends on the fields the Admin defined,
 * so that part of the checking is added by `buildSchema`.
 */
const baseSchema = z.object({
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
  /** Answers to the extra fields the Admin defined: text, each at `slot(key)`. A field that has just appeared has none yet. */
  extra: z.record(z.string(), z.string().optional()),
});
type Values = z.infer<typeof baseSchema>;

/**
 * Where an extra field's answer sits in the form: under `extra`, at its key with a prefix. A key can
 * be any lower-case word, "constructor" among them, and every JavaScript object already has one of
 * those. The prefix keeps the form's bookkeeping from taking that for an answer or an error.
 */
const slot = (key: string) => `f_${key}`;

/** The built-in fields, and each extra field checked as its own type asks. */
const buildSchema = (fields: ExtraField[]) =>
  baseSchema.superRefine((values, context) => {
    for (const field of fields) {
      const problem = extraAnswerProblem(field, values.extra[slot(field.key)] ?? "");
      if (problem) context.addIssue({ code: "custom", path: ["extra", slot(field.key)], message: problem });
    }
  });

const oneOf = <T extends string>(options: readonly T[], value: string, fallback: T): T =>
  (options as readonly string[]).includes(value) ? (value as T) : fallback;

function toValues(profile: Profile, fields: ExtraField[]): Values {
  const stored = profile.extra ?? {};
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
    // Only the fields that are defined now. An answer to a field the Admin removed stays stored and is not shown.
    extra: Object.fromEntries(
      fields.map((field) => [
        slot(field.key),
        extraAnswerText(field, Object.hasOwn(stored, field.key) ? stored[field.key] : undefined),
      ]),
    ),
  };
}

function toBody(values: Values, fields: ExtraField[]): ProfileIn {
  return {
    ...values,
    years_experience: values.years_experience === "" ? null : Number(values.years_experience),
    salary_min: values.salary_min === "" ? null : Number(values.salary_min),
    extra: Object.fromEntries(
      fields.map((field) => [field.key, extraAnswerValue(field, values.extra[slot(field.key)] ?? "")]),
    ),
  };
}

/**
 * Only what the person changed is sent. The profile can change while the form is
 * open (uploading a resume fills in the skills, a manager can edit it too), and a
 * field that was not touched here must not write an older value over that.
 */
function changedFields(values: Values, profile: Profile, fields: ExtraField[]): Partial<ProfileIn> {
  const next = toBody(values, fields);
  const saved = toBody(toValues(profile, fields), fields);
  const changed: Record<string, unknown> = {};
  for (const key of Object.keys(next) as Array<keyof ProfileIn>) {
    if (key !== "extra" && JSON.stringify(next[key]) !== JSON.stringify(saved[key])) changed[key] = next[key];
  }
  // The extra answers are compared one by one, for the same reason: the API replaces the answers
  // it is sent and keeps the rest. Null clears a stored answer.
  const before = saved.extra ?? {};
  const answers = Object.entries(next.extra ?? {}).filter(
    ([key, answer]) => JSON.stringify(answer) !== JSON.stringify(before[key]),
  );
  if (answers.length > 0) changed.extra = Object.fromEntries(answers);
  return changed as Partial<ProfileIn>;
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

/** Yes or no, or neither: an answer that was given can be taken back. */
function YesNoField({
  label,
  value,
  onChange,
  error,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  error?: string;
}) {
  const id = useId();
  return (
    <Field data-invalid={error ? true : undefined}>
      <FieldTitle id={`${id}-label`} className="leading-snug">
        {label}
      </FieldTitle>
      <div className="flex flex-wrap items-center gap-2">
        <ToggleGroup
          type="single"
          variant="outline"
          spacing={0}
          value={value}
          // Pressing the chosen answer again takes it back, as the Clear button does.
          onValueChange={onChange}
          aria-labelledby={`${id}-label`}
          aria-describedby={error ? `${id}-error` : undefined}
          className="bg-card"
        >
          <ToggleGroupItem value={EXTRA_YES} className="px-3! data-[state=on]:bg-accent data-[state=on]:text-foreground">
            Yes
          </ToggleGroupItem>
          <ToggleGroupItem value={EXTRA_NO} className="px-3! data-[state=on]:bg-accent data-[state=on]:text-foreground">
            No
          </ToggleGroupItem>
        </ToggleGroup>
        {value !== "" && (
          <Button type="button" variant="ghost" size="sm" className="text-muted-foreground" onClick={() => onChange("")}>
            Clear<span className="sr-only"> the answer to {label}</span>
          </Button>
        )}
      </div>
      {error && <FieldError id={`${id}-error`}>{error}</FieldError>}
    </Field>
  );
}

/** Radix Select cannot hold an empty value, so "no answer" travels as this token and an option as its position. */
const NO_ANSWER = "none";

function ChoiceField({
  label,
  options,
  value,
  onChange,
  error,
}: {
  label: string;
  options: string[];
  value: string;
  onChange: (value: string) => void;
  error?: string;
}) {
  const chosen = options.indexOf(value);
  return (
    <FormField label={label} error={error}>
      {({ id, describedBy, invalid }) => (
        <Select
          // An answer picked here that the Admin took off the list meanwhile matches no entry: the box
          // then shows its placeholder, and either an option or "No answer" can be chosen.
          value={value === "" ? NO_ANSWER : chosen === -1 ? "" : String(chosen)}
          onValueChange={(next) => onChange(next === NO_ANSWER ? "" : (options[Number(next)] ?? ""))}
        >
          <SelectTrigger id={id} aria-invalid={invalid || undefined} aria-describedby={describedBy} className="w-full">
            <SelectValue placeholder="Choose an option" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={NO_ANSWER}>No answer</SelectItem>
            {options.map((option, position) => (
              <SelectItem key={position} value={String(position)}>
                {option}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
    </FormField>
  );
}

/** One extra field, as the control its type asks for: text, a number, yes or no, or one of a list. */
function ExtraAnswerField({ field, form, error }: { field: ExtraField; form: UseFormReturn<Values>; error?: string }) {
  const name = `extra.${slot(field.key)}` as const;
  if (field.type === "yes_no") {
    return (
      <Controller
        control={form.control}
        name={name}
        render={({ field: input }) => (
          <YesNoField label={field.label} value={input.value ?? ""} onChange={input.onChange} error={error} />
        )}
      />
    );
  }
  if (field.type === "choice") {
    return (
      <Controller
        control={form.control}
        name={name}
        render={({ field: input }) => (
          <ChoiceField
            label={field.label}
            options={field.options ?? []}
            value={input.value ?? ""}
            onChange={input.onChange}
            error={error}
          />
        )}
      />
    );
  }
  return (
    <TextField
      label={field.label}
      autoComplete="off"
      inputMode={field.type === "number" ? "decimal" : undefined}
      maxLength={field.type === "number" ? 40 : EXTRA_ANSWER_MAX}
      error={error}
      {...form.register(name)}
    />
  );
}

function Editor({ profile, memberId, onBehalfOf }: { profile: Profile; memberId: string; onBehalfOf?: string }) {
  const switchId = useId();
  const client = useQueryClient();
  // The extra fields the Admin defined, the same list for everyone (GET /api/auth/me).
  const fields = useMe().settings.profile_extra_fields;
  const save = useUpdateProfile(memberId);
  const schema = useMemo(() => buildSchema(fields), [fields]);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: toValues(profile, fields) });
  const { errors, isDirty } = form.formState;

  // The stored profile, or the Admin's list of extra fields, changed somewhere else: show it, and
  // keep what is being typed here.
  useEffect(() => {
    form.reset(toValues(profile, fields), { keepDirtyValues: true });
  }, [form, profile, fields]);

  const onSubmit = form.handleSubmit((values) => {
    save.mutate(changedFields(values, profile, fields), {
      onSuccess: (saved) => {
        form.reset(toValues(saved, fields));
        toast.success("Profile saved");
      },
      onError: (error) => {
        applyServerErrors(form, error);
        // The API refuses an extra answer that does not fit its field (422, with a sentence that is shown
        // below). If the Admin changed the fields while this form was open, load them as they are now.
        if (error instanceof ApiError && error.status === 422) void client.invalidateQueries({ queryKey: qk.me });
      },
    });
  });
  const failure = isGeneralError(form, save.error);

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
                  <SelectTrigger id={id} className="w-full sm:max-w-64">
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
                  <SelectTrigger id={id} className="w-full sm:max-w-96">
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
                  <SelectTrigger id={id} className="w-full sm:max-w-64">
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
      </Group>

      {fields.length > 0 && (
        <Group title="Additional information" description="Extra questions set up for this portal. All of them are optional.">
          {/* Labels and options are the Admin's own words, of any length: the columns may not grow with them, and a long word wraps. */}
          <div className="@container">
            <div className="grid grid-cols-1 gap-5 wrap-anywhere @md:grid-cols-2">
              {fields.map((field) => (
                <ExtraAnswerField key={field.key} field={field} form={form} error={errors.extra?.[slot(field.key)]?.message} />
              ))}
            </div>
          </div>
        </Group>
      )}

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
