"use client";

import { useEffect } from "react";
import { Controller, useForm, type Control } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { toast } from "sonner";

import { FormField, FormMessage, TextareaField, TextField } from "@/components/shared/form-fields";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { errorMessage } from "@/lib/api";
import {
  EMPTY_ANSWERS,
  useApplicationAnswers,
  useUpdateApplicationAnswers,
  type ApplicationAnswers,
  type ApplicationAnswersIn,
} from "@/lib/auto-apply";
import { formatDateTime } from "@/lib/format";
import { applyServerErrors, isGeneralError } from "@/lib/forms";
import { z } from "@/lib/zod";

const tri = z.enum(["yes", "no", "unknown"]);
const text = (max: number) => z.string().trim().max(max);
const url = z
  .string()
  .trim()
  .max(300)
  .refine((v) => v === "" || /^https?:\/\/\S+$/i.test(v), "Enter a full link starting with https://");

const schema = z.object({
  authorized_us: tri,
  sponsorship_now_or_future: tri,
  age_18_or_over: tri,
  willing_to_relocate: tri,
  restrictive_agreements: tri,
  earliest_start: text(100),
  notice_period: text(100),
  expected_salary: text(100),
  /** One employer per line in the form; a list in the API. */
  previously_employed_by: z.string().refine((v) => lines(v).length <= 50, "At most 50 employers."),
  previous_employers_complete: z.boolean(),
  eeo_gender: text(100),
  eeo_race: text(100),
  eeo_hispanic_latino: text(100),
  eeo_veteran: text(100),
  eeo_disability: text(100),
  linkedin_url: url,
  github_url: url,
  portfolio_url: url,
  current_company: text(160),
  current_title: text(160),
  city: text(100),
  state: text(100),
  country: text(100),
  phone: text(40),
  first_name: text(100),
  last_name: text(100),
  preferred_name: text(100),
  referral_source: text(100),
});
type Values = z.infer<typeof schema>;
type Name = keyof Values;

/** One employer per line. Commas stay: "Acme, Inc." is one employer. */
const lines = (value: string) => [...new Set(value.split(/\r?\n/).map((l) => l.trim()).filter(Boolean))];

const toValues = (a: ApplicationAnswersIn): Values => ({ ...a, previously_employed_by: a.previously_employed_by.join("\n") });
const toBody = (v: Values): ApplicationAnswersIn => ({ ...v, previously_employed_by: lines(v.previously_employed_by) });

const TRI_OPTIONS = [
  { value: "unknown", label: "Unknown (not recorded)" },
  { value: "yes", label: "Yes" },
  { value: "no", label: "No" },
];

const DECLINE = { value: "decline", label: "Decline to self-identify" };
/** The value is matched against each form's own options, so these are the common wordings. */
const EEO_OPTIONS: Record<"eeo_gender" | "eeo_race" | "eeo_hispanic_latino" | "eeo_veteran" | "eeo_disability", Array<{ value: string; label: string }>> = {
  eeo_gender: [DECLINE, ...["Female", "Male", "Non-binary"].map((v) => ({ value: v, label: v }))],
  eeo_race: [
    DECLINE,
    ...[
      "American Indian or Alaska Native",
      "Asian",
      "Black or African American",
      "Native Hawaiian or Other Pacific Islander",
      "White",
      "Two or More Races",
    ].map((v) => ({ value: v, label: v })),
  ],
  eeo_hispanic_latino: [DECLINE, { value: "Yes", label: "Yes" }, { value: "No", label: "No" }],
  eeo_veteran: [
    DECLINE,
    { value: "I am a protected veteran", label: "Protected veteran" },
    { value: "I am not a protected veteran", label: "Not a protected veteran" },
  ],
  eeo_disability: [
    DECLINE,
    { value: "Yes, I have a disability", label: "Yes, has a disability" },
    { value: "No, I do not have a disability", label: "No disability" },
  ],
};

function ChoiceField({
  control,
  name,
  label,
  options,
  disabled,
  description,
}: {
  control: Control<Values>;
  name: Name;
  label: string;
  options: Array<{ value: string; label: string }>;
  disabled: boolean;
  description?: string;
}) {
  return (
    <Controller
      control={control}
      name={name}
      render={({ field, fieldState }) => {
        const value = String(field.value ?? "");
        // A value recorded elsewhere that is not in the list is kept and shown as it is.
        const all = !value || options.some((o) => o.value === value) ? options : [...options, { value, label: value }];
        return (
          <FormField label={label} error={fieldState.error?.message} description={description}>
            {({ id, describedBy, invalid }) => (
              <Select value={value} onValueChange={field.onChange} disabled={disabled}>
                <SelectTrigger
                  id={id}
                  aria-describedby={describedBy}
                  aria-invalid={invalid || undefined}
                  className="w-full scroll-mt-24"
                  data-answer={name}
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {all.map((o) => (
                    <SelectItem key={o.value} value={o.value}>
                      {o.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </FormField>
        );
      }}
    />
  );
}

function Group({ title, description, children }: { title: string; description?: string; children: React.ReactNode }) {
  return (
    <fieldset className="flex min-w-0 flex-col gap-3">
      <legend className="mb-1 font-label text-sm">{title}</legend>
      {description && <p className="-mt-1 text-2sm text-muted-foreground">{description}</p>}
      <div className="grid gap-4 sm:grid-cols-2">{children}</div>
    </fieldset>
  );
}

/** "Used: $120,000 (from the profile)" under a field that is empty here but filled from elsewhere. */
function effectiveNote(data: ApplicationAnswers, name: string, own: string): string | undefined {
  const used = data.effective[name];
  if (!used || own || !used.value || used.source === "application_answers") return undefined;
  return `Used when empty: ${used.value} (from ${used.source.replace(/_/g, " ")})`;
}

/**
 * The answers application forms ask for that a resume does not hold: work authorization, availability,
 * self-identification, links. An Admin or a Team Lead records them; nothing here is ever guessed.
 */
export function ApplicationAnswersForm({ candidateId }: { candidateId: string }) {
  const query = useApplicationAnswers(candidateId);
  const save = useUpdateApplicationAnswers(candidateId);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: toValues(EMPTY_ANSWERS) });

  useEffect(() => {
    if (query.data) form.reset(toValues(query.data));
  }, [query.data, form]);

  // Links from an application's "needs input" list point at #answer-<field>.
  useEffect(() => {
    if (!query.data) return;
    const hash = window.location.hash;
    if (!hash.startsWith("#answer-")) return;
    const name = CSS.escape(hash.slice("#answer-".length));
    const target = name ? document.querySelector(`[name="${name}"], [data-answer="${name}"]`) : document.getElementById("application-answers");
    if (target instanceof HTMLElement) {
      target.scrollIntoView({ block: "center" });
      target.focus({ preventScroll: true });
    }
  }, [query.data]);

  if (query.isPending) return <LoadingSkeleton variant="detail" count={3} />;
  if (query.isError) return <ErrorState compact error={query.error} onRetry={() => query.refetch()} />;
  const data = query.data;

  const submit = form.handleSubmit((values) => {
    // Only the fields that changed are sent.
    const dirty = form.formState.dirtyFields as Partial<Record<Name, unknown>>;
    const body = toBody(values);
    const changed = Object.fromEntries(Object.entries(body).filter(([key]) => dirty[key as Name])) as Partial<ApplicationAnswersIn>;
    save.mutate(changed, {
      onSuccess: () => toast.success("Application answers saved"),
      onError: (error) => applyServerErrors(form, error),
    });
  });
  const err = form.formState.errors;
  const field = (name: Name, label: string, extra: { description?: string; type?: string } = {}) => (
    <TextField
      key={name}
      label={label}
      optional
      error={err[name]?.message}
      type={extra.type}
      description={extra.description ?? effectiveNote(data, name, String(form.getValues(name) ?? ""))}
      className="scroll-mt-24"
      {...form.register(name)}
    />
  );
  const choice = (name: Name, label: string, options = TRI_OPTIONS, description?: string) => (
    <ChoiceField
      control={form.control}
      name={name}
      label={label}
      options={options}
      disabled={false}
      description={description ?? (name in data.effective && form.getValues(name) === "unknown" ? effectiveNote(data, name, "") : undefined)}
    />
  );

  return (
    <form id="application-answers" onSubmit={submit} className="flex scroll-mt-24 flex-col gap-6" noValidate>
      <p className="max-w-[80ch] text-2sm text-muted-foreground">
        Legal questions (work authorization, sponsorship, agreements) and self-identification are answered only as recorded here,
        never guessed. An answer left Unknown stops an application that asks it until someone records it. Self-identification is
        declined unless the candidate chose otherwise.
      </p>

      <Group title="Eligibility">
        {choice("authorized_us", "Authorized to work in the US")}
        {choice("sponsorship_now_or_future", "Needs visa sponsorship now or in the future")}
        {choice("age_18_or_over", "18 years or older")}
        {choice("willing_to_relocate", "Willing to relocate")}
        {choice("restrictive_agreements", "Bound by a non-compete or other restrictive agreement")}
      </Group>

      <Group title="Availability and pay">
        {field("earliest_start", "Earliest start", { description: "For example 2026-12-15, or 2 weeks after an offer" })}
        {field("notice_period", "Notice period")}
        {field("expected_salary", "Expected salary")}
      </Group>

      <Group title="Previous employers" description="Answers &quot;Have you worked for us before?&quot;">
        <TextareaField
          label="Employers the candidate has worked for"
          optional
          rows={3}
          description="One per line."
          error={err.previously_employed_by?.message}
          className="sm:col-span-2"
          {...form.register("previously_employed_by")}
        />
        <Controller
          control={form.control}
          name="previous_employers_complete"
          render={({ field: f }) => (
            <label className="flex items-start gap-3 text-sm sm:col-span-2">
              <Checkbox checked={f.value} onCheckedChange={(v) => f.onChange(v === true)} className="mt-0.5" />
              <span className="flex flex-col gap-0.5">
                <span className="font-medium">This is the complete list</span>
                <span className="text-2sm text-muted-foreground">
                  Then &quot;No&quot; may be answered for every other employer. Otherwise the question waits for input.
                </span>
              </span>
            </label>
          )}
        />
      </Group>

      <Group title="Voluntary self-identification (EEO)">
        {choice("eeo_gender", "Gender", EEO_OPTIONS.eeo_gender, "")}
        {choice("eeo_race", "Race", EEO_OPTIONS.eeo_race, "")}
        {choice("eeo_hispanic_latino", "Hispanic or Latino", EEO_OPTIONS.eeo_hispanic_latino, "")}
        {choice("eeo_veteran", "Veteran status", EEO_OPTIONS.eeo_veteran, "")}
        {choice("eeo_disability", "Disability", EEO_OPTIONS.eeo_disability, "")}
      </Group>

      <Group title="Name and contact">
        {field("first_name", "First name")}
        {field("last_name", "Last name")}
        {field("preferred_name", "Preferred name")}
        {field("phone", "Phone", { type: "tel" })}
        {field("city", "City")}
        {field("state", "State")}
        {field("country", "Country")}
      </Group>

      <Group title="Work and links">
        {field("current_company", "Current company")}
        {field("current_title", "Current title")}
        {field("linkedin_url", "LinkedIn", { type: "url" })}
        {field("github_url", "GitHub", { type: "url" })}
        {field("portfolio_url", "Portfolio", { type: "url" })}
        {field("referral_source", "How did you hear about us?")}
      </Group>

      {isGeneralError(form, save.error) && <FormMessage>{errorMessage(save.error)}</FormMessage>}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className="text-2sm text-muted-foreground">
          {data.updated_at
            ? `Last saved ${formatDateTime(data.updated_at)}${data.updated_by ? ` by ${data.updated_by.name}` : ""}`
            : "Nothing recorded yet"}
        </span>
        <Button type="submit" disabled={save.isPending || !form.formState.isDirty}>
          {save.isPending && <Spinner data-icon="inline-start" />}
          Save answers
        </Button>
      </div>
    </form>
  );
}
