"""Workable. Two anonymous JSON surfaces; findings of 2026-11-21 (docs/board-sources.md has the table):

- ONE EMPLOYER ACCOUNT (key = the account subdomain, e.g. "huggingface"): the officially documented public endpoint is
  `GET https://www.workable.com/api/accounts/{subdomain}?details=true` ("Returns a collection of the public jobs for an
  account", https://workable.readme.io/reference/jobs-1 and the help article "Using the Workable API to create a
  careers page"; no authentication). Measured: that address answers `302` to
  `https://apply.workable.com/api/v1/widget/accounts/{subdomain}?details=true`, so the reader asks the redirect target
  directly (one request per account instead of two). One request returns every published job of the account with its
  description; the documented parameters are `subdomain` and `details`, there is no paging. robots.txt of
  apply.workable.com disallows nothing and of www.workable.com only `/user_password_resets`, `/admin`,
  `/auth/google`, `/j/`. In Workable's terms (last updated 2026-03-02; read through a page fetcher, not as raw text)
  no clause on automated access was found. This is the reader the board catalogue uses (app/board_catalog.py).
- CROSS-COMPANY SEARCH (key "*"): `https://jobs.workable.com/api/v1/jobs?query=&location=`, the endpoint Workable's own
  job-board page calls. It is NOT in Workable's documentation. robots.txt of jobs.workable.com disallows the search
  pages (`/search`, `/search*?*`) but not `/api/`; in the job-board terms (2023-09-21; read through a page fetcher) no
  clause on automated access was found, and clause 15.2 licenses the site "only for the purpose of looking for a job".
  Kept because nothing that was read forbids it; `WORKABLE_SEARCH_ENABLED=0` switches it off without touching the
  account reader (the documented one is the preferred source: it carries the employer's own apply address).
  A posting found this way carries NO posting id (`ats_id` is empty): the job board numbers its listings with ids of
  its own, which are not the employer's posting codes (apply.workable.com/j/{CODE}) and appear nowhere in the
  account's answer. Stored as ids of the same ATS they would make the same vacancy, read both ways, look like two
  different requisitions that must never be merged. Without one, the listing is identified by its address and is
  merged with the account's record by employer, title and place; the account's record wins (it has the apply link).

Every account request passes netguard (public address only), robots.txt and the shared limiter at one request per
second for all accounts together (they live on one host); HTTP 429 pauses the group for ten minutes
(providers/base.py). An account that answers 404 raises BoardNotFound. Account ids are placed into a path, so only
plain slug characters are accepted."""
import re

from .. import netguard
from ..job_normalizer import pretty_slug
from ..schemas import Job
from .base import BoardNotFound, Ctx, SourceError, iso_date, now_iso

GROUP = "workable"                           # limiter group of the cross-company search (jobs.workable.com)
ACCOUNTS_GROUP = "workable-accounts"         # limiter group of the account reader (apply.workable.com)
ACCOUNT_INTERVAL_SECONDS = 1.0               # all accounts are on one host: at most one request per second
ACCOUNT_LIMIT = 2
SEARCH_API = "https://jobs.workable.com/api/v1/jobs"
DOCUMENTED_API = "https://www.workable.com/api/accounts/{account}"                      # answers 302 to ACCOUNT_API
ACCOUNT_API = "https://apply.workable.com/api/v1/widget/accounts/{account}"
WIDGET_API = ACCOUNT_API                     # earlier name
ACCOUNT = re.compile(r"[a-z0-9](?:[a-z0-9_.-]{0,98}[a-z0-9])?")
_RESERVED = {"api", "j"}                     # paths of apply.workable.com itself, never an employer
_SHORTCODE = re.compile(r"[A-Z0-9]{4,32}")
_MODES = {"remote": "Remote", "hybrid": "Hybrid", "on_site": "On-site", "onsite": "On-site", "on-site": "On-site"}
_US = {"us", "usa", "united states", "united states of america"}


def valid_account(key) -> bool:
    """Whether `key` is a well-formed Workable account id (the subdomain in apply.workable.com/{account})."""
    return bool(isinstance(key, str) and ACCOUNT.fullmatch(key) and ".." not in key and key not in _RESERVED)


async def _guard(url: str) -> None:
    """Every account fetch passes the SSRF guard first (tests replace this; it resolves DNS)."""
    await netguard.assert_public_url(url)


def _pace(ctx: Ctx) -> None:
    """Install the account reader's polite pace on the shared limiter (only if the engine did not set one)."""
    ctx.limiter.intervals.setdefault(ACCOUNTS_GROUP, ACCOUNT_INTERVAL_SECONDS)
    ctx.limiter.limits.setdefault(ACCOUNTS_GROUP, ACCOUNT_LIMIT)


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _https(value) -> str:
    url = _text(value)
    return url if url.startswith("https://") and len(url) <= 600 and not re.search(r"[\s\\]", url) else ""


def _join(*parts) -> str:
    """'New York, New York, United States': city, region and country as the source gives them (a city may be named like its state)."""
    return ", ".join(p for p in (_text(p) for p in parts) if p)


# ---------------------------------------------------------------- cross-company search (undocumented)
def _search_job(raw: dict, stamp: str) -> Job | None:
    title = str(raw.get("title") or "").strip()
    url = str(raw.get("url") or "")
    company = str((raw.get("company") or {}).get("title") or "").strip()
    if not title or not url.startswith("https://") or not company or raw.get("state") not in (None, "published"):
        return None
    loc = raw.get("location") or {}
    locations = [str(l) for l in raw.get("locations") or [] if l and str(l).upper() != "TELECOMMUTE"]
    if not locations and isinstance(loc, dict):
        locations = [", ".join(str(loc[k]) for k in ("city", "subregion", "countryName") if loc.get(k))]
    html = " ".join(str(raw.get(k) or "") for k in ("description", "requirementsSection", "benefitsSection"))
    return Job(
        title=title, company=company, location="; ".join(l for l in locations if l) or "Not listed",
        work_mode=_MODES.get(str(raw.get("workplace") or "").lower(), "Not listed"), raw_html=html,
        url=url, apply_url="", source="Workable", sources=["Workable"], ats="workable", ats_id="",      # see the module docstring
        board="*", country_hint=str(loc.get("countryName") or "") if isinstance(loc, dict) else "",
        posted_at=str(raw.get("created") or "")[:10], verified_at=stamp,
        verification="Listed in Workable's live job search API",
    )


async def fetch_search(ctx: Ctx, scope: str, pages: int = 5) -> list[Job]:
    stamp = now_iso()
    jobs, seen, token = [], set(), None
    for _ in range(max(1, pages)):
        params = {"query": scope, "location": "United States"}
        if token:
            params["pageToken"] = token
        data = await ctx.json(GROUP, "GET", SEARCH_API, params=params, check_robots=True)
        for raw in data.get("jobs") or []:
            job = _search_job(raw, stamp)
            listing = str(raw.get("id") or "")              # the job board's own id: only tells two listings of one answer apart
            if job and listing and listing not in seen:
                seen.add(listing)
                jobs.append(job)
        token = data.get("nextPageToken")
        if not token or not data.get("jobs"):
            break
    return jobs


# ---------------------------------------------------------------- one employer account (documented)
def account_job(account: str, company: str, raw: dict, stamp: str) -> Job | None:
    """One published job of an account's answer as a Job, or None when the row is not usable."""
    if not isinstance(raw, dict):
        return None
    title, code = _text(raw.get("title")), _text(raw.get("shortcode")).upper()
    url = _https(raw.get("url")) or _https(raw.get("shortlink"))
    if not title or not url or not _SHORTCODE.fullmatch(code):
        return None
    places = [l for l in raw.get("locations") or [] if isinstance(l, dict)] if isinstance(raw.get("locations"), list) else []
    if places:
        # A location marked hidden shows the country only, as the employer's own page does.
        names = [_text(l.get("country")) if l.get("hidden") is True else _join(l.get("city"), l.get("region"), l.get("country"))
                 for l in places]
        countries = ["United States" if _text(l.get("countryCode")).upper() == "US" else _text(l.get("country")) for l in places]
    else:
        names = [_join(raw.get("city"), raw.get("state"), raw.get("country"))]
        countries = [_text(raw.get("country"))]
    countries = [c for c in countries if c]
    hint = "United States" if any(c.lower() in _US for c in countries) else (countries[0] if countries else "")
    mode = _MODES.get(_text(raw.get("workplace_type")).lower()) or ("Remote" if raw.get("telecommuting") is True else "Not listed")
    return Job(
        title=title, company=_text(company) or pretty_slug(account),
        location="; ".join(dict.fromkeys(n for n in names if n)) or "Not listed", work_mode=mode,
        raw_html=_text(raw.get("description")), url=url, apply_url=_https(raw.get("application_url")),
        source="Workable", sources=["Workable"], ats="workable", ats_id=code, board=account, country_hint=hint,
        posted_at=iso_date(_text(raw.get("published_on"))),       # the publishing date only; `created_at` is not it
        verified_at=stamp, verification="Listed in the employer's live Workable careers API",
    )


_widget_job = account_job                    # earlier name


async def fetch_account(ctx: Ctx, key: str, scope: str = "") -> list[Job]:
    """Every published job of one employer account (one request, descriptions included)."""
    if not valid_account(key):
        raise BoardNotFound("Malformed Workable account id")
    url = ACCOUNT_API.format(account=key)
    await _guard(url)
    _pace(ctx)
    data = await ctx.json(ACCOUNTS_GROUP, "GET", url, params={"details": "true"}, check_robots=True)
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise SourceError("Unexpected Workable payload")
    stamp = now_iso()
    jobs = [account_job(key, _text(data.get("name")), raw, stamp) for raw in data["jobs"]]
    return list({j.ats_id: j for j in jobs if j}.values())


async def fetch(ctx: Ctx, key: str, scope: str, pages: int = 5) -> list[Job]:
    """Registry entry: key "*" = the cross-company search for `scope`; any other key = that employer's account."""
    if key != "*":
        return await fetch_account(ctx, key, scope)
    return await fetch_search(ctx, scope, pages)
