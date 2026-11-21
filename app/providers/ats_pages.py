"""Single-posting addresses on applicant-tracking systems that have public career pages but no anonymous API this
app may read. A posting address found by any search engine becomes a "page" lead; the page verifier then opens the
employer's live page (robots.txt of that host is checked there, per tenant) and accepts it only when the page proves
an open posting. Nothing is read from a search result's title or snippet, and nothing is fetched here.

The patterns are strict on purpose: a career home page, a search page or a listing must never count as a posting.
Each pattern is the shape seen on a live page on 2026-11-21 (docs/ats-platforms.md has the evidence per platform):

- jazzhr   https://{company}.applytojob.com/apply/{id}[/{title-slug}]
- jobvite  https://jobs.jobvite.com/{company}/job/{id}
- avature  https://{company}.avature.net/[{locale}/]{portal}/JobDetail/{title-slug}/{number}
- oracle   https://{pod}.fa.[{region}.]oraclecloud.com/hcmUI/CandidateExperience/{lang}/sites/{site}/job/{id}
- adp      https://workforcenow.adp.com/[mascsr/default/]mdf/recruitment/recruitment.html?cid={uuid}&jobId={id}[&ccId=..&lang=..]
           (both path forms answered with the same career centre on 2026-11-21; the form found is the form kept)

`not_a_posting()` is the same knowledge turned round, for the page verifier: an address on one of these hosts (or on
SmartRecruiters' job pages) that is not one posting is a career home page, a list, a search or an error page, and is
never accepted whatever the page says. `server_rendered()` names the platforms whose posting pages arrive complete
in the first response (one page of each was also rendered in a browser on 2026-11-21 and showed nothing more).

Left out deliberately (see the platform notes in app/ats_platforms.py): iCIMS (terms forbid robots on icims.com and
its subdomains, several tenants disallow everything in robots.txt), SAP SuccessFactors career{N}.successfactors.com
(robots.txt `Disallow: /`), BambooHR (terms), SmartRecruiters API (robots.txt)."""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"                      # one DNS label
# Subdomains that belong to the vendor, not to an employer.
_RESERVED = {"www", "app", "api", "auth", "careers", "career", "jobs", "docs", "help", "support", "status", "blog", "cdn",
             "static", "assets", "mail", "admin", "login", "info", "sandbox", "integrations"}
_JAZZ_HOST = re.compile(rf"({_LABEL})\.applytojob\.com")
_JAZZ_ID = re.compile(r"(?=[A-Za-z0-9]*[A-Z0-9])[A-Za-z0-9]{8,14}")       # ten mixed-case characters on live pages; never a plain word
_AVATURE_HOST = re.compile(rf"({_LABEL})\.avature\.net")
ORACLE_HOST = re.compile(rf"{_LABEL}\.fa\.(?:{_LABEL}\.){{0,3}}oraclecloud\.com")
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~%+-]{0,199}")
_COMPANY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
_JOBVITE_ID = re.compile(r"[A-Za-z0-9]{6,12}")
_JOBVITE_NOT_A_COMPANY = {"careers", "job", "jobs", "search", "api", "support", "login", "static", "assets"}
_LOCALE = re.compile(r"[a-z]{2}(?:[_-][A-Za-z]{2})?")
_PORTAL = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")
_ORACLE_SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,59}")
_ORACLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")
_ORACLE_LANG = re.compile(r"[a-z]{2,3}(?:[_-][A-Za-z]{2,4})?")
_ADP_PATH = "/mascsr/default/mdf/recruitment/recruitment.html"
_ADP_PATHS = (_ADP_PATH, "/mdf/recruitment/recruitment.html")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ADP_JOB = re.compile(r"\d{4,20}(?:_\d{1,4})?")
_ADP_CC = re.compile(r"[0-9]{6,10}_[0-9]{4,8}")
_ADP_LANG = re.compile(r"[a-z]{2}_[A-Z]{2}")

# Host names for `site:` queries, per platform (a bare domain covers every employer subdomain).
SITES: dict[str, tuple[str, ...]] = {
    "jazzhr": ("applytojob.com",),
    "jobvite": ("jobs.jobvite.com",),
    "avature": ("avature.net",),
    "oracle": ("oraclecloud.com",),
    "adp": ("workforcenow.adp.com",),
}


def _split(url: str):
    """(host, path segments, query pairs) of a plain https address, or None. No userinfo, no port but 443, no
    control characters or backslashes, no encoded slashes or dots in the path."""
    if not isinstance(url, str) or not url or len(url) > 600 or any(c in url for c in "\r\n\t\\ ") or not url.isascii():
        return None
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return None
    host = (p.hostname or "").lower().removeprefix("www.")
    if p.scheme != "https" or not host or p.username is not None or p.password is not None or port not in (None, 443):
        return None
    if "@" in p.netloc or re.search(r"%2f|%5c|%2e|%00|//", p.path, re.I) or ".." in p.path:
        return None
    return host, [s for s in p.path.split("/") if s], parse_qsl(p.query, keep_blank_values=False)


def _jazzhr(host, parts, query):
    m = _JAZZ_HOST.fullmatch(host)
    if not m or m.group(1) in _RESERVED or len(parts) not in (2, 3) or parts[0] != "apply" or not _JAZZ_ID.fullmatch(parts[1]):
        return None
    if len(parts) == 3 and not _SLUG.fullmatch(parts[2]):
        return None
    return f"https://{host}/" + "/".join(parts)


def _jobvite(host, parts, query):
    if host != "jobs.jobvite.com" or len(parts) != 3 or parts[1] != "job":
        return None
    if not _COMPANY.fullmatch(parts[0]) or parts[0].lower() in _JOBVITE_NOT_A_COMPANY or not _JOBVITE_ID.fullmatch(parts[2]):
        return None
    return f"https://{host}/" + "/".join(parts)


def _avature(host, parts, query):
    m = _AVATURE_HOST.fullmatch(host)
    if not m or m.group(1) in _RESERVED:
        return None
    segs = parts[1:] if len(parts) == 5 and _LOCALE.fullmatch(parts[0]) else parts
    if len(segs) != 4 or segs[1] != "JobDetail" or not _PORTAL.fullmatch(segs[0]):
        return None
    if not _SLUG.fullmatch(segs[2]) or not re.fullmatch(r"\d{1,10}", segs[3]):
        return None
    return f"https://{host}/" + "/".join(parts)


def _oracle(host, parts, query):
    if not ORACLE_HOST.fullmatch(host) or len(parts) != 7:
        return None
    if parts[0] != "hcmUI" or parts[1] != "CandidateExperience" or parts[3] != "sites" or parts[5] != "job":
        return None
    if not _ORACLE_LANG.fullmatch(parts[2]) or not _ORACLE_SITE.fullmatch(parts[4]) or not _ORACLE_ID.fullmatch(parts[6]):
        return None
    return f"https://{host}/" + "/".join(parts)


def _adp(host, parts, query):
    path = "/" + "/".join(parts)
    if host != "workforcenow.adp.com" or path not in _ADP_PATHS:
        return None
    q = {}
    for key, value in query:
        if key in q:
            return None                                   # a repeated parameter: not an address the career centre writes
        q[key] = value
    cid, job = q.get("cid", "").lower(), q.get("jobId", "")
    if not _UUID.fullmatch(cid) or not _ADP_JOB.fullmatch(job):
        return None
    keep = [("cid", cid)]
    if _ADP_CC.fullmatch(q.get("ccId", "")):
        keep.append(("ccId", q["ccId"]))
    keep.append(("jobId", job))
    if _ADP_LANG.fullmatch(q.get("lang", "")):
        keep.append(("lang", q["lang"]))
    return f"https://{host}{path}?" + urlencode(keep)


_MATCHERS = (("jazzhr", _jazzhr), ("jobvite", _jobvite), ("avature", _avature), ("oracle", _oracle), ("adp", _adp))


# SmartRecruiters job pages are page leads too (providers/searxng.py: PAGE_LEAD_HOSTS). Only their address shape is
# needed here; the hosts are not in SITES because they are queried through the platform's own entry.
_SR_HOSTS = ("jobs.smartrecruiters.com", "careers.smartrecruiters.com")
_SR_COMPANY = re.compile(r"[A-Za-z0-9._-]{1,80}")
_SR_POSTING = re.compile(r"\d{6,}[A-Za-z0-9%._-]*")
SERVER_RENDERED = frozenset({"jazzhr", "jobvite", "avature", "smartrecruiters"})


def _smartrecruiters(host, parts) -> bool:
    return host == "jobs.smartrecruiters.com" and len(parts) == 2 and bool(_SR_COMPANY.fullmatch(parts[0])) \
        and bool(_SR_POSTING.fullmatch(parts[1]))


def platform_of(url: str) -> str:
    """Platform id of the host of `url` ('' when it is none of these platforms). Says nothing about the path."""
    try:
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except (ValueError, AttributeError):
        return ""
    if host in _SR_HOSTS:
        return "smartrecruiters"
    for platform, sites in SITES.items():
        if any(host == site or host.endswith("." + site) for site in sites):
            return platform
    return ""


def not_a_posting(url: str) -> bool:
    """True when `url` is on a host of these platforms and is NOT the address of one posting: a career home page,
    a list, a search, an application form or an error page. False for every other host (nothing is known there)."""
    platform = platform_of(url)
    if not platform:
        return False
    if url[:7].lower() == "http://":                  # the patterns are written for https; the scheme is not what decides here
        url = "https://" + url[7:]
    if platform == "smartrecruiters":
        split = _split(url)
        return split is None or not _smartrecruiters(split[0], split[1])
    return posting(url) is None


def server_rendered(url: str) -> bool:
    """Whether posting pages of this host arrive complete in the first response, so a browser adds nothing."""
    return platform_of(url) in SERVER_RENDERED


def is_page_host(host: str) -> bool:
    """Whether `host` belongs to one of the platforms handled here (posting or not)."""
    host = (host or "").lower().removeprefix("www.")
    return any(host == site or host.endswith("." + site) for sites in SITES.values() for site in sites)


def posting(url: str) -> tuple[str, str] | None:
    """(platform id, clean posting address) when `url` is one posting on a career page of these platforms, else None.
    The clean address has no fragment and no parameters except those that name the posting (ADP)."""
    split = _split(url.strip() if isinstance(url, str) else url)
    if split is None:
        return None
    host, parts, query = split
    if not is_page_host(host):
        return None
    for platform, match in _MATCHERS:
        clean = match(host, parts, query)
        if clean:
            return platform, clean
    return None
