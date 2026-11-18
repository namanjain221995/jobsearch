# AI auto-apply: API contract for the Admin UI

Admin-only feature. Machine-readable contract: `openapi.json` (schemas prefixed `AutoApply`, `Consent`,
`ApplicationAnswers`). Re-export after every change with
`.venv/bin/python -m app.portal.export_openapi > openapi.json`.

## What it does

An Admin picks one candidate and some of that candidate's open job assignments. For each assignment the worker
opens the employer's own hosted application form (Greenhouse, Lever, Ashby, Workable), reads its fields, prepares
an answer for every field from the candidate's recorded data and resume, and performs a **dry-run fill** in a
browser in which every form submission is blocked. The Admin reviews the field list, the sources of every answer
and a screenshot, edits answers if needed, and approves. Only then does a second worker job open the form again,
fill it, check it once more and submit it once. Nothing is submitted without an Admin approval of that exact
preview.

The browser is the portal's honest browser: it identifies itself, never solves or works around a CAPTCHA, a
sign-in, an e-mail code or a bot check. Any of those, and any question that cannot be answered from recorded
facts, stops the run and hands it to a person.

## Who sees it

| Role | Auto-apply runs (`/api/auto-apply/*`) | Consents (`/api/candidates/{id}/consents`) | Application answers (`/api/candidates/{id}/application-answers`) |
| --- | --- | --- | --- |
| ADMIN | everything | read and write | read and write |
| TEAM_LEAD | **403** on every route | **403** | read and write (own team; other team 404) |
| MEMBER | **403** on every route | **403** | **403** |

Team Leads collect the legal/EEO answers from candidates, so they may edit them. Nothing about runs or consents
is returned to them anywhere: auto-apply events in assignment history and candidate activity are hidden from
non-Admins, and the member's notification after a submission says only that an administrator submitted the
application (type `application_submitted`).

The whole feature is off until `AUTO_APPLY_ENABLED=true`. When off, `POST /api/auto-apply/runs`,
`/approve` and `/approve-batch` answer **409** `{"error": "Auto-apply is switched off ..."}`; reading still works.

## Endpoints

| Method and path | Request | Response |
| --- | --- | --- |
| `GET /api/auto-apply/status` | | `AutoApplyStatus` |
| `GET /api/auto-apply/candidates/{id}/eligibility` | | `AutoApplyEligibilityOut` (start dialog: every open assignment with its ATS and whether a run can start) |
| `POST /api/auto-apply/runs` | `AutoApplyRunCreate` | 201 `AutoApplyRunsCreated` |
| `GET /api/auto-apply/runs` | query: `candidate_id`, `assignment_id`, `status` (repeatable), `ats`, `page`, `page_size` (max 200) | `Page[AutoApplyRunOut]` (newest first) |
| `GET /api/auto-apply/runs/{id}` | | `AutoApplyRunDetail` |
| `GET /api/auto-apply/runs/{id}/screenshots/{screenshot_id}` | | `image/jpeg`, `Content-Disposition: inline`, `Cache-Control: no-store, private`; 404 when deleted by retention |
| `PATCH /api/auto-apply/runs/{id}/answers` | `AutoApplyAnswersPatch` | `AutoApplyRunDetail` (status `preparing`: a new dry run is queued) |
| `POST /api/auto-apply/runs/{id}/approve` | `AutoApplyApproveIn` | 202 `AutoApplyRunDetail` (status `approved`) |
| `POST /api/auto-apply/runs/{id}/cancel` | | `AutoApplyRunDetail` (status `cancelled`) |
| `POST /api/auto-apply/runs/approve-batch` | `AutoApplyBatchApproveIn` | `AutoApplyBatchApproveOut` |
| `GET /api/candidates/{id}/consents` | | `list[ConsentOut]` (newest first) |
| `POST /api/candidates/{id}/consents` | `ConsentIn` | 201 `ConsentOut` |
| `POST /api/candidates/{id}/consents/{consent_id}/revoke` | `ConsentRevokeIn` (optional note) | `ConsentOut`; every queued/open run of the candidate becomes `cancelled` (`consent_revoked`) |
| `GET /api/candidates/{id}/application-answers` | | `ApplicationAnswersOut` |
| `PATCH /api/candidates/{id}/application-answers` | `ApplicationAnswersIn` (only sent fields change) | `ApplicationAnswersOut` |

Errors use the portal's usual body `{"error": "...", "detail": ""}`. 404 for a candidate/run/assignment that
does not exist. 409 for a state conflict (wrong run status, no active consent, limit reached, preview changed).
422 for invalid input.

## Run statuses

```
queued -> preparing -> ready_for_review -> approved -> submitting -> submitted
                    \-> needs_input   (Admin edits answers or fixes the candidate's records, PATCH answers -> preparing)
                    \-> blocked       (needs a human: CAPTCHA, sign-in, unsupported site, certification, ...)
any open status -> cancelled (Admin, or consent revoked)
preparing/submitting -> failed  (error; `outcome_unknown=true` when a submit may have gone through)
```

Open statuses: `queued`, `preparing`, `needs_input`, `ready_for_review`, `approved`, `submitting`. One open run per
assignment at a time. Terminal: `submitted`, `failed`, `blocked`, `cancelled` (a new run can then be created,
except after `submitted` or an `outcome_unknown` failure: that posting is never applied to twice).

`failed` with `outcome_unknown=true` means the submit button was pressed and the result could not be read. The
run is **never retried**. The UI must tell the Admin to check the employer's site / candidate's mailbox and then
record the outcome on the assignment by hand.

## Schemas (summary; exact definitions in `openapi.json`)

`AutoApplyRunCreate`: `candidate_id` (uuid), `assignment_ids` (1..20 uuids; must be this candidate's active
assignments whose status is `ASSIGNED`, `VIEWED` or `APPLYING` and not yet applied).

`AutoApplyRunsCreated`: `runs: AutoApplyRunOut[]`, `skipped: {assignment_id, reason}[]` where `reason` is one of
`not_found`, `not_this_candidate`, `withdrawn`, `already_applied`, `run_open`, `already_submitted`,
`no_application_url`.

`AutoApplyRunOut`: `id`, `status`, `ats` (`greenhouse|lever|ashby|workable|unsupported`), `job_url`,
`candidate: CandidateRef`, `assignment_id`, `job: {id, title, company}`, `blocked_reasons: Blocker[]`,
`needs_input: NeedsInput[]`, `counts: {fields, answered, needs_input, ai_drafted}`, `outcome_unknown`, `error`,
`created_by: UserRef`, `approved_by: UserRef|null`, `created_at`, `updated_at`, `approved_at`, `submitted_at`,
`finished_at`.

`AutoApplyRunDetail` = `AutoApplyRunOut` plus
- `form: FormField[]` (snapshot of the employer form: `key`, `label`, `type` = `text|email|tel|url|textarea|select|radio|checkbox|file|date|number|unknown`,
  `required`, `options: string[]`, `section`; never any third-party personal data),
- `answers: Answer[]`: `field_key`, `label`, `value` (string; for a checkbox `"checked"`/`"unchecked"`, for a file
  the file name), `source` (see below), `evidence: string[]` (resume lines / record names the value comes from),
  `classification` (what the question was understood as, e.g. `standard:email`, `legal:sponsorship`,
  `eeo:gender`, `free_text`, `certification:attest`), `required`, `status` (`ok|missing|blocked`), `note`,
- `fill_report: {field_key, label, value, filled: bool, error}[]` (what the dry run typed),
- `screenshots: {id, kind: dry_run|before_submit|confirmation, created_at, size_bytes}[]`,
- `answers_hash` (string; send it back on approve),
- `confirmation: {detected, text_sha256, reference, url} | null`,
- `submission_guard: {blocked_requests, allowed_submissions}` of the last dry run (`allowed_submissions` is 0 by construction).

`Answer.source`: `profile` (job-search profile), `candidate` (candidate details: name, e-mail, phone),
`application_answers` (recorded legal/EEO/etc. answers), `resume_file`, `resume_line`, `salesforce`
(candidate came from Salesforce and the value is a Salesforce field), `ai_draft` (free text written by the model
and verified), `eeo_decline` (decline option chosen because nothing was recorded), `admin_edit`.

`Blocker`: `code`, `detail`. Codes: `captcha_visible`, `challenge_page`, `sign_in_required`,
`account_creation_required`, `email_code_required`, `anti_bot_puzzle`, `personal_completion_certification`,
`attestation_not_permitted`, `data_processing_consent_not_permitted`, `arbitration_agreement`,
`unsupported_site`, `custom_career_site`, `employer_iframe`, `robots_disallowed`, `no_resume_file`,
`no_consent`, `form_not_found`, `form_changed`, `employer_daily_cap`, `posting_closed`.

`NeedsInput`: `field_key`, `label`, `reason` (`not_recorded|unknown|no_matching_option|free_text_unverified|required_unknown_field|file_required`),
`fill_in` (where to fix it: `application_answers.<name>`, `profile.<name>`, `run_answer`).

`AutoApplyAnswersPatch`: `answers: {field_key, value}[]` (1..100). Values are recorded with `source=admin_edit`.
Allowed while the run is `needs_input` or `ready_for_review`. A new dry run is queued (status `preparing`).

`AutoApplyEligibilityOut`: `candidate_id`, `has_active_consent`, `has_resume_file`, `items: AutoApplyEligibility[]`.
Only active assignments in `ASSIGNED|VIEWED|APPLYING` without `applied_at` are listed. `AutoApplyEligibility`:
`assignment_id`, `job: {id, title, company}`, `status` (assignment status), `ats`, `application_url` (normalised form
address when supported), `supported` (bool: one of the four systems), `eligible` (supported, no open run, never
submitted), `reason_code` (`""` when eligible; else `no_application_url`, `unsupported_site`,
`account_creation_required` (Workday), `custom_career_site`, `run_open`, `already_submitted`), `reason` (English
sentence), `open_run_id`, `open_run_status`. A consent or resume file missing does not change `eligible`; show the
two top-level flags instead (creating runs without consent answers 409; without a resume file the run ends
`blocked: no_resume_file`).

`AutoApplyApproveIn`: `answers_hash` (the hash from the detail the Admin looked at). 409 when the run is not
`ready_for_review`, the hash differs (answers changed), consent is no longer active, the assignment was applied
or withdrawn meanwhile, the employer's daily cap is reached, or the feature is off.

`AutoApplyBatchApproveIn`: `items: {run_id, answers_hash}[]` (1..50). Every run is checked like a single approve;
`AutoApplyBatchApproveOut`: `approved: uuid[]`, `refused: {run_id, reason, detail}[]`. Stable `reason` codes
(also the prefix of the single-approve 409 `error`, e.g. `"answers_changed: ..."`):
`not_found`, `not_ready` (status is not `ready_for_review`), `answers_changed` (hash differs: reload and review),
`no_consent`, `assignment_changed` (applied or withdrawn meanwhile), `employer_daily_cap`, `disabled`,
`already_submitted`.

`AutoApplyStatus`: `enabled`, `worker_online`, `browser_concurrency`, `employer_daily_cap`,
`run_timeout_seconds`, `screenshot_retention_days`, `supported_ats: string[]`.

`ConsentIn`: `scope` (`auto_apply`), `granted` (bool, default true), `method`
(`written|email|e_signature|verbal_recorded`), `note` (max 2000), `document_reference` (max 300, free text, e.g.
"signed form in Salesforce Files 2026-11-18"), `may_attest` (bool, default false: candidate allowed the portal
to tick "I certify my answers are true and correct"), `may_accept_data_processing` (bool, default false:
candidate allowed accepting the employer's privacy / data-processing notice). Recording a new consent supersedes
the previous active one.

`ConsentOut`: the fields above plus `id`, `candidate_id`, `recorded_by: UserRef`, `recorded_at`, `revoked_at`,
`revoked_by: UserRef|null`, `active` (granted and not revoked).

`ApplicationAnswersIn` / `ApplicationAnswersOut` (every field defaults to "unknown"/empty; nothing is guessed):

| Field | Values | Default / fallback |
| --- | --- | --- |
| `authorized_us` | `yes|no|unknown` | from profile `work_authorization` (US Citizen, Green Card, Current Work Visa, EAD -> yes; others unknown) when not recorded |
| `sponsorship_now_or_future` | `yes|no|unknown` | from profile `sponsorship_needed` (`Yes`/`No`; `Unknown` blocks) |
| `age_18_or_over` | `yes|no|unknown` | unknown |
| `willing_to_relocate` | `yes|no|unknown` | unknown |
| `earliest_start` | text (max 100) e.g. "2026-12-15" or "2 weeks notice" | empty |
| `notice_period` | text (max 100) | empty |
| `expected_salary` | text (max 100) | from profile `salary_min` ("$120,000") when empty |
| `previously_employed_by` | list of employer names (max 50) | empty; an empty list means unknown, so "Have you worked here before?" needs input unless `previous_employers_complete=true` |
| `previous_employers_complete` | bool | false; true = the list is the complete list, so "No" may be answered for other employers |
| `restrictive_agreements` | `yes|no|unknown` | unknown |
| `eeo_gender`, `eeo_race`, `eeo_hispanic_latino`, `eeo_veteran`, `eeo_disability` | free text matching an option, or `decline` | `decline` (the form's "decline to self-identify" option) |
| `linkedin_url`, `github_url`, `portfolio_url` | URL (max 300) | empty |
| `current_company`, `current_title` | text (max 160) | empty |
| `city`, `state`, `country` | text (max 100) | empty |
| `phone` | phone | candidate `phone` when empty |
| `first_name`, `last_name`, `preferred_name` | text (max 100) | first/last split of the candidate name |
| `referral_source` | text (max 100), the answer to "How did you hear about us?" | empty |

`ApplicationAnswersOut` additionally has `effective` (the value used after fallbacks, with `source` per field),
`updated_at`, `updated_by: UserRef|null`. EEO values are never shown outside this endpoint and the run detail.

## Answer rules (what the UI should explain)

- Standard fields come from candidate details, the job-search profile, the recorded answers and the stored resume
  file. No stored resume file -> `blocked: no_resume_file`.
- Every other question is classified (rules first; unknown labels by the company model `techsara-35b`, then
  OpenAI/Claude). Legal and EEO questions are answered only from recorded answers; a missing value -> `needs_input`
  naming the exact `application_answers.<field>`.
- EEO questions without a recorded value use the form's decline option (`source=eeo_decline`); optional EEO
  questions without a recorded value stay empty.
- Free text ("Why do you want to work here?", cover letter, "Describe your experience with X") is drafted once per
  run by OpenAI or Claude from resume and profile facts only, then verified deterministically: every number, year
  count, employer, degree, skill or certification in the draft must appear in the sources. Failure -> one
  regeneration -> still failing: `needs_input (free_text_unverified)`.
- Salary only from `expected_salary`/`salary_min`. Never a certification the candidate did not make: "I personally
  completed this application" -> `blocked`; "my answers are true and correct" only with consent `may_attest`;
  arbitration agreements -> `blocked`.

## Audit actions (Admin audit log, `GET /api/audit-logs?action=auto_apply`)

Metadata never contains answer values, EEO data or resume text.

| Action | target_type / target_id | metadata keys |
| --- | --- | --- |
| `auto_apply.runs_created` | `candidate` / candidate id | `runs` (ids), `skipped` (count) |
| `auto_apply.answers_edited` | `auto_apply_run` / run id | `fields` (field keys) |
| `auto_apply.approved` | `auto_apply_run` / run id | `answers_hash` |
| `auto_apply.cancelled` | `auto_apply_run` / run id | |
| `auto_apply.submitted` | `auto_apply_run` / run id | `assignment_id`, `answers_hash`, `confirmation_sha256` |
| `consent.recorded` | `candidate` / candidate id | `granted`, `method`, `may_attest`, `may_accept_data_processing`, `superseded`, `runs_cancelled` |
| `consent.revoked` | `candidate` / candidate id | `consent_id`, `runs_cancelled` |
| `application_answers.updated` | `candidate` / candidate id | `fields` (names of the fields sent) |
| `assignment.status_changed` | `assignment` | the usual one, written by the status service on submission (`on_behalf_of_member: true`) |

## Notifications (`GET /api/notifications`)

To the Admin who created the run and the Admin who approved it (Admins only):
`auto_apply_ready`, `auto_apply_needs_input`, `auto_apply_blocked`, `auto_apply_submitted`, `auto_apply_failed`.
Payload: `run_id`, `candidate_id`, `candidate_name`, `assignment_id`, `job_title`, `company`, `status`,
`reasons` (blocker codes and `fill_in` targets, at most 20), `outcome_unknown`.

To the candidate's responsible member, only after a confirmed submission: `application_submitted`, payload
`assignment_id`, `candidate_id`, `candidate_name`, `job_title`, `company` (no mention of automation).
Needs-input / blocked / failed are not sent to the member (the feature is invisible to members).

## Assignment history

A submission writes the normal `status_changed` event (to `APPLIED`, `on_behalf_of_member: true`, reference if the
confirmation page showed one) plus an `auto_apply_submitted` event (metadata `source: auto_apply`, `run_id`,
`answers_hash`, `schema_hash`, `confirmation_sha256`, `reference_recorded`). `auto_apply_submitted` is returned only
to Admins (assignment detail and candidate/member activity filter it out for everyone else).
