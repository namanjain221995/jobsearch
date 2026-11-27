# AI Job Finder — team portal

A multi-team platform for a staffing business: it finds real US job postings for candidates, assigns them and tracks the applications made on each candidate's behalf.

- **Admin** creates teams and Team Leads, adds or imports jobs, runs searches, delegates jobs and sees organization-wide analytics.
- **Team Lead** invites members, creates candidates and uploads their resumes, runs AI searches for selected candidates, assigns each candidate to a member and follows the team's progress.
- **Member** is a recruiter working under a Team Lead. A member sees the candidates assigned to them (details, profile, resume) and those candidates' jobs, reads the full description, opens the application page, applies for the candidate and records what was actually applied to.
- A **candidate** is the person who needs a job. A candidate is a record, never an account, and never signs in.

Every job comes from a live source (employer job boards, job-board APIs, AI web search with verification, or manual entry by an Admin). Nothing is generated to fill a quota.

## Architecture

```
Browser ──► Next.js (frontend/, port 3010) ──/api/* proxy──► FastAPI (app/portal, port 8010) ──► PostgreSQL
                                                                   │ enqueue                         ▲
                                                                   ▼                                 │
                                                                 Redis ──► worker (arq) ─────────────┘
                                                                           search engine, job index refresh,
                                                                           headless browser pool, AI calls
```

| Part | Technology | Where |
| --- | --- | --- |
| Web app | Next.js (App Router), TypeScript, Tailwind, shadcn/ui, Recharts, TanStack Query | `frontend/` |
| API | FastAPI, Pydantic, SQLAlchemy 2 (async, asyncpg), Alembic | `app/portal/` |
| Background searches | arq worker on Redis | `app/portal/worker.py`, `search_runner.py` |
| Search engine | unchanged engine from the single-user version: 9 source types, verification, deduplication, ranking | `app/*.py`, `app/providers/` |
| Database | PostgreSQL 17: users, teams, candidates, jobs, searches, assignments, events, audit log, and the crawler's job index | `alembic/` |

The browser only talks to Next.js. Next.js forwards `/api/*` to FastAPI, so the session cookie is same-origin and API keys never reach the browser.

## Run it locally

```bash
# 1. PostgreSQL and Redis
docker compose up -d postgres redis

# 2. Backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium          # browser crawler (optional)
cp .env.example .env                           # add OPENAI_API_KEY / ANTHROPIC_API_KEY
.venv/bin/alembic upgrade head                 # create / upgrade the schema

# 3. First administrator (asks for e-mail and password; safe to run again)
.venv/bin/python -m app.portal.bootstrap

# 4. Three processes
.venv/bin/uvicorn app.portal.app:app --host 127.0.0.1 --port 8010 --no-access-log     # API (it writes its own request log)
.venv/bin/arq app.portal.worker.WorkerSettings                        # background worker
cd frontend && npm install && npm run dev                             # web app on http://127.0.0.1:3010 (development mode)
```

For everyday use run the production build of the web app instead of the development server:

```bash
cd frontend && npm run build && npm run start                         # http://127.0.0.1:3010
```

The address of the API (`API_ORIGIN`, default `http://127.0.0.1:8010`) is fixed into the web app when it is built, so set it before `npm run build` if the API runs elsewhere. `make help` lists a short command for each of these steps.

Open <http://127.0.0.1:3010>. You land on the sign-in page; after signing in each role is sent to its own dashboard (`/admin/dashboard`, `/tl/dashboard`, `/member/dashboard`).

The worker reads the employer boards into the job index when it starts (about a minute). Searches started earlier simply cover fewer employers.

### Upgrading from the single-user version

The old app kept its job index in `jobfinder.db` (SQLite). Copy it into PostgreSQL once:

```bash
.venv/bin/python -m app.portal.migrate_sqlite
```

The SQLite file is opened read-only and is never changed. The copy is one transaction and is verified (row counts, a checksum sample, a full-text query) before it is committed. Running it again does nothing.

### Whole stack in Docker

```bash
cp .env.example .env                           # once; the backend services read it
docker compose --profile app up -d --build
docker compose --profile app exec -e BOOTSTRAP_ADMIN_EMAIL=you@company.com -e BOOTSTRAP_ADMIN_PASSWORD='...' backend python -m app.portal.bootstrap
```

Services: `postgres`, `redis`, `migrate` (runs `alembic upgrade head`, then exits), `backend`, `worker`, `frontend`. The web app is on <http://localhost:3010>. Set `PUBLIC_BASE_URL` to the address users open (default `http://localhost:3010`; invitation and reset links are built from it), and `POSTGRES_PASSWORD` to a real secret on any shared machine. `WEB_PORT`, `API_PORT`, `POSTGRES_PORT` and `REDIS_PORT` change the published ports. Stop the local API and web app first if they use the same ports.

## How accounts are created

There is no public registration.

1. The Admin creates a Team Lead (with a new or existing team). A Team Lead adds members to their own team. An Admin can also invite a further Admin (Settings, tab "Admins", or "Invite Admin" on the Invitations screen): such an invitation has no team, only an Admin can create, resend, revoke or see it, and its link works only while the Admin who issued it is still an active Admin. The last Admin who can sign in cannot be deactivated, and nobody can deactivate themselves.
2. The system creates an invitation: a random token that is stored only as a hash, works once and expires after 48 hours. Role and team are fixed on the server when the invitation is created.
3. With SMTP configured the link is e-mailed. Without SMTP the person who created the invitation sees the link and copies it; the app never claims an e-mail was sent when it was not. A send counts only when the mail server accepted the message. The connection is encrypted (STARTTLS, or TLS from the start on port 465) and the server's certificate is verified; a server without encryption or with an untrusted certificate gets neither the SMTP password nor the message.
4. The invited person opens `/invite/accept?token=…`, chooses a password and signs in.

Password reset works the same way (`/forgot-password`, or a manager creates a reset link for someone in their scope).

## Candidates, profiles and resumes

A Team Lead (or an Admin) creates a candidate in their team: name, optional e-mail and phone, free-text notes. The candidate's job-search profile holds the desired titles, experience, skills, locations, work arrangement, work authorization, sponsorship, salary, seniority, excluded companies and required keywords. Members do not look for jobs themselves and have no profile or resume.

The Team Lead uploads the candidate's resume as PDF, DOCX or TXT (10 MB at most). Its text is extracted and stored for searching, and the uploaded file is kept so that the responsible member can download it when applying. The download is offered to exactly the people who may see the candidate and is always sent as an attachment. Uploading a new resume erases the text and the file of the previous version. Resumes uploaded before files were kept have only their text; the app says so and offers no download for them. The upload area says which outside AI services receive text from the resume when a search runs, read from the server's configuration.

Each candidate has at most one responsible member at a time, always an active member of the candidate's own team; a member can hold many candidates. The Team Lead can hand a candidate to another member at any moment: all jobs and their history stay with the candidate, the previous member loses access at once and the new one gains it. The member is told about a new candidate by a notification. When a member is deactivated or moved to another team, their candidates stay in the team and become unassigned; the Team Lead is notified and finds them under the "unassigned" filter. A candidate can be deactivated (no searches, no new jobs) without losing anything.

An Admin can add up to 20 extra profile fields under Settings (text, number, yes or no, or a choice from a list). They appear on every candidate's profile under "Additional information". Stored answers always match the fields that exist: removing a field, changing its type or removing an option deletes the answers that no longer fit when the settings are saved, the audit log records how many, and a field added again later starts empty.

When this model was introduced (migration `c4d2a9e71f36`), every member account that held job-seeker data (a resume, searches, jobs, or a profile with at least one field filled in) became a candidate carrying the same name, in the same team, assigned to that same member, so nothing was lost; a member who only had the empty profile created at invitation stayed a plain member; `docs/candidate-api.md` describes the API.

## Salesforce (read-only)

**Job-title suggestions on Find Jobs.** The Job titles box takes any typed title as before; two things help. While typing, `GET /api/job-titles/suggestions` proposes up to 8 titles: the selected candidates' own titles first, then titles that postings in the job index really carry, the most frequent first, cleaned of requisition numbers, places and "(Remote)" (`app/job_titles.py`). The index is read once every six hours with one aggregate query and matched in memory, so typing never queries it. For the selected candidates, `POST /api/job-titles/from-resumes` returns the titles saved in the profile, the titles the resume names, and, when a model is configured and answers in time, other names employers use for the same work (the model is given titles and skills, never the resume text; its answer is remembered per candidate, resume version and profile for a week). Without a model the answer says so and holds the saved and resume titles only. Both endpoints are for Team Leads and Admins, limited per person, and refuse a request that names a candidate outside the caller's scope.

Candidates already exist in the company's Salesforce as Person Accounts, so a Team Lead or Admin does not type them in again. In the Find Jobs form and on the Candidates page they type a name (or e-mail or phone) into a search box, the matching Salesforce candidates appear with their key details, and choosing one brings it into the portal: details, the job-search profile and the newest active resume file. The Candidates page can list the candidates that were brought in this way (`source=salesforce`). Creating a candidate by hand and uploading or replacing a resume in the portal work as before. `docs/salesforce-api.md` is the API contract, with the mapping and the refresh rule.

**The portal only reads Salesforce.** The only requests it can send are `GET` requests to the REST data API and the one sign-in `POST` to `/services/oauth2/token`. This is built into the client (`app/portal/services/salesforce.py`): every call goes through one function that refuses any other method, and the HTTP client itself refuses any request that is neither a `GET` nor that sign-in. Nothing is ever created, changed or deleted in Salesforce.

Settings (all in `.env`; leave them empty to switch the integration off):

| Variable | Purpose |
| --- | --- |
| `SF_CLIENT_ID` | consumer key of the connected app |
| `SF_USERNAME` | user name of the integration user (a read-only profile is enough and recommended) |
| `SF_LOGIN_URL` | `https://login.salesforce.com`, `https://test.salesforce.com` or the org's My Domain URL; also the audience of the sign-in token |
| `PRIVATE_KEY_B64` (or `SF_PRIVATE_KEY_B64`) | the connected app's RSA private key in PEM, base64 on one line |
| `SALESFORCE_SEARCHES_PER_MINUTE` | searches one signed-in user may run per minute (default 60) |

Sign-in is the OAuth JWT bearer flow (RS256). The access token is kept in memory only, reused until shortly before it expires and renewed once when Salesforce answers 401. The API version (`v68.0`) is one constant. Requests have timeouts and at most four run at the same time. Identical searches are answered from memory for ten seconds. A Salesforce failure is reported as 502 or 503 with a plain sentence; Salesforce's own error text is never passed on. Logs hold an operation name, a status code and Salesforce's error code: never a query, a name, a token or a response body. Admin → Integrations shows whether Salesforce is configured and reachable (`GET /api/salesforce/status`, checked at most once a minute).

What is read, with an explicit field list in every query (`FIELDS(ALL)` is never used, and no record is ever fetched whole):

- Account (Person Accounts only): `Id`, `Name`, `PersonEmail`, `Phone`, `Candidate_Status__c`, `Technology__c`, `Niche__c`, `Niches__r.Name`, `Preferred_Job_Positions__c`, `Preferred_Job_Locations__c`, `Preferred_Job_Type__c`, `Total_Years_of_Experience__c`, `Work_Authorization__c`, `Current_Visa_Status__c`, `Sponsorship_Required_For_VISA__c`, `Targeted_Experience_Level__c`, `Assigned_Recruiter__r.Name`, `Assigned_Marketing_Team_Lead__r.Name`, `Assigned_Marketing_Team_Lead__r.Email__c`, `LastModifiedDate`.
- `Resume__c`: `Id`, `Candidate__c`, `Is_Active__c`, `Resume_Status__c`, `CreatedDate`; of the file attached to it (`ContentDocumentLink` → `ContentDocument`) the extension, size, latest version id and modification date; and the file's bytes (`ContentVersion/<id>/VersionData`). Only the newest active resume with status Uploaded or Approved, only PDF and DOCX, only up to the upload limit (10 MB), and only when the bytes are what the extension says. The file then goes through the same pipeline as an uploaded resume.

What is never read: the Account object also holds identity-document numbers, bank details, account passwords, the date of birth and protected personal characteristics. None of these fields is named anywhere in the code. A whitelist in the client lists every field that may be read (a few more than are used today); a query naming any other field cannot be built, and a test fails if any query sent contains a field outside that list.

Who may search: Admins and Team Leads. Members never reach a Salesforce endpoint (403). The Admin setting `salesforce_team_lead_scope` (Settings, `PATCH /api/settings`) decides what a Team Lead finds: `all` (default) every Salesforce candidate, or `own` only candidates whose `Assigned_Marketing_Team_Lead__r.Email__c` equals the Team Lead's sign-in e-mail (compared without regard to case). The same rule applies to import and refresh. An Admin always sees all. One Salesforce candidate exists at most once in the portal: a Team Lead who picks a candidate that another team already brought in is told only that it is already in the portal.

Refreshing a candidate from Salesforce does not destroy local work: a field is written again only while it is empty or still holds the value Salesforce gave it last time, an empty Salesforce value never erases anything, and a resume uploaded in the portal is replaced only by a Salesforce file that was changed after that upload. The original text of the preferred locations is kept in the candidate's notes.

The database part is migration `e7b31f08a5c2` (`candidates.salesforce_id`, unique when set, `salesforce_synced_at`, `salesforce_snapshot`; `resumes.source`, `resumes.salesforce_content_version_id`). Run `alembic upgrade head` before starting an API or worker that has this code.

## Searching and assigning

1. A Team Lead (or Admin) picks candidates, optionally overrides the search criteria, and chooses **Find & Assign Matching Jobs** or **Search first, review, then assign**. The result count is per candidate. A candidate needs no responsible member to be searched.
2. The API stores a search run and returns its id at once. The worker does the work; it continues if the browser is closed.
3. For each candidate the existing search engine runs with that candidate's current resume and preferences: local index first, live sources, AI web search (deep search), verification, US filter, authorization filters, deduplication, ranking. The run records which resume version it used.
4. Results are saved as global job postings (one row per vacancy) plus per-candidate results. In auto-assign mode, jobs scoring at least the minimum match score become assignments. A job is never assigned twice to the same candidate; the same job can go to several candidates who each qualify. The member responsible for a candidate is notified about new jobs for that candidate.
5. Progress (sources read, postings examined, verified, duplicates, matched, assigned, warnings) is written to the run about once a second and shown in the progress window. If 50 jobs were requested and 18 qualify, 18 is what is shown.

**Fresh postings only ("Posted within").** Every search has a posting-age limit: Last 24 hours, 3, 7 (the default), 14 or 30 days, or Any time (`filters.posted_within_days`: 1, 3, 7, 14, 30, 0), and a switch "Also show jobs with no posting date" (`filters.include_undated`, on by default). The rules (`app/freshness.py`):

- A job whose **known** posting date is older than the limit is never delivered. It is screened out before any model ranks, judges or explains it, and the rounds of a search count only jobs that can be delivered. The search reports how many were left out (`targets[].discovery.too_old`, shown under "Where the jobs came from", and per source in `age_by_source`).
- A job with **no known date** is delivered only while the switch is on, is listed after dated jobs of the same match score (a tie-break, never a score change) and is marked "Posting date not stated". A copy without a date (an employer's career page) is not delivered in place of the same vacancy's dated record when that record was too old.
- A date is never invented. It is the publish date the source gives: Greenhouse `first_published`, Ashby `publishedAt`, Lever `createdAt` (creation; the only date its public API has), Workable `published_on` / `created`, Recruitee `published_at`, SmartRecruiters `releasedDate`, Oracle `PostedDate`, Workday `startDate` and the list's "Posted N Days Ago", Amazon `posted_date`, schema.org `datePosted` on a page, or words the page itself states ("Posted 3 days ago", "Date posted: November 20, 2026"; the AI page check may only point at such words, which are then checked to be on the page). A "last updated" date is never shown as a posting date: like "Posted 30+ Days Ago" it only proves that a posting is at least that old. A date in the future or more than ten years back counts as unknown.
- Days are calendar days and "today" is the day in the last US time zone (UTC-10), so a posting of the 20th is within a 7-day limit through the whole of the 27th anywhere in the United States.
- Discovery aims at fresh postings without hiding others: the AI web-search calls are told the limit and asked to put recently posted results first and to keep results that show no date; SearXNG is asked with `time_range` on every other query (`SEARXNG_TIME_RANGE`) and a restricted query that brings nothing usable is asked again without it.
- Searches and presets stored before 2026-11-27 have no limit stored: they load and run with 7 days and undated jobs included. Measured on 2026-11-27 ("Data Engineer", 25 jobs, 7 days): of about 1,570 verified matching postings 86 % were older than 7 days; the 25 delivered were 22 dated within the week and 3 without a date (before: 5 of 25 within the week, the oldest 127 days).

An Admin can also add a job by hand, import one from a URL (reviewed before saving), import a CSV, delegate jobs to a team's queue, or assign directly to candidates of any team. A manually entered job is marked **unverified** until its page has been checked.

## Application tracking

Statuses: `ASSIGNED → VIEWED → APPLYING → APPLIED → INTERVIEWING → OFFER`, plus `REJECTED`, `WITHDRAWN`, `SKIPPED`.

An assignment is a job given to a candidate; the member responsible for that candidate works on it. Opening a job page is **not** applying. "Open Job" goes through an audited redirect that records who opened which job and when. The member marks the application as submitted separately (optionally with a reference number and the real date). Mistakes can be corrected; the earlier state stays in the history.

### Metric definitions (identical on every dashboard)

| Metric | Definition |
| --- | --- |
| Assigned | Active assignments |
| Applied | Assignments with a confirmed submission, whatever happened later |
| Not applied | Active assignments without a confirmed submission, excluding skipped and withdrawn ones |
| Interviews / Offers | Assignments that reached that stage |
| Rejected / Closed | Status `REJECTED`, `WITHDRAWN` or `SKIPPED` |
| Completion rate | Applied ÷ (assigned − skipped − withdrawn before applying) |
| Link opens | Times "Open Job" was used; never counted as applied |

A member's job list is shown in sections, by status: **To apply** (`ASSIGNED`, `VIEWED`, `APPLYING`; the same count as "Not applied"), **Applied** (`APPLIED`), **In progress** (`INTERVIEWING`, `OFFER`) and **Closed** (`REJECTED`, `WITHDRAWN`, `SKIPPED`). Every job is in exactly one section; `GET /api/assignments/sections` gives their sizes for the caller's scope and the list's `candidate_id` / `q` filters. "Mark as applied" on a card moves the job at once and can be undone from the confirmation; the correction is kept in the job's history.

A member's numbers are the totals over the candidates assigned to that member at the moment; team and organization numbers also include candidates nobody is responsible for. Team and organization dashboards break the numbers down per candidate and per responsible member. A date range selects assignments by the day they were assigned (UTC). Charts show, per day or week, jobs assigned, applications confirmed and links opened.

## AI auto-apply (Admin only, off by default)

An Admin can let the worker fill a candidate's application on the employer's own hosted form (Greenhouse, Lever,
Ashby, Workable; Workday and aggregators are refused with a reason). Every run first does a **dry run** in a browser
whose submissions are blocked (request routing plus the guard proxy), shows the Admin each field, the value, its
source and a screenshot, and submits only after the Admin approves that exact preview. CAPTCHAs, sign-ins, e-mail
codes, bot checks, "I personally completed this application" and arbitration agreements always go back to a person.
Legal and EEO questions are answered only from answers recorded for the candidate (`/application-answers`); EEO
defaults to the form's decline option; free text is drafted from the resume and rejected if it states anything the
resume does not. It needs a recorded consent. Switch on with `AUTO_APPLY_ENABLED=true` and run
`alembic upgrade head` (revision `d3f6a1b8c925`). Contract and rules: [docs/auto-apply-api.md](docs/auto-apply-api.md).

## Security

- **Passwords:** Argon2id, computed in worker threads so a sign-in never stalls other requests. **Sessions:** random token in an HttpOnly, SameSite=Lax cookie; only its hash is stored; validated on the server for every request; revoked on sign-out, password reset and deactivation. Set `COOKIE_SECURE=true` (or `APP_ENV=production`) when served over HTTPS.
- **CSRF:** state-changing requests need the `X-CSRF-Token` header matching the session, and the `Origin` must be the app's own. The two GET requests that record something (opening an assignment, Open Job) do not record when the browser reports a cross-site request.
- **Sign-in throttling**, counted in Redis: an account is locked for 15 minutes after 5 failed sign-ins. See "Client addresses" below for the finer limits that become available behind a reverse proxy.
- **Authorization** is enforced in the API on every endpoint and query, never only in the UI. A Team Lead's queries are limited to the team they lead today (a lead who is moved or removed loses the old team's members, candidates, assignments and searches at once); a member's to the candidates assigned to them at that moment and those candidates' jobs. A candidate outside the caller's scope answers exactly like one that does not exist. Client-supplied team or member ids are checked against the session, not trusted.
- **One-time links** (invitations, password resets) are stored as hashes, expire, work once, and die early when the manager who issued them is deactivated or no longer manages that person, when the person moves to another team, and when a newer link or a password change replaces them. A link that was e-mailed is not also shown to the manager who requested it.
- **Job links for members:** raw URLs are not sent to members at all, and web addresses written inside a posting's text are removed from the member's copy. The redirect target is read from the stored job, must be a public http(s) address, and cannot be changed by request parameters.
- **What a member receives about other people:** of colleagues, names only; of the candidates assigned to them, the details needed to apply (e-mail, phone, notes, profile, resume). Sign-in e-mail addresses of managers, private notes (including their existence) and manager-only events are left out.
- **Outbound requests** (page verification, URL import, headless browser) are allowed only to globally routable addresses, and the connection is made to the address that was checked, so a host name cannot answer differently the second time (DNS rebinding). The headless browser reaches the network only through a local guard proxy.
- **Uploads and request sizes:** a request without a session is refused before its body is read; JSON bodies are limited to 512 KB, resumes to 10 MB, CSV imports to 2 MB. PDF and DOCX resumes are parsed in a separate process limited to 1 GB of memory, 10 s of CPU and 20 s of run time, and a file whose content inflates beyond the upload limit is refused, so a "decompression bomb" costs about a tenth of a second.
- **Limits against abuse by a signed-in account:** at most 3 queued or running searches per person and 12 in total, 5 invitations per address and 60 per sender per hour, 120 job-title suggestions while typing and 20 "from the resume" requests per person per minute. A search stops assigning as soon as the person who started it is deactivated or no longer manages that team.
- **Audit log:** sign-ins, invitations, account changes, assignments, status changes, searches, settings. Request logs never hold tokens, query strings or bodies, and SQL parameters are kept out of error logs.
- **API documentation** (`/api/docs`, `/api/openapi.json`) is switched off when `APP_ENV=production` (`API_DOCS=true` turns it back on).
- **Resumes:** the extracted text, the structured profile and the uploaded file are stored in PostgreSQL. Only managers upload or delete; the file is downloadable by the people who may see the candidate, always as an attachment with a content type fixed by the server per extension, `X-Content-Type-Options: nosniff` and a sanitised file name, so a browser never renders it. Replacing a resume erases the text and the file of the previous version. Resume text is sent to the configured AI providers when a search runs for that candidate; the UI says so. Resume and posting text are passed to models as untrusted data.

The rules above were checked by an adversarial review on 2026-11-17; its 23 findings and their fixes are pinned by the 30 tests of `tests/portal/test_security_review.py`; further rules are in `tests/portal/test_hardening.py`. `tests/portal/test_email_delivery.py` runs the real mail sender against an SMTP server on the test machine: delivery, refusals, and the certificate checks.

### Client addresses

The browser talks to the Next.js app, which forwards `/api/*` to the API. Next.js passes an `X-Forwarded-For` header it receives on unchanged, so without anything in front of it that header is whatever the caller sent (measured on this installation). The API therefore ignores it by default: the audit trail shows the web app's address, and sign-in limits are per account only.

For production put a TLS-terminating reverse proxy or load balancer in front of the web app (needed for HTTPS anyway), make it set `X-Forwarded-For` itself, and tell the API:

```
APP_ENV=production
TRUST_FORWARDED_FOR=true
TRUSTED_PROXY_HOPS=1        # number of such proxies; the entry read is counted from the right
```

Then the audit trail holds real client addresses and the limits become: 5 failures per account and address, 30 per address, 20 per account from all addresses, each in 15 minutes. Do not set `TRUST_FORWARDED_FOR` when the web app is reachable directly; anyone could then choose the address they are counted under.

### Accepted limits

- An authenticated manager can tell whether an e-mail address already has an account (inviting it is refused). The message does not say in which team or role.
- With no trusted proxy, anyone who knows an account's e-mail address can keep that account locked by failing to sign in 5 times every 15 minutes.
- The resume parser runs with limited resources but as the same operating-system user; it is not a full sandbox.
- A name may still carry up to 100 characters of plain words into an invitation e-mail (no line breaks, no links).

### What the link and screenshot measures can and cannot do

The app discourages redistribution: members get no copyable URLs, every link opening is logged against the member, and member job views carry a faint watermark with the member's e-mail and the time (an Admin can switch it off). These are deterrents. Once the employer's page is open its address is visible in the browser, and no web application can stop screenshots, screen recording or a phone camera. The app does not pretend otherwise.

## Deployment (CI/CD)

`.github/workflows/ci-cd.yml` runs on every push and pull request: the backend tests against PostgreSQL and Redis, and
the web app's lint, type check and production build. A push to `main` that passes both builds the two images for
arm64, stores them in the GitHub container registry (`ghcr.io/namanjain221995/jobsearch-backend` and `-frontend`,
tagged with the commit id) and releases them on the server.

The server runs `deploy/docker-compose.prod.yml` in `/opt/jobfinder`: PostgreSQL, Redis, migrations, API, worker, web
app and a Cloudflare Tunnel. No port is open to the internet for the application; people reach it through the
tunnel, whose public hostname points at `http://frontend:3000`.

- The pipeline signs in with its own SSH key, which the server binds to `deploy/deploy.sh` and nothing else. The
  script pulls the images, starts them, and waits until `/api/health` reports the database, Redis and the worker.
  When that does not happen within three minutes it starts the previous build again and the pipeline fails.
  Database migrations are not undone by that. Before a release the script writes a database dump to
  `/opt/jobfinder/backups`; when a migration changed tables the previous build needs (the candidate migration
  `c4d2a9e71f36` renames columns), going back means `alembic downgrade` to the earlier revision or restoring that
  dump, by hand.
- Repository secrets: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`. The server's SSH port must
  accept connections from GitHub's runners.
- On the server, never in git: `/opt/jobfinder/.env` (API keys, `POSTGRES_PASSWORD`, `PUBLIC_BASE_URL`) and
  `/opt/jobfinder/tunnel.env` (`TUNNEL_TOKEN`). After changing either: `cd /opt/jobfinder && docker compose up -d`.
- `PUBLIC_BASE_URL` must be the address people use (for example `https://jobs.example.com`); invitation and reset
  links are built from it.
- `deploy/backup.sh` writes a compressed database dump to `/opt/jobfinder/backups` every night (systemd timer
  `jobfinder-backup.timer`) and keeps 14 days. The dumps are on the same disk as the database.
- Go back to an earlier build by hand: Actions, the run of that commit, "Re-run all jobs".

## Job sources

Greenhouse, Ashby and Lever employer boards (about 420 seed employers plus boards learned from discovery), Workday (159 large employers, rate-limited by Workday), Workable search and Workable employer accounts found by discovery, Amazon Jobs, Remote OK, Jobicy, Himalayas, OpenAI web search and Claude web search with verification, a headless browser for pages that need JavaScript, and Adzuna (optional key, not live-tested). LinkedIn, Indeed, Glassdoor and ZipRecruiter are not scraped: their terms forbid it and the app does not bypass blocks. An Admin can switch sources off under Settings → Integrations.

**Board discovery from Common Crawl.** `python -m app.portal.import_boards --common-crawl [N]` (default 2 crawls) reads the Common Crawl URL index of the N most recent crawls for `job-boards.greenhouse.io`, `boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`, `apply.workable.com` and `*.myworkdayjobs.com`, keeps only board ids, and registers new ones with origin `commoncrawl` through the same dedupe and `register_catalog` path as the CSV import; the paced catalogue trickle then reads them. The CDX API at `index.commoncrawl.org/CC-MAIN-*-index` is disallowed by that host's robots.txt, so the tool reads the same index shards from `data.commoncrawl.org` (whose robots.txt welcomes HTTPS downloads) by byte range: one request at a time, a named User-Agent, exponential backoff with jitter up to 5 minutes, resume per (crawl, pattern, page) from `~/.cache/jobsearch/common_crawl_state.json` (`--cc-state`), a progress line per page, and `--cc-max-pages M` as a cap. One crawl (CC-MAIN-2026-43) took 2 min 17 s and found 3,859 Greenhouse, 264 Lever, 1,054 Ashby, 3,818 Workable and 2,918 Workday board ids.

**SearXNG engines.** The production SearXNG (`configs.searxng_settings` in `deploy/docker-compose.prod.yml`) keeps only the web engines brave, mojeek, qwant, startpage, yandex, google, bing and duckduckgo, with 5–6 s per-engine timeouts; image, video, news, music and other default engines are dropped, and the app asks for `categories=general`. Engines that answer a server address with a CAPTCHA or 403 are reported as unresponsive and skipped; nothing tries to get around that.

#### Free search engines considered

- **SearXNG**: used (self-hosted meta-search, JSON API).
- **Common Crawl index**: used for board discovery (open data, read from `data.commoncrawl.org`).
- **Whoogle, 4get, LibreY**: rejected. They fetch Google or Bing result pages directly from our server, which those engines' terms forbid and which gets blocked; SearXNG already covers meta-search.
- **YaCy, Stract**: rejected. They run their own crawler and index, need far more memory and disk than the server has, and would take months to index job boards that the ATS feeds already give directly.

### Finding more per search

A search asks for up to 1,000 jobs per candidate and returns what really exists: only postings read from an employer's board API or a verified page, never a filler. Five things widen what one search finds; each is a setting and each can be switched off (`.env.example` lists every variable).

1. **Title variants** (`TITLE_EXPANSION`). One model call per search proposes other titles employers use for the same work ("Data Engineer": "Analytics Engineer", "ETL Developer", "Big Data Engineer"). They are validated (plain short titles, no duplicates, at most `TITLE_EXPANSION_MAX`), cached for a week, and used only to find postings: a variant match counts 85 % of a requested-title match and every posting is still screened and scored against the candidate. Each result records which title it was found under (`matched_query`). Seniority words are not variants: matching has always ignored them ("Senior Data Engineer" matches "Data Engineer").
2. **Search-engine discovery** (`SEARXNG_URL`, source name `searxng`). For each title and each ATS host the app can read, one paced `site:` query goes to a SearXNG instance. Only result *addresses* are used. An address of an employer board that the index does not know is registered and read through the board's own API, exactly like a seed board, and is read again by every later search; an address of a single posting is checked against its live board (or, for SmartRecruiters, its page). Results on hosts outside the configured ATS list are ignored, and LinkedIn, Indeed, Glassdoor, ZipRecruiter and similar are never followed even if someone configures them. Identical queries are answered from the index for `SEARXNG_CACHE_HOURS`. When the instance is slow, empty or down the search goes on without it and says so. No two search engines send the same query in one job search: SearXNG, the company model's web search, TinyFish and AWS claim a query (normalised text and host) in one record of the search before they send it. The company model's web search asks the queries SearXNG will not reach, and once more those SearXNG failed on or found nothing for; when SearXNG is off or stops answering it takes SearXNG's place.
3. **AI web search at full strength on both jobs** (`OPENAI_SEARCH_CALLS`, `CLAUDE_SEARCH_CALLS`, `OPENAI_OPEN_WEB_CALLS`, `CLAUDE_OPEN_WEB_CALLS`). In every round OpenAI and Claude each make calls on the ATS hosts (10 each, 5 searches a call) and, **on top of them**, calls on **employers' own career sites** on any domain (`careers.company.com`, `company.com/careers`, `jobs.company.com`; OpenAI 8, Claude 10, 10 searches a call), all side by side; nothing is divided between the two jobs. (Raised on 2026-11-27 from 6 + 6 and 3 + 6; the first round now also searches the title variants of point 1, so the extra calls ask for further titles and further kinds of employer, never the same search twice.) When the first open-web calls of a round have returned, up to `AI_OPEN_WEB_FOLLOWUP_CALLS` (6) further calls search the career sites they found postings on once more, held to exactly those sites (first the sites on which a posting with a fitting title was verified before). These numbers are what one round makes for 50 results or fewer (a larger request up to twice as much a round); they are not budgets: every further round makes them again with new searches until the target is reached or rounds stop bringing new verified jobs, and the `*_MAX` settings are safety ceilings only. The calls run on the cheapest current model of each provider (`OPENAI_SEARCH_MODEL=gpt-6-luna`, `ANTHROPIC_SEARCH_MODEL=claude-haiku-5-5`): on the same searches the larger models verified no more postings, because the addresses come from the search tool. One round is about 310 web searches (measured 2026-11-27, "Data Engineer", 25 jobs: OpenAI 126 to 130, Claude 184 to 185, no call failed or was rate-limited; at the 0.01 USD a search measured on 2026-11-21 that is about 3 USD of search fees a round). Every page lead the calls return is opened and checked: `MAX_PAGE_VERIFICATIONS` is 1200 a round and the lead check has `LEAD_VERIFICATION_SECONDS` (240) for it; a site that refuses two pages is not asked for more. `AI_OPEN_WEB_SHARE=0` still switches everything about the open web off; `AI_OPEN_WEB_SPLIT` is no longer used. A call on the ATS hosts is given its own web searches, one `site:host "title" place` search per unit of its budget (the requested titles on every host first, then the candidate's preferred states and "remote", then the title variants), so no search is asked twice in one job search, by either provider, in any round; it is restricted to the hosts of its searches, which are the hosts the search can use (board readers that are switched on, platforms whose postings are verified page by page). A call on employers' own sites is given one kind of employer that runs its own career system (large technology companies; banks and insurers; hospital systems and health companies; retailers; manufacturers and energy; government contractors and consulting; telecom, media and logistics; universities and public employers), chooses that many employers itself, and searches each one's career site for the candidate's titles (Claude's search tool holds a `site:` query to its site; OpenAI's does not, so for OpenAI the model first names the career sites in a call of its own and the search is then restricted to exactly those sites); it blocks the ATS hosts the other calls search, the career platforms that may not be read (iCIMS, SAP SuccessFactors, BambooHR, a reader that is switched off) and the job boards, and it is told which employers the engine already reads and which career sites earlier calls already brought postings from. Under an ATS platform filter no call looks at the open web: only the chosen platforms' hosts are searched. Only addresses the search tool itself returned become leads: an address the model merely wrote is dropped, job boards and aggregators never become leads, and neither does an address on a platform that may not be read (it reaches neither a board reader nor the page verifier). Of an open-web call the leads are the direct posting pages the model picked plus further postings on the career sites of that call; a page the search tool dates older than 60 days is handed on last, because search indexes keep postings long after they close (when measured, 20 of 386 such pages were still open, against 21 of 38 pages up to 30 days old). What needs a page check is handed on up to 60 a call, the models' own picks of every call first, and the search's page-verification budget (`MAX_PAGE_VERIFICATIONS`) decides how many are opened. A provider that fails twice in a row (`AI_DISCOVERY_BREAKER_FAILURES`; a wrong key or used-up quota: at once) is switched off for the rest of that job search and the other one takes over its searches; a call still running at the time limit is dropped and the others keep their leads. Employer boards and career pages they name go through the same registration path.
4. **Rounds: a search keeps going.** After the first pass, while fewer verified jobs at or above the minimum match score (by keyword score) than requested exist, another round starts with the next title variants, locations, search-engine queries and AI calls, and from round 2 on the paid sources. A search stops when the target is reached, when `SEARCH_STALE_ROUNDS` (2) rounds in a row add no new such job, when every discovery budget of the search is used up, at `SEARCH_MAX_SECONDS` (3 hours), at the safety limit `SEARCH_MAX_ROUNDS`, or when it is cancelled. The reason is stored with the run (`targets[].discovery.stop_reason`), written into the progress events ("Search stopped after n round(s): ...") and shown as a note. The progress window shows "Round n" and the running totals. Per-stage caps (detail fetches, page checks, audited and AI-judged jobs, boards per search) scale with the request up to their `*_MAX` ceilings. The target is counted before the final ranking: after OpenAI / Claude and the company model have judged the jobs, fewer may remain above the minimum score than the search asked for.
5. **Ranking large lists.** Every job gets the keyword score. With the company model (`TECHSARA_API_KEY`) its reranker then reads every job against the profile, OpenAI / Claude judge the top slice (40 % of the request, 120 to `MAX_AI_RANKED_JOBS_MAX`) with a written explanation, and the company chat model writes explanations for the next `TECHSARA_EXPLAIN_MAX_JOBS`. Without the company model the judged slice grows the same way and the rest keep the keyword score. Each result says how it was scored (`score_source`: `ai`, `company_model` or `heuristic`, and a sentence in the explanation).

6. **Paid sources, after the free ones** (source names `serpapi`, `tinyfish`; both wait for round 2).
   - *Google Jobs through SerpApi* (`serpapi_KEY`). A licensed listing API with a monthly allowance (free plan: 250 searches). Every call that is not answered from the app's own cache is counted per calendar month, the account endpoint (free) is read every five minutes, `SERPAPI_MONTHLY_RESERVE` calls are kept back, and a search says so when SerpApi was skipped. Queries are title variants x the candidate's preferred states; a query is paged deeper only while a page brings new jobs. An apply link on an ATS the app reads or on the employer's own site is verified like any other lead. Pages of job boards and aggregators (`AGGREGATOR_HOSTS`) are never fetched. A job that only has job-board links is returned from the API's own data, stored as **Not verified** with the note "Listed on Google Jobs via ... (SerpApi API); not checked on the employer's site" (`SERPAPI_INCLUDE_LISTED_ONLY=false` drops such jobs).
   - *TinyFish* (`tinyfish_api_key`). TinySearch is a second discovery engine beside SearXNG (same queries, same handling of result addresses); TinyFetch reads a page our own fetch and browser could not turn into a posting, never one whose site refused an automated browser. Spending is computed from the per-call prices, limited per UTC day (`TINYFISH_DAILY_USD`) and stopped when the wallet is below `TINYFISH_WALLET_RESERVE_USD`. Its agent, research, browser and vault endpoints are never called.

7. **AWS web search** (source name `aws_web_search`, `AWS_WEB_SEARCH_ENABLED`, off by default). AWS Bedrock AgentCore Web Search (`bedrock-agentcore` `InvokeWebSearch`, boto3, credentials from the EC2 instance profile) is a discovery source in *every* search: round 1 and every later round while the target is not reached. Each call is one query: a title or title variant x a US location (the candidate's preferred states first, "remote" first for remote candidates) with the work-mode word, restricted by `domainFilter.include` to one group of the ATS hosts the app reads (Greenhouse/Lever/Ashby, then Workable/Workday/SmartRecruiters), and every third call unrestricted except for the job boards in `AGGREGATOR_HOSTS` (`exclude`; include and exclude are never sent together). `maxResults` is 25 (the API's maximum) and `publishedDateFilter.publishedAfter` is `AWS_WEB_SEARCH_PUBLISHED_DAYS` (60) days back. Only result **addresses** are used, exactly like SearXNG results: a posting or board on a readable ATS is checked against the employer's live board API (a posting the board no longer lists is dropped as unverified), an employer page from the unrestricted query must prove itself with the page's own posting data, and search, category, board-listing and aggregator pages are never vacancies (counted as `aws_generic`). Result addresses are deduplicated per search, and the jobs then go through the same screening, deduplication and ranking as every other source.
   - *Allowance*: `AWS_WEB_SEARCH_MONTHLY_LIMIT` (4,000) queries per calendar month (UTC), counted in the PostgreSQL table `aws_web_search_usage` **before** each attempt by one conditional `UPDATE ... WHERE used < ceiling`, so the API and every worker together can never exceed it. botocore's own retries are off; the app retries throttling, 5xx and timeouts itself (`AWS_WEB_SEARCH_MAX_ATTEMPTS`, 3) and every attempt counts. Background searches stop at limit minus `AWS_WEB_SEARCH_MONTHLY_RESERVE` (300, kept for the managers' endpoint). One candidate search may make `AWS_WEB_SEARCH_MAX_CALLS_PER_SEARCH` (4, for 50 results; scaled with larger requests up to `..._MAX`, 10) queries, handed out over the rounds, and fewer when the month runs low: the cap is lowered to the remaining allowance divided by `AWS_WEB_SEARCH_EXPECTED_SEARCHES_PER_DAY` (45) x the working days left (at least 1 while anything remains). The progress statistics show `aws_queries`, `aws_budget` and `aws_skipped` (for example when the month is used up); Integrations shows enabled, region and the month's usage (read from the counter; the health check never calls AWS).
   - *Data use* (AWS Web Search restrictions): AWS result titles, text extracts and rankings are used inside one search or one request only, in memory. They are never written to the database, the job index, a cache or a log (log lines carry counts and timings, never the query or the results), and are not used to build a job database. What is stored is what the app stores from every source: the employer's own posting as read from the employer's feed, with the employer's address as its source URL; its provenance may say "AWS web search", and a board it found is recorded with origin `aws_web_search`. A result that cannot be verified against the employer's live feed is dropped.
   - *Managers' endpoint* `POST /api/web-search/jobs` (Admin and Team Lead): `{keywords, locations (up to 5 US cities/states), work_mode: remote|hybrid|onsite|any, limit 1-50 (20), candidate_id?}` runs up to `AWS_WEB_SEARCH_ENDPOINT_MAX_CALLS` (6) queries, verifies every hit against the employer's feed, and returns verified jobs (`discovered_via: "aws_web_search"`) with statistics (queries used, results seen, dropped as generic / unverifiable, monthly allowance left). With `candidate_id` (a candidate in the caller's scope; otherwise 404) the jobs are scored against that candidate's resume and profile with the engine's keyword rubric. Per user: `AWS_WEB_SEARCH_USER_PER_MINUTE` (3) and `AWS_WEB_SEARCH_USER_PER_DAY` (40). Nothing is saved; a manager adds a result to a candidate through the existing job and assignment endpoints.
   - *Setup*: the instance role needs `bedrock-agentcore:InvokeWebSearch` on `arn:aws:bedrock-agentcore:<region>:aws:tool/web-search.v1`; `boto3>=1.44.5` is in `requirements.txt`; the counter table comes with `alembic upgrade head` (revision `b5e8d2c47a19`).

**One scale.** All scores are on the 0 to 100 rubric (skills 35, title 25, experience 20, arrangement 10, additional 10). The reranker's relevance (0 to 1, measured: most jobs below 0.05, strong matches near 1) is never used as a score. In each search it is calibrated against jobs that carry a rubric score: the jobs OpenAI / Claude judged (a sample of them is deliberately taken from across the whole list, not only the top), or the keyword scores when no AI provider is configured. The fit is monotone and never leaves the range the reference scorer used, so a job passes the minimum match score only when jobs of the same relevance were scored above it by the reference scorer.

**What more costs** (measured live on 2026-11-18 on a development machine against a copy of the index, Workday switched off; one candidate each; the QA and Data Engineer searches reached their target in the first round, the second Salesforce search used every round and every budget and still found fewer than 300, which is what it returned):

| Search | Returned | At or above score 50 after judging | Time | OpenAI | Claude | Company model |
| --- | --- | --- | --- | --- | --- | --- |
| "QA Engineer", 300, earlier single-pass engine | 86 | 81 | 66 s | 18 calls, 93k in / 18k out, 5 web searches | 4 calls, 70k in / 5k out, 6 web searches | not used |
| "QA Engineer", 300, everything on | 300 | 291 | 410 s | 24 calls, 166k in / 27k out, 13 web searches | 4 calls, 113k in / 11k out, 20 web searches | 90 calls, 1.07M tokens |
| "Salesforce Developer", 300, earlier engine | 69 | 58 | 137 s | 15 calls, 76k in / 14k out | 4 calls, 98k in / 7k out | not used |
| "Salesforce Developer", 300, everything on, 5 rounds, with SerpApi (8 calls) and TinyFish (0.11 USD) | 228 | 104 | 551 s | 33 calls, 309k in / 31k out, 25 web searches | 13 calls, 312k in / 28k out, 75 web searches | 37 calls, 0.29M tokens |
| "Data Engineer", 1,000, everything on | 1,000 | 639 | 541 s | 72 calls, 304k in / 73k out, 12 web searches | 5 calls, 135k in / 17k out, 18 web searches | 68 calls, 1.11M tokens |

Token counts are what the providers reported. At Anthropic's list price for `claude-opus-5-5` (4 USD per million input tokens, 20 USD per million output tokens; web-search fees come on top and are not included) the Claude part was about 0.38 USD for the earlier engine, 0.68 USD for the one-round 300 search, 1.80 USD for the five-round 300 search and 0.88 USD for the 1,000 search. The OpenAI part is the token counts above times the price of the configured model, plus its web-search fee. The company model has no per-call bill; it is the slow part: its reranker reads about 18 jobs a second (1,000 jobs: about a minute) and its explanations take up to `TECHSARA_EXPLAIN_SECONDS`. A search that needs all its rounds spends up to four times the discovery calls of one round (300 results: 12 OpenAI and 12 Claude web-search calls instead of 3 each; 1,000 results: 16 each); the ceilings are `OPENAI_SEARCH_CALLS_MAX`, `CLAUDE_SEARCH_CALLS_MAX`, `AI_WEB_SEARCHES_PER_CALL_MAX`, `MAX_AI_RANKED_JOBS_MAX` and `MAX_AI_CALLS_PER_SEARCH_MAX`. SearXNG costs nothing but time (one query every two seconds). TinyFish costs at most 0.11 USD per candidate search at the defaults, SerpApi at most 8 of the month's 250 calls. Memory of the search process stayed below 450 MB in every run.

A possible further source, not built: the USAJOBS API (`https://data.usajobs.gov/api/search`) for federal jobs; it needs a free API key.

**Limits.** `SEARCH_MAX_RESULTS` (default 1000) is the ceiling per candidate; the search form reads it from `GET /api/searches/options` (`limits.max_target_count`). The Admin setting "results per search run" (`MAX_RESULTS_PER_SEARCH_RUN`, default 500, up to 5000) still limits candidates x results in one run, so a 1,000-job search for one candidate needs it at 1000 or more.

## Configuration

See `.env.example` for every variable. The ones you are most likely to set:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL`, `REDIS_URL` | PostgreSQL and Redis |
| `PUBLIC_BASE_URL` | Address of the web app (links in invitations, same-origin check) |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_MODEL`, `ANTHROPIC_MODEL` | AI providers (the app also works with none) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_STARTTLS` | E-mail delivery for invitations and resets (STARTTLS, or TLS from the start on port 465; `SSL_CERT_FILE` names a private certificate authority) |
| `COOKIE_SECURE`, `APP_ENV` | Secure cookies in production; production also switches the API docs off |
| `TRUST_FORWARDED_FOR`, `TRUSTED_PROXY_HOPS` | Believe `X-Forwarded-For` (only behind a reverse proxy that sets it; see Security) |
| `MAX_ACTIVE_SEARCHES_PER_USER`, `MAX_ACTIVE_SEARCHES` | Queued and running searches per person (3) and in total (12) |
| `MAX_MEMBERS_PER_SEARCH`, `MAX_RESULTS_PER_SEARCH_RUN`, `MAX_AI_CALLS_PER_SEARCH` | Cost and concurrency limits (`MAX_MEMBERS_PER_SEARCH` counts candidates per search; the name is historical) |
| `SEARCH_MAX_RESULTS` | Most results per candidate (default and maximum 1000) |
| `TECHSARA_API_KEY`, `TECHSARA_BASE_URL`, `TECHSARA_MODEL`, `TECHSARA_RERANK_MODEL` | The company model service: title variants, page extraction, scoring and explaining large lists. Empty key = not used |
| `SEARXNG_URL` | SearXNG instance for search-engine discovery (JSON API enabled). Empty = off |
| `AWS_WEB_SEARCH_ENABLED`, `AWS_WEB_SEARCH_REGION`, `AWS_WEB_SEARCH_MONTHLY_LIMIT`, `AWS_WEB_SEARCH_MONTHLY_RESERVE`, `AWS_WEB_SEARCH_MAX_CALLS_PER_SEARCH` | AWS Bedrock AgentCore Web Search (off by default; instance-profile credentials; 4,000 queries a month, 300 kept for the managers' endpoint, 4 per candidate search) |
| `TITLE_EXPANSION`, `AI_BUDGET_SCALING`, `SEARCH_MAX_ROUNDS` | Switches for title variants, scaled AI budgets and rounds (`false`, `false`, `1` give the earlier single-pass search) |
| `OPENAI_SEARCH_CALLS_MAX`, `CLAUDE_SEARCH_CALLS_MAX`, `AI_WEB_SEARCHES_PER_CALL_MAX`, `MAX_AI_RANKED_JOBS_MAX`, `MAX_AI_CALLS_PER_SEARCH_MAX` | Hard ceilings of what one large search may spend on OpenAI / Claude (see "What more costs") |
| `OPENAI_SEARCH_MODEL`, `ANTHROPIC_SEARCH_MODEL`, `OPENAI_SEARCH_CONCURRENCY`, `CLAUDE_SEARCH_CONCURRENCY`, `AI_DISCOVERY_CALL_TIMEOUT_SECONDS`, `AI_DISCOVERY_DEADLINE_SECONDS`, `AI_DISCOVERY_BREAKER_FAILURES`, `AI_OPEN_WEB_SHARE`, `AI_OPEN_WEB_SPLIT` | AI web search per provider: a model for discovery only (empty = the main model), calls at the same time (4), time limit of one call (100 s) and of a round (120 s), failures in a row that switch a provider off (2), the share of the calls that look at employers' own career sites instead of the ATS hosts (0.5; 0 = none, 1 = all; always none under an ATS platform filter), and who makes them (`auto`: Claude first, OpenAI keeps the ATS hosts; `even`: each provider alternates) |
| `TECHSARA_WEB_SEARCHES_BASE`, `TECHSARA_WEB_SEARCHES_PER_SEARCH`, `TECHSARA_WEB_SEARCH_CONCURRENCY` | The company model's web search: searches for 50 results or fewer (12), ceiling per job search (40), searches at the same time (2) |
| `SEARXNG_QUERIES_PER_SEARCH_MAX`, `SEARXNG_NEW_BOARDS_PER_SEARCH_MAX`, `SEARXNG_MIN_INTERVAL_SECONDS` | Search-engine discovery: queries and new boards per search, pace |
| `API_ORIGIN` (frontend) | Where Next.js forwards `/api/*` (default `http://127.0.0.1:8010`) |
| `SF_CLIENT_ID`, `SF_USERNAME`, `SF_LOGIN_URL`, `PRIVATE_KEY_B64` | Salesforce, read-only (see "Salesforce (read-only)"); empty = off |
| `ANALYTICS_CACHE_SECONDS` | Lifetime of cached team and organization analytics in Redis (default 60, 0 turns the cache off) |
| `LOG_FORMAT`, `LOG_LEVEL` | `json` (default, one object per line) or `text`; `INFO` by default |

Limits, the watermark, the default minimum match score and source switches can also be changed by an Admin in the app.

## Tests

```bash
.venv/bin/python -m pytest -q                 # engine + portal API tests (needs the PostgreSQL container)
```

Portal tests run against a real PostgreSQL database (`jobfinder_test`) built from the Alembic migrations. Searches in tests use the real engine and background runner; only the network is replaced by synthetic fixtures in `tests/`.

A deterministic demo stack for UI work (own database `jobfinder_e2e`, synthetic postings, no network, no AI cost):

```bash
.venv/bin/python -m tests.e2e.server --reset                      # API on 127.0.0.1:8011 with demo data
cd frontend && API_ORIGIN=http://127.0.0.1:8011 npm run dev -- --port 3011
```

### Browser tests

```bash
tests/e2e/run.sh                # everything, about 20 minutes; or: acceptance | crawl | checks
```

The script builds the web app against the demo API (into `frontend/.next-e2e`, beside the normal build), starts both on ports 8011 and 3011 with freshly seeded data, drives a real Chromium browser, and stops everything again. It never touches the real database or the real servers. External hosts are made unreachable for the browser, so no employer site is contacted. Salesforce in the demo stack is an in-process fake (`tests/e2e/salesforce_fake.py`, synthetic people); the real one is never contacted.

- `tests/e2e/test_acceptance.py`: the seven acceptance scenarios of the brief and an eighth for Salesforce, step by step in the page (Admin creates teams and Team Leads, invitations are accepted, a Team Lead adds a candidate with a resume and a responsible member and searches for the candidate, the member opens one of the candidate's jobs, marks it applied and records an interview and the three dashboards follow, the Admin delegates ten jobs and Team Lead Beta distributes five each to two candidates, isolation by typed address between teams and between members of one team including a candidate handed to another member and back, a review-mode search that continues after its window is put away and assigns only what is chosen; eighth: a Team Lead finds a candidate in Salesforce from the Find Jobs form, brings them in with the resume, chooses the responsible member and runs the search, and the member finds the candidate with resume and jobs under My Candidates). Where the portal is connected to Salesforce nothing offers to type a candidate in, so the scenarios and checks that use the "Add Candidate" dialog tell the browser that Salesforce is not configured.
- `tests/e2e/ui_checks.py`: about 560 named checks over every feature, run at 1280 px and again at 390 px; any console error, page error or failed API request fails the run. Among them: Add Candidate with and without a resume, the candidate list filters, assigning and moving a candidate with the member's access following at once, the resume download (attachment, `nosniff`), the search form's candidate picker with resume upload and responsible member, My Candidates, the read-only candidate page of a member, the three candidate notifications and the CSV export.
- `tests/e2e/ui_checks_more.py`: about 135 further checks at each width for the screens that follow what the API states about itself: extra candidate-profile fields (Admin defines, Team Lead and Admin answer, the responsible member reads), password rules, the forgot-password answer, a job a manager took back from a candidate, reactivation of an account an Admin deactivated, the deep-search default and the notice about outside AI services on a candidate's resume. Some of them alter an API answer on its way to the browser, to prove the screen shows the API's numbers and words and not constants of its own.
- `tests/e2e/ui_checks_salesforce.py`: about 200 checks at each width for candidates from Salesforce: the two groups of the Find Jobs candidate list, "Active candidates only", bringing a person in from the row with and without a resume, a person who is already in the portal (own team, another team), the team an Admin brings a candidate into, "Add from Salesforce" as the only way to add a candidate on the Candidates pages and an Admin's team page, the source marker and filter, the Salesforce block and "Refresh from Salesforce" with a local change kept, the Team Lead scope `all` / `own`, the integrations page, and that a member's pages never ask about Salesforce and the API refuses a member (403). Unreachable, too many searches, a failing status request, `connected: false` and "not configured" are checked by answering the portal's own request in the browser; "more matches than shown" comes from 21 extra records in the fake.
- `tests/e2e/ui_crawl.py`: every page for every role at both widths (82 page loads at each): console messages, failed requests, horizontal overflow, elements sticking out of their box.

"Open Job" in these tests uses a synthetic posting whose address is on a real public host (the API refuses to redirect to a host that does not resolve), so the machine needs working DNS; the browser itself reaches no outside host.

Searches in the demo stack finish within a second, so the states "queued" and "running", polling and cancellation are checked there with stubbed API answers; the real worker's behaviour is covered by the backend tests and by the live checks described under Known limits.

Demo accounts exist only in that database; they are listed at the top of `tests/e2e/server.py`.

Last full run, 2026-11-18, after candidates from Salesforce were introduced: 8 scenarios, 164 page loads and 1,805 checks (563 + 134 + 201 at 1280 px, 568 + 135 + 204 at 390 px), no failure, 20.4 minutes. The backend suite had 247 tests on that day.

## Measured performance

These figures were measured before candidates were introduced (when assignments belonged to members) and have not been repeated since. API response times on a synthetic dataset of 60 teams, 1,200 members, 5,000 postings, 240,000 assignments and 415,000
events (PostgreSQL 17 in Docker on the development machine, best of three calls):

| Endpoint | Time |
| --- | --- |
| Admin dashboard (all cards, charts and the team table) | 560 to 630 ms computed, 4 ms from the cache |
| Team analytics with its member table | 105 to 117 ms computed, 3 ms from the cache |
| Teams list with statistics (60 teams) | 85 ms |
| Members page (50 of 1,200, with statistics) | 27 ms |
| Organization-wide assignments page | 20 ms |
| Member dashboard | 8 ms |
| One member's assignments page | 7 ms |
| Status change (commit and cache invalidation) | 23 ms |

Creating a search returns in about 30 ms; the search itself runs in the worker (a live 10-job deep search took 89 s).

### Analytics cache

Team and organization analytics are stored in Redis for `ANALYTICS_CACHE_SECONDS`. Each stored answer belongs to a
data version. Any committed change to assignments, application events, jobs, people, teams or searches, made by the
API or by the worker, raises the version before the request that made the change returns, so the next dashboard read
is computed again. In the measurement above a status change moved the "applied" figure by exactly one on the very next
read of both dashboards. Member dashboards and every list are always read live.

Three things are not seen by that mechanism and can be up to one cache lifetime old: rows changed directly in
PostgreSQL, the "postings indexed" figures maintained by the crawler, and a change committed at the moment Redis was
unreachable. When Redis is down the API computes every answer from PostgreSQL.

### Logs

The API and the worker write one JSON object per line to standard error (`LOG_FORMAT=text` for plain lines). Request
lines hold the method, the route template, the status, the duration, a request id (also returned in the
`X-Request-ID` header) and the signed-in user's id and role. They never hold the raw path, the query string, bodies,
cookies or tokens; invitation and reset links carry their token in the URL, which is why uvicorn's own access log is
switched off in the commands above.

## API

Interactive documentation: `http://127.0.0.1:8010/api/docs`. `docs/candidate-api.md` lists the candidate endpoints and every field renamed when candidates were introduced. `docs/salesforce-api.md` describes the Salesforce search, import and refresh. The contract is exported with `python -m app.portal.export_openapi > openapi.json`. Errors have one shape: `{"error": "...", "detail": "...", "fields": {...}}`.

## The earlier single-user app

`app/main.py` and `static/` are the previous single-page version. It still runs on its own (`uvicorn app.main:app --port 8012`) with the SQLite index and has no sign-in, so do not expose it. It is kept until the portal has replaced it in daily use.

## Known limits

- A search interrupted by a worker stop, crash or the worker's job limit (`WORKER_JOB_TIMEOUT_SECONDS`, four hours) is resumed once: candidates already finished are kept, the others are searched again. After a clean stop the next worker resumes at once; after a crash it takes about two and a half minutes (the run's heartbeat must be 120 seconds old). A second interruption closes the run as failed or partial. Measured live: stop, 0.3 s to re-queue; kill, resumed after 141 s.
- Workday is throttled by Workday, so Workday coverage builds up over several minutes in the background.
- Dashboards use page/offset pagination; very large lists (tens of thousands of rows per page set) would need keyset pagination.
- E-mail addresses on reserved domains (for example `.test`, `.local`) are rejected by validation.
- E-mail delivery is tested against an SMTP server on the test machine, not against a real mail provider. Adzuna has not been tested with a real key. The browser tests use Chromium only.
- Time series are bucketed in UTC.
