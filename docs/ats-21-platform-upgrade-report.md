# 21-ATS upgrade report

Date: 2026-11-21. Scope: the "21 ATS US job discovery" brief. Every number below comes from a command that was
run; where something was not checked, it says so.

## 1. Executive summary

- The portal now knows all 21 platforms in one registry (`app/ats_platforms.py`) and shows the real state of each
  to an Admin (Settings, tab "ATS Platforms"). The state is computed from evidence in the job index, never set by hand.
- **Seven platforms have a working board reader**: Greenhouse, Lever, Ashby, Workday, Workable (already there) plus
  **Recruitee and Rippling (new)**. Four more are reachable through **public posting pages** that are verified one
  by one: Jobvite, JazzHR, Avature, ADP Workforce Now (new), plus Oracle Recruiting Cloud pages.
- **Ten platforms are not fully integrated, on purpose**: the vendor forbids automated reading (iCIMS, SAP
  SuccessFactors, SmartRecruiters, BambooHR, Pinpoint's employer sites), the documentation reserves the endpoint
  (Oracle board reader, built but off), or the API needs the employer's own credentials (Bullhorn, CEIPAL, JobDiva,
  SmartSearch). Nothing was bypassed.
- Find Jobs has an ATS platform filter and a startup-focus switch; the search page shows where the jobs came from,
  per platform and per provider, with time to first verified results and total time.
- Two complete searches (Team Lead to member) ran with the real engine and real providers on a fresh local database:
  25 of 25 requested jobs assigned in 331 s, and a Recruitee + Rippling search that assigned 7 jobs in 170 s.
- Backend tests: 832 passed, 0 failed. Frontend lint, type check and production build pass.

## 2. What was already implemented (reused, not rebuilt)

Board readers for Greenhouse, Lever, Ashby, Workday, Workable; remote boards, Adzuna, Amazon; the PostgreSQL job
index with a board catalogue (about 27,000 boards) and progressive reading; SearXNG, Common Crawl board discovery,
SerpApi, TinyFish, AWS AgentCore Web Search with the PostgreSQL monthly counter; OpenAI and Claude web search and
ranking; the TechSara model (title expansion, rerank, explanations, web search); page verification with JobPosting
structured data; SSRF guard, robots cache, per-host limiter; resume parsing, the 0-100 rubric, explanations; team
isolation, Salesforce import, auto-apply with consent and approval. SmartRecruiters and BambooHR readers existed and
were off.

## 3. What was added or improved

| Area | Change |
| --- | --- |
| Registry | `app/ats_platforms.py`: 21 platforms with access class, public hosts, reader, startup flag, docs link, note |
| New readers | `app/providers/ats_startup.py`: Recruitee (on), Rippling (on), Pinpoint (off); registered, discoverable from search results, catalogue lists imported (2,956 + 2,660 + 1,339 boards) |
| Public pages | `app/providers/ats_pages.py`: strict posting-address patterns for Jobvite, JazzHR, Avature, ADP, Oracle pages; listing and home pages never count as postings |
| Oracle | `app/providers/ats_oracle.py`: board reader, verified live, off by default (`BOARD_SOURCES_ORACLE=1`) |
| Health | `GET /api/integrations/ats` (Admin) and the "ATS Platforms" tab; `app/portal/services/ats_health.py` |
| Search | `SearchFilters.ats_platforms`, `SearchFilters.startup_focus`; `app/search_scope.py`; per-platform and per-provider statistics in `SearchTargetOut.discovery` |
| Earlier this week, same goal | TechSara model web search as a discovery engine; AWS web search tuned (one host per query, date filter off): 79 of 80 results usable in the live check, before 0 |

## 4. The 21 platforms and their actual status

"State" is what the health view derives. For readers it becomes "Verified working" only after a board was read
successfully with postings in the last 7 days; the production values are in section 8.

| # | Platform | Access | What the portal does | Evidence |
| --- | --- | --- | --- | --- |
| 1 | Greenhouse | Public API | Board reader | Local search: 342 verified, 10 assigned |
| 2 | Lever | Public API | Board reader | Local search: 37 verified, 2 assigned |
| 3 | Ashby | Public API | Board reader | Local search: 132 verified, 2 assigned |
| 4 | Workday | Tenant search endpoint | Board reader, paced | Local search: 22 leads; Workday rate-limited this machine (HTTP 429), reads continued in the background |
| 5 | iCIMS | Restricted | Nothing is read | Terms forbid robots on icims.com and subdomains; 2 of 3 tenants checked answer `Disallow: /` |
| 6 | SmartRecruiters | Restricted | API reader off; posting pages found by search are verified | `api.smartrecruiters.com/robots.txt`: only LinkedInBot allowed |
| 7 | Workable | Public search | Cross-company reader | Local search: 81 verified, 6 assigned |
| 8 | Jobvite | Public pages | Posting pages verified one by one | No robots rules; plain fetch found no structured data, so it depends on the browser or AI page check (not run) |
| 9 | Oracle Recruiting Cloud | Public pages; reader off | Posting pages; board reader built, off | Live: 50 postings, 39 US from one query; Oracle docs call the endpoint "only for Oracle internal use" |
| 10 | SAP SuccessFactors | Restricted | Nothing is read | `career4.successfactors.com`, `career5.successfactors.eu`: `Disallow: /` |
| 11 | Bullhorn | Credentials | Nothing | Public Career Portal endpoint needs the firm's corp token and returns no posting address |
| 12 | CEIPAL | Credentials | Nothing | API needs customer credentials; `jobsapi.ceipal.com` robots `Disallow: /` |
| 13 | JobDiva | Credentials | Nothing | `api.jobdiva.com` answers 401 |
| 14 | BambooHR | Restricted | Reader off | Terms section 4.2 forbid robots |
| 15 | JazzHR | Public pages | Posting pages verified one by one; host is in the default query list | Live: one real posting verified from the employer's page |
| 16 | Avature | Public pages | Posting pages verified one by one | Robots allow `/careers`; plain fetch found no structured data (browser or AI check not run) |
| 17 | Pinpoint | Public feed, robots per employer | Reader built, off | Pinpoint's own board read (3 postings); both employer tenants tried answer `Disallow: /` and were refused |
| 18 | Recruitee | Public API (documented) | Board reader, on | Live: 3 boards, 22 postings, 10 US. Complete search: 1 verified |
| 19 | Rippling | Public board endpoints | Board reader, on | Live: 3 boards, 21 postings, 11 US. Complete search: 8 verified, 7 assigned |
| 20 | SmartSearch | Credentials | Nothing | No public documentation, feed or page pattern found |
| 21 | ADP Workforce Now | Public pages | Posting addresses recognised | Not tested with a real posting; the page is a script shell |

Details, quoted robots lines and terms: `docs/ats-platforms.md` and `docs/board-sources.md`.

## 5. OpenAI, Claude, AWS and the other providers

From the first complete search (25 requested, no filter):

| Provider | Calls | What it brought |
| --- | --- | --- |
| Claude | 2 calls, 5 web searches, 41,513 input tokens | 46 leads, 31 new boards with 5,487 postings |
| OpenAI | 12 calls, 3 web searches, 56,776 input tokens | 24 leads, 16 new boards with 701 postings |
| TechSara model | 98 calls, 957,164 input tokens | rerank of 754 jobs, explanations, 3 web searches: 10 leads, 4 new boards with 500 postings |
| TinyFish | 0 (not needed) in run 1; 20 searches, 0.10 USD in run 2 | run 2: 51 leads, 26 new boards with 432 postings |
| SerpApi | 0 calls | the target was reached before paid sources were needed |
| SearXNG | not available locally | runs on the server only |
| AWS web search | not available locally (needs the EC2 role) | see section 8 for the production run |

No provider failed. A failing provider is skipped with a warning; this is covered by tests, not by these runs.

## 6. Supported and unsupported capabilities

- Supported: official per-employer APIs with pagination, closed-job detection (a posting the board no longer lists
  is dropped), US location filtering, direct apply links, deduplication on canonical ATS ids, page verification
  for public-page platforms, board discovery from search results and from the catalogue.
- Not supported: platforms whose terms or robots forbid robots; credential-only APIs without credentials;
  Recruitee boards on employer-owned domains; Oracle boards discovered from search results (keys must come from
  real career-site addresses); per-platform breakdown of unverifiable leads.

## 7. Test results

| Check | Result |
| --- | --- |
| `pytest --ignore=tests/e2e` | 832 passed, 0 failed (439 s) |
| New test files | `test_ats_startup.py` 73, `test_ats_pages.py` + `test_ats_oracle.py`, `test_ats_platforms.py` + `portal/test_ats_health.py` 10, `test_platform_scope.py` + `portal/test_search_platforms.py` 22 |
| Frontend `npm run lint`, `npm run typecheck`, `npm run build` | pass |
| Migrations from zero on a fresh database | pass (head `d3f6a1b8c925`; this upgrade adds no migration) |
| Browser test suites (`tests/e2e`) | not run for this upgrade |
| New screens in a real browser | not looked at |

## 8. End-to-end search evidence

Fresh local database, catalogue imported, real engine, real providers, through the API as a Team Lead; candidate
"Data Engineer", 8 years, with a resume; then signed in as the member.

| | Search 1: all platforms, 25 requested | Search 2: Recruitee + Rippling only, 10 requested |
| --- | --- | --- |
| Discovered | 46,423 | 710 |
| Verified | 809 | 9 |
| Duplicates removed | 3 | 0 |
| Matched | 25 | 9 |
| Assigned, visible to the member | 25 | 7 |
| Time to first verified results | 50 s | 127 s |
| Total time | 331 s | 170 s |
| Rounds | 1 | 5 (all budgets used) |
| Platforms that contributed | Greenhouse 10, Workable 6, Amazon 4, Ashby 2, Lever 2, Himalayas 1 | Rippling 7 |
| Match scores of assigned jobs | 80 to 93 | 60 to 96 |
| New boards registered | 51 (6,688 postings) | 38 (579 postings) |
| Results from other platforms dropped by the filter | 0 | 110 |

Notes: in search 2, two matched postings (one Recruitee) were not assigned; the warnings say postings from paced
sources could not be opened within the time limit. All 32 assigned postings carry verification status "verified".
The 6 Workable jobs have no separate apply address stored; their posting address is the application page.

Production run (after deployment): see section 13.

## 9. Search performance before and after

There is no like-for-like benchmark: the "before" figure is a production search of 2026-11-18 (50 requested: 120,187
discovered, 1,895 verified, 50 matched, 6 min 8 s) and the "after" figures above are local runs with 25 and 10
requested. What can be said: the new code reached its target in one round, first verified results arrived after
50 s, and the engine change did not slow the existing test suite. Measured bottleneck in search 1: 241 of 331 s were
spent in the ranking stage (98 company-model calls). That is the next place to save time.

## 10. Cost and quota

- AWS web search: 19 of 4,000 queries used this month before this upgrade; 4 to 10 per candidate search.
- SerpApi: 250 per month on the free plan; used only when free sources leave a search short.
- TinyFish: 0.005 USD per search in these runs; daily ceiling enforced.
- OpenAI and Claude: 14 and 7 calls in the two runs; token counts are in section 5. A cost in USD was not computed.
- Under a platform filter, AI web search still runs and its off-platform leads are dropped, so those calls are spent.

## 11. Remaining limitations and required credentials

- Bullhorn, CEIPAL, JobDiva, SmartSearch: the employer's API credentials (per firm) are required.
- iCIMS, SuccessFactors, SmartRecruiters API, BambooHR: written permission from the vendor would be required.
- Decisions for the owner: Rippling is on although its terms pages could not be read (`RIPPLING_ENABLED=0` switches
  it off); the Oracle reader is off because of the "internal use" sentence (`BOARD_SOURCES_ORACLE=1` switches it on);
  Pinpoint is off (`PINPOINT_ENABLED=1` reads only employers whose robots.txt allow it).
- Jobvite, Avature, ADP and Oracle posting pages depend on the browser or AI page check; that path was not run.
- The search-provider block of the health view says "configured", not "working": provider answers are not recorded.

## 12. Changed files and migrations

New: `app/ats_platforms.py`, `app/search_scope.py`, `app/providers/ats_startup.py`, `app/providers/ats_pages.py`,
`app/providers/ats_oracle.py`, `app/portal/services/ats_health.py`, `app/data/boards/{recruitee,pinpoint,rippling}.csv`,
`frontend/src/components/views/ats-health.tsx`, `frontend/src/components/search/{platform-filter,search-origins}.tsx`,
`docs/ats-platforms.md`, this report, and the test files named in section 7.

Edited: `app/search_engine.py`, `app/schemas.py`, `app/config.py`, `app/job_normalizer.py`, `app/board_catalog.py`,
`app/providers/{registry,searxng}.py`, `app/portal/{schemas,search_runner}.py`, `app/portal/routers/{admin,searches}.py`,
`app/portal/services/searches.py`, frontend search form, search detail, admin settings, `openapi.json`, API types.

Migrations: none.

## 13. Git commit and deployment status

See the end of this file; it is filled in after the release.

## 14. Production-readiness verdict

Ready for production as an honest 21-platform registry with seven working board readers, page verification for
five more, and ten platforms that are correctly reported as restricted or credential-only. It is not a full
integration of all 21, and it cannot become one without vendor permission or employer credentials.
