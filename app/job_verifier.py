"""Verification of job leads that did not come straight from a source API.

A lead is accepted only when its page proves it: schema.org JobPosting data, or page content that
matches the claimed job, and no closed/expired marker. HTTP 200 alone never counts.

Order of work, cheapest first:
1. plain HTTP fetch (fast);
2. headless browser render, only when the plain page loaded but needs JavaScript to show the job;
3. AI extraction (optional, batched by the engine) for pages with no structured data. Whatever the
   model returns must appear verbatim in the page text, otherwise it is discarded.

All fetches pass the SSRF guard and robots.txt. Sites that block automated access are not worked around."""
import asyncio
import html as html_lib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import httpx

from .job_normalizer import (analyze_authorization, detect_work_mode, extract_experience, extract_salary,
                             html_to_text, parse_ats)
from .job_ranker import best_title_relevance
from .netguard import UnsafeURL, assert_public_url  # noqa: F401  (assert_public_url is looked up on this module at call time)
from .providers.base import USER_AGENT, RobotsCache, now_iso
from .schemas import Candidate, Job

MAX_PAGE_BYTES = 1_500_000
# Sites that forbid automated access or require login: never fetched, leads from them are not verifiable here.
BLOCKED_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "monster.com",
                 "dice.com", "simplyhired.com", "careerbuilder.com", "facebook.com", "x.com", "twitter.com")
_CLOSED = re.compile(
    r"(no longer (accepting|available|open|active|posted)|position (has been|is|was) (filled|closed)|"
    r"job (has|is) (expired|closed|been filled|been removed)|this (job|position|posting|role|opening|requisition) "
    r"(is|has been|has) (no longer|closed|expired|filled|removed|been closed|been filled)|posting (has )?(closed|expired)|"
    r"applications? (are|is) (now )?closed|job not found|page not found|sorry, (this|the) (job|position))", re.I)
_JSONLD = re.compile(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)
_ANCHOR = re.compile(r"(?is)<a\b[^>]*?href\s*=\s*[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>")
_SITE_NAME = re.compile(r"(?is)<meta[^>]+property=[\"']og:site_name[\"'][^>]+content=[\"']([^\"']{2,80})[\"']")
NEEDS_RENDER = ("No structured job data on page", "Page title does not match the claimed job",
                "No application action found on page", "Page lacks a job title or employer")


def host_blocked(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS)


class Budget:
    """A small counter for per-search limits (browser renders, crawled links, ...)."""

    def __init__(self, amount: int):
        self.left = max(0, amount)
        self.used = 0

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        self.used += 1
        return True


@dataclass
class Page:
    url: str
    status: int
    html: str
    text: str
    rendered: bool = False


@dataclass
class Verdict:
    job: Job | None = None
    reason: str = ""
    page: Page | None = None      # set when the page loaded but could not be verified deterministically


class PageVerifier:
    def __init__(self, client: httpx.AsyncClient, browser=None, timeout: float = 15.0):
        self.client = client
        self.browser = browser
        self.timeout = timeout
        self.robots = RobotsCache(fetcher=self._robots_fetch)

    async def _fetch(self, url: str) -> tuple[int, str, str]:
        """GET with manual, validated redirects and a size cap. Returns (status, final_url, text)."""
        current = url
        for _ in range(4):
            await assert_public_url(current)
            async with self.client.stream("GET", current, timeout=self.timeout, follow_redirects=False,
                                          headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"}) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    current = urljoin(current, r.headers["location"])
                    continue
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    chunks.append(chunk)
                    if size > MAX_PAGE_BYTES:
                        break
                return r.status_code, current, b"".join(chunks)[:MAX_PAGE_BYTES].decode(r.encoding or "utf-8", "replace")
        raise UnsafeURL("Too many redirects")

    async def _robots_fetch(self, url: str) -> tuple[int, str]:
        status, _, text = await self._fetch(url)
        return status, text

    async def verify(self, cand: Candidate, browser_budget: Budget | None = None) -> Verdict:
        """Check one lead. Verdict.job is set only when the page proves a live posting."""
        if host_blocked(cand.url):
            return Verdict(reason="Source does not permit automated verification")
        try:
            if not await self.robots.allowed(self.client, cand.url):
                return Verdict(reason="Disallowed by robots.txt")
            status, final_url, body = await self._fetch(cand.url)
        except UnsafeURL as exc:
            return Verdict(reason=f"URL rejected: {exc}")
        except (httpx.HTTPError, asyncio.TimeoutError):
            return Verdict(reason="Page could not be fetched")
        if status in (404, 410):
            return Verdict(reason="Posting not found (closed or removed)")
        if status != 200:
            return Verdict(reason=f"Page returned HTTP {status}")     # includes 403: blocks are not worked around
        page = Page(final_url, status, body, html_to_text(body))
        job, reason = _evaluate(page, cand)
        if job or reason not in NEEDS_RENDER:
            return Verdict(job=job, reason=reason)          # verified, or definitely not usable (closed, expired)
        if not (self.browser and self.browser.available and browser_budget and browser_budget.take()):
            return Verdict(reason=reason, page=page)
        if not await self.robots.allowed(self.client, final_url):
            return Verdict(reason="Disallowed by robots.txt")
        result = await self.browser.render(final_url)
        if result.error:
            note = "site blocks automated browsers (not bypassed)" if result.error == "blocked" else result.error
            return Verdict(reason=f"{reason}; browser render failed: {note}", page=page)
        rendered = Page(result.url or final_url, result.status or 200, result.html, html_to_text(result.html), rendered=True)
        job, reason = _evaluate(rendered, cand)
        return Verdict(job=job, reason=reason, page=rendered if (not job and reason in NEEDS_RENDER) else None)

    def listing_links(self, page: Page, titles: list[str], related: list[str], parent: Candidate,
                      limit: int = 8) -> list[Candidate]:
        """Links on a careers/listing page whose anchor text reads like one of the wanted job titles.
        Only same-site or known ATS links are followed, and each one is verified like any other lead."""
        base_host = (urlsplit(page.url).hostname or "").lower()
        site = ".".join(base_host.split(".")[-2:])
        company = parent.company or (m.group(1).strip() if (m := _SITE_NAME.search(page.html)) else "")
        out: dict[str, Candidate] = {}
        for href, inner in _ANCHOR.findall(page.html):
            text = html_to_text(inner)[:160].strip()
            if len(text) < 4 or best_title_relevance(text, titles, related) < 0.6:
                continue
            url = urljoin(page.url, html_lib.unescape(href.strip()))
            host = (urlsplit(url).hostname or "").lower()
            if not url.startswith(("http://", "https://")) or url.split("#")[0] == page.url.split("#")[0]:
                continue
            if not (host.endswith(site) or parse_ats(url)):
                continue
            out.setdefault(url, Candidate(url=url, title=text[:120], company=company, location="",
                                          provider=parent.provider or "Web crawl", cited=True))
            if len(out) >= limit:
                break
        return list(out.values())


def _evaluate(page: Page, cand: Candidate) -> tuple[Job | None, str]:
    if _CLOSED.search(page.text[:6000]):
        return None, "Posting is marked closed"
    posting = _find_job_posting(page.html)
    if posting:
        return _job_from_jsonld(posting, cand, page)
    return _job_from_content(cand, page)


def _find_job_posting(body: str) -> dict | None:
    for block in _JSONLD.findall(body):
        try:
            data = json.loads(html_lib.unescape(block.strip()) if "&quot;" in block else block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                kind = node.get("@type")
                if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                    return node
                stack.extend(v for v in node.values() if isinstance(v, (list, dict)))
    return None


def _jsonld_location(posting: dict) -> tuple[str, str]:
    places = posting.get("jobLocation") or []
    places = places if isinstance(places, list) else [places]
    names, countries = [], []
    for place in places:
        addr = (place or {}).get("address") if isinstance(place, dict) else None
        if isinstance(addr, dict):
            country = addr.get("addressCountry")
            if isinstance(country, dict):
                country = country.get("name")
            parts = [addr.get("addressLocality"), addr.get("addressRegion"), country]
            names.append(", ".join(str(p) for p in parts if p))
            if country:
                countries.append(str(country))
        elif isinstance(addr, str):
            names.append(addr)
    req = posting.get("applicantLocationRequirements")
    for r in (req if isinstance(req, list) else [req] if req else []):
        if isinstance(r, dict) and r.get("name"):
            names.append(f"Remote ({r['name']})")
            countries.append(str(r["name"]))
    us = {"us", "usa", "united states", "united states of america"}
    hint = ""
    if countries:
        hint = "United States" if any(c.lower() in us for c in countries) else countries[0]
    return "; ".join(n for n in names if n), hint


def _how(page: Page) -> str:
    return "Posting page rendered in a headless browser" if page.rendered else "Posting page fetched"


def _source(cand: Candidate) -> str:
    return f"{cand.provider} web search" if cand.provider in ("OpenAI", "Claude") else (cand.provider or "Web crawl")


def _job_from_jsonld(posting: dict, cand: Candidate, page: Page) -> tuple[Job | None, str]:
    title = html_lib.unescape(str(posting.get("title") or "")).strip()
    org = posting.get("hiringOrganization")
    company = html_lib.unescape(str(org.get("name") if isinstance(org, dict) else org or "")).strip() or cand.company
    if not title or not company:
        return None, "Page lacks a job title or employer"
    valid = str(posting.get("validThrough") or "")
    if valid:
        try:
            expiry = datetime.fromisoformat(valid.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry < datetime.now(timezone.utc):
                return None, "Posting expired (validThrough passed)"
        except ValueError:
            pass
    description = html_to_text(str(posting.get("description") or "")) or page.text[:8000]
    location, hint = _jsonld_location(posting)
    remote = "TELECOMMUTE" in str(posting.get("jobLocationType") or "").upper()
    salary = "Not listed"
    base = posting.get("baseSalary")
    if isinstance(base, dict):
        value = base.get("value")
        cur = base.get("currency") or ""
        if isinstance(value, dict) and value.get("minValue") and value.get("maxValue"):
            salary = f"{cur} {value['minValue']} - {value['maxValue']}" + (f" / {value['unitText']}" if value.get("unitText") else "")
        elif isinstance(value, (int, float, str)) and value:
            salary = f"{cur} {value}".strip()
    if salary == "Not listed":
        salary = extract_salary(description)
    years, label = extract_experience(description)
    source = _source(cand)
    return Job(
        title=title, company=company, location=location or cand.location or "Not listed",
        work_mode="Remote" if remote else detect_work_mode(location, description[:600]),
        salary=salary, experience_required=label, required_years=years, description=description,
        url=page.url, apply_url="", source=source, sources=[source],
        country_hint=hint, posted_at=str(posting.get("datePosted") or "")[:10], verified_at=now_iso(),
        verification=f"{_how(page)}; schema.org JobPosting data present and not expired",
        **analyze_authorization(description),
    ), ""


def _job_from_content(cand: Candidate, page: Page) -> tuple[Job | None, str]:
    """No structured data: accept only when the page itself shows the claimed title and an apply action."""
    if not cand.title or not cand.company or not cand.cited:
        return None, "No structured job data on page"
    head = " ".join(re.findall(r"(?is)<(?:title|h1)[^>]*>(.*?)</(?:title|h1)>", page.html))
    head_text = html_to_text(head).lower()
    title_tokens = [t for t in re.findall(r"[a-z0-9+#]+", cand.title.lower()) if len(t) > 1]
    if not title_tokens or sum(t in head_text for t in title_tokens) / len(title_tokens) < 0.8:
        return None, "Page title does not match the claimed job"
    if not re.search(r"\bapply\b", page.text, re.I):
        return None, "No application action found on page"
    return _content_job(cand.title.strip(), cand.company.strip(), cand.location, cand, page,
                        "title and apply action present, no closed marker"), ""


def _content_job(title: str, company: str, location: str, cand: Candidate, page: Page, evidence: str) -> Job:
    years, label = extract_experience(page.text)
    source = _source(cand)
    return Job(
        title=title, company=company, location=location or "Not listed",
        work_mode=detect_work_mode(location, page.text[:800]), salary=extract_salary(page.text),
        experience_required=label, required_years=years, description=page.text[:12000], url=page.url, apply_url="",
        source=source, sources=[source], verified_at=now_iso(), verification=f"{_how(page)}; {evidence}",
        **analyze_authorization(page.text),
    )


def job_from_extraction(page: Page, cand: Candidate, data: dict) -> tuple[Job | None, str]:
    """Turn an AI extraction into a job only if every extracted value is literally on the page."""
    if not data.get("is_open_job_posting"):
        return None, "AI check: not a single open job posting"
    text = " ".join(page.text.lower().split())

    def on_page(value: str) -> bool:
        v = " ".join(str(value).lower().split())
        return len(v) >= 3 and v in text

    title = str(data.get("title") or "").strip()[:200]
    company = str(data.get("company") or "").strip()[:120] or cand.company
    location = str(data.get("location") or "").strip()[:160]
    if not title or not on_page(title):
        return None, "AI-extracted title is not on the page"
    if not company or not on_page(company):
        return None, "AI-extracted employer is not on the page"
    if location and not on_page(location):
        location = ""
    if not re.search(r"\bapply\b", page.text, re.I):
        return None, "No application action found on page"
    return _content_job(title, company, location, cand, page,
                        "job details extracted by AI and matched word for word against the page text"), ""
