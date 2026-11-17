"use client";

/**
 * Everything the web app knows about the Salesforce candidate import, in one place: the
 * shapes the portal API answers with, the calls, and the words for its failures.
 *
 * The browser never talks to Salesforce. Every call here goes to the portal's own /api,
 * which holds the credentials, decides who may see or bring in which candidate, and is
 * the only side that reads Salesforce (it never writes there).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "./api";
import { invalidateCandidates, qk } from "./queries";
import type { components } from "./api-types";
import type { Candidate, CandidateListParams, Integrations, PortalSettings, Resume } from "./types";

/* ------------------------------------------------------------------ contract (docs/salesforce-api.md; shapes generated from openapi.json) */

type Schemas = components["schemas"];

export type TeamLeadScope = Schemas["SettingsOut"]["salesforce_team_lead_scope"];
/** GET /api/salesforce/status; also `salesforce` in GET /api/integrations. */
export type SalesforceStatus = Schemas["SalesforceStatus"];
/** One person found in Salesforce. */
export type SalesforceCandidate = Schemas["SalesforceCandidate"];
/** GET /api/salesforce/candidates. */
export type SalesforceSearchResult = Schemas["SalesforceSearchOut"];
/** POST /api/candidates/import-salesforce. `team_id` is required for an Admin; a Team Lead's own team is used. */
export type SalesforceImport = Schemas["SalesforceImportIn"];
/** What an import or a refresh did: fields written, fields kept as changed in the portal, and what became of the resume. */
export type SalesforceSync = Schemas["SalesforceSync"];
export type SyncResume = SalesforceSync["resume"];
/** The candidate as every other call returns it, plus what this call did. */
export type SyncedCandidate = Schemas["CandidateSyncOut"];

export type SalesforceSearchStatus = "active" | "all";

export interface SalesforceSearchParams {
  q: string;
  status: SalesforceSearchStatus;
  limit: number;
}

/** Fewer characters than this find nothing: the API answers with an empty list without asking Salesforce. */
export const SALESFORCE_MIN_QUERY = 2;
/** The API accepts at most this many characters in `q`. */
export const SALESFORCE_MAX_QUERY = 80;
/** Results asked for at once (the API allows 1 to 25). */
export const SALESFORCE_PAGE = 20;

const NOT_CONFIGURED: SalesforceStatus = {
  configured: false,
  connected: null,
  message: "",
  checked_at: null,
  api_version: "",
  team_lead_scope: "all",
};

/* ------------------------------------------------------------------ what a portal candidate says about Salesforce */

export type CandidateSource = Candidate["source"];

export interface CandidateOrigin {
  source: CandidateSource;
  /** The record's page in Salesforce. The API sends it to managers only. */
  salesforceUrl: string | null;
  /** When the portal last read this candidate from Salesforce. */
  syncedAt: string | null;
}

/** Only an https address is ever put into a link. */
function safeUrl(value: string | null | undefined): string | null {
  const url = (value ?? "").trim();
  return /^https:\/\/[^\s]+$/i.test(url) ? url : null;
}

export function candidateOrigin(candidate: Candidate): CandidateOrigin {
  return {
    source: candidate.source,
    salesforceUrl: safeUrl(candidate.salesforce_url),
    syncedAt: candidate.salesforce_synced_at ?? null,
  };
}

export const isFromSalesforce = (candidate: Candidate): boolean => candidate.source === "salesforce";

export type ResumeSource = Resume["source"];

export const resumeSource = (resume: Resume): ResumeSource => resume.source;

/* ------------------------------------------------------------------ failures */

export type SalesforceProblemKind = "rate" | "unreachable" | "other";

/** Which of the answers the API gives for a Salesforce call this is: 429, 502/503, or something else. */
export function salesforceProblem(error: unknown): { kind: SalesforceProblemKind; message: string } {
  if (error instanceof ApiError) {
    if (error.status === 429) return { kind: "rate", message: error.message };
    if (error.isNetwork) return { kind: "unreachable", message: error.message };
    return { kind: "other", message: error.message };
  }
  return { kind: "other", message: error instanceof Error && error.message ? error.message : "Something went wrong. Try again." };
}

/** The candidate as GET /api/candidates/{id} gives it. */
function withoutSync(synced: SyncedCandidate): Candidate {
  const candidate: Candidate & { sync?: SalesforceSync } = { ...synced };
  delete candidate.sync;
  return candidate;
}

/* ------------------------------------------------------------------ words for what a sync did */

const SYNC_FIELD_LABELS: Record<string, string> = {
  name: "name",
  email: "e-mail",
  phone: "phone",
  notes: "notes",
  desired_titles: "job titles",
  years_experience: "years of experience",
  work_mode: "work arrangement",
  work_authorization: "work authorization",
  sponsorship_needed: "sponsorship",
  seniority: "seniority",
  preferred_states: "preferred states",
};

const fieldNames = (fields: string[]): string => fields.map((field) => SYNC_FIELD_LABELS[field] ?? field.replace(/_/g, " ")).join(", ");

/** What became of the resume, as a sentence. Empty when the API did not say. */
export function syncResumeSentence(resume: SyncResume | undefined): string {
  switch (resume) {
    case "imported":
      return "The resume was brought in from Salesforce.";
    case "unchanged":
      return "Salesforce has no resume file that the portal has not received already, so the current resume was kept.";
    case "none":
      return "Salesforce has no active resume file for this candidate.";
    case "kept_local":
      return "The resume uploaded in the portal is newer than the one in Salesforce, so it was kept.";
    case "refused_type":
      return "The resume in Salesforce is not a PDF or DOCX file, so it was not brought in.";
    case "refused_size":
      return "The resume in Salesforce is larger than the upload limit, so it was not brought in.";
    case "unreadable":
      return "The resume in Salesforce could not be read, so it was not brought in.";
    case "unavailable":
      return "Salesforce failed while the resume was read. Refresh the candidate from Salesforce later to fetch it.";
    default:
      return "";
  }
}

/** What a refresh changed and what it left alone, for the confirmation after it. */
export function syncSummary(sync: SalesforceSync): string {
  const parts = [
    sync.updated_fields.length > 0 ? `Updated from Salesforce: ${fieldNames(sync.updated_fields)}.` : "No details changed.",
    sync.kept_fields.length > 0 ? `Kept as changed in the portal: ${fieldNames(sync.kept_fields)}.` : "",
    syncResumeSentence(sync.resume),
  ];
  return parts.filter(Boolean).join(" ");
}

/* ------------------------------------------------------------------ calls */

export const salesforceKeys = {
  all: ["salesforce"] as const,
  status: ["salesforce", "status"] as const,
  search: (params: SalesforceSearchParams) => ["salesforce", "candidates", params] as const,
};

/**
 * Whether the portal is connected to Salesforce. An API that does not offer the endpoint, or
 * does not offer it to this person (403, 404), counts as "not configured": nothing is shown.
 */
export function useSalesforceStatus(enabled = true) {
  return useQuery({
    queryKey: salesforceKeys.status,
    queryFn: async ({ signal }) => {
      try {
        return await api.get<SalesforceStatus>("/api/salesforce/status", undefined, signal);
      } catch (error) {
        if (error instanceof ApiError && (error.isForbidden || error.isNotFound)) {
          return NOT_CONFIGURED;
        }
        throw error;
      }
    },
    enabled,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

/** People in Salesforce whose name, e-mail or phone contains the text. Asked only for two characters or more. */
export function useSalesforceSearch(params: SalesforceSearchParams, enabled = true) {
  return useQuery({
    queryKey: salesforceKeys.search(params),
    queryFn: ({ signal }) =>
      api.get<SalesforceSearchResult>("/api/salesforce/candidates", { q: params.q, status: params.status, limit: params.limit }, signal),
    enabled: enabled && params.q.length >= SALESFORCE_MIN_QUERY,
    // No rows of the previous search are kept on screen: a row is an action, and it must belong to what was typed.
    staleTime: 30_000,
    refetchOnWindowFocus: false,
    // "Salesforce cannot be reached" and "too fast" are answers: the person decides when to ask again.
    retry: false,
  });
}

/**
 * Bring a person from Salesforce into the portal: details, profile and the newest active resume file.
 * Someone who is already a candidate in the caller's scope is refreshed and returned. A resume
 * problem never stops the import: `sync.resume` says what became of the file.
 */
export function useImportSalesforceCandidate() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: SalesforceImport) => api.post<SyncedCandidate>("/api/candidates/import-salesforce", body),
    onSuccess: (synced) => {
      client.setQueryData(qk.candidate(synced.id), withoutSync(synced));
      return Promise.all([invalidateCandidates(client), client.invalidateQueries({ queryKey: ["salesforce", "candidates"] })]);
    },
  });
}

/** Read a candidate again from Salesforce. */
export function useRefreshSalesforceCandidate() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<SyncedCandidate>(`/api/candidates/${id}/salesforce-refresh`),
    onSuccess: (synced) => {
      client.setQueryData(qk.candidate(synced.id), withoutSync(synced));
      return invalidateCandidates(client);
    },
  });
}

/* ------------------------------------------------------------------ the candidate list and the Admin screens */

export const SOURCE_OPTIONS: Array<{ value: CandidateSource; label: string }> = [
  { value: "salesforce", label: "From Salesforce" },
  { value: "manual", label: "Added manually" },
];

/** The `source` filter of GET /api/candidates. Anything but the two known values is no filter. */
export function withSource(params: CandidateListParams, source: string): CandidateListParams {
  return source === "salesforce" || source === "manual" ? { ...params, source } : params;
}

export const teamLeadScopeOf = (settings: PortalSettings): TeamLeadScope => settings.salesforce_team_lead_scope;

/** The `salesforce` entry of GET /api/integrations. */
export const salesforceStatusOf = (integrations: Integrations): SalesforceStatus => integrations.salesforce ?? NOT_CONFIGURED;

export const TEAM_LEAD_SCOPES: Array<{ value: TeamLeadScope; label: string; text: string }> = [
  {
    value: "all",
    label: "Every candidate",
    text: "A Team Lead can find and bring in every candidate in Salesforce.",
  },
  {
    value: "own",
    label: "Only their own candidates",
    text: "A Team Lead can find, bring in and refresh only the candidates whose marketing team lead in Salesforce has the Team Lead's sign-in e-mail address.",
  },
];
