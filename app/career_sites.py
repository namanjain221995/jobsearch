"""Career-site crawler: an employer's own career site, once known, becomes a source of all its US postings.

Until now the portal learned single postings on company sites from the AI web search and kept each one as a unit of
the source `career_page` (search_engine._remember_pages). It knew nothing about the company's other openings. Here the
site itself is remembered and read in the background, without any ATS platform:

1. Site record (table index_career_sites, migration a8c5e3f1d2b7). `Crawler.note_posting` is called for every posting
   a search keeps as a `career_page` unit; when its host is an employer's own (not a career platform, not a job board,
   not a host that is never fetched: `refusal`), host, employer name and the posting's address are stored once.
   `python -m app.portal.import_boards --career-sites FILE` registers sites from a file. No personal data is stored.
2. Listing discovery, cheapest evidence first:
   a. robots.txt `Sitemap:` lines, else /sitemap.xml; sitemap indexes and gzip files are followed (a bounded number
      of files, job-named ones first). Kept are addresses of the site's own host that look like ONE posting
      (`Addresses.clean`: the strict rules of job_verifier.not_one_posting and aws_web_search.employer_posting_address,
      a few more path words, plus an id or a long slug, or the shape of a posting already verified on this site; a
      list, search, category, home or form address never), newest `lastmod` first;
   b. the site's listing page by a plain request: addresses in JobPosting data, in embedded JSON and in its links; a
      career home page that only points to its job list ("View all jobs") is followed there;
   c. only when (a) and (b) gave nothing: the listing page rendered in the headless browser, with "next" / "load
      more" followed where it is a plain link, and, where it is a button of the list without an address, by pressing
      that button up to CAREER_SITE_LIST_STEPS times with the host's pause before each press (browser_pool.render_list:
      the only control the browser ever presses; never a form's button, nothing is typed; 0 switches it off). Measured
      on 2026-11-21 on a list of 2,408 jobs shown behind a "Next jobs" button: 20 posting addresses without it, 119
      with ten presses.
   When what the sitemaps named proved no posting at all (a sitemap of marketing pages), (b) and (c) are tried too.
   Nothing is submitted, no login, no CAPTCHA, no evasion: a site that answers 401 / 403 / 429 or a robot check, or
   whose robots.txt disallows the pages, is recorded as not readable and left alone for a long time. A listing that
   redirects to another host is not followed there.
3. Reading postings. Every candidate address goes through `PageVerifier.verify` (plain request, all its vetoes) and
   counts only with schema.org JobPosting data on the page: the crawler makes no claim about a page and asks no
   model. A verified posting in the United States (`in_united_states`) is handed to the engine's own
   `_remember_pages`, so it is stored exactly like a posting a search found and searches find it through the normal
   index path. Postings that only show in a browser are not kept (the page reader that keeps remembered pages current
   uses plain requests); such a site is recorded as "needs a browser". Pages without JobPosting data are not kept
   either: a search hands those to the AI page check, the crawler asks no model.
4. Background only (`tick`, called once a minute by board_catalog.catalog_tick, never by a search; no crawl starts and
   a running one reads no page while the engine has more than board_catalog.MAX_INFLIGHT reads in flight), bounded and polite:
   one site at most once in CAREER_SITE_CRAWL_DAYS, at most CAREER_SITE_PAGES_PER_CRAWL new pages per site and crawl
   and CAREER_SITE_PAGES_PER_DAY in 24 hours, one request at a time per host with at least one second (or the site's
   Crawl-delay) of silence between them, robots.txt for every request (sitemaps included, every redirect target), the
   SSRF guard and the pinned address on every request and redirect. Every crawl is recorded (index_career_crawls); a
   site that failed, is not readable or brought no US posting waits longer each time (`next_wait`).

Pages already read on a site are remembered by hash (`seen`), so the next crawl spends its budget on new addresses."""
import asyncio
import contextvars
import hashlib
import html as html_lib
import logging
import re
import time
import zlib
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from . import ats_platforms, board_catalog, job_verifier
from .browser_pool import _CHALLENGE
from .job_normalizer import norm_company
from .job_verifier import Budget, Page, PageVerifier, find_postings, never_fetched, not_one_posting, page_text
from .netguard import UnsafeURL, pinned
from .providers import ats_pages, aws_web_search
from .providers.base import ROBOTS_TOKEN, USER_AGENT, iso_date
from .providers.paid import aggregator_hosts, is_aggregator
from .schemas import Candidate, Job

log = logging.getLogger(__name__)

CAREER_PAGE_SOURCE = "Company career site"      # Job.source of a crawled posting: the same as a remembered page read again
# States of a site after a crawl.
NEW, OK, NO_US, NO_POSTINGS, NEEDS_BROWSER, NOT_READABLE, NOT_A_COMPANY, FAILED = (
    "new", "ok", "no_us_postings", "no_postings_found", "needs_browser", "not_readable", "not_a_company_site", "failed")
SITEMAP, LISTING_DATA, LISTING_LINKS, BROWSER = "sitemap", "structured data", "listing links", "browser"

MAX_URL_CHARS = 600                      # the same limit as a remembered page (search_engine._remember_pages)
MAX_SITEMAP_BYTES = 16_000_000           # read from one sitemap response
MAX_SITEMAP_TEXT = 48_000_000            # of one sitemap after decompression (the protocol allows 50 MB)
MAX_SITEMAP_ENTRIES = 50_000             # entries looked at in one sitemap file (the protocol's own limit)
MAX_SITEMAP_CHILDREN = 500               # child sitemaps of an index that are considered at all
MAX_CANDIDATES = 20_000                  # posting addresses kept for one crawl before the newest are chosen
MAX_LISTING_BYTES = 3_000_000
LISTING_TRIES = 4                        # listing addresses asked by a plain request in one crawl
LISTING_PAGES = 3                        # pages of one listing followed through plain "next" links
MAX_CRAWL_DELAY = 60.0                   # a site that asks for more seconds between requests than this is not crawled
MAX_SEEN = 20_000                        # page hashes remembered per site
MIN_BUDGET = 10                          # no crawl is started with fewer pages left in the day's budget
NOT_READABLE_DAYS = 30.0                 # first wait after "not readable"
SHAPE_TRIES, SHAPE_US_TRIES = 6, 25      # an address shape is given up after this many pages without a posting / without a US posting
CRAWL_LOG_DAYS = 180.0
BUSY_PAUSE = 5.0                         # seconds a crawl stands back before it looks again whether the engine is still busy

# Hosts that are not one employer's career site although their pages may carry job data: career platforms beyond the
# 17 of app/ats_platforms.py (whose hosts are refused through that list) and job boards beyond AGGREGATOR_HOSTS.
# A leading "." is not needed: every entry also covers its subdomains.
NOT_COMPANY_SITES = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "myworkdaysite.com", "workable.com",
    "smartrecruiters.com", "jobvite.com", "applytojob.com", "avature.net", "oraclecloud.com", "workforcenow.adp.com",
    "recruiting.adp.com", "myjobs.adp.com", "recruitee.com", "taleo.net", "ultipro.com", "ukg.net", "paylocity.com",
    "paycomonline.net", "dayforcehcm.com", "teamtailor.com", "breezy.hr", "jobs.personio.com", "jobs.personio.de", "jobscore.com",
    "eightfold.ai", "phenompeople.com", "csod.com", "silkroad.com", "hirebridge.com", "applicantpro.com", "isolvedhire.com",
    "clearcompany.com", "freshteam.com", "zohorecruit.com", "comeet.com", "trinethire.com", "jobs.gusto.com",
    "bullhornstaffing.com", "ceipal.com", "jobdiva.com",
    "tal.net", "brassring.com", "hrmdirect.com", "recruiterbox.com", "jobs.net", "careerplug.com", "applicantstack.com",
    "remoteok.com", "weworkremotely.com", "himalayas.app", "jobicy.com", "ycombinator.com", "workatastartup.com",
    "usajobs.gov", "governmentjobs.com", "idealist.org", "otta.com", "welcometothejungle.com", "higheredjobs.com",
    "clearancejobs.com", "efinancialcareers.com", "themuse.com", "craigslist.org", "github.io", "notion.site",
    "wixsite.com", "webflow.io", "wordpress.com", "blogspot.com", "medium.com", "substack.com", "youtube.com", "reddit.com")

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(rf"(?:{_LABEL}\.)+[a-z]{{2,24}}")          # a DNS name with a letters-only top level: never an IP literal
_INTERNAL = (".local", ".internal", ".localhost", ".localdomain", ".lan", ".home", ".corp", ".test", ".invalid", ".example", ".onion")
_BAD_CHARS = re.compile(r"[\s\\<>\"'`{}|^\x00-\x1f\x7f]")
_NOT_A_PAGE = re.compile(r"(?i)\.(pdf|docx?|xlsx?|pptx?|zip|gz|xml|json|rss|atom|txt|csv|ics|jpe?g|png|gif|svg|webp|ico|css|js|mp4|mp3|woff2?)$")
# Path words before a posting's own name that aws_web_search._POSTING_PATH does not know ("/profile/job_details/123",
# "/roles/123-title", "/opportunities/123"). A candidate is only ever read and has to prove itself on its page.
_POSTING_PATH = re.compile(r"(?i)/(job[-_]?details?|job[-_]?descriptions?|job[-_]?postings?|opportunit(?:y|ies)|roles?|postings?|listings?|"
                           r"details?)/[^/?#]*[A-Za-z0-9][^/?#]*")
_FORM_SEGMENTS = {"apply", "application", "applications", "login", "signin", "sign-in", "register", "signup", "sign-up"}      # forms, never read
_JOBISH = re.compile(r"(?i)job|career|position|opening|vacanc|requisition|posting")
_ENTRY = re.compile(r"(?is)<(?:\w+:)?(url|sitemap)\b[^>]*>(.*?)</(?:\w+:)?\1\s*>")
_LOC = re.compile(r"(?is)<(?:\w+:)?loc\b[^>]*>(.*?)</(?:\w+:)?loc\s*>")
_LASTMOD = re.compile(r"(?is)<(?:\w+:)?lastmod\b[^>]*>(.*?)</(?:\w+:)?lastmod\s*>")
_CDATA = re.compile(r"(?s)^\s*<!\[CDATA\[(.*?)\]\]>\s*$")
_ABSOLUTE = re.compile(r"https://[^\s\"'<>\\`]{4,600}")
_ROOTED = re.compile(r"[\"'](/[A-Za-z0-9][^\s\"'<>\\`]{2,400})[\"']")
_REL_NEXT = re.compile(r"(?is)<(?:a|link)\b[^>]*\brel\s*=\s*[\"']?[^\"'>]*\bnext\b[^>]*>")
_HREF = re.compile(r"(?is)\bhref\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_NEXT_TEXT = re.compile(r"(?i)^\W*(next( page)?|load more( jobs)?|show more( jobs)?|view more( jobs)?|more (jobs|openings|positions|results)|older)\W*$")
_TITLE = re.compile(r"(?is)<title\b[^>]*>(.*?)</title>")
_SCRIPT = re.compile(r"(?is)<script\b[^>]*>(.*?)</script\s*>")

_CRAWL: contextvars.ContextVar = contextvars.ContextVar("career_site_crawl", default=None)


# ---------------------------------------------------------------- which hosts are a site
def site_key(host: str) -> str:
    """The key of a site: its host name in lower case without "www.". '' when this is not a public DNS name the
    crawler accepts (an IP literal, a single label, an internal name, anything that is not plain ASCII)."""
    host = (host or "").strip().lower().rstrip(".")
    if not host or len(host) > 253 or not host.isascii() or not _HOST.fullmatch(host) or host.endswith(_INTERNAL):
        return ""
    return host.removeprefix("www.")


def same_site(key: str, host: str) -> bool:
    """`host` is the site `key` or lies directly above or below it (careers.example.com, example.com, eu.careers.example.com).
    Sibling hosts are another site."""
    other = site_key(host)
    return bool(other) and (other == key or other.endswith("." + key) or key.endswith("." + other))


def _under(host: str, names: Iterable[str]) -> bool:
    return any(host == n or host.endswith("." + n) for n in names)


def refusal(host: str, aggregators: tuple[str, ...] = ()) -> str:
    """'' when `host` may be kept and crawled as one employer's career site, else the reason why not."""
    key = site_key(host)
    if not key:
        return "not a public host name"
    url = f"https://{key}/"
    if never_fetched(url) or never_fetched(f"https://www.{key}/"):
        return "a host that is never fetched"
    if is_aggregator(url, aggregators):
        return "a job board or aggregator"
    if ats_platforms.platform_of_host(key) is not None or ats_pages.is_page_host(key) or ats_pages.platform_of(url):
        return "a career platform's host, not an employer's own site"
    if _under(key, NOT_COMPANY_SITES):
        return "a career platform, job board or publishing host, not an employer's own site"
    return ""


# ---------------------------------------------------------------- which addresses are one posting
def _idlike(segment: str) -> bool:
    """A path segment that names one thing: digits, a long slug, an opaque id (the rule of job_verifier._link_run)."""
    return bool(re.search(r"\d{3,}", segment) or len(segment) > 24 or segment.count("-") >= 2
                or (len(segment) >= 8 and re.search(r"[a-z]", segment) and re.search(r"[A-Z0-9]", segment[1:]) and "-" not in segment))


def shape(url: str) -> str:
    """The address with everything that names one posting replaced by "*": /careers/job/790298 -> /careers/job/*,
    /us/en/job/R17/IT-Auditor -> /us/en/job/*/* (what follows an id is that posting's own name, however short)."""
    p = urlsplit(url)
    segments, named = [], False
    for segment in (s for s in p.path.split("/") if s):
        named = named or _idlike(segment)
        segments.append("*" if named else segment.lower())
    keys = sorted(k.lower() for k, v in parse_qsl(p.query) if job_verifier._ID_KEY.search(k) and re.search(r"\d{3,}|[0-9a-f-]{16,}", v))
    return "/" + "/".join(segments) + ("?" + "&".join(keys) if keys else "")


_LOCALE = re.compile(r"(?i)(?:^|/)([a-z]{2})[-_]([a-z]{2})(?=/|$)")
_US_WORDS = re.compile(r"(?i)(?<![a-z])(united[-_ ]states|usa)(?![a-z])")


def us_hint(url: str) -> int:
    """What the address itself says about the country: 1 = United States ("/en-us/", "united-states"), -1 = another
    country's locale ("/de-de/", "/en-gb/"), 0 = nothing. Only the order of reading depends on it; whether a posting is
    in the United States is decided on its page."""
    path = urlsplit(url).path
    if _US_WORDS.search(path):
        return 1
    m = _LOCALE.search(path)
    if m:
        return 1 if m.group(2).lower() == "us" else -1
    first = path.split("/")[1].lower() if path.count("/") >= 2 else ""       # "/fr/positions/123": a language edition
    return 0 if len(first) != 2 or not first.isalpha() or first == "en" else 1 if first == "us" else -1


def _parent(url: str) -> str:
    segments = [s.lower() for s in urlsplit(url).path.split("/") if s]
    return f"{len(segments)}:" + "/".join(segments[:-1])


class Addresses:
    """Which addresses of one site are read as single postings. `known` are postings already verified on the site: an
    address shaped like one of them (or lying beside one) is a candidate even where the general patterns do not fit."""

    def __init__(self, key: str, known: Iterable[str] = (), aggregators: tuple[str, ...] = ()):
        self.key = key
        self.aggregators = aggregators
        known = [u for u in known if u]
        self.shapes = {s for s in (shape(u) for u in known) if "*" in s or "?" in s}
        # A known posting whose own name is a short word ("/jobs/ml-1") says nothing by its shape: what lies beside it counts.
        self.parents = {_parent(u) for u in known if _parent(u).split(":", 1)[1] and "*" not in shape(u).rsplit("/", 1)[-1]}

    def like_known(self, url: str) -> bool:
        return shape(url) in self.shapes or _parent(url) in self.parents

    def clean(self, url: str) -> str | None:
        """The address without its fragment when it may be read as one posting of this site, else None. A listing,
        search, category or home address is never a posting; nor is anything on another host."""
        if not isinstance(url, str) or not (12 <= len(url) <= MAX_URL_CHARS) or _BAD_CHARS.search(url):
            return None
        try:
            p = urlsplit(url)
            port = p.port
        except ValueError:
            return None
        if p.scheme != "https" or p.username is not None or p.password is not None or port not in (None, 443):
            return None
        if site_key(p.hostname or "") != self.key or _NOT_A_PAGE.search(p.path) or ".." in p.path:
            return None
        if {seg.lower() for seg in p.path.split("/")} & _FORM_SEGMENTS:          # an application or sign-in form is not a posting
            return None
        url = p._replace(fragment="").geturl()
        if never_fetched(url) or not_one_posting(url):
            return None
        if aws_web_search._LISTING_PATH.search(p.path) or {k.lower() for k, _ in parse_qsl(p.query)} & aws_web_search._SEARCH_PARAMS:
            return None
        if self.like_known(url):
            return url
        if not (aws_web_search.employer_posting_address(url, self.aggregators) or _POSTING_PATH.search(p.path)):
            return None
        form = shape(url)
        return url if "*" in form or "?" in form else None  # "/jobs/engineering" is a team page; a posting carries an id or a long slug


# ---------------------------------------------------------------- sitemaps
def inflate(data: bytes, limit: int = MAX_SITEMAP_TEXT) -> bytes:
    """`data` without its gzip wrapping (a .xml.gz file arrives as raw gzip bytes), never more than `limit` bytes:
    a small file that unpacks to gigabytes is cut off, not unpacked."""
    for _ in range(2):
        if data[:2] != b"\x1f\x8b":
            break
        try:
            data = zlib.decompressobj(47).decompress(data, limit)
        except zlib.error:
            return b""
    return data[:limit]


def parse_sitemap(data: bytes, limit: int = MAX_SITEMAP_ENTRIES) -> tuple[str, list[tuple[str, str]]]:
    """("index" | "urlset" | "", [(address, lastmod as YYYY-MM-DD or '')]) of one sitemap file. Read with patterns, not
    with an XML parser: entity tricks and broken markup cannot hurt, and at most `limit` entries are looked at."""
    text = inflate(data).decode("utf-8", "replace")
    head = text[:4000].lower()
    kind = "index" if "<sitemapindex" in head or ":sitemapindex" in head else "urlset" if "<urlset" in head or ":urlset" in head else ""
    out: list[tuple[str, str]] = []
    if not kind:
        if "<" in head:                                   # some other document (HTML, RSS): not a sitemap
            return "", []
        for line in text.splitlines()[:limit]:            # a plain-text sitemap: one address a line
            line = line.strip()
            if line.startswith("https://"):
                out.append((line, ""))
        return ("urlset" if out else ""), out
    want = "sitemap" if kind == "index" else "url"
    for n, m in enumerate(_ENTRY.finditer(text)):
        if n >= limit:
            break
        if m.group(1).lower() != want:
            continue
        loc = _LOC.search(m.group(2))
        if not loc:
            continue
        address = loc.group(1)
        cdata = _CDATA.match(address)
        address = html_lib.unescape((cdata.group(1) if cdata else address).strip())
        mod = _LASTMOD.search(m.group(2))
        out.append((address, iso_date(html_lib.unescape(mod.group(1).strip())) if mod else ""))
    return kind, out


def robots_extras(text: str) -> tuple[list[str], float]:
    """(`Sitemap:` addresses, Crawl-delay in seconds for this crawler or for "*") of one robots.txt."""
    sitemaps: list[str] = []
    delays: dict[str, float] = {}
    agents: list[str] = []
    last_was_agent = False
    for raw in text.splitlines()[:20000]:
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        name, value = (part.strip() for part in line.split(":", 1))
        name = name.lower()
        if name == "sitemap":
            if value and len(sitemaps) < 50:
                sitemaps.append(value)
        elif name == "user-agent":
            if not last_was_agent:
                agents = []
            agents.append(value.lower())
            last_was_agent = True
        else:
            last_was_agent = False
            if name == "crawl-delay":
                try:
                    seconds = float(value)
                except ValueError:
                    continue
                for agent in agents:
                    delays[agent] = max(0.0, seconds)
    return sitemaps, delays.get(ROBOTS_TOKEN, delays.get("*", 0.0))


# ---------------------------------------------------------------- listing pages
def page_addresses(page: Page) -> tuple[list[str], list[str]]:
    """(addresses in the page's data, addresses of its links), absolute, in page order. Data: the `url` of JobPosting
    records and every address inside a script element, embedded JSON included (escaped slashes too). Nothing is judged
    here."""
    markup = page.html[:MAX_LISTING_BYTES]
    data: list[str] = []
    try:
        postings, _ = find_postings(markup)
    except Exception:
        postings = []
    for node in postings[:2000]:
        value = node.get("url") or node.get("@id") or ""
        if isinstance(value, str) and value.strip():
            data.append(urljoin(page.url, value.strip()))
    plain = "\n".join(_SCRIPT.findall(markup)).replace("\\u002F", "/").replace("\\u002f", "/").replace("\\/", "/")
    for n, m in enumerate(_ABSOLUTE.finditer(plain)):
        if n >= 20000:
            break
        data.append(html_lib.unescape(m.group(0)).rstrip(".,;)"))
    for n, m in enumerate(_ROOTED.finditer(plain)):
        if n >= 20000:
            break
        data.append(urljoin(page.url, html_lib.unescape(m.group(1))))
    links = []
    for href, _inner in job_verifier._ANCHOR.findall(markup)[:5000]:
        href = html_lib.unescape(href.strip())
        if href and not href.lower().startswith(("javascript:", "mailto:", "tel:", "data:")):
            links.append(urljoin(page.url, href))
    return list(dict.fromkeys(data)), list(dict.fromkeys(links))


def next_pages(page: Page, key: str) -> list[str]:
    """Addresses of the listing's further pages, where the page names them as plain links: rel="next", or a link that
    says "Next" / "Load more". Only addresses on the site's own host; a button without an address is never pressed."""
    here = page.url.split("#")[0]
    found: list[str] = []
    for tag in _REL_NEXT.findall(page.html[:MAX_LISTING_BYTES])[:20]:
        m = _HREF.search(tag)
        if m:
            found.append(urljoin(page.url, html_lib.unescape((m.group(1) or m.group(2) or "").strip())))
    for attrs, inner in job_verifier._A_TAG.findall(page.html[:MAX_LISTING_BYTES])[:5000]:
        got = {k.lower(): html_lib.unescape(a or b).strip() for k, a, b in job_verifier._ATTR.findall(attrs)}
        label = job_verifier.html_to_text(inner).strip() or got.get("aria-label", "")
        if got.get("href") and len(label) <= 40 and (_NEXT_TEXT.match(label) or _NEXT_TEXT.match(got.get("aria-label", ""))):
            found.append(urljoin(page.url, got["href"]))
    out = []
    for url in dict.fromkeys(u.split("#")[0] for u in found):
        try:
            p = urlsplit(url)
        except ValueError:
            continue
        if p.scheme == "https" and site_key(p.hostname or "") == key and url != here and len(url) <= MAX_URL_CHARS \
                and not _BAD_CHARS.search(url) and not never_fetched(url):
            out.append(url)
    return out[:4]


_JOBS_LABEL = re.compile(r"(?i)^\W*((view|see|search|browse|explore|find|show)\s+)?((all|our|open|current|available)\s+)*"
                         r"(jobs?|job search|job openings?|roles?|positions?|openings?|opportunities|vacancies|careers)\W*$")
_JOBS_PATH = {"jobs", "all-jobs", "open-positions", "openings", "positions", "search", "job-search", "search-jobs", "jobsearch",
              "opportunities", "vacancies", "roles", "open-roles", "search-results", "careers", "all", "list"}


def jobs_links(page: Page, key: str) -> list[str]:
    """Links of a career page that say where the openings are listed ("View all jobs", "Open positions", /all-jobs),
    on the site's own host, the ones whose address also says so first. Used when the page itself named no posting."""
    here = page.url.split("#")[0]
    ranked: dict[str, int] = {}
    for attrs, inner in job_verifier._A_TAG.findall(page.html[:MAX_LISTING_BYTES])[:5000]:
        got = {k.lower(): html_lib.unescape(a or b).strip() for k, a, b in job_verifier._ATTR.findall(attrs)}
        href = got.get("href", "")
        if not href or href.lower().startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        url = urljoin(page.url, href).split("#")[0]
        try:
            p = urlsplit(url)
        except ValueError:
            continue
        if p.scheme != "https" or site_key(p.hostname or "") != key or url == here or len(url) > MAX_URL_CHARS \
                or _BAD_CHARS.search(url) or never_fetched(url) or {s.lower() for s in p.path.split("/")} & _FORM_SEGMENTS:
            continue
        label = job_verifier.html_to_text(inner).strip() or got.get("aria-label", "")
        by_label = len(label) <= 40 and bool(_JOBS_LABEL.match(label))
        by_path = (p.path.rstrip("/").rsplit("/", 1)[-1].lower() in _JOBS_PATH) and p.path.count("/") <= 4
        if by_label or by_path:
            ranked.setdefault(url, 0)
            ranked[url] = max(ranked[url], by_label + by_path)
    return [u for u, _ in sorted(ranked.items(), key=lambda kv: -kv[1])][:3]


def listing_addresses(site: "Site") -> list[str]:
    """Where the site's list of openings may be, most likely first: the address the file named, the pages above the
    posting that led to the site, then the usual places. Three at most; with the links such a page itself points to
    (`jobs_links`), no more than LISTING_TRIES addresses are asked in one crawl."""
    base = site.base_url
    out = [site.listing_url] if site.listing_url else []
    if site.found_url:
        segments = [s for s in urlsplit(site.found_url).path.split("/") if s]
        for depth in range(len(segments) - 1, 0, -1):
            if not _idlike(segments[depth - 1]):
                out.append(f"{base}/" + "/".join(segments[:depth]))
    label = site.host.split(".")[0]
    if label in ("jobs", "careers", "career", "join", "work", "apply") or not out:
        out.append(base + "/")
    out += [base + "/careers", base + "/jobs"]
    return list(dict.fromkeys(out))[:3]


def _blocked(status: int, markup: str = "") -> bool:
    if status in (401, 403, 429):
        return True
    title = _TITLE.search(markup[:20000] if markup else "")
    return bool(title and _CHALLENGE.search(title.group(1))) or (status == 503 and bool(_CHALLENGE.search(markup[:4000])))


def in_united_states(job: Job) -> bool:
    """The engine's rule for "a US posting" (board_catalog.us_postings), and stricter where the page itself names the
    country: a posting whose JobPosting data states countries, none of them the United States, is not a US posting,
    whatever its place names look like. Seen live on 2026-11-21: "Bogota, Bogota,CO, CO" (Colombia) and "Goodwood,
    ON,CA, CA" (Canada) read as Colorado and California by the place rule alone."""
    hint = (job.country_hint or "").strip().lower()
    if hint and hint not in job_verifier._US:
        return False
    return bool(board_catalog.us_postings([job]))


# ---------------------------------------------------------------- records
@dataclass
class Site:
    host: str                         # the key: host name without "www."
    base_url: str                     # scheme and host as first seen: https://www.example.com
    employer: str = ""
    origin: str = "search"            # "search": a posting a search verified; "file": import_boards --career-sites
    found_url: str = ""               # search: the verified posting that led to the site
    listing_url: str = ""             # file: the page that lists the openings, when the file names one
    state: str = NEW
    misses: int = 0                   # crawls in a row that stored no US posting
    crawled_at: float | None = None
    seen: tuple[int, ...] = ()        # hashes of the pages earlier crawls read


@dataclass
class CrawlRecord:
    host: str
    started_at: float
    finished_at: float = 0.0
    method: str = ""                  # how posting addresses were found: sitemap / structured data / listing links / browser
    state: str = FAILED
    candidates: int = 0               # posting addresses found
    pages_seen: int = 0               # posting pages read in this crawl
    postings: int = 0                 # of those, pages that proved an open posting
    us_postings: int = 0              # of those, postings in the United States that were stored
    requests: int = 0                 # every request of this crawl: robots.txt, sitemaps, listing pages, posting pages, redirects
    browser_pages: int = 0
    list_steps: int = 0               # "next" / "load more" controls pressed on rendered listing pages (each is counted in `requests`)
    error: str = ""                   # why nothing (more) was read
    known: int = 0                    # candidates already in the index (kept current by the page reader, not read here)
    sitemaps: int = 0
    blocked: int = 0
    disallowed: int = 0
    throttled: bool = False           # the site answered 429: nothing more is asked of it in this crawl
    reasons: Counter = field(default_factory=Counter)        # why pages did not prove a posting (report only)
    read: list[int] = field(default_factory=list)            # hashes of the pages read
    stored: list[str] = field(default_factory=list)          # addresses of the postings stored (report only)


def page_hash(url: str) -> int:
    return int.from_bytes(hashlib.blake2b(url.encode("utf-8", "replace"), digest_size=8).digest(), "big", signed=True)


def next_wait(state: str, misses: int, crawl_days: float, max_days: float) -> float:
    """Seconds until a site is crawled again. Never sooner than CAREER_SITE_CRAWL_DAYS; `misses` counts the crawls in
    a row, this one included, that stored no US posting. A failed crawl is repeated after one interval, then two,
    four ...; a site without postings for the US after two, four, eight ...; a site that is not readable (403, robot
    check, robots.txt) or is not one company's site after 30 days, then 60 ...; nothing waits longer than `max_days`."""
    week = max(1.0, crawl_days) * 86400.0
    top = max(week, max_days * 86400.0)
    n = max(0, min(misses, 16))
    if state == OK or n == 0:
        return week
    if state == FAILED:
        return min(top, week * 2 ** (n - 1))
    if state in (NOT_READABLE, NOT_A_COMPANY):
        return min(top, max(week, NOT_READABLE_DAYS * 86400.0) * 2 ** (n - 1))
    return min(top, week * 2 ** n)


# ---------------------------------------------------------------- network
class _PacedBrowser:
    """The browser pool behind the same per-host gate as plain requests: one page view at a time, after the pause."""

    def __init__(self, pool, reader: "SiteReader"):
        self.pool, self.reader = pool, reader

    @property
    def available(self) -> bool:
        return bool(self.pool is not None and getattr(self.pool, "available", False))

    async def render(self, url: str):
        async with self.reader._slot(url):
            record = _CRAWL.get()
            if record is not None:
                record.browser_pages += 1
            return await self.pool.render(url)

    async def render_list(self, url: str, steps: int):
        """A listing page, followed through its own "Next" / "Load more" control up to `steps` times (browser_pool.
        render_list). The host's slot is held for the whole time, so no other request of this crawler goes to the site
        meanwhile; every press waits the host's pause and is counted as a request of the crawl."""
        paged = getattr(self.pool, "render_list", None)
        if steps <= 0 or paged is None:
            return await self.render(url)
        host = (urlsplit(url).hostname or "").lower()
        async with self.reader._slot(url):
            record = _CRAWL.get()
            if record is not None:
                record.browser_pages += 1
            result = await paged(url, steps, max(self.reader.host_gap, self.reader.gaps.get(host, 0.0)))
            if record is not None:
                record.requests += int(getattr(result, "steps", 0) or 0)
                record.list_steps += int(getattr(result, "steps", 0) or 0)
            return result


class SiteReader(PageVerifier):
    """The page verifier at a crawler's pace: one request at a time per host, at least `gap` seconds (or the host's
    Crawl-delay) between the end of one request and the start of the next. Every request of every kind passes
    `_slot`, which is also where a crawl's requests are counted."""

    def __init__(self, client: httpx.AsyncClient, browser=None, gap: float = 1.0, timeout: float = 20.0):
        super().__init__(client, browser=None, timeout=timeout, per_host=1, host_gap=max(1.0, gap))
        self.browser = _PacedBrowser(browser, self)
        self.gaps: dict[str, float] = {}                       # host -> seconds its robots.txt asks for, when more than `gap`
        self.extras: dict[str, tuple[list[str], float]] = {}   # origin -> (sitemaps, crawl-delay) of its robots.txt

    @asynccontextmanager
    async def _slot(self, url: str):
        host = (urlsplit(url).hostname or "").lower()
        loop = asyncio.get_running_loop()
        if self._hosts_loop is not loop:
            self._hosts, self._hosts_loop = {}, loop
        gate = self._hosts.get(host)
        if gate is None:
            if len(self._hosts) > 4000:
                self._hosts = {h: g for h, g in self._hosts.items() if g[0].locked()}
            gate = self._hosts[host] = [asyncio.Semaphore(1), 0.0]
        async with gate[0]:
            wait = gate[1] - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            record = _CRAWL.get()
            if record is not None:
                record.requests += 1
            try:
                yield
            finally:
                gate[1] = loop.time() + max(self.host_gap, self.gaps.get(host, 0.0))

    async def _robots_fetch(self, url: str) -> tuple[int, str]:
        status, _, text = await self._fetch(url)
        if status == 200:
            for old in list(self.extras)[: max(0, len(self.extras) - 4000)]:
                del self.extras[old]
            self.extras[url[: -len("/robots.txt")]] = robots_extras(text)
        return status, text

    async def fetch_bytes(self, url: str, limit: int = MAX_SITEMAP_BYTES, accept: str = "application/xml,text/xml,text/plain,*/*;q=0.5",
                          within: str = "", only: str = "") -> tuple[int, str, bytes, str]:
        """GET (status, final address, body, charset) under the rules of a page fetch: a host that is never fetched is
        refused, the SSRF guard checks every hop and the request goes to the checked address, the robots.txt of every
        host on the way must allow the address, redirects are followed by hand (five at most), the body is bounded.
        With `within` (a site key) a redirect that leaves the site is not followed: its status comes back with no body;
        with `only` the redirect must stay on that very host ("www." aside), not just in its domain.
        Raises job_verifier._RobotsDenied, UnsafeURL or an httpx error."""
        current = url
        for _ in range(5):
            if never_fetched(current):
                raise job_verifier._RobotsDenied("Source does not permit automated access")
            address = await job_verifier.assert_public_url(current)            # looked up at call time (tests replace it)
            if not await self.robots.allowed(self.client, current):
                rules = self.robots.peek(current)
                raise job_verifier._RobotsDenied("robots.txt could not be read" if rules is not None and rules.transient
                                                 else "Disallowed by robots.txt")
            target, extra, extensions = pinned(current, address) if address else (current, {}, {})
            async with self._slot(current), \
                    self.client.stream("GET", target, timeout=self.timeout, follow_redirects=False, extensions=extensions,
                                       headers={"User-Agent": USER_AGENT, "Accept": accept, **extra}) as r:
                location = r.headers.get("location") if r.status_code in (301, 302, 303, 307, 308) else None
                if not location:
                    chunks, size = [], 0
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        chunks.append(chunk)
                        if size > limit:
                            break
                    return r.status_code, current, b"".join(chunks)[:limit], r.encoding or "utf-8"
                status = r.status_code
            target = urljoin(current, location)
            host = urlsplit(target).hostname or ""
            if (within and not same_site(within, host)) or (only and site_key(host) != only):
                return status, current, b"", "utf-8"            # the address moved to another site: that site is not this crawl's
            current = target
        raise UnsafeURL("Too many redirects")

    async def fetch_page(self, url: str, within: str = "") -> tuple[int, Page | None]:
        """One listing page by a plain request: (status, page). Status 0 = could not be fetched, -1 = robots.txt or
        the network guard refused the address. A redirect to another host than `within` is not followed: postings
        are only ever read on the site's own host, so a listing elsewhere has nothing for this crawl."""
        try:
            status, final, body, charset = await self.fetch_bytes(url, MAX_LISTING_BYTES, "text/html,*/*;q=0.5", only=within)
        except (job_verifier._RobotsDenied, UnsafeURL):
            return -1, None
        except (httpx.HTTPError, asyncio.TimeoutError):
            return 0, None
        try:
            markup = body.decode(charset, "replace")
        except LookupError:
            markup = body.decode("utf-8", "replace")
        return status, Page(final, status, markup, page_text(markup) if status == 200 else "", requested=url if final != url else "")


# ---------------------------------------------------------------- the crawler
class Crawler:
    def __init__(self, store, reader: SiteReader, remember: Callable[[list[Job]], Awaitable[int]], settings,
                 clock: Callable[[], float] = time.time, spawn: Callable[[asyncio.Task], None] | None = None,
                 busy: Callable[[], bool] | None = None):
        self.store = store
        self.busy = busy                          # True while searches keep the engine busy: a running crawl reads no page then
        self.reader = reader
        self.remember = remember                  # search_engine._remember_pages: stores a posting as a `career_page` unit
        self.enabled = bool(getattr(settings, "career_site_crawl", True))
        self.crawl_days = float(getattr(settings, "career_site_crawl_days", 7.0))
        self.max_days = float(getattr(settings, "career_site_max_backoff_days", 90.0))
        self.per_crawl = max(0, int(getattr(settings, "career_site_pages_per_crawl", 150)))
        self.per_day = max(0, int(getattr(settings, "career_site_pages_per_day", 2000)))
        self.max_sitemaps = max(1, int(getattr(settings, "career_site_sitemaps_per_crawl", 12)))
        self.browser_pages = max(0, int(getattr(settings, "career_site_browser_pages", 4)))
        self.list_steps = max(0, int(getattr(settings, "career_site_list_steps", 10)))
        self.concurrency = max(1, int(getattr(settings, "career_site_concurrency", 2)))
        self.seconds = max(30.0, float(getattr(settings, "career_site_crawl_seconds", 1200.0)))
        self.aggregators = aggregator_hosts(getattr(settings, "aggregator_hosts", ""))
        self.clock = clock
        self._spawned = spawn
        self._running: dict[asyncio.Task, int] = {}       # crawl -> pages of the day's budget it may use
        self._noted: set[str] = set()                     # hosts already looked at by note_posting (kept or refused)
        self._warned = False

    @classmethod
    def for_engine(cls, engine) -> "Crawler | None":
        """The crawler of a search engine, or None where there is nothing to crawl with: the index is not the
        PostgreSQL one (the single-user SQLite app has no background reader for remembered pages either), or
        company-site postings are not kept at all (CAREER_PAGE_MEMORY=false, or the source is switched off)."""
        from .providers.registry import CAREER_PAGE
        if not hasattr(engine.index, "catalog_due") or CAREER_PAGE not in engine.specs:
            return None
        settings = engine.settings
        reader = SiteReader(engine.client, engine.browser, gap=float(getattr(settings, "career_site_request_gap_seconds", 1.0)))

        def spawn(task: asyncio.Task) -> None:            # cancelled with the engine's other background work
            engine._background.add(task)
            task.add_done_callback(engine._background.discard)
        return cls(PgStore(engine.index), reader, engine._remember_pages, settings, spawn=spawn,
                   busy=lambda: len(engine._inflight) > board_catalog.MAX_INFLIGHT)

    def _warn(self, what: str, exc: Exception) -> None:
        if not self._warned:
            self._warned = True
            log.warning("career sites: %s failed (%s); is the migration applied?", what, type(exc).__name__)

    # ------------------------------------------------------------ site records
    async def note_posting(self, job: Job) -> bool:
        """Engine hook: a posting was verified on a page and kept. Remember its site when the host is an employer's
        own. One INSERT for a host seen for the first time, nothing otherwise; never raises. True when the site is new."""
        try:
            p = urlsplit(job.url or "")
            key = site_key(p.hostname or "")
            if not key or key in self._noted:
                return False
            if len(self._noted) > 50000:
                self._noted.clear()
            new = False
            if p.scheme == "https" and p.port in (None, 443) and not refusal(key, self.aggregators):
                new = await self.store.register(key, f"https://{p.hostname.lower()}", (job.company or "").strip()[:200], "search",
                                                p._replace(fragment="").geturl()[:MAX_URL_CHARS])
            self._noted.add(key)                      # kept or refused: this host is not looked at again by this process
            return new
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn("remembering a site", exc)
            return False

    # ------------------------------------------------------------ schedule
    async def tick(self) -> int:
        """Background hook, once a minute: start crawling the sites that are due, while fewer than
        CAREER_SITE_CONCURRENCY crawls run and the day's page budget is not used up. Returns how many were started."""
        if not self.enabled or self.per_crawl <= 0:
            return 0
        try:
            for task in [t for t in self._running if t.done()]:
                self._running.pop(task, None)
            free = self.concurrency - len(self._running)
            if free <= 0:
                return 0
            now = self.clock()
            left = self.per_day - await self.store.pages_since(now - 86400.0) - sum(self._running.values())
            least = min(MIN_BUDGET, self.per_crawl)
            budgets = []
            while len(budgets) < free and left >= least:   # every crawl gets its pages of the day's budget before it starts
                budgets.append(min(self.per_crawl, left))
                left -= budgets[-1]
            if not budgets:
                return 0
            # Claiming a site moves its next crawl one full interval ahead at once: whatever happens to this crawl
            # (a restart, a crash), the site is not asked again within the interval.
            sites = await self.store.claim(now, len(budgets), self.crawl_days * 86400.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn("the schedule", exc)
            return 0
        for site, budget in zip(sites, budgets):
            task = asyncio.create_task(self.run(site, budget))
            self._running[task] = budget
            if self._spawned:
                self._spawned(task)
        return len(sites)

    async def run(self, site: Site, budget: int) -> CrawlRecord:
        """Crawl one site and record how it went. Never raises (except on cancellation)."""
        record = await self.crawl(site, budget)
        misses = 0 if record.state == OK else site.misses + 1
        wait = next_wait(record.state, misses, self.crawl_days, self.max_days)
        seen = tuple(dict.fromkeys([*site.seen, *record.read]))[-MAX_SEEN:]
        try:
            await self.store.finish(site.host, record, record.started_at + wait, misses, seen)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn("recording a crawl", exc)
        return record

    # ------------------------------------------------------------ one crawl
    async def crawl(self, site: Site, budget: int) -> CrawlRecord:
        record = CrawlRecord(host=site.host, started_at=self.clock())
        token = _CRAWL.set(record)
        try:
            await asyncio.wait_for(self._crawl(site, budget, record), timeout=self.seconds * 2 + 120)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            record.error = record.error or "the crawl did not finish in time"
            record.state = OK if record.us_postings else FAILED
        except Exception as exc:
            record.state, record.error = (OK if record.us_postings else FAILED), f"{type(exc).__name__}"
        finally:
            _CRAWL.reset(token)
        record.finished_at = self.clock()
        return record

    def _stop(self, record: CrawlRecord, state: str, error: str) -> None:
        record.state, record.error = state, error

    async def _crawl(self, site: Site, budget: int, record: CrawlRecord) -> None:
        reader, base = self.reader, site.base_url
        why = refusal(site.host, self.aggregators)
        if why or not base.startswith("https://") or site_key(urlsplit(base).hostname or "") != site.host:
            return self._stop(record, NOT_READABLE, why or "not an https site")
        try:
            await job_verifier.assert_public_url(base + "/")
        except UnsafeURL as exc:
            return self._stop(record, FAILED, f"URL rejected: {exc}")
        rules = await reader.robots.rules(reader.client, base + "/")
        if rules.transient:
            return self._stop(record, FAILED, "robots.txt could not be read")
        sitemaps, delay = reader.extras.get(base, ([], 0.0))
        if delay > MAX_CRAWL_DELAY:
            return self._stop(record, NOT_READABLE, f"robots.txt asks for {delay:g} s between requests")
        if delay > reader.host_gap:
            for host in {urlsplit(base).hostname, site.host, "www." + site.host}:
                reader.gaps[host] = delay
        deadline = time.monotonic() + self.seconds
        known = await self.store.known_pages(site.host)
        addresses = Addresses(site.host, [site.found_url, *sorted(known)[:200]], self.aggregators)

        last = iso_date(site.crawled_at) if site.crawled_at else ""
        seen = set(site.seen)
        every: dict[str, str] = {}

        async def from_sitemaps():
            return await self._from_sitemaps(site, sitemaps, addresses, record), SITEMAP

        for stage in (from_sitemaps, lambda: self._from_listing(site, addresses, record)):
            if record.throttled:
                break
            found, how = await stage()
            found = {u: m for u, m in found.items() if u not in every}
            if not found:
                continue
            every.update(found)
            record.method = record.method or how
            fresh = []
            for url, mod in found.items():
                if url in known:
                    record.known += 1                  # in the index already: the page reader keeps it current
                elif page_hash(url) not in seen or (mod and last and mod > last):
                    fresh.append((url, mod))
            # Postings shaped like one already verified here first, then addresses that say "United States" before
            # those that name another country, then the newest, then addresses that carry a number. Among equals the
            # order is that of a hash: a sitemap without dates is then read across the whole site, not from "A" onwards.
            fresh.sort(key=lambda pair: (not addresses.like_known(pair[0]), -us_hint(pair[0]), -int(pair[1].replace("-", "") or 0),
                                         not re.search(r"\d{4,}", pair[0]), page_hash(pair[0])))
            before, record.error = record.postings, ""
            await self._read(site, fresh, budget, record, known, deadline)
            if record.postings > before:
                record.method = how
            if record.postings or record.known or record.pages_seen >= budget or time.monotonic() > deadline \
                    or record.state in (NOT_READABLE, FAILED, NEEDS_BROWSER, NOT_A_COMPANY):
                break
            # What this stage named proved no posting (a sitemap of marketing pages, say): the next stage looks further.
        record.candidates = len(every)
        if record.throttled and not record.us_postings:
            return self._stop(record, FAILED, "HTTP 429: the site asked to slow down; the crawl stopped")
        if not every:
            if record.blocked:
                return self._stop(record, NOT_READABLE, "the site answered HTTP 401 / 403 / 429 or a robot check (left alone)")
            if record.disallowed:
                return self._stop(record, NOT_READABLE, "robots.txt disallows the site's sitemaps and listing pages")
            return self._stop(record, NO_POSTINGS, "no posting addresses found (sitemaps, listing page"
                              + (", browser)" if record.browser_pages else ")"))

    # ------------------------------------------------------------ (a) sitemaps
    async def _from_sitemaps(self, site: Site, declared: list[str], addresses: Addresses, record: CrawlRecord) -> dict[str, str]:
        base = site.base_url
        declared = sorted(dict.fromkeys(declared), key=lambda u: not _JOBISH.search(u.split("://", 1)[-1].partition("/")[2]))      # job-named first
        queue: list[tuple[int, str]] = [(0, u) for u in declared] or [(0, base + "/sitemap.xml")]
        asked: set[str] = set()
        found: dict[str, str] = {}
        while queue and record.sitemaps < self.max_sitemaps and len(found) < MAX_CANDIDATES:
            depth, url = queue.pop(0)
            try:
                p = urlsplit(url)
                host = site_key(p.hostname or "")
            except ValueError:
                continue
            if url in asked or p.scheme != "https" or not same_site(site.host, host) or len(url) > MAX_URL_CHARS or _BAD_CHARS.search(url):
                continue
            asked.add(url)
            record.sitemaps += 1
            try:
                status, _final, body, _ = await self.reader.fetch_bytes(url, within=site.host)
            except job_verifier._RobotsDenied:
                record.disallowed += 1
                continue
            except (UnsafeURL, httpx.HTTPError, asyncio.TimeoutError):
                continue
            if status != 200:
                record.blocked += _blocked(status)
                if status == 429:                      # asked to slow down: nothing more is asked of this site now
                    record.throttled = True
                    break
                continue
            kind, entries = await asyncio.to_thread(parse_sitemap, body)
            if kind == "index" and depth < 2:
                children = [(loc, mod) for loc, mod in entries[:MAX_SITEMAP_CHILDREN] if loc not in asked]
                jobish = [c for c in children if _JOBISH.search(urlsplit(c[0]).path)]
                # Sitemaps named after jobs first and, when there are any, only those; the newest first either way.
                chosen = sorted(jobish or children, key=lambda c: c[1], reverse=True)
                queue = [*((depth + 1, loc) for loc, _ in chosen), *queue]
            elif kind == "urlset":
                def pick() -> dict[str, str]:
                    out: dict[str, str] = {}
                    for loc, mod in entries:
                        clean = addresses.clean(loc)
                        if clean and (clean not in out or mod > out[clean]):
                            out[clean] = mod
                    return out
                for clean, mod in (await asyncio.to_thread(pick)).items():
                    if len(found) >= MAX_CANDIDATES:
                        break
                    if mod > found.get(clean, "") or clean not in found:
                        found[clean] = mod
        return found

    # ------------------------------------------------------------ (b) listing page, (c) browser
    def _candidates(self, page: Page, addresses: Addresses) -> tuple[dict[str, str], str]:
        data, links = page_addresses(page)
        in_data = {c: "" for c in (addresses.clean(u) for u in data) if c}
        in_links = {c: "" for c in (addresses.clean(u) for u in links) if c}
        only_links = [u for u in in_links if u not in in_data]
        return {**in_data, **in_links}, (LISTING_LINKS if only_links and len(only_links) >= len(in_data) else LISTING_DATA)

    async def _from_listing(self, site: Site, addresses: Addresses, record: CrawlRecord) -> tuple[dict[str, str], str]:
        """(posting addresses, how they were found) from the site's listing page: by plain requests first, and only
        when those named nothing, in the browser."""
        reader = self.reader
        queue = listing_addresses(site)
        asked: set[str] = set()
        answered: list[str] = []                       # listing addresses that answered: the browser looks at the best of them
        hinted: list[str] = []                         # ... which is one a career page itself pointed to, when there is one
        tries = 0
        while queue and tries < LISTING_TRIES:
            url = queue.pop(0)
            if url in asked or site_key(urlsplit(url).hostname or "") != site.host:
                continue
            asked.add(url)
            tries += 1
            status, page = await reader.fetch_page(url, site.host)
            if status == -1:
                record.disallowed += 1
                continue
            if _blocked(status, page.html if page is not None else ""):
                record.blocked += 1                    # 401 / 403 / 429 or a robot check: the site's pages are left alone
                record.throttled = record.throttled or status == 429
                return {}, ""
            if page is None or status != 200:
                continue
            asked.add(page.url)
            answered.append(page.url)
            found, how = self._candidates(page, addresses)
            pages = 1
            while found and pages < LISTING_PAGES:      # "next" / "load more" where it is a plain link
                more = [u for u in next_pages(page, site.host) if u not in asked]
                if not more:
                    break
                asked.add(more[0])
                status, nxt = await reader.fetch_page(more[0], site.host)
                if nxt is None or status != 200 or _blocked(status, nxt.html):
                    break
                extra, _ = self._candidates(nxt, addresses)
                if not set(extra) - set(found):
                    break
                found.update(extra)
                page, pages = nxt, pages + 1
            if found:
                return found, how
            # The page named no posting, but it may say where the openings are ("View all jobs"): that comes next.
            hints = [u for u in jobs_links(page, site.host) if u not in asked and u not in queue][:2]
            hinted += [u for u in hints if u not in hinted]
            queue = [*hints, *queue]
        if not answered or not reader.browser.available or record.browser_pages >= self.browser_pages:
            return {}, ""
        # (c) The listing shows its jobs only with scripts: look at it in the browser. It reads; the one thing it may press is the list's own paging button.
        found: dict[str, str] = {}
        todo = [*[u for u in answered if u in hinted][-1:], *answered[:1]]
        rendered: set[str] = set()
        while todo and record.browser_pages < self.browser_pages:
            url = todo.pop(0)
            if url in rendered:
                continue
            rendered.add(url)
            if not await reader.robots.allowed(reader.client, url):
                record.disallowed += 1
                continue
            # A list built by script often shows its first part only (20 of 2,408 jobs on one site read on 2026-11-21):
            # its own "Next" / "Load more" control is pressed up to CAREER_SITE_LIST_STEPS times (0 = never).
            result = await reader.browser.render_list(url, self.list_steps)
            if result.error:
                record.blocked += result.error == "blocked"
                break
            final = result.url or url
            if site_key(urlsplit(final).hostname or "") != site.host:
                continue
            rendered.add(final)
            page = Page(final, result.status or 200, result.html, result.text or page_text(result.html), rendered=True)
            extra, _ = self._candidates(page, addresses)
            for link in getattr(result, "links", ()) or ():          # the further parts of the list: each address is judged like any other
                clean = addresses.clean(link)
                if clean:
                    extra.setdefault(clean, "")
            new = set(extra) - set(found)
            found.update(extra)
            if new:
                todo = next_pages(page, site.host)[:1]                      # "next" / "load more" where it is a plain link
            elif not found:
                todo = [*jobs_links(page, site.host)[:1], *todo]            # a career home page: its "all jobs" link
        return found, (BROWSER if found else "")

    # ------------------------------------------------------------ reading the postings
    async def _read(self, site: Site, fresh: list[tuple[str, str]], budget: int, record: CrawlRecord, known: set[str],
                    deadline: float) -> None:
        reader = self.reader
        tries: Counter = Counter()
        proved: Counter = Counter()
        in_us: Counter = Counter()
        bare: Counter = Counter()                       # pages of a shape that carried no job data in the first response
        employers: Counter = Counter()
        refused = 0
        probe = Budget(1 if self.browser_pages > 0 and reader.browser.available else 0)
        stop = ("", "")
        for url, _mod in fresh:
            if record.pages_seen >= budget:
                break
            while self.busy is not None and self.busy() and time.monotonic() <= deadline:
                await asyncio.sleep(BUSY_PAUSE)          # the engine is reading many boards for searches: the crawl stands back
            if time.monotonic() > deadline:
                record.error = "time limit of one crawl reached"
                break
            form = shape(url)
            form = form if "*" in form or "?" in form else _parent(url)        # addresses without an id are judged with their neighbours
            probing = False
            if (tries[form] >= SHAPE_TRIES and not proved[form]) or (tries[form] >= SHAPE_US_TRIES and not in_us[form]):
                # Addresses of this shape have not been postings (for the US) so far: no more of them are read. When
                # nearly all of them lacked job data, one more is looked at with the browser allowed, only to learn
                # whether this site's postings need a browser (they are then not kept, see the module text).
                probing = (probe.left > 0 and not record.postings and tries[form] < SHAPE_TRIES + 2
                           and bare[form] >= SHAPE_TRIES - 1 and record.browser_pages < self.browser_pages)
                if not probing:
                    continue
            if not await reader.robots.allowed(reader.client, url):
                record.disallowed += 1
                continue
            verdict = await reader.verify(Candidate(url=url, provider=CAREER_PAGE_SOURCE), probe if probing else None)
            record.pages_seen += 1
            record.read.append(page_hash(url))
            tries[form] += 1
            job = verdict.job
            if job is not None and "schema.org JobPosting" in (job.verification or ""):
                if not (job.verification or "").startswith("Posting page fetched"):
                    stop = (NEEDS_BROWSER, "posting pages show their job data only in a browser; they are not kept")
                    break
                if site_key(urlsplit(job.url or "").hostname or "") != site.host:
                    record.reasons["Posting address leads to another host (not this site's)"] += 1
                    continue
                record.postings += 1
                proved[form] += 1
                employers[norm_company(job.company)] += 1
                if sum(employers.values()) >= 6 and len(employers) >= 4 and max(employers.values()) * 2 < sum(employers.values()):
                    stop = (NOT_A_COMPANY, f"its postings name {len(employers)} different employers: a job board, not one company's site")
                    break
                if in_united_states(job):
                    in_us[form] += 1
                    if await self.remember([job]):
                        record.us_postings += 1
                        record.stored.append(job.url)
                        known.add(job.url)
                continue
            reason = verdict.reason or "not verified"
            record.reasons[re.sub(r"\d{4}-\d{2}-\d{2}", "DATE", reason)[:80]] += 1
            if reason.startswith("Page returned HTTP 429"):
                record.throttled = True
                stop = (FAILED, "HTTP 429: the site asked to slow down; the crawl stopped")
                break
            if re.match(r"Page returned HTTP (401|403)", reason) or "blocks automated browsers" in reason:
                refused += 1
                if refused >= 3 and not record.postings:
                    record.blocked += refused
                    stop = (NOT_READABLE, "posting pages answered HTTP 401 / 403 (left alone)")
                    break
            elif reason.startswith(("No structured job data on page", "Page lacks a job title or employer")):
                bare[form] += 1
        if stop[0]:
            record.state, record.error = stop
            if record.us_postings and stop[0] not in (NOT_A_COMPANY,):
                record.state = OK
            return
        if refused and refused == record.pages_seen and not record.postings:
            record.blocked += refused
            record.state, record.error = NOT_READABLE, "posting pages answered HTTP 401 / 403 (left alone)"
        elif record.us_postings or (known and not record.pages_seen):
            record.state = OK                           # new US postings, or nothing new on a site whose postings are in the index
        elif record.postings:
            record.state, record.error = NO_US, record.error or f"{record.postings} posting(s) verified, none in the United States"
        elif record.pages_seen:
            without = sum(bare.values())
            why = (f"{without} of {record.pages_seen} posting pages carry no JobPosting data in the first response"
                   + (" (nor in a browser)" if probe.used else "")) if without * 2 >= record.pages_seen else "no page proved an open posting"
            record.state, record.error = NO_US, record.error or why
        else:
            record.state, record.error = NO_US, record.error or "no new posting addresses since the last crawl"


# ---------------------------------------------------------------- storage (PostgreSQL)
class PgStore:
    """Site records and crawl history in the job index database (tables of migration a8c5e3f1d2b7). `index` is the
    PgJobIndex; its pool is looked up at call time."""

    COLUMNS = "host, base_url, employer, origin, found_url, listing_url, state, misses, crawled_at, seen"

    def __init__(self, index):
        self.index = index

    @property
    def pool(self):
        return self.index.pool

    @staticmethod
    def _site(row) -> Site:
        return Site(host=row["host"], base_url=row["base_url"], employer=row["employer"], origin=row["origin"],
                    found_url=row["found_url"], listing_url=row["listing_url"], state=row["state"], misses=row["misses"],
                    crawled_at=row["crawled_at"], seen=tuple(row["seen"] or ()))

    async def register(self, host: str, base_url: str, employer: str = "", origin: str = "search", found_url: str = "",
                       listing_url: str = "") -> bool:
        """Remember a site; an existing record is left alone. A new site is due at once. True when it was new."""
        now = time.time()
        result = await self.pool.execute(
            "INSERT INTO index_career_sites (host, base_url, employer, origin, found_url, listing_url, created_at, next_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$7) ON CONFLICT (host) DO NOTHING", host, base_url, employer, origin, found_url, listing_url, now)
        return result.split()[-1] == "1"

    async def claim(self, now: float, limit: int, hold: float) -> list[Site]:
        """Up to `limit` sites that are due, the longest waiting first. Each is marked as started and not due again
        for `hold` seconds in the same statement, so two processes never crawl one site and a crawl that dies does
        not repeat."""
        if limit <= 0:
            return []
        rows = await self.pool.fetch(
            "UPDATE index_career_sites s SET tried_at = $1::float8, next_at = $1::float8 + $3::float8 FROM ("
            "SELECT host FROM index_career_sites WHERE next_at <= $1::float8 ORDER BY next_at, host LIMIT $2::int FOR UPDATE SKIP LOCKED) d "
            f"WHERE s.host = d.host RETURNING {', '.join('s.' + c for c in self.COLUMNS.split(', '))}", now, limit, hold)
        return [self._site(r) for r in rows]

    async def finish(self, host: str, record: CrawlRecord, next_at: float, misses: int, seen: tuple[int, ...]) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE index_career_sites SET state=$2, method=$3, crawled_at=$4, next_at=$5, misses=$6, candidates=$7, "
                    "pages_seen=$8, postings=$9, us_postings=$10, error=$11, seen=$12::bigint[] WHERE host=$1",
                    host, record.state, record.method, record.finished_at, next_at, misses, record.candidates, record.pages_seen,
                    record.postings, record.us_postings, record.error[:300], list(seen))
                await conn.execute(
                    "INSERT INTO index_career_crawls (host, started_at, finished_at, method, state, candidates, pages_seen, postings, "
                    "us_postings, requests, browser_pages, error) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
                    host, record.started_at, record.finished_at, record.method, record.state, record.candidates, record.pages_seen,
                    record.postings, record.us_postings, record.requests, record.browser_pages, record.error[:300])
                await conn.execute("DELETE FROM index_career_crawls WHERE started_at < $1", record.started_at - CRAWL_LOG_DAYS * 86400.0)

    async def pages_since(self, since: float) -> int:
        return int(await self.pool.fetchval("SELECT coalesce(sum(pages_seen), 0) FROM index_career_crawls WHERE started_at > $1", since))

    async def known_pages(self, host: str) -> set[str]:
        """Addresses on this site that are already kept as `career_page` units (alive or gone)."""
        like = host.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        rows = await self.pool.fetch("SELECT slug FROM index_boards WHERE ats = 'career_page' AND (slug LIKE $1 OR slug LIKE $2)",
                                     f"https://{like}/%", f"https://www.{like}/%")
        return {r["slug"] for r in rows}


SITE_STATS_SQL = ("SELECT count(*), count(*) FILTER (WHERE crawled_at > :since), count(*) FILTER (WHERE state = 'ok'), "
                  "count(*) FILTER (WHERE state = 'not_readable') FROM index_career_sites")
PAGE_STATS_SQL = "SELECT count(*) FROM index_boards WHERE ats = 'career_page' AND ok AND job_count > 0"


# ---------------------------------------------------------------- seeding from a file
def read_seed_file(path, aggregators: tuple[str, ...] = ()) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """Rows of a seed file as ([(key, base address, employer, listing address)], [why a row was refused]).
    The file is CSV with a header: `host` (or `url`), `employer`, and optionally `listing` (the page that lists the
    openings, on the same host). Every host is checked here by its form and against the hosts that are never a
    company site; whether it resolves to a public address is checked by the importer."""
    import csv
    rows, errors, taken = [], [], set()
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        for n, row in enumerate(csv.DictReader(fh), start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if isinstance(v, str) or v is None}
            raw = row.get("host") or row.get("url") or ""
            if not raw or raw.startswith("#"):
                continue
            if "://" in raw:
                try:
                    p = urlsplit(raw)
                    name, scheme, port = p.hostname or "", p.scheme, p.port
                except ValueError:
                    name, scheme, port = "", "", None
            else:
                name, scheme, port = raw.split("/")[0], "https", None
            key = site_key(name) if ":" not in name else ""
            why = "only https sites are crawled" if scheme != "https" or port not in (None, 443) else refusal(name, aggregators)
            if why or not key:
                errors.append(f"line {n}: {raw[:80]}: {why or 'not a public host name'}")
                continue
            if key in taken:
                errors.append(f"line {n}: {raw[:80]}: listed twice")
                continue
            listing = row.get("listing") or ""
            base = f"https://{name.strip().lower().rstrip('.')}"
            if listing.startswith("/"):
                listing = base + listing
            if listing:
                try:
                    lp = urlsplit(listing)
                    fits = lp.scheme == "https" and site_key(lp.hostname or "") == key and len(listing) <= MAX_URL_CHARS and not _BAD_CHARS.search(listing)
                except ValueError:
                    fits = False
                if not fits:
                    errors.append(f"line {n}: {raw[:80]}: the listing address is not an https address on that host")
                    continue
            taken.add(key)
            rows.append((key, base, (row.get("employer") or row.get("name") or "")[:200], listing))
    return rows, errors
