"""Extra employer-board readers: SmartRecruiters and BambooHR (public, unauthenticated career-site feeds).

Both are switched OFF by default (see docs/board-sources.md for the robots.txt / terms findings):
- SmartRecruiters: api.smartrecruiters.com/robots.txt allows only LinkedInBot (`User-agent: * / Disallow: /`).
  The reader checks robots.txt on every read, so even when switched on it refuses while that rule stands.
- BambooHR: robots.txt allows everything, but BambooHR's terms of service forbid using "any robot, spider, other
  automated device ... to monitor or copy any content from the Service".
Switch on with BOARD_SOURCES_SMARTRECRUITERS=1 / BOARD_SOURCES_BAMBOOHR=1 only after the owner has cleared the terms.

Board keys: SmartRecruiters company identifier (case-sensitive, e.g. "BoschGroup"); BambooHR subdomain ("10squared").
Every request goes through netguard (public address only), robots.txt and the shared per-host limiter; 429/503 back off
exactly as the other paced sources do (providers/base.py: retry-after, then a ten-minute pause of the group)."""
import os
import re

from .. import netguard
from ..job_normalizer import detect_work_mode, pretty_slug
from ..schemas import Job
from .base import BoardNotFound, Ctx, SourceError, iso_date, now_iso


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return default if not value else value in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Settings (to be moved into app/config.py by the engine engineer; names follow its env style).
SMARTRECRUITERS_ENABLED = _env_flag("BOARD_SOURCES_SMARTRECRUITERS")
BAMBOOHR_ENABLED = _env_flag("BOARD_SOURCES_BAMBOOHR")
SMARTRECRUITERS_MAX_PAGES = _env_int("SMARTRECRUITERS_MAX_PAGES", 10)          # 100 postings per page
HOST_INTERVAL_SECONDS = {"smartrecruiters": _env_float("SMARTRECRUITERS_INTERVAL_SECONDS", 1.0),
                         "bamboohr": _env_float("BAMBOOHR_INTERVAL_SECONDS", 1.0)}
HOST_LIMITS = {"smartrecruiters": 2, "bamboohr": 2}

_US = {"us", "usa", "united states", "united states of america"}
SR_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
BAMBOO_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

SR_LIST = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
SR_DETAIL = "https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting}"
SR_PUBLIC = "https://jobs.smartrecruiters.com/{company}/{posting}"
BAMBOO_LIST = "https://{co}.bamboohr.com/careers/list"
BAMBOO_DETAIL = "https://{co}.bamboohr.com/careers/{id}/detail"
BAMBOO_PUBLIC = "https://{co}.bamboohr.com/careers/{id}"


async def _guard(url: str) -> None:
    """Every fetch passes the SSRF guard first (tests replace this; it resolves DNS)."""
    await netguard.assert_public_url(url)


def _pace(ctx: Ctx, group: str) -> None:
    """Install this source's polite pace on the shared limiter (only if the engine did not set one)."""
    ctx.limiter.intervals.setdefault(group, HOST_INTERVAL_SECONDS[group])
    ctx.limiter.limits.setdefault(group, HOST_LIMITS[group])


async def _get(ctx: Ctx, group: str, url: str, params: dict | None = None):
    await _guard(url)
    _pace(ctx, group)
    return await ctx.json(group, "GET", url, params=params, check_robots=True)


def _location(city: str, state: str, country: str) -> tuple[str, str]:
    """(display location, country hint). The engine's own location logic decides US or not."""
    country = (country or "").strip()
    hint = "United States" if country.lower() in _US else (country.upper() if len(country) == 2 else country)
    parts = [p.strip() for p in (city, state, hint) if p and str(p).strip()]
    return (", ".join(dict.fromkeys(parts)) or "Not listed"), hint


# ---------------------------------------------------------------- SmartRecruiters
def check_sr_key(key: str) -> str:
    if not SR_SLUG.fullmatch(key or ""):
        raise BoardNotFound("Malformed SmartRecruiters company id")
    return key


def sr_job(company: str, raw: dict, stamp: str) -> Job | None:
    if not isinstance(raw, dict):
        return None
    pid, title = str(raw.get("id") or "").strip(), str(raw.get("name") or "").strip()
    if not pid or not title or not re.fullmatch(r"[A-Za-z0-9-]{1,40}", pid):
        return None
    loc = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    location, hint = _location(str(loc.get("city") or ""), str(loc.get("region") or ""), str(loc.get("country") or ""))
    mode = "Remote" if loc.get("remote") else "Hybrid" if loc.get("hybrid") else detect_work_mode(str(loc.get("fullLocation") or ""))
    comp = raw.get("company") if isinstance(raw.get("company"), dict) else {}
    url = SR_PUBLIC.format(company=company, posting=pid)
    return Job(title=title, company=str(comp.get("name") or pretty_slug(company)).strip(), location=location, work_mode=mode,
               url=url, apply_url="", source="SmartRecruiters", sources=["SmartRecruiters"], ats="smartrecruiters",
               ats_id=pid, board=company, country_hint=hint, posted_at=iso_date(raw.get("releasedDate")), verified_at=stamp,
               verification="Listed in the employer's live SmartRecruiters posting API", needs_detail=True, detail_ref=pid)


async def fetch_smartrecruiters(ctx: Ctx, key: str, scope: str = "") -> list[Job]:
    company = check_sr_key(key)
    stamp, jobs, seen = now_iso(), [], set()
    pages = int(ctx.options.get("smartrecruiters_pages", SMARTRECRUITERS_MAX_PAGES))
    for page in range(max(1, pages)):
        data = await _get(ctx, "smartrecruiters", SR_LIST.format(company=company), {"limit": 100, "offset": page * 100})
        if not isinstance(data, dict):
            raise SourceError("Unexpected SmartRecruiters payload")
        content = data.get("content") if isinstance(data.get("content"), list) else []
        for raw in content:
            job = sr_job(company, raw, stamp)
            if job and job.ats_id not in seen:
                seen.add(job.ats_id)
                jobs.append(job)
        try:
            total = int(data.get("totalFound") or 0)
        except (TypeError, ValueError):
            total = 0
        if not content or (page + 1) * 100 >= total:
            break
    return jobs


async def detail_smartrecruiters(ctx: Ctx, job: Job) -> Job | None:
    try:
        data = await _get(ctx, "smartrecruiters", SR_DETAIL.format(company=check_sr_key(job.board), posting=job.detail_ref))
    except BoardNotFound:
        return None
    if not isinstance(data, dict) or data.get("active") is False:
        return None
    sections = ((data.get("jobAd") or {}).get("sections") or {}) if isinstance(data.get("jobAd"), dict) else {}
    parts = [str((sections.get(k) or {}).get("text") or "") for k in
             ("companyDescription", "jobDescription", "qualifications", "additionalInformation") if isinstance(sections.get(k), dict)]
    job.raw_html = "\n".join(p for p in parts if p)
    if str(data.get("applyUrl") or "").startswith("https://"):
        job.apply_url = str(data["applyUrl"])
    job.needs_detail = False
    job.verified_at = now_iso()
    return job


# ---------------------------------------------------------------- BambooHR
def check_bamboo_key(key: str) -> str:
    if not BAMBOO_SLUG.fullmatch(key or ""):
        raise BoardNotFound("Malformed BambooHR subdomain")
    return key


def bamboo_job(co: str, raw: dict, stamp: str) -> Job | None:
    if not isinstance(raw, dict):
        return None
    jid, title = str(raw.get("id") or "").strip(), str(raw.get("jobOpeningName") or "").strip()
    if not jid.isdigit() or not title:
        return None
    loc = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    ats_loc = raw.get("atsLocation") if isinstance(raw.get("atsLocation"), dict) else {}
    city = str(loc.get("city") or ats_loc.get("city") or "")
    state = str(loc.get("state") or ats_loc.get("state") or ats_loc.get("province") or "")
    country = str(loc.get("addressCountry") or ats_loc.get("country") or "")
    location, hint = _location(city, state, country)
    mode = "Remote" if raw.get("isRemote") else detect_work_mode(str(raw.get("locationType") or "") if not str(raw.get("locationType") or "").isdigit() else "")
    url = BAMBOO_PUBLIC.format(co=co, id=jid)
    return Job(title=title, company=pretty_slug(co), location=location, work_mode=mode, url=url, apply_url=url,
               source="BambooHR", sources=["BambooHR"], ats="bamboohr", ats_id=f"{co}:{jid}", board=co, country_hint=hint,
               verified_at=stamp, verification="Listed in the employer's live BambooHR careers page",
               needs_detail=True, detail_ref=jid)


async def fetch_bamboohr(ctx: Ctx, key: str, scope: str = "") -> list[Job]:
    co = check_bamboo_key(key)
    data = await _get(ctx, "bamboohr", BAMBOO_LIST.format(co=co))
    if not isinstance(data, dict) or not isinstance(data.get("result"), list):
        raise SourceError("Unexpected BambooHR payload")
    stamp = now_iso()
    jobs = [bamboo_job(co, raw, stamp) for raw in data["result"]]
    return list({j.ats_id: j for j in jobs if j}.values())


async def detail_bamboohr(ctx: Ctx, job: Job) -> Job | None:
    try:
        data = await _get(ctx, "bamboohr", BAMBOO_DETAIL.format(co=check_bamboo_key(job.board), id=job.detail_ref))
    except BoardNotFound:
        return None
    opening = ((data.get("result") or {}).get("jobOpening") or {}) if isinstance(data, dict) else {}
    if not isinstance(opening, dict) or not opening or str(opening.get("jobOpeningStatus") or "Open").lower() != "open":
        return None
    loc = opening.get("location") if isinstance(opening.get("location"), dict) else {}
    if loc:
        job.location, job.country_hint = _location(str(loc.get("city") or ""), str(loc.get("state") or ""),
                                                   str(loc.get("addressCountry") or ""))
    job.raw_html = str(opening.get("description") or "")
    job.posted_at = iso_date(opening.get("datePosted")) or job.posted_at
    job.needs_detail = False
    job.verified_at = now_iso()
    return job
