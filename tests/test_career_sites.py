"""The career-site crawler (app/career_sites.py): which hosts are a site, which addresses are one posting, sitemaps
(index, gzip, hostile), robots.txt, the network guard on redirects, listing pages (data, links, browser), budgets,
back-off and the weekly schedule. No network: every site here is synthetic and served by a mock transport."""
import asyncio
import gzip
import json
import time
from datetime import date, timedelta

import httpx
import pytest

from app import board_catalog, career_sites as cs, netguard
from app.browser_pool import RenderResult
from app.config import Settings
from app.providers.registry import CAREER_PAGE
from app.schemas import Job

HOST = "careers.acme-robotics.com"
BASE = f"https://{HOST}"
TODAY = date.today()


def day(ago: int) -> str:
    return (TODAY - timedelta(days=ago)).isoformat()


def posting(title: str, city: str = "Austin", region: str = "TX", country: str = "US", company: str = "Acme Robotics",
            data: bool = True) -> str:
    node = {"@context": "https://schema.org", "@type": "JobPosting", "title": title, "datePosted": day(3),
            "hiringOrganization": {"@type": "Organization", "name": company},
            "jobLocation": {"@type": "Place", "address": {"addressLocality": city, "addressRegion": region, "addressCountry": country}},
            "description": "Build robots with Python. 3+ years of experience."}
    script = f'<script type="application/ld+json">{json.dumps(node)}</script>' if data else ""
    return (f"<html><head><title>{title} | {company}</title>{script}</head><body><h1>{title}</h1>"
            f"<p>{company} is hiring in {city}.</p><a href='/apply'>Apply now</a></body></html>")


def urlset(entries) -> bytes:
    rows = "".join(f"<url><loc>{loc}</loc>" + (f"<lastmod>{mod}</lastmod>" if mod else "") + "</url>" for loc, mod in entries)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</urlset>'.encode()


def sitemap_index(entries) -> bytes:
    rows = "".join(f"<sitemap><loc>{loc}</loc>" + (f"<lastmod>{mod}</lastmod>" if mod else "") + "</sitemap>" for loc, mod in entries)
    return f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</sitemapindex>'.encode()


class Web:
    """A synthetic web: address -> (status, body[, headers]). Every request is logged with its time."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.log: list[tuple[str, float]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.log.append((url, time.monotonic()))
        assert request.method == "GET"
        hit = self.routes.get(url)
        if hit is None:
            return httpx.Response(404, text="not found")
        status, body, *rest = hit
        headers = {"content-type": "text/html", **(rest[0] if rest else {})}
        return httpx.Response(status, content=body if isinstance(body, bytes) else body.encode(), headers=headers)

    def asked(self, host: str = "") -> list[str]:
        return [u for u, _ in self.log if not host or httpx.URL(u).host == host]


class MemoryStore:
    """The store's contract in memory (the SQL one is tested against PostgreSQL in tests/portal)."""

    def __init__(self):
        self.sites: dict[str, cs.Site] = {}
        self.next_at: dict[str, float] = {}
        self.misses: dict[str, int] = {}
        self.crawls: list[cs.CrawlRecord] = []
        self.pages: set[str] = set()

    async def register(self, host, base_url, employer="", origin="search", found_url="", listing_url=""):
        if host in self.sites:
            return False
        self.sites[host] = cs.Site(host, base_url, employer, origin, found_url, listing_url)
        self.next_at[host] = 0.0
        return True

    async def claim(self, now, limit, hold):
        due = sorted((h for h in self.sites if self.next_at[h] <= now), key=lambda h: (self.next_at[h], h))[:limit]
        for host in due:
            self.next_at[host] = now + hold
        return [self.sites[h] for h in due]

    async def finish(self, host, record, next_at, misses, seen):
        site = self.sites[host]
        site.state, site.misses, site.crawled_at, site.seen = record.state, misses, record.finished_at, tuple(seen)
        self.next_at[host], self.misses[host] = next_at, misses
        self.crawls.append(record)

    async def pages_since(self, since):
        return sum(r.pages_seen for r in self.crawls if r.started_at > since)

    async def known_pages(self, host):
        return {u for u in self.pages if cs.site_key(httpx.URL(u).host) == host}


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Browser:
    def __init__(self, pages=None, available=True):
        self.pages, self.available, self.rendered = pages or {}, available, []

    async def render(self, url):
        self.rendered.append(url)
        return self.pages.get(url) or RenderResult(200, url, "<html><body></body></html>")


def make(web: Web, *, browser=None, store=None, clock=None, fast=True, **settings):
    """(crawler, store, kept jobs, client). `fast` takes the pause between requests out (one test keeps it)."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(web.handler))
    reader = cs.SiteReader(client, browser)
    if fast:
        reader.host_gap = 0.0
    store = store or MemoryStore()
    kept: list[Job] = []

    async def remember(jobs):
        kept.extend(jobs)
        store.pages.update(j.url for j in jobs)
        return len(jobs)

    conf = Settings(_env_file=None, **settings)
    return cs.Crawler(store, reader, remember, conf, clock=clock or time.time), store, kept, client


def site(found="", listing="", **over) -> cs.Site:
    return cs.Site(HOST, BASE, "Acme Robotics", found_url=found, listing_url=listing, **over)


def run(coro):
    return asyncio.run(coro)


async def crawl(crawler, client, the_site, budget=150):
    try:
        return await crawler.crawl(the_site, budget)
    finally:
        await client.aclose()


# ---------------------------------------------------------------- hosts and addresses
def test_which_hosts_may_be_a_company_site():
    assert cs.site_key("WWW.Example-Corp.com.") == "example-corp.com" and cs.site_key("jobs.example.co.uk") == "jobs.example.co.uk"
    for bad in ("", "localhost", "intranet", "10.0.0.5", "[::1]", "127.0.0.1", "db.internal", "printer.local", "exämple.com",
                "a" * 250 + ".com", "example.com:8443", "exa mple.com", "user@example.com"):
        assert cs.site_key(bad) == "", bad
    assert cs.refusal("careers.example-corp.com") == "" and cs.refusal("www.metacareers.com") == ""
    for vendor in ("boards.greenhouse.io", "jobs.lever.co", "acme.wd5.myworkdayjobs.com", "jobs.ashbyhq.com", "acme.applytojob.com",
                   "jobs.jobvite.com", "acme.avature.net", "acme.teamtailor.com", "acme.breezy.hr", "eeho.fa.us2.oraclecloud.com"):
        assert "career platform" in cs.refusal(vendor), vendor
    for never in ("careers-acme.icims.com", "career4.successfactors.com", "acme.bamboohr.com", "ats.rippling.com", "acme.pinpointhq.com",
                  "www.linkedin.com", "indeed.com", "www.glassdoor.com", "ziprecruiter.com"):
        assert cs.refusal(never) == "a host that is never fetched", never
    assert cs.refusal("builtin.com", ("builtin.com",)) == "a job board or aggregator"
    assert "job board" in cs.refusal("usajobs.gov") and cs.refusal("10.1.2.3") == "not a public host name"


def test_a_listing_search_category_or_home_address_is_never_a_posting():
    a = cs.Addresses(HOST, [f"{BASE}/jobs/4471823/senior-robotics-engineer"])
    assert a.clean(f"{BASE}/jobs/5582934/controls-engineer#apply") == f"{BASE}/jobs/5582934/controls-engineer"
    assert a.clean(f"https://www.{HOST}/jobs/5582934/controls-engineer")                       # "www." is the same site
    assert a.clean(f"{BASE}/careers/senior-robotics-engineer-austin")                            # no known shape, but the general one
    assert a.clean(f"{BASE}/en-us/job/R-104455") and a.clean(f"{BASE}/profile/job_details/1436181490732782")
    assert a.clean(f"{BASE}/careers/view?jobId=4471823") and a.clean(f"{BASE}/roles/senior-robotics-engineer-austin")
    assert a.clean(f"{BASE}/detail/4471823/?gh_jid=4471823") and a.clean(f"{BASE}/en-us/details/200554357/robotics-engineer?team=HRDWR")
    refused = [
        f"{BASE}/", f"{BASE}/jobs", f"{BASE}/jobs/", f"{BASE}/careers", f"{BASE}/jobs/search?q=engineer", f"{BASE}/search-jobs/austin",
        f"{BASE}/jobs/category/engineering-roles-and-more", f"{BASE}/jobs/engineering", f"{BASE}/careers/teams", f"{BASE}/jobs?page=2",
        f"{BASE}/jobs/locations", f"{BASE}/about/our-long-company-story", f"{BASE}/roles/engineering", f"{BASE}/careers/apply", f"{BASE}/careers/apply?jobId=4471823",
        f"{BASE}/jobs/apply/senior-robotics-engineer/4471823", f"{BASE}/jobs/4471823/application", f"{BASE}/login/jobs/4471823/x", f"{BASE}/blog/a-day-in-the-life-of-an-engineer",
        f"{BASE}/jobs/5582934/controls-engineer.pdf", f"{BASE}/sitemap-jobs.xml", f"http://{HOST}/jobs/5582934/controls-engineer",
        f"https://user:pw@{HOST}/jobs/5582934/x", f"https://{HOST}:8443/jobs/5582934/x", f"{BASE}/jobs/../admin/5582934",
        "https://other-company.com/jobs/5582934/controls-engineer", "https://www.linkedin.com/jobs/view/5582934",
        "https://careers-acme.icims.com/jobs/5582934/job", f"{BASE}/jobs/5582934/" + "x" * 600, f"{BASE}/jobs/55 82934", "", None, 7,
        "javascript:alert(1)", "ftp://careers.acme-robotics.com/jobs/5582934",
    ]
    for url in refused:
        assert a.clean(url) is None, url
    assert cs.shape(f"{BASE}/careers/job/790298012345") == "/careers/job/*" and cs.shape(f"{BASE}/apply?jobId=44718") == "/apply?jobid"
    assert cs.shape(f"{BASE}/us/en/job/SNCOUS9787F02/IT-Auditor") == cs.shape(f"{BASE}/us/en/job/SNCOUS363B3B2/Principal-Practice-Lead-Data") == "/us/en/job/*/*"
    assert [cs.us_hint(f"{BASE}{p}") for p in ("/en-us/job/1234", "/de-de/job/1234", "/job/1234-engineer-austin-texas-united-states", "/job/1234")] == [1, -1, 1, 0]
    assert [cs.us_hint(f"{BASE}{p}") for p in ("/fr/positions/1234", "/us/positions/1234", "/en/positions/1234", "/positions/1234", "/fr")] == [-1, 1, 0, 0, 0]
    assert cs.same_site("careers.acme.com", "acme.com") and cs.same_site("acme.com", "eu.careers.acme.com") and cs.same_site("acme.com", "www.acme.com")
    assert not cs.same_site("jobs.acme.com", "apply.acme.com") and not cs.same_site("acme.com", "evil-acme.com") and not cs.same_site("acme.com", "com")
    # A site whose postings have short names: what lies beside a verified posting is a candidate, a list still is not.
    short = cs.Addresses(HOST, [f"{BASE}/openings/ml-1"])
    assert short.clean(f"{BASE}/openings/qa-2") and short.clean(f"{BASE}/openings") is None and short.clean(f"{BASE}/team/qa-2") is None


# ---------------------------------------------------------------- sitemaps and robots.txt
def test_sitemap_index_urlset_gzip_and_text_are_read():
    kind, entries = cs.parse_sitemap(urlset([(f"{BASE}/jobs/1001/a", "2026-11-20T08:00:00+00:00"), (f"{BASE}/jobs/1002/b&amp;c", "")]))
    assert kind == "urlset" and entries == [(f"{BASE}/jobs/1001/a", "2026-11-20"), (f"{BASE}/jobs/1002/b&c", "")]
    kind, entries = cs.parse_sitemap(gzip.compress(sitemap_index([(f"{BASE}/sitemap-jobs.xml.gz", "2026-11-01")])))
    assert kind == "index" and entries == [(f"{BASE}/sitemap-jobs.xml.gz", "2026-11-01")]
    prefixed = b'<sm:urlset xmlns:sm="x"><sm:url><sm:loc><![CDATA[ https://a.example.com/jobs/12345 ]]></sm:loc></sm:url></sm:urlset>'
    assert cs.parse_sitemap(prefixed) == ("urlset", [("https://a.example.com/jobs/12345", "")])
    assert cs.parse_sitemap(f"{BASE}/jobs/1001/a\nnot an address\n{BASE}/jobs/1002/b\n".encode()) == (
        "urlset", [(f"{BASE}/jobs/1001/a", ""), (f"{BASE}/jobs/1002/b", "")])
    assert cs.parse_sitemap(b"<html><body>Not found</body></html>") == ("", []) and cs.parse_sitemap(b"") == ("", [])
    assert cs.parse_sitemap(b"\x1f\x8b broken gzip") == ("", [])


def test_a_hostile_sitemap_is_bounded():
    huge = urlset([(f"{BASE}/jobs/{100000 + i}/x", "") for i in range(60_000)])
    kind, entries = cs.parse_sitemap(huge)
    assert kind == "urlset" and len(entries) == cs.MAX_SITEMAP_ENTRIES
    assert len(cs.parse_sitemap(huge, limit=25)[1]) == 25
    bomb = gzip.compress(b"<urlset>" + b" " * 80_000_000 + b"</urlset>")           # 80 MB of nothing in 80 kB
    assert len(bomb) < 200_000 and len(cs.inflate(bomb)) == cs.MAX_SITEMAP_TEXT and len(cs.inflate(bomb, 1000)) == 1000
    entity = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]><urlset><url><loc>&b;</loc></url></urlset>'
    assert cs.parse_sitemap(entity) == ("urlset", [("&b;", "")])                    # entities are never expanded


def test_robots_sitemap_lines_and_crawl_delay():
    text = ("# comment\nUser-agent: *\nDisallow: /admin\nCrawl-delay: 5\n\nUser-agent: usa-ai-job-finder\nCrawl-delay: 2\n"
            f"Sitemap: {BASE}/sitemap.xml\nsitemap:{BASE}/jobs.xml  # jobs\nSitemap:\n")
    assert cs.robots_extras(text) == ([f"{BASE}/sitemap.xml", f"{BASE}/jobs.xml"], 2.0)
    assert cs.robots_extras("User-agent: *\nCrawl-delay: 7\nCrawl-delay: soon\n") == ([], 7.0)
    assert cs.robots_extras("User-agent: otherbot\nCrawl-delay: 30\n") == ([], 0.0)


def a_site_with_a_sitemap(extra_routes=None) -> Web:
    jobs = [(f"{BASE}/jobs/4471823/senior-robotics-engineer", day(1)), (f"{BASE}/jobs/4471824/controls-engineer", day(9)),
            (f"{BASE}/jobs/4471825/field-technician-toronto", day(2)), (f"{BASE}/jobs/4471826/closed-role", day(4)),
            (f"{BASE}/jobs/4471827/gone-role", day(5)),
            (f"{BASE}/jobs", day(0)), (f"{BASE}/jobs/search?q=robot", day(0)), (f"{BASE}/about/our-long-company-story", day(0)),
            ("https://www.linkedin.com/jobs/view/4471828", day(0)), ("https://careers-acme.icims.com/jobs/4471829/job", day(0)),
            ("https://other-company.com/jobs/4471830/intruder", day(0))]
    routes = {
        f"{BASE}/robots.txt": (200, f"User-agent: *\nDisallow: /admin\nSitemap: {BASE}/sitemap_index.xml\n"),
        f"{BASE}/sitemap_index.xml": (200, sitemap_index([(f"{BASE}/sitemaps/products.xml", day(0)), (f"{BASE}/sitemaps/jobs-1.xml.gz", day(1))]),
                                      {"content-type": "application/xml"}),
        f"{BASE}/sitemaps/jobs-1.xml.gz": (200, gzip.compress(urlset(jobs)), {"content-type": "application/x-gzip"}),
        f"{BASE}/sitemaps/products.xml": (200, urlset([(f"{BASE}/products/arm-9000-industrial-robot", day(0))])),
        f"{BASE}/jobs/4471823/senior-robotics-engineer": (200, posting("Senior Robotics Engineer")),
        f"{BASE}/jobs/4471824/controls-engineer": (200, posting("Controls Engineer", "Remote", "", "US")),
        f"{BASE}/jobs/4471825/field-technician-toronto": (200, posting("Field Technician", "Toronto", "ON", "CA")),
        f"{BASE}/jobs/4471826/closed-role": (200, posting("Welder").replace("<h1>", "<p>This position has been filled.</p><h1>")),
    }
    routes.update(extra_routes or {})
    return Web(routes)


def test_a_site_is_read_through_its_sitemap_index_and_gzip_sitemap_newest_first():
    web = a_site_with_a_sitemap()
    crawler, _store, kept, client = make(web)
    record = run(crawl(crawler, client, site()))
    assert (record.state, record.method, record.error) == (cs.OK, cs.SITEMAP, "")
    assert (record.candidates, record.pages_seen, record.postings, record.us_postings) == (5, 5, 3, 2)
    assert sorted(j.title for j in kept) == ["Controls Engineer", "Senior Robotics Engineer"]
    assert all(j.source == "Company career site" and j.verification.startswith("Posting page fetched; schema.org JobPosting") for j in kept)
    asked = web.asked()
    assert asked[:3] == [f"{BASE}/robots.txt", f"{BASE}/sitemap_index.xml", f"{BASE}/sitemaps/jobs-1.xml.gz"]     # job-named sitemap only
    assert f"{BASE}/sitemaps/products.xml" not in asked
    pages = asked[3:]
    assert [p.rsplit("/", 2)[1] for p in pages] == ["4471823", "4471825", "4471826", "4471827", "4471824"]           # newest lastmod first
    assert {httpx.URL(u).host for u in asked} == {HOST}                           # nothing on LinkedIn, iCIMS or another company's host
    assert record.requests == len(asked) == 8 and record.sitemaps == 2 and record.browser_pages == 0
    assert record.reasons["Posting is marked closed"] == 1 and record.reasons["Posting not found (closed or removed)"] == 1


def test_without_a_sitemap_line_the_usual_sitemap_address_is_tried():
    web = Web({f"{BASE}/sitemap.xml": (200, urlset([(f"{BASE}/jobs/4471823/senior-robotics-engineer", "")])),
               f"{BASE}/jobs/4471823/senior-robotics-engineer": (200, posting("Senior Robotics Engineer"))})
    crawler, _store, kept, client = make(web)
    record = run(crawl(crawler, client, site()))
    assert record.method == cs.SITEMAP and record.us_postings == 1 and web.asked()[:2] == [f"{BASE}/robots.txt", f"{BASE}/sitemap.xml"]


def test_robots_txt_is_obeyed_for_sitemaps_and_pages():
    # Posting pages are disallowed: the sitemap is read, no posting page is requested.
    web = a_site_with_a_sitemap({f"{BASE}/robots.txt": (200, f"User-agent: *\nDisallow: /jobs/\nSitemap: {BASE}/sitemap_index.xml\n")})
    crawler, _store, kept, client = make(web)
    record = run(crawl(crawler, client, site()))
    assert kept == [] and record.pages_seen == 0 and record.disallowed == 5 and not [u for u in web.asked() if "/jobs/" in u]
    # The sitemaps are disallowed too: only robots.txt and the listing addresses that robots.txt allows are asked.
    web = a_site_with_a_sitemap({f"{BASE}/robots.txt": (200, f"User-agent: *\nDisallow: /\nSitemap: {BASE}/sitemap_index.xml\n")})
    crawler, _store, kept, client = make(web)
    record = run(crawl(crawler, client, site()))
    assert web.asked() == [f"{BASE}/robots.txt"] and (record.state, record.candidates) == (cs.NOT_READABLE, 0) and "robots.txt" in record.error
    # robots.txt cannot be read (server error): nothing else is asked and the crawl counts as failed, to be tried again.
    web = a_site_with_a_sitemap({f"{BASE}/robots.txt": (503, "busy")})
    crawler, _store, kept, client = make(web)
    record = run(crawl(crawler, client, site()))
    assert web.asked() == [f"{BASE}/robots.txt"] and record.state == cs.FAILED and record.error == "robots.txt could not be read"


def test_a_crawl_delay_is_kept_and_an_excessive_one_means_the_site_is_left_alone():
    web = a_site_with_a_sitemap({f"{BASE}/robots.txt": (200, f"User-agent: *\nCrawl-delay: 4\nSitemap: {BASE}/sitemap_index.xml\n")})
    crawler, _store, _kept, client = make(web)
    crawler.reader.host_gap = 0.0

    async def go():
        task = asyncio.create_task(crawler.crawl(site(), 5))
        await asyncio.sleep(0.3)
        task.cancel()                                   # the second request would start four seconds after the first
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()
    run(go())
    assert crawler.reader.gaps[HOST] == 4.0 and len(web.asked()) <= 2
    web = a_site_with_a_sitemap({f"{BASE}/robots.txt": (200, "User-agent: *\nCrawl-delay: 300\n")})
    crawler, _store, _kept, client = make(web)
    record = run(crawl(crawler, client, site()))
    assert record.state == cs.NOT_READABLE and "300 s between requests" in record.error and web.asked() == [f"{BASE}/robots.txt"]


def test_requests_to_one_host_are_at_least_a_second_apart():
    web = Web({f"{BASE}/sitemap.xml": (200, urlset([(f"{BASE}/jobs/4471823/senior-robotics-engineer", "")])),
               f"{BASE}/jobs/4471823/senior-robotics-engineer": (200, posting("Senior Robotics Engineer"))})
    crawler, _store, kept, client = make(web, fast=False, career_site_request_gap_seconds=0.0)       # a setting below 1 s is raised to 1 s
    assert crawler.reader.host_gap == 1.0
    record = run(crawl(crawler, client, site()))
    times = [t for _, t in web.log]
    assert len(times) == 3 and record.us_postings == 1 and all(b - a >= 1.0 for a, b in zip(times, times[1:]))


def test_a_redirect_to_a_private_address_is_not_followed(monkeypatch):
    async def guard(url):                                # the real guard for literal addresses; names resolve nowhere in tests
        host = httpx.URL(url).host
        if host.replace(".", "").isdigit() or host in ("localhost", "metadata.internal"):
            return await netguard.assert_public_url(url)
        return None
    monkeypatch.setattr("app.job_verifier.assert_public_url", guard)
    web = Web({
        f"{BASE}/robots.txt": (200, f"Sitemap: {BASE}/sitemap.xml\nSitemap: {BASE}/inner.xml\n"),
        f"{BASE}/inner.xml": (302, "", {"location": "http://169.254.169.254/latest/meta-data/"}),
        f"{BASE}/sitemap.xml": (200, urlset([(f"{BASE}/jobs/4471823/senior-robotics-engineer", ""), (f"{BASE}/jobs/4471824/trap", "")])),
        f"{BASE}/jobs/4471823/senior-robotics-engineer": (200, posting("Senior Robotics Engineer")),
        f"{BASE}/jobs/4471824/trap": (301, "", {"location": "http://10.0.0.5/jobs/4471824/trap"}),
        "http://10.0.0.5/jobs/4471824/trap": (200, posting("Trap")),
        "http://169.254.169.254/latest/meta-data/": (200, urlset([(f"{BASE}/jobs/9999999/planted", "")])),
    })
    crawler, _store, kept, client = make(web)
    record = run(crawl(crawler, client, site()))
    assert [j.title for j in kept] == ["Senior Robotics Engineer"] and record.candidates == 2
    assert not [u for u in web.asked() if "10.0.0.5" in u or "169.254" in u]
    assert record.reasons["URL rejected: Hostname resolves to a non-public address"] == 1
    # A site record whose own address is not public is refused before any request.
    web = Web()
    crawler, _store, _kept, client = make(web)
    bad = cs.Site("10.0.0.5", "https://10.0.0.5")
    record = run(crawl(crawler, client, bad))
    assert record.state == cs.NOT_READABLE and web.asked() == []


def test_a_sitemap_on_a_foreign_or_never_fetched_host_is_not_requested():
    web = Web({f"{BASE}/robots.txt": (200, "Sitemap: https://www.linkedin.com/sitemap.xml\nSitemap: https://cdn.elsewhere.net/acme.xml\n"
                                           f"Sitemap: https://acme-robotics.com/sitemap.xml\nSitemap: http://{HOST}/plain.xml\n"),
               "https://acme-robotics.com/sitemap.xml": (200, urlset([(f"{BASE}/jobs/4471823/senior-robotics-engineer", "")])),
               f"{BASE}/jobs/4471823/senior-robotics-engineer": (200, posting("Senior Robotics Engineer"))})
    crawler, _store, kept, client = make(web)
    record = run(crawl(crawler, client, site()))
    hosts = {httpx.URL(u).host for u in web.asked()}
    assert hosts == {HOST, "acme-robotics.com"} and record.us_postings == 1         # the parent domain's sitemap is the same site
    assert f"http://{HOST}/plain.xml" not in web.asked()


# ---------------------------------------------------------------- budgets, memory of pages
def hostile_site(n=3000) -> Web:
    entries = [(f"{BASE}/jobs/{5_000_000 + i}/role-{i}", day(i % 30)) for i in range(n)]
    entries += [(f"https://www.indeed.com/viewjob?jk={i}", day(0)) for i in range(200)]
    web = Web({f"{BASE}/sitemap.xml": (200, urlset(entries))})
    for loc, _ in entries[:n]:
        web.routes[loc] = (200, posting("Robot Operator"))
    return web


def test_a_crawl_reads_no_more_pages_than_its_budget_however_large_the_sitemap():
    web = hostile_site()
    crawler, _store, kept, client = make(web, career_site_pages_per_crawl=7)
    record = run(crawl(crawler, client, site(), budget=7))
    assert record.candidates == 3000 and record.pages_seen == 7 == len(kept) and len(web.asked()) == 2 + 7
    assert {httpx.URL(u).host for u in web.asked()} == {HOST}


def test_the_next_crawl_reads_new_pages_not_the_ones_already_read_or_stored():
    web = a_site_with_a_sitemap()
    crawler, store, kept, client = make(web)

    async def go():
        await store.register(HOST, BASE, "Acme Robotics", "search", f"{BASE}/jobs/4471823/senior-robotics-engineer")
        store.pages.add(f"{BASE}/jobs/4471823/senior-robotics-engineer")          # the posting the search itself kept
        first = await crawler.run(store.sites[HOST], 150)
        before = len(web.log)
        second = await crawler.run(store.sites[HOST], 150)
        # A new posting appears, and a page read before carries a newer lastmod: both are read, nothing else.
        new = f"{BASE}/jobs/4471899/new-role"
        web.routes[new] = (200, posting("Calibration Engineer"))
        future = (TODAY + timedelta(days=2)).isoformat()
        web.routes[f"{BASE}/sitemaps/jobs-1.xml.gz"] = (200, gzip.compress(urlset([
            (new, ""), (f"{BASE}/jobs/4471826/closed-role", future), (f"{BASE}/jobs/4471827/gone-role", day(5))])))
        third = await crawler.run(store.sites[HOST], 150)
        await client.aclose()
        return first, second, third, before

    first, second, third, before = run(go())
    assert (first.known, first.pages_seen, first.us_postings) == (1, 4, 1) and first.state == cs.OK
    assert (second.pages_seen, second.known, second.state) == (0, 2, cs.OK) and len(web.log) - before == 6      # robots is cached: 2 sitemaps, then 2 + pages
    assert third.pages_seen == 2 and third.us_postings == 1 and [j.title for j in kept] == ["Controls Engineer", "Calibration Engineer"]


def test_address_shapes_that_never_prove_a_posting_are_given_up():
    entries = [(f"{BASE}/careers/story-of-our-great-team-{i}", day(0)) for i in range(40)]
    entries += [(f"{BASE}/jobs/{4471823 + i}/role", day(1)) for i in range(3)]
    web = Web({f"{BASE}/sitemap.xml": (200, urlset(entries))})
    for loc, _ in entries[:40]:
        web.routes[loc] = (200, "<html><head><title>Our team</title></head><body><h1>Our team</h1><p>" + "A story. " * 80 + "</p></body></html>")
    for loc, _ in entries[40:]:
        web.routes[loc] = (200, posting("Robot Operator"))
    crawler, _store, kept, client = make(web)
    record = run(crawl(crawler, client, site()))
    assert record.candidates == 43 and len(kept) == 3 and record.pages_seen == cs.SHAPE_TRIES + 3       # 34 story pages were not read


def test_pages_without_job_data_stop_a_crawl_early_and_a_browser_only_site_is_not_kept():
    entries = [(f"{BASE}/jobs/{4471823 + i}/role", day(0)) for i in range(60)]
    shell = "<html><head><title>Careers</title></head><body><div id='app'></div></body></html>"

    def build(browser):
        web = Web({f"{BASE}/sitemap.xml": (200, urlset(entries))})
        for loc, _ in entries:
            web.routes[loc] = (200, shell)
        return (web, *make(web, browser=browser))

    # No browser: after SHAPE_TRIES pages of the one shape without job data the crawl reads no more of them.
    web, crawler, _store, kept, client = build(None)
    record = run(crawl(crawler, client, site()))
    assert (record.state, record.pages_seen, kept) == (cs.NO_US, cs.SHAPE_TRIES, [])
    assert record.error == "6 of 6 posting pages carry no JobPosting data in the first response"
    # A browser that shows the posting: one page is looked at, the site is recorded as needing a browser, nothing is kept.
    shown = Browser({loc: RenderResult(200, loc, posting("Robot Operator")) for loc, _ in entries})
    web, crawler, _store, kept, client = build(shown)
    record = run(crawl(crawler, client, site()))
    assert (record.state, kept, len(shown.rendered), record.browser_pages) == (cs.NEEDS_BROWSER, [], 1, 1)
    assert record.pages_seen == cs.SHAPE_TRIES + 1 and record.us_postings == 0
    web, crawler, _store, kept, client = build(Browser({loc: RenderResult(200, loc, posting("Robot Operator")) for loc, _ in entries}))
    crawler.browser_pages = 0                              # CAREER_SITE_BROWSER_PAGES=0: not even that one look
    record = run(crawl(crawler, client, site()))
    assert (record.state, record.browser_pages, record.pages_seen) == (cs.NO_US, 0, cs.SHAPE_TRIES)
    # A browser that shows nothing more either.
    web, crawler, _store, kept, client = build(Browser())
    record = run(crawl(crawler, client, site()))
    assert record.state == cs.NO_US and "nor in a browser" in record.error and (record.browser_pages, record.pages_seen) == (1, 7)


def test_a_site_whose_postings_name_many_employers_is_a_job_board_and_is_dropped():
    entries = [(f"{BASE}/jobs/{4471823 + i}/role", day(0)) for i in range(30)]
    web = Web({f"{BASE}/sitemap.xml": (200, urlset(entries))})
    for i, (loc, _) in enumerate(entries):
        web.routes[loc] = (200, posting("Robot Operator", company=f"Client Company {i}"))
    crawler, _store, _kept, client = make(web)
    record = run(crawl(crawler, client, site()))
    assert record.state == cs.NOT_A_COMPANY and record.pages_seen == 6 and "different employers" in record.error
    assert cs.next_wait(cs.NOT_A_COMPANY, 1, 7, 90) == 30 * 86400


# ---------------------------------------------------------------- listing pages
def test_without_a_sitemap_posting_addresses_come_from_the_data_of_the_listing_page():
    state = {"props": {"jobs": [{"id": 4471823, "path": "/jobs/4471823/senior-robotics-engineer"},
                                {"id": 4471824, "url": f"{BASE}/jobs/4471824/controls-engineer"}]}}
    listing = ('<html><head><title>Open roles</title><script type="application/ld+json">' + json.dumps({
        "@type": "ItemList", "itemListElement": [{"@type": "JobPosting", "title": "Field Technician", "url": "/jobs/4471825/field-technician-toronto"},
                                                 {"@type": "JobPosting", "title": "Other", "url": "/jobs/4471826/closed-role"}]}) + "</script></head>"
               '<body><div id="app"></div><script id="__NEXT_DATA__" type="application/json">' + json.dumps(state).replace("/", "\\/")
               + "</script></body></html>")
    web = a_site_with_a_sitemap({f"{BASE}/robots.txt": (200, "User-agent: *\nDisallow: /admin\n"), f"{BASE}/jobs": (200, listing)})
    del web.routes[f"{BASE}/sitemap_index.xml"]
    crawler, _store, kept, client = make(web)
    record = run(crawl(crawler, client, site(found=f"{BASE}/jobs/4471823/senior-robotics-engineer")))
    assert (record.method, record.candidates, record.state) == (cs.LISTING_DATA, 4, cs.OK)
    assert sorted(j.title for j in kept) == ["Controls Engineer", "Senior Robotics Engineer"] and record.browser_pages == 0
    assert web.asked()[:3] == [f"{BASE}/robots.txt", f"{BASE}/sitemap.xml", f"{BASE}/jobs"]


def test_plain_links_and_a_plain_next_link_are_followed():
    page1 = ("<html><head><title>Open roles</title></head><body><a href='/jobs/4471823/senior-robotics-engineer'>Senior Robotics Engineer</a>"
             "<a href='/about'>About</a><a href='/jobs?page=2' rel='next'>Next</a><form action='/jobs/search' method='post'><input name='q'></form></body></html>")
    page2 = ("<html><head><title>Open roles</title></head><body><a href='/jobs/4471824/controls-engineer'>Controls Engineer</a>"
             "<a href='https://www.linkedin.com/jobs/view/4471828'>On LinkedIn</a><a href='/jobs?page=3'>Load more</a></body></html>")
    web = a_site_with_a_sitemap({f"{BASE}/robots.txt": (404, ""), f"{BASE}/jobs": (200, page1), f"{BASE}/jobs?page=2": (200, page2)})
    del web.routes[f"{BASE}/sitemap_index.xml"]
    crawler, _store, kept, client = make(web)
    record = run(crawl(crawler, client, site(listing=f"{BASE}/jobs")))
    assert (record.method, record.candidates) == (cs.LISTING_LINKS, 2) and len(kept) == 2
    assert f"{BASE}/jobs?page=2" in web.asked() and f"{BASE}/jobs?page=3" in web.asked() and not [u for u in web.asked() if "search" in u]
    assert {httpx.URL(u).host for u in web.asked()} == {HOST}


def test_a_script_rendered_listing_is_read_in_the_browser_only_when_nothing_else_worked():
    shell = "<html><head><title>Careers</title></head><body><div id='root'></div><script src='/app.js'></script></body></html>"
    rendered = ("<html><head><title>Careers</title></head><body><a href='/jobs/4471823/senior-robotics-engineer'>Senior Robotics Engineer</a>"
                "<a href='/jobs/4471825/field-technician-toronto'>Field Technician</a><a href='/jobs?page=2'>Next</a></body></html>")
    more = "<html><body><a href='/jobs/4471824/controls-engineer'>Controls Engineer</a></body></html>"
    web = a_site_with_a_sitemap({f"{BASE}/robots.txt": (404, ""), f"{BASE}/jobs": (200, shell)})
    del web.routes[f"{BASE}/sitemap_index.xml"]
    browser = Browser({f"{BASE}/jobs": RenderResult(200, f"{BASE}/jobs", rendered), f"{BASE}/jobs?page=2": RenderResult(200, f"{BASE}/jobs?page=2", more)})
    crawler, _store, kept, client = make(web, browser=browser)
    record = run(crawl(crawler, client, site(listing=f"{BASE}/jobs")))
    assert (record.method, record.candidates, record.browser_pages, record.state) == (cs.BROWSER, 3, 2, cs.OK)
    assert browser.rendered == [f"{BASE}/jobs", f"{BASE}/jobs?page=2"] and len(kept) == 2             # posting pages are read by plain requests
    # With a sitemap the browser is never started.
    web = a_site_with_a_sitemap()
    idle = Browser()
    crawler, _store, _kept, client = make(web, browser=idle)
    assert run(crawl(crawler, client, site())).method == cs.SITEMAP and idle.rendered == []
    # CAREER_SITE_BROWSER_PAGES=0: never.
    web = a_site_with_a_sitemap({f"{BASE}/robots.txt": (404, ""), f"{BASE}/jobs": (200, shell)})
    del web.routes[f"{BASE}/sitemap_index.xml"]
    crawler, _store, _kept, client = make(web, browser=browser, career_site_browser_pages=0)
    record = run(crawl(crawler, client, site(listing=f"{BASE}/jobs")))
    assert (record.state, record.browser_pages) == (cs.NO_POSTINGS, 0)


def test_a_site_that_answers_403_or_a_robot_check_is_recorded_as_not_readable_and_left_alone():
    challenge = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
    for answer in ((403, "Forbidden"), (200, challenge)):
        web = Web({f"{BASE}/sitemap.xml": (403, "Forbidden"), f"{BASE}/jobs": answer, f"{BASE}/": answer, f"{BASE}/careers": answer})
        browser = Browser()
        crawler, _store, kept, client = make(web, browser=browser)
        record = run(crawl(crawler, client, site()))
        assert record.state == cs.NOT_READABLE and "left alone" in record.error and kept == [] and browser.rendered == [], answer
        assert web.asked() == [f"{BASE}/robots.txt", f"{BASE}/sitemap.xml", f"{BASE}/"]       # one refused page is enough: no second try
    # 429 ("slow down") on a sitemap or a listing page: not one more request, and the crawl counts as failed (tried again later).
    for routes, asked in (({f"{BASE}/sitemap.xml": (429, "slow down"), f"{BASE}/": (200, "<html></html>")}, 2),
                          ({f"{BASE}/": (429, "slow down"), f"{BASE}/careers": (200, "<html></html>")}, 3)):
        web = Web(routes)
        browser = Browser()
        crawler, _store, kept, client = make(web, browser=browser)
        record = run(crawl(crawler, client, site()))
        assert record.state == cs.FAILED and "429" in record.error and len(web.asked()) == asked and browser.rendered == []
    # The browser meets the robot check: reported as blocked, never worked around.
    shell = "<html><head><title>Careers</title></head><body><div id='root'></div></body></html>"
    web = Web({f"{BASE}/": (200, shell)})
    browser = Browser({f"{BASE}/": RenderResult(403, f"{BASE}/", "", "blocked")})
    crawler, _store, _kept, client = make(web, browser=browser)
    record = run(crawl(crawler, client, site()))
    assert record.state == cs.NOT_READABLE and browser.rendered == [f"{BASE}/"]
    # Posting pages that answer 403: three are enough.
    web = hostile_site(50)
    for url in list(web.routes):
        if "/jobs/" in url:
            web.routes[url] = (403, "Forbidden")
    crawler, _store, _kept, client = make(web)
    record = run(crawl(crawler, client, site()))
    assert record.state == cs.NOT_READABLE and record.pages_seen == 3
    # 429 on a posting page: the crawl stops at once.
    web = hostile_site(50)
    first = next(u for u in web.routes if "/jobs/" in u)
    for url in list(web.routes):
        if "/jobs/" in url:
            web.routes[url] = (429, "slow down")
    crawler, _store, _kept, client = make(web)
    record = run(crawl(crawler, client, site()))
    assert record.state == cs.FAILED and record.pages_seen == 1 and "429" in record.error and first


# ---------------------------------------------------------------- schedule and back-off
def test_waits_grow_and_never_fall_below_the_weekly_interval():
    d = 86400.0
    assert cs.next_wait(cs.OK, 0, 7, 90) == 7 * d and cs.next_wait(cs.OK, 5, 7, 90) == 7 * d
    assert [cs.next_wait(cs.NO_US, n, 7, 90) / d for n in (1, 2, 3, 4, 9)] == [14, 28, 56, 90, 90]
    assert [cs.next_wait(cs.NO_POSTINGS, n, 7, 90) / d for n in (1, 2)] == [14, 28] and cs.next_wait(cs.NEEDS_BROWSER, 1, 7, 90) == 14 * d
    assert [cs.next_wait(cs.FAILED, n, 7, 90) / d for n in (1, 2, 3, 6)] == [7, 14, 28, 90]
    assert [cs.next_wait(cs.NOT_READABLE, n, 7, 90) / d for n in (1, 2, 3)] == [30, 60, 90]
    assert cs.next_wait(cs.NO_US, 3, 0.01, 90) >= d and cs.next_wait(cs.FAILED, 40, 7, 90) == 90 * d
    assert cs.next_wait(cs.NOT_READABLE, 1, 60, 30) == 60 * d                      # a longer interval is never undercut


def test_the_schedule_one_crawl_a_week_and_longer_after_misses():
    web = a_site_with_a_sitemap()
    clock = Clock()
    crawler, store, kept, client = make(web, clock=clock)
    week = 7 * 86400.0

    async def tick_and_wait():
        started = await crawler.tick()
        await asyncio.gather(*list(crawler._running))
        return started

    async def go():
        await store.register(HOST, BASE, "Acme Robotics", "file")
        out = [await tick_and_wait()]                      # a new site is due at once
        out.append(await tick_and_wait())                  # a minute later: not due
        clock.now += week - 60
        out.append(await tick_and_wait())                  # still inside the week
        clock.now += 61
        out.append(await tick_and_wait())                  # a week after the first crawl started: nothing new, postings known -> ok
        # From now on the site lists nothing for the US: the waits double.
        web.routes[f"{BASE}/sitemaps/jobs-1.xml.gz"] = (200, gzip.compress(urlset([(f"{BASE}/jobs/4471825/field-technician-toronto", "")])))
        store.pages.clear()
        store.sites[HOST].seen = ()
        waits = []
        for _ in range(4):
            clock.now = store.next_at[HOST] + 1
            before = clock.now
            await tick_and_wait()
            waits.append(round((store.next_at[HOST] - before) / 86400.0))
        await client.aclose()
        return out, waits

    out, waits = run(go())
    assert out == [1, 0, 0, 1] and len(kept) == 2
    assert waits == [14, 28, 56, 90] and store.misses[HOST] == 4 and store.crawls[-1].state == cs.NO_US


def test_a_claimed_site_is_not_due_again_within_the_interval_even_when_its_crawl_never_finishes():
    clock = Clock()
    crawler, store, _kept, client = make(Web(), clock=clock)

    async def go():
        await store.register(HOST, BASE)
        first = await store.claim(clock.now, 5, 7 * 86400.0)
        again = await store.claim(clock.now + 6 * 86400.0, 5, 7 * 86400.0)
        later = await store.claim(clock.now + 7 * 86400.0, 5, 7 * 86400.0)
        await client.aclose()
        return len(first), len(again), len(later)

    assert run(go()) == (1, 0, 1)


def test_the_daily_budget_and_the_number_of_crawls_at_a_time():
    clock = Clock()

    async def go(**settings):
        web = hostile_site(40)
        crawler, store, kept, client = make(web, clock=clock, **settings)
        for n in range(4):
            host = f"jobs{n}.acme-robotics.com"
            await store.register(host, f"https://{host}", "Acme Robotics", "file")
        started = await crawler.tick()
        budgets = sorted(crawler._running.values())
        again = await crawler.tick()                       # the running crawls hold their share of the day's budget
        await asyncio.gather(*list(crawler._running))
        await client.aclose()
        return started, budgets, again, store

    started, budgets, again, _ = run(go(career_site_pages_per_day=25, career_site_pages_per_crawl=20, career_site_concurrency=3))
    assert (started, budgets, again) == (1, [20], 0)       # 25 pages a day: one crawl of 20 now; 5 left is below the minimum
    started, budgets, again, _ = run(go(career_site_pages_per_day=50, career_site_pages_per_crawl=20, career_site_concurrency=3))
    assert (started, budgets, again) == (3, [10, 20, 20], 0)                                 # the third gets what is left of the day
    started, budgets, again, _ = run(go(career_site_pages_per_day=1000, career_site_pages_per_crawl=20, career_site_concurrency=2))
    assert (started, budgets, again) == (2, [20, 20], 0)   # two at a time
    started, _budgets, _again, _ = run(go(career_site_crawl=False))
    assert started == 0
    # What was read in the last 24 hours counts against the day.
    async def spent():
        crawler, store, _kept, client = make(Web(), clock=clock, career_site_pages_per_day=100)
        await store.register(HOST, BASE)
        store.crawls.append(cs.CrawlRecord(host="x.example.com", started_at=clock.now - 3600, pages_seen=95))
        first = await crawler.tick()
        store.crawls[0].started_at = clock.now - 90000
        second = await crawler.tick()
        await asyncio.gather(*list(crawler._running))
        await client.aclose()
        return first, second
    assert run(spent()) == (0, 1)


def test_a_failed_crawl_is_recorded_and_never_raises():
    def boom(request):
        raise httpx.ConnectError("no route")
    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    reader = cs.SiteReader(client)
    reader.host_gap = 0.0
    store = MemoryStore()

    async def remember(jobs):
        return 0

    async def go():
        crawler = cs.Crawler(store, reader, remember, Settings(_env_file=None))
        await store.register(HOST, BASE)
        record = await crawler.run(store.sites[HOST], 10)
        await client.aclose()
        return record

    record = run(go())
    assert record.state == cs.FAILED and record.error == "robots.txt could not be read" and store.misses[HOST] == 1
    assert store.next_at[HOST] == pytest.approx(record.started_at + 7 * 86400.0)


# ---------------------------------------------------------------- hooks
def job(url: str, company: str = "Acme Robotics") -> Job:
    return Job(title="Senior Robotics Engineer", company=company, url=url, source="Claude web search", location="Austin, TX",
               verification="Posting page fetched; schema.org JobPosting data present and not expired")


def test_a_verified_posting_on_an_employers_own_host_registers_the_site_once():
    crawler, store, _kept, client = make(Web())

    async def go():
        out = [await crawler.note_posting(job(f"https://www.{HOST}/jobs/4471823/senior-robotics-engineer#top")),
               await crawler.note_posting(job(f"{BASE}/jobs/4471824/controls-engineer")),
               await crawler.note_posting(job("https://boards.greenhouse.io/acme/jobs/4471823")),
               await crawler.note_posting(job("https://acme.applytojob.com/apply/Ab3dEf9hIj/Engineer")),
               await crawler.note_posting(job("https://careers-acme.icims.com/jobs/4471823/job")),
               await crawler.note_posting(job("https://www.linkedin.com/jobs/view/4471823")),
               await crawler.note_posting(job("https://www.builtin.com/job/4471823")),
               await crawler.note_posting(job("http://plain.acme-robotics.com/jobs/4471823")),
               await crawler.note_posting(job("https://10.0.0.5/jobs/4471823")),
               await crawler.note_posting(job("not a url"))]
        await client.aclose()
        return out

    assert run(go()) == [True] + [False] * 9
    assert list(store.sites) == [HOST]
    kept = store.sites[HOST]
    assert (kept.base_url, kept.employer, kept.origin) == (f"https://www.{HOST}", "Acme Robotics", "search")
    assert kept.found_url == f"https://www.{HOST}/jobs/4471823/senior-robotics-engineer"


def test_the_engine_hands_remembered_postings_to_the_crawler_and_the_background_reader_starts_it(make_engine):
    engine = make_engine(sources=("greenhouse", "lever", "ashby", CAREER_PAGE))
    assert engine.career_sites is None                     # the single-user SQLite index has no background reader: no crawler
    store = MemoryStore()
    ticks = []

    class Recorder(cs.Crawler):
        async def tick(self):
            ticks.append(1)
            return 0

    async def go():
        engine.career_sites = Recorder(store, cs.SiteReader(engine.client), engine._remember_pages, engine.settings)
        kept = await engine._remember_pages([job("https://careers.example.com/jobs/ai-9", "Example Careers Inc"),
                                             job("https://careers.example.com/jobs/ml-1", "Example Careers Inc")])

        class Index:
            async def catalog_due(self, *a, **k):
                return []

            async def catalog_touch(self, due):
                return None
        engine.index = Index()
        await board_catalog.catalog_tick(engine, {})
        engine._inflight = {i: None for i in range(board_catalog.MAX_INFLIGHT + 1)}       # a busy engine starts nothing
        await board_catalog.catalog_tick(engine, {})
        engine._inflight = {}
        await engine.aclose()
        return kept

    assert run(go()) == 2 and list(store.sites) == ["careers.example.com"] and ticks == [1]
    assert store.sites["careers.example.com"].employer == "Example Careers Inc"


def test_a_seed_file_is_validated_row_by_row(tmp_path):
    path = tmp_path / "sites.csv"
    path.write_text("host,employer,listing\n"
                    "careers.acme-robotics.com,Acme Robotics,/openings\n"
                    "https://www.example-corp.com/careers,Example Corp,https://www.example-corp.com/careers/all\n"
                    "CAREERS.ACME-ROBOTICS.COM,Acme again,\n"
                    "boards.greenhouse.io,Vendor,\n"
                    "www.linkedin.com,LinkedIn,\n"
                    "builtin.com,Built In,\n"
                    "10.0.0.5,Internal,\n"
                    "localhost,Local,\n"
                    "http://plain.example-corp.com,Plain,\n"
                    "jobs.example-corp.com:8443,Port,\n"
                    "jobs.example-corp.com,Wrong listing,https://elsewhere.com/jobs\n"
                    "# a comment,,\n"
                    ",,\n", encoding="utf-8")
    rows, refused = cs.read_seed_file(path, ("builtin.com",))
    assert rows == [("careers.acme-robotics.com", "https://careers.acme-robotics.com", "Acme Robotics", "https://careers.acme-robotics.com/openings"),
                    ("example-corp.com", "https://www.example-corp.com", "Example Corp", "https://www.example-corp.com/careers/all")]
    assert len(refused) == 9 and "listed twice" in refused[0] and "career platform" in refused[1] and "never fetched" in refused[2]
    assert "job board" in refused[3] and "not a public host name" in refused[4] and "only https" in refused[6] and "listing address" in refused[8]


# ---------------------------------------------------------------- what the live sites taught (2026-11-21)
def test_a_listing_that_moved_to_another_host_is_not_followed_there():
    web = Web({f"{BASE}/": (302, "", {"location": "https://www.other-brand.com/careers/us/"}),
               f"{BASE}/careers": (301, "", {"location": "https://acme-robotics.com/company/careers"}),      # the parent domain is another host too
               f"{BASE}/jobs": (301, "", {"location": f"{BASE}/jobs/"}),
               f"{BASE}/jobs/": (200, "<html><body><a href='/jobs/4471823/senior-robotics-engineer'>Senior Robotics Engineer</a></body></html>"),
               f"{BASE}/jobs/4471823/senior-robotics-engineer": (200, posting("Senior Robotics Engineer")),
               "https://www.other-brand.com/careers/us/": (200, "<html><body><a href='/jobs/1234567/x'>X</a></body></html>"),
               "https://acme-robotics.com/company/careers": (200, "<html><body>Careers</body></html>")})
    crawler, _store, kept, client = make(web, browser=Browser())
    record = run(crawl(crawler, client, site()))
    assert {httpx.URL(u).host for u in web.asked()} == {HOST} and len(kept) == 1 and record.method == cs.LISTING_LINKS
    assert web.asked() == [f"{BASE}/robots.txt", f"{BASE}/sitemap.xml", f"{BASE}/", f"{BASE}/careers", f"{BASE}/jobs", f"{BASE}/jobs/",
                           f"{BASE}/jobs/4471823/senior-robotics-engineer"]


def test_a_career_home_page_that_points_to_its_job_list_is_followed_there():
    home = ("<html><head><title>Careers at Acme</title></head><body><a href='/life'>Life at Acme</a><a href='/all-jobs/'>View all jobs</a>"
            "<a href='/apply'>Apply</a><a href='https://www.linkedin.com/company/acme'>LinkedIn</a></body></html>")
    listed = "<html><body><a href='/detail/4471823/?gh_jid=4471823'>Senior Robotics Engineer</a></body></html>"
    web = Web({f"{BASE}/": (200, home), f"{BASE}/all-jobs/": (200, listed),
               f"{BASE}/detail/4471823/?gh_jid=4471823": (200, posting("Senior Robotics Engineer"))})
    crawler, _store, kept, client = make(web)
    record = run(crawl(crawler, client, site()))
    assert [j.title for j in kept] == ["Senior Robotics Engineer"] and record.method == cs.LISTING_LINKS
    assert web.asked()[2:] == [f"{BASE}/", f"{BASE}/all-jobs/", f"{BASE}/detail/4471823/?gh_jid=4471823"]
    # The same when the job list needs scripts: the browser looks at the page the home page pointed to.
    shell = "<html><head><title>Jobs</title></head><body><div id='root'></div></body></html>"
    web = Web({f"{BASE}/": (200, home), f"{BASE}/all-jobs/": (200, shell),
               f"{BASE}/detail/4471823/?gh_jid=4471823": (200, posting("Senior Robotics Engineer"))})
    browser = Browser({f"{BASE}/all-jobs/": RenderResult(200, f"{BASE}/all-jobs/", listed)})
    crawler, _store, kept, client = make(web, browser=browser)
    record = run(crawl(crawler, client, site()))
    assert browser.rendered == [f"{BASE}/all-jobs/"] and len(kept) == 1 and (record.method, record.browser_pages) == (cs.BROWSER, 1)


def test_a_sitemap_that_names_no_real_posting_is_followed_by_the_listing_page():
    program = "<html><head><title>Development program</title></head><body><h1>Our program</h1><p>" + "Learn with us. " * 60 + "</p></body></html>"
    listed = "<html><body><a href='/careers/job/senior-robotics-engineer-4471823'>Senior Robotics Engineer</a></body></html>"
    web = Web({f"{BASE}/sitemap.xml": (200, urlset([(f"{BASE}/careers/manufacturing-development-program", "")])),
               f"{BASE}/careers/manufacturing-development-program": (200, program),
               f"{BASE}/careers": (200, listed),
               f"{BASE}/careers/job/senior-robotics-engineer-4471823": (200, posting("Senior Robotics Engineer"))})
    crawler, _store, kept, client = make(web)
    record = run(crawl(crawler, client, site(listing=f"{BASE}/careers")))
    assert [j.title for j in kept] == ["Senior Robotics Engineer"] and (record.method, record.candidates, record.pages_seen, record.state) == (
        cs.LISTING_LINKS, 2, 2, cs.OK) and record.error == ""
    # When the only pages the site names answer 403, the site is not readable; nothing is tried around it.
    web = Web({f"{BASE}/sitemap.xml": (200, urlset([(f"{BASE}/careers/manufacturing-development-program", "")])),
               f"{BASE}/careers/manufacturing-development-program": (403, "Access Denied"), f"{BASE}/careers": (403, "Access Denied"),
               f"{BASE}/": (403, "Access Denied"), f"{BASE}/jobs": (403, "Access Denied")})
    browser = Browser()
    crawler, _store, kept, client = make(web, browser=browser)
    record = run(crawl(crawler, client, site()))
    assert record.state == cs.NOT_READABLE and kept == [] and browser.rendered == [] and len(web.asked()) == 3


def test_a_posting_that_leads_to_another_host_is_not_stored_as_this_sites():
    web = Web({f"{BASE}/sitemap.xml": (200, urlset([(f"{BASE}/jobs/4471823/senior-robotics-engineer", ""), (f"{BASE}/jobs/4471824/controls-engineer", "")])),
               f"{BASE}/jobs/4471823/senior-robotics-engineer": (200, posting("Senior Robotics Engineer")),
               f"{BASE}/jobs/4471824/controls-engineer": (302, "", {"location": "https://jobs.partner-site.com/jobs/4471824/controls-engineer"}),
               "https://jobs.partner-site.com/jobs/4471824/controls-engineer": (200, posting("Controls Engineer", company="Partner Site"))})
    crawler, _store, kept, client = make(web)
    record = run(crawl(crawler, client, site()))
    assert [j.title for j in kept] == ["Senior Robotics Engineer"] and record.postings == 1
    assert record.reasons["Posting address leads to another host (not this site's)"] == 1


def test_a_posting_whose_own_data_names_another_country_is_not_a_us_posting():
    def page(places, **extra):
        node = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Channel Marketing Manager", "datePosted": day(3),
                "hiringOrganization": {"@type": "Organization", "name": "Acme Robotics"}, "description": "Marketing. 5+ years of experience.",
                "jobLocation": [{"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": c, "addressRegion": r,
                                                                "addressCountry": {"@type": "Country", "name": k}}} for c, r, k in places], **extra}
        return f'<html><head><title>Channel Marketing Manager</title><script type="application/ld+json">{json.dumps(node)}</script></head><body>Apply</body></html>'

    pages = {"co": page([("Bogotá", "Bogota,CO", "CO"), ("México D.F.", "CDMX,MX", "MX")]),          # Colombia, not Colorado
             "ca": page([("Goodwood", "ON,CA", "CA")]),                                               # Canada, not California
             "us": page([("Redmond", "WA,US", "US"), ("Vancouver", "BC,CA", "CA")]),                  # one of its places is in the US
             "remote-ca": page([], applicantLocationRequirements={"@type": "Country", "name": "Canada"}, jobLocationType="TELECOMMUTE")}
    web = Web({f"{BASE}/sitemap.xml": (200, urlset([(f"{BASE}/jobs/44718{i}/{k}", "") for i, k in enumerate(pages)]))})
    for i, (k, body) in enumerate(pages.items()):
        web.routes[f"{BASE}/jobs/44718{i}/{k}"] = (200, body)
    crawler, _store, kept, client = make(web)
    record = run(crawl(crawler, client, site()))
    assert (record.postings, record.us_postings) == (4, 1) and [j.url.rsplit("/", 1)[1] for j in kept] == ["us"]
    assert kept[0].country_hint == "United States"


def test_a_running_crawl_stands_back_while_the_engine_is_busy(monkeypatch):
    monkeypatch.setattr(cs, "BUSY_PAUSE", 0.01)
    web = a_site_with_a_sitemap()
    crawler, _store, kept, client = make(web)
    asked_when_busy = []
    state = {"calls": 0}

    def busy():
        state["calls"] += 1
        still = state["calls"] <= 5
        if still:
            asked_when_busy.append(len([u for u in web.asked() if "/jobs/" in u]))
        return still
    crawler.busy = busy
    record = run(crawl(crawler, client, site()))
    assert asked_when_busy == [0] * 5 and record.pages_seen == 5 and len(kept) == 2          # no page while busy, all of them afterwards
