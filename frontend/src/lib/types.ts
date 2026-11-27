/**
 * Names for the API contract. Every shape here comes from `api-types.ts`, which is
 * generated from ../openapi.json (`npm run gen:api`). Nothing is typed by hand.
 */
import type { components, operations } from "./api-types";

type Schemas = components["schemas"];
type QueryOf<K extends keyof operations> = operations[K] extends { parameters: { query?: infer Q } }
  ? NonNullable<Q>
  : never;

export type Page<T> = { items: T[]; total: number; page: number; page_size: number };

export type Role = Schemas["MeOut"]["role"];
export type Me = Schemas["MeOut"];
export type UiSettings = Schemas["UiSettings"];
export type ExtraField = Schemas["ExtraField"];
export type ExtraFieldType = ExtraField["type"];
export type PasswordPolicy = Schemas["PasswordPolicy"];
export type Message = Schemas["Message"];
export type TeamRef = Schemas["TeamRef"];
export type UserRef = Schemas["UserRef"];

export type LoginIn = Schemas["LoginIn"];
export type ForgotPasswordIn = Schemas["ForgotPasswordIn"];
export type ResetPasswordIn = Schemas["ResetPasswordIn"];
export type ChangePasswordIn = Schemas["ChangePasswordIn"];

export type Invitation = Schemas["InvitationOut"];
export type InvitationCreated = Schemas["InvitationCreated"];
export type InvitationCreate = Schemas["InvitationCreate"];
export type InvitationPreview = Schemas["InvitationPreview"];
export type InvitationAccept = Schemas["InvitationAccept"];
export type InvitationStatus = Invitation["status"];

export type AssignmentStats = Schemas["AssignmentStats"];
export type Team = Schemas["TeamOut"];
export type TeamCreate = Schemas["TeamCreate"];
export type TeamUpdate = Schemas["TeamUpdate"];
export type User = Schemas["UserOut"];
export type UserUpdate = Schemas["UserUpdate"];
export type UserStatus = User["status"];
export type ResetLink = Schemas["ResetLinkOut"];
export type Profile = Schemas["ProfileOut"];
export type ProfileIn = Schemas["ProfileIn"];
export type Resume = Schemas["ResumeOut"];

export type CandidateRef = Schemas["CandidateRef"];
export type Candidate = Schemas["CandidateOut"];
export type CandidateCreate = Schemas["CandidateCreate"];
export type CandidateUpdate = Schemas["CandidateUpdate"];

export type JobSummary = Schemas["JobSummary"];
export type JobDetail = Schemas["JobDetail"];
export type JobListItem = Schemas["JobListItem"];
export type JobManagerView = Schemas["JobManagerView"];
export type JobCreate = Schemas["JobCreate"];
export type JobUpdate = Schemas["JobUpdate"];
export type JobAssignee = Schemas["JobAssignee"];
export type JobImportDraft = Schemas["JobImportDraft"];
export type CsvImportResult = Schemas["CsvImportResult"];
export type QueueItem = Schemas["QueueItemOut"];
export type QueueDelegate = Schemas["QueueDelegate"];

export type SearchRun = Schemas["SearchRunOut"];
export type SearchTarget = Schemas["SearchTargetOut"];
export type SearchCreate = Schemas["SearchCreate"];
export type SearchCreated = Schemas["SearchCreated"];
export type SearchSkipped = Schemas["SearchSkipped"];
export type SearchFilters = Schemas["SearchFilters"];
export type SearchOptions = Schemas["SearchOptions"];
export type SearchResult = Schemas["SearchResultOut"];
export type SearchStatus = SearchRun["status"];
export type SearchMode = SearchRun["mode"];
export type MatchExplanation = Schemas["MatchExplanation"];
export type Preset = Schemas["PresetOut"];
export type PresetIn = Schemas["PresetIn"];

export type Assignment = Schemas["AssignmentOut"];
export type AssignmentDetail = Schemas["AssignmentDetail"];
export type AssignmentSections = Schemas["AssignmentSections"];
export type AssignmentStatus = Assignment["status"];
export type AssignmentEvent = Schemas["EventOut"];
export type AssignmentNote = Schemas["NoteOut"];
export type NoteIn = Schemas["NoteIn"];
export type StatusUpdate = Schemas["StatusUpdate"];
export type BulkAssign = Schemas["BulkAssign"];
export type BulkAssignResult = Schemas["BulkAssignResult"];
export type ActivityItem = Schemas["ActivityItem"];

export type SeriesPoint = Schemas["SeriesPoint"];
export type NameValue = Schemas["NameValue"];
export type MemberRow = Schemas["MemberRow"];
export type CandidateRow = Schemas["CandidateRow"];
export type TeamRow = Schemas["TeamRow"];
export type SearchUsagePoint = Schemas["SearchUsagePoint"];
export type MemberAnalytics = Schemas["MemberAnalytics"];
export type CandidateAnalytics = Schemas["CandidateAnalytics"];
export type TeamAnalytics = Schemas["TeamAnalytics"];
export type AdminAnalytics = Schemas["AdminAnalytics"];

export type Notification = Schemas["NotificationOut"];
export type AuditEntry = Schemas["AuditOut"];
export type PortalSettings = Schemas["SettingsOut"];
export type PortalSettingsUpdate = Schemas["SettingsUpdate"];
export type Integrations = Schemas["IntegrationsOut"];
export type Health = Schemas["HealthOut"];

/* Query-string parameters, taken from the operation definitions. */
export type AssignmentListParams = QueryOf<"list_assignments_api_assignments_get">;
export type AssignmentSectionParams = QueryOf<"assignment_sections_api_assignments_sections_get">;
export type AssignmentExportParams = QueryOf<"export_assignments_api_assignments_export_csv_get">;
export type CandidateListParams = QueryOf<"list_candidates_api_candidates_get">;
export type UserListParams = QueryOf<"list_users_api_users_get">;
export type JobListParams = QueryOf<"list_jobs_api_jobs_get">;
export type SearchListParams = QueryOf<"list_searches_api_searches_get">;
export type SearchResultParams = QueryOf<"search_results_api_searches__search_id__results_get">;
export type InvitationListParams = QueryOf<"list_invitations_api_invitations_get">;
export type AuditListParams = QueryOf<"audit_logs_api_audit_logs_get">;
export type AdminAnalyticsParams = QueryOf<"admin_analytics_api_analytics_admin_get">;
export type NotificationListParams = QueryOf<"list_notifications_api_notifications_get">;
export type QueueListParams = QueryOf<"team_queue_api_teams__team_id__queue_get">;
export type DateRange = { date_from?: string | null; date_to?: string | null };
