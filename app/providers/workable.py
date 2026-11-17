"""Workable. Two public JSON surfaces, both allowed by robots.txt:
- jobs.workable.com/api/v1/jobs  : cross-company search of Workable-hosted postings (key "*").
- apply.workable.com/api/v1/widget/accounts/{account} : one employer's postings (key = account)."""
from ..job_normalizer import pretty_slug
from ..schemas import Job
from .base import Ctx, now_iso

GROUP = "workable"
SEARCH_API = "https://jobs.workable.com/api/v1/jobs"
WIDGET_API = "https://apply.workable.com/api/v1/widget/accounts/{account}"
_MODES = {"remote": "Remote", "hybrid": "Hybrid", "on_site": "On-site", "onsite": "On-site"}


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
        url=url, apply_url="", source="Workable", sources=["Workable"], ats="workable", ats_id=str(raw.get("id") or ""),
        board="*", country_hint=str(loc.get("countryName") or "") if isinstance(loc, dict) else "",
        posted_at=str(raw.get("created") or "")[:10], verified_at=stamp,
        verification="Listed in Workable's live job search API",
    )


def _widget_job(account: str, company: str, raw: dict, stamp: str) -> Job | None:
    title = str(raw.get("title") or "").strip()
    url = str(raw.get("url") or raw.get("shortlink") or "")
    code = str(raw.get("shortcode") or "").upper()
    if not title or not url.startswith("https://") or not code:
        return None
    location = ", ".join(str(raw[k]) for k in ("city", "state", "country") if raw.get(k))
    return Job(
        title=title, company=company or pretty_slug(account), location=location or "Not listed",
        work_mode="Remote" if raw.get("telecommuting") else "Not listed", raw_html=str(raw.get("description") or ""),
        url=url, apply_url=str(raw.get("application_url") or ""), source="Workable", sources=["Workable"],
        ats="workable", ats_id=code, board=account, country_hint=str(raw.get("country") or ""),
        posted_at=str(raw.get("published_on") or raw.get("created_at") or "")[:10], verified_at=stamp,
        verification="Listed in the employer's live Workable careers API",
    )


async def fetch(ctx: Ctx, key: str, scope: str, pages: int = 5) -> list[Job]:
    stamp = now_iso()
    if key != "*":
        data = await ctx.json(GROUP, "GET", WIDGET_API.format(account=key), params={"details": "true"})
        jobs = [_widget_job(key, str(data.get("name") or ""), raw, stamp) for raw in data.get("jobs") or []]
        return [j for j in jobs if j]
    jobs, seen, token = [], set(), None
    for _ in range(max(1, pages)):
        params = {"query": scope, "location": "United States"}
        if token:
            params["pageToken"] = token
        data = await ctx.json(GROUP, "GET", SEARCH_API, params=params)
        for raw in data.get("jobs") or []:
            job = _search_job(raw, stamp)
            if job and job.ats_id and job.ats_id not in seen:
                seen.add(job.ats_id)
                jobs.append(job)
        token = data.get("nextPageToken")
        if not token or not data.get("jobs"):
            break
    return jobs
