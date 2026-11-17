"""Remote-job boards that publish a free public API. Each has usage terms, honoured here:

- Remote OK (remoteok.com/api): link back to the Remote OK URL and name Remote OK as the source.
- Jobicy (jobicy.com/api/v2/remote-jobs): credit Jobicy with a link; apply buttons go to the Jobicy URL;
  poll at most once per hour.
- Himalayas (himalayas.app/jobs/api/search): use `applicationLink` unchanged for Apply, show Himalayas as
  the source, do not pass listings on to other job boards. Only the first result page is read because
  robots.txt disallows `page=` URLs.

These are copies of listings (aggregator=True): when the employer's own ATS posting is also found,
the ATS record wins during deduplication."""
from ..schemas import Job
from .base import Ctx, iso_date, now_iso

GROUP = "remote-boards"


def _salary(lo, hi, currency: str = "USD", period: str = "") -> str:
    try:
        lo, hi = int(float(lo or 0)), int(float(hi or 0))
    except (TypeError, ValueError):
        return "Not listed"
    if lo <= 0 or hi <= 0:
        return "Not listed"
    return f"{currency or 'USD'} {lo:,} - {hi:,}" + (f" / {period}" if period else "")


# ---------------------------------------------------------------- Remote OK
async def fetch_remoteok(ctx: Ctx, key: str, scope: str) -> list[Job]:
    data = await ctx.json(GROUP, "GET", "https://remoteok.com/api")
    stamp = now_iso()
    jobs = []
    for raw in data if isinstance(data, list) else []:
        title, url = str(raw.get("position") or "").strip(), str(raw.get("url") or "")
        if not title or not url.startswith("https://") or not raw.get("company") or not raw.get("id"):
            continue      # the first element is the legal notice
        jobs.append(Job(
            title=title, company=str(raw["company"]).strip(), location=str(raw.get("location") or "").strip() or "Not listed",
            work_mode="Remote", salary=_salary(raw.get("salary_min"), raw.get("salary_max")),
            raw_html=str(raw.get("description") or ""), url=url, apply_url=url,
            source="Remote OK", sources=["Remote OK"], ats="remoteok", ats_id=str(raw["id"]), board="*",
            posted_at=iso_date(raw.get("date")), verified_at=stamp, aggregator=True,
            verification="Listed in the Remote OK public API (job board copy of the employer's listing)",
        ))
    return jobs


# ---------------------------------------------------------------- Jobicy
async def fetch_jobicy(ctx: Ctx, key: str, scope: str, pages: int = 3) -> list[Job]:
    stamp = now_iso()
    jobs, seen, cursor = [], set(), None
    for _ in range(max(1, pages)):
        params = {"count": 100, "geo": key or "usa"}
        if cursor:
            params["cursor"] = cursor
        data = await ctx.json(GROUP, "GET", "https://jobicy.com/api/v2/remote-jobs", params=params)
        for raw in data.get("jobs") or []:
            title, url = str(raw.get("jobTitle") or "").strip(), str(raw.get("url") or "")
            if not title or not url.startswith("https://") or not raw.get("id") or raw["id"] in seen:
                continue
            seen.add(raw["id"])
            geo = str(raw.get("jobGeo") or "").replace("  ", " ")
            jobs.append(Job(
                title=title, company=str(raw.get("companyName") or "").strip() or "Not listed",
                location=f"Remote ({geo})" if geo else "Remote", work_mode="Remote",
                salary=_salary(raw.get("salaryMin"), raw.get("salaryMax"), str(raw.get("salaryCurrency") or "USD"), str(raw.get("salaryPeriod") or "")),
                raw_html=str(raw.get("jobDescription") or raw.get("jobExcerpt") or ""), url=url, apply_url=url,
                source="Jobicy", sources=["Jobicy"], ats="jobicy", ats_id=str(raw["id"]), board=key or "usa",
                country_hint="United States" if "usa" in geo.lower() or "united states" in geo.lower() else "",
                posted_at=iso_date(raw.get("pubDate")), verified_at=stamp, aggregator=True,
                verification="Listed in the Jobicy public API (job board copy of the employer's listing)",
            ))
        cursor = data.get("nextCursor")
        if not cursor:
            break
    return jobs


# ---------------------------------------------------------------- Himalayas
async def fetch_himalayas(ctx: Ctx, key: str, scope: str) -> list[Job]:
    data = await ctx.json(GROUP, "GET", "https://himalayas.app/jobs/api/search", params={"q": scope, "country": "US"})
    stamp = now_iso()
    jobs = []
    for raw in data.get("jobs") or []:
        title, url = str(raw.get("title") or "").strip(), str(raw.get("guid") or "")
        if not title or not url.startswith("https://"):
            continue
        expiry = iso_date(raw.get("expiryDate"))
        if expiry and expiry < stamp[:10]:
            continue
        restrictions = [str(r) for r in raw.get("locationRestrictions") or []]
        us = any(r.lower() in ("united states", "usa", "us") for r in restrictions)
        apply_url = str(raw.get("applicationLink") or "")
        jobs.append(Job(
            title=title, company=str(raw.get("companyName") or "").replace("&#39;", "'").replace("&amp;", "&").strip() or "Not listed",
            location="Remote (" + ", ".join(restrictions[:6]) + ")" if restrictions else "Remote", work_mode="Remote",
            salary=_salary(raw.get("minSalary"), raw.get("maxSalary"), str(raw.get("currency") or "USD"), str(raw.get("salaryPeriod") or "")),
            raw_html=str(raw.get("description") or raw.get("excerpt") or ""), url=url,
            apply_url=apply_url if apply_url.startswith("https://") else "",
            source="Himalayas", sources=["Himalayas"], ats="himalayas", ats_id=url.rstrip("/").split("/companies/")[-1], board="*",
            country_hint="United States" if us else "", posted_at=iso_date(raw.get("pubDate")), verified_at=stamp,
            aggregator=True, verification="Listed in the Himalayas public API (job board copy of the employer's listing)",
        ))
    return jobs
