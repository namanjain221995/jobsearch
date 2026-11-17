"""Ashby public Job Postings API. https://developers.ashbyhq.com/docs/public-job-posting-api"""
import httpx

from ..job_normalizer import detect_work_mode, extract_salary, html_to_structured_text, html_to_text, pretty_slug
from ..schemas import Job
from .base import get_json, now_iso

API = "https://api.ashbyhq.com/posting-api/job-board/{board}"
_MODES = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site", "on-site": "On-site"}


def _country(address: dict | None) -> str:
    return str(((address or {}).get("postalAddress") or {}).get("addressCountry") or "")


def normalize(board: str, raw: dict, verified_at: str) -> Job | None:
    url = raw.get("jobUrl") or ""
    title = (raw.get("title") or "").strip()
    if not url or not title or not raw.get("id") or raw.get("isListed") is False:
        return None
    description = raw.get("descriptionPlain") or html_to_text(raw.get("descriptionHtml") or "")
    secondary = raw.get("secondaryLocations") or []
    locations = [raw.get("location") or ""] + [s.get("location") or "" for s in secondary]
    countries = [_country(raw.get("address"))] + [_country(s.get("address")) for s in secondary]
    us_words = {"us", "usa", "united states", "united states of america"}
    country_hint = ""
    known = [c for c in countries if c]
    if known:
        country_hint = "United States" if any(c.lower() in us_words for c in known) else known[0]
    comp = raw.get("compensation") or {}
    salary = comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary") or ""
    if not salary:
        salary = extract_salary(description)
    mode = _MODES.get(str(raw.get("workplaceType") or "").lower())
    if not mode:
        mode = "Remote" if raw.get("isRemote") else detect_work_mode(raw.get("location") or "")
    return Job(
        title=title,
        company=pretty_slug(board),
        location="; ".join(l for l in locations if l) or "Not listed",
        work_mode=mode,
        salary=salary or "Not listed",
        description=description,
        description_rich=html_to_structured_text(raw.get("descriptionHtml") or "")[:40000],
        url=url,
        apply_url=raw.get("applyUrl") or "",
        source="Ashby",
        sources=["Ashby"],
        ats="ashby",
        ats_id=str(raw["id"]).lower(),
        board=board,
        country_hint=country_hint,
        posted_at=(raw.get("publishedAt") or "")[:10],
        verified_at=verified_at,
        verification="Listed in the employer's live Ashby job postings API",
    )


async def fetch_board(client: httpx.AsyncClient, board: str, timeout: float = 60.0) -> list[Job]:
    data = await get_json(client, API.format(board=board), params={"includeCompensation": "true"}, timeout=timeout)
    stamp = now_iso()
    jobs = [normalize(board, raw, stamp) for raw in data.get("jobs", [])]
    return [j for j in jobs if j]
