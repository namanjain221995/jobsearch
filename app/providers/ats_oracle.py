"""Oracle Recruiting Cloud (Oracle Fusion HCM) candidate-experience sites: one employer site per board.

What was found on 2026-11-21 (docs/ats-platforms.md has the evidence):

- Every public career site of an Oracle Recruiting tenant (`https://{pod}.fa.[{region}.]oraclecloud.com/hcmUI/
  CandidateExperience/{lang}/sites/{site}`) loads its job list from
  `/hcmRestApi/resources/latest/recruitingCEJobRequisitions?finder=findReqs;siteNumber={site},...` and a posting from
  `/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?finder=ById;Id="{id}",siteNumber={site}`.
  Both answered without any login in a live check (Oracle's own careers site, eeho.fa.us2.oraclecloud.com, CX_45001).
- The tenant host has no robots.txt (HTTP 404), so robots.txt forbids nothing there; it is still read on every board.
- Oracle documents the resource (REST API for Oracle Fusion Cloud HCM, "Recruiting CE Job Requisitions") with the
  sentence "Note that the service endpoints in this resource category are only for Oracle internal use."

Because of that sentence the reader is OFF by default: the endpoint is public and anonymous, but Oracle does not offer
it to third parties. Switch it on with BOARD_SOURCES_ORACLE=1 only after the owner has decided to accept that.

Board key: "{host}|{siteNumber}", for example "eeho.fa.us2.oraclecloud.com|CX_45001". The host must be an Oracle
Fusion host (`*.fa.*.oraclecloud.com`); nothing else is ever contacted. Like Workday, a site is searched per title
(`scope`), newest first, a few pages at most; a search without a title reads the newest postings.
Every request goes through netguard (public address only), robots.txt and the shared limiter at one request per
second for all Oracle tenants together; 429/503 back off as the other paced sources do (providers/base.py)."""
import os
import re
from urllib.parse import quote

from .. import netguard
from ..job_normalizer import detect_work_mode, pretty_slug
from ..schemas import Job
from .ats_pages import ORACLE_HOST
from .base import USER_AGENT, BoardNotFound, Ctx, SourceError, iso_date, now_iso


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


ENABLED = _env_flag("BOARD_SOURCES_ORACLE")
GROUP = "oracle"
HOST_INTERVAL_SECONDS = 1.0
HOST_LIMIT = 1
PAGE_SIZE = 25                     # the page size Oracle's own career site asks for
MAX_PAGES = 4                      # at most 100 postings of one site per title
SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,59}")
JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")
_US = {"us", "usa", "united states", "united states of america"}
_SITE_NAME = re.compile(r"(?is)<meta[^>]+property=[\"']og:site_name[\"'][^>]+content=[\"']([^\"'<>]{2,80})[\"']")
DISPLAY_NAMES: dict[str, str] = {}            # board key -> employer name (seed lists may fill it)
_site_names: dict[str, str] = {}              # board key -> name read once from the site's own home page

LIST = ("https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
        "&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site},limit={limit},offset={offset},"
        "sortBy=POSTING_DATES_DESC{keyword}")
DETAIL = ("https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
          "&finder=ById;Id=%22{id}%22,siteNumber={site}")
SITE_HOME = "https://{host}/hcmUI/CandidateExperience/en/sites/{site}"
PUBLIC = "https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{id}"


async def _guard(url: str) -> None:
    """Every fetch passes the SSRF guard first (tests replace this; it resolves DNS)."""
    await netguard.assert_public_url(url)


def split_key(key: str) -> tuple[str, str]:
    """(host, site number) of a board key; BoardNotFound for anything that is not an Oracle Fusion host and site."""
    parts = (key or "").split("|")
    if len(parts) != 2 or len(key) > 200 or not ORACLE_HOST.fullmatch(parts[0]) or not SITE.fullmatch(parts[1]):
        raise BoardNotFound("Malformed Oracle Recruiting board key")
    return parts[0], parts[1]


def valid_key(key: str) -> bool:
    try:
        split_key(key)
    except BoardNotFound:
        return False
    return True


def _keyword(scope: str) -> str:
    """The finder's keyword variable. The finder syntax uses ';' ',' '=' and quotes, so only plain words are sent."""
    words = " ".join(re.sub(r"[^A-Za-z0-9+#.& -]", " ", scope or "").split())[:80]
    return f",keyword=%22{quote(words, safe='')}%22" if words else ""


def _pace(ctx: Ctx) -> None:
    ctx.limiter.intervals.setdefault(GROUP, HOST_INTERVAL_SECONDS)
    ctx.limiter.limits.setdefault(GROUP, HOST_LIMIT)


async def _get(ctx: Ctx, url: str):
    await _guard(url)
    _pace(ctx)
    return await ctx.json(GROUP, "GET", url, check_robots=True, retries=0)


async def company_name(ctx: Ctx, key: str) -> str:
    """The employer's name: from the seed list, else the `og:site_name` of the site's own home page (read once per
    board and process), else the pod label of the host."""
    if DISPLAY_NAMES.get(key):
        return DISPLAY_NAMES[key]
    if key not in _site_names:
        host, site = split_key(key)
        name = ""
        url = SITE_HOME.format(host=host, site=site)
        try:
            await _guard(url)
            _pace(ctx)
            rules = ctx.robots.peek(url) or await ctx.robots.rules(ctx.client, url)
            if rules.allows(url):
                async with ctx.limiter.slot(GROUP):
                    r = await ctx.client.get(url, timeout=ctx.timeout, follow_redirects=False,
                                             headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
                if r.status_code == 200 and (m := _SITE_NAME.search(r.text[:300_000])):
                    name = " ".join(m.group(1).split())
        except Exception:
            name = ""
        _site_names[key] = name or pretty_slug(host.split(".")[0])
    return _site_names[key]


def _job(key: str, company: str, raw: dict, stamp: str) -> Job | None:
    if not isinstance(raw, dict):
        return None
    host, site = split_key(key)
    jid, title = str(raw.get("Id") or "").strip(), str(raw.get("Title") or "").strip()
    if not title or not JOB_ID.fullmatch(jid):
        return None
    places = [str(raw.get("PrimaryLocation") or "").strip()]
    for extra in raw.get("secondaryLocations") or []:
        if isinstance(extra, dict) and extra.get("Name"):
            places.append(str(extra["Name"]).strip())
    places = [p for p in dict.fromkeys(places) if p]
    country = str(raw.get("PrimaryLocationCountry") or "").strip()
    hint = "United States" if country.lower() in _US else country.upper()
    return Job(
        title=title, company=company, location="; ".join(places[:6]) or "Not listed",
        work_mode=detect_work_mode(str(raw.get("WorkplaceType") or ""), places[0] if places else ""),
        url=PUBLIC.format(host=host, site=site, id=jid), apply_url="", source="Oracle Recruiting Cloud",
        sources=["Oracle Recruiting Cloud"], ats="oracle", ats_id=f"{host.split('.')[0]}:{site}:{jid}", board=key,
        country_hint=hint, posted_at=iso_date(raw.get("PostedDate")), verified_at=stamp,
        verification="Listed in the employer's live Oracle Recruiting career site", needs_detail=True, detail_ref=jid,
    )


async def fetch(ctx: Ctx, key: str, scope: str = "") -> list[Job]:
    """Search one employer's Oracle career site for `scope` (a job title; empty = newest postings). List-only records."""
    host, site = split_key(key)
    pages = max(1, int(ctx.options.get("oracle_pages", MAX_PAGES)))
    stamp, jobs, seen = now_iso(), [], set()
    company = ""
    for page in range(pages):
        data = await _get(ctx, LIST.format(host=host, site=site, limit=PAGE_SIZE, offset=page * PAGE_SIZE, keyword=_keyword(scope)))
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise SourceError("Unexpected Oracle Recruiting payload")
        search = items[0] if items and isinstance(items[0], dict) else {}
        rows = search.get("requisitionList") if isinstance(search.get("requisitionList"), list) else []
        if rows and not company:
            company = await company_name(ctx, key)
        for raw in rows:
            job = _job(key, company, raw, stamp)
            if job and job.ats_id not in seen:
                seen.add(job.ats_id)
                jobs.append(job)
        try:
            total = int(search.get("TotalJobsCount") or 0)
        except (TypeError, ValueError):
            total = 0
        if not rows or (page + 1) * PAGE_SIZE >= total:
            break
    return jobs


async def detail(ctx: Ctx, job: Job) -> Job | None:
    """Description of one posting. None when the site no longer lists it."""
    try:
        host, site = split_key(job.board)
        if not JOB_ID.fullmatch(job.detail_ref or ""):
            return None
        data = await _get(ctx, DETAIL.format(host=host, site=site, id=job.detail_ref))
    except BoardNotFound:
        return None
    items = data.get("items") if isinstance(data, dict) else None
    row = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None
    if not row or not str(row.get("Title") or "").strip():
        return None
    parts = [str(row.get(k) or "") for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                                             "ExternalQualificationsStr", "CorporateDescriptionStr")]
    job.raw_html = "\n".join(p for p in parts if p.strip()) or str(row.get("ShortDescriptionStr") or "")
    job.posted_at = iso_date(row.get("ExternalPostedStartDate")) or job.posted_at
    job.needs_detail = False
    job.verified_at = now_iso()
    return job
