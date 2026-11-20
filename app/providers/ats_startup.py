"""Employer-board readers for three startup-focused applicant-tracking systems: Recruitee, Pinpoint and Rippling.
Each reads ONE employer board through the anonymous endpoint the vendor publishes for career sites.

Findings of 2026-11-21 (endpoints, robots.txt and terms were read on that day; docs/board-sources.md has the table):

- Recruitee: `https://<company>.recruitee.com/api/offers/` is the documented Careers Site API
  (https://docs.recruitee.com/reference/offers, no authentication, "Returns a collection of published company jobs").
  One request returns the whole board with full descriptions; there is no pagination. Tenant robots.txt disallows only
  `/v/`. The Tellent Recruitee terms have no clause on automated access. ON by default.
- Pinpoint: `https://<company>.pinpointhq.com/postings.json` is the documented job postings JSON endpoint
  (https://developers.pinpointhq.com/docs/jobs-json-endpoint). One request, full descriptions, no pagination, and no
  posting date and no country in the answer (so none is stored). robots.txt differs per tenant: Pinpoint's own board
  (workwithus.pinpointhq.com) disallows only `/mydata`, `/admin` and `/companies`, but the two employer tenants read
  in the live test (ada, afm) both answer `User-Agent: * / Disallow: /`, as a tenant did on 2026-11-18. Because the
  employer tenants forbid automated reading, the reader is switched OFF by default. It reads robots.txt of every
  tenant on every read, so even when switched on (PINPOINT_ENABLED=1, and add pinpointhq.com to SEARXNG_SITES) it
  refuses each tenant whose robots.txt disallows the feed. Pinpoint's acceptable-use policy has no clause on reading
  public career pages.
- Rippling: the list is `https://ats.rippling.com/api/v2/board/<slug>/jobs?page=&pageSize=`, the endpoint Rippling's own
  hosted career pages call (paginated; one row per posting and location, merged here by posting id). The description
  and creation date come from `https://api.rippling.com/platform/api/ats/v1/board/<slug>/jobs/<uuid>` (one request
  per posting, made only for postings a search wants to show). Neither endpoint is in Rippling's public documentation.
  ats.rippling.com/robots.txt disallows only `/internal/`; api.rippling.com has no robots.txt (HTTP 404). Rippling's
  terms could NOT be read (the legal pages are rendered by script and returned no text), so "the terms allow it" is
  not established. ON by default because nothing read forbids it; set RIPPLING_ENABLED=0 to switch it off.

Switches: RECRUITEE_ENABLED (on) / PINPOINT_ENABLED (off) / RIPPLING_ENABLED (on) in app/config.py, or DISABLED_SOURCES.

Board keys: Recruitee and Pinpoint = the career-site subdomain ("namecheap"); Rippling = the board slug in
`ats.rippling.com/<slug>/jobs`. Keys are placed into host names and paths, so only plain slug characters are accepted.
Every request goes through netguard (public address only), robots.txt and the shared per-host limiter at one request
per second per platform; 429/503 back off as the other paced sources do (providers/base.py). A board that answers 404
raises BoardNotFound; a posting the board no longer lists disappears with the next read of the board."""
import re

from .. import netguard
from ..job_normalizer import ATS_VENDOR_SUBDOMAINS, detect_work_mode, pretty_slug
from ..schemas import Job
from .base import BoardNotFound, Ctx, SourceError, iso_date, now_iso

HOST_INTERVAL_SECONDS = {"recruitee": 1.0, "pinpoint": 1.0, "rippling": 1.0}
HOST_LIMITS = {"recruitee": 2, "pinpoint": 2, "rippling": 2}
RIPPLING_PAGE_SIZE = 100
RIPPLING_MAX_PAGES = 20            # at most 2,000 posting/location rows of one board per read

_US = {"us", "usa", "united states", "united states of america"}
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SUBDOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")          # one DNS label
RIPPLING_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,98}[a-z0-9])?")
OFFER_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,118}[a-z0-9])?")
RESERVED = ATS_VENDOR_SUBDOMAINS      # subdomains that belong to the vendor, not to an employer
SLUGS = {"recruitee": SUBDOMAIN, "pinpoint": SUBDOMAIN, "rippling": RIPPLING_SLUG}

RECRUITEE_LIST = "https://{co}.recruitee.com/api/offers/"
PINPOINT_LIST = "https://{co}.pinpointhq.com/postings.json"
RIPPLING_LIST = "https://ats.rippling.com/api/v2/board/{slug}/jobs"
RIPPLING_DETAIL = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{uuid}"
RIPPLING_PUBLIC = "https://ats.rippling.com/{slug}/jobs/{uuid}"

_MODES = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site", "on_site": "On-site", "on-site": "On-site"}


def valid_key(src: str, key: str) -> bool:
    """Whether `key` is a well-formed employer key of one of the three platforms."""
    rx = SLUGS.get(src)
    return bool(rx and isinstance(key, str) and rx.fullmatch(key) and key not in RESERVED and not key.startswith("xn--"))


def _check(src: str, key: str) -> str:
    if not valid_key(src, key):
        raise BoardNotFound(f"Malformed {src.capitalize()} board id")
    return key


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


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _https(value) -> str:
    url = _text(value)
    return url if url.startswith("https://") and len(url) <= 600 and not re.search(r"[\s\\]", url) else ""


def _country_hint(countries: list[str]) -> str:
    """'United States' when any location is in the US, else the first country named, else ''."""
    known = [c for c in (_text(c) for c in countries) if c]
    if any(c.lower() in _US for c in known):
        return "United States"
    return known[0] if known else ""


def _join(*parts) -> str:
    return ", ".join(dict.fromkeys(p for p in (_text(p) for p in parts) if p))


def _section(header, body) -> str:
    body = _text(body)
    if not body:
        return ""
    return (f"<h3>{_text(header)}</h3>" if _text(header) else "") + body


# ---------------------------------------------------------------- Recruitee
def recruitee_job(co: str, raw: dict, stamp: str) -> Job | None:
    if not isinstance(raw, dict) or str(raw.get("status") or "published").lower() != "published":
        return None
    title, slug = _text(raw.get("title")), _text(raw.get("slug")).lower()
    url = _https(raw.get("careers_url"))
    if not title or not url or not OFFER_SLUG.fullmatch(slug):
        return None
    places = [l for l in raw.get("locations") or [] if isinstance(l, dict)] if isinstance(raw.get("locations"), list) else []
    if places:
        names = [_join(l.get("city"), l.get("state"), l.get("country")) for l in places]
        countries = [("United States" if _text(l.get("country_code")).upper() == "US" else _text(l.get("country"))) for l in places]
    else:
        names = [_join(raw.get("city"), raw.get("state_name"), raw.get("country"))]
        countries = ["United States" if _text(raw.get("country_code")).upper() == "US" else _text(raw.get("country"))]
    location = "; ".join(dict.fromkeys(n for n in names if n)) or _text(raw.get("location"))
    mode = ("Remote" if raw.get("remote") is True else "Hybrid" if raw.get("hybrid") is True
            else "On-site" if raw.get("on_site") is True else detect_work_mode(_text(raw.get("location")), location))
    salary = "Not listed"
    pay = raw.get("salary") if isinstance(raw.get("salary"), dict) else {}
    try:
        low, high = float(pay.get("min") or 0), float(pay.get("max") or 0)
    except (TypeError, ValueError):
        low = high = 0.0
    if low > 0 and high > 0:
        salary = f"{_text(pay.get('currency')) or 'USD'} {int(low):,} - {int(high):,}" + (f" / {_text(pay.get('period'))}" if _text(pay.get("period")) else "")
    return Job(title=title, company=_text(raw.get("company_name")) or pretty_slug(co), location=location or "Not listed",
               work_mode=mode, salary=salary,
               raw_html=_text(raw.get("description")) + _section("Requirements", raw.get("requirements")),
               url=url, apply_url=_https(raw.get("careers_apply_url")), source="Recruitee", sources=["Recruitee"],
               ats="recruitee", ats_id=f"{co}:{slug}", board=co, country_hint=_country_hint(countries),
               posted_at=iso_date(_text(raw.get("published_at"))), verified_at=stamp,
               verification="Listed in the employer's live Recruitee careers site API")


async def fetch_recruitee(ctx: Ctx, key: str, scope: str = "") -> list[Job]:
    co = _check("recruitee", key)
    data = await _get(ctx, "recruitee", RECRUITEE_LIST.format(co=co))
    if not isinstance(data, dict) or not isinstance(data.get("offers"), list):
        raise SourceError("Unexpected Recruitee payload")
    stamp = now_iso()
    jobs = [recruitee_job(co, raw, stamp) for raw in data["offers"]]
    return list({j.ats_id: j for j in jobs if j}.values())


# ---------------------------------------------------------------- Pinpoint
def pinpoint_job(co: str, raw: dict, stamp: str) -> Job | None:
    if not isinstance(raw, dict):
        return None
    title, url = _text(raw.get("title")), _https(raw.get("url"))
    if not title or not url:
        return None
    m = re.search(r"/postings/(" + _UUID.pattern + r")(?:[/?#]|$)", url.lower())
    numeric = str(raw.get("id") or "").strip()
    if not m and not numeric.isdigit():
        return None
    loc = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    place, label = _join(loc.get("city"), loc.get("province")), _text(loc.get("name"))
    location = f"{place} ({label})" if place and label and label.lower() not in place.lower() else (place or label)
    mode = _MODES.get(_text(raw.get("workplace_type")).lower()) or detect_work_mode(_text(raw.get("workplace_type_text")), location)
    salary = _text(raw.get("compensation")) if raw.get("compensation_visible") is True else ""
    html = "".join((_text(raw.get("description")),
                    _section(raw.get("key_responsibilities_header"), raw.get("key_responsibilities")),
                    _section(raw.get("skills_knowledge_expertise_header"), raw.get("skills_knowledge_expertise")),
                    _section(raw.get("benefits_header"), raw.get("benefits"))))
    return Job(title=title, company=pretty_slug(co), location=location or "Not listed", work_mode=mode,
               salary=salary[:120] or "Not listed", raw_html=html, url=url, apply_url=url,
               source="Pinpoint", sources=["Pinpoint"], ats="pinpoint", ats_id=m.group(1) if m else f"{co}:{numeric}",
               board=co, country_hint="", posted_at="", verified_at=stamp,       # the feed carries no country and no posting date
               verification="Listed in the employer's live Pinpoint job postings feed")


async def fetch_pinpoint(ctx: Ctx, key: str, scope: str = "") -> list[Job]:
    co = _check("pinpoint", key)
    data = await _get(ctx, "pinpoint", PINPOINT_LIST.format(co=co))
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise SourceError("Unexpected Pinpoint payload")
    stamp = now_iso()
    jobs = [pinpoint_job(co, raw, stamp) for raw in data["data"]]
    return list({j.ats_id: j for j in jobs if j}.values())


# ---------------------------------------------------------------- Rippling
def rippling_jobs(slug: str, rows: list, stamp: str) -> list[Job]:
    """Rows of the list endpoint (one per posting and location) merged into one Job per posting."""
    merged: dict[str, dict] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        uuid, title = _text(raw.get("id")).lower(), _text(raw.get("name"))
        if not _UUID.fullmatch(uuid) or not title:
            continue
        entry = merged.setdefault(uuid, {"title": title, "places": []})
        for loc in raw.get("locations") if isinstance(raw.get("locations"), list) else []:
            if isinstance(loc, dict) and loc not in entry["places"]:
                entry["places"].append(loc)
    jobs = []
    for uuid, entry in merged.items():
        places = entry["places"]
        names = [_text(l.get("name")) or _join(l.get("city"), l.get("state"), l.get("country")) for l in places]
        countries = ["United States" if _text(l.get("countryCode")).upper() == "US" else _text(l.get("country")) for l in places]
        location = "; ".join(dict.fromkeys(n for n in names if n))
        kinds = {_text(l.get("workplaceType")).lower() for l in places} - {""}
        mode = _MODES.get(next(iter(kinds))) if len(kinds) == 1 else None
        url = RIPPLING_PUBLIC.format(slug=slug, uuid=uuid)
        jobs.append(Job(title=entry["title"], company=pretty_slug(slug), location=location or "Not listed",
                        work_mode=mode or detect_work_mode(location), url=url, apply_url=url,
                        source="Rippling", sources=["Rippling"], ats="rippling", ats_id=uuid, board=slug,
                        country_hint=_country_hint(countries), posted_at="", verified_at=stamp,
                        verification="Listed in the employer's live Rippling job board",
                        needs_detail=True, detail_ref=uuid))
    return jobs


async def fetch_rippling(ctx: Ctx, key: str, scope: str = "") -> list[Job]:
    slug = _check("rippling", key)
    rows: list = []
    pages = max(1, int(ctx.options.get("rippling_pages", RIPPLING_MAX_PAGES)))
    for page in range(pages):
        data = await _get(ctx, "rippling", RIPPLING_LIST.format(slug=slug), {"page": page, "pageSize": RIPPLING_PAGE_SIZE})
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise SourceError("Unexpected Rippling payload")
        rows.extend(data["items"])
        try:
            total_pages = int(data.get("totalPages") or 0)
        except (TypeError, ValueError):
            total_pages = 0
        if not data["items"] or page + 1 >= total_pages:
            break
    return rippling_jobs(slug, rows, now_iso())


async def detail_rippling(ctx: Ctx, job: Job) -> Job | None:
    """Description and creation date of one posting. None = the board no longer has it."""
    uuid = (job.detail_ref or "").lower()
    if not _UUID.fullmatch(uuid):
        return None
    try:
        data = await _get(ctx, "rippling", RIPPLING_DETAIL.format(slug=_check("rippling", job.board), uuid=uuid))
    except BoardNotFound:
        return None
    if not isinstance(data, dict) or not _text(data.get("name")):
        raise SourceError("Unexpected Rippling posting payload")
    text = data.get("description") if isinstance(data.get("description"), dict) else {}
    job.raw_html = _text(text.get("role")) + _text(text.get("company"))
    if _text(data.get("companyName")):
        job.company = _text(data.get("companyName"))
    job.posted_at = iso_date(_text(data.get("createdOn"))) or job.posted_at
    job.needs_detail = False
    job.verified_at = now_iso()
    return job
