"""Amazon Jobs public search JSON (amazon.jobs/en/search.json). robots.txt only restricts /internal paths."""
from ..schemas import Job
from .base import Ctx, iso_date, now_iso

GROUP = "amazon"
API = "https://www.amazon.jobs/en/search.json"
PAGE_SIZE = 100


def _job(raw: dict, stamp: str) -> Job | None:
    title = str(raw.get("title") or "").strip()
    path = str(raw.get("job_path") or "")
    job_id = str(raw.get("id_icims") or raw.get("id") or "")
    if not title or not path.startswith("/") or not job_id:
        return None
    html = "<br/>".join(str(raw.get(k) or "") for k in ("description", "basic_qualifications", "preferred_qualifications"))
    apply_url = str(raw.get("url_next_step") or "")
    return Job(
        title=title, company="Amazon",
        location=str(raw.get("normalized_location") or raw.get("location") or "Not listed"),
        raw_html=html, url=f"https://www.amazon.jobs{path}",
        apply_url=apply_url if apply_url.startswith("https://") else "",
        source="Amazon Jobs", sources=["Amazon Jobs"], ats="amazon", ats_id=job_id, board="*",
        country_hint="United States" if str(raw.get("country_code") or "").upper() in ("USA", "US") else str(raw.get("country_code") or ""),
        posted_at=iso_date(raw.get("posted_date")), verified_at=stamp,
        verification="Listed in the live Amazon Jobs search API",
    )


async def fetch(ctx: Ctx, key: str, scope: str, pages: int = 2) -> list[Job]:
    stamp = now_iso()
    jobs, seen = [], set()
    for page in range(max(1, pages)):
        data = await ctx.json(GROUP, "GET", API, params={
            "base_query": scope, "normalized_country_code[]": "USA", "result_limit": PAGE_SIZE,
            "offset": page * PAGE_SIZE, "sort": "relevant"})
        rows = data.get("jobs") or []
        for raw in rows:
            job = _job(raw, stamp)
            if job and job.ats_id not in seen:
                seen.add(job.ats_id)
                jobs.append(job)
        if len(rows) < PAGE_SIZE or (page + 1) * PAGE_SIZE >= int(data.get("hits") or 0):
            break
    return jobs
