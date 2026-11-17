"""Workday career sites.

Workday has no documented cross-company API. Each employer's public career site is backed by JSON
endpoints (`/wday/cxs/{tenant}/{site}/jobs` and `.../job/...`) that need no login. They are used here
only when that tenant's robots.txt allows the path, at low concurrency, and always per employer.
A board key is "tenant|wdN|site", for example "nvidia|wd5|NVIDIAExternalCareerSite"."""
from datetime import date

from ..job_normalizer import detect_work_mode, parse_ats, pretty_slug
from ..schemas import Job
from .base import BoardNotFound, Ctx, SourceError, iso_date, now_iso

PAGE_SIZE = 20
_US_NAMES = {"united states", "united states of america", "usa", "us"}
_us_facet: dict[str, dict | None] = {}        # board key -> appliedFacets that restrict results to the US (None = unknown)
DISPLAY_NAMES: dict[str, str] = {}            # filled from the seed list: board key -> employer name


def split_key(key: str) -> tuple[str, str, str]:
    parts = key.split("|")
    if len(parts) != 3 or not all(parts):
        raise SourceError("Malformed Workday board key")
    return parts[0], parts[1], parts[2]


def _group(key: str) -> str:
    """All Workday tenants share one rate-limit group: Workday throttles per client, not per employer."""
    return "workday"


def _host(key: str) -> str:
    tenant, wd, _ = split_key(key)
    return f"https://{tenant}.{wd}.myworkdayjobs.com"


def search_url(key: str) -> str:
    tenant, _, site = split_key(key)
    return f"{_host(key)}/wday/cxs/{tenant}/{site}/jobs"


def detail_url(key: str, external_path: str) -> str:
    tenant, _, site = split_key(key)
    return f"{_host(key)}/wday/cxs/{tenant}/{site}{external_path}"


def public_url(key: str, external_path: str) -> str:
    return f"{_host(key)}/{split_key(key)[2]}{external_path}"


def company_name(key: str) -> str:
    return DISPLAY_NAMES.get(key) or pretty_slug(split_key(key)[0])


def find_us_facet(facets: list) -> dict | None:
    """Locate the facet value meaning 'United States' anywhere in the (nested) facet tree."""
    stack = [(f, f.get("facetParameter")) for f in facets or [] if isinstance(f, dict)]
    while stack:
        node, param = stack.pop()
        for value in node.get("values") or []:
            if not isinstance(value, dict):
                continue
            child_param = value.get("facetParameter") or param
            if value.get("id") and str(value.get("descriptor", "")).strip().lower() in _US_NAMES and param:
                return {param: [value["id"]]}
            if value.get("values"):
                stack.append((value, child_param))
    return None


def _list_job(key: str, raw: dict, us_only: bool, stamp: str) -> Job | None:
    title = str(raw.get("title") or "").strip()
    path = str(raw.get("externalPath") or "")
    if not title or not path.startswith("/job/"):
        return None
    url = public_url(key, path)
    parsed = parse_ats(url)
    bullets = [str(b) for b in raw.get("bulletFields") or [] if b]
    tenant = split_key(key)[0]
    ats_id = (parsed[2] if parsed and parsed[2] else "") or (f"{tenant}:{bullets[0].upper()}" if bullets else "")
    if not ats_id:
        return None
    location = str(raw.get("locationsText") or "")
    return Job(
        title=title, company=company_name(key), location=location or "Not listed",
        work_mode=detect_work_mode(str(raw.get("remoteType") or ""), location),
        url=url, apply_url="", source="Workday", sources=["Workday"], ats="workday", ats_id=ats_id, board=key,
        country_hint="United States" if us_only else "", verified_at=stamp,
        verification="Listed in the employer's live Workday career site",
        needs_detail=True, detail_ref=path,
    )


async def fetch(ctx: Ctx, key: str, scope: str, pages: int = 1) -> list[Job]:
    """Search one employer's Workday site for `scope` (free text). Returns list-only records (needs_detail)."""
    url = search_url(key)
    body = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": 0, "searchText": scope}
    facet = _us_facet.get(key)
    if facet is None and key not in _us_facet:
        first = await ctx.json(_group(key), "POST", url, json_body=body, check_robots=True, retries=0)
        facet = _us_facet[key] = find_us_facet(first.get("facets") or [])
        if not facet:
            return _collect(key, [first], us_only=False)
    body["appliedFacets"] = facet or {}
    responses = []
    for page in range(max(1, pages)):
        body["offset"] = page * PAGE_SIZE
        data = await ctx.json(_group(key), "POST", url, json_body=body, check_robots=True, retries=0)
        responses.append(data)
        if (page + 1) * PAGE_SIZE >= int(data.get("total") or 0):
            break
    return _collect(key, responses, us_only=bool(facet))


def _collect(key: str, responses: list[dict], us_only: bool) -> list[Job]:
    stamp = now_iso()
    jobs, seen = [], set()
    for data in responses:
        for raw in data.get("jobPostings") or []:
            job = _list_job(key, raw, us_only, stamp)
            if job and job.ats_id not in seen:
                seen.add(job.ats_id)
                jobs.append(job)
    return jobs


def _apply_detail(job: Job, info: dict) -> Job | None:
    if info.get("posted") is False or info.get("canApply") is False:
        return None
    end = iso_date(info.get("endDate"))
    if end and end < date.today().isoformat():
        return None
    locations = [str(info.get("location") or "")] + [str(x) for x in info.get("additionalLocations") or []]
    location = "; ".join(dict.fromkeys(l for l in locations if l))
    country = info.get("country") or {}
    job.title = str(info.get("title") or job.title).strip()
    job.location = location or job.location
    job.country_hint = str(country.get("descriptor") or job.country_hint) if isinstance(country, dict) else job.country_hint
    job.raw_html = str(info.get("jobDescription") or "")
    job.posted_at = iso_date(info.get("startDate"))
    job.work_mode = detect_work_mode(str(info.get("remoteType") or ""), location) if (info.get("remoteType") or location) else job.work_mode
    if info.get("externalUrl"):
        job.url = str(info["externalUrl"])
    job.needs_detail = False
    job.verified_at = now_iso()
    return job


async def detail(ctx: Ctx, job: Job) -> Job | None:
    """Fill in the description for a list-only record. None means the posting is no longer open."""
    try:
        data = await ctx.json(_group(job.board), "GET", detail_url(job.board, job.detail_ref), check_robots=True, retries=0)
    except BoardNotFound:
        return None
    return _apply_detail(job, data.get("jobPostingInfo") or {})


async def lead(ctx: Ctx, key: str, url: str) -> Job | None:
    """Verify one posting URL found elsewhere (for example by AI web search) against the live site."""
    marker = url.find("/job/")
    if marker == -1:
        return None
    path = url[marker:].split("?")[0].split("#")[0]
    stub = _list_job(key, {"title": "?", "externalPath": path}, us_only=False, stamp=now_iso())
    return await detail(ctx, stub) if stub else None
