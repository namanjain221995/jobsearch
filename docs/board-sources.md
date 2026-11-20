# Board sources: imported catalogue and extra readers

Status: 2026-11-18. Owner of this file: board-sources work. The engine engineer wires the hooks below.

## What exists

| Piece | File |
|---|---|
| Board lists (CSV `name,slug,url`, MIT, from kalil0321/ats-scrapers `ats-companies/`, 2026-11-18) | `app/data/boards/*.csv`, `LICENSE-ats-scrapers`, `SOURCE.txt` |
| Loading, id normalization, dedupe, progressive reading | `app/board_catalog.py` |
| Import command | `python -m app.portal.import_boards` (`app/portal/import_boards.py`) |
| SmartRecruiters + BambooHR readers (both OFF by default) | `app/providers/ats_extra.py`, exposed by `app/providers/boards.py: extra_specs()` |
| Index support | `app/portal/index_pg.py`: `register_catalog`, `board_slugs`, `catalog_due`, `catalog_touch`, `catalog_stats`; `known_boards` changed |

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
| `BOARD_CATALOG_RECHECK_DAYS` | 7 (empty or failed catalogue boards are tried again after this) | board_catalog |
| `BOARD_CATALOG_MAX_INFLIGHT` | 150 | board_catalog |
| `BOARD_SOURCES_SMARTRECRUITERS` | off | ats_extra |
| `BOARD_SOURCES_BAMBOOHR` | off | ats_extra |
| `SMARTRECRUITERS_MAX_PAGES` | 10 (x100 postings) | ats_extra |
| `SMARTRECRUITERS_INTERVAL_SECONDS` / `BAMBOOHR_INTERVAL_SECONDS` | 1.0 | ats_extra |

## Why registration cannot cause a fetch storm

- `import_boards` inserts rows `origin='catalog', ok=true, job_count=0, checked_at=NULL` and fetches nothing.
- `known_boards()` now excludes catalogue rows with `job_count = 0`, so searches/prewarm plan exactly what they planned
  before the import (otherwise 19,000 unread boards would have pushed Workday/Ashby out of `max_boards_per_search`).
- Only the trickle reads catalogue boards, at the per-ATS rate above. `catalog_touch` stamps each attempt so a failing
  board is not retried every minute. A board that answers with postings gets `job_count > 0` and from then on is a
  normal known board, ordered by `job_count DESC` like web-search finds, so a search reads the boards with most
  postings first. A 404 marks it dead (`ok=false`, board sources); an empty board is re-checked weekly.

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
| Rippling | list `ats.rippling.com/api/v2/board/<slug>/jobs`, detail `api.rippling.com/platform/api/ats/v1/board/<slug>/jobs/<uuid>` (both undocumented) | ats: only `/internal/` disallowed; api: robots.txt 404 (no rules) | could not be read 2026-11-21 (script-rendered legal pages) | **on**, `RIPPLING_ENABLED=0` switches it off |
| Pinpoint | `<co>.pinpointhq.com/postings.json` (documented) | employer tenants: `Disallow: /` (ada, afm on 2026-11-21); Pinpoint's own board allows | acceptable-use policy: no clause on reading career pages | **off**; reader refuses a tenant while its robots disallows |
| Personio | `<co>.jobs.personio.de/xml` | allows | not reviewed (429 on terms page) | not built |
| Teamtailor | `<co>.teamtailor.com/jobs.rss` | allows; `Content-Signal: search=yes, ai-train=no` | not reviewed | not built |
