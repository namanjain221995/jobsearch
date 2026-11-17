# AI Job Finder — team portal

A multi-team platform for finding real US job postings, assigning them to people and tracking applications.

- **Admin** creates teams and Team Leads, adds or imports jobs, runs searches, delegates jobs and sees organization-wide analytics.
- **Team Lead** invites members, manages their resumes, runs AI searches for selected members, assigns jobs and follows the team's progress.
- **Member** sees only their own assigned jobs, reads the full description, opens the application page and records what they actually applied to.

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
| Database | PostgreSQL 17: users, teams, jobs, searches, assignments, events, audit log, and the crawler's job index | `alembic/` |

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
cd frontend && npm install && npm run dev                             # web app on http://127.0.0.1:3010
```

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
docker compose --profile app up -d --build
docker compose --profile app run --rm -e BOOTSTRAP_ADMIN_EMAIL=you@company.com -e BOOTSTRAP_ADMIN_PASSWORD='...' backend python -m app.portal.bootstrap
```

Services: `postgres`, `redis`, `migrate` (runs `alembic upgrade head` once), `backend`, `worker`, `frontend`. Set `PUBLIC_BASE_URL` to the address users open, and `POSTGRES_PASSWORD` to a real secret on any shared machine.

## How accounts are created

There is no public registration.

1. The Admin creates a Team Lead (with a new or existing team). A Team Lead adds members to their own team.
2. The system creates an invitation: a random token that is stored only as a hash, works once and expires after 48 hours. Role and team are fixed on the server when the invitation is created.
3. With SMTP configured the link is e-mailed. Without SMTP the person who created the invitation sees the link and copies it; the app never claims an e-mail was sent when it was not.
4. The invited person opens `/invite/accept?token=…`, chooses a password and signs in.

Password reset works the same way (`/forgot-password`, or a manager creates a reset link for someone in their scope).

## Searching and assigning

1. A Team Lead (or Admin) picks members, optionally overrides the search criteria, and chooses **Find & Assign Matching Jobs** or **Search first, review, then assign**. The result count is per member.
2. The API stores a search run and returns its id at once. The worker does the work; it continues if the browser is closed.
3. For each member the existing search engine runs with that member's resume and preferences: local index first, live sources, AI web search (deep search), verification, US filter, authorization filters, deduplication, ranking.
4. Results are saved as global job postings (one row per vacancy) plus per-member results. In auto-assign mode, jobs scoring at least the minimum match score become assignments. A job is never assigned twice to the same member; the same job can go to several members who each qualify.
5. Progress (sources read, postings examined, verified, duplicates, matched, assigned, warnings) is written to the run about once a second and shown in the progress window. If 50 jobs were requested and 18 qualify, 18 is what is shown.

An Admin can also add a job by hand, import one from a URL (reviewed before saving), import a CSV, delegate jobs to a team's queue, or assign directly to members of any team. A manually entered job is marked **unverified** until its page has been checked.

## Application tracking

Statuses: `ASSIGNED → VIEWED → APPLYING → APPLIED → INTERVIEWING → OFFER`, plus `REJECTED`, `WITHDRAWN`, `SKIPPED`.

Opening a job page is **not** applying. "Open Job" goes through an audited redirect that records who opened which job and when. A member marks the application as submitted separately (optionally with a reference number and the real date). Mistakes can be corrected; the earlier state stays in the history.

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

A date range selects assignments by the day they were assigned (UTC). Charts show, per day or week, jobs assigned, applications confirmed and links opened.

## Security

- **Passwords:** Argon2id, computed in worker threads so a sign-in never stalls other requests. **Sessions:** random token in an HttpOnly, SameSite=Lax cookie; only its hash is stored; validated on the server for every request; revoked on sign-out, password reset and deactivation. Set `COOKIE_SECURE=true` (or `APP_ENV=production`) when served over HTTPS.
- **CSRF:** state-changing requests need the `X-CSRF-Token` header matching the session, and the `Origin` must be the app's own. The two GET requests that record something (opening an assignment, Open Job) do not record when the browser reports a cross-site request.
- **Sign-in throttling**, counted in Redis: an account is locked for 15 minutes after 5 failed sign-ins. See "Client addresses" below for the finer limits that become available behind a reverse proxy.
- **Authorization** is enforced in the API on every endpoint and query, never only in the UI. A Team Lead's queries are limited to the team they lead today (a lead who is moved or removed loses the old team's members, assignments and searches at once); a member's to their own rows. Client-supplied team or member ids are checked against the session, not trusted.
- **One-time links** (invitations, password resets) are stored as hashes, expire, work once, and die early when the manager who issued them is deactivated or no longer manages that person, when the person moves to another team, and when a newer link or a password change replaces them. A link that was e-mailed is not also shown to the manager who requested it.
- **Job links for members:** raw URLs are not sent to members at all, and web addresses written inside a posting's text are removed from the member's copy. The redirect target is read from the stored job, must be a public http(s) address, and cannot be changed by request parameters.
- **What a member receives about other people:** names only. Sign-in e-mail addresses of managers, private notes (including their existence) and manager-only events are left out.
- **Outbound requests** (page verification, URL import, headless browser) are allowed only to globally routable addresses, and the connection is made to the address that was checked, so a host name cannot answer differently the second time (DNS rebinding). The headless browser reaches the network only through a local guard proxy.
- **Uploads and request sizes:** a request without a session is refused before its body is read; JSON bodies are limited to 512 KB, resumes to 10 MB, CSV imports to 2 MB. PDF and DOCX resumes are parsed in a separate process limited to 1 GB of memory, 10 s of CPU and 20 s of run time, and a file whose content inflates beyond the upload limit is refused, so a "decompression bomb" costs about a tenth of a second.
- **Limits against abuse by a signed-in account:** at most 3 queued or running searches per person and 12 in total, 5 invitations per address and 60 per sender per hour. A search stops assigning as soon as the person who started it is deactivated or no longer manages that team.
- **Audit log:** sign-ins, invitations, account changes, assignments, status changes, searches, settings. Request logs never hold tokens, query strings or bodies, and SQL parameters are kept out of error logs.
- **API documentation** (`/api/docs`, `/api/openapi.json`) is switched off when `APP_ENV=production` (`API_DOCS=true` turns it back on).
- **Resumes:** the extracted text and structured profile are stored in PostgreSQL; the uploaded file is not kept. Replacing a resume erases the text of the previous version. Resume text is sent to the configured AI providers when a search runs for that member; the UI says so. Resume and posting text are passed to models as untrusted data.

The rules above were checked by an adversarial review on 2026-11-17; its 23 findings and their fixes are pinned by the 30 tests of `tests/portal/test_security_review.py`; further rules are in `tests/portal/test_hardening.py`.

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

## Job sources

Greenhouse, Ashby and Lever employer boards (about 420 seed employers plus boards learned from AI web search), Workday (159 large employers, rate-limited by Workday), Workable search, Amazon Jobs, Remote OK, Jobicy, Himalayas, OpenAI web search and Claude web search with verification, a headless browser for pages that need JavaScript, and Adzuna (optional key, not live-tested). LinkedIn, Indeed, Glassdoor and ZipRecruiter are not scraped: their terms forbid it and the app does not bypass blocks. An Admin can switch sources off under Settings → Integrations.

## Configuration

See `.env.example` for every variable. The ones you are most likely to set:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL`, `REDIS_URL` | PostgreSQL and Redis |
| `PUBLIC_BASE_URL` | Address of the web app (links in invitations, same-origin check) |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_MODEL`, `ANTHROPIC_MODEL` | AI providers (the app also works with none) |
| `SMTP_HOST`, `SMTP_FROM`, … | E-mail delivery for invitations and resets |
| `COOKIE_SECURE`, `APP_ENV` | Secure cookies in production; production also switches the API docs off |
| `TRUST_FORWARDED_FOR`, `TRUSTED_PROXY_HOPS` | Believe `X-Forwarded-For` (only behind a reverse proxy that sets it; see Security) |
| `MAX_ACTIVE_SEARCHES_PER_USER`, `MAX_ACTIVE_SEARCHES` | Queued and running searches per person (3) and in total (12) |
| `MAX_MEMBERS_PER_SEARCH`, `MAX_RESULTS_PER_SEARCH_RUN`, `MAX_AI_CALLS_PER_SEARCH` | Cost and concurrency limits |
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

Browser tests (Playwright) for the main user journeys are not in the repository yet.

Demo accounts exist only in that database; they are listed at the top of `tests/e2e/server.py`.

## Measured performance

API response times on a synthetic dataset of 60 teams, 1,200 members, 5,000 postings, 240,000 assignments and 415,000
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

Interactive documentation: `http://127.0.0.1:8010/api/docs`. The contract is exported with `python -m app.portal.export_openapi > openapi.json`. Errors have one shape: `{"error": "...", "detail": "...", "fields": {...}}`.

## The earlier single-user app

`app/main.py` and `static/` are the previous single-page version. It still runs on its own (`uvicorn app.main:app --port 8012`) with the SQLite index and has no sign-in, so do not expose it. It is kept until the portal has replaced it in daily use.

## Known limits

- A search interrupted by a worker stop, crash or the one-hour job limit is resumed once: members already finished are kept, the others are searched again. After a clean stop the next worker resumes at once; after a crash it takes about two and a half minutes (the run's heartbeat must be 120 seconds old). A second interruption closes the run as failed or partial. Measured live: stop, 0.3 s to re-queue; kill, resumed after 141 s.
- Workday is throttled by Workday, so Workday coverage builds up over several minutes in the background.
- Dashboards use page/offset pagination; very large lists (tens of thousands of rows per page set) would need keyset pagination.
- E-mail addresses on reserved domains (for example `.test`, `.local`) are rejected by validation.
- Time series are bucketed in UTC.
