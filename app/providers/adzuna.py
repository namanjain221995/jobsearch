"""Adzuna job search API (optional). Needs a free app id + key from https://developer.adzuna.com.
Adzuna aggregates listings from many job boards and employer sites. Limits worth knowing:
- `description` is only a snippet, so citizenship / sponsorship statements are often not visible;
- `salary_is_predicted` marks Adzuna's own estimate, which is never shown as a published salary;
- links go through Adzuna's redirect URL to the original posting.
NOT live-tested in this project: no key was available when it was written."""
from ..schemas import Job
from .base import Ctx, SourceError, iso_date, now_iso

GROUP = "adzuna"
API = "https://api.adzuna.com/v1/api/jobs/us/search/{page}"


def _job(raw: dict, stamp: str) -> Job | None:
    title = str(raw.get("title") or "").replace("<strong>", "").replace("</strong>", "").strip()
    url = str(raw.get("redirect_url") or "")
    company = str((raw.get("company") or {}).get("display_name") or "").strip()
    if not title or not url.startswith("https://") or not company or not raw.get("id"):
        return None
    salary = "Not listed"
    predicted = str(raw.get("salary_is_predicted") or "0") == "1"
    try:
        lo, hi = int(float(raw.get("salary_min") or 0)), int(float(raw.get("salary_max") or 0))
        if not predicted and lo > 0 and hi > 0:
            salary = f"USD {lo:,} - {hi:,}"
    except (TypeError, ValueError):
        pass
    return Job(
        title=title, company=company, location=str((raw.get("location") or {}).get("display_name") or "Not listed"),
        salary=salary, raw_html=str(raw.get("description") or ""), url=url, apply_url="",
        source="Adzuna", sources=["Adzuna"], ats="adzuna", ats_id=str(raw["id"]), board="us",
        country_hint="United States", posted_at=iso_date(raw.get("created")), verified_at=stamp, aggregator=True,
        verification="Listed in the Adzuna job search API (aggregated listing; description is a snippet)",
    )


async def fetch(ctx: Ctx, key: str, scope: str, pages: int = 2) -> list[Job]:
    app_id, app_key = ctx.options.get("adzuna_app_id"), ctx.options.get("adzuna_app_key")
    if not app_id or not app_key:
        raise SourceError("Adzuna keys are not configured")
    stamp = now_iso()
    jobs, seen = [], set()
    for page in range(1, max(1, pages) + 1):
        data = await ctx.json(GROUP, "GET", API.format(page=page), params={
            "app_id": app_id, "app_key": app_key, "results_per_page": 50, "what": scope,
            "content-type": "application/json"})
        rows = data.get("results") or []
        for raw in rows:
            job = _job(raw, stamp)
            if job and job.ats_id not in seen:
                seen.add(job.ats_id)
                jobs.append(job)
        if len(rows) < 50:
            break
    return jobs
