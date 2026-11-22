"""Operational state of the applicant-tracking systems and the search providers, for Admins (GET /api/integrations/ats).

Nothing here is a claim: every state is derived at request time from
- app/ats_platforms.py (the platform's access class and the name of its reader),
- providers/registry.py and the settings (is that reader installed, configured and switched on), and
- the job index (what was really read).

Evidence in the index, per source name:
- index_units: one row per read. `ok` and `count` say whether the last read of that unit worked and how many
  postings it stored, `fetched_at` when, `error` why it failed (written by fail_unit).
- index_boards: one row per employer board. `ok`, `job_count` and `checked_at` are written after every read of a
  board (record_board); `ok = false` marks a board that answered "not found". Index units older than three days are
  pruned, board rows are not, so both are read.
A read counts as successful only when it worked and brought at least one posting.

The evidence costs two aggregate statements and is kept for CACHE_SECONDS; the states are computed on every request, so
switching a source on or off shows at once."""
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ... import ats_platforms, career_sites
from ...config import Settings
from ...providers import registry
from ..schemas import AtsHealthOut, AtsPlatformHealth, AwsWebSearchStatus, ProviderHealth

WINDOW_DAYS = 7
CACHE_SECONDS = 60.0

_EVIDENCE_SQL = """
SELECT src,
       count(*) FILTER (WHERE key <> '*') AS boards,
       count(*) FILTER (WHERE good AND key <> '*') AS boards_ok,
       count(*) FILTER (WHERE good) AS reads_ok,
       count(*) FILTER (WHERE bad AND good IS NOT TRUE) AS failed,
       max(good_at) AS last_ok, coalesce(sum(postings), 0) AS postings, max(err) AS err
FROM (SELECT src, key, bool_or(good) AS good, bool_or(bad) AS bad, max(good_at) AS good_at, sum(postings) AS postings, max(err) AS err
      FROM (SELECT ats AS src, slug AS key, (ok AND job_count > 0 AND checked_at > :since) AS good,
                   (NOT ok AND checked_at > :since) AS bad, CASE WHEN ok AND job_count > 0 THEN checked_at END AS good_at,
                   0::bigint AS postings, NULL::text AS err
            FROM index_boards
            UNION ALL
            SELECT src, key, (ok AND count > 0 AND fetched_at > :since), (NOT ok AND fetched_at > :since),
                   CASE WHEN ok AND count > 0 THEN fetched_at END, CASE WHEN ok THEN count ELSE 0 END::bigint,
                   CASE WHEN NOT ok AND fetched_at > :since AND error <> '' THEN error END
            FROM index_units) AS reads
      GROUP BY src, key) AS boards
GROUP BY src
"""
_CRAWL_SQL = ("SELECT count(*), count(checked_at), count(*) FILTER (WHERE ok AND job_count > 0) "
              "FROM index_boards WHERE origin = 'commoncrawl'")


@dataclass(frozen=True)
class Evidence:
    """What the index holds for one source name."""
    boards: int = 0             # employer boards known (board rows and read units; the cross-company key "*" is not a board)
    boards_ok: int = 0          # of those, read successfully with at least one posting inside the window
    reads_ok: int = 0           # the same, including a cross-company read
    failed: int = 0             # boards whose reads inside the window failed and never succeeded
    last_ok: float | None = None    # epoch seconds of the last successful read with postings, at any time
    postings: int = 0           # postings stored by reads that worked
    error: str = ""             # one error text of a failed read inside the window


async def load_evidence(db: AsyncSession, now: float | None = None) -> tuple[dict[str, Evidence], dict[str, int]]:
    """(evidence per source name, Common Crawl catalogue counts). Two statements."""
    since = (now or time.time()) - WINDOW_DAYS * 86400
    rows = (await db.execute(text(_EVIDENCE_SQL), {"since": since})).all()
    evidence = {r[0]: Evidence(boards=int(r[1]), boards_ok=int(r[2]), reads_ok=int(r[3]), failed=int(r[4]),
                               last_ok=float(r[5]) if r[5] is not None else None, postings=int(r[6]), error=r[7] or "")
                for r in rows}
    crawl = (await db.execute(text(_CRAWL_SQL))).one()
    counts = {"boards": int(crawl[0]), "read": int(crawl[1]), "live": int(crawl[2])}
    counts.update(await _career_sites(db, since))
    return evidence, counts


async def _career_sites(db: AsyncSession, since: float) -> dict[str, int]:
    """Company career sites (app/career_sites.py): sites known, sites crawled inside the window, sites not readable, and
    company-site postings in the index. Two statements, inside a savepoint: a database whose migration a8c5e3f1d2b7 has
    not run yet shows no sites instead of failing the whole view."""
    try:
        async with db.begin_nested():
            sites = (await db.execute(text(career_sites.SITE_STATS_SQL), {"since": since})).one()
            pages = (await db.execute(text(career_sites.PAGE_STATS_SQL))).scalar_one()
    except Exception:
        return {}
    return {"career_sites": int(sites[0]), "career_sites_read": int(sites[1]), "career_sites_ok": int(sites[2]),
            "career_sites_blocked": int(sites[3]), "career_postings": int(pages)}


async def cached_evidence(db: AsyncSession, holder) -> tuple[dict[str, Evidence], dict[str, int]]:
    """The evidence, at most CACHE_SECONDS old. `holder` is any object that keeps the cache (the application state)."""
    kept = getattr(holder, "ats_health_cache", None)
    if kept and time.monotonic() - kept[0] < CACHE_SECONDS:
        return kept[1]
    fresh = await load_evidence(db)
    holder.ats_health_cache = (time.monotonic(), fresh)
    return fresh


def invalidate(holder) -> None:
    holder.ats_health_cache = None


def _when(epoch: float | None) -> datetime | None:
    return datetime.fromtimestamp(epoch, timezone.utc) if epoch else None


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count:,} {one if count == 1 else (many or one + 's')}"


def reader_switch(reader: str, settings: Settings, off: set[str]) -> str:
    """"" when the reader is installed, configured and switched on; otherwise the plain reason why it is not."""
    spec = registry.SPECS.get(reader)
    if spec is None:
        return "Its reader is not switched on in this installation."
    if reader in off:
        return "Its reader is switched off under Job sources."
    if not spec.needs(settings):
        return "Its reader is switched off in the server settings."
    return ""


def platform_state(platform: ats_platforms.Platform, settings: Settings, off: set[str], found: Evidence | None) -> tuple[str, str]:
    """(state, reason) of one platform. `found` is the index evidence for the platform's reader."""
    if platform.access == "restricted":
        return "unsupported", "The vendor's terms or robots.txt do not allow automated reading, so nothing is read."
    if not platform.reader:
        return "public_discovery_only", ("There is no reader for this platform. Postings are found on public career pages "
                                         "and each one is verified on its own page.")
    why_off = reader_switch(platform.reader, settings, off)
    if why_off:
        if platform.access == "public_pages":            # the reader is off, page-by-page verification still works
            return "public_discovery_only", ("Its board reader is switched off. Postings found on public career pages "
                                             "are verified one by one on their own page.")
        return "unsupported", why_off
    found = found or Evidence()
    if found.reads_ok > 0:
        if found.boards_ok > 0:
            return "verified_working", (f"{_n(found.boards_ok, 'board')} read successfully with postings "
                                        f"in the last {WINDOW_DAYS} days.")
        return "verified_working", f"Read successfully with postings in the last {WINDOW_DAYS} days."
    if found.failed > 0:
        detail = f" Latest error: {found.error}." if found.error else ""
        return "temporarily_unavailable", (f"No read worked in the last {WINDOW_DAYS} days and "
                                           f"{_n(found.failed, 'board')} failed.{detail}")
    if found.last_ok:
        return "configured_unverified", f"The last successful read is more than {WINDOW_DAYS} days old."
    if found.boards > 0:
        return "configured_unverified", f"{_n(found.boards, 'board')} known, none read successfully yet."
    return "configured_unverified", "The reader is switched on, but no read is recorded yet."


def platforms(settings: Settings, off: set[str], evidence: dict[str, Evidence]) -> list[AtsPlatformHealth]:
    out = []
    for platform in ats_platforms.PLATFORMS.values():          # read at call time: the list is edited by others
        found = evidence.get(platform.reader) if platform.reader else None
        state, reason = platform_state(platform, settings, off, found)
        found = found or Evidence()
        out.append(AtsPlatformHealth(
            id=platform.id, label=platform.label, startup=platform.startup, access=platform.access,
            access_note=ats_platforms.ACCESS.get(platform.access, ""), hosts=list(platform.hosts), docs=platform.docs,
            note=platform.note, reader=platform.reader,
            reader_enabled=bool(platform.reader) and not reader_switch(platform.reader, settings, off),
            state=state, reason=reason, boards_known=found.boards, boards_read_ok=found.boards_ok, boards_failed=found.failed,
            open_postings=found.postings, last_success_at=_when(found.last_ok)))
    return out


# ---------------------------------------------------------------- search providers
_WEB_PROVIDERS = ("techsara_web", "aws_web_search", "searxng", "serpapi", "tinyfish")     # registry source names


def _key_provider(id: str, label: str, configured: bool, model: str) -> ProviderHealth:
    return ProviderHealth(id=id, label=label, kind="ai", state="configured" if configured else "not_configured", detail=model,
                          reason="A key is set on the server." if configured else "No key is set on the server.")


def providers(settings: Settings, off: set[str], aws: AwsWebSearchStatus | None, crawl: dict[str, int]) -> list[ProviderHealth]:
    """The search providers, from the same settings, registry entries and AWS counter GET /api/integrations shows.
    A provider is never reported as working: the portal keeps no record of provider answers, only whether it is set up."""
    out = [_key_provider("openai", "OpenAI", bool(settings.openai_api_key), settings.openai_model),
           _key_provider("claude", "Claude", bool(settings.anthropic_api_key), settings.anthropic_model),
           _key_provider("techsara", "TechSara model", settings.techsara_configured, settings.techsara_model)]
    for name in _WEB_PROVIDERS:
        spec = registry.SPECS.get(name)
        if spec is None:
            continue
        if name in off:
            state, reason = "switched_off", "Switched off under Job sources."
        elif not spec.needs(settings):
            state, reason = "not_configured", "Not configured on the server."
        else:
            state, reason = "configured", "Configured and switched on."
        item = ProviderHealth(id=name, label=spec.label, kind="web_search", state=state, reason=reason, detail=spec.note)
        if name == "aws_web_search" and aws is not None:
            if aws.counter_available:
                item.month, item.used, item.limit = aws.month, aws.used, aws.limit
                item.reason = f"{reason} {aws.used:,} of {aws.limit:,} searches used this month."
            elif aws.note:
                item.reason = f"{reason} {aws.note}"
        out.append(item)
    boards = crawl.get("boards", 0)
    out.append(ProviderHealth(
        id="common_crawl", label="Common Crawl catalogue", kind="catalogue", state="configured" if boards else "not_configured",
        detail="Employer boards found in the open Common Crawl address index; each is read through its platform's reader.",
        reason=(f"{_n(boards, 'board')} imported, {crawl.get('read', 0):,} read so far, {crawl.get('live', 0):,} with postings."
                if boards else "No boards have been imported from Common Crawl.")))
    out.append(_career_site_line(settings, off, crawl))
    return out


def _career_site_line(settings: Settings, off: set[str], counts: dict[str, int]) -> ProviderHealth:
    """"Company career sites": employers' own sites the crawler reads without any ATS platform. Counts only."""
    spec = registry.SPECS.get(registry.CAREER_PAGE)
    sites, read, postings = counts.get("career_sites", 0), counts.get("career_sites_read", 0), counts.get("career_postings", 0)
    if registry.CAREER_PAGE in off:
        state, why = "switched_off", "Switched off under Job sources."
    elif spec is None or not spec.needs(settings):
        state, why = "switched_off", "Company-site postings are not kept (CAREER_PAGE_MEMORY is off)."
    elif not getattr(settings, "career_site_crawl", True):
        state, why = "switched_off", "The crawler is switched off in the server settings (CAREER_SITE_CRAWL)."
    else:
        state, why = ("configured" if sites else "not_configured"), ""
    blocked = counts.get("career_sites_blocked", 0)
    figures = (f"{_n(sites, 'site')} known, {read:,} read in the last {WINDOW_DAYS} days, {_n(postings, 'posting')} in the index"
               + (f"; {_n(blocked, 'site')} not readable (left alone)." if blocked else "."))
    if not sites and not postings:
        figures = "No company career site is known yet: a site is added when a search verifies a posting on it, or from a file."
    return ProviderHealth(
        id="career_sites", label="Company career sites", kind="catalogue", state=state, reason=f"{why} {figures}".strip(),
        detail="Employers' own career sites, read in the background without any ATS platform: sitemaps, job data on the "
               "listing page, then the browser; each posting is verified on its own page. One site at most once a week.")


async def report(db: AsyncSession, settings: Settings, off: set[str], aws: AwsWebSearchStatus | None, holder) -> AtsHealthOut:
    evidence, crawl = await cached_evidence(db, holder)
    return AtsHealthOut(generated_at=datetime.now(timezone.utc), window_days=WINDOW_DAYS, platforms=platforms(settings, off, evidence),
                        providers=providers(settings, off, aws, crawl))
