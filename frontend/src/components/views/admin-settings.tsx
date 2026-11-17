"use client";

import { useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Controller, useFieldArray, useForm, useWatch, type UseFormReturn } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { ArrowDown, ArrowUp, CircleCheck, CircleX, Plus, Trash2, TriangleAlert } from "lucide-react";
import { toast } from "sonner";
import { z } from "@/lib/zod";

import { FormField, FormMessage, TextareaField, TextField } from "@/components/shared/form-fields";
import { PageHeader, Panel } from "@/components/shared/page-header";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { AccountPanels } from "@/components/views/account-settings";
import { ApiError, errorMessage } from "@/lib/api";
import { formatDateTime, humanize } from "@/lib/format";
import {
  applyServerErrors,
  EXTRA_FIELDS_MAX,
  EXTRA_OPTIONS_MAX,
  EXTRA_OPTIONS_MIN,
  extraFieldKey,
  extraFieldsSchema,
  isGeneralError,
} from "@/lib/forms";
import { EXTRA_FIELD_TYPE_LABELS, EXTRA_FIELD_TYPES } from "@/lib/options";
import { useHealth, useIntegrations, usePortalSettings, useUpdatePortalSettings } from "@/lib/queries";
import { salesforceStatusOf, TEAM_LEAD_SCOPES, teamLeadScopeOf } from "@/lib/salesforce";
import type { ExtraField, Integrations, PortalSettings } from "@/lib/types";
import { useUrlState } from "@/lib/url-state";
import { cn } from "@/lib/utils";
const whole = (min: number, max: number) =>
  z
    .string()
    .trim()
    .refine((value) => {
      const number = Number(value);
      return value !== "" && Number.isInteger(number) && number >= min && number <= max;
    }, `Enter a whole number between ${min.toLocaleString()} and ${max.toLocaleString()}.`);

/** One extra candidate-profile field as this form holds it. */
const fieldRow = z.object({
  /** Empty until the field has been saved once. After that it is never changed. */
  key: z.string(),
  label: z.string(),
  type: z.enum(EXTRA_FIELD_TYPES),
  /** The options of a choice, one per line. */
  options: z.string(),
});
type FieldRow = z.infer<typeof fieldRow>;

/** The lines of an options box: trimmed, without empty lines and without repeats. */
const optionLines = (text: string) => [
  ...new Set(
    text
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean),
  ),
];

const toRow = (field: ExtraField): FieldRow => ({
  key: field.key,
  label: field.label,
  type: field.type,
  options: (field.options ?? []).join("\n"),
});

/**
 * The rows as PATCH /api/settings takes them. A row that has not been saved yet gets its key here,
 * from its label; a row that exists keeps the key it has, whatever its label says now, because the
 * the candidates' answers are stored under it. Options only travel with a choice.
 *
 * `saved` is the list the API gave. A field that was not touched goes back exactly as it came, so
 * that saving something else never rewrites it.
 */
function toExtraFields(rows: FieldRow[], saved: ExtraField[]): ExtraField[] {
  const stored = new Map(saved.map((field) => [field.key, field]));
  const taken = new Set(rows.map((row) => row.key).filter(Boolean));
  return rows.map((row) => {
    const before = stored.get(row.key);
    if (before) {
      const untouched = toRow(before);
      if (untouched.label === row.label && untouched.type === row.type && untouched.options === row.options) {
        return { key: before.key, label: before.label, type: before.type, options: before.options ?? [] };
      }
    }
    const label = row.label.trim();
    const key = row.key || extraFieldKey(label, taken);
    taken.add(key);
    return { key, label, type: row.type, options: row.type === "choice" ? optionLines(row.options) : [] };
  });
}

/** `saved` is the list of extra fields as the API gave it (see `toExtraFields`). */
const makeSchema = (saved: ExtraField[]) =>
  z
    .object({
      watermark_enabled: z.boolean(),
      deep_search_default: z.boolean(),
      default_target_count: whole(1, 1000),
      default_min_match_score: whole(0, 100),
      max_members_per_search: whole(1, 50),
      max_results_per_search_run: whole(1, 5000),
      invitation_hours: whole(1, 720),
      salesforce_team_lead_scope: z.enum(["all", "own"]),
      profile_extra_fields: z.array(fieldRow),
    })
    .superRefine((values, context) => {
      if (Number(values.default_target_count) > Number(values.max_results_per_search_run)) {
        context.addIssue({
          code: "custom",
          path: ["default_target_count"],
          message: "The default cannot be higher than the limit of results per search.",
        });
      }
      // The extra fields are checked in the shape they are sent in, by the rules the API applies to them.
      // A problem with a key is shown at the label, which is what a key is made from.
      const checked = extraFieldsSchema.safeParse(toExtraFields(values.profile_extra_fields, saved));
      for (const issue of checked.success ? [] : checked.error.issues) {
        const [index, part] = issue.path;
        context.addIssue({
          code: "custom",
          path:
            typeof index === "number"
              ? ["profile_extra_fields", index, part === "options" ? "options" : "label"]
              : ["profile_extra_fields"],
          message: issue.message,
        });
      }
    });
type Values = z.infer<ReturnType<typeof makeSchema>>;

const toValues = (settings: PortalSettings): Values => ({
  watermark_enabled: settings.watermark_enabled,
  deep_search_default: settings.deep_search_default,
  default_target_count: String(settings.default_target_count),
  default_min_match_score: String(settings.default_min_match_score),
  max_members_per_search: String(settings.max_members_per_search),
  max_results_per_search_run: String(settings.max_results_per_search_run),
  invitation_hours: String(settings.invitation_hours),
  salesforce_team_lead_scope: teamLeadScopeOf(settings),
  profile_extra_fields: settings.profile_extra_fields.map(toRow),
});

/**
 * What the API itself refused about the extra fields (422 `fields`), each with the field it is about.
 * The form checks the same rules before sending, so this is only seen if the two ever disagree. It is
 * listed in one place because a refusal can be about a whole field, or the whole list, and then no
 * input would show it.
 */
function extraFieldRefusals(error: unknown): string[] {
  if (!(error instanceof ApiError)) return [];
  const refusals = Object.entries(error.fields).flatMap(([name, message]) => {
    const match = /^profile_extra_fields(?:\.(\d+)(?:\.([a-z]+))?)?(?:\.|$)/.exec(name);
    if (!match) return [];
    if (match[1] === undefined) return [message];
    return [`Field ${Number(match[1]) + 1}${match[2] ? `, ${match[2]}` : ""}: ${message}`];
  });
  return [...new Set(refusals)];
}

function SwitchRow({
  checked,
  onChange,
  label,
  description,
  disabled,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  description: React.ReactNode;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="flex items-start gap-3">
      <Switch id={id} checked={checked} onCheckedChange={onChange} disabled={disabled} className="mt-0.5" aria-describedby={`${id}-description`} />
      <div className="flex flex-col gap-1">
        <Label htmlFor={id}>{label}</Label>
        <p id={`${id}-description`} className="max-w-[60ch] text-2sm text-muted-foreground">
          {description}
        </p>
      </div>
    </div>
  );
}

const NEW_FIELD: FieldRow = { key: "", label: "", type: "text", options: "" };

/**
 * The extra candidate-profile fields: add, rename, change the kind, put in order, remove. Nothing is stored
 * until the settings are saved, so a removal can still be discarded.
 */
function ExtraFieldsPanel({
  form,
  saved,
  refusals,
}: {
  form: UseFormReturn<Values>;
  /** The fields as they are stored now. */
  saved: ExtraField[];
  refusals: string[];
}) {
  const listRef = useRef<HTMLOListElement>(null);
  const addRef = useRef<HTMLButtonElement>(null);
  /** The row a move button was pressed in: the row is re-inserted elsewhere, which would drop the focus. */
  const moved = useRef<{ id: string; direction: "up" | "down" } | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const { fields: rows, append, remove, move } = useFieldArray({ control: form.control, name: "profile_extra_fields" });
  const values = useWatch({ control: form.control, name: "profile_extra_fields" });
  const keys = toExtraFields(values, saved).map((field) => field.key);
  const errors = form.formState.errors.profile_extra_fields;
  const full = rows.length >= EXTRA_FIELDS_MAX;

  useLayoutEffect(() => {
    const target = moved.current;
    if (!target) return;
    moved.current = null;
    const row = listRef.current?.querySelector(`[data-row="${target.id}"]`);
    const button =
      row?.querySelector<HTMLButtonElement>(`[data-move="${target.direction}"]:enabled`) ??
      row?.querySelector<HTMLButtonElement>("[data-move]:enabled");
    button?.focus();
  });

  const nameOf = (index: number) => values[index]?.label.trim() || `Field ${index + 1}`;

  const shift = (index: number, direction: "up" | "down") => {
    const to = direction === "up" ? index - 1 : index + 1;
    moved.current = { id: rows[index].id, direction };
    setAnnouncement(`${nameOf(index)} is now field ${to + 1} of ${rows.length}.`);
    move(index, to);
  };

  const drop = (index: number) => {
    setAnnouncement(`${nameOf(index)} removed.`);
    remove(index);
    addRef.current?.focus();
  };

  return (
    <Panel
      title="Extra candidate profile fields"
      description="Questions added to every candidate's profile, under Additional information. The Team Lead or an Admin fills in the answers on the candidate's page; the candidate's responsible member can read them."
    >
      <div className="flex flex-col gap-4">
        {refusals.length > 0 && (
          <FormMessage>
            <ul className="flex flex-col gap-1">
              {refusals.map((refusal) => (
                <li key={refusal}>{refusal}</li>
              ))}
            </ul>
          </FormMessage>
        )}
        {errors?.root?.message && <FormMessage>{errors.root.message}</FormMessage>}

        {rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">No extra fields yet. Candidate profiles have the built-in fields only.</p>
        ) : (
          <ol ref={listRef} aria-label="Extra candidate profile fields" className="flex flex-col gap-3">
            {rows.map((row, index) => {
              const current = values[index] ?? row;
              const rowErrors = errors?.[index];
              const name = nameOf(index);
              return (
                <li key={row.id} data-row={row.id} data-slot="extra-field">
                  {/* Every row has a "Label" and a "Type": the group says which field they belong to. */}
                  <div
                    role="group"
                    aria-label={`Field ${index + 1}${current.label.trim() ? `: ${current.label.trim()}` : ""}`}
                    className="flex flex-col gap-4 rounded-md border bg-muted/40 p-3"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <p className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5 pt-1 text-2sm text-muted-foreground">
                        <span className="font-label text-foreground">Field {index + 1}</span>
                        {(row.key || current.label.trim()) && (
                          <span className="min-w-0">
                            Key <code className="font-mono text-xs break-all text-foreground">{keys[index]}</code>
                            {!row.key && ", set when you save"}
                          </span>
                        )}
                      </p>
                      <div className="flex shrink-0 items-center gap-1">
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-sm"
                          data-move="up"
                          disabled={index === 0}
                          aria-label={`Move ${name} up`}
                          onClick={() => shift(index, "up")}
                        >
                          <ArrowUp />
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-sm"
                          data-move="down"
                          disabled={index === rows.length - 1}
                          aria-label={`Move ${name} down`}
                          onClick={() => shift(index, "down")}
                        >
                          <ArrowDown />
                        </Button>
                        <Button type="button" variant="ghost" size="icon-sm" aria-label={`Remove ${name}`} onClick={() => drop(index)}>
                          <Trash2 />
                        </Button>
                      </div>
                    </div>
                    <div className="grid grid-cols-1 gap-4 sm:grid-cols-[minmax(0,1fr)_13rem]">
                      <TextField
                        label="Label"
                        autoComplete="off"
                        placeholder="Shirt size"
                        error={rowErrors?.label?.message}
                        {...form.register(`profile_extra_fields.${index}.label`)}
                      />
                      <Controller
                        control={form.control}
                        name={`profile_extra_fields.${index}.type`}
                        render={({ field }) => (
                          <FormField label="Type">
                            {({ id }) => (
                              <Select value={field.value} onValueChange={field.onChange}>
                                <SelectTrigger id={id} className="w-full">
                                  <SelectValue />
                                </SelectTrigger>
                                <SelectContent>
                                  {EXTRA_FIELD_TYPES.map((type) => (
                                    <SelectItem key={type} value={type}>
                                      {EXTRA_FIELD_TYPE_LABELS[type]}
                                    </SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                            )}
                          </FormField>
                        )}
                      />
                    </div>
                    {current.type === "choice" && (
                      <TextareaField
                        label="Options"
                        rows={3}
                        placeholder={"Small\nMedium\nLarge"}
                        description={`One per line, ${EXTRA_OPTIONS_MIN} to ${EXTRA_OPTIONS_MAX} of them. One of them is chosen for each candidate.`}
                        error={rowErrors?.options?.message}
                        {...form.register(`profile_extra_fields.${index}.options`)}
                      />
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        )}

        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <Button type="button" variant="outline" ref={addRef} disabled={full} onClick={() => append(NEW_FIELD)}>
            <Plus data-icon="inline-start" />
            Add Field
          </Button>
          <span className="text-2sm text-muted-foreground tabular-nums">
            {rows.length} of {EXTRA_FIELDS_MAX}
            {full && ". Remove a field to add another."}
          </span>
        </div>
        <p className="max-w-[72ch] text-2sm text-muted-foreground">
          A field gets its key from its label when it is first saved, and keeps it: renaming a field keeps its answers.
          Saving deletes the answers that no longer fit: every answer to a removed field, and answers that do not match
          a changed type or a removed option. A field added again later starts empty.
        </p>
        <p role="status" className="sr-only">
          {announcement}
        </p>
      </div>
    </Panel>
  );
}

function PortalSettingsForm({ settings }: { settings: PortalSettings }) {
  const save = useUpdatePortalSettings();
  const savedFields = settings.profile_extra_fields;
  const schema = useMemo(() => makeSchema(savedFields), [savedFields]);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: toValues(settings) });
  const { errors, isDirty } = form.formState;
  const scopeId = useId();

  const onSubmit = form.handleSubmit((values) => {
    save.mutate(
      {
        salesforce_team_lead_scope: values.salesforce_team_lead_scope,
        watermark_enabled: values.watermark_enabled,
        deep_search_default: values.deep_search_default,
        default_target_count: Number(values.default_target_count),
        default_min_match_score: Number(values.default_min_match_score),
        max_members_per_search: Number(values.max_members_per_search),
        max_results_per_search_run: Number(values.max_results_per_search_run),
        invitation_hours: Number(values.invitation_hours),
        profile_extra_fields: toExtraFields(values.profile_extra_fields, savedFields),
      },
      {
        onSuccess: (saved) => {
          form.reset(toValues(saved));
          toast.success("Settings saved");
        },
        onError: (error) => {
          applyServerErrors(form, error);
        },
      },
    );
  });
  const failure = isGeneralError(form, save.error);

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col gap-5">
      <Panel title="Member job views">
        <Controller
          control={form.control}
          name="watermark_enabled"
          render={({ field }) => (
            <SwitchRow
              checked={field.value}
              onChange={field.onChange}
              label="Show a watermark on member job views"
              description="The member's e-mail address and the current time are laid faintly over the job pages of their candidates, so a copy can be traced back. It does not stop anyone from taking a screenshot."
            />
          )}
        />
      </Panel>

      <Panel title="Searches" description="Defaults and limits for every search started by a Team Lead or an Admin.">
        <div className="flex flex-col gap-5">
          <div className="grid gap-5 sm:grid-cols-2">
            <TextField
              label="Default results per candidate"
              inputMode="numeric"
              autoComplete="off"
              description="What the search form starts with. 1 to 1000."
              error={errors.default_target_count?.message}
              {...form.register("default_target_count")}
            />
            <TextField
              label="Default minimum match score"
              inputMode="numeric"
              autoComplete="off"
              description="Jobs scoring below this are not assigned automatically. 0 to 100."
              error={errors.default_min_match_score?.message}
              {...form.register("default_min_match_score")}
            />
            <TextField
              label="Most candidates in one search"
              inputMode="numeric"
              autoComplete="off"
              description="1 to 50."
              error={errors.max_members_per_search?.message}
              {...form.register("max_members_per_search")}
            />
            <TextField
              label="Most results in one search"
              inputMode="numeric"
              autoComplete="off"
              description="Candidates times results per candidate may not exceed this. 1 to 5,000."
              error={errors.max_results_per_search_run?.message}
              {...form.register("max_results_per_search_run")}
            />
          </div>
          <Controller
            control={form.control}
            name="deep_search_default"
            render={({ field }) => (
              <SwitchRow
                checked={field.value}
                onChange={field.onChange}
                label="Deep search by default"
                description="Deep search adds AI web search and a browser crawl to the job-board sources. It finds more and takes longer."
              />
            )}
          />
        </div>
      </Panel>

      <Panel
          title="Salesforce"
          description="Which candidates a Team Lead can find in Salesforce and bring into their team. An Admin always sees all of them."
        >
          <Controller
            control={form.control}
            name="salesforce_team_lead_scope"
            render={({ field }) => (
              <RadioGroup value={field.value} onValueChange={field.onChange} aria-label="Candidates a Team Lead can find in Salesforce" className="gap-3 sm:grid-cols-2">
                {TEAM_LEAD_SCOPES.map((option) => {
                  const id = `${scopeId}-${option.value}`;
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
                        <span className="text-sm font-medium">{option.label}</span>
                        <span className="text-2sm text-muted-foreground">{option.text}</span>
                      </span>
                    </label>
                  );
                })}
              </RadioGroup>
            )}
          />
      </Panel>

      <ExtraFieldsPanel form={form} saved={savedFields} refusals={extraFieldRefusals(save.error)} />
      <Panel title="Invitations">
        <TextField
          label="Hours an invitation link stays valid"
          inputMode="numeric"
          autoComplete="off"
          description="1 to 720 (30 days). Applies to invitations created from now on."
          className="sm:max-w-80"
          error={errors.invitation_hours?.message}
          {...form.register("invitation_hours")}
        />
      </Panel>

      <div className="flex flex-wrap items-center justify-end gap-3">
        {failure && <FormMessage className="mr-auto">{errorMessage(save.error)}</FormMessage>}
        {isDirty && (
          <Button type="button" variant="ghost" onClick={() => form.reset()} disabled={save.isPending}>
            Discard changes
          </Button>
        )}
        <Button type="submit" disabled={save.isPending || !isDirty}>
          {save.isPending && <Spinner data-icon="inline-start" />}
          Save Settings
        </Button>
      </div>
    </form>
  );
}

/* ------------------------------------------------------------------ integrations */

function State({ ok, yes, no }: { ok: boolean; yes: string; no: string }) {
  return ok ? (
    <Tag tone="success">
      <CircleCheck aria-hidden="true" />
      {yes}
    </Tag>
  ) : (
    <Tag tone="warning">
      <TriangleAlert aria-hidden="true" />
      {no}
    </Tag>
  );
}

interface Source {
  name: string;
  label: string;
  kind?: string;
  note?: string;
  enabled?: boolean;
  available?: boolean;
  units?: number;
  postings?: number;
}

/** The API types a source as a free-form object; keep only entries that have the fields this screen shows. */
function toSources(raw: Array<Record<string, unknown>>): Source[] {
  return raw.flatMap((item) =>
    typeof item.name === "string" && typeof item.label === "string"
      ? [
          {
            name: item.name,
            label: item.label,
            kind: typeof item.kind === "string" ? item.kind : undefined,
            note: typeof item.note === "string" ? item.note : undefined,
            enabled: typeof item.enabled === "boolean" ? item.enabled : undefined,
            available: typeof item.available === "boolean" ? item.available : undefined,
            units: typeof item.units === "number" ? item.units : undefined,
            postings: typeof item.postings === "number" ? item.postings : undefined,
          },
        ]
      : [],
  );
}

function Sources({ sources, settings }: { sources: Source[]; settings: PortalSettings | undefined }) {
  const save = useUpdatePortalSettings();
  const disabled = new Set(settings?.disabled_sources ?? []);

  const toggle = (source: Source, enable: boolean) => {
    const next = new Set(disabled);
    if (enable) next.delete(source.name);
    else next.add(source.name);
    save.mutate(
      { disabled_sources: [...next] },
      {
        onSuccess: () => toast.success(`${source.label} ${enable ? "switched on" : "switched off"} for searches`),
        onError: (error) => toast.error(errorMessage(error)),
      },
    );
  };

  return (
    <ul className="flex flex-col divide-y">
      {sources.map((source) => {
        const enabled = settings ? !disabled.has(source.name) : source.enabled !== false;
        return (
          <li key={source.name} className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 py-3 first:pt-0 last:pb-0">
            <div className="flex min-w-0 flex-1 flex-col gap-0.5">
              <span className="flex flex-wrap items-center gap-2 text-sm font-medium">
                {source.label}
                {source.kind && <Tag>{humanize(source.kind)}</Tag>}
                {source.available === false && (
                  <Tag tone="warning">
                    <TriangleAlert aria-hidden="true" />
                    Not configured on the server
                  </Tag>
                )}
              </span>
              {source.note && <span className="text-2sm text-muted-foreground">{source.note}</span>}
            </div>
            <span className="text-2sm text-muted-foreground tabular-nums">
              {(source.postings ?? 0).toLocaleString()} fresh postings from {(source.units ?? 0).toLocaleString()} reads
            </span>
            <div className="flex items-center gap-2">
              <Switch
                checked={enabled}
                disabled={!settings || save.isPending}
                onCheckedChange={(checked) => toggle(source, checked)}
                aria-label={`Use ${source.label} in searches`}
              />
              <span className="w-7 text-2sm text-muted-foreground">{enabled ? "On" : "Off"}</span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function Facts({ rows }: { rows: Array<[string, React.ReactNode]> }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] items-center gap-x-5 gap-y-2.5 text-sm">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="min-w-0 break-words">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function IntegrationsPanels({ data, settings }: { data: Integrations; settings: PortalSettings | undefined }) {
  const health = useHealth();
  const providers = data.ai_providers as Record<string, { configured?: boolean; model?: string }>;
  const worker = data.worker as { online?: boolean; last_seen?: string | number; browser?: unknown; active_searches?: number };
  const email = data.email as { backend?: string; from?: string };
  const queue = data.queue as { redis?: boolean };
  const index = data.index as { fresh_postings?: number; fresh_units?: number; stored_postings?: number };
  const lastSeen =
    typeof worker.last_seen === "number" ? formatDateTime(new Date(worker.last_seen * 1000)) : worker.last_seen ? formatDateTime(worker.last_seen) : null;
  const emailBackend = email.backend ?? "none";
  const salesforce = salesforceStatusOf(data);
  const scope = TEAM_LEAD_SCOPES.find((option) => option.value === salesforce.team_lead_scope);

  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Panel title="AI providers" description="Used for resume analysis, web search and ranking. Keys are set on the server and never shown here.">
        <Facts
          rows={[
            ["OpenAI", <State key="o" ok={Boolean(providers.openai?.configured)} yes="Configured" no="No key configured" />],
            ["OpenAI model", providers.openai?.model ?? "Not set"],
            ["Claude", <State key="c" ok={Boolean(providers.claude?.configured)} yes="Configured" no="No key configured" />],
            ["Claude model", providers.claude?.model ?? "Not set"],
          ]}
        />
        {!providers.openai?.configured && !providers.claude?.configured && (
          <p className="mt-3 text-2sm text-muted-foreground">
            Without a provider, searches still run: jobs come from the job-board sources and are scored by keywords.
          </p>
        )}
      </Panel>

      <Panel title="Background services" description="Searches run in a background worker, not in the web server.">
        <Facts
          rows={[
            ["Search worker", <State key="w" ok={Boolean(worker.online)} yes="Online" no="Offline" />],
            ...(lastSeen ? ([["Worker last seen", lastSeen]] as Array<[string, React.ReactNode]>) : []),
            ...(typeof worker.active_searches === "number"
              ? ([["Searches running now", worker.active_searches.toLocaleString()]] as Array<[string, React.ReactNode]>)
              : []),
            ["Job queue (Redis)", <State key="q" ok={Boolean(queue.redis)} yes="Reachable" no="Not reachable" />],
            [
              "Database",
              health.data ? (
                <State ok={health.data.database} yes="Reachable" no="Not reachable" />
              ) : health.isError ? (
                <Tag tone="danger">
                  <CircleX aria-hidden="true" />
                  Health check failed
                </Tag>
              ) : (
                "Checking"
              ),
            ],
          ]}
        />
      </Panel>

      <Panel title="E-mail" description="How invitations and password reset links are delivered.">
        <Facts
          rows={[
            [
              "Delivery",
              emailBackend === "smtp" ? (
                <State ok yes="SMTP server" no="" />
              ) : (
                <State ok={false} yes="" no={emailBackend === "console" ? "Written to the server log only" : "Not configured"} />
              ),
            ],
            ...(email.from ? ([["Sent from", email.from]] as Array<[string, React.ReactNode]>) : []),
          ]}
        />
        {emailBackend !== "smtp" && (
          <p className="mt-3 text-2sm text-muted-foreground">
            No e-mail leaves the server. Whoever creates an invitation or a reset link is shown the link and has to pass it on.
          </p>
        )}
      </Panel>

      <Panel
        title="Salesforce"
        description="Where Team Leads and Admins find candidates and bring them into the portal. Credentials are set on the server and never shown here."
        className="lg:col-span-2"
      >
        <Facts
          rows={[
            ["Connection", <State key="c" ok={salesforce.configured} yes="Configured" no="Not configured on the server" />],
            ...(salesforce.configured
              ? ([
                  ["Salesforce", <State key="r" ok={salesforce.connected === true} yes="Reachable" no="Not reachable" />],
                  ...(salesforce.message ? [["Last check", salesforce.message]] : []),
                  ...(salesforce.checked_at ? [["Checked", formatDateTime(salesforce.checked_at)]] : []),
                  ...(salesforce.api_version ? [["API version", salesforce.api_version]] : []),
                  ["Access", <Tag key="a">Read-only</Tag>],
                  ...(scope ? [["Team Leads can find", scope.label]] : []),
                ] as Array<[string, React.ReactNode]>)
              : []),
          ]}
        />
        <p className="mt-3 max-w-[80ch] text-2sm text-muted-foreground">
          {salesforce.configured
            ? "The portal only reads Salesforce: candidates, their details and their resume files. Nothing is written back."
            : "Without the Salesforce settings on the server, nothing about Salesforce is offered: candidates are added manually."}
        </p>
      </Panel>

      <Panel title="Search index" description="Postings the search engine has read from the sources and keeps to search through.">
        <Facts
          rows={[
            ["Postings stored", (index.stored_postings ?? 0).toLocaleString()],
            ["Fresh postings", (index.fresh_postings ?? 0).toLocaleString()],
            ["Fresh source reads", (index.fresh_units ?? 0).toLocaleString()],
          ]}
        />
      </Panel>

      <Panel
        title="Job sources"
        description="Where searches look for postings. Switching a source off takes effect for searches started from then on."
        className="lg:col-span-2"
      >
        <Sources sources={toSources(data.sources)} settings={settings} />
      </Panel>
    </div>
  );
}

const TAB_DEFAULTS = { tab: "portal" };
const TABS = [
  { value: "portal", label: "Portal" },
  { value: "integrations", label: "Integrations" },
  { value: "account", label: "My Account" },
];

/** Admin "Settings": portal-wide settings, the state of the integrations, and the Admin's own account. */
export function AdminSettingsView() {
  const [state, setState] = useUrlState(TAB_DEFAULTS);
  const tab = TABS.some((item) => item.value === state.tab) ? state.tab : "portal";
  const settings = usePortalSettings();
  const integrations = useIntegrations();

  return (
    <>
      <PageHeader
        title="Settings"
        description="Limits and defaults for the whole portal, the services it depends on, and your own account."
      />
      <Tabs value={tab} onValueChange={(next) => setState({ tab: next })} className="gap-5">
        <TabsList variant="line" className="w-full justify-start overflow-x-auto border-b">
          {TABS.map((item) => (
            <TabsTrigger key={item.value} value={item.value} className="flex-none">
              {item.label}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="portal">
          {settings.isPending ? (
            <LoadingSkeleton variant="form" count={5} />
          ) : settings.isError ? (
            <div className="rounded-lg border bg-card">
              <ErrorState error={settings.error} onRetry={() => settings.refetch()} />
            </div>
          ) : (
            <PortalSettingsForm settings={settings.data} />
          )}
        </TabsContent>
        <TabsContent value="integrations">
          {integrations.isPending ? (
            <LoadingSkeleton variant="cards" count={4} />
          ) : integrations.isError ? (
            <div className="rounded-lg border bg-card">
              <ErrorState error={integrations.error} onRetry={() => integrations.refetch()} />
            </div>
          ) : (
            <IntegrationsPanels data={integrations.data} settings={settings.data} />
          )}
        </TabsContent>
        <TabsContent value="account">
          <AccountPanels />
        </TabsContent>
      </Tabs>
    </>
  );
}
