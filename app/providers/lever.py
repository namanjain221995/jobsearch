"""Lever public Postings API. https://github.com/lever/postings-api"""
from datetime import datetime, timezone

import httpx

from ..job_normalizer import detect_work_mode, extract_salary, html_to_structured_text, html_to_text, pretty_slug
from ..schemas import Job
from .base import get_json, now_iso

API = "https://api.lever.co/v0/postings/{board}"
_MODES = {"remote": "Remote", "hybrid": "Hybrid", "on-site": "On-site", "onsite": "On-site"}


def normalize(board: str, raw: dict, verified_at: str) -> Job | None:
    url = raw.get("hostedUrl") or ""
    title = (raw.get("text") or "").strip()
    if not url or not title or not raw.get("id"):
        return None
    lists = " ".join(f"{l.get('text', '')}: {html_to_text(l.get('content', ''))}" for l in raw.get("lists") or [])
    description = " ".join(p for p in [raw.get("descriptionPlain") or html_to_text(raw.get("description") or ""),
                                       lists, raw.get("additionalPlain") or ""] if p).strip()
    # Formatted version (paragraphs, section headings, bullets) for display, when Lever sends HTML.
    rich_html = ""
    if raw.get("description"):
        rich_html = (str(raw["description"]) + "".join(f"<h3>{l.get('text', '')}</h3><ul>{l.get('content', '')}</ul>"
                                                         for l in raw.get("lists") or []) + str(raw.get("additional") or ""))
    cats = raw.get("categories") or {}
    locations = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
    location = "; ".join(locations)
    salary = "Not listed"
    sr = raw.get("salaryRange") or {}
    if sr.get("min") and sr.get("max"):
        salary = f"{sr.get('currency', 'USD')} {int(sr['min']):,} - {int(sr['max']):,}" + (f" / {sr['interval']}" if sr.get("interval") else "")
    else:
        salary = extract_salary(description)
    posted = ""
    if raw.get("createdAt"):
        try:
            posted = datetime.fromtimestamp(int(raw["createdAt"]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            posted = ""
    mode = _MODES.get(str(raw.get("workplaceType") or "").lower()) or detect_work_mode(location)
    return Job(
        title=title,
        company=pretty_slug(board),
        location=location or "Not listed",
        work_mode=mode,
        salary=salary,
        description=description,
        description_rich=html_to_structured_text(rich_html)[:40000] if rich_html else "",
        url=url,
        apply_url=raw.get("applyUrl") or "",
        source="Lever",
        sources=["Lever"],
        ats="lever",
        ats_id=str(raw["id"]).lower(),
        board=board,
        country_hint=str(raw.get("country") or ""),
        posted_at=posted,
        verified_at=verified_at,
        verification="Listed in the employer's live Lever postings API",
    )


async def fetch_board(client: httpx.AsyncClient, board: str, timeout: float = 45.0) -> list[Job]:
    data = await get_json(client, API.format(board=board), params={"mode": "json"}, timeout=timeout)
    stamp = now_iso()
    jobs = [normalize(board, raw, stamp) for raw in (data if isinstance(data, list) else [])]
    return [j for j in jobs if j]
