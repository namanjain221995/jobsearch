"""Verification of job leads that did not come straight from a source API.

A lead is accepted only when its page proves ONE open posting: schema.org JobPosting data (JSON-LD or microdata), or
page content that states the job, and no sign that the posting is closed, a list or somewhere else. HTTP 200 alone
never counts, and nothing a lead claims (title, employer, location) is shown unless the page says the same.

Order of work, cheapest evidence first:
1. plain HTTP fetch and structured data (fast, and the only path that also gives posting date, country and pay);
2. headless browser render, only when the plain page loaded but needs JavaScript to show the job. Platforms whose
   pages arrive complete in the first response are not rendered (providers/ats_pages.server_rendered);
3. AI extraction (optional, batched by the engine) for pages with no structured data. Whatever the model returns
   must appear verbatim in the page text, the title must be the page's own heading, and every veto below still
   applies: the model's "yes" is necessary, never sufficient.

Vetoes that no claim and no model answer can override (each one was seen on a live page on 2026-11-21, see
tests/fixtures/pages/eval_set.json): closed or expired wording, a stated closing date in the past, `validThrough` in
the past, a posting date more than five years old, an address that was redirected to a page without the posting's
id, an address of a known career platform that is not a posting address, a list or search address, and a page that
carries several JobPosting records.

All fetches pass the SSRF guard and robots.txt (every redirect target included). Sites that block automated access
are not worked around; job boards and career platforms that forbid automated reading are never requested at all
(never_fetched). No host gets more than two requests at a time."""
import asyncio
import html as html_lib
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from . import ats_platforms
from .job_normalizer import (analyze_authorization, detect_work_mode, extract_experience, extract_salary,
                             html_to_text, parse_ats)
from .job_ranker import best_title_relevance
from .netguard import UnsafeURL, assert_public_url, pinned  # noqa: F401  (assert_public_url is looked up on this module at call time)
from .providers import ats_pages
from .providers.base import USER_AGENT, RobotsCache, iso_date, now_iso
from .schemas import Candidate, Job

MAX_PAGE_BYTES = 1_500_000
STALE_POSTING_DAYS = 5 * 365        # a stated posting date older than this is not an open requisition (see _job_from_posting)
THIN_TEXT_CHARS = 400               # less readable text than this: the page is a script shell
EXCERPT_CHARS = 2500                # page text per page handed to the AI check (its answer time grows with the text)
# Sites that forbid automated access or require login: never fetched, leads from them are not verifiable here.
BLOCKED_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "monster.com",
                 "dice.com", "simplyhired.com", "careerbuilder.com", "facebook.com", "x.com", "twitter.com")
# Closed wording, as statements about the posting ("This job is no longer available."), not as words that also occur
# in open postings ("until the position is filled", "products that are no longer available"). Every form below was
# read on a live page on 2026-11-21 or is a plain variant of one (tests/test_page_verifier.py lists the sentences).
_NOUN = r"(?:job|position|posting|role|opening|requisition|vacancy|opportunity|listing|page)"
_SUBJECT = (rf"(?:(?:this|the|that)\s+)?{_NOUN}(?:\s+(?:posting|listing|opening|description|"
            r"you(?: are|'re| were)? (?:looking for|trying to (?:apply for|reach|view)|requested)))?")
_START = r"(?:^|[.!?…:|]\s+)"
_CLOSED = re.compile("|".join((
    rf"{_SUBJECT}\s+(?:is|was|has been|are)\s+no longer\s+(?:accepting|available|open|active|posted|listed|online|taking)",
    rf"{_SUBJECT}\s+no longer\s+exists?",
    rf"{_SUBJECT}\s+(?:does not|doesn't|did not)\s+exist",
    r"no longer\s+(?:accepting|taking)\s+(?:applications|applicants|candidates|resumes|submissions)",
    r"not\s+(?:currently\s+)?accepting\s+(?:applications|applicants|candidates)(?!\s+(?:from|via|through|by)\b)",
    rf"{_SUBJECT}\s+(?:has been|has|is|was)\s+(?:expired|closed|filled|removed|deleted|taken down|unpublished|withdrawn)\b",
    r"applications?\s+(?:are|is|have|has)\s+(?:now\s+)?closed",
    rf"{_SUBJECT}\s+(?:(?:is|was)\s+not\s+found|(?:could not|couldn't|cannot|can't)\s+be\s+found)",
    rf"{_SUBJECT}\s+(?:is|was)\s+(?:currently\s+)?(?:not available|unavailable|not (?:currently\s+)?active|inactive)\b",
    rf"{_START}(?:error:?\s+)?(?:404[\s.:-]*)?(?:job|page|position|posting)\s+not\s+found",
    rf"{_START}(?:job\s+)?(?:posting|listing|requisition|vacancy)\s+(?:closed|expired|filled)\s*(?:[.!]|$)",
    r"sorry,?\s+(?:but\s+)?(?:this|the|that)\s+(?:job|position|role|posting)",
)), re.I)
# ... unless the sentence only says what will happen: "until the position is filled", "if this role is no longer available".
_CONDITIONAL = re.compile(r"\b(?:until|once|when|whenever|if|after|before|unless|as soon as|whether|should|in case|while)\b[^.!?]*$", re.I)
_MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
# "Closed : January 7, 2026", "Expired Saturday, 7 March 2026", "Posting Close Date . November 30 , 2026": a stated end date.
_END_DATE = re.compile(
    r"\b(?:closed|closes|clos(?:e|ing) date|expired|expires|expiry date|application deadline|deadline(?: day)?|apply by|"
    r"applications? close[sd]?)\s*[.:\-–]?\s*(?:on\s+)?(?:[A-Za-z]{3,6}day,?\s+)?"
    rf"((?:{_MONTHS})\.? \d{{1,2}}\s?(?:st|nd|rd|th)?\s?,? \d{{4}}|\d{{1,2}}\s?(?:st|nd|rd|th)? (?:{_MONTHS})\.?,? \d{{4}}|\d{{4}}-\d{{2}}-\d{{2}})", re.I)
_JSONLD = re.compile(r"<script\b[^>]*\btype\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>", re.I | re.S)
_MICRODATA = re.compile(r"itemtype\s*=\s*[\"']https?://schema\.org/JobPosting[\"']", re.I)
_ANCHOR = re.compile(r"(?is)<a\b[^>]*?href\s*=\s*[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>")
_SITE_NAME = re.compile(r"(?is)<meta[^>]+property=[\"']og:site_name[\"'][^>]+content=[\"']([^\"']{2,80})[\"']")
_META_TITLE = re.compile(r"(?is)<meta\b[^>]*\b(?:property|name)\s*=\s*[\"'](?:og:title|twitter:title)[\"'][^>]*>")
_CONTENT = re.compile(r"(?is)\bcontent\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_HEADING = re.compile(r"(?is)<(title|h1|h2)\b[^>]*>(.*?)</\1>")
_APPLY = re.compile(r"\b(apply|i'?m interested|submit (your |an? |my )?application|start (your |an? |my )?application)\b", re.I)
# The words of a link that IS the application ("Apply", "Apply now", "Apply for this job"), as opposed to a sentence about applying.
_APPLY_LINK = re.compile(r"^\W*(apply(\s+(now|here|today|online|for (this|the) (job|position|role|opening)))?|start (your |my )?application|"
                         r"submit (your |an |my )?application|i'?m interested)\W*$", re.I)
_A_TAG = re.compile(r"(?is)<a\b([^>]*)>(.*?)</a>")
_ATTR = re.compile(r"(?is)\b(href|aria-label|title)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
NEEDS_RENDER = ("No structured job data on page", "Page title does not match the claimed job",
                "No application action found on page", "Page lacks a job title or employer")
NOT_ONE_POSTING = "The address is a list, search or home page, not one posting"
# Text that speaks to the model that reads the page, or names the fields of its answer: such a page is never accepted
# on the model's word (structured data, which no model reads, is unaffected).
_ADDRESSES_MODEL = re.compile(
    r"(ignore|disregard|forget) (all |any |the |your )?(previous|prior|above|earlier|system) (instructions?|prompts?|rules?)|"
    r"is_open_job_posting|page_kind|one_open_job_posting|closed_quote|"
    r"\b(you are|as) an? (large )?language model\b|"
    r"\b(mark|classify|treat|report|label) this (page|posting|job|listing) as (a |an )?(verified|open|valid|genuine|one open)|"
    r"</?(system|assistant|instructions?)>", re.I)
MOVED = "Posting address redirects to another page (closed or removed)"
SEVERAL = "Page carries several job postings, not one"
_US = {"us", "usa", "united states", "united states of america", "u.s.", "u.s.a."}
# Last path segment of a list / search / home page, and path segments that only such pages have.
_LIST_END = {"jobs", "careers", "career", "search", "searchjobs", "search-jobs", "job-search", "jobsearch", "openings",
             "positions", "vacancies", "opportunities", "open-positions", "open-roles", "all-jobs", "results", "job-list",
             "joblist", "job-board", "jobboard", "requisitions", "join-us", "work-with-us", "home", "index", "job-not-found",
             "jobnotfound", "error", "404", "not-found"}
_LIST_PART = {"search", "searchjobs", "search-jobs", "job-search", "search-results", "category", "categories"}
_SEARCH_KEYS = {"q", "query", "keyword", "keywords", "search", "page", "pg", "offset", "start", "sort", "error"}
_ID_KEY = re.compile(r"(?i)(jid|job|req|posting|opening|position|vacanc|opportunity|pid|^id$)")


def _now() -> datetime:
    """The moment every date on a page is compared with (one place, so the tests can hold it still)."""
    return datetime.now(timezone.utc)


def host_blocked(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS)


# Career platforms whose pages this verifier never opens, whatever named the address (a search engine, a model, a
# redirect, a frame inside a rendered page): their terms or robots.txt forbid automated reading (iCIMS, SAP
# SuccessFactors, BambooHR), or their reader is switched off until that question is settled (Rippling, Pinpoint).
# The hosts come from app/ats_platforms.py. SmartRecruiters is "restricted" for its API only: its posting pages have
# no robots.txt rule against reading and stay page leads.
NEVER_READ_PLATFORMS = ("icims", "successfactors", "bamboohr", "rippling", "pinpoint")


def platform_refused(url: str) -> bool:
    """The address is on a host of a career platform that must not be read page by page."""
    try:
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return False
    for platform_id in NEVER_READ_PLATFORMS:
        platform = ats_platforms.PLATFORMS.get(platform_id)
        for known in (platform.hosts if platform else ()):
            if host == known.lstrip(".") or (known.startswith(".") and host.endswith(known)):
                return True
    return False


def never_fetched(url: str) -> bool:
    """No request of any kind goes to this address: not a fetch, not a redirect, not a request of a rendered page."""
    return host_blocked(url) or platform_refused(url)


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
    requested: str = ""           # the address that was asked for, when the fetch was redirected somewhere else


@dataclass
class Verdict:
    job: Job | None = None
    reason: str = ""
    page: Page | None = None      # set when the page loaded but could not be verified deterministically
    claimed: Job | None = None    # only with defer_claim: the job the lead's own claim would give, should the AI check not answer
    extractable: bool = True      # False: the page may be searched for job links, but it is known not to be one posting


class _RobotsDenied(Exception):
    pass


# ---------------------------------------------------------------- page text
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "select", "datalist", "canvas", "object", "math", "textarea"}
_BREAK_TAGS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "ul", "ol", "table", "header",
               "main", "aside", "dd", "dt", "blockquote", "pre", "form", "title", "td", "th", "hr", "fieldset", "figure", "details"}
_CHROME_TAGS = {"nav", "footer"}          # site navigation and footers: kept, but after the page's own content


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.main: list[str] = []
        self.chrome: list[str] = []
        self.title: list[str] = []
        self._skip_tag, self._skip_depth = "", 0
        self._chrome_tag, self._chrome_depth = "", 0
        self._in_title = False

    def _out(self) -> list[str]:
        return self.title if self._in_title else (self.chrome if self._chrome_depth else self.main)

    def handle_starttag(self, tag, attrs):
        if self._skip_depth:
            self._skip_depth += tag == self._skip_tag
            return
        if tag in _SKIP_TAGS:
            self._skip_tag, self._skip_depth = tag, 1
            return
        if tag == "title":
            self._in_title = True
        elif tag in _CHROME_TAGS:
            if not self._chrome_depth:
                self._chrome_tag, self._chrome_depth = tag, 1
            elif tag == self._chrome_tag:
                self._chrome_depth += 1
        if tag == "input":                    # a button written as an input: its label is what the reader sees
            a = dict(attrs)
            if (a.get("type") or "").lower() in ("submit", "button") and a.get("value"):
                self._out().append(f" {a['value']} ")
        self._out().append(". " if tag in _BREAK_TAGS else " ")

    def handle_startendtag(self, tag, attrs):
        if not self._skip_depth:
            self.handle_starttag(tag, attrs)
            if tag in _SKIP_TAGS:             # <svg/> and the like: nothing to skip
                self._skip_depth = 0

    def handle_endtag(self, tag):
        if self._skip_depth:
            self._skip_depth -= tag == self._skip_tag
            return
        out = self._out()
        out.append(". " if tag in _BREAK_TAGS else " ")
        if tag == "title":
            self._in_title = False
        elif self._chrome_depth and tag == self._chrome_tag:
            self._chrome_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self._out().append(data)


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"(?:\s*\.){2,}\s*", ". ", re.sub(r"\s+\.", ".", text)).strip(" .")


def page_text(markup: str) -> str:
    """Readable text of a page: its title, then its own content, then navigation and footer. Scripts, styles,
    templates and form option lists are left out; attribute values never leak into the text (a ">" inside an attribute
    does not end the tag here, as it does for a regular expression)."""
    if not markup:
        return ""
    parser = _TextParser()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:                      # a document the parser cannot follow: the plain conversion is good enough
        return html_to_text(markup)
    parts = [_tidy("".join(p)) for p in (parser.title, parser.main, parser.chrome)]
    return ". ".join(p for p in parts if p)


def _norm(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def _few_h2(page: Page) -> bool:
    return len(re.findall(r"(?i)<h2\b", page.html)) <= 3


def headings(page: Page) -> list[str]:
    """What the page calls itself: <title>, <h1>, og:title / twitter:title. Second-level headings count only on a
    page that has at most three of them: a list of jobs often writes every job as an <h2>."""
    found = _HEADING.findall(page.html)
    few = _few_h2(page)
    out = [html_to_text(inner) for tag, inner in found[:40] if tag.lower() != "h2" or few]
    for meta in _META_TITLE.findall(page.html)[:4]:
        m = _CONTENT.search(meta)
        if m:
            out.append(html_lib.unescape(m.group(1) or m.group(2) or ""))
    return [h.strip() for h in dict.fromkeys(out) if h and h.strip()]


def _link_run(page: Page) -> int:
    """Size of the largest group of links on the page that differ only in an id or a long slug. A list of jobs has
    such a group (12 to 111 links on the lists read on 2026-11-21); so do some posting pages (category menus), which
    is why this is never a reason to reject on its own."""
    groups: dict[str, set[str]] = {}
    for href, _ in _ANCHOR.findall(page.html)[:1500]:
        p = urlsplit(urljoin(page.url, html_lib.unescape(href.strip())))
        if p.scheme not in ("http", "https"):
            continue
        shape = [("*" if re.search(r"\d{3,}", seg) or len(seg) > 24 or seg.count("-") >= 2
                  or (len(seg) >= 8 and re.search(r"[a-z]", seg) and re.search(r"[A-Z0-9]", seg[1:]) and "-" not in seg) else seg.lower())
                 for seg in p.path.split("/") if seg]
        if "*" in shape:
            groups.setdefault((p.hostname or "") + "/" + "/".join(shape), set()).add(p.path + "?" + p.query)
    return max((len(v) for v in groups.values()), default=0)


def _in_heading(title: str, page: Page) -> bool:
    """The title is what the page calls itself, not one line in a list of jobs: it stands, as a whole phrase, in the
    page title or a page-level heading. On a page without a run of job links or of second-level headings it may instead
    open the page's own text (small career pages write the role in a paragraph under a general heading)."""
    want = _norm(title)
    if len(want) < 3:
        return False
    rx = re.compile(r"(?<![a-z0-9])" + re.escape(want) + r"(?![a-z0-9])")
    if any(rx.search(_norm(h)) for h in headings(page)):
        return True
    return bool(rx.search(_norm(page.text[:300]))) and _few_h2(page) and _link_run(page) <= 3


# ---------------------------------------------------------------- structured data
def _loads(block: str):
    text = block.strip()
    text = re.sub(r"^\s*(?:<!--|//\s*<!\[CDATA\[|<!\[CDATA\[)|(?:-->|//\s*\]\]>|\]\]>)\s*$", "", text).strip()
    for candidate in (text, html_lib.unescape(text) if "&" in text else None, re.sub(r",\s*([}\]])", r"\1", text)):
        if candidate is None:
            continue
        try:
            return json.loads(candidate, strict=False)          # strict=False: raw line breaks inside strings are common
        except ValueError:
            continue
    return None


def _is_posting(node: dict) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(k, str) and k.rsplit("/", 1)[-1] == "JobPosting" for k in kinds)


class _MicrodataParser(HTMLParser):
    """schema.org microdata (itemscope / itemprop) as nested dicts shaped like JSON-LD. Only what a posting needs."""
    VOID = {"meta", "link", "img", "br", "hr", "input", "area", "base", "col", "embed", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self.stack: list[tuple] = []          # (tag, item this element opened or None, text target (item, prop, parts) or None)

    def _scope(self):
        for _tag, item, _text in reversed(self.stack):
            if item is not None:
                return item
        return None

    @staticmethod
    def _put(item: dict, prop: str, value) -> None:
        for name in prop.split():
            if name not in item:
                item[name] = value
            elif isinstance(item[name], list):
                item[name].append(value)
            else:
                item[name] = [item[name], value]

    def _sep(self, tag) -> None:
        for _tag, _item, text in self.stack:
            if text is not None:
                text[2].append(". " if tag in _BREAK_TAGS else " ")

    def handle_starttag(self, tag, attrs):
        self._sep(tag)
        a = {k: (v or "") for k, v in attrs}
        prop, scope = a.get("itemprop"), self._scope()
        item = text = None
        if "itemscope" in a:
            item = {"@type": a.get("itemtype", "").rstrip("/").rsplit("/", 1)[-1]}
            if prop and scope is not None:
                self._put(scope, prop, item)
            elif not prop:
                self.items.append(item)
        elif prop and scope is not None:
            for key in ("content", "datetime", "href", "src"):
                if key in a and (key != "href" or tag in ("a", "link")):
                    self._put(scope, prop, a[key])
                    break
            else:
                text = (scope, prop, [])
        if tag not in self.VOID:
            self.stack.append((tag, item, text))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID and self.stack and self.stack[-1][0] == tag:
            self._close(self.stack.pop())

    def _close(self, entry) -> None:
        text = entry[2]
        if text is not None:
            scope, prop, parts = text
            self._put(scope, prop, _tidy("".join(parts))[:20000])

    def handle_endtag(self, tag):
        self._sep(tag)
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for entry in reversed(self.stack[i:]):
                    self._close(entry)
                del self.stack[i:]
                return

    def handle_data(self, data):
        for _tag, _item, text in self.stack:
            if text is not None and len(text[2]) < 6000:          # a description is a few hundred pieces of text, not more
                text[2].append(data)


def _microdata_postings(markup: str) -> list[dict]:
    parser = _MicrodataParser()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        return []
    out, stack = [], list(parser.items)
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            if _is_posting(node):
                out.append(node)
            else:
                stack.extend(v for v in node.values() if isinstance(v, (list, dict)))
    return out


def find_postings(markup: str) -> tuple[list[dict], dict]:
    """Every distinct JobPosting on the page (JSON-LD first, microdata when there is no JSON-LD posting) and the
    JSON-LD nodes by @id (an employer is sometimes given as a reference)."""
    found: list[dict] = []
    by_id: dict[str, dict] = {}
    for block in _JSONLD.findall(markup):
        if "JobPosting" not in block and "@id" not in block:
            continue
        data = _loads(block)
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if isinstance(node.get("@id"), str) and len(node) > 1:
                    by_id.setdefault(node["@id"], node)
                if _is_posting(node):
                    found.append(node)
                stack.extend(v for v in node.values() if isinstance(v, (list, dict)))
    if not found and _MICRODATA.search(markup):
        found = _microdata_postings(markup)
        for node in found:
            node["@source"] = "microdata"
    distinct: dict[tuple, dict] = {}
    for node in found:
        key = (_norm(_text_of(node.get("title") or node.get("name"))), _norm(json.dumps(node.get("jobLocation"), sort_keys=True, default=str))[:300],
               _norm(_text_of(node.get("url") or node.get("identifier"))))
        distinct.setdefault(key, node)
    return list(distinct.values()), by_id


def _text_of(value) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("value") or value.get("@id") or ""
    if isinstance(value, list):
        value = next((_text_of(v) for v in value if _text_of(v)), "")
    return html_lib.unescape(str(value)).strip() if value not in (None, "") else ""


def _organization(posting: dict, by_id: dict, cand: Candidate, page: Page) -> str:
    org = posting.get("hiringOrganization")
    if isinstance(org, list):
        org = next((o for o in org if o), None)
    if isinstance(org, dict):
        if not (org.get("name") or org.get("legalName")) and isinstance(org.get("@id"), str):
            org = by_id.get(org["@id"], org)
        name = _text_of(org.get("name") or org.get("legalName"))
    else:
        name = _text_of(org)
    if name:
        return name
    if cand.company and _norm(cand.company) in _norm(page.text):       # a claimed employer counts only when the page names it
        return cand.company.strip()
    m = _SITE_NAME.search(page.html)
    return html_lib.unescape(m.group(1)).strip() if m else ""


def _places(posting: dict) -> tuple[str, str]:
    """(location as the page states it, country hint)."""
    places = posting.get("jobLocation") or []
    places = places if isinstance(places, list) else [places]
    names, countries = [], []
    for place in places:
        if isinstance(place, str):
            names.append(html_lib.unescape(place).strip())
            continue
        if not isinstance(place, dict):
            continue
        addr = place.get("address")
        if isinstance(addr, list):
            addr = next((a for a in addr if a), None)
        if isinstance(addr, dict):
            country = _text_of(addr.get("addressCountry"))
            parts = [_text_of(addr.get("addressLocality")), _text_of(addr.get("addressRegion")), country]
            parts = [p for i, p in enumerate(parts) if p and p not in parts[:i]]
            if parts:
                names.append(", ".join(parts))
            if country:
                countries.append(country)
        elif isinstance(addr, str) and addr.strip():
            names.append(html_lib.unescape(addr).strip())
        elif _text_of(place.get("name")):
            names.append(_text_of(place.get("name")))
    req = posting.get("applicantLocationRequirements")
    wanted = []
    for r in (req if isinstance(req, list) else [req] if req else []):
        name = _text_of(r)
        if name:
            names.append(f"Remote ({name})")
            wanted.append(name)
    countries += wanted
    hint = ""
    if countries:
        hint = "United States" if any(c.lower() in _US for c in countries) else countries[0]
    return "; ".join(dict.fromkeys(n for n in names if n)), hint


def _parse_when(value: str) -> tuple[datetime | None, bool]:
    """(moment, whether only a date was given). None when the value is not a date this code can read for sure."""
    text = str(value or "").strip()
    if not text:
        return None, False
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return datetime.fromisoformat(text).replace(tzinfo=timezone.utc), True
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)), False
    except ValueError:
        return None, False


def _base_salary(posting: dict) -> str:
    base = posting.get("baseSalary")
    if isinstance(base, (int, float)) and base > 0:
        return str(base)
    if not isinstance(base, dict):
        return "Not listed"
    value, cur = base.get("value"), _text_of(base.get("currency"))
    number = lambda v: v if isinstance(v, (int, float)) and v > 0 else (v.strip() if isinstance(v, str) and re.search(r"[1-9]", v) else None)
    if isinstance(value, dict):
        cur = cur or _text_of(value.get("currency"))
        low, high, single, unit = number(value.get("minValue")), number(value.get("maxValue")), number(value.get("value")), _text_of(value.get("unitText"))
        if low is not None and high is not None:
            return f"{cur} {low} - {high}".strip() + (f" / {unit}" if unit else "")
        if single is not None or low is not None or high is not None:
            return f"{cur} {single if single is not None else (low if low is not None else high)}".strip() + (f" / {unit}" if unit else "")
        return "Not listed"
    single = number(value)
    return f"{cur} {single}".strip() if single is not None else "Not listed"


_USD_RANGE = re.compile(r"\b(\d{2,3},\d{3})\s*(?:-|–|—|to)\s*(\d{2,3},\d{3})\s*(?:USD|US dollars)\b")
_HOURLY = re.compile(r"\$\s?\d{1,3}(?:\.\d{2})?\s*(?:-|–|—|to)\s*\$?\s?\d{1,3}(?:\.\d{2})?\s*(?:per hour|an hour|/\s?hour|/\s?hr|hourly)\b", re.I)


_PAY_RANGE = re.compile(r"\$\s?\d{2,3}(?:,\d{3})+(?:\.\d{2})?\s*(?:-|–|—|to)\s*\$?\s?\d{2,3}(?:,\d{3})+(?:\.\d{2})?")
_PAY_WORDS = re.compile(r"(?i)\b(salary|pay|compensation|hiring range|wage|base|per annum|annual|annually|per year|a year)\b")


def _salary_near_pay_words(text: str) -> str:
    """A pay range from the page's wider text, taken only when the words around it say it is pay. Used when the
    structured description is shorter than the posting the page shows (the range often stands further down)."""
    for m in _PAY_RANGE.finditer((text or "")[:40000]):
        if _PAY_WORDS.search(text[max(0, m.start() - 120): m.end() + 40]):
            return " ".join(m.group(0).split())
    return "Not listed"


def _salary(text: str) -> str:
    """A pay range the text states, in the text's own words. 'Not listed' when it states none."""
    found = extract_salary(text)
    if found != "Not listed":
        return found
    m = _USD_RANGE.search(text or "")
    if m:
        return f"{m.group(1)} - {m.group(2)} USD"
    m = _HOURLY.search(text or "")
    return " ".join(m.group(0).split()) if m else "Not listed"


_MODE = r"(remote|hybrid|on-?site|in-?office|in-?person)"
_MODE_STATED = [re.compile(p, re.I) for p in (
    r"\b(?:work (?:arrangement|model|mode|style|setting|environment|type)|workplace(?: type)?|location type)\s*[.:\-–]?\s*(?:is\s+)?(?:fully |100% )?" + _MODE + r"\b",
    r"\bthis (?:is|role is|position is|job is|opportunity is|position will be|role will be)\s+(?:an? )?(?:fully |100% |full-time |primarily )?" + _MODE + r"\b(?! (?:cloud|infrastructure|identity|environments?\b))",
    r"\b" + _MODE + r"\s*[–—-]\s*\d",
    r"\(\s*(?:fully |100% )?" + _MODE + r"\s*[)–—,-]",
    r"\b[A-Z][A-Za-z .]+, [A-Z]{2}\s+[-–—]\s+" + _MODE + r"\b",
    r"\b(?:fully|100%)\s+" + _MODE + r"\b",
    r"\b" + _MODE + r" (?:role|position|job|work schedule|schedule|work environment|work model|opportunity)\b",
)]


def _work_mode(location: str, description: str, remote: bool = False) -> str:
    """Remote / Hybrid / On-site only from a statement about the work arrangement ("This is a hybrid role",
    "Work Mode: On-Site", "(Remote)"), never from the word alone ("hybrid cloud" says nothing about the job)."""
    if remote:
        return "Remote"
    said = {m.group(1).lower().replace("-", "").replace(" ", "") for rx in _MODE_STATED for m in rx.finditer((description or "")[:20000])}
    if "hybrid" in said:
        return "Hybrid"
    if "remote" in said:
        return "Remote"
    if said:
        return "On-site"
    return detect_work_mode(location, (description or "")[:600])


def closed_reason(text: str, today: date | None = None) -> str:
    """Why the page says the posting is closed, or ''. The whole readable text is searched: on several live pages the
    notice stands after a full description."""
    for m in _CLOSED.finditer(text):
        if not _CONDITIONAL.search(text[max(0, m.start() - 60): m.start()]):
            return "Posting is marked closed"
    today = today or _now().date()
    for m in _END_DATE.finditer(text):
        raw = re.sub(r"(\d)\s?(?:st|nd|rd|th)\b", r"\1", m.group(1)).replace(" ,", ",").replace("Sept ", "Sep ").replace("Sept.", "Sep")
        day = iso_date(raw) or _day_first(raw)
        if day and date.fromisoformat(day) < today - timedelta(days=1):
            return f"Posting closed on {day} (stated on the page)"
    return ""


def _day_first(raw: str) -> str:
    for fmt in ("%d %B %Y", "%d %b %Y", "%d %B, %Y", "%d %b, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(raw.replace(".", ""), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _id_tokens(url: str) -> list[str]:
    """What identifies the posting in an address: runs of four or more digits, opaque ids, else the last path segment."""
    p = urlsplit(url)
    parts = [s for s in p.path.split("/") if s] + [v for _, v in parse_qsl(p.query)]
    tokens = [t for part in parts for t in re.findall(r"\d{4,}", part)]
    tokens += [part for part in parts if re.fullmatch(r"[A-Za-z0-9_]{8,40}", part) and re.search(r"\d", part) and re.search(r"[A-Za-z]", part)]
    # letters in mixed case that are not a word written in CamelCase ("KNnkHogzIW", not "JobDetail")
    tokens += [part for part in parts if re.fullmatch(r"[A-Za-z]{8,14}", part)
               and len(re.findall(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[a-z])", part)) >= 4]
    if not tokens:
        segments = [s for s in p.path.split("/") if s]
        tokens = [segments[-1]] if segments and len(segments[-1]) >= 4 else []
    return list(dict.fromkeys(t.lower() for t in tokens))


def moved_away(requested: str, final: str) -> bool:
    """The fetch was redirected, nothing that identified the posting is left in the address it ended on, and that
    address names no other posting either: the employer took the posting down and sends visitors to a list, a home
    page or an error page."""
    if not requested or not final:
        return False
    a, b = urlsplit(requested), urlsplit(final)
    if (a.hostname or "").lower().removeprefix("www.") == (b.hostname or "").lower().removeprefix("www.") \
            and a.path.rstrip("/").lower() == b.path.rstrip("/").lower():
        return False
    tokens = _id_tokens(requested)
    target = (b.path + "?" + b.query).lower()
    if not tokens or any(t in target for t in tokens):
        return False
    # A short link or a renumbered posting ends on an address with an id of its own; a posting that was taken down
    # ends on a list, a home page or an error page, which has none.
    return not re.search(r"\d{4,}", target)


def not_one_posting(url: str) -> bool:
    """The address itself says list, search, home or error page. Known career platforms are judged by their exact
    posting-address shapes (providers/ats_pages); any other host by a few unmistakable forms."""
    if ats_pages.not_a_posting(url):
        return True
    if ats_pages.platform_of(url):
        return False
    p = urlsplit(url)
    segments = [s.lower() for s in p.path.split("/") if s]
    keys = {k.lower(): v for k, v in parse_qsl(p.query)}
    names_one = any(_ID_KEY.search(k) and re.search(r"\d{3,}|[0-9a-f-]{16,}", v) for k, v in keys.items())
    if not segments:
        return not names_one                      # a bare host is a home page unless the query names a posting
    if names_one:
        return False
    if any(s in _LIST_PART for s in segments):
        return True
    if re.search(r"\d{4,}", p.path):                # an id in the path: ".../jobs/12345/title/job" is a posting, whatever it ends on
        return False
    last = re.sub(r"\.(html?|aspx?|php|jsp)$", "", segments[-1])
    return last in _LIST_END or bool(keys.keys() & _SEARCH_KEYS)


def render_may_help(page: Page) -> bool:
    """Whether a browser (ours or a paid fetch service) can show more of this page than the first response did."""
    return not page.rendered and not ats_pages.server_rendered(page.url)


def apply_link(page: Page) -> str:
    """The address the page's own "Apply" link leads to ('' when the page has no such link: the application is then
    on the posting page itself, or behind a button without an address). Never a link to a host that is not fetched."""
    for attrs, inner in _A_TAG.findall(page.html)[:3000]:
        found = {k.lower(): html_lib.unescape(a or b).strip() for k, a, b in _ATTR.findall(attrs)}
        label = html_to_text(inner).strip() or found.get("aria-label", "") or found.get("title", "")
        href = found.get("href", "")
        if not href or len(label) > 60 or not (_APPLY_LINK.match(label) or _APPLY_LINK.match(found.get("aria-label", ""))):
            continue
        url = urljoin(page.url, href).split("#")[0]
        if url.startswith(("http://", "https://")) and url != page.url.split("#")[0] and not host_blocked(url):
            return url[:1000]
    return ""


def _how(page: Page) -> str:
    return "Posting page rendered in a headless browser" if page.rendered else "Posting page fetched"


def _source(cand: Candidate) -> str:
    return f"{cand.provider} web search" if cand.provider in ("OpenAI", "Claude", "SearXNG") else (cand.provider or "Web crawl")


def _job_from_posting(posting: dict, by_id: dict, cand: Candidate, page: Page) -> tuple[Job | None, str]:
    title = _text_of(posting.get("title") or posting.get("name"))
    company = _organization(posting, by_id, cand, page)
    if not title or not company:
        return None, "Page lacks a job title or employer"
    now = _now()
    expiry, date_only = _parse_when(_text_of(posting.get("validThrough")))
    if expiry is not None and expiry + (timedelta(days=2) if date_only else timedelta(0)) < now:
        return None, "Posting expired (validThrough passed)"       # a bare date is good through that day, in every time zone
    posted_at = iso_date(_text_of(posting.get("datePosted")))
    if posted_at:
        try:
            if (now.date() - date.fromisoformat(posted_at)).days > STALE_POSTING_DAYS:
                return None, f"Posting date {posted_at} is more than five years old (not an open requisition)"
        except ValueError:
            posted_at = ""
    markup = _text_of(posting.get("description"))
    description = html_to_text(markup)
    # Some sites give only a summary in the structured data (1,068 characters for a posting of 12,000 on an Oracle
    # career site): the text the page shows is then the description, so skills, experience and pay are read from all of it.
    whole = len(description) >= 3000 or len(page.text) <= 2 * len(description)
    if not whole:
        description, markup = page.text[:20000], ""
    remote = "TELECOMMUTE" in str(posting.get("jobLocationType") or "").upper()
    location, hint = _places(posting)
    salary = _base_salary(posting)
    if salary == "Not listed":
        salary = _salary(description)
    if salary == "Not listed":
        salary = _salary_near_pay_words(page.text)
    years, label = extract_experience(description)
    source = _source(cand)
    kind = "microdata" if posting.get("@source") == "microdata" else "data"
    return Job(
        title=title[:300], company=company[:200], location=location or "Not listed",
        work_mode=_work_mode(location, description, remote),
        salary=salary, experience_required=label, required_years=years, description=description,
        raw_html=markup if "<" in markup else "",       # enrich_job turns it into the text with paragraphs and bullets kept
        url=page.url, apply_url=apply_link(page), source=source, sources=[source],
        country_hint=hint, posted_at=posted_at, verified_at=now_iso(),
        verification=f"{_how(page)}; schema.org JobPosting {kind} present and not expired",
        **analyze_authorization(description),
    ), ""


def _claim_matches(cand: Candidate, page: Page) -> str:
    """'' when the page's own heading carries the claimed title, else why not."""
    tokens = [t for t in re.findall(r"[a-z0-9+#]+", cand.title.lower()) if len(t) > 1]
    if not tokens:
        return "Page title does not match the claimed job"
    for heading in headings(page):
        words = set(re.findall(r"[a-z0-9+#]+", heading.lower()))
        if sum(t in words for t in tokens) / len(tokens) >= 0.8:           # whole words: "engineer" is not "engineering"
            return ""
    return "Page title does not match the claimed job"


def _job_from_content(cand: Candidate, page: Page) -> tuple[Job | None, str]:
    """No structured data: the lead's own claim counts only when the page's heading carries the claimed title, the
    page names the claimed employer and offers an application. A claimed location is shown only if the page states it."""
    if not cand.title or not cand.company or not cand.cited:
        return None, "No structured job data on page"
    reason = _claim_matches(cand, page)
    if reason:
        return None, reason
    text = _norm(page.text)
    if _norm(cand.company) not in text:
        return None, "Page lacks a job title or employer"
    if not _APPLY.search(page.text):
        return None, "No application action found on page"
    location = cand.location.strip() if cand.location and _norm(cand.location) in text else ""
    return _content_job(cand.title.strip(), cand.company.strip(), location, cand, page,
                        "title in the page heading, employer and apply action present, no closed marker"), ""


def _content_job(title: str, company: str, location: str, cand: Candidate, page: Page, evidence: str) -> Job:
    years, label = extract_experience(page.text)
    source = _source(cand)
    return Job(
        title=title, company=company, location=location or "Not listed",
        work_mode=_work_mode(location, page.text[:12000]), salary=_salary(page.text),
        experience_required=label, required_years=years, description=page.text[:12000], url=page.url, apply_url=apply_link(page),
        source=source, sources=[source], verified_at=now_iso(), verification=f"{_how(page)}; {evidence}",
        **analyze_authorization(page.text),
    )


@dataclass
class _Outcome:
    job: Job | None = None
    reason: str = ""
    final: bool = False           # nothing further (browser, AI) can change the answer
    crawlable: bool = False       # final, but the page may still be searched for links to single postings
    claimed: Job | None = None    # accepted on the lead's claim alone (see _job_from_content)


def _veto(page: Page) -> str:
    """Reasons that no claim and no model answer can override. '' when there is none."""
    closed = closed_reason(page.text)
    if closed:
        return closed
    if not_one_posting(page.url):
        return NOT_ONE_POSTING
    if moved_away(page.requested, page.url):
        return MOVED
    if len(find_postings(page.html)[0]) > 1:
        return SEVERAL
    return ""


def _assess(page: Page, cand: Candidate) -> _Outcome:
    closed = closed_reason(page.text)
    if closed:
        return _Outcome(reason=closed, final=True)
    if ats_pages.not_a_posting(page.url):                 # exact knowledge of the platform's addresses: nothing on the page overrides it
        return _Outcome(reason=NOT_ONE_POSTING, final=True, crawlable=True)
    postings, by_id = find_postings(page.html)
    if len(postings) > 1:
        return _Outcome(reason=SEVERAL, final=True, crawlable=True)
    if postings:
        job, reason = _job_from_posting(postings[0], by_id, cand, page)
        if job or reason not in NEEDS_RENDER:
            return _Outcome(job=job, reason=reason, final=True)
    if not_one_posting(page.url):
        return _Outcome(reason=NOT_ONE_POSTING, final=True, crawlable=True)
    if moved_away(page.requested, page.url):
        return _Outcome(reason=MOVED, final=True)
    job, reason = _job_from_content(cand, page)
    return _Outcome(reason=reason or "No structured job data on page", claimed=job)


def _evaluate(page: Page, cand: Candidate) -> tuple[Job | None, str]:
    """(job, reason) for one loaded page. The job is set when the page's structured data proves the posting, or, without
    structured data, when the page bears out the lead's own claim (see _job_from_content)."""
    out = _assess(page, cand)
    job = out.job or out.claimed
    return job, "" if job else out.reason


class PageVerifier:
    def __init__(self, client: httpx.AsyncClient, browser=None, timeout: float = 15.0, per_host: int = 2, host_gap: float = 0.3):
        self.client = client
        self.browser = browser
        self.timeout = timeout
        self.robots = RobotsCache(fetcher=self._robots_fetch)
        # Many leads are checked at once, and a list page can name eight postings of one site: no host gets more than
        # `per_host` requests at a time, nor two that start less than `host_gap` seconds apart.
        self.per_host, self.host_gap = max(1, per_host), max(0.0, host_gap)
        self._hosts: dict[str, list] = {}          # host -> [semaphore, earliest start of the next request]
        self._hosts_loop = None                    # the gates belong to one event loop (its clock, its semaphores)

    @asynccontextmanager
    async def _slot(self, url: str):
        host = (urlsplit(url).hostname or "").lower()
        loop = asyncio.get_running_loop()
        if self._hosts_loop is not loop:
            self._hosts, self._hosts_loop = {}, loop
        gate = self._hosts.get(host)
        if gate is None:
            if len(self._hosts) > 4000:              # a long-lived verifier: forget hosts nobody is waiting for
                self._hosts = {h: g for h, g in self._hosts.items() if g[0].locked()}
            gate = self._hosts[host] = [asyncio.Semaphore(self.per_host), 0.0]
        async with gate[0]:
            wait = gate[1] - loop.time()
            gate[1] = max(loop.time(), gate[1]) + self.host_gap
            if wait > 0:
                await asyncio.sleep(wait)
            yield

    async def _fetch(self, url: str, check_robots: bool = False) -> tuple[int, str, str]:
        """GET with manual, validated redirects and a size cap. Returns (status, final_url, text). With check_robots
        every redirect target must be allowed by its own host's robots.txt before it is requested."""
        current = url
        for _ in range(5):
            address = await assert_public_url(current)
            target, extra, extensions = pinned(current, address) if address else (current, {}, {})
            # The request goes to the address that was just checked, not to whatever the name resolves to next.
            async with self._slot(current), \
                    self.client.stream("GET", target, timeout=self.timeout, follow_redirects=False, extensions=extensions,
                                       headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5", **extra}) as r:
                location = r.headers.get("location") if r.status_code in (301, 302, 303, 307, 308) else None
                if not location:
                    chunks, size = [], 0
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        chunks.append(chunk)
                        if size > MAX_PAGE_BYTES:
                            break
                    return r.status_code, current, b"".join(chunks)[:MAX_PAGE_BYTES].decode(r.encoding or "utf-8", "replace")
            current = urljoin(current, location)          # the host's slot is free again before the next hop is looked at
            if check_robots:
                if never_fetched(current):
                    raise _RobotsDenied("Source does not permit automated verification")
                await assert_public_url(current)
                if not await self.robots.allowed(self.client, current):
                    rules = self.robots.peek(current)
                    raise _RobotsDenied("The site's robots.txt could not be read, so the page was not opened"
                                        if rules is not None and rules.transient else "Disallowed by robots.txt")
        raise UnsafeURL("Too many redirects")

    async def _robots_fetch(self, url: str) -> tuple[int, str]:
        status, _, text = await self._fetch(url)
        return status, text

    async def verify(self, cand: Candidate, browser_budget: Budget | None = None, defer_claim: bool = False) -> Verdict:
        """Check one lead. Verdict.job is set only when the page proves a live posting.

        defer_claim (the engine sets it when an AI page check will follow): a page without structured data that only
        bears out the lead's own claim is not accepted here; it is returned as Verdict.page with the claim-based job
        in Verdict.claimed, so the model can read the page first (it also reads the location) and can say no."""
        if never_fetched(cand.url):
            return Verdict(reason="Source does not permit automated verification")
        try:
            try:
                await assert_public_url(cand.url)         # an address that cannot be used is reported as such, not as a robots.txt rule
            except UnsafeURL as exc:
                if "does not resolve" not in str(exc):
                    raise
                await asyncio.sleep(0.5)                  # a resolver that did not answer once is asked once more
                await assert_public_url(cand.url)
            if not await self.robots.allowed(self.client, cand.url):
                rules = self.robots.peek(cand.url)
                if rules is not None and rules.transient:
                    return Verdict(reason="The site's robots.txt could not be read, so the page was not opened")
                return Verdict(reason="Disallowed by robots.txt")
            status, final_url, body = await self._fetch(cand.url, check_robots=True)
        except _RobotsDenied as exc:
            return Verdict(reason=f"{exc} (after a redirect)")
        except UnsafeURL as exc:
            return Verdict(reason=f"URL rejected: {exc}")
        except (httpx.HTTPError, asyncio.TimeoutError):
            return Verdict(reason="Page could not be fetched")
        if status in (404, 410):
            return Verdict(reason="Posting not found (closed or removed)")
        if status != 200:
            return Verdict(reason=f"Page returned HTTP {status}")     # includes 403: blocks are not worked around
        requested = cand.url if final_url != cand.url else ""
        page = Page(final_url, status, body, page_text(body), requested=requested)
        out = _assess(page, cand)
        if out.job:
            return Verdict(job=out.job)
        thin = len(page.text) < THIN_TEXT_CHARS
        if out.final and not (out.crawlable and thin):
            return Verdict(reason=out.reason, page=page if out.crawlable else None, extractable=False)
        # The browser is worth a page of the budget only when it can show more than the first response did: not where
        # the first response already bears out the lead, and not on platforms whose pages arrive complete.
        if out.claimed is not None or not render_may_help(page) \
                or not (self.browser and self.browser.available and browser_budget and browser_budget.take()):
            return self._open(out, page, defer_claim)
        if not await self.robots.allowed(self.client, final_url):
            return Verdict(reason="Disallowed by robots.txt")
        result = await self.browser.render(final_url)
        if result.error:
            note = "site blocks automated browsers (not bypassed)" if result.error == "blocked" else result.error
            verdict = self._open(out, page, defer_claim)
            verdict.reason = f"{out.reason}; browser render failed: {note}"
            return verdict
        seen = (getattr(result, "text", "") or "").strip()
        text = _tidy(seen) if seen else page_text(result.html)
        rendered = Page(result.url or final_url, result.status or 200, result.html, text, rendered=True,
                        requested=cand.url if (result.url or final_url) != cand.url else "")
        out = _assess(rendered, cand)
        if out.job:
            return Verdict(job=out.job)
        return self._open(out, rendered, defer_claim)

    @staticmethod
    def _open(out: _Outcome, page: Page, defer_claim: bool) -> Verdict:
        """The verdict for a page that did not prove a posting by structured data."""
        if out.final:
            return Verdict(reason=out.reason, page=page if out.crawlable else None, extractable=False)
        if out.claimed and not defer_claim:
            return Verdict(job=out.claimed)
        return Verdict(reason=out.reason, page=page, claimed=out.claimed)

    def listing_links(self, page: Page, titles: list[str], related: list[str], parent: Candidate,
                      limit: int = 8) -> list[Candidate]:
        """Links on a careers/listing page that lead to one of the wanted job titles. Only same-site or known ATS
        links are followed, and each one is verified like any other lead.

        The title is read where the page puts it: in the link itself ("Senior Data Engineer", or the first line of
        a card that is one link: "Major Account Manager. Sales. Houston, TX."), or, when the link only says "Read
        more" / "View job" / "Apply", in its aria-label, in the card it closes (the text since the previous link)
        or in its address ("/jobs/fullstack-engineer/")."""
        base_host = (urlsplit(page.url).hostname or "").lower()
        site = ".".join(base_host.split(".")[-2:])
        company = parent.company or (m.group(1).strip() if (m := _SITE_NAME.search(page.html)) else "")
        score = lambda text: best_title_relevance(text, titles, related)
        hits: dict[str, tuple[float, Candidate]] = {}
        for match in _A_TAG.finditer(page.html):
            found = {k.lower(): html_lib.unescape(a or b).strip() for k, a, b in _ATTR.findall(match.group(1))}
            href = found.get("href", "").split("#")[0].strip()
            if not href or href.lower().startswith(("javascript:", "mailto:", "tel:")):
                continue
            text = html_to_text(match.group(2))[:160].strip()
            options = [text, re.split(r"\.\s+", text, maxsplit=1)[0].strip()]
            if len(text) < 4 or _GENERIC_LINK.match(text):
                slug = [seg for seg in urlsplit(href).path.split("/") if seg][-1:]
                options += [found.get("aria-label", ""), found.get("title", ""), *_card_lines(page.html, match.start()),
                            *(" ".join(w for w in re.split(r"[-_+]+|%20", seg) if not w.isdigit()) for seg in slug if re.search(r"[A-Za-z]{3}", seg))]
            options = [o for o in dict.fromkeys(options) if 4 <= len(o) <= 160]
            if not options:
                continue
            text = max(options, key=lambda o: (round(score(o), 3), -len(o)))      # among equals, the title without the rest of the card
            if score(text) < 0.6:
                continue
            url = urljoin(page.url, href)
            host = (urlsplit(url).hostname or "").lower()
            if not url.startswith(("http://", "https://")) or url.split("#")[0] == page.url.split("#")[0] or never_fetched(url):
                continue
            if not (host.endswith(site) or parse_ats(url)):
                continue
            # An address with an id in it is a posting; one without is as often a category or team page. Postings first.
            rank = (1.0 if re.search(r"\d{4,}", urlsplit(url).path + "?" + urlsplit(url).query) else 0.0) + score(text)
            if url not in hits:
                hits[url] = (rank, Candidate(url=url, title=text[:120], company=company, location="",
                                             provider=parent.provider or "Web crawl", cited=True))
            if len(hits) >= 60:
                break
        ordered = sorted(hits.values(), key=lambda pair: -pair[0])          # stable: page order among equals
        return [cand for _, cand in ordered[:limit]]


# A link that names no job by itself: the job is named beside it.
_GENERIC_LINK = re.compile(r"^\W*((read|learn|find out|see|view|show)\s+(more|details|job|role|position|opening|posting|description)|"
                           r"more(\s+(info|information|details))?|details|job details|full description|view|apply(\s+now)?|open)\W*$", re.I)


def _card_lines(markup: str, start: int) -> list[str]:
    """The lines of text between the previous link (or the start of the card) and the link at `start`."""
    chunk = markup[max(0, start - 1500): start]
    cut = max(chunk.rfind("</a>"), chunk.rfind("<article"), chunk.rfind("<li"), chunk.rfind("<tr"))
    chunk = chunk[cut:] if cut >= 0 else chunk[chunk.find(">") + 1:]
    lines = [" ".join(line.split()) for line in html_lib.unescape(re.sub(r"(?s)<[^>]*>", "\n", chunk)).split("\n")]
    return [line for line in lines if 4 <= len(line) <= 120][-12:]


def extraction_excerpt(page: Page) -> str:
    """What the AI page check reads of one page: its address, what it calls itself, and the start of its readable
    text. Angle brackets are taken out, so page text can never close the <page> element it is sent in."""
    names = " | ".join(h[:160] for h in headings(page)[:6])
    body = f"URL: {page.url}\nHEADINGS: {names}\nTEXT: {page.text[:EXCERPT_CHARS]}"
    return body.replace("<", "‹").replace(">", "›")


def job_from_extraction(page: Page, cand: Candidate, data: dict) -> tuple[Job | None, str]:
    """Turn an AI extraction into a job only if the page bears out every part of it: the values are literally on
    the page, the title is the page's own heading, an application is offered, and no veto applies."""
    if not data.get("is_open_job_posting") or data.get("page_kind", "one_open_job_posting") != "one_open_job_posting":
        return None, "AI check: not a single open job posting"
    veto = _veto(page)
    if veto:
        return None, veto
    if _ADDRESSES_MODEL.search(page.text):
        return None, "Page text addresses the model that reads it; not accepted on the model's word"
    text = _norm(page.text)

    def on_page(value: str) -> bool:
        v = _norm(str(value).replace("‹", "<").replace("›", ">"))
        return len(v) >= 3 and v in text

    said_closed = str(data.get("closed_quote") or "").strip()
    if said_closed and on_page(said_closed):
        return None, "AI check: the page says the posting is closed"
    title = str(data.get("title") or "").strip()[:200]
    company = str(data.get("company") or "").strip()[:120] or cand.company
    location = str(data.get("location") or "").strip()[:160]
    if not title or not on_page(title):
        return None, "AI-extracted title is not on the page"
    if not _in_heading(title, page):
        return None, "AI-extracted title is not the heading of the page"
    if not company or not on_page(company):
        return None, "AI-extracted employer is not on the page"
    if location and not on_page(location):
        location = ""
    if not _APPLY.search(page.text):
        return None, "No application action found on page"
    return _content_job(title, company, location, cand, page,
                        "job details extracted by AI and matched word for word against the page text"), ""
