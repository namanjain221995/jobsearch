# Candidate model: API contract for the web app

The machine-readable contract is `openapi.json` in the repository root (re-exported with
`.venv/bin/python -m app.portal.export_openapi > openapi.json` after every schema or route change).
This page lists what changed and what the UI has to know. Schema names are the ones in `openapi.json`.

## The model in one paragraph

ADMIN, TEAM_LEAD and MEMBER are accounts of employees. A MEMBER is a recruiter and has no profile and no resume.
A **candidate** is the person who needs a job: a record, never an account. A candidate belongs to one team, has
details, a job-search profile and a resume, and at most one responsible member (`assigned_member`, may be null =
unassigned). Searches run for candidates, jobs are assigned to candidates, and the responsible member works on
those jobs. When a candidate is handed to another member, all jobs and history go along: the old member loses
access at once, the new one gains it.

## Status codes for things the caller may not see

- `/api/candidates/{id}...`: **404** for every role when the candidate does not exist or is outside the caller's
  scope (another team for a Team Lead; not assigned to them for a Member). A Member who tries to change a
  candidate they can see gets **403**. Creating a candidate as a Member: **403**.
- Everywhere else nothing changed: a Team Lead or Member gets 403 for foreign assignments, searches, teams,
  users; an Admin gets 404 for ids that do not exist. Candidate ids passed in a body (`candidate_ids`,
  `candidate_id`) that are outside a Team Lead's team: 403.

## New endpoints (`/api/candidates`)

| Method and path | Who | Request | Response |
| --- | --- | --- | --- |
| `GET /api/candidates` | Admin: all. Team Lead: own team. Member: the candidates assigned to them | query, see below | `Page[CandidateOut]` |
| `POST /api/candidates` | Admin, Team Lead | `CandidateCreate` | 201 `CandidateOut` |
| `GET /api/candidates/{id}` | whoever may see the candidate | | `CandidateOut` |
| `PATCH /api/candidates/{id}` | Admin, Team Lead (own team) | `CandidateUpdate` | `CandidateOut` |
| `GET /api/candidates/{id}/profile` | whoever may see the candidate | | `ProfileOut` |
| `PATCH /api/candidates/{id}/profile` | Admin, Team Lead | `ProfileIn` | `ProfileOut` |
| `POST /api/candidates/{id}/resume` | Admin, Team Lead | multipart, field `file` (PDF, DOCX, TXT; 10 MB) | 201 `ResumeOut` (with `text`) |
| `GET /api/candidates/{id}/resume` | whoever may see the candidate | | `ResumeOut` (with `text`) or `null` when there is none |
| `GET /api/candidates/{id}/resume/download` | whoever may see the candidate | | the file, see below; 404 when no file is stored |
| `DELETE /api/candidates/{id}/resume` | Admin, Team Lead | | `Message`; 404 when there is none |
| `GET /api/candidates/{id}/analytics` | whoever may see the candidate | `date_from`, `date_to` | `CandidateAnalytics` (shape of the member dashboard) |
| `GET /api/candidates/{id}/activity` | whoever may see the candidate | `page`, `page_size` | `Page[ActivityItem]` |

`GET /api/candidates` query parameters (all optional, they only narrow the caller's scope):
`team_id`, `assigned_member_id`, `unassigned` (`true` = no responsible member, `false` = has one), `active`
(`true`/`false`), `has_resume` (`true`/`false`), `q` (name, e-mail or phone, case-insensitive), `sort`
(`name` default, `recent`, `updated`, `newest_job`, `not_applied`), `page`, `page_size` (max 200).

`CandidateCreate`: `name` (required; same rule as a person's name elsewhere), `team_id` (required for an Admin;
a Team Lead's own team is used, another team is refused with 403), `email` (optional; `null` or `""` = none),
`phone` (optional, max 40 characters: digits, spaces, `+ ( ) - . / #` and `x`/`ext`), `notes` (max 10,000),
`assigned_member_id` (optional).

`CandidateUpdate`: only the fields that are sent change. `name`, `email` (`null` or `""` removes it), `phone`,
`notes`, `assigned_member_id` (`null` unassigns), `is_active`, `team_id` (Admin only; moves the candidate with
all jobs and history to another team and unassigns the member unless a member of the new team is sent along).

Assigning a member (`assigned_member_id`), for Admin and Team Lead alike: the user must be an active MEMBER of
the candidate's own team. Otherwise **422** "A candidate can only be assigned to a member of the candidate's own
team" (same answer for an unknown id, a Team Lead, an Admin, a member of another team), or **409** when that
member is deactivated.

`CandidateOut`: `id`, `name`, `email` (`""` when none), `phone`, `notes`, `team` (`TeamRef` or null),
`assigned_member` (`UserRef` or null = unassigned), `is_active`, `has_resume`, `resume_has_file`,
`profile_completion` (0..100), `profile_summary`, `last_assigned_at` (both described below), `created_by`, `created_at`, `updated_at`, `stats` (`AssignmentStats`).
A Member sees e-mail, phone and notes of their candidates (needed to apply).

### Added later (after the first version of this page): candidate cards without extra requests

- `CandidateOut.last_assigned_at`: timestamp of the candidate's newest **active** assignment, `null` when there is
  none (a withdrawn assignment does not count).
- `CandidateOut.profile_summary` (`CandidateProfileSummary`): `desired_titles`, `years_experience` (number or
  null), `work_authorization`, `preferred_states`. Present on list and single reads, so a card needs no
  `/profile` request. A candidate without a saved profile gets the defaults (empty lists, null, "Other / Prefer
  Not to Say").
- `GET /api/candidates?sort=`: besides `name`, `recent`, `updated` there are `newest_job` (by `last_assigned_at`,
  newest first, candidates without jobs last) and `not_applied` (number of active assignments not yet applied to,
  the same definition as `stats.not_applied`, highest first). Ties are ordered by name. An unknown sort is 422.

### Resume file

- `ResumeOut.has_file` (and `CandidateOut.resume_has_file`): `true` when the uploaded file is stored and can be
  downloaded. Resumes uploaded before this release have only their text: `has_file` is `false` and the download
  answers 404 with an explanation. Show the download button only when `has_file` is true.
- The download is always an attachment (`Content-Disposition: attachment; filename="..."`, ASCII-only sanitised
  name ending in `.pdf`, `.docx` or `.txt`), with a content type fixed by the server per extension,
  `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`. It is a plain GET with the session cookie, so a
  normal link (`<a href="/api/candidates/{id}/resume/download">`) works.
- Uploading replaces the current resume; text and file of the previous version are erased. `ResumeOut.version`
  counts up.
- The notice about outside AI services is unchanged: `settings.ai_providers` on `GET /api/auth/me` is the single
  source (also in `GET /api/searches/options` as `limits.ai_providers`).

## Removed endpoints

| Removed | Use instead |
| --- | --- |
| `GET/PATCH /api/users/{id}/profile` | `GET/PATCH /api/candidates/{id}/profile` |
| `POST /api/resumes/upload` (form field `member_id`) | `POST /api/candidates/{id}/resume` |
| `GET /api/resumes/{resume_id}` | `GET /api/candidates/{id}/resume` |
| `DELETE /api/resumes/{resume_id}` | `DELETE /api/candidates/{id}/resume` |
| `GET /api/members/{id}/resume` | `GET /api/candidates/{id}/resume` |

A Member can no longer upload a resume or edit a profile anywhere.

## Changed endpoints and renamed fields

### Users and teams

- `UserOut`: removed `has_resume` and `profile_completion`; added `candidate_count` (members: candidates assigned
  to them). `stats` (members only) = totals over the candidates currently assigned to the member.
- `PATCH /api/users/{id}`: deactivating a member, or moving a member to another team (`team_id`, Admin), makes
  all their candidates unassigned. The candidates stay in their team with jobs and history (previously the
  member's assignments moved with the member).
- `TeamOut`: added `candidate_count` (active candidates) and `unassigned_candidates`.
- `UiSettings.max_members_per_search` and `SettingsOut.max_members_per_search` keep their names and now limit
  **candidates** per search.
- `ExtraField` / `profile_extra_fields`: unchanged shape; they now describe the candidate profile.
- `ProfileOut.user_id` -> `ProfileOut.candidate_id`. `ProfileIn` unchanged.
- `ResumeOut.user_id` -> `ResumeOut.candidate_id`; added `has_file`.

### Searches

- `SearchCreate.member_ids` -> `candidate_ids`. `team_id` now means "every active candidate of this team".
  A candidate needs no responsible member to be searched.
- `SearchRunOut.member_count` -> `candidate_count`.
- `SearchTargetOut.member` (`UserRef`) -> `candidate` (`CandidateRef`: `id`, `name`); added `resume_id` (the resume
  version the search ran with; null before the run starts, when the candidate has no resume, and for searches
  made before this release).
- `SearchSkipped.member` -> `candidate`. `SearchResultOut.member` -> `candidate`.
- `GET /api/searches?member_id=` -> `?candidate_id=`. `GET /api/searches/{id}/results?member_id=` -> `?candidate_id=`.
- Stored presets (`PresetIn.filters`) are validated as a `SearchCreate` without `candidate_ids`/`team_id`, as before.
- Messages changed wording only ("candidates" instead of "members").

### Assignments

- `AssignItem` / `AssignCreate`: `member_id` -> `candidate_id`. `BulkAssign.member_ids` -> `candidate_ids`
  (and each `items[].member_id` -> `candidate_id`).
- `AssignmentOut` / `AssignmentDetail`: `member` (`UserRef`, was the job seeker) is replaced by
  `candidate` (`CandidateRef`) plus `member` (`UserRef` or **null**) = the member responsible for the candidate
  today. The same in `JobAssignee` (`other_assignees`, `JobManagerView.assignees`) and `ActivityItem`.
- `GET /api/assignments` and `GET /api/assignments/export.csv`: new filter `candidate_id`; `member_id` now means
  "responsible member" (the candidates that member holds today). A Member's list is the assignments of the
  candidates assigned to them; both filters only narrow it.
- CSV columns: `candidate, responsible_member, team, company, title, location, status, match_score, assigned_at,
  applied_at, link_opens, assigned_by, source` (was `member, email, ...`).
- A Member opening an assignment of a candidate that is theirs but was withdrawn by a manager: 410 "This job is
  no longer assigned to your candidate." An assignment of a candidate that was moved to someone else: 403.
- Status changes by a manager carry `metadata.on_behalf_of_member: true` in the event, as before.

### Analytics

- `GET /api/users/{id}/dashboard` and `GET /api/analytics/member/{id}` (`MemberAnalytics`): totals over the
  candidates assigned to the member today; added `candidates: CandidateRow[]` (their stats add up to `stats`).
- `GET /api/users/{id}/activity`: events on the jobs of the member's current candidates. `ActivityItem` see above.
- `TeamAnalytics`: added `candidate_count`, `unassigned_candidates`, `candidates: CandidateRow[]`;
  `members: MemberRow[]` is now per responsible member.
- `MemberRow`: removed `has_resume`; added `candidates` (count).
- `CandidateRow`: `candidate`, `member` (or null), `team`, `is_active`, `has_resume`, `stats`.
- `AdminAnalytics`: `totals` gained `candidates` and `unassigned_candidates`; `teams[].candidates` (count) added;
  added `candidate_distribution` beside `member_distribution` (which is per responsible member; unassigned
  candidates are in no member's band). `GET /api/analytics/admin`: new filter `candidate_id`; `member_id` =
  responsible member.
- Totals agree across roles only for jobs whose candidate has a responsible member: a member's numbers do not
  include unassigned candidates, a team's and the organization's do.

## Notifications (`NotificationOut.type` and `payload`)

| Type | To | Payload |
| --- | --- | --- |
| `candidate_assigned` (new) | the member who received a candidate | `candidate_id`, `candidate_name`, `assigned_by` (name) |
| `candidate_jobs_assigned` (replaces `jobs_assigned`) | the candidate's responsible member, if any | `count`, `candidate_id`, `candidate_name`, `assigned_by` (name), `source` (`search`, `manual`, `queue`), `search_run_id` or null |
| `candidates_unassigned` (new) | the Team Lead of the team, unless they caused it | `count`, `member_name`, `reason` (`member_deactivated`, `member_moved_to_another_team`), `team_id` |
| `search_finished` | who started the search | `search_run_id`, `status`, `matched`, `assigned`, `candidates` (was `members`) |
| `jobs_delegated` | Team Lead | unchanged |

Old `jobs_assigned` notifications stay in the database with their old payload (`count`, `assigned_by`, `source`,
`search_run_id`); render them as a generic "N jobs assigned".

## Audit actions (Admin audit log)

`candidate.created`, `candidate.updated` (`fields`), `candidate.assigned` / `candidate.unassigned` (`from_member`,
`to_member`, optional `reason`), `candidate.activated` / `candidate.deactivated`, `candidate.transferred`
(`from_team`), `resume.uploaded`, `resume.deleted`, `profile.updated`; all with `target_type: "candidate"`.
Downloads are not audited.

## Existing data after the migration

A former member account became a candidate **with the same id as the user** (named after the user, in the user's
team, assigned to that same user, active exactly when the account was) only if it held job-seeker data: a resume,
a search, a search result, a job, or a profile with at least one field filled in. The empty profile row every
member got at invitation does not count, so an employee who never was a job seeker is a plain member with no
candidates. A member who did hold such data sees one candidate carrying their own name with all their old jobs.
Their old resumes have `has_file: false`.

## Demo stack (`tests/e2e/server.py`, port 8011)

Accounts and password are unchanged. Seed data is described at the top of that file.
