# Salesforce (read-only): API contract for the web app

The machine-readable contract is `openapi.json` in the repository root (re-exported with
`.venv/bin/python -m app.portal.export_openapi > openapi.json`). Schema names below are the ones in `openapi.json`.
Errors have the usual shape `{"error": "...", "detail": ""}`.

## What it does

Candidates already exist in the company's Salesforce as Person Accounts. A Team Lead or Admin types a name into a
search box, picks a match, and the portal creates a local candidate from it: details, job-search profile and the
newest active resume file. The portal only ever **reads** Salesforce. Nothing is written back.

Members never reach any Salesforce endpoint (**403**).

## Status codes shared by every endpoint here

| Code | When | `error` text |
| --- | --- | --- |
| 403 | the caller is a Member | "You do not have access to this resource" |
| 429 | more than 60 Salesforce searches by one user within a minute (header `Retry-After`) | "Too many Salesforce searches. Wait a moment and try again." |
| 502 | Salesforce answered with an error, or refused the integration user's sign-in | a plain sentence, never Salesforce's own text |
| 503 | Salesforce is not configured on this server, cannot be reached, or timed out | a plain sentence |

## Endpoints

| Method and path | Who | Request | Response |
| --- | --- | --- | --- |
| `GET /api/salesforce/status` | Admin, Team Lead | | `SalesforceStatus` |
| `GET /api/salesforce/candidates` | Admin, Team Lead | query `q`, `status`, `limit` | `SalesforceSearchOut` |
| `POST /api/candidates/import-salesforce` | Admin, Team Lead | `SalesforceImportIn` | **201** `CandidateSyncOut` when created, **200** when it already existed and was refreshed |
| `POST /api/candidates/{id}/salesforce-refresh` | Admin, Team Lead (own team) | no body | `CandidateSyncOut` |
| `GET /api/integrations` (existing, Admin) | | | gains `salesforce`: the same object as `SalesforceStatus` |
| `GET` / `PATCH /api/settings` (existing, Admin) | | gains `salesforce_team_lead_scope` | |

### `GET /api/salesforce/status`

`SalesforceStatus`:

| Field | Type | Meaning |
| --- | --- | --- |
| `configured` | bool | all four server settings are present. When false, hide the search box |
| `connected` | bool or null | result of a cheap connectivity check, cached for 60 seconds; `null` when not configured |
| `message` | string | one plain sentence for the Admin integrations page ("Connected", or what is wrong) |
| `checked_at` | datetime or null | when the check last ran |
| `api_version` | string | e.g. `v68.0` |
| `team_lead_scope` | `all` \| `own` | the Admin setting below |

This endpoint never fails because of Salesforce: a broken connection is reported as `connected: false`.

### `GET /api/salesforce/candidates?q=&status=&limit=`

Searches Person Accounts whose name, e-mail or phone contains `q` (case-insensitive).

- `q`: at most 80 characters. Without `q`, or with fewer than 2 characters after trimming, the answer is an empty
  list (200) and Salesforce is not asked.
- `status`: `active` (default; only `Candidate_Status__c = Active`) or `all`.
- `limit`: 1 to 25, default 10. Results are ordered by name.
- Identical searches are answered from a 10 second cache; the "already in the portal" fields are always fresh.

`SalesforceSearchOut`: `items: SalesforceCandidate[]`, `limit_reached: bool` (true when there may be more matches
than `limit`: ask the user to type more).

`SalesforceCandidate`:

| Field | Type | From Salesforce |
| --- | --- | --- |
| `salesforce_id` | string (18 characters) | `Id` |
| `name` | string | `Name` |
| `email`, `phone` | string, `""` when empty | `PersonEmail`, `Phone` |
| `candidate_status` | string | `Candidate_Status__c` (In Progress, Active, Hold, Paused, Placed, Terminate, Closed) |
| `technology`, `niche` | string | `Technology__c`; `Niches__r.Name`, else `Niche__c` |
| `work_authorization`, `visa_status` | string | `Work_Authorization__c`, `Current_Visa_Status__c`, as written in Salesforce |
| `years_experience` | number or null | `Total_Years_of_Experience__c` |
| `preferred_job_type`, `preferred_locations`, `experience_level` | string | as written in Salesforce |
| `assigned_recruiter`, `marketing_team_lead` | string | names only |
| `recruiter_member` | user or null | the active portal member whose sign-in address equals the recruiter's e-mail address in Salesforce (`Assigned_Recruiter__r.Email__c`); null when there is none or the caller does not manage that member's team |
| `has_resume` | bool | an active resume record with status Uploaded or Approved exists |
| `imported_candidate_id` | uuid or null | the portal candidate made from it, **only when the caller may see that candidate** |
| `imported_elsewhere` | bool | it is in the portal in a team the caller cannot see. A Team Lead learns nothing more (no team, no id). Always false for an Admin |
| `importable` | bool | not in the portal yet. When false and `imported_candidate_id` is set, offer "Open" instead of "Import" |

### `POST /api/candidates/import-salesforce`

`SalesforceImportIn`: `salesforce_id` (18 characters, letters and digits), `team_id` (an Admin must give it when
the candidate is new; a Team Lead's own team is always used and another team is refused with 403),
`assigned_member_id` (optional; an active MEMBER of that team, same rules and answers as `POST /api/candidates`).

What happens:

1. The Person Account is read. Unknown id, not a Person Account, or (scope `own`) not this Team Lead's: **404**
   "Salesforce candidate not found".
2. Already in the portal and visible to the caller: it is refreshed (rules below) and returned with **200**;
   `team_id` and `assigned_member_id` are ignored. In a team the caller cannot see (Team Lead only): **409**
   "This candidate is already in the portal." and nothing else.
3. Otherwise a candidate is created (**201**) with name, e-mail, phone, the mapped job-search profile and the
   newest active resume. Audit entries `candidate.imported_salesforce` and, with a file, `resume.imported_salesforce`.

A resume problem never blocks the import: the candidate is created without a resume and `sync.resume` says why,
so the UI can offer the normal upload.

`CandidateSyncOut` is `CandidateOut` plus one field `sync`:

| `sync` field | Type | Meaning |
| --- | --- | --- |
| `created` | bool | true on 201 |
| `updated_fields` | string[] | fields written from Salesforce in this call |
| `kept_fields` | string[] | fields where Salesforce differs but a local edit was kept |
| `resume` | string | `imported`, `unchanged` (the portal already has this Salesforce file), `none` (Salesforce has no active resume file), `kept_local` (a resume uploaded in the portal is newer), `refused_type` (not PDF or DOCX), `refused_size` (larger than the upload limit, 10 MB), `unreadable` (the file could not be parsed), `unavailable` (Salesforce failed while the file was read; try a refresh later) |

Field names used in `updated_fields` / `kept_fields`: `name`, `email`, `phone`, `notes`, `desired_titles`,
`years_experience`, `work_mode`, `work_authorization`, `sponsorship_needed`, `seniority`, `preferred_states`.

### `POST /api/candidates/{id}/salesforce-refresh`

Re-reads details, profile and resume for a candidate that came from Salesforce. 404 when the candidate is outside
the caller's scope (as everywhere), **409** "This candidate did not come from Salesforce" for a manual candidate,
**404** "Salesforce candidate not found" when the Person Account is gone. Audit entry `candidate.salesforce_refreshed`.

**The rule that protects local work** (the same code runs for import-again and refresh):

- *Fields.* For each synced field the portal remembers the value it last wrote from Salesforce. A field is written
  again only when the local value is still empty (or the built-in default), or still equal to that remembered
  value. A field a manager changed in the portal is kept and listed in `kept_fields`. An empty value in
  Salesforce never erases a local value.
- *Notes.* The original text of `Preferred_Job_Locations__c` is kept in the notes as a line starting with
  `Preferred job locations (from Salesforce): `. On refresh that line is replaced when it is still exactly as
  written; when it was edited or removed, a changed Salesforce text is appended as a new line and the rest of
  the notes is never touched.
- *Resume.* The Salesforce file is fetched when its ContentVersion id differs from every Salesforce file this
  candidate already received **and** (a) the candidate has no current resume, or (b) the current resume came from
  Salesforce, or (c) the current resume was uploaded in the portal *before* the Salesforce file was last modified.
  A resume uploaded in the portal after the Salesforce file was modified is never replaced (`kept_local`).
  Uploading or replacing a resume in the portal works as before.

## Changed schemas

`CandidateOut` gains:

| Field | Type | Meaning |
| --- | --- | --- |
| `source` | `manual` \| `salesforce` | where the candidate came from |
| `salesforce_id` | string or null | the Person Account id. `null` for manual candidates, and always `null` for a Member |
| `salesforce_url` | string or null | `<instance URL>/<id>`, to open the record in Salesforce. Managers only; `null` for a Member |
| `salesforce_synced_at` | datetime or null | last successful import or refresh |

`ResumeOut` gains `source`: `upload` | `salesforce`.

`SettingsOut` / `SettingsUpdate` gain `salesforce_team_lead_scope`: `all` (default) or `own`.

- `all`: a Team Lead can find and import every Salesforce candidate.
- `own`: only candidates whose `Assigned_Marketing_Team_Lead__r.Email__c` equals the Team Lead's sign-in e-mail
  (compared without regard to case). The same check applies to import and refresh, not only to search.
- An Admin always sees all.

`IntegrationsOut` gains `salesforce` (`SalesforceStatus`).

`GET /api/candidates` gains the filter `source=manual|salesforce`.

## Mapping into the job-search profile

Nothing is invented: a value Salesforce does not have leaves the portal field as it is.

| Portal field | Salesforce | Rule |
| --- | --- | --- |
| `desired_titles` | `Preferred_Job_Positions__c`, else `Technology__c`, else niche | positions are split at commas, semicolons and line breaks (at most 8); placeholders such as "NA" count as empty; the niche "Other" is ignored |
| `years_experience` | `Total_Years_of_Experience__c` | 0 to 40, one decimal; anything else is ignored |
| `work_mode` | `Preferred_Job_Type__c` | exactly one of Remote, Hybrid, On-site: that value; several, or "All of the above": `All` |
| `work_authorization` | `Current_Visa_Status__c`, else `Work_Authorization__c` | see the table below; a value not in the table becomes `Other / Prefer Not to Say` |
| `sponsorship_needed` | `Sponsorship_Required_For_VISA__c` | checked: `Yes`. Unchecked: left alone (the box is unchecked on 853 of 856 records, so it does not mean "No") |
| `seniority` | `Targeted_Experience_Level__c` | the picklist labels (`Entry Level`, `Associate`, `Mid-Senior level`, `Director`, `Executive`), several joined with ", " |
| `preferred_states` | `Preferred_Job_Locations__c` | US state names and upper-case two-letter codes become codes (`TX`); cities, regions and "anywhere" are ignored; the original text goes to the notes |

| Salesforce value | Portal `work_authorization` |
| --- | --- |
| US Citizen, USC | US Citizen |
| Green Card / Permanent Resident, Green Card | Green Card / US Permanent Resident |
| Green Card EAD, OPT, STEM OPT Extension | Employment Authorization Document (EAD) |
| H1B | Current Work Visa |
| CPT, H4, B2, Other, anything else | Other / Prefer Not to Say |

## The assigned recruiter becomes the responsible member

Salesforce names a recruiter for a candidate (`Account.Assigned_Recruiter__c`, a lookup to `Recruiter__c`, whose
`Email__c` is read). On import and on refresh the portal looks for an active MEMBER with that sign-in address:

| `sync.recruiter` | meaning |
| --- | --- |
| `assigned` | that member is now the responsible member (audit `candidate.assigned`, reason `salesforce_assigned_recruiter`; the member is notified) |
| `unchanged` | that member was already responsible |
| `kept` | a manager chose another member in the portal; that choice stays |
| `no_recruiter` | Salesforce names no recruiter, or the recruiter has no e-mail address |
| `not_in_portal` | nobody in the portal has that address as an active member |
| `other_team` | the member belongs to another team than the candidate |

The member changes only when nobody is responsible or when the last sync set the current one. An Admin who imports
without `team_id` gets the recruiter's team. `POST /api/searches` does the same for selected Salesforce candidates
that have nobody responsible, before the search starts, so the jobs found go to that member; a Salesforce failure
there changes nothing and never stops the search.
