"use client";

/**
 * AI auto-apply: the one place the UI knows the shape of the auto-apply API.
 *
 * Follows docs/auto-apply-api.md; the types are aliases of the generated `components["schemas"]` (openapi.json,
 * `npm run gen:api`). The normalisers (`toRun`, ...) keep every screen working on the same shapes - nothing
 * outside this file reads the raw responses.
 *
 * Authorization stays in the API. The UI hides the feature for everyone but an Admin (the Application
 * answers form is also shown to a Team Lead).
 */
import { useSyncExternalStore } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { api, ApiError, buildUrl } from "./api";
import { invalidateWork, LIVE_INTERVAL } from "./queries";
import type { components } from "./api-types";
import type { Page } from "./types";

/* ------------------------------------------------------------------ types */

/** Generated from openapi.json (`npm run gen:api`). The screens use the normalised shapes below. */
type S = components["schemas"];

export type RunStatus = S["AutoApplyRunOut"]["status"];
export const RUN_STATUSES = [
  "queued",
  "preparing",
  "needs_input",
  "ready_for_review",
  "approved",
  "submitting",
  "submitted",
  "failed",
  "blocked",
  "cancelled",
] as const satisfies readonly RunStatus[];

export type Ats = S["AutoApplyRunOut"]["ats"];

export type AnswerSource =
  | "profile"
  | "candidate"
  | "application_answers"
  | "resume_file"
  | "resume_line"
  | "salesforce"
  | "ai_draft"
  | "eeo_decline"
  | "admin_edit";

/** `email` may be missing on the references the UI builds itself. */
export type UserRef = Pick<S["UserRef"], "id" | "name">;
export type CandidateRef = S["CandidateRef"];
export type NeedsInput = S["AutoApplyNeedsInput"];
export type Blocker = S["AutoApplyBlocker"];
export type RunCounts = S["AutoApplyCounts"];
export type JobRef = S["AutoApplyJobRef"];
export type FormField = S["AutoApplyFormField"];

/** An answer, with the options of its form field (from the form snapshot) for editing. */
export type RunAnswer = S["AutoApplyAnswer"] & { options: string[] };

/** A screenshot, with the address it is served from. */
export type Screenshot = S["AutoApplyScreenshotOut"] & { url: string };

type Detail = S["AutoApplyRunDetail"];

/** A run (list row or detail). Optional/empty API fields are normalised to `null`. */
export interface AutoApplyRun {
  id: string;
  status: RunStatus;
  ats: Ats;
  job_url: string | null;
  candidate: CandidateRef | null;
  assignment_id: string | null;
  job: JobRef;
  blocked_reasons: Blocker[];
  needs_input: NeedsInput[];
  counts: RunCounts;
  outcome_unknown: boolean;
  error: string | null;
  created_by: UserRef | null;
  approved_by: UserRef | null;
  created_at: string | null;
  updated_at: string | null;
  approved_at: string | null;
  submitted_at: string | null;
  finished_at: string | null;
  /** Detail only (empty in lists). */
  answers: RunAnswer[];
  screenshots: Screenshot[];
  answers_hash: string | null;
  confirmation: (Omit<NonNullable<Detail["confirmation"]>, "reference" | "url"> & { reference: string | null; url: string | null }) | null;
  submission_guard: Pick<NonNullable<Detail["submission_guard"]>, "blocked_requests" | "allowed_submissions"> | null;
  /** True once the detail (not only the list row) has been read. */
  detailed: boolean;
}

export type ConsentMethod = S["ConsentIn"]["method"];
export type Consent = Omit<S["ConsentOut"], "recorded_by" | "revoked_by" | "revoked_at" | "recorded_at"> & {
  recorded_by: UserRef | null;
  recorded_at: string | null;
  revoked_at: string | null;
  revoked_by: UserRef | null;
};
export type ConsentIn = S["ConsentIn"];

/** "unknown" is the default of every answer: nothing is assumed. */
export type Tri = S["ApplicationAnswersIn"]["authorized_us"];

export const TRI_FIELDS = [
  "authorized_us",
  "sponsorship_now_or_future",
  "age_18_or_over",
  "willing_to_relocate",
  "restrictive_agreements",
] as const satisfies readonly (keyof S["ApplicationAnswersIn"])[];
export const EEO_FIELDS = ["eeo_gender", "eeo_race", "eeo_hispanic_latino", "eeo_veteran", "eeo_disability"] as const satisfies readonly (keyof S["ApplicationAnswersIn"])[];
export const TEXT_FIELDS = [
  "earliest_start",
  "notice_period",
  "expected_salary",
  "linkedin_url",
  "github_url",
  "portfolio_url",
  "current_company",
  "current_title",
  "city",
  "state",
  "country",
  "phone",
  "first_name",
  "last_name",
  "preferred_name",
  "referral_source",
] as const satisfies readonly (keyof S["ApplicationAnswersIn"])[];

export type ApplicationAnswersIn = Required<S["ApplicationAnswersIn"]>;

export type ApplicationAnswers = ApplicationAnswersIn & {
  /** The value used after fallbacks (profile, candidate details), with where it came from. */
  effective: Record<string, { value: string; source: string }>;
  updated_at: string | null;
  updated_by: UserRef | null;
};

export type AutoApplyStatus = S["AutoApplyStatus"];

export type Eligibility = Omit<S["AutoApplyEligibility"], "open_run_id" | "open_run_status"> & {
  open_run_id: string | null;
  open_run_status: RunStatus | null;
};
export type EligibilityOut = Omit<S["AutoApplyEligibilityOut"], "items"> & { items: Eligibility[] };

export type RefusalReason = S["AutoApplyRefusal"]["reason"];
export type SkipReason = S["AutoApplySkipped"]["reason"];

/* ------------------------------------------------------------------ labels */

export const RUN_STATUS_META: Record<RunStatus, { label: string; tone: "neutral" | "primary" | "success" | "warning" | "danger" }> = {
  queued: { label: "Queued", tone: "neutral" },
  preparing: { label: "Preparing", tone: "primary" },
  needs_input: { label: "Needs input", tone: "warning" },
  ready_for_review: { label: "Ready for review", tone: "primary" },
  approved: { label: "Approved", tone: "primary" },
  submitting: { label: "Submitting", tone: "primary" },
  submitted: { label: "Submitted", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  blocked: { label: "Blocked", tone: "danger" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

/** Runs the worker is busy with: screens poll while one of these is shown. */
export const ACTIVE_RUN_STATUSES: RunStatus[] = ["queued", "preparing", "approved", "submitting"];
/** What the Admin queue lists. */
export const ATTENTION_STATUSES: RunStatus[] = ["needs_input", "ready_for_review", "failed", "blocked"];
/** "Open" in the contract: one per assignment, and these can be cancelled. */
export const OPEN_STATUSES: RunStatus[] = ["queued", "preparing", "needs_input", "ready_for_review", "approved", "submitting"];

export const isRunActive = (status: RunStatus) => ACTIVE_RUN_STATUSES.includes(status);

export const ANSWER_SOURCE_META: Record<AnswerSource, { label: string; tone: "neutral" | "primary" | "success" | "warning" }> = {
  profile: { label: "Profile", tone: "neutral" },
  candidate: { label: "Candidate details", tone: "neutral" },
  application_answers: { label: "Application answers", tone: "neutral" },
  resume_file: { label: "Resume file", tone: "neutral" },
  resume_line: { label: "Resume", tone: "neutral" },
  salesforce: { label: "Salesforce", tone: "neutral" },
  ai_draft: { label: "AI draft", tone: "warning" },
  eeo_decline: { label: "Declined (EEO)", tone: "neutral" },
  admin_edit: { label: "Edited by Admin", tone: "primary" },
};

export const CONSENT_METHODS: Array<{ value: ConsentMethod; label: string }> = [
  { value: "written", label: "Signed in writing" },
  { value: "email", label: "By e-mail" },
  { value: "e_signature", label: "E-signature" },
  { value: "verbal_recorded", label: "Verbal, recorded" },
];

export const consentMethodLabel = (value: string) => CONSENT_METHODS.find((m) => m.value === value)?.label ?? value;

/** Blocker codes in plain words. Unknown codes fall back to the API's detail. */
const BLOCKERS: Record<string, string> = {
  captcha_visible: "The form shows a visible CAPTCHA. The portal never solves one: a person has to apply.",
  challenge_page: "The site showed a security check page before the form.",
  sign_in_required: "The employer's site requires the candidate to sign in.",
  account_creation_required: "The employer's site requires the candidate to create an account.",
  email_code_required: "The site sends a code to the candidate's e-mail that has to be entered.",
  anti_bot_puzzle: "The form asks a question or puzzle meant to tell people from automated tools.",
  personal_completion_certification: "The form asks the candidate to certify that they completed the application personally.",
  attestation_not_permitted: "The form asks to certify that the answers are true, and the candidate's consent does not allow attesting.",
  data_processing_consent_not_permitted: "The form asks to accept the employer's privacy or data-processing notice, and the consent does not allow it.",
  arbitration_agreement: "The form asks the candidate to accept an arbitration agreement. Only the candidate can agree to that.",
  unsupported_site: "This application system is not supported.",
  custom_career_site: "A custom career site: it cannot be filled automatically.",
  employer_iframe: "The form is embedded from another site and cannot be filled safely.",
  robots_disallowed: "The site asks automated tools not to open this page.",
  no_resume_file: "No resume file is stored for the candidate.",
  no_consent: "No consent to apply on the candidate's behalf is in force.",
  form_not_found: "No application form was found on the page.",
  form_changed: "The employer changed the form since it was read.",
  employer_daily_cap: "The daily limit of applications to this employer is reached. Try again tomorrow.",
  posting_closed: "The posting is closed.",
};
export const blockerText = (b: Blocker) => BLOCKERS[b.code] ?? (b.detail || b.code);

const NEEDS_INPUT_REASONS: Record<string, string> = {
  not_recorded: "not recorded",
  unknown: "recorded as Unknown",
  no_matching_option: "the recorded answer matches none of the form's options",
  free_text_unverified: "the AI draft could not be checked against the resume",
  required_unknown_field: "a required question the portal does not understand",
  file_required: "a file is required",
};
export const needsInputReason = (reason: string) => NEEDS_INPUT_REASONS[reason] ?? reason;

const SKIP_REASONS: Record<SkipReason, string> = {
  not_found: "not found",
  not_this_candidate: "not this candidate's job",
  withdrawn: "withdrawn",
  already_applied: "already applied",
  run_open: "an application is already being prepared",
  already_submitted: "already submitted once",
  no_application_url: "no application link",
};
export const skipReason = (reason: string) => SKIP_REASONS[reason as SkipReason] ?? reason;

/**
 * Why an approval was refused. The codes are stable: they are the `reason` of a batch refusal and the prefix
 * of a single approve's 409 `error` ("answers_changed: ...").
 */
const REFUSALS: Record<RefusalReason, string> = {
  not_found: "The run no longer exists.",
  not_ready: "The run is no longer ready for review.",
  answers_changed: "The answers changed since you opened the preview. Open it again, review it, then approve.",
  no_consent: "The candidate's consent is no longer in force.",
  assignment_changed: "The job was applied to or withdrawn meanwhile.",
  employer_daily_cap: "Today's limit of applications to this employer is reached. Try again tomorrow.",
  disabled: "Auto-apply is switched off on the server.",
  already_submitted: "An application to this posting was already submitted for the candidate.",
};
export const refusalText = (reason: string, detail = "") => REFUSALS[reason as RefusalReason] ?? (detail || reason);

/** The plain-language text of a failed single approve: its 409 error starts with a refusal code. */
export function approveErrorText(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const code = error.message.split(":", 1)[0].trim();
  if (code in REFUSALS) return REFUSALS[code as RefusalReason];
  if (/switched off/i.test(error.message)) return REFUSALS.disabled;
  return null;
}

/* ------------------------------------------------------------------ ATS */

export interface AtsInfo {
  name: string;
  supported: boolean;
  reason: string | null;
}

const ATS_LABELS: Record<string, string> = { greenhouse: "Greenhouse", lever: "Lever", ashby: "Ashby", workable: "Workable", unsupported: "Unsupported" };
export const atsLabel = (ats: string) => ATS_LABELS[ats] ?? ats;

/** Why an assignment cannot be started, in plain words (the API also sends an English `reason`). */
const ELIGIBILITY_REASONS: Record<string, string> = {
  no_application_url: "The job has no application link.",
  unsupported_site: "This application system is not supported.",
  account_creation_required: "The site (Workday) requires the candidate's own account.",
  custom_career_site: "A custom career site cannot be filled automatically.",
  run_open: "An application is already being prepared.",
  already_submitted: "An application was already submitted for this job.",
};
export const eligibilityReason = (item: Pick<Eligibility, "reason_code" | "reason">) =>
  ELIGIBILITY_REASONS[item.reason_code] ?? (item.reason || item.reason_code);

/** The badge of one eligibility row: which system, and whether auto-apply can fill it. */
export function atsInfo(item: Eligibility): AtsInfo {
  const name = item.ats === "unsupported" ? (item.reason_code === "no_application_url" ? "No link" : item.reason_code === "account_creation_required" ? "Workday" : "Career site") : atsLabel(item.ats);
  return { name, supported: item.supported, reason: item.supported ? null : eligibilityReason(item) };
}

/* ------------------------------------------------------------------ normalisers */

const str = (v: unknown, fallback = ""): string => (typeof v === "string" ? v : fallback);
const strOrNull = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const rec = (v: unknown): Record<string, unknown> => (v && typeof v === "object" ? (v as Record<string, unknown>) : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const ref = (v: unknown): UserRef | null => {
  if (!v || typeof v !== "object") return null;
  const r = v as Record<string, unknown>;
  return { id: str(r.id), name: str(r.name, str(r.email, "Someone")) };
};

function toRun(raw: unknown): AutoApplyRun {
  const r = rec(raw);
  const job = rec(r.job);
  const status = RUN_STATUSES.includes(r.status as RunStatus) ? (r.status as RunStatus) : "queued";
  const id = str(r.id);
  const form = new Map(arr(r.form).map((f) => [str(rec(f).key), rec(f)]));
  const counts = rec(r.counts);
  const confirmation = r.confirmation && typeof r.confirmation === "object" ? rec(r.confirmation) : null;
  const guard = r.submission_guard && typeof r.submission_guard === "object" ? rec(r.submission_guard) : null;
  return {
    id,
    status,
    ats: str(r.ats, "unsupported") as Ats,
    job_url: strOrNull(r.job_url),
    candidate: ref(r.candidate),
    assignment_id: strOrNull(r.assignment_id),
    job: { id: str(job.id), title: str(job.title, "Untitled job"), company: str(job.company) },
    blocked_reasons: arr(r.blocked_reasons).map((b) => (typeof b === "string" ? { code: b, detail: "" } : { code: str(rec(b).code), detail: str(rec(b).detail) })),
    needs_input: arr(r.needs_input).map((n) => {
      const x = rec(n);
      return { field_key: str(x.field_key), label: str(x.label, str(x.field_key)), reason: str(x.reason), fill_in: str(x.fill_in) };
    }),
    counts: { fields: num(counts.fields), answered: num(counts.answered), needs_input: num(counts.needs_input), ai_drafted: num(counts.ai_drafted) },
    outcome_unknown: r.outcome_unknown === true,
    error: strOrNull(r.error),
    created_by: ref(r.created_by),
    approved_by: ref(r.approved_by),
    created_at: strOrNull(r.created_at),
    updated_at: strOrNull(r.updated_at),
    approved_at: strOrNull(r.approved_at),
    submitted_at: strOrNull(r.submitted_at),
    finished_at: strOrNull(r.finished_at),
    answers: arr(r.answers).map((a) => {
      const x = rec(a);
      const key = str(x.field_key);
      const field = form.get(key) ?? {};
      return {
        field_key: key,
        label: str(x.label, str(field.label, key)),
        value: typeof x.value === "string" ? x.value : x.value == null ? "" : String(x.value),
        source: str(x.source),
        evidence: arr(x.evidence).filter((e): e is string => typeof e === "string"),
        classification: str(x.classification),
        required: x.required === true,
        status: (["ok", "missing", "blocked"].includes(str(x.status)) ? str(x.status) : "ok") as RunAnswer["status"],
        note: str(x.note),
        type: str(field.type, "text"),
        options: arr(field.options).filter((o): o is string => typeof o === "string"),
      };
    }),
    screenshots: arr(r.screenshots).map((s) => {
      const x = rec(s);
      return {
        id: str(x.id),
        kind: str(x.kind, "dry_run") as Screenshot["kind"],
        size_bytes: num(x.size_bytes),
        created_at: str(x.created_at),
        url: `/api/auto-apply/runs/${id}/screenshots/${str(x.id)}`,
      };
    }),
    answers_hash: strOrNull(r.answers_hash),
    confirmation: confirmation
      ? {
          detected: confirmation.detected === true,
          text_sha256: str(confirmation.text_sha256),
          reference: strOrNull(confirmation.reference),
          url: strOrNull(confirmation.url),
        }
      : null,
    submission_guard: guard ? { blocked_requests: num(guard.blocked_requests), allowed_submissions: num(guard.allowed_submissions) } : null,
    detailed: "answers" in r,
  };
}

function toConsent(raw: unknown): Consent {
  const r = rec(raw);
  const revoked = strOrNull(r.revoked_at);
  const granted = r.granted !== false;
  return {
    id: str(r.id),
    scope: str(r.scope, "auto_apply"),
    granted,
    active: typeof r.active === "boolean" ? r.active : granted && !revoked,
    method: str(r.method) as ConsentMethod,
    candidate_id: str(r.candidate_id),
    may_attest: r.may_attest === true,
    may_accept_data_processing: r.may_accept_data_processing === true,
    note: str(r.note),
    document_reference: str(r.document_reference),
    recorded_by: ref(r.recorded_by),
    recorded_at: strOrNull(r.recorded_at),
    revoked_at: revoked,
    revoked_by: ref(r.revoked_by),
  };
}

export const EMPTY_ANSWERS: ApplicationAnswersIn = {
  ...(Object.fromEntries(TRI_FIELDS.map((k) => [k, "unknown"])) as Record<(typeof TRI_FIELDS)[number], Tri>),
  ...(Object.fromEntries(EEO_FIELDS.map((k) => [k, "decline"])) as Record<(typeof EEO_FIELDS)[number], string>),
  ...(Object.fromEntries(TEXT_FIELDS.map((k) => [k, ""])) as Record<(typeof TEXT_FIELDS)[number], string>),
  previously_employed_by: [],
  previous_employers_complete: false,
};

function toAnswers(raw: unknown): ApplicationAnswers {
  const r = rec(raw);
  const out: ApplicationAnswersIn = { ...EMPTY_ANSWERS };
  for (const key of TRI_FIELDS) out[key] = r[key] === "yes" || r[key] === "no" ? (r[key] as Tri) : "unknown";
  for (const key of EEO_FIELDS) out[key] = typeof r[key] === "string" && r[key] ? (r[key] as string) : "decline";
  for (const key of TEXT_FIELDS) out[key] = typeof r[key] === "string" ? (r[key] as string) : "";
  out.previously_employed_by = arr(r.previously_employed_by).filter((x): x is string => typeof x === "string");
  out.previous_employers_complete = r.previous_employers_complete === true;
  const effective: ApplicationAnswers["effective"] = {};
  for (const [key, value] of Object.entries(rec(r.effective))) {
    const x = rec(value);
    effective[key] = { value: typeof x.value === "string" ? x.value : x.value == null ? "" : String(x.value), source: str(x.source) };
  }
  return { ...out, effective, updated_at: strOrNull(r.updated_at), updated_by: ref(r.updated_by) };
}

function toPage<T>(raw: unknown, map: (x: unknown) => T): Page<T> {
  if (Array.isArray(raw)) return { items: raw.map(map), total: raw.length, page: 1, page_size: raw.length };
  const r = rec(raw);
  const items = arr(r.items).map(map);
  return { items, total: typeof r.total === "number" ? r.total : items.length, page: num(r.page) || 1, page_size: num(r.page_size) || items.length };
}

function toStatus(raw: unknown): AutoApplyStatus {
  const r = rec(raw);
  return {
    enabled: r.enabled === true,
    worker_online: r.worker_online === true,
    browser_concurrency: num(r.browser_concurrency),
    employer_daily_cap: num(r.employer_daily_cap),
    run_timeout_seconds: num(r.run_timeout_seconds),
    screenshot_retention_days: num(r.screenshot_retention_days),
    supported_ats: arr(r.supported_ats).filter((x): x is string => typeof x === "string"),
  };
}

function toEligibility(raw: unknown): EligibilityOut {
  const r = rec(raw);
  return {
    candidate_id: str(r.candidate_id),
    has_active_consent: r.has_active_consent === true,
    has_resume_file: r.has_resume_file === true,
    items: arr(r.items).map((x) => {
      const i = rec(x);
      const job = rec(i.job);
      const ats = str(i.ats, "unsupported") as Ats;
      return {
        assignment_id: str(i.assignment_id),
        job: { id: str(job.id), title: str(job.title, "Untitled job"), company: str(job.company) },
        status: str(i.status),
        ats,
        application_url: str(i.application_url),
        supported: i.supported === true,
        eligible: i.eligible === true,
        reason_code: str(i.reason_code),
        reason: str(i.reason),
        open_run_id: strOrNull(i.open_run_id),
        open_run_status: RUN_STATUSES.includes(i.open_run_status as RunStatus) ? (i.open_run_status as RunStatus) : null,
      };
    }),
  };
}

/* ------------------------------------------------------------------ queries */

export const aak = {
  all: ["auto-apply"] as const,
  status: ["auto-apply", "status"] as const,
  eligibility: (candidateId: string) => ["auto-apply", "eligibility", candidateId] as const,
  runs: (params: object) => ["auto-apply", "runs", params] as const,
  run: (id: string) => ["auto-apply", "run", id] as const,
  consents: (candidateId: string) => ["auto-apply", "consents", candidateId] as const,
  answers: (candidateId: string) => ["auto-apply", "answers", candidateId] as const,
};

export const RUN_POLL_INTERVAL = 3_000;

export interface RunListParams {
  candidate_id?: string;
  assignment_id?: string;
  /** Sent as a repeated `status` parameter. */
  status?: RunStatus[];
  ats?: string;
  page?: number;
  page_size?: number;
}

function runsUrl(params: RunListParams): string {
  const { status, ...rest } = params;
  const base = buildUrl("/api/auto-apply/runs", rest as Record<string, string | number | undefined>);
  if (!status || status.length === 0) return base;
  const extra = status.map((s) => `status=${encodeURIComponent(s)}`).join("&");
  return `${base}${base.includes("?") ? "&" : "?"}${extra}`;
}

/** GET /api/auto-apply/status (Admin only). Feeds the settings screen and the start dialog. */
export function useAutoApplyStatus(enabled = true) {
  return useQuery({
    queryKey: aak.status,
    queryFn: async ({ signal }) => toStatus(await api.get<unknown>("/api/auto-apply/status", undefined, signal)),
    enabled,
    staleTime: 30_000,
    refetchInterval: 60_000,
  });
}

/** Every open assignment of the candidate with its application system and whether a run can start. */
export function useEligibility(candidateId: string | undefined) {
  return useQuery({
    queryKey: aak.eligibility(candidateId ?? ""),
    queryFn: async ({ signal }) =>
      toEligibility(await api.get<unknown>(`/api/auto-apply/candidates/${candidateId}/eligibility`, undefined, signal)),
    enabled: Boolean(candidateId),
  });
}

/** Polls quickly while a listed run is being worked on (or `fast`), otherwise at the normal list pace. */
export function useAutoApplyRuns(params: RunListParams, enabled = true, fast = false) {
  return useQuery({
    queryKey: aak.runs(params),
    queryFn: async ({ signal }) => toPage(await api.get<unknown>(runsUrl(params), undefined, signal), toRun),
    placeholderData: keepPreviousData,
    enabled,
    refetchInterval: (query) =>
      fast || query.state.data?.items.some((run) => isRunActive(run.status)) ? RUN_POLL_INTERVAL : LIVE_INTERVAL,
  });
}

export function useAutoApplyRun(id: string | null | undefined) {
  return useQuery({
    queryKey: aak.run(id ?? ""),
    queryFn: async ({ signal }) => toRun(await api.get<unknown>(`/api/auto-apply/runs/${id}`, undefined, signal)),
    enabled: Boolean(id),
    refetchInterval: (query) => (query.state.data && isRunActive(query.state.data.status) ? RUN_POLL_INTERVAL : false),
  });
}

const invalidateRuns = (client: QueryClient) => client.invalidateQueries({ queryKey: aak.all });

export type RunsCreated = Omit<S["AutoApplyRunsCreated"], "runs"> & { runs: AutoApplyRun[] };

export function useStartRuns() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (body: { candidate_id: string; assignment_ids: string[] }): Promise<RunsCreated> => {
      const raw = rec(await api.post<unknown>("/api/auto-apply/runs", body));
      return {
        runs: arr(raw.runs).map(toRun),
        skipped: arr(raw.skipped).map((x) => ({ assignment_id: str(rec(x).assignment_id), reason: str(rec(x).reason) as SkipReason })),
      };
    },
    onSuccess: () => invalidateRuns(client),
  });
}

/** Values are recorded with source admin_edit and a new dry run is queued (status preparing). */
export function useEditAnswers(runId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (answers: Array<{ field_key: string; value: string }>) =>
      toRun(await api.patch<unknown>(`/api/auto-apply/runs/${runId}/answers`, { answers })),
    onSuccess: (run) => {
      client.setQueryData(aak.run(runId), run);
      return invalidateRuns(client);
    },
  });
}

/** Approves exactly the preview the Admin looked at: the API refuses (409) when its answers_hash changed. */
export function useApproveRun(runId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (answers_hash: string) =>
      toRun(await api.post<unknown>(`/api/auto-apply/runs/${runId}/approve`, { answers_hash })),
    onSuccess: (run) => {
      client.setQueryData(aak.run(runId), run);
      return Promise.all([invalidateRuns(client), invalidateWork(client)]);
    },
    onError: () => invalidateRuns(client),
  });
}

export function useCancelRun(runId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async () => toRun(await api.post<unknown>(`/api/auto-apply/runs/${runId}/cancel`)),
    onSuccess: (run) => {
      client.setQueryData(aak.run(runId), run);
      return invalidateRuns(client);
    },
  });
}

export type BatchResult = S["AutoApplyBatchApproveOut"];

export function useApproveBatch() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (items: Array<{ run_id: string; answers_hash: string }>): Promise<BatchResult> => {
      const raw = rec(await api.post<unknown>("/api/auto-apply/runs/approve-batch", { items }));
      return {
        approved: arr(raw.approved).filter((x): x is string => typeof x === "string"),
        refused: arr(raw.refused).map((x) => ({ run_id: str(rec(x).run_id), reason: str(rec(x).reason) as RefusalReason, detail: str(rec(x).detail) })),
      };
    },
    onSettled: () => Promise.all([invalidateRuns(client), invalidateWork(client)]),
  });
}

/* consent */

export function useConsents(candidateId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: aak.consents(candidateId ?? ""),
    queryFn: async ({ signal }) => {
      const raw = await api.get<unknown>(`/api/candidates/${candidateId}/consents`, undefined, signal);
      return (Array.isArray(raw) ? raw : arr(rec(raw).items)).map(toConsent);
    },
    enabled: Boolean(candidateId) && enabled,
  });
}

/** The consent in force for auto-apply. A new consent supersedes the previous one; the newest active wins. */
export function activeConsent(consents: Consent[] | undefined): Consent | null {
  return (
    (consents ?? [])
      .filter((c) => c.scope === "auto_apply" && c.active)
      .sort((a, b) => (b.recorded_at ?? "").localeCompare(a.recorded_at ?? ""))[0] ?? null
  );
}

export function useRecordConsent(candidateId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (body: ConsentIn) => toConsent(await api.post<unknown>(`/api/candidates/${candidateId}/consents`, body)),
    // Consents, eligibility and runs (recording a refusal cancels open runs).
    onSuccess: () => invalidateRuns(client),
  });
}

/** Revoking cancels every open run of the candidate (consent_revoked). */
export function useRevokeConsent(candidateId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ consentId, note }: { consentId: string; note?: string }) =>
      toConsent(await api.post<unknown>(`/api/candidates/${candidateId}/consents/${consentId}/revoke`, note ? { note } : {})),
    onSuccess: () => invalidateRuns(client),
  });
}

/* application answers */

export function useApplicationAnswers(candidateId: string | undefined) {
  return useQuery({
    queryKey: aak.answers(candidateId ?? ""),
    queryFn: async ({ signal }) =>
      toAnswers(await api.get<unknown>(`/api/candidates/${candidateId}/application-answers`, undefined, signal)),
    enabled: Boolean(candidateId),
  });
}

/** Only the fields sent change. */
export function useUpdateApplicationAnswers(candidateId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (body: Partial<ApplicationAnswersIn>) =>
      toAnswers(await api.patch<unknown>(`/api/candidates/${candidateId}/application-answers`, body)),
    onSuccess: (answers) => {
      client.setQueryData(aak.answers(candidateId), answers);
      return invalidateRuns(client);
    },
  });
}

/* ------------------------------------------------------------------ previewed in this session */

/**
 * Runs whose preview the Admin opened in this browser tab, with the answers_hash of that preview.
 * A ready run can only be picked for batch approval after its preview was seen, and the batch approves
 * exactly that preview (the API refuses a run whose answers changed since). Kept in sessionStorage so a
 * reload keeps it; a new tab starts empty.
 */
const PREVIEW_KEY = "auto-apply-previewed";
let previewed: Map<string, string> | null = null;
const listeners = new Set<() => void>();

function previewMap(): Map<string, string> {
  if (previewed) return previewed;
  previewed = new Map();
  try {
    const raw = window.sessionStorage.getItem(PREVIEW_KEY);
    if (raw) for (const [id, hash] of Object.entries(JSON.parse(raw) as Record<string, string>)) previewed.set(id, hash);
  } catch {
    // Storage unavailable: the record lives in memory only.
  }
  return previewed;
}

/** Records that the detail of a ready run, with this answers_hash, was shown. */
export function markPreviewed(id: string, hash: string): void {
  const map = previewMap();
  if (map.get(id) === hash) return;
  previewed = new Map(map).set(id, hash);
  try {
    window.sessionStorage.setItem(PREVIEW_KEY, JSON.stringify(Object.fromEntries(previewed)));
  } catch {
    // ignore
  }
  listeners.forEach((listener) => listener());
}

const EMPTY_MAP = new Map<string, string>();
export function usePreviewed(): Map<string, string> {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => previewMap(),
    () => EMPTY_MAP,
  );
}
