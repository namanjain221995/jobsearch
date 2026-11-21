# Board sources: imported catalogue and extra readers

Status: 2026-11-21 (first written 2026-11-18). Owner of this file: board-sources work. The engine engineer wires the hooks below.

## What exists

| Piece | File |
|---|---|
| Board lists (CSV `name,slug,url`, MIT, from kalil0321/ats-scrapers `ats-companies/`, 2026-11-18) | `app/data/boards/*.csv`, `LICENSE-ats-scrapers`, `SOURCE.txt` |
| Loading, id normalization, dedupe, progressive reading | `app/board_catalog.py` |
| Import command | `python -m app.portal.import_boards` (`app/portal/import_boards.py`) |
| SmartRecruiters + BambooHR readers (both OFF by default) | `app/providers/ats_extra.py`, exposed by `app/providers/boards.py: extra_specs()` |
| Index support | `app/portal/index_pg.py`: `register_catalog`, `board_slugs`, `catalog_due`, `catalog_touch`, `catalog_result`, `catalog_stats`; `known_boards` changed |
| Read history (migration `f2a9c4d7e1b3`) | `index_boards.tried_at`, `us_count`, `misses`: see "Reading order" below |
| Workable account reader (documented public endpoint) | `app/providers/workable.py: fetch_account` |

Lists shipped: greenhouse, lever, ashby, workable, workday, smartrecruiters, bamboohr. Recruitee, Breezy, Rippling,
Pinpoint, Personio, Teamtailor readers were NOT built (cut for the release); their lists were not copied.

## Hooks the engine must call

1. **Progressive reading (required for imported boards to ever be read):**
   ```python
   from app import board_catalog
   board_catalog.start_catalog_trickle(engine)      # once, e.g. at the end of SearchEngine.prewarm()
   ```
   Every 60 s it asks `index.catalog_due({ats: n})` for at most `n` due catalogue boards per enabled keyed source and
   starts each through `engine._spawn(Unit(ats, key, ""), "catalog")`, i.e. the normal `_refresh_unit` path: same
   `HostLimiter` (Workday 1 request / 1.5 s and the 10-minute 429 pause), robots.txt, `INDEX_TTL_SECONDS`, dead-board
   marking. It skips a tick while more than `BOARD_CATALOG_MAX_INFLIGHT` units are in flight, so searches come first.
   With the SQLite index (no `catalog_due`) it does nothing. Workday catalogue reads use scope `""` (all US postings,
   first page) only to find out whether the tenant is live; searches then query it per title as usual.
   Workable is not a keyed source in the registry (its entry is the cross-company search), but the same entry reads
   one employer account when the unit key is an account id, so the trickle reads Workable catalogue accounts too
   (`board_catalog.ACCOUNT_SOURCES`). Until 2026-11-21 it skipped them: no Workable catalogue account was ever read.
   After every board read the engine calls `board_catalog.note_read(index, unit, jobs)` (jobs = `None` when the read
   failed), which stores the number of US postings or a miss (`PgJobIndex.catalog_result`).
2. **Extra sources (only when switched on):** in `app/providers/registry.py` after `SPECS` is built:
   ```python
   from . import boards
   SPECS.update(boards.extra_specs())
   ```
   `extra_specs()` returns `{}` unless `BOARD_SOURCES_SMARTRECRUITERS=1` / `BOARD_SOURCES_BAMBOOHR=1`. Spec names equal
   `Job.ats` (`smartrecruiters`, `bamboohr`) so `SPECS.get(job.ats).detail` works for on-demand descriptions. The UI
   source list comes from `engine.specs` (`app/main.py:138`, portal worker `app/portal/worker.py:42`), so the merge is
   all that is needed to show them. Suggested `HOST_LIMITS` in search_engine.py: `smartrecruiters: 2, bamboohr: 2`; the
   readers install a 1 s interval on the shared limiter themselves if none is set.

## Settings (defaults live in the modules; move into app/config.py)

| Env var | Default | Where |
|---|---|---|
| `BOARD_CATALOG_TRICKLE` | `1` | board_catalog |
| `BOARD_CATALOG_PER_MINUTE_GREENHOUSE` / `_LEVER` / `_ASHBY` / `_WORKABLE` / `_WORKDAY` / `_SMARTRECRUITERS` / `_BAMBOOHR` | 20 / 8 / 8 / 4 / 2 / 4 / 6 | board_catalog |
| `BOARD_CATALOG_PER_MINUTE_RECRUITEE` / `_PINPOINT` / `_RIPPLING` | 15 / 6 / 4 (Pinpoint and Rippling are read only while their reader is switched on) | board_catalog |
| `BOARD_CATALOG_REFRESH_HOURS` | 24 (a board whose last read brought US postings is read again when this old) | board_catalog |
| `BOARD_CATALOG_RECHECK_DAYS` | 7 (first re-check of a board that had no US posting; doubles with every further miss) | board_catalog |
| `BOARD_CATALOG_RETRY_HOURS` | 6 (first retry of a board whose read failed; doubles with every further miss) | board_catalog |
| `BOARD_CATALOG_DEAD_DAYS` | 30 (first retry of a board that answered "not found"; doubles likewise) | board_catalog |
| `BOARD_CATALOG_MAX_BACKOFF_DAYS` | 90 (no wait grows beyond this) | board_catalog |
| `WORKABLE_SEARCH_ENABLED` | `true` (Workable's undocumented cross-company search; `false` keeps only the account reader) | app/config.py |
| `RIPPLING_ENABLED` | `false` since 2026-11-21 (until Rippling's terms are reviewed) | app/config.py |
| `BOARD_CATALOG_MAX_INFLIGHT` | 150 | board_catalog |
| `BOARD_SOURCES_SMARTRECRUITERS` | off | ats_extra |
| `BOARD_SOURCES_BAMBOOHR` | off | ats_extra |
| `SMARTRECRUITERS_MAX_PAGES` | 10 (x100 postings) | ats_extra |
| `SMARTRECRUITERS_INTERVAL_SECONDS` / `BAMBOOHR_INTERVAL_SECONDS` | 1.0 | ats_extra |

## Why registration cannot cause a fetch storm

- `import_boards` inserts rows `origin='catalog', ok=true, job_count=0, checked_at=NULL` and fetches nothing.
- `known_boards()` now excludes catalogue rows with `job_count = 0`, so searches/prewarm plan exactly what they planned
  before the import (otherwise 19,000 unread boards would have pushed Workday/Ashby out of `max_boards_per_search`).
- Only the trickle reads catalogue boards, at the per-ATS rate above. `catalog_touch` stamps each attempt
  (`tried_at`; `checked_at` stays the time of the last answer, which the health view reads) so a failing board is not
  retried every minute. A board that answers with postings gets `job_count > 0` and from then on is a normal known
  board, ordered by `job_count DESC` like web-search finds, so a search reads the boards with most postings first.
  A 404 marks it dead (`ok=false`, board sources).

## Reading order (2026-11-21)

The per-minute allowance of an ATS is the only thing that decides how many requests are made. Inside it,
`PgJobIndex.catalog_due` picks boards in this order:

1. **Live**: the last read brought US postings (`misses = 0`, `us_count > 0`; boards read before US postings were
   counted qualify with `job_count > 0`). Read again once `BOARD_CATALOG_REFRESH_HOURS` old, the longest unread first
   (Workday tenants, which searches query per title anyway, once `BOARD_CATALOG_RECHECK_DAYS` old).
   While never-read boards are waiting, refreshes take at most half of the allowance (rounded up).
2. **New**: never read.
3. **Retry**: the last read failed (first wait `BOARD_CATALOG_RETRY_HOURS`) or brought nothing for the US (first
   wait `BOARD_CATALOG_RECHECK_DAYS`). Every further miss in a row doubles the wait: 6 h, 12 h, 24 h ... and 7, 14,
   28 days ...
4. **Dead** (404): `BOARD_CATALOG_DEAD_DAYS`, then double.

No wait is longer than `BOARD_CATALOG_MAX_BACKOFF_DAYS`. A read with US postings clears `misses`. What was recorded
before: `ok`, `job_count` (postings of every country) and `checked_at` on the board, and the last read of each unit in
`index_units` (pruned after three days). Not recorded before, hence the three columns: the number of US postings, the
number of misses in a row, and the time of an attempt as distinct from an answer.

Recruitee rate, 15 boards a minute (was 6): every Recruitee board is its own employer host and one read is two
requests to it (robots.txt, then the one API call that returns the whole board); the host is then left alone for at
least 24 hours. The platform-wide limiter stays at one request a second, so 15 boards take 30 of the 60 request slots
of a minute and searches keep the other half. First pass of the 2,956 boards: 3.3 hours of trickle time (8.2 hours at
6); at most 7.0 hours if refreshes of live boards use their full share (8 of the 15) the whole time. The trickle pauses while a
search has more than `BOARD_CATALOG_MAX_INFLIGHT` units in flight, so wall-clock time is longer on a busy server.
Workday (2) and every other rate are unchanged.

## Import

```
python -m app.portal.import_boards [--dry-run] [--only greenhouse,lever] [--validate N] [--simplify] [--dsn postgresql://...] [--json]
```
Idempotent (`ON CONFLICT DO NOTHING`), dedupes against `SEED_BOARDS`, `WORKDAY_SITES` and every existing
`index_boards` row (case-insensitive except SmartRecruiters). Rows that are not a well-formed id are skipped (EU Lever
`jobs.eu.lever.co` boards, which the Lever reader cannot read; Ashby slugs with spaces; anything with `/`, `..`,
non-ASCII). `--simplify` (off by default) downloads SimplifyJobs New-Grad-Positions `listings.json` and keeps only board
ids (greenhouse/lever/ashby/workable/workday/smartrecruiters); their listing content is discarded (no licence).

Counts (against a copy of the live index, 2026-11-18):

| ATS | new | existing | skipped | live ratio (10 sampled) | postings / live board | US postings / live board | bytes / posting (packed) |
|---|---|---|---|---|---|---|---|
| greenhouse | 7,094 | 269 | 0 | 0.8 | 12.1 | 10.8 | 3,631 |
| lever | 2,245 | 57 | 75 | 0.9 (1 dead) | 12.6 | 8.7 | 3,571 |
| ashby | 3,852 | 231 | 88 | 0.8 | 23.4 | 9.5 | 3,249 |
| workable | 4,752 (plan only) | 0 | 0 | not measured | not measured | not measured | not measured |
| workday | 5,157 (plan only) | 158 | 1 | not measured | not measured | not measured | not measured |

Workable and Workday validation was not measured: a combined run including them did not finish inside 15 minutes
(most likely the Workday 10-minute 429 pause; not diagnosed). The non-dry run against a scratch database was not done.

Estimates (greenhouse/lever/ashby only, from the sample): about 166,000 extra postings once read
(7,094 x 9.7 + 2,245 x 11.3 + 3,852 x 18.7 per board, including empty boards). The current index measures about
4.9 KB per posting including indexes (276 MB / 56,833 postings), so roughly +0.8 GB. Read-all time at the default
rates, ATS in parallel: greenhouse 6 h, lever 4.7 h, ashby 8 h, workable 20 h, workday 43 h.

## Common Crawl discovery

`--common-crawl [N]` registers board ids found in the Common Crawl URL index with origin `commoncrawl`; `known_boards`, `catalog_due`, `catalog_touch` and `catalog_stats` treat `commoncrawl` like `catalog`. Code: app/portal/common_crawl.py (see its docstring for why the index shards on data.commoncrawl.org are read instead of the robots-disallowed CDX API).

## Terms and robots.txt findings (2026-11-18)

| Vendor | Endpoint | robots.txt for our UA | Terms | Default |
|---|---|---|---|---|
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/<id>/postings` | `User-agent: *  Disallow: /` (only LinkedInBot allowed) | Posting API is documented for career sites; robots forbids us | **off**; reader refuses while robots disallows |
| BambooHR | `<co>.bamboohr.com/careers/list`, `/careers/<id>/detail` | allows all | ToS forbids "any robot, spider, other automated device ... to monitor or copy any content from the Service" | **off** |
| Recruitee | `<co>.recruitee.com/api/offers/` (documented Careers Site API) | allows (only `/v/` disallowed) | Tellent Recruitee terms read 2026-11-21: no clause on automated access | **on** (providers/ats_startup.py) |
| Breezy | `<co>.breezy.hr/json` | allows | not reviewed (terms page 404) | not built |
| Rippling | list `ats.rippling.com/api/v2/board/<slug>/jobs`, detail `api.rippling.com/platform/api/ats/v1/board/<slug>/jobs/<uuid>` (both undocumented) | ats: only `/internal/` disallowed; api: robots.txt 404 (no rules) | could not be read 2026-11-21 (script-rendered legal pages) | **off** since 2026-11-21 until the terms are reviewed (owner's decision); `RIPPLING_ENABLED=1` switches it on, then add `ats.rippling.com` to `SEARXNG_SITES` |
| Workable, one account | documented: `www.workable.com/api/accounts/<account>?details=true` (https://workable.readme.io/reference/jobs-1, help article "Using the Workable API to create a careers page"); it answers 302 to `apply.workable.com/api/v1/widget/accounts/<account>?details=true`, which the reader asks directly | apply: `Disallow:` (nothing), `Content-Signal: search=yes, ai-input=yes, ai-train=no`; www: only `/user_password_resets`, `/admin`, `/auth/google`, `/j/` | workable.com/terms (last updated 2026-03-02), read through a page fetcher: no clause on robots, scraping or automated access was found | **on** (part of the `workable` source) |
| Workable, cross-company search | `jobs.workable.com/api/v1/jobs?query=&location=` (undocumented; the endpoint of Workable's job-board page) | `Allow: /search/*`, `Disallow: /search*?*`, `/search`, `*/search/$`, `/profile*`, `/company-login-unauthorized`; `/api/` is not named | jobs.workable.com/terms (2023-09-21), read through a page fetcher: no clause on automated access was found; clause 15.2 licenses the site "only for the purpose of looking for a job and applying to job Openings", clause 4.1 forbids to "implement any form of software or scripts onto the Website" | **on**; `WORKABLE_SEARCH_ENABLED=0` switches it off. Owner's decision whether an undocumented endpoint should stay |
| Pinpoint | `<co>.pinpointhq.com/postings.json` (documented) | employer tenants: `Disallow: /` (ada, afm on 2026-11-21); Pinpoint's own board allows | acceptable-use policy: no clause on reading career pages | **off**; reader refuses a tenant while its robots disallows |
| Personio | `<co>.jobs.personio.de/xml` | allows | not reviewed (429 on terms page) | not built |
| Teamtailor | `<co>.teamtailor.com/jobs.rss` | allows; `Content-Signal: search=yes, ai-train=no` | not reviewed | not built |

## Workable on production: why it was "configured, not verified" (2026-11-21)

Two causes, both in the code. A refusal of the server's address by Workable is not needed to explain the state and
does not fit it: a refused read (HTTP 403 or 429) is stored as a failed unit, and the health view then says
"temporarily unavailable" with the error, not "configured, not verified". Whether Workable answers the server at
all was not verified (the server was not touched); from this machine all three Workable hosts answered HTTP 200.

1. The 6,527 Workable accounts in `index_boards` were never read. The trickle only read sources whose registry entry
   is `keyed`, and Workable's entry is the query-based search, so `catalog_tick` left it out; and `known_boards()`
   hides catalogue rows until a read found postings, so no search planned them either. Fixed: see hook 1 above.
2. The cross-company search was not asked either. A search cuts its planned units to its budget
   (`MAX_BOARDS_PER_SEARCH`, 1,500 for a 25-job search) in registry order, Greenhouse first. With 6,629 Greenhouse
   boards known, all 1,500 units were Greenhouse boards and the Workable, Amazon and Himalayas queries and the
   Remote OK and Jobicy feeds fell outside the cut (reproduced with those counts in
   `tests/test_workable_accounts.py`). On a fresh database only 584 units are planned, which is why the same code
   returned 81 Workable jobs locally. Fixed: `registry.within_budget` keeps the handful of query and feed units and
   cuts employer boards only.

One vacancy, two readers: the job board numbers its listings with ids of its own (a UUID), the account endpoint gives
the employer's posting code (`apply.workable.com/j/{CODE}`), and neither answer contains the other's id. Both used to
be stored as posting ids of the ATS "workable", and postings of one ATS with different ids are never merged (they
count as different requisitions), so a vacancy read both ways appeared twice, in one search and in the stored
postings. A listing from the cross-company search now carries no posting id: it is identified by its address and
merged with the account's record by employer, title and place; the account's record wins because it has the apply
link. Side effect: two job-board listings of one employer with the same title and place now count as one.

Still open, for the engine owner: the same cut leaves no room for the boards of Ashby, Lever, Workday, Recruitee and
Workable accounts in an unfiltered search once Greenhouse alone exceeds the budget. Their postings then count as
verified only for `INDEX_TTL_SECONDS` after a trickle read. Not changed here.

Live check of the account reader (2026-11-21, 5 requests: robots.txt, three accounts, one unknown account):
huggingface 10 postings (5 US), rokt 9 (3 US), blueground 5 (2 US); every posting had a description, a posting date
and a direct apply address; the unknown account answered 404 and was reported as not found.
