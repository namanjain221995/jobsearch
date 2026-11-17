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

1. The Admin creates a Team Lead (with a new or existing team). A Team Lead adds members to their own team.
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

## Searching and assigning

1. A Team Lead (or Admin) picks candidates, optionally overrides the search criteria, and chooses **Find & Assign Matching Jobs** or **Search first, review, then assign**. The result count is per candidate. A candidate needs no responsible member to be searched.
2. The API stores a search run and returns its id at once. The worker does the work; it continues if the browser is closed.
3. For each candidate the existing search engine runs with that candidate's current resume and preferences: local index first, live sources, AI web search (deep search), verification, US filter, authorization filters, deduplication, ranking. The run records which resume version it used.
4. Results are saved as global job postings (one row per vacancy) plus per-candidate results. In auto-assign mode, jobs scoring at least the minimum match score become assignments. A job is never assigned twice to the same candidate; the same job can go to several candidates who each qualify. The member responsible for a candidate is notified about new jobs for that candidate.
5. Progress (sources read, postings examined, verified, duplicates, matched, assigned, warnings) is written to the run about once a second and shown in the progress window. If 50 jobs were requested and 18 qualify, 18 is what is shown.

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

A member's numbers are the totals over the candidates assigned to that member at the moment; team and organization numbers also include candidates nobody is responsible for. Team and organization dashboards break the numbers down per candidate and per responsible member. A date range selects assignments by the day they were assigned (UTC). Charts show, per day or week, jobs assigned, applications confirmed and links opened.

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
- **Limits against abuse by a signed-in account:** at most 3 queued or running searches per person and 12 in total, 5 invitations per address and 60 per sender per hour. A search stops assigning as soon as the person who started it is deactivated or no longer manages that team.
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

Greenhouse, Ashby and Lever employer boards (about 420 seed employers plus boards learned from AI web search), Workday (159 large employers, rate-limited by Workday), Workable search, Amazon Jobs, Remote OK, Jobicy, Himalayas, OpenAI web search and Claude web search with verification, a headless browser for pages that need JavaScript, and Adzuna (optional key, not live-tested). LinkedIn, Indeed, Glassdoor and ZipRecruiter are not scraped: their terms forbid it and the app does not bypass blocks. An Admin can switch sources off under Settings → Integrations.

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
| `API_ORIGIN` (frontend) | Where Next.js forwards `/api/*` (default `http://127.0.0.1:8010`) |
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
tests/e2e/run.sh                # everything, about 17 minutes; or: acceptance | crawl | checks
```

The script builds the web app against the demo API (into `frontend/.next-e2e`, beside the normal build), starts both on ports 8011 and 3011 with freshly seeded data, drives a real Chromium browser, and stops everything again. It never touches the real database or the real servers. External hosts are made unreachable for the browser, so no employer site is contacted.

- `tests/e2e/test_acceptance.py`: the seven acceptance scenarios of the brief, step by step in the page (Admin creates teams and Team Leads, invitations are accepted, a Team Lead adds a candidate with a resume and a responsible member and searches for the candidate, the member opens one of the candidate's jobs, marks it applied and records an interview and the three dashboards follow, the Admin delegates ten jobs and Team Lead Beta distributes five each to two candidates, isolation by typed address between teams and between members of one team including a candidate handed to another member and back, a review-mode search that continues after its window is put away and assigns only what is chosen).
- `tests/e2e/ui_checks.py`: about 560 named checks over every feature, run at 1280 px and again at 390 px; any console error, page error or failed API request fails the run. Among them: Add Candidate with and without a resume, the candidate list filters, assigning and moving a candidate with the member's access following at once, the resume download (attachment, `nosniff`), the search form's candidate picker with resume upload and responsible member, My Candidates, the read-only candidate page of a member, the three candidate notifications and the CSV export.
- `tests/e2e/ui_checks_more.py`: about 135 further checks at each width for the screens that follow what the API states about itself: extra candidate-profile fields (Admin defines, Team Lead and Admin answer, the responsible member reads), password rules, the forgot-password answer, a job a manager took back from a candidate, reactivation of an account an Admin deactivated, the deep-search default and the notice about outside AI services on a candidate's resume. Some of them alter an API answer on its way to the browser, to prove the screen shows the API's numbers and words and not constants of its own.
- `tests/e2e/ui_crawl.py`: every page for every role at both widths (76 page loads at each): console messages, failed requests, horizontal overflow, elements sticking out of their box.

"Open Job" in these tests uses a synthetic posting whose address is on a real public host (the API refuses to redirect to a host that does not resolve), so the machine needs working DNS; the browser itself reaches no outside host.

Searches in the demo stack finish within a second, so the states "queued" and "running", polling and cancellation are checked there with stubbed API answers; the real worker's behaviour is covered by the backend tests and by the live checks described under Known limits.

Demo accounts exist only in that database; they are listed at the top of `tests/e2e/server.py`.

Last full run, 2026-11-17, after candidates were introduced: 7 scenarios, 152 page loads and 1,396 checks (561 + 134 at 1280 px, 566 + 135 at 390 px), no failure, 16.5 minutes. The backend suite had 247 tests on that day.

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

Interactive documentation: `http://127.0.0.1:8010/api/docs`. `docs/candidate-api.md` lists the candidate endpoints and every field renamed when candidates were introduced. The contract is exported with `python -m app.portal.export_openapi > openapi.json`. Errors have one shape: `{"error": "...", "detail": "...", "fields": {...}}`.

## The earlier single-user app

`app/main.py` and `static/` are the previous single-page version. It still runs on its own (`uvicorn app.main:app --port 8012`) with the SQLite index and has no sign-in, so do not expose it. It is kept until the portal has replaced it in daily use.

## Known limits

- A search interrupted by a worker stop, crash or the one-hour job limit is resumed once: candidates already finished are kept, the others are searched again. After a clean stop the next worker resumes at once; after a crash it takes about two and a half minutes (the run's heartbeat must be 120 seconds old). A second interruption closes the run as failed or partial. Measured live: stop, 0.3 s to re-queue; kill, resumed after 141 s.
- Workday is throttled by Workday, so Workday coverage builds up over several minutes in the background.
- Dashboards use page/offset pagination; very large lists (tens of thousands of rows per page set) would need keyset pagination.
- E-mail addresses on reserved domains (for example `.test`, `.local`) are rejected by validation.
- E-mail delivery is tested against an SMTP server on the test machine, not against a real mail provider. Adzuna has not been tested with a real key. The browser tests use Chromium only.
- Time series are bucketed in UTC.
