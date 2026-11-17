"use client";

import { useId, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { BadgeCheck, FileSpreadsheet, TriangleAlert } from "lucide-react";
import { toast } from "sonner";
import { z } from "@/lib/zod";

import { FormField, FormMessage, TextareaField, TextField } from "@/components/shared/form-fields";
import { TagInput } from "@/components/shared/tag-input";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { ApiError, errorMessage } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import { applyServerErrors, isGeneralError } from "@/lib/forms";
import { CITIZENSHIP_REQUIREMENTS, JOB_WORK_MODES, normalizeState, SPONSORSHIP_STATUSES } from "@/lib/options";
import { useCreateJob, useImportJobUrl, useImportJobsCsv, useUpdateJob } from "@/lib/queries";
import type { CsvImportResult, JobCreate, JobImportDraft, JobManagerView, JobUpdate } from "@/lib/types";

const optionalUrl = z
  .string()
  .trim()
  .max(2000, "Use at most 2,000 characters.")
  .refine((value) => value === "" || /^https?:\/\/[^\s]+$/i.test(value), "Enter a full address starting with http:// or https://.");

const amount = (max: number, message: string, integer: boolean) =>
  z
    .string()
    .trim()
    .refine((value) => {
      if (value === "") return true;
      const number = Number(value);
      return Number.isFinite(number) && number >= 0 && number <= max && (!integer || Number.isInteger(number));
    }, message);

const schema = z
  .object({
    company: z.string().trim().min(1, "Enter the company name.").max(200, "Use at most 200 characters."),
    title: z.string().trim().min(2, "Enter the job title.").max(300, "Use at most 300 characters."),
    city: z.string().trim().max(120, "Use at most 120 characters."),
    state: z.string().trim().max(60, "Use at most 60 characters."),
    work_mode: z.enum(JOB_WORK_MODES),
    employment_type: z.string().trim().max(40, "Use at most 40 characters."),
    seniority: z.string().trim().max(40, "Use at most 40 characters."),
    salary_min: amount(100_000_000, "Enter a whole amount, without symbols.", true),
    salary_max: amount(100_000_000, "Enter a whole amount, without symbols.", true),
    salary_currency: z.string().trim().max(8, "Use at most 8 characters."),
    salary_text: z.string().trim().max(160, "Use at most 160 characters."),
    experience_min: amount(60, "Enter a number of years between 0 and 60.", false),
    experience_max: amount(60, "Enter a number of years between 0 and 60.", false),
    experience_text: z.string().trim().max(80, "Use at most 80 characters."),
    description: z.string().max(60_000, "Use at most 60,000 characters."),
    requirements: z.array(z.string()).max(80, "Keep it to 80 skills."),
    canonical_url: optionalUrl,
    apply_url: optionalUrl,
    company_url: optionalUrl,
    citizenship_requirement: z.enum(CITIZENSHIP_REQUIREMENTS),
    sponsorship_status: z.enum(SPONSORSHIP_STATUSES),
    clearance_required: z.boolean(),
    posted_at: z.string().refine((value) => value === "" || /^\d{4}-\d{2}-\d{2}$/.test(value), "Choose a date."),
  })
  .superRefine((values, context) => {
    if (values.salary_min && values.salary_max && Number(values.salary_min) > Number(values.salary_max)) {
      context.addIssue({ code: "custom", path: ["salary_max"], message: "The maximum is lower than the minimum." });
    }
    if (values.experience_min && values.experience_max && Number(values.experience_min) > Number(values.experience_max)) {
      context.addIssue({ code: "custom", path: ["experience_max"], message: "The maximum is lower than the minimum." });
    }
  });
type Values = z.infer<typeof schema>;

const oneOf = <T extends string>(options: readonly T[], value: string | undefined, fallback: T): T =>
  (options as readonly string[]).includes(value ?? "") ? (value as T) : fallback;
const text = (value: number | null | undefined) => (value === null || value === undefined ? "" : String(value));

function toValues(job: Partial<JobCreate> | JobManagerView | undefined): Values {
  const source = (job ?? {}) as Partial<JobCreate>;
  return {
    company: source.company ?? "",
    title: source.title ?? "",
    city: source.city ?? "",
    state: source.state ?? "",
    work_mode: oneOf(JOB_WORK_MODES, source.work_mode, "Not listed"),
    employment_type: source.employment_type ?? "",
    seniority: source.seniority ?? "",
    salary_min: text(source.salary_min),
    salary_max: text(source.salary_max),
    salary_currency: source.salary_currency ?? "",
    salary_text: source.salary_text ?? "",
    experience_min: text(source.experience_min),
    experience_max: text(source.experience_max),
    experience_text: source.experience_text ?? "",
    description: source.description ?? "",
    requirements: source.requirements ?? [],
    canonical_url: source.canonical_url ?? "",
    apply_url: source.apply_url ?? "",
    company_url: source.company_url ?? "",
    citizenship_requirement: oneOf(CITIZENSHIP_REQUIREMENTS, source.citizenship_requirement, "Not stated"),
    sponsorship_status: oneOf(SPONSORSHIP_STATUSES, source.sponsorship_status, "Not stated"),
    clearance_required: source.clearance_required ?? false,
    posted_at: source.posted_at ?? "",
  };
}

const numberOrNull = (value: string) => (value === "" ? null : Number(value));

function toCreate(values: Values): JobCreate {
  return {
    ...values,
    country: "United States",
    salary_min: numberOrNull(values.salary_min),
    salary_max: numberOrNull(values.salary_max),
    experience_min: numberOrNull(values.experience_min),
    experience_max: numberOrNull(values.experience_max),
    posted_at: values.posted_at || null,
  };
}

/** The fields PATCH /api/jobs/{id} accepts. */
function toUpdate(values: Values): JobUpdate {
  return {
    company: values.company,
    title: values.title,
    state: values.state,
    city: values.city,
    work_mode: values.work_mode,
    employment_type: values.employment_type,
    seniority: values.seniority,
    salary_min: numberOrNull(values.salary_min),
    salary_max: numberOrNull(values.salary_max),
    salary_currency: values.salary_currency,
    salary_text: values.salary_text,
    experience_text: values.experience_text,
    description: values.description,
    requirements: values.requirements,
    canonical_url: values.canonical_url,
    apply_url: values.apply_url,
    citizenship_requirement: values.citizenship_requirement,
    sponsorship_status: values.sponsorship_status,
    posted_at: values.posted_at || null,
  };
}

function EnumSelect<T extends string>({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: T;
  onChange: (value: T) => void;
  options: readonly T[];
}) {
  return (
    <FormField label={label}>
      {({ id }) => (
        <Select value={value} onValueChange={(next) => onChange(next as T)}>
          <SelectTrigger id={id} className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {options.map((option) => (
              <SelectItem key={option} value={option}>
                {option}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
    </FormField>
  );
}

/** The job fields. `editing` leaves out the few values that can only be set when a job is created. */
function JobFields({
  initial,
  editing,
  submitLabel,
  pending,
  error,
  onSubmit,
  onCancel,
  notice,
}: {
  initial: Values;
  editing: boolean;
  submitLabel: string;
  pending: boolean;
  error: unknown;
  onSubmit: (values: Values, form: ReturnType<typeof useForm<Values>>) => void;
  onCancel: () => void;
  notice?: React.ReactNode;
}) {
  const clearanceId = useId();
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: initial });
  const { errors } = form.formState;
  const failure = isGeneralError(form, error);

  return (
    <form onSubmit={form.handleSubmit((values) => onSubmit(values, form))} noValidate className="flex min-h-0 flex-col gap-4">
      <div className="-mx-4 flex max-h-[62svh] flex-col gap-5 overflow-y-auto px-4 pb-1">
        {notice}
        {Boolean(failure) && <FormMessage>{errorMessage(error)}</FormMessage>}

        <div className="grid gap-4 sm:grid-cols-2">
          <TextField label="Job title" autoComplete="off" error={errors.title?.message} {...form.register("title")} />
          <TextField label="Company" autoComplete="off" error={errors.company?.message} {...form.register("company")} />
          <TextField label="City" optional autoComplete="off" error={errors.city?.message} {...form.register("city")} />
          <TextField
            label="State"
            optional
            autoComplete="off"
            placeholder="TX"
            error={errors.state?.message}
            {...form.register("state", { setValueAs: (value: string) => normalizeState(value ?? "") })}
          />
          <Controller
            control={form.control}
            name="work_mode"
            render={({ field }) => <EnumSelect label="Work arrangement" value={field.value} onChange={field.onChange} options={JOB_WORK_MODES} />}
          />
          <TextField
            label="Employment type"
            optional
            autoComplete="off"
            placeholder="Full-time"
            error={errors.employment_type?.message}
            {...form.register("employment_type")}
          />
          <TextField label="Seniority" optional autoComplete="off" placeholder="Senior" error={errors.seniority?.message} {...form.register("seniority")} />
          <TextField label="Posted on" optional type="date" error={errors.posted_at?.message} {...form.register("posted_at")} />
        </div>

        <fieldset className="grid gap-4 border-t pt-4 sm:grid-cols-3">
          <legend className="sr-only">Salary</legend>
          <TextField label="Salary from" optional inputMode="numeric" autoComplete="off" placeholder="140000" error={errors.salary_min?.message} {...form.register("salary_min")} />
          <TextField label="Salary to" optional inputMode="numeric" autoComplete="off" placeholder="180000" error={errors.salary_max?.message} {...form.register("salary_max")} />
          <TextField label="Currency" optional autoComplete="off" placeholder="USD" error={errors.salary_currency?.message} {...form.register("salary_currency")} />
          <TextField
            label="Salary as the posting words it"
            optional
            autoComplete="off"
            className="sm:col-span-3"
            description="Shown as written. Left empty, it is built from the amounts above."
            error={errors.salary_text?.message}
            {...form.register("salary_text")}
          />
        </fieldset>

        <fieldset className="grid gap-4 border-t pt-4 sm:grid-cols-3">
          <legend className="sr-only">Experience</legend>
          {!editing && (
            <>
              <TextField label="Years from" optional inputMode="decimal" autoComplete="off" error={errors.experience_min?.message} {...form.register("experience_min")} />
              <TextField label="Years to" optional inputMode="decimal" autoComplete="off" error={errors.experience_max?.message} {...form.register("experience_max")} />
            </>
          )}
          <TextField
            label="Experience as the posting words it"
            optional
            autoComplete="off"
            placeholder="3+ years"
            className={editing ? "sm:col-span-3" : undefined}
            error={errors.experience_text?.message}
            {...form.register("experience_text")}
          />
        </fieldset>

        <div className="flex flex-col gap-4 border-t pt-4">
          <Controller
            control={form.control}
            name="requirements"
            render={({ field }) => (
              <TagInput
                label="Skills named in the posting"
                optional
                value={field.value}
                onChange={field.onChange}
                placeholder="Python, SQL"
                max={80}
                maxLength={80}
                error={errors.requirements?.message}
              />
            )}
          />
          <TextareaField
            label="Description"
            optional
            rows={8}
            description="Plain text. Put each heading on its own line and start bullets with a dash. Markup is removed."
            error={errors.description?.message}
            {...form.register("description")}
          />
        </div>

        <fieldset className="grid gap-4 border-t pt-4">
          <legend className="sr-only">Links</legend>
          <TextField
            label="Posting address"
            optional
            type="url"
            inputMode="url"
            autoComplete="off"
            placeholder="https://"
            description="Members open this through Open Job when they apply for a candidate. They never see the address itself."
            error={errors.canonical_url?.message}
            {...form.register("canonical_url")}
          />
          <TextField label="Application form address" optional type="url" inputMode="url" autoComplete="off" placeholder="https://" error={errors.apply_url?.message} {...form.register("apply_url")} />
          {!editing && (
            <TextField label="Company site" optional type="url" inputMode="url" autoComplete="off" placeholder="https://" error={errors.company_url?.message} {...form.register("company_url")} />
          )}
        </fieldset>

        <fieldset className="grid gap-4 border-t pt-4 sm:grid-cols-2">
          <legend className="sr-only">Eligibility</legend>
          <Controller
            control={form.control}
            name="citizenship_requirement"
            render={({ field }) => <EnumSelect label="US citizenship" value={field.value} onChange={field.onChange} options={CITIZENSHIP_REQUIREMENTS} />}
          />
          <Controller
            control={form.control}
            name="sponsorship_status"
            render={({ field }) => <EnumSelect label="Visa sponsorship" value={field.value} onChange={field.onChange} options={SPONSORSHIP_STATUSES} />}
          />
          {!editing && (
            <div className="flex items-center gap-3 sm:col-span-2">
              <Controller
                control={form.control}
                name="clearance_required"
                render={({ field }) => <Switch id={clearanceId} checked={field.value} onCheckedChange={field.onChange} ref={field.ref} />}
              />
              <Label htmlFor={clearanceId}>The posting requires a security clearance</Label>
            </div>
          )}
        </fieldset>
      </div>

      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel} disabled={pending}>
          Cancel
        </Button>
        <Button type="submit" disabled={pending}>
          {pending && <Spinner data-icon="inline-start" />}
          {submitLabel}
        </Button>
      </DialogFooter>
    </form>
  );
}

function CreateJobFields({ draft, notice, onDone, onCancel }: { draft?: Partial<JobCreate>; notice?: React.ReactNode; onDone: (job: JobManagerView) => void; onCancel: () => void }) {
  const create = useCreateJob();
  return (
    <JobFields
      initial={toValues(draft)}
      editing={false}
      submitLabel="Save Job"
      pending={create.isPending}
      error={create.error}
      notice={notice}
      onCancel={onCancel}
      onSubmit={(values, form) =>
        create.mutate(toCreate(values), {
          onSuccess: (job) => {
            toast.success("Job added", { description: `${job.title} at ${job.company}. It is marked as not verified until you verify it.` });
            onDone(job);
          },
          onError: (error) => {
            applyServerErrors(form, error);
          },
        })
      }
    />
  );
}

/** Add a job by hand (POST /api/jobs). */
export function AddJobDialog({ open, onOpenChange, onCreated }: { open: boolean; onOpenChange: (open: boolean) => void; onCreated?: (job: JobManagerView) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="text-lg">Add Job</DialogTitle>
          <DialogDescription>Enter a posting by hand. Only the title and the company are required.</DialogDescription>
        </DialogHeader>
        {open && (
          <CreateJobFields
            onCancel={() => onOpenChange(false)}
            onDone={(job) => {
              onCreated?.(job);
              onOpenChange(false);
            }}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

/** Edit a stored job (PATCH /api/jobs/{id}). */
export function EditJobDialog({ job, open, onOpenChange }: { job: JobManagerView; open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="text-lg">Edit Job</DialogTitle>
          <DialogDescription>
            Changing the title, the company or a link of a job that was not found by a search marks it as not verified again.
          </DialogDescription>
        </DialogHeader>
        {open && <EditJobFields job={job} onClose={() => onOpenChange(false)} />}
      </DialogContent>
    </Dialog>
  );
}

function EditJobFields({ job, onClose }: { job: JobManagerView; onClose: () => void }) {
  const update = useUpdateJob(job.id);
  return (
    <JobFields
      initial={toValues(job)}
      editing
      submitLabel="Save Changes"
      pending={update.isPending}
      error={update.error}
      onCancel={onClose}
      onSubmit={(values, form) =>
        update.mutate(toUpdate(values), {
          onSuccess: () => {
            toast.success("Job saved");
            onClose();
          },
          onError: (error) => {
            applyServerErrors(form, error);
          },
        })
      }
    />
  );
}

/* ------------------------------------------------------------------ import from URL */

const urlSchema = z.object({
  url: z
    .string()
    .trim()
    .min(10, "Paste the full address of the posting.")
    .max(2000, "Use at most 2,000 characters.")
    .refine((value) => /^https?:\/\/[^\s]+$/i.test(value), "Enter a full address starting with http:// or https://."),
});

function ImportUrlSteps({ onClose, onCreated }: { onClose: () => void; onCreated?: (job: JobManagerView) => void }) {
  const read = useImportJobUrl();
  const [draft, setDraft] = useState<{ result: JobImportDraft; url: string } | null>(null);
  const form = useForm<z.infer<typeof urlSchema>>({ resolver: zodResolver(urlSchema), defaultValues: { url: "" } });

  if (draft) {
    const { result, url } = draft;
    // When the page could not be read as a posting, the Admin can still enter the job by hand with the link kept.
    const initial: Partial<JobCreate> = result.job ?? { canonical_url: url };
    return (
      <CreateJobFields
        draft={initial}
        onCancel={onClose}
        onDone={(job) => {
          onCreated?.(job);
          onClose();
        }}
        notice={
          <FormMessage tone={result.verified ? "success" : "info"}>
            <span className="flex items-start gap-2">
              {result.verified ? (
                <BadgeCheck className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              ) : (
                <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
              )}
              <span>
                <span className="font-medium">
                  {result.verified ? "The posting was read. Check the details before saving." : "The page could not be read as a job posting."}
                </span>{" "}
                {result.note}
                {!result.verified && " You can still fill in the job by hand; the link is kept."}
              </span>
            </span>
          </FormMessage>
        }
      />
    );
  }

  return (
    <form
      noValidate
      className="flex flex-col gap-4"
      onSubmit={form.handleSubmit(({ url }) =>
        read.mutate(url, {
          onSuccess: (result) => setDraft({ result, url }),
          onError: (error) => {
            applyServerErrors(form, error);
          },
        }),
      )}
    >
      {read.error && !(read.error instanceof ApiError && read.error.fields.url) && <FormMessage>{errorMessage(read.error)}</FormMessage>}
      <TextField
        label="Address of the posting"
        type="url"
        inputMode="url"
        autoComplete="off"
        placeholder="https://"
        description="The page is read and its details are shown for you to check. Nothing is saved at this step."
        error={form.formState.errors.url?.message}
        {...form.register("url")}
      />
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose} disabled={read.isPending}>
          Cancel
        </Button>
        <Button type="submit" disabled={read.isPending}>
          {read.isPending && <Spinner data-icon="inline-start" />}
          {read.isPending ? "Reading the page" : "Read Posting"}
        </Button>
      </DialogFooter>
    </form>
  );
}

/** Import one job from its address: read it (POST /api/jobs/import-url), review it, then save it (POST /api/jobs). */
export function ImportUrlDialog({ open, onOpenChange, onCreated }: { open: boolean; onOpenChange: (open: boolean) => void; onCreated?: (job: JobManagerView) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="text-lg">Import from URL</DialogTitle>
          <DialogDescription>Two steps: the posting is read first, then you review it and save.</DialogDescription>
        </DialogHeader>
        {open && <ImportUrlSteps onClose={() => onOpenChange(false)} onCreated={onCreated} />}
      </DialogContent>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ import CSV */

const CSV_MAX_BYTES = 2 * 1024 * 1024;
const CSV_COLUMNS = [
  "company",
  "title",
  "state",
  "city",
  "work_mode",
  "employment_type",
  "salary_text",
  "experience_text",
  "description",
  "requirements",
  "canonical_url",
  "apply_url",
  "citizenship_requirement",
  "sponsorship_status",
  "posted_at",
];

function CsvImportSteps({ onClose }: { onClose: () => void }) {
  const inputId = useId();
  const upload = useImportJobsCsv();
  const [file, setFile] = useState<File | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [result, setResult] = useState<CsvImportResult | null>(null);

  if (result) {
    return (
      <>
        <div className="flex flex-col gap-3">
          <dl className="grid grid-cols-3 gap-px overflow-hidden rounded-lg border bg-border">
            {(
              [
                ["Added", result.created],
                ["Updated", result.updated],
                ["Skipped", result.skipped],
              ] as const
            ).map(([label, value]) => (
              <div key={label} className="flex flex-col gap-1 bg-card px-3 py-2.5">
                <dt className="text-xs text-muted-foreground">{label}</dt>
                <dd className="font-figure text-2xl leading-none">{value.toLocaleString()}</dd>
              </div>
            ))}
          </dl>
          {result.errors.length > 0 ? (
            <div className="flex flex-col gap-1.5">
              <p className="text-sm font-medium">Rows that were not imported</p>
              <ul className="max-h-48 overflow-y-auto rounded-md border bg-muted/40 px-3 py-2 text-2sm">
                {result.errors.map((line) => (
                  <li key={line} className="py-0.5 break-words">
                    {line}
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">Every row was imported. New jobs are marked as not verified.</p>
          )}
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => {
              setResult(null);
              setFile(null);
            }}
          >
            Import Another File
          </Button>
          <Button onClick={onClose}>Done</Button>
        </DialogFooter>
      </>
    );
  }

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (!file) {
          setProblem("Choose a CSV file first.");
          return;
        }
        upload.mutate(file, { onSuccess: setResult, onError: (error) => setProblem(errorMessage(error)) });
      }}
    >
      <div className="flex flex-col gap-2">
        <Label htmlFor={inputId}>CSV file</Label>
        <Input
          id={inputId}
          type="file"
          accept=".csv,text/csv"
          onChange={(event) => {
            const next = event.target.files?.[0] ?? null;
            setFile(next);
            setProblem(next && next.size > CSV_MAX_BYTES ? `This file is ${formatBytes(next.size)}. The limit is 2 MB.` : null);
          }}
        />
        {problem && <FormMessage>{problem}</FormMessage>}
      </div>
      <div className="flex flex-col gap-2 rounded-md border bg-muted/40 px-3 py-2.5 text-2sm">
        <p className="flex items-center gap-2 font-medium text-foreground">
          <FileSpreadsheet className="size-4" aria-hidden="true" />
          What the file needs
        </p>
        <ul className="flex list-disc flex-col gap-1 pl-5 text-muted-foreground">
          <li>UTF-8, with a header row, at most 2 MB. The first 500 rows are read.</li>
          <li>
            The columns <code className="text-foreground">company</code> and <code className="text-foreground">title</code> are required.
          </li>
          <li>Separate several skills in <code className="text-foreground">requirements</code> with a semicolon or a vertical bar.</li>
          <li>A posting that is already stored (same link) is skipped.</li>
        </ul>
        <p className="break-words text-muted-foreground">
          Recognised columns: <span className="text-foreground">{CSV_COLUMNS.join(", ")}</span>
        </p>
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose} disabled={upload.isPending}>
          Cancel
        </Button>
        <Button type="submit" disabled={upload.isPending || !file || Boolean(file && file.size > CSV_MAX_BYTES)}>
          {upload.isPending && <Spinner data-icon="inline-start" />}
          Import Jobs
        </Button>
      </DialogFooter>
    </form>
  );
}

/** Import many jobs from a CSV file (POST /api/jobs/import-csv) and report exactly what happened to each row. */
export function CsvImportDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle className="text-lg">Import CSV</DialogTitle>
          <DialogDescription>Add many jobs at once from a spreadsheet export.</DialogDescription>
        </DialogHeader>
        {open && <CsvImportSteps onClose={() => onOpenChange(false)} />}
      </DialogContent>
    </Dialog>
  );
}
