"use client";

/**
 * Every API call the UI makes, as TanStack Query hooks. Lists poll at the
 * intervals the product asks for (dashboards and job lists about every 25 s,
 * notifications every 30 s), and every mutation invalidates what it changed so
 * new work shows up without signing in again.
 */
import {
  keepPreviousData,
  queryOptions,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
  type UseQueryOptions,
} from "@tanstack/react-query";

import { api, apiFetch, type QueryParams } from "./api";
import type {
  ActivityItem,
  AdminAnalytics,
  AdminAnalyticsParams,
  Assignment,
  AssignmentDetail,
  AssignmentListParams,
  AssignmentNote,
  AuditEntry,
  AuditListParams,
  BulkAssign,
  BulkAssignResult,
  Candidate,
  CandidateAnalytics,
  CandidateCreate,
  CandidateListParams,
  CandidateUpdate,
  ChangePasswordIn,
  CsvImportResult,
  DateRange,
  ForgotPasswordIn,
  Health,
  Integrations,
  Invitation,
  InvitationAccept,
  InvitationCreate,
  InvitationCreated,
  InvitationListParams,
  InvitationPreview,
  JobCreate,
  JobImportDraft,
  JobListItem,
  JobListParams,
  JobManagerView,
  JobUpdate,
  LoginIn,
  Me,
  MemberAnalytics,
  Message,
  NoteIn,
  Notification,
  Page,
  PasswordPolicy,
  PortalSettings,
  PortalSettingsUpdate,
  Preset,
  PresetIn,
  Profile,
  ProfileIn,
  QueueDelegate,
  QueueItem,
  ResetLink,
  ResetPasswordIn,
  Resume,
  SearchCreate,
  SearchCreated,
  SearchListParams,
  SearchOptions,
  SearchResult,
  SearchResultParams,
  SearchRun,
  StatusUpdate,
  Team,
  TeamAnalytics,
  TeamCreate,
  TeamUpdate,
  User,
  UserListParams,
  UserUpdate,
} from "./types";

/** Dashboards and job lists stay fresh on their own. */
export const LIVE_INTERVAL = 25_000;
export const NOTIFICATION_INTERVAL = 30_000;
export const SEARCH_POLL_INTERVAL = 1_500;

type Options<T> = Omit<UseQueryOptions<T>, "queryKey" | "queryFn">;

const asQuery = (params: object | undefined): QueryParams => (params ?? {}) as QueryParams;

export const qk = {
  me: ["me"] as const,
  passwordPolicy: ["password-policy"] as const,
  notifications: ["notifications"] as const,
  notificationsUnread: ["notifications", "unread"] as const,
  notificationsList: (pageSize: number) => ["notifications", "list", pageSize] as const,
  teams: ["teams"] as const,
  teamList: (includeInactive: boolean) => ["teams", "list", includeInactive] as const,
  team: (id: string) => ["teams", "detail", id] as const,
  teamMembers: (id: string) => ["teams", "members", id] as const,
  teamAnalytics: (id: string, range: DateRange) => ["analytics", "team", id, range] as const,
  teamQueue: (id: string, params: object) => ["teams", "queue", id, params] as const,
  users: ["users"] as const,
  userList: (params: object) => ["users", "list", params] as const,
  user: (id: string) => ["users", "detail", id] as const,
  userDashboard: (id: string, range: DateRange) => ["analytics", "member", id, range] as const,
  userActivity: (id: string, params: object) => ["activity", id, params] as const,
  candidates: ["candidates"] as const,
  candidateList: (params: object) => ["candidates", "list", params] as const,
  candidate: (id: string) => ["candidates", "detail", id] as const,
  candidateProfile: (id: string) => ["candidates", "profile", id] as const,
  candidateResume: (id: string) => ["candidates", "resume", id] as const,
  candidateAnalytics: (id: string, range: DateRange) => ["analytics", "candidate", id, range] as const,
  candidateActivity: (id: string, params: object) => ["activity", "candidate", id, params] as const,
  jobs: ["jobs"] as const,
  jobList: (params: object) => ["jobs", "list", params] as const,
  job: (id: string) => ["jobs", "detail", id] as const,
  searches: ["searches"] as const,
  searchOptions: ["searches", "options"] as const,
  searchList: (params: object) => ["searches", "list", params] as const,
  searchActiveMine: ["searches", "active-mine"] as const,
  search: (id: string) => ["searches", "detail", id] as const,
  searchResults: (id: string, params: object) => ["searches", "results", id, params] as const,
  presets: ["search-presets"] as const,
  assignments: ["assignments"] as const,
  assignmentList: (params: object) => ["assignments", "list", params] as const,
  assignment: (id: string) => ["assignments", "detail", id] as const,
  adminAnalytics: (params: object) => ["analytics", "admin", params] as const,
  invitations: ["invitations"] as const,
  invitationList: (params: object) => ["invitations", "list", params] as const,
  invitationPreview: (token: string) => ["invitations", "validate", token] as const,
  audit: (params: object) => ["audit", params] as const,
  settings: ["settings"] as const,
  integrations: ["integrations"] as const,
  health: ["health"] as const,
};

/**
 * Assignments feed almost every number in the product (dashboards, team, member
 * and candidate rows, job counts, search results). After anything that creates or
 * changes one, all of those are refreshed together so they always agree.
 */
export function invalidateWork(client: QueryClient): Promise<unknown> {
  return Promise.all(
    [qk.assignments, ["analytics"], ["activity"], qk.candidates, qk.users, qk.teams, qk.jobs, qk.searches, qk.notifications].map(
      (queryKey) => client.invalidateQueries({ queryKey }),
    ),
  );
}

/* ------------------------------------------------------------------ session */

/** Shared by every reader of the signed-in user (see useMe in the shell). */
export const meQueryOptions = queryOptions({
  queryKey: qk.me,
  queryFn: ({ signal }) => api.get<Me>("/api/auth/me", undefined, signal),
  staleTime: 30_000,
  refetchInterval: 60_000,
});

export function useLogin() {
  return useMutation({
    mutationFn: (body: LoginIn) => api.post<Me>("/api/auth/login", body, { redirectOn401: false }),
  });
}

export function useLogout() {
  return useMutation({ mutationFn: () => api.post<Message>("/api/auth/logout") });
}

export function useForgotPassword() {
  return useMutation({
    mutationFn: (body: ForgotPasswordIn) =>
      api.post<Message>("/api/auth/forgot-password", body, { redirectOn401: false }),
  });
}

export function useResetPassword() {
  return useMutation({
    mutationFn: (body: ResetPasswordIn) =>
      api.post<Message>("/api/auth/reset-password", body, { redirectOn401: false }),
  });
}

/**
 * The rules a new password has to meet, for the reset screen: nobody is signed in there, so they
 * cannot come from the signed-in user's settings. They are part of the server's configuration and
 * do not change while a page is open.
 */
export function usePasswordPolicy(enabled = true) {
  return useQuery({
    queryKey: qk.passwordPolicy,
    queryFn: ({ signal }) => apiPublicGet<PasswordPolicy>("/api/auth/password-policy", {}, signal),
    enabled,
    retry: false,
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  });
}

export function useChangePassword() {
  return useMutation({
    mutationFn: (body: ChangePasswordIn) => api.post<Message>("/api/auth/change-password", body),
  });
}

/* ------------------------------------------------------------------ notifications */

export function useUnreadCount(enabled = true) {
  return useQuery({
    queryKey: qk.notificationsUnread,
    queryFn: async ({ signal }) => {
      const page = await api.get<Page<Notification>>(
        "/api/notifications",
        { unread_only: true, page_size: 1 },
        signal,
      );
      return page.total;
    },
    refetchInterval: NOTIFICATION_INTERVAL,
    enabled,
  });
}

export function useNotifications(pageSize: number, enabled: boolean) {
  return useQuery({
    queryKey: qk.notificationsList(pageSize),
    queryFn: ({ signal }) =>
      api.get<Page<Notification>>("/api/notifications", { page: 1, page_size: pageSize }, signal),
    refetchInterval: NOTIFICATION_INTERVAL,
    placeholderData: keepPreviousData,
    enabled,
  });
}

export function useReadNotification() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<Notification>(`/api/notifications/${id}/read`),
    onSuccess: () => client.invalidateQueries({ queryKey: qk.notifications }),
  });
}

export function useReadAllNotifications() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<Message>("/api/notifications/read-all"),
    onSuccess: () => client.invalidateQueries({ queryKey: qk.notifications }),
  });
}

/* ------------------------------------------------------------------ invitations */

export function useInvitations(params: InvitationListParams) {
  return useQuery({
    queryKey: qk.invitationList(params),
    queryFn: ({ signal }) => api.get<Page<Invitation>>("/api/invitations", asQuery(params), signal),
    placeholderData: keepPreviousData,
  });
}

export function useCreateInvitation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: InvitationCreate) => api.post<InvitationCreated>("/api/invitations", body),
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: qk.invitations }),
        client.invalidateQueries({ queryKey: qk.users }),
        client.invalidateQueries({ queryKey: qk.teams }),
        client.invalidateQueries({ queryKey: ["analytics"] }),
      ]),
  });
}

export function useResendInvitation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<InvitationCreated>(`/api/invitations/${id}/resend`),
    onSuccess: () => client.invalidateQueries({ queryKey: qk.invitations }),
  });
}

export function useRevokeInvitation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<Invitation>(`/api/invitations/${id}/revoke`),
    onSuccess: () => client.invalidateQueries({ queryKey: qk.invitations }),
  });
}

export function useInvitationPreview(token: string) {
  return useQuery({
    queryKey: qk.invitationPreview(token),
    queryFn: ({ signal }) =>
      apiPublicGet<InvitationPreview>("/api/invitations/validate", { token }, signal),
    enabled: token.length >= 20,
    retry: false,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

export function useAcceptInvitation() {
  return useMutation({
    mutationFn: (body: InvitationAccept) =>
      api.post<Message>("/api/invitations/accept", body, { redirectOn401: false }),
  });
}

/** For screens that are used signed out: a 401 or 404 here is an answer, not a lost session. */
function apiPublicGet<T>(path: string, query: QueryParams, signal?: AbortSignal) {
  return apiFetch<T>(path, { query, signal, redirectOn401: false });
}

/* ------------------------------------------------------------------ teams */

export function useTeams(includeInactive = true, options?: Options<Team[]>) {
  return useQuery({
    queryKey: qk.teamList(includeInactive),
    queryFn: ({ signal }) => api.get<Team[]>("/api/teams", { include_inactive: includeInactive }, signal),
    ...options,
  });
}

export function useTeam(id: string | undefined, options?: Options<Team>) {
  return useQuery({
    queryKey: qk.team(id ?? ""),
    queryFn: ({ signal }) => api.get<Team>(`/api/teams/${id}`, undefined, signal),
    enabled: Boolean(id),
    ...options,
  });
}

export function useTeamMembers(id: string | undefined) {
  return useQuery({
    queryKey: qk.teamMembers(id ?? ""),
    queryFn: ({ signal }) => api.get<User[]>(`/api/teams/${id}/members`, undefined, signal),
    enabled: Boolean(id),
  });
}

export function useTeamAnalytics(id: string | undefined, range: DateRange = {}) {
  return useQuery({
    queryKey: qk.teamAnalytics(id ?? "", range),
    queryFn: ({ signal }) => api.get<TeamAnalytics>(`/api/teams/${id}/analytics`, asQuery(range), signal),
    enabled: Boolean(id),
    refetchInterval: LIVE_INTERVAL,
    placeholderData: keepPreviousData,
  });
}

export function useTeamQueue(
  id: string | undefined,
  params: { status?: string; page?: number; page_size?: number } = {},
) {
  return useQuery({
    queryKey: qk.teamQueue(id ?? "", params),
    queryFn: ({ signal }) => api.get<Page<QueueItem>>(`/api/teams/${id}/queue`, asQuery(params), signal),
    enabled: Boolean(id),
    refetchInterval: LIVE_INTERVAL,
    placeholderData: keepPreviousData,
  });
}

export function useCreateTeam() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: TeamCreate) => api.post<Team>("/api/teams", body),
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: qk.teams }),
        client.invalidateQueries({ queryKey: ["analytics"] }),
      ]),
  });
}

export function useUpdateTeam() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: Partial<TeamUpdate> }) =>
      api.patch<Team>(`/api/teams/${id}`, body),
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: qk.teams }),
        client.invalidateQueries({ queryKey: qk.users }),
        client.invalidateQueries({ queryKey: ["analytics"] }),
      ]),
  });
}

export function useDelegateJobs() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ teamId, body }: { teamId: string; body: QueueDelegate }) =>
      api.post<QueueItem[]>(`/api/teams/${teamId}/queue`, body),
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: qk.teams }),
        client.invalidateQueries({ queryKey: qk.jobs }),
        client.invalidateQueries({ queryKey: ["analytics"] }),
      ]),
  });
}

export function useUpdateQueueItem() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, status }: { id: string; status: "pending" | "dismissed" }) =>
      api.patch<QueueItem>(`/api/team-queue/${id}`, { status }),
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: qk.teams }),
        client.invalidateQueries({ queryKey: qk.jobs }),
        client.invalidateQueries({ queryKey: ["analytics"] }),
      ]),
  });
}

/* ------------------------------------------------------------------ users (the employees: Team Leads and members) */

export function useUsers(params: UserListParams, options?: Options<Page<User>>) {
  return useQuery({
    queryKey: qk.userList(params),
    queryFn: ({ signal }) => api.get<Page<User>>("/api/users", asQuery(params), signal),
    placeholderData: keepPreviousData,
    refetchInterval: LIVE_INTERVAL,
    ...options,
  });
}

export function useUser(id: string | undefined) {
  return useQuery({
    queryKey: qk.user(id ?? ""),
    queryFn: ({ signal }) => api.get<User>(`/api/users/${id}`, undefined, signal),
    enabled: Boolean(id),
  });
}

export function useUpdateUser() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: Partial<UserUpdate> }) =>
      api.patch<User>(`/api/users/${id}`, body),
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: qk.users }),
        client.invalidateQueries({ queryKey: qk.teams }),
        client.invalidateQueries({ queryKey: qk.me }),
        client.invalidateQueries({ queryKey: qk.assignments }),
        // A member who is deactivated or moved to another team stops being responsible for their candidates.
        client.invalidateQueries({ queryKey: qk.candidates }),
        client.invalidateQueries({ queryKey: ["analytics"] }),
      ]),
  });
}

export function useCreateResetLink() {
  return useMutation({
    mutationFn: (id: string) => api.post<ResetLink>(`/api/users/${id}/reset-link`),
  });
}

export function useMemberDashboard(id: string | undefined, range: DateRange = {}) {
  return useQuery({
    queryKey: qk.userDashboard(id ?? "", range),
    queryFn: ({ signal }) => api.get<MemberAnalytics>(`/api/users/${id}/dashboard`, asQuery(range), signal),
    enabled: Boolean(id),
    refetchInterval: LIVE_INTERVAL,
    placeholderData: keepPreviousData,
  });
}

export function useMemberActivity(id: string | undefined, params: { page: number; page_size: number }) {
  return useQuery({
    queryKey: qk.userActivity(id ?? "", params),
    queryFn: ({ signal }) => api.get<Page<ActivityItem>>(`/api/users/${id}/activity`, params, signal),
    enabled: Boolean(id),
    refetchInterval: LIVE_INTERVAL,
    placeholderData: keepPreviousData,
  });
}

/* ------------------------------------------------------------------ candidates: details, profile, resume */

/**
 * The people the teams find jobs for. An Admin receives every candidate, a Team Lead their team's,
 * a member the candidates assigned to them today (GET /api/candidates decides).
 */
export function useCandidates(params: CandidateListParams, options?: Options<Page<Candidate>> & { live?: boolean }) {
  const { live = true, ...rest } = options ?? {};
  return useQuery({
    queryKey: qk.candidateList(params),
    queryFn: ({ signal }) => api.get<Page<Candidate>>("/api/candidates", asQuery(params), signal),
    placeholderData: keepPreviousData,
    refetchInterval: live ? LIVE_INTERVAL : false,
    ...rest,
  });
}

export function useCandidate(id: string | undefined) {
  return useQuery({
    queryKey: qk.candidate(id ?? ""),
    queryFn: ({ signal }) => api.get<Candidate>(`/api/candidates/${id}`, undefined, signal),
    enabled: Boolean(id),
    refetchInterval: LIVE_INTERVAL,
  });
}

/** Everything that shows a candidate, their member or their counts. */
function invalidateCandidates(client: QueryClient): Promise<unknown> {
  return Promise.all(
    [qk.candidates, qk.users, qk.teams, qk.assignments, ["analytics"], ["activity"], qk.notifications].map((queryKey) =>
      client.invalidateQueries({ queryKey }),
    ),
  );
}

export function useCreateCandidate() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: CandidateCreate) => api.post<Candidate>("/api/candidates", body),
    onSuccess: () => invalidateCandidates(client),
  });
}

/** Details, the responsible member (`assigned_member_id`, null to take the candidate back) and `is_active`. */
export function useUpdateCandidate() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: Partial<CandidateUpdate> }) =>
      api.patch<Candidate>(`/api/candidates/${id}`, body),
    onSuccess: (candidate) => {
      client.setQueryData(qk.candidate(candidate.id), candidate);
      return invalidateCandidates(client);
    },
  });
}

export function useCandidateProfile(id: string | undefined) {
  return useQuery({
    queryKey: qk.candidateProfile(id ?? ""),
    queryFn: ({ signal }) => api.get<Profile>(`/api/candidates/${id}/profile`, undefined, signal),
    enabled: Boolean(id),
  });
}

export function useUpdateCandidateProfile(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<ProfileIn>) => api.patch<Profile>(`/api/candidates/${id}/profile`, body),
    onSuccess: (profile) => {
      client.setQueryData(qk.candidateProfile(id), profile);
      return Promise.all([
        client.invalidateQueries({ queryKey: qk.candidate(id) }),
        client.invalidateQueries({ queryKey: ["candidates", "list"] }),
      ]);
    },
  });
}

/** The candidate's current resume with its text, or null when there is none. */
export function useCandidateResume(id: string | undefined) {
  return useQuery({
    queryKey: qk.candidateResume(id ?? ""),
    queryFn: ({ signal }) => api.get<Resume | null>(`/api/candidates/${id}/resume`, undefined, signal),
    enabled: Boolean(id),
  });
}

/** Where the browser downloads the uploaded file from (same origin, with the session cookie). */
export const candidateResumeDownloadPath = (id: string) => `/api/candidates/${id}/resume/download`;

function resumeChanged(client: QueryClient, id: string): Promise<unknown> {
  return Promise.all([
    client.invalidateQueries({ queryKey: qk.candidateProfile(id) }),
    client.invalidateQueries({ queryKey: qk.candidate(id) }),
    client.invalidateQueries({ queryKey: ["candidates", "list"] }),
    client.invalidateQueries({ queryKey: ["analytics"] }),
  ]);
}

/** Upload a resume for a candidate; it replaces the current one. The candidate is named per call. */
export function useUploadCandidateResume() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, file }: { id: string; file: File }) => {
      const form = new FormData();
      form.append("file", file);
      return api.upload<Resume>(`/api/candidates/${id}/resume`, form);
    },
    onSuccess: (resume, { id }) => {
      client.setQueryData(qk.candidateResume(id), resume);
      return resumeChanged(client, id);
    },
  });
}

export function useDeleteCandidateResume(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.delete<Message>(`/api/candidates/${id}/resume`),
    onSuccess: () => {
      client.setQueryData(qk.candidateResume(id), null);
      return resumeChanged(client, id);
    },
  });
}

export function useCandidateAnalytics(id: string | undefined, range: DateRange = {}) {
  return useQuery({
    queryKey: qk.candidateAnalytics(id ?? "", range),
    queryFn: ({ signal }) =>
      api.get<CandidateAnalytics>(`/api/candidates/${id}/analytics`, asQuery(range), signal),
    enabled: Boolean(id),
    refetchInterval: LIVE_INTERVAL,
    placeholderData: keepPreviousData,
  });
}

export function useCandidateActivity(id: string | undefined, params: { page: number; page_size: number }) {
  return useQuery({
    queryKey: qk.candidateActivity(id ?? "", params),
    queryFn: ({ signal }) => api.get<Page<ActivityItem>>(`/api/candidates/${id}/activity`, params, signal),
    enabled: Boolean(id),
    refetchInterval: LIVE_INTERVAL,
    placeholderData: keepPreviousData,
  });
}

/* ------------------------------------------------------------------ jobs */

export function useJobs(params: JobListParams, options?: Options<Page<JobListItem>>) {
  return useQuery({
    queryKey: qk.jobList(params),
    queryFn: ({ signal }) => api.get<Page<JobListItem>>("/api/jobs", asQuery(params), signal),
    placeholderData: keepPreviousData,
    ...options,
  });
}

export function useJob(id: string | undefined) {
  return useQuery({
    queryKey: qk.job(id ?? ""),
    queryFn: ({ signal }) => api.get<JobManagerView>(`/api/jobs/${id}`, undefined, signal),
    enabled: Boolean(id),
  });
}

export function useCreateJob() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: JobCreate) => api.post<JobManagerView>("/api/jobs", body),
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: qk.jobs }),
        client.invalidateQueries({ queryKey: ["analytics"] }),
      ]),
  });
}

export function useUpdateJob(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<JobUpdate>) => api.patch<JobManagerView>(`/api/jobs/${id}`, body),
    onSuccess: (job) => {
      client.setQueryData(qk.job(id), job);
      return Promise.all([
        client.invalidateQueries({ queryKey: qk.jobs }),
        client.invalidateQueries({ queryKey: qk.assignments }),
      ]);
    },
  });
}

export function useVerifyJob() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<JobManagerView>(`/api/jobs/${id}/verify`),
    onSuccess: (job) => {
      client.setQueryData(qk.job(job.id), job);
      return client.invalidateQueries({ queryKey: qk.jobs });
    },
  });
}

export function useImportJobUrl() {
  return useMutation({
    mutationFn: (url: string) => api.post<JobImportDraft>("/api/jobs/import-url", { url }),
  });
}

export function useImportJobsCsv() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api.upload<CsvImportResult>("/api/jobs/import-csv", form);
    },
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: qk.jobs }),
        client.invalidateQueries({ queryKey: ["analytics"] }),
      ]),
  });
}

/* ------------------------------------------------------------------ searches */

export function useSearchOptions() {
  return useQuery({
    queryKey: qk.searchOptions,
    queryFn: ({ signal }) => api.get<SearchOptions>("/api/searches/options", undefined, signal),
    staleTime: 60_000,
  });
}

export function useSearches(params: SearchListParams, options?: Options<Page<SearchRun>>) {
  return useQuery({
    queryKey: qk.searchList(params),
    queryFn: ({ signal }) => api.get<Page<SearchRun>>("/api/searches", asQuery(params), signal),
    placeholderData: keepPreviousData,
    refetchInterval: LIVE_INTERVAL,
    ...options,
  });
}

/** Searches this person started that are still queued or running. Used to restore the progress indicator. */
export function useMyActiveSearches(enabled: boolean) {
  return useQuery({
    queryKey: qk.searchActiveMine,
    queryFn: ({ signal }) =>
      api.get<Page<SearchRun>>("/api/searches", { status: "active", mine: true, page_size: 10 }, signal),
    enabled,
    staleTime: 0,
  });
}

export const isSearchActive = (status: string | undefined) => status === "queued" || status === "running";

/** One search run. Polls about every 1.5 s for as long as the run is queued or running. */
export function useSearch(id: string | null | undefined) {
  return useQuery({
    queryKey: qk.search(id ?? ""),
    queryFn: ({ signal }) => api.get<SearchRun>(`/api/searches/${id}`, undefined, signal),
    enabled: Boolean(id),
    refetchInterval: (query) => (isSearchActive(query.state.data?.status) ? SEARCH_POLL_INTERVAL : false),
    refetchIntervalInBackground: true,
    staleTime: 0,
  });
}

/** Results of a run. While the run is still going (`live`), the list refreshes as candidates finish. */
export function useSearchResults(id: string | undefined, params: SearchResultParams, live = false) {
  return useQuery({
    queryKey: qk.searchResults(id ?? "", params),
    queryFn: ({ signal }) =>
      api.get<Page<SearchResult>>(`/api/searches/${id}/results`, asQuery(params), signal),
    enabled: Boolean(id),
    placeholderData: keepPreviousData,
    refetchInterval: live ? 3_000 : false,
  });
}

export function useCreateSearch() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: SearchCreate) => api.post<SearchCreated>("/api/searches", body),
    onSuccess: (created) => {
      client.setQueryData(qk.search(created.search.id), created.search);
      return client.invalidateQueries({ queryKey: qk.searches });
    },
  });
}

export function useCancelSearch() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<SearchRun>(`/api/searches/${id}/cancel`),
    onSuccess: (run) => {
      client.setQueryData(qk.search(run.id), run);
      return client.invalidateQueries({ queryKey: qk.searches });
    },
  });
}

export function usePresets() {
  return useQuery({
    queryKey: qk.presets,
    queryFn: ({ signal }) => api.get<Preset[]>("/api/search-presets", undefined, signal),
  });
}

export function useCreatePreset() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: PresetIn) => api.post<Preset>("/api/search-presets", body),
    onSuccess: () => client.invalidateQueries({ queryKey: qk.presets }),
  });
}

export function useDeletePreset() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<Message>(`/api/search-presets/${id}`),
    onSuccess: () => client.invalidateQueries({ queryKey: qk.presets }),
  });
}

/* ------------------------------------------------------------------ assignments */

export function useAssignments(
  params: AssignmentListParams,
  options?: Options<Page<Assignment>> & { live?: boolean },
) {
  const { live = true, ...rest } = options ?? {};
  return useQuery({
    queryKey: qk.assignmentList(params),
    queryFn: ({ signal }) => api.get<Page<Assignment>>("/api/assignments", asQuery(params), signal),
    placeholderData: keepPreviousData,
    refetchInterval: live ? LIVE_INTERVAL : false,
    ...rest,
  });
}

export function useAssignment(id: string | null | undefined) {
  return useQuery({
    queryKey: qk.assignment(id ?? ""),
    // No abort signal, on purpose. For a member this request records "viewed" on the server. A request
    // that is cancelled and sent again (React does that to every effect in development) reaches the API
    // twice at the same moment, and the view is then written to the history twice.
    queryFn: () => api.get<AssignmentDetail>(`/api/assignments/${id}`),
    enabled: Boolean(id),
  });
}

export function useUpdateStatus(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: StatusUpdate) => api.patch<AssignmentDetail>(`/api/assignments/${id}/status`, body),
    onSuccess: (detail) => {
      client.setQueryData(qk.assignment(id), detail);
      return invalidateWork(client);
    },
  });
}

export function useAddNote(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: NoteIn) => api.post<AssignmentNote>(`/api/assignments/${id}/notes`, body),
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: qk.assignment(id) }),
        client.invalidateQueries({ queryKey: qk.assignments }),
        client.invalidateQueries({ queryKey: ["activity"] }),
      ]),
  });
}

export function useBulkAssign() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<BulkAssign>) => api.post<BulkAssignResult>("/api/assignments/bulk", body),
    onSuccess: () => invalidateWork(client),
  });
}

export function useWithdrawAssignment() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<Message>(`/api/assignments/${id}`),
    onSuccess: () => invalidateWork(client),
  });
}

/* ------------------------------------------------------------------ analytics, audit, settings */

export function useAdminAnalytics(params: AdminAnalyticsParams, enabled = true) {
  return useQuery({
    queryKey: qk.adminAnalytics(params),
    queryFn: ({ signal }) => api.get<AdminAnalytics>("/api/analytics/admin", asQuery(params), signal),
    refetchInterval: LIVE_INTERVAL,
    placeholderData: keepPreviousData,
    enabled,
  });
}

export function useAuditLogs(params: AuditListParams) {
  return useQuery({
    queryKey: qk.audit(params),
    queryFn: ({ signal }) => api.get<Page<AuditEntry>>("/api/audit-logs", asQuery(params), signal),
    placeholderData: keepPreviousData,
  });
}

export function usePortalSettings() {
  return useQuery({
    queryKey: qk.settings,
    queryFn: ({ signal }) => api.get<PortalSettings>("/api/settings", undefined, signal),
  });
}

export function useUpdatePortalSettings() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<PortalSettingsUpdate>) => api.patch<PortalSettings>("/api/settings", body),
    onSuccess: (settings) => {
      client.setQueryData(qk.settings, settings);
      return Promise.all([
        client.invalidateQueries({ queryKey: qk.me }),
        client.invalidateQueries({ queryKey: qk.searchOptions }),
        client.invalidateQueries({ queryKey: qk.integrations }),
      ]);
    },
  });
}

export function useIntegrations() {
  return useQuery({
    queryKey: qk.integrations,
    queryFn: ({ signal }) => api.get<Integrations>("/api/integrations", undefined, signal),
    refetchInterval: 30_000,
  });
}

export function useHealth() {
  return useQuery({
    queryKey: qk.health,
    queryFn: ({ signal }) => api.get<Health>("/api/health", undefined, signal),
    refetchInterval: 30_000,
  });
}
