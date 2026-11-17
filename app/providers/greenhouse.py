"""Greenhouse public Job Board API (per-company boards; this is not a cross-company search API).
https://developers.greenhouse.io/job-board.html"""
import httpx

from ..job_normalizer import detect_work_mode, pretty_slug
from ..schemas import Job
from .base import get_json, now_iso

API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"


def normalize(board: str, raw: dict, verified_at: str) -> Job | None:
    url = raw.get("absolute_url") or ""
    title = (raw.get("title") or "").strip()
    if not url or not title or not raw.get("id"):
        return None
    offices = [o.get("location") or o.get("name") or "" for o in raw.get("offices") or []]
    location = (raw.get("location") or {}).get("name") or ""
    full_location = location or "; ".join(o for o in offices if o)
    loc_type = " ".join(str(m.get("value") or "") for m in raw.get("metadata") or []
                        if "location type" in str(m.get("name") or "").lower() or "workplace" in str(m.get("name") or "").lower())
    job = Job(
        title=title,
        company=(raw.get("company_name") or pretty_slug(board)).strip(),
        location=full_location or "Not listed",
        work_mode=detect_work_mode(loc_type, location) if (loc_type or location) else "Not listed",
        raw_html=raw.get("content") or "",
        url=url,
        apply_url=url,   # Greenhouse hosts the application form on the posting page itself
        source="Greenhouse",
        sources=["Greenhouse"],
        ats="greenhouse",
        ats_id=str(raw["id"]),
        board=board,
        country_hint="",
        posted_at=(raw.get("first_published") or raw.get("updated_at") or "")[:10],
        verified_at=verified_at,
        verification="Listed in the employer's live Greenhouse job board API",
    )
    # Offices carry "City, State, United States" which is stronger than the display location.
    if offices and all("united states" in o.lower() for o in offices if o) and any(offices):
        job.country_hint = "United States"
    return job


async def fetch_board(client: httpx.AsyncClient, board: str, timeout: float = 45.0) -> list[Job]:
    data = await get_json(client, API.format(board=board), params={"content": "true"}, timeout=timeout)
    stamp = now_iso()
    jobs = [normalize(board, raw, stamp) for raw in data.get("jobs", [])]
    return [j for j in jobs if j]
