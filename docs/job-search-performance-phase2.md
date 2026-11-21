# Job search, phase 2: speed, ranking, discovery beyond the ATS platforms

Date: 2026-11-21. Baseline: commit `a336188` (the 21-ATS upgrade) as deployed, plus its report commit `b738740`.
Every number below was printed by a command that was run for this work. Where something was not measured, it says so.
Nothing of this phase is deployed: see section 10.

## 1. Summary

| | Deployed code | This change |
| --- | --- | --- |
| 25 jobs, cold index and cold caches | 375.6 s | 107.6 s |
| 25 jobs, warm index, first search for a candidate | 283.4 s | 73.5 s |
| 25 jobs, the same search again | 283 s (nothing is remembered) | 61.2 s |
| Ranking stage in those three cases | 237 s | 15.6 s, 11.7 s, 9.9 s |
| Company model calls per search | 95 to 107 | 27 to 28, then 2 on a repeat |
| Boards a search reads when the budget binds (100 boards, "Data Engineer") | 301 verified postings, Greenhouse only | 663 verified postings on 5 platforms |
| Postings on employers' own career sites, verified per 7 Claude search calls | 5 to 14 | 51 to 56 |
| Company-site postings kept for later searches | none | kept, checked again on their page |

The target of under 120 seconds for a 25-job search is met in the three measured cases on the test machine.
Production was not measured: the server alone has SearXNG and AWS web search, and nothing was deployed.

Ranking quality did not change: the stage ranks exactly as before, it only stopped doing work nobody saw
(section 3). The lists of the old and the new code share 21 of 25 jobs, and so do two runs of the same code.

## 2. Platform decisions

| Platform | Decision | State in the code |
| --- | --- | --- |
| Oracle Recruiting Cloud | board reader stays off (documentation reserves the endpoint) | unchanged, `BOARD_SOURCES_ORACLE` unset |
| Pinpoint | stays off; even when switched on it refuses employers whose robots.txt disallow it | unchanged |
| Rippling | switched off pending a review of its terms | default `RIPPLING_ENABLED=false`, its host removed from the default search-engine hosts, stored postings no longer trigger description requests |
| Workable | investigated and fixed (below) | account reader on, cross-company search kept |
| All 21 definitions | preserved | `app/ats_platforms.py` still lists 21; states stay evidence-based |

Rippling is still read in production until this change is released. The server's `.env` sets neither
`RIPPLING_ENABLED` nor `SEARXNG_SITES` (checked by counting matching lines, values not read), so the new defaults
take effect with the release.

### Workable: why production showed "not verified"

Two causes, both in the code and both reproduced locally:

1. The 6,527 Workable accounts of the catalogue were never read. The background reader only took sources that are
   read per board, and Workable was registered as a cross-company search.
2. A search cut its list of planned units to the budget (1,500 for a small request) in registry order, Greenhouse
   first. Production knows 6,629 Greenhouse boards, so every one of the 1,500 was a Greenhouse board; the Workable,
   Amazon and Himalayas queries and the Remote OK and Jobicy feeds were never asked.

Fixes: a reader for one employer account on Workable's documented public endpoint
(`www.workable.com/api/accounts/{subdomain}?details=true`, which redirects to the widget address the reader calls),
with robots check, SSRF guard, its own pace of one request a second and strict id validation; the background reader
now reads accounts; query and feed units are no longer cut by the budget. Live check: 3 accounts, 24 postings, all
with description, date and apply address. Whether Workable answers the production server's address was not checked.

The cross-company Workable search uses an endpoint that is not documented. It stays on because nothing that was read
forbids it; `WORKABLE_SEARCH_ENABLED=false` switches it off alone.

## 3. Ranking: where the time went and what changed

Measured on a real search ("Data Engineer", 8 years, 25 jobs asked, 828 verified postings):

| Step of the ranking stage | Before | After |
| --- | --- | --- |
| Company reranker over every posting | 42.2 s (26 requests, one after the other, inside the stage) | 0.2 to 2.5 s inside the stage; the reading happens earlier, in the background |
| OpenAI judging the pool of 55, Claude audit | about 10 s | 10 to 12 s; 0 to 9 s on a repeat |
| Explanations by the company chat model | 183.7 s (68 calls, 340 jobs) | 0 s for this request |
| Total | 236.6 s | 15.6 s cold, 9.9 s on a repeat |

What the measurements showed, and the change that follows from each:

1. **Explanations nobody saw.** The company model explained the best 400 jobs whatever the request. A request of 25
   delivers 25 jobs that OpenAI or Claude judged (the judged pool is `max(2T, T+30)` and judged jobs sort first), so
   none of the 340 explained jobs was delivered. Now only jobs that can be delivered are explained: the provisional
   final selection plus a margin. A request of 200 with 40 judged jobs still gets its 160 explanations.
2. **The reranker is serial.** It reads 18 to 20 postings a second whether a request carries 8, 32, 64 or 128
   documents and whether 1, 4 or 8 requests run at once (up to about 40 a second when nothing else uses it). Shorter
   documents (2,300 instead of 4,400 characters) gave 28 a second and changed the top of the list, so documents were
   not shortened. Parallel requests gain nothing, so one worker per search sends one batch at a time.
3. **Its time can be moved, not removed.** The worker starts as soon as the profile is extracted, on the postings the
   index already holds, best keyword matches first, and goes on while sources are read and leads are verified. The
   ranking stage puts the final list at the front of the queue, waits at most `TECHSARA_RERANK_WAIT_SECONDS` (20) and
   goes on with what is scored.
4. **A keyword shortlist would lose good jobs.** Of the postings the reranker rates above 0.3, a shortlist of the 600
   best keyword matches lost 4 of 25 in one search and 1 of 99 in another; the deepest good posting sat at place
   2,117 of 3,575. So no shortlist cuts the list. With the bounded wait, the postings not reached are the weakest
   keyword matches, and they keep their keyword score.
5. **The same question was asked again in every search.** `app/rank_cache.py` remembers a model's answer for the same
   candidate side and the same posting text for 7 days: reranker relevance, OpenAI / Claude judgements, the audit,
   company-model explanations. A changed resume, request, model, prompt or posting text is a different key. Only
   hashes and model output are stored, in the job index's key/value table; the extracted profile is kept in the
   worker's memory only. The memory is wiped with the test data ("Switch to Real Data").

Already true and left alone: eligibility and work-arrangement rules run before the reranker (0 of 828 postings in the
ranking input would be rejected by them), and keyword and skill scoring of 828 postings takes 14 ms.

Time limits: the reranker worker stops after `TECHSARA_RERANK_SECONDS` (90), the OpenAI / Claude judging after
`AI_RANK_SECONDS` (150); what was not reached keeps the score it has, and the search says so.

### Quality against the baseline

The scale is unchanged (0 to 100: skills 35, title 25, experience 20, arrangement 10, additional 10) and so is the
choice of jobs that are judged.

| Comparison, 25 delivered jobs | Same jobs | Mean score |
| --- | --- | --- |
| Old code vs new code | 21 of 25 | 86.2 vs 87.6 |
| New code vs new code (two independent judge passes) | 21 of 25 | 87.6 vs 86.6 |

The jobs that differ sit at the cut-off (scores 78 to 80 in the other run). With the memory a repeated search gives the
same job the same score.

Large case ("Java Developer", 3,575 matching postings because a related title was generic): reading all 3,000 allowed
postings took 142 s. With the bounded wait the ranking stage took 36.9 s in a replay without the early pass, the
delivered list had a mean score of 83.4 against 80.4, and 14 against 13 of its jobs were rated good by the reranker.

## 4. Which boards a search reads

`index_ttl_seconds` is one hour, so a posting counts as verified only when its board was read within the hour. A
search may refresh a limited number of boards. Until now it took them in registry order. Now (`_reading_plan`):

- boards whose last read, up to three days old, listed postings with the requested titles go first, most matches
  first, whatever their platform;
- units that are still fresh do not use up the budget;
- sources that allow about one request a second (Workday, Recruitee, Workable accounts) get at most 40 stale units
  per search, their best matches first, and a search waits only a short while for them.

Measured with a budget of 100 boards, which is what production's budget of 1,500 against 12,000 known boards looks
like in proportion (search without AI, old code against new code on the same index):

| Search | Before | After |
| --- | --- | --- |
| Data Engineer | 301 verified postings, all Greenhouse; 37 with a keyword score of 80 or more; 57 employers | 663 on Greenhouse, Amazon, Ashby, Workable, Lever, Jobicy; 104; 111 employers |
| Java Developer | 107 (Greenhouse 104, Lever 3); 4; 32 employers | 151 on 6 sources; 10; 61 employers |

The "after" column includes the Workable fix of section 2 (query and feed units are kept).

### Background reader

Order inside each platform's per-minute allowance: boards whose last read had US postings (again after 24 hours,
oldest first), then boards never read, then misses (a failed read after 6 hours, no US posting after 7 days, doubling
each time), then dead boards (30 days, doubling). No wait exceeds 90 days. Every read is recorded, successful or not
(migration `f2a9c4d7e1b3`: `tried_at`, `us_count`, `misses` on `index_boards`). Recruitee goes from 6 to 15 boards a
minute: one read is two requests to that employer's own host. Computed, not measured: the 2,956 Recruitee boards take
about 3.3 hours of reader time instead of 8.2. Workday's rate was not raised.

## 5. Discovery beyond the ATS platforms

The owner's direction during this work: do not depend on ATS platforms or job boards; the AI must find jobs on the
web and on companies' own sites.

### AI web search (OpenAI and Claude)

Audited against the providers' current documentation and changed:

- OpenAI: a search is now required in every call; one query per call (the model had packed up to 11 into one action,
  using 15 of 70 allowed searches); addresses are taken only from the search tool's own output, never from text the
  model wrote; each call carries its own domain filter and a US location; strict JSON schema; page opens are no
  longer counted as searches.
- Claude: current web search tool with a step down to older versions on HTTP 400; never both an allow list and a block
  list; paused turns are continued as documented; search errors inside a 200 answer are counted; billed searches come
  from the usage record.
- Both: every `site:host "title" place` cell has one owner for the whole search; a provider that fails twice is taken
  out for the rest of the search and the other takes over; a round keeps the calls that finished in time.
- `AI_OPEN_WEB_SHARE=0.5`: half of the web-search calls of an unfiltered search look at employers' own career sites,
  with the job boards, the ATS hosts and the restricted platforms blocked (44 domains). By default Claude makes these
  calls and OpenAI keeps the ATS hosts (`AI_OPEN_WEB_SPLIT=auto`): OpenAI's search tool returned 4 to 24 usable leads
  per call from named career sites, Claude's 35 to 60.

Measured with the real providers, 14 web-search calls (the plan of a 175-job search), leads checked on an empty index:

| Data Engineer | Verified matching postings (on boards new to the index) | Cost of the calls |
| --- | --- | --- |
| Before | 273 (19) | 1.62 USD |
| ATS hosts first (open web 1 call in 6) | 618 (68) | 2.24 USD |
| Half on company sites (final) | 452 (75) | 2.39 USD |

| Java Developer | Verified matching postings (new) | Cost |
| --- | --- | --- |
| Before | 92 (26) | 2.05 USD |
| ATS hosts first | 109 (49) | 2.27 USD |
| Half on company sites (final) | 93 (40) | 2.28 USD |

Costs are computed from recorded token and search counts and the providers' price pages, not from invoices.

Employers' own career sites (Claude, 7 calls, the new page verifier with its budget of 560):

| | Leads on employer sites | Employer domains | Pages verified as open postings | Verified and matching the titles |
| --- | --- | --- | --- | --- |
| Data Engineer, before (3 general calls) | 28 | 21 | 5 | not separable |
| Data Engineer, after | 336 | 47 | 51 on 19 domains | 27 |
| Java Developer, before | 60 | 36 | 14 | not separable |
| Java Developer, after | 337 | 47 | 56 on 22 domains | 8 |

The trade-off is real: half the calls on company sites costs discoveries on ATS boards (618 against 452 for one
profile). `AI_OPEN_WEB_SHARE` is the dial. The main limit of web search on career sites: half to two thirds of the
posting pages a search engine returns are closed (64 of 96 and 59 of 113 in one run); pages dated more than 60 days
ago are therefore handed to the verifier last.

Duplicate requests: SearXNG and the TechSara web search no longer send the same query (12 of 43 requests were
repeats in a simulated 50-job search, now 0; 43 of 137 in a 300-job search, now 0). The four search engines share one
ledger of what was asked. Under an ATS platform filter every AI call is restricted to the chosen platforms' hosts and
no open-web call is made.

Not built: an open-web share for the `site:` search engines (SearXNG, TechSara web search, TinyFish). AWS web search
keeps its existing "any site" query.

### Reading company pages

Evaluated on 79 real pages (29 open postings, 50 pages that must be rejected), the same captures replayed through
the old and the new code:

| | Before | After |
| --- | --- | --- |
| Open US postings verified, leads with an address only (17) | 12 | 16 |
| Open postings verified, leads with a claimed title and employer | 8 | 16 |
| Pages wrongly accepted, address-only leads | 1 | 0 |
| Pages wrongly accepted, claimed leads | 4 | 0 |
| Non-US or place-less postings shown as US | 6 | 0 |
| Location filled on verified postings | 20 of 26 | 27 of 27 |
| Apply address filled | 0 | 12 |

Changes: JobPosting microdata is read beside JSON-LD; a posting dated more than five years ago, a stated end date or
closed wording rejects; list, search and home addresses and redirects that lost the posting id are never one posting;
with an AI page check available a claimed lead waits for the model, and the model's answer is checked against the
page (the title must be the page's heading, a page that addresses the model is never accepted on its word); the
browser waits until posting data is present instead of looking once. Budgets: 160 page checks per 50 results instead
of 40 (ceiling 1,200 instead of 600), 32 at a time instead of 8, 48 followed links instead of 16. Plain page checks
ran at 11.5 pages a second in a replay with recorded response times (not a live run).

Never requested, whatever names the address: job boards and aggregators, and every host of iCIMS, SAP SuccessFactors,
BambooHR, Rippling and Pinpoint. The discovery drops such leads before any request and the verifier refuses them as a
second line. Not readable and not worked around: careers.ibm.com and delta.avature.net (robot check),
jobs.uber.com (403), careers.walmart.com (robots.txt).

### Company-site postings stay in the index

A posting that a search verified on an employer's own page by a plain request is kept as a unit of the new source
`career_page` (`CAREER_PAGE_MEMORY=true`). Later searches check the remembered pages whose title fits again on their
page (up to 200 per search, outside the board budget) and find them without any web search; the background reader
checks each page about once a day; a page that no longer proves an open posting leaves the index. Pages that needed a
browser are not kept, and only US postings are. In the benchmark searches of section 6 the first search kept 3
postings (on Meta's own career site) and the repeat 2 more (on Netflix's own career site).

## 6. Benchmark

Test machine, real providers (OpenAI, Claude, the company model), no SearXNG and no AWS web search (server only).
Synthetic resume "Data Engineer", 8 years, 25 jobs asked, AI web search on. The index is the bench database with the
imported catalogue.

| | Deployed code | This change |
| --- | --- | --- |
| **Cold: fresh database** | | |
| Total | 375.6 s | 107.6 s |
| First verified results | 78.6 s | 51.9 s |
| Ranking stage | 236.6 s | 15.6 s |
| Verified postings entering the ranking stage | 828 | 827 |
| Calls: OpenAI / Claude / company model | 12 / 2 / 95 | 12 / 2 / 28 |
| **Warm index, first search for the candidate** | | |
| Total | 283.4 s | 73.5 s |
| First verified results | 25.8 s | 25.4 s |
| Ranking stage | 236.9 s | 11.7 s |
| Verified postings entering the ranking stage | 1,032 | 835 |
| Calls: OpenAI / Claude / company model | 12 / 2 / 107 | 12 / 2 / 27 |
| **The same search again, same worker process** | | |
| Total | as above | 61.2 s |
| Ranking stage | as above | 9.9 s |
| Calls: OpenAI / Claude / company model | as above | 3 / 2 / 2 |
| Taken from memory | nothing | profile, 812 relevance scores, 47 judgements, 13 audits |

All six searches delivered 25 of 25 jobs, all judged by OpenAI / Claude, with scores between 79 and 96. Tokens of the
new cold run: OpenAI 64,354 in and 10,593 out, Claude 22,981 in and 3,791 out with 5 web searches, company model
805,910 in and 530 out (it was 986,051 in and 37,034 out). The cost of a 25-job search in USD was not computed.

Failures seen in these runs: Workday rate-limited the test machine (HTTP 429, backed off), four Ashby boards could
not be read; no provider call failed. In one earlier run Claude had one failed call while its client was being
changed.

Where the remaining time goes (warm case): about 25 s for the first read of sources (the wait for one-request-a-second
sources), the AI web search (about 22 s, side by side with it), 26 to 37 s to verify leads and company pages, 10 to
12 s for judging. Lead verification was not shortened: AI leads gave 4 of the 25 delivered jobs in the production run
of the previous report.

The production baseline of the brief (25 asked, 859 verified, first results after 22 s, 322 s total, about 255 s in
ranking) matches the local "deployed code, warm index" row (283 s, 237 s in ranking). The new numbers on production
were not measured.

## 7. Tests

| Check | Result |
| --- | --- |
| `pytest --ignore=tests/e2e` on the final code | 1,144 passed, 0 failed (524.8 s) |
| Frontend `npm run lint`, `npm run typecheck` | pass |
| Browser suite `tests/e2e/run.sh all` on the integrated code | exit 0, "All browser tests passed": acceptance 8; crawl 0 problems at both widths; at 1280 px 563 + 134 + 201 + 77 + 186 checks, at 390 px 568 + 135 + 204 + 77 + 186, 0 failed |
| Migrations from zero on a fresh database | pass, head `f2a9c4d7e1b3` |
| Previous build against the new schema | checked for the index code: works (the migration only adds three columns) |

New test files: `test_rank_speed.py`, `test_reading_plan.py`, `test_career_page_memory.py`, `test_ai_discovery.py`,
`test_discovery_rounds.py`, `test_page_verifier.py` (201 tests on trimmed copies of real pages),
`test_page_browser.py` (a real Chromium against a local site), `test_workable_accounts.py`,
`portal/test_catalog_order_pg.py`, `e2e/ui_checks_platforms.py` (186 checks per width).

The browser checks cover: the ATS Platforms tab (21 platforms, state badges, startup filter, counts, notes, phone
layout, no access for Team Leads and Members), the platform filter and the startup switch (request body, presets,
unknown platform refused), a filtered and an unfiltered search with live progress, "Where the jobs came from",
"Settings used", a result with its match explanation and direct employer link, the member's view, and the test-mode
panel. The complete Team Lead to Member workflow runs in the demo stack, with fake sources and no production data.

Defects found by looking at the new screens, fixed: the "Test data removed" message never appeared; the note column
of the ATS table was cut off at 1280 px; figures of "Where the jobs came from" were off-screen at 390 px; several
wordings. The search page now also shows the ranking time and how many judgements were used again.

## 8. ATS health

The health view is unchanged in how it decides: a platform is "verified working" only after a read with postings in
the last 7 days. Expected after the release, not observed on production: Rippling "not supported" (reader switched
off), Workable "verified working" once account reads have run, Recruitee more boards read per day. The other states
of the previous report stand. "Company career sites" appears as a source of its own, not as one of the 21 platforms.

## 9. Remaining risks and open decisions

1. Not deployed; production timing, SearXNG and AWS web search were not part of the measurements.
2. The page evaluation set has only 3 open postings on employer-owned hosts. Extending it to about 25 needs about 150
   more page requests to 21 company career sites; the work stopped at its limit of 200 requests and that limit is the
   owner's to raise.
3. The page tests use 62 trimmed copies of public career pages (424 kB, e-mail addresses, phone numbers and recruiter
   names removed). The repository is public; whether they are pushed is the owner's decision.
4. Half of the AI calls on company sites costs ATS-board discoveries (section 5). `AI_OPEN_WEB_SHARE` sets the share.
5. OpenAI's web search finds little on company sites, so Claude carries the open-web calls, which cost more per call.
6. Search engines return many closed posting pages; the yield per call on company sites is modest and varies by
   title.
7. After a worker restart the profile is extracted again and may be worded differently; the reranker's memory then
   misses and it reads again in the background. Judgements are keyed by the resume and still hit.
8. For a very large candidate set the reranker does not reach the weakest keyword matches within its wait. One
   measured case showed no loss; it is one case.
9. Only company pages that verify with a plain request are remembered. Each remembered page is one row of the board
   table; rows of postings that are gone are not removed yet.
10. Results reach the Team Lead when the search has finished; showing verified jobs before the end is not built.
11. A 500 answer was seen once in the browser suite's demo stack, probably the web proxy reusing a connection the API
    had closed after 5 idle seconds. Adding `--timeout-keep-alive` to the API's start command would address it; the
    deployment files were left unchanged.
12. The OpenAI model in use is named as superseded in OpenAI's documentation; `OPENAI_SEARCH_MODEL` lets the search
    use another one without a code change.

## 10. Rollout and rollback

Status: implemented and tested locally, committed locally, not pushed, not deployed. A push to `main` is a release;
it needs the owner's word.

Release: the usual pipeline (tests, images, release with a database dump first, health check). The one migration
(`f2a9c4d7e1b3`) adds three columns to `index_boards` and the previous build works on the new schema.

After the release:

1. Health: `/api/health` 200 with the worker online; Admin Settings, ATS Platforms: Rippling "not supported".
2. One real search as a Team Lead; on its page "Where the jobs came from" shows the ranking time.
3. The next day: Workable "verified working", Recruitee boards read.

Rollback: the release script starts the previous build again by itself when the new one is not healthy within three
minutes. By hand: revert the commit and push, or release the previous commit; the migration can stay (downgrade
exists and was run). The dump taken before the release is the newest file in `/opt/jobfinder/backups`.

Switches that undo single parts without a release (server `.env`, then restart API and worker):

| Setting | Effect |
| --- | --- |
| `RANK_CACHE_DAYS=0` | no remembered judgements |
| `TECHSARA_RERANK_WAIT_SECONDS=90` | the ranking stage waits for the reranker as long as before |
| `CAREER_PAGE_MEMORY=false` | company-site postings are not kept |
| `AI_OPEN_WEB_SHARE=0` | AI web search on ATS hosts only |
| `MAX_PAGE_VERIFICATIONS=40`, `MAX_PAGE_VERIFICATIONS_MAX=600` | the old page budgets |
| `RIPPLING_ENABLED=true` | Rippling reader on again |
| `WORKABLE_SEARCH_ENABLED=false` | Workable's undocumented cross-company search off |

## 11. Changed files

New: `app/rank_cache.py`, `alembic/versions/20261121_f2a9c4d7e1b3_catalog_read_history.py`, the test files of
section 7, `tests/fixtures/pages/`, `tests/fixtures/boards/workable_account.json`, this report.

Changed, backend: `app/search_engine.py`, `app/ai_orchestrator.py`, `app/job_verifier.py`, `app/browser_pool.py`,
`app/search_plan.py`, `app/search_scope.py`, `app/board_catalog.py`, `app/job_normalizer.py`, `app/ats_platforms.py`,
`app/config.py`, `app/portal/index_pg.py`, `app/portal/search_runner.py`, `app/portal/services/ats_health.py`,
`app/portal/services/settings.py`, `app/providers/openai_provider.py`, `claude_provider.py`, `techsara_provider.py`,
`workable.py`, `registry.py`, `ats_pages.py`, `ats_startup.py`.

Changed, frontend: `components/search/search-origins.tsx`, `search-run.tsx`, `search-progress.tsx`, `search-form.tsx`,
`platform-filter.tsx`, `components/views/ats-health.tsx`, `admin-settings.tsx`, `search-detail.tsx`.

Changed, tests and docs: `tests/e2e/` (`run.sh`, `server.py`, `fixtures.py`, `ui_checks_salesforce.py`,
`ui_crawl.py`), several unit tests, `docs/ats-platforms.md`, `docs/board-sources.md`, `README.md`, `.env.example`.
