import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { api, apiFetch, ApiError } from "./api";
import type { components } from "./api-types";

type Schemas = components["schemas"];
export type TitleSuggestions = Schemas["TitleSuggestionsOut"];
export type TitleSuggestion = Schemas["TitleSuggestion"];
export type ResumeTitles = Schemas["ResumeTitlesOut"];
export type CandidateTitle = Schemas["CandidateTitle"];

/** The fewest typed characters the API answers (shorter text matches too much to help). */
export const MIN_TITLE_QUERY = 2;
/** As many candidates as the API takes in one request. */
const MAX_CANDIDATES = 50;
/** How often the form asks again while a model is still working on related titles. */
const PENDING_RETRIES = 5;

const ids = (candidateIds: readonly string[]) => [...candidateIds].sort().slice(0, MAX_CANDIDATES);

/**
 * Titles matching what was typed so far (GET /api/job-titles/suggestions). `typed` should already be
 * debounced. A request for text that has been typed over is cancelled; an answer already received for
 * the same text is reused for a minute. A failure (also "too many requests") simply means no
 * suggestions: typing a title never depends on them.
 */
export function useTitleSuggestions(typed: string, candidateIds: readonly string[]) {
  const q = typed.trim().replace(/\s+/g, " ").slice(0, 80);
  const candidates = ids(candidateIds);
  return useQuery({
    queryKey: ["job-titles", "suggestions", q.toLowerCase(), candidates],
    queryFn: ({ signal }) =>
      api.get<TitleSuggestions>("/api/job-titles/suggestions", { q, candidate_ids: candidates.join(",") || undefined }, signal),
    enabled: q.length >= MIN_TITLE_QUERY,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
    gcTime: 5 * 60_000,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

/**
 * Titles to offer for the selected candidates (POST /api/job-titles/from-resumes): saved in the
 * profile, found in the resume, and related titles from a model when one answers. While the answer
 * says a model is still working ("pending") it is asked again a few times, a few seconds apart.
 */
export function useResumeTitles(candidateIds: readonly string[]) {
  const candidates = ids(candidateIds);
  return useQuery({
    queryKey: ["job-titles", "from-resumes", candidates],
    queryFn: ({ signal }) =>
      apiFetch<ResumeTitles>("/api/job-titles/from-resumes", { method: "POST", body: { candidate_ids: candidates }, signal }),
    enabled: candidates.length > 0,
    placeholderData: keepPreviousData,
    staleTime: 5 * 60_000,
    retry: (failures, error) => !(error instanceof ApiError) && failures < 1,
    refetchOnWindowFocus: false,
    refetchInterval: (query) =>
      query.state.data?.model === "pending" && query.state.dataUpdateCount <= PENDING_RETRIES ? 3000 : false,
  });
}
