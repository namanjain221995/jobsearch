"""Startup ATS board readers (Recruitee, Pinpoint, Rippling): parsing, pagination, missing boards, malformed answers,
address parsing, slug validation, registry, catalogue and discovery. No network: every transport is a mock.

The fixtures in tests/fixtures/boards/ are real answers of 2026-11-21 (Namecheap's Recruitee board, Pinpoint's own
board, Rippling's own board), trimmed: long HTML is cut, application-form questions are reduced to one, the job mailbox
address is dropped and a first name in one description is removed. The Rippling list pages keep real rows but carry
page counters for a two-page board."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app import board_catalog
from app.config import Settings
from app.database import Database
from app.job_normalizer import ats_key, enrich_job, parse_ats, us_location_status
from app.providers import ats_startup, registry
from app.providers.base import BoardNotFound, Ctx, HostLimiter, SourceError
from app.providers.searxng import BOARD_SOURCES, extract, site_hosts, valid_board
from app.schemas import Job

FIX = Path(__file__).parent / "fixtures" / "boards"
UUID = "96601a74-e7b6-469d-8c69-6fdfd1c83bc0"


def load(name):
    return json.loads((FIX / name).read_text())


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    async def ok(url):
        return "93.184.216.34"
    monkeypatch.setattr(ats_startup, "_guard", ok)


def make_ctx(routes, calls=None, limiter=None, options=None):
    def handler(request: httpx.Request):
        if calls is not None:
            calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=routes.get("robots", "User-agent: *\nDisallow:\n"))
        for match, resp in routes.items():
            if match != "robots" and match in str(request.url):
                r = resp(request) if callable(resp) else resp
                return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)
        return httpx.Response(404, json={})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    unpaced = HostLimiter(intervals={name: 0.0 for name in ats_startup.HOST_LIMITS})        # tests do not wait a second per request
    return Ctx(client, limiter or unpaced, options=options or {})


# ---------------------------------------------------------------- Recruitee
def test_recruitee_reads_the_whole_board_in_one_request():
    calls = []
    jobs = run(ats_startup.fetch_recruitee(make_ctx({"/api/offers/": load("recruitee_offers.json")}, calls), "namecheap"))
    assert [j.ats_id for j in jobs] == ["namecheap:sr-director-incubation-hub", "namecheap:payment-administrator",
                                        "namecheap:financial-controller-3"]
    assert calls == ["https://namecheap.recruitee.com/robots.txt", "https://namecheap.recruitee.com/api/offers/"]
    us = jobs[1]
    assert us.title == "Payment Administrator" and us.company == "Namecheap Inc" and us.board == "namecheap"
    assert us.location == "Florida, United States" and us.country_hint == "United States" and us.work_mode == "Remote"
    assert us_location_status(us.location, us.country_hint)[0]
    assert us.posted_at == "2026-11-06" and us.salary == "Not listed"            # the answer's salary object is empty
    assert us.url == "https://namecheap.recruitee.com/o/payment-administrator"
    assert us.apply_url == "https://namecheap.recruitee.com/o/payment-administrator/c/new"
    assert us.source == "Recruitee" and us.sources == ["Recruitee"] and us.ats == "recruitee" and not us.needs_detail
    assert jobs[0].location == "Lisbon, Lisboa, Portugal" and jobs[0].country_hint == "Portugal" and jobs[0].posted_at == "2026-11-20"
    enrich_job(us)                                                               # the description came with the list
    assert us.description.startswith("Namecheap, Inc. is looking for") and "<" not in us.description and not us.raw_html
    # A posting address found by a search engine names exactly this record.
    assert parse_ats(us.url) == ("recruitee", "namecheap", us.ats_id) == parse_ats(us.apply_url)


def test_recruitee_details_salary_modes_and_unpublished():
    base = load("recruitee_offers.json")["offers"][1]
    job = ats_startup.recruitee_job("acme", {**base, "remote": False, "hybrid": True,
                                             "salary": {"min": "90000", "max": "120000.0", "currency": "USD", "period": "year"}}, "T")
    assert job.work_mode == "Hybrid" and job.salary == "USD 90,000 - 120,000 / year" and job.ats_id == "acme:payment-administrator"
    on_site = ats_startup.recruitee_job("acme", {**base, "remote": False, "on_site": True, "locations": [], "city": "Austin",
                                                 "state_name": "Texas", "country": "United States", "country_code": "US"}, "T")
    assert on_site.work_mode == "On-site" and on_site.location == "Austin, Texas, United States" and on_site.country_hint == "United States"
    assert ats_startup.recruitee_job("acme", {**base, "published_at": None}, "T").posted_at == ""        # never invented
    for broken in ({**base, "status": "draft"}, {**base, "title": " "}, {**base, "slug": "../x"}, {**base, "careers_url": "http://x/o/y"},
                   {**base, "careers_url": None}, "text", None, 7):
        assert ats_startup.recruitee_job("acme", broken, "T") is None
    odd = ats_startup.recruitee_job("acme", {**base, "locations": "x", "salary": {"min": "a lot", "max": 5}, "careers_apply_url": "javascript:x"}, "T")
    assert odd.salary == "Not listed" and odd.apply_url == "" and odd.location == "Florida, United States"


# ---------------------------------------------------------------- Pinpoint
def test_pinpoint_reads_the_feed_without_inventing_a_date_or_country():
    jobs = run(ats_startup.fetch_pinpoint(make_ctx({"/postings.json": load("pinpoint_postings.json")}), "workwithus"))
    assert [j.ats_id for j in jobs] == ["fa79e602-e5a1-4ee7-9cee-3bb2a13eb20a", "207471c2-3297-4f76-8307-6ec844b20760",
                                        "6fc547ee-7139-41eb-92c8-add28d89e8c5"]
    first = jobs[0]
    assert first.title == "Staff Product Designer" and first.location == "London (Remote)" and first.work_mode == "Remote"
    assert jobs[1].location == "London (United Kingdom)"
    assert first.posted_at == "" and first.country_hint == "" and first.salary == "Not listed"       # compensation is not visible
    assert first.url == first.apply_url == "https://workwithus.pinpointhq.com/en/postings/fa79e602-e5a1-4ee7-9cee-3bb2a13eb20a"
    assert first.source == "Pinpoint" and first.ats == "pinpoint" and first.board == "workwithus" and not first.needs_detail
    assert parse_ats(first.url) == ("pinpoint", "workwithus", first.ats_id)
    enrich_job(first)
    assert "What you'll do" in first.description_rich and "What you'll bring" in first.description_rich and "What we offer" in first.description_rich


def test_pinpoint_modes_salary_and_malformed_rows():
    base = load("pinpoint_postings.json")["data"][0]
    job = ats_startup.pinpoint_job("acme", {**base, "workplace_type": "hybrid", "compensation_visible": True, "compensation": "$120,000 - $150,000 / year",
                                            "location": {"city": "Austin", "province": "Texas", "name": "Austin HQ"}}, "T")
    assert job.work_mode == "Hybrid" and job.salary == "$120,000 - $150,000 / year" and job.location == "Austin, Texas (Austin HQ)"
    assert ats_startup.pinpoint_job("acme", {**base, "workplace_type": "onsite", "location": None}, "T").location == "Not listed"
    fallback = ats_startup.pinpoint_job("acme", {**base, "url": "https://careers.acme.example/roles/designer"}, "T")
    assert fallback.ats_id == "acme:591998"                                      # no posting uuid in the address: the feed's own id
    for broken in ({**base, "title": ""}, {**base, "url": "http://x/postings/1"}, {**base, "url": "https://x/y", "id": None}, [], None):
        assert ats_startup.pinpoint_job("acme", broken, "T") is None


def test_a_tenant_whose_robots_txt_forbids_the_feed_is_not_read():
    calls = []
    ctx = make_ctx({"robots": "User-Agent: *\nDisallow: /\n", "/postings.json": load("pinpoint_postings.json")}, calls)
    with pytest.raises(SourceError, match="robots.txt disallows"):
        run(ats_startup.fetch_pinpoint(ctx, "ada"))
    assert calls == ["https://ada.pinpointhq.com/robots.txt"]                     # the feed itself was never requested


# ---------------------------------------------------------------- Rippling
def test_rippling_pages_are_followed_and_rows_merged_per_posting():
    calls = []
    ctx = make_ctx({"page=0": load("rippling_list_page0.json"), "page=1": load("rippling_list_page1.json")}, calls)
    jobs = run(ats_startup.fetch_rippling(ctx, "rippling"))
    assert len(jobs) == 6 and len({j.ats_id for j in jobs}) == 6                  # 7 rows: one posting is listed in two cities
    assert [c for c in calls if "/api/v2/" in c] == [
        "https://ats.rippling.com/api/v2/board/rippling/jobs?page=0&pageSize=100",
        "https://ats.rippling.com/api/v2/board/rippling/jobs?page=1&pageSize=100"]
    first = jobs[0]
    assert first.title == "Business Operations Manager" and first.location == "New York, NY; San Francisco, CA"
    assert first.work_mode == "On-site" and first.country_hint == "United States" and first.posted_at == ""
    assert first.url == first.apply_url == "https://ats.rippling.com/rippling/jobs/84d388b6-7656-434c-8862-0312eb6b97ac"
    assert first.needs_detail and first.detail_ref == first.ats_id and first.source == "Rippling" and first.ats == "rippling"
    assert parse_ats(first.url) == ("rippling", "rippling", first.ats_id)
    by_title = {j.title: j for j in jobs}
    assert by_title["Staff Technical Program Manager"].country_hint == "India"
    remote = by_title["Customer Experience Operations Manager, Business Unit"]
    assert remote.work_mode == "Remote" and remote.location == "Remote (United States)" and remote.country_hint == "United States"


def test_rippling_page_limit_and_empty_board():
    calls = []
    ctx = make_ctx({"page=0": load("rippling_list_page0.json"), "page=1": load("rippling_list_page1.json")}, calls, options={"rippling_pages": 1})
    assert len(run(ats_startup.fetch_rippling(ctx, "rippling"))) == 3 and sum("/api/v2/" in c for c in calls) == 1
    empty = make_ctx({"/api/v2/": {"items": [], "page": 0, "pageSize": 100, "totalItems": 0, "totalPages": 0}})
    assert run(ats_startup.fetch_rippling(empty, "quiet-co")) == []


def test_rippling_detail_brings_description_and_date_and_404_means_closed():
    ctx = make_ctx({"page=0": load("rippling_list_page0.json"), "page=1": load("rippling_list_page1.json"),
                    f"/jobs/{UUID}": load("rippling_detail.json")})
    job = run(ats_startup.fetch_rippling(ctx, "rippling"))[0]
    job.detail_ref = job.ats_id = UUID                                           # the posting the detail fixture describes
    done = run(ats_startup.detail_rippling(ctx, job))
    assert done is job and not job.needs_detail and job.posted_at == "2026-08-12" and job.company == "Rippling"
    assert "About the role" in job.raw_html
    gone = Job(title="x", company="Rippling", source="Rippling", url="https://ats.rippling.com/rippling/jobs/" + UUID.replace("9", "8"),
               board="rippling", ats="rippling", needs_detail=True, detail_ref=UUID.replace("9", "8"))
    assert run(ats_startup.detail_rippling(ctx, gone)) is None                   # 404: the board no longer has it
    gone.detail_ref = "../../x"
    assert run(ats_startup.detail_rippling(ctx, gone)) is None                   # never placed into an address
    bad = make_ctx({f"/jobs/{UUID}": ["not", "a", "posting"]})
    job.needs_detail = True
    with pytest.raises(SourceError):
        run(ats_startup.detail_rippling(bad, job))


def test_rippling_malformed_rows_are_skipped():
    rows = [{"id": "not-a-uuid", "name": "x"}, {"id": UUID, "name": ""}, "text", None,
            {"id": UUID.upper(), "name": "Engineer", "locations": "nowhere"}, {"id": UUID, "name": "Engineer", "locations": [None, {"name": "Austin, TX", "countryCode": "us"}]}]
    jobs = ats_startup.rippling_jobs("acme", rows, "T")
    assert len(jobs) == 1 and jobs[0].ats_id == UUID and jobs[0].location == "Austin, TX" and jobs[0].country_hint == "United States"
    assert jobs[0].work_mode == "Not listed" and jobs[0].company == "Acme"


# ---------------------------------------------------------------- shared behaviour
READERS = [("recruitee", ats_startup.fetch_recruitee, "acme"), ("pinpoint", ats_startup.fetch_pinpoint, "acme"),
           ("rippling", ats_startup.fetch_rippling, "acme-co")]


@pytest.mark.parametrize("src,fetch,key", READERS)
def test_a_board_that_does_not_exist_is_board_not_found(src, fetch, key):
    with pytest.raises(BoardNotFound):
        run(fetch(make_ctx({}), key))


@pytest.mark.parametrize("src,fetch,key", READERS)
def test_malformed_answers_are_source_errors_not_empty_boards(src, fetch, key):
    for body in ([], {"unexpected": 1}, {"offers": "x", "data": "x", "items": "x"}, "text"):
        ctx = make_ctx({"/": lambda request, body=body: httpx.Response(200, json=body)})
        with pytest.raises(SourceError) as info:
            run(fetch(ctx, key))
        assert not isinstance(info.value, BoardNotFound)
    html = make_ctx({"/": lambda request: httpx.Response(200, text="<html>maintenance</html>")})
    with pytest.raises(SourceError, match="Invalid JSON"):
        run(fetch(html, key))


@pytest.mark.parametrize("src,fetch,key", READERS)
def test_malformed_keys_never_reach_the_network(src, fetch, key):
    calls = []
    ctx = make_ctx({}, calls)
    for bad in ("", "../etc", "a/b", "a.b", "evil.com#", "A B", "-acme", "acme-", "www", "api", "xn--80ak6aa92e", "a" * 120, "acme%2f", "acme?x=1", "ACME"):
        with pytest.raises(BoardNotFound):
            run(fetch(ctx, bad))
    assert calls == []


@pytest.mark.parametrize("src,fetch,key", READERS)
def test_every_request_passes_the_address_guard_and_the_shared_pace(src, fetch, key, monkeypatch):
    seen = []

    async def refuse(url):
        seen.append(url)
        raise ValueError("Hostname resolves to a non-public address")
    monkeypatch.setattr(ats_startup, "_guard", refuse)
    calls = []
    limiter = HostLimiter()
    ctx = make_ctx({}, calls, limiter)
    with pytest.raises(ValueError):
        run(fetch(ctx, key))
    assert len(seen) == 1 and seen[0].startswith("https://") and calls == []      # refused before any request

    async def ok(url):
        return "93.184.216.34"
    monkeypatch.setattr(ats_startup, "_guard", ok)
    with pytest.raises(BoardNotFound):
        run(fetch(ctx, key))
    assert limiter.intervals[src] == 1.0 and limiter.limits[src] == 2            # one request per second per platform


def test_throttling_pauses_the_whole_platform():
    async def go():
        ctx = make_ctx({"/api/offers/": lambda request: httpx.Response(429, headers={"retry-after": "0"})},
                       limiter=HostLimiter(intervals={"recruitee": 0.01}))
        with pytest.raises(SourceError, match="HTTP 429"):
            await ats_startup.fetch_recruitee(ctx, "acme")
        return ctx.limiter._next["recruitee"] - asyncio.get_running_loop().time()
    assert run(go()) > 500                                                       # left alone for ten minutes


# ---------------------------------------------------------------- addresses
@pytest.mark.parametrize("url,expected", [
    ("https://namecheap.recruitee.com", ("recruitee", "namecheap", "")),
    ("https://Namecheap.Recruitee.com/o/Full-Stack-Engineer-1?utm_source=x", ("recruitee", "namecheap", "namecheap:full-stack-engineer-1")),
    ("https://namecheap.recruitee.com/o/full-stack-engineer-1/c/new", ("recruitee", "namecheap", "namecheap:full-stack-engineer-1")),
    ("https://namecheap.recruitee.com/l/nl/o/full-stack-engineer-1", ("recruitee", "namecheap", "namecheap:full-stack-engineer-1")),
    ("https://namecheap.recruitee.com/o/..%2fx", ("recruitee", "namecheap", "")),
    ("https://workwithus.pinpointhq.com/", ("pinpoint", "workwithus", "")),
    ("https://workwithus.pinpointhq.com/en/postings/FA79E602-e5a1-4ee7-9cee-3bb2a13eb20a", ("pinpoint", "workwithus", "fa79e602-e5a1-4ee7-9cee-3bb2a13eb20a")),
    ("https://workwithus.pinpointhq.com/postings/fa79e602-e5a1-4ee7-9cee-3bb2a13eb20a/applications/new", ("pinpoint", "workwithus", "fa79e602-e5a1-4ee7-9cee-3bb2a13eb20a")),
    ("https://workwithus.pinpointhq.com/en/postings/123", ("pinpoint", "workwithus", "")),
    ("https://ats.rippling.com/greentown-labs/jobs", ("rippling", "greentown-labs", "")),
    (f"https://ats.rippling.com/Greentown-Labs/jobs/{UUID}", ("rippling", "greentown-labs", UUID)),
    (f"https://ats.rippling.com/en-GB/greentown-labs/jobs/{UUID}/apply", ("rippling", "greentown-labs", UUID)),
    ("https://ats.rippling.com/go-to/jobs", ("rippling", "go-to", "")),          # a slug that looks like a locale
    ("https://ats.rippling.com/greentown-labs/jobs/123", ("rippling", "greentown-labs", "")),
])
def test_parse_ats_board_and_posting_addresses(url, expected):
    assert parse_ats(url) == expected


@pytest.mark.parametrize("url", [
    "https://recruitee.com/pricing", "https://www.recruitee.com/o/x", "https://careers.recruitee.com/o/x", "https://docs.recruitee.com/reference/offers",
    "https://a.b.recruitee.com/o/x", "https://recruitee.com.evil.example/o/x", "https://evilrecruitee.com/o/x", "https://-x.recruitee.com/",
    "https://www.pinpointhq.com/", "https://developers.pinpointhq.com/docs/jobs-json-endpoint", "https://xn--80ak6aa92e.pinpointhq.com/",
    "https://ats.rippling.com/", "https://ats.rippling.com/api/v2/board/x/jobs", "https://ats.rippling.com/internal/x",
    "https://app.rippling.com/x/jobs", "https://www.rippling.com/careers",
])
def test_parse_ats_ignores_vendor_pages_and_lookalikes(url):
    assert parse_ats(url) is None


def test_posting_ids_do_not_collide_between_employers():
    a, b = parse_ats("https://acme.recruitee.com/o/engineer"), parse_ats("https://other.recruitee.com/o/engineer")
    assert ats_key(a[0], a[2]) == "recruitee:acme:engineer" != ats_key(b[0], b[2])


def test_valid_board_is_strict():
    assert all(src in BOARD_SOURCES for src in ("recruitee", "pinpoint", "rippling"))
    assert valid_board("recruitee", "namecheap") and valid_board("pinpoint", "work-with-us") and valid_board("rippling", "greentown-labs")
    assert valid_board("rippling", "acme_co2") and not valid_board("recruitee", "acme_co")       # a subdomain has no underscore
    for src in ("recruitee", "pinpoint", "rippling"):
        for bad in ("", ".", "..", "a.b", "a/b", "-a", "a-", "A", "www", "api", "careers", "jobs", "a b", "a%2fb", "xn--abc", "a" * 101, "é"):
            assert not valid_board(src, bad), (src, bad)
    assert not valid_board("recruitee", "a" * 64) and valid_board("recruitee", "a" * 63)


def hit(url, hosts):
    found = extract(url, hosts)
    return (found.kind, found.src, found.board, found.job_id)


def test_search_engine_results_on_these_hosts_become_board_and_posting_hits():
    hosts = site_hosts("recruitee.com,pinpointhq.com,ats.rippling.com")
    assert hit("https://namecheap.recruitee.com/", hosts) == ("board", "recruitee", "namecheap", "")
    assert hit("https://namecheap.recruitee.com/o/ai-engineer", hosts) == ("posting", "recruitee", "namecheap", "namecheap:ai-engineer")
    assert hit(f"https://workwithus.pinpointhq.com/en/postings/{UUID}", hosts) == ("posting", "pinpoint", "workwithus", UUID)
    assert hit("https://ats.rippling.com/greentown-labs/jobs", hosts) == ("board", "rippling", "greentown-labs", "")
    assert hit(f"https://ats.rippling.com/greentown-labs/jobs/{UUID}", hosts) == ("posting", "rippling", "greentown-labs", UUID)
    for url in ("https://careers.recruitee.com/", "https://recruitee.com/blog", "http://namecheap.recruitee.com/", "https://namecheap.recruitee.com:8443/",
                "https://ats.rippling.com/api/v2/board/x/jobs", "https://ats.rippling.com/careers/jobs", "https://user@namecheap.recruitee.com/",
                "https://namecheap.recruitee.com.evil.example/", "https://ats.rippling.com/%2e%2e/jobs"):
        assert extract(url, hosts) is None, url
    assert extract("https://namecheap.recruitee.com/", site_hosts("jobs.lever.co")) is None     # host not configured
    default = site_hosts(Settings(_env_file=None).searxng_sites)
    assert "recruitee.com" in default
    assert "ats.rippling.com" not in default and "pinpointhq.com" not in default     # Rippling and Pinpoint are off: no queries for them


# ---------------------------------------------------------------- registry, switches, catalogue
def test_registry_has_the_three_readers_and_pinpoint_and_rippling_are_off_by_default():
    for name, label in (("recruitee", "Recruitee"), ("pinpoint", "Pinpoint"), ("rippling", "Rippling")):
        spec = registry.SPECS[name]
        assert spec.label == label and spec.kind == "board" and spec.keyed and name in registry.BOARD_LEAD_SOURCES
    assert registry.SPECS["rippling"].detail is ats_startup.detail_rippling
    assert registry.SPECS["recruitee"].detail is None and registry.SPECS["pinpoint"].detail is None
    assert Settings(_env_file=None).rippling_enabled is False and Settings(_env_file=None).pinpoint_enabled is False
    default = registry.enabled_specs(Settings(_env_file=None))
    assert "recruitee" in default and "rippling" not in default and "pinpoint" not in default
    # Rippling is off until its terms are reviewed: nothing plans a Rippling board, even one the index knows.
    units = registry.plan_units(default, {"recruitee": ["acme"], "rippling": ["acme-co"], "pinpoint": ["x"]}, ["AI Engineer"])
    assert registry.Unit("recruitee", "acme", "") in units and not any(u.src in ("rippling", "pinpoint") for u in units)
    every = registry.enabled_specs(Settings(_env_file=None, pinpoint_enabled=True, rippling_enabled=True))
    assert "pinpoint" in every and "rippling" in every
    on = registry.enabled_specs(Settings(_env_file=None, rippling_enabled=True))
    assert registry.Unit("rippling", "acme-co", "") in registry.plan_units(on, {"rippling": ["acme-co"]}, ["AI Engineer"])
    off = registry.enabled_specs(Settings(_env_file=None, recruitee_enabled=False))
    assert "recruitee" not in off
    assert "rippling" not in registry.enabled_specs(Settings(_env_file=None, rippling_enabled=True, disabled_sources="rippling"))


def test_a_switched_off_reader_makes_no_detail_request_for_stored_postings():
    """Rippling is off by default. A Rippling posting that is still in the index from the time the reader was on must
    not be opened either: the description request goes to Rippling just like a board read."""
    from tests import conftest, test_wider_search as wide
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path.endswith(f"/jobs/{UUID}"):
            return httpx.Response(200, json=load("rippling_detail.json"))
        return httpx.Response(404, json={})

    async def details(**settings):
        ai = wide.WideAI(available=False)
        engine = wide.SearchEngine(wide.wide_settings(searxng_url="", **settings), Database(":memory:"),
                                   client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), seeds=[], ai_factory=lambda: ai,
                                   browser=conftest.FakeBrowser(available=False))
        engine.limiter.intervals = {"rippling": 0.0}
        stored = Job(title="AI Engineer", company="Rippling", source="Rippling", url=f"https://ats.rippling.com/rippling/jobs/{UUID}",
                     board="rippling", ats="rippling", ats_id=UUID, location="San Francisco, CA", country_hint="United States",
                     needs_detail=True, detail_ref=UUID)
        try:
            return "rippling" in engine.specs, await engine._fetch_details([stored], {"rejected_non_us": 0}, None)
        finally:
            await engine.aclose()

    enabled, opened = run(details())
    assert not enabled and opened == [] and asked == []                           # off: not one request
    enabled, opened = run(details(rippling_enabled=True))
    assert enabled and len(opened) == 1 and not opened[0].needs_detail and opened[0].posted_at == "2026-08-12"
    assert any(u.startswith("https://api.rippling.com/platform/api/ats/v1/board/rippling/jobs/") for u in asked)


@pytest.mark.parametrize("ats,slug,url,expected", [
    ("recruitee", "Namecheap", "https://namecheap.recruitee.com", "namecheap"),
    ("recruitee", "namecheap", "", "namecheap"),
    ("recruitee", "jobs.atiba.com", "https://jobs.atiba.com", None),             # an employer's own domain: not a board id
    ("recruitee", "landbot", "https://jobs.landbot.io", None),                   # slug given, but the board lives elsewhere
    ("recruitee", "acme", "https://other.recruitee.com", None),
    ("recruitee", "acme", "http://acme.recruitee.com", None),
    ("recruitee", "careers", "https://careers.recruitee.com", None),
    ("pinpoint", "57blocks", "https://57blocks.pinpointhq.com", "57blocks"),
    ("pinpoint", "ats", "https://ats.pinpointhq.com", None),
    ("pinpoint", "a_b", "", None),
    ("rippling", "11fs-group-ltd", "https://ats.rippling.com/11fs-group-ltd/jobs", "11fs-group-ltd"),
    ("rippling", "internal", "https://ats.rippling.com/internal/jobs", None),
    ("rippling", "acme", "https://ats.rippling.com/other/jobs", None),
    ("rippling", "acme/../x", "", None),
    ("rippling", "acme%2Fx", "", None),
])
def test_catalogue_ids_are_validated_strictly(ats, slug, url, expected):
    assert board_catalog.normalize(ats, slug, url) == expected


def test_catalogue_lists_load_and_plan():
    counts = {}
    for ats in ("recruitee", "pinpoint", "rippling"):
        assert ats in board_catalog.CATALOG_ATS and board_catalog.PER_MINUTE[ats] >= 0
        loaded = board_catalog.load_csv(ats)
        assert loaded.keys and len(set(loaded.keys)) == len(loaded.keys) and all(valid_board(ats, k) for k in loaded.keys)
        assert loaded.rows == len(loaded.keys) + loaded.malformed + loaded.duplicates
        counts[ats] = (loaded.rows, len(loaded.keys))
    assert counts == {"recruitee": (2972, 2956), "pinpoint": (1340, 1339), "rippling": (2662, 2660)}
    plans = run(board_catalog.plan_import(None, ["recruitee", "rippling"]))
    assert [(p.ats, len(p.new), p.existing) for p in plans] == [("recruitee", 2956, 0), ("rippling", 2660, 0)]
    found = board_catalog.simplify_keys([{"url": "https://acme.recruitee.com/o/intern"}, {"url": f"https://ats.rippling.com/acme-co/jobs/{UUID}"},
                                         {"url": "https://careers.recruitee.com/o/x"}])
    assert found == {"recruitee": ["acme"], "rippling": ["acme-co"]}


# ---------------------------------------------------------------- discovery to results, through the engine
def test_a_search_engine_hit_registers_the_board_and_its_postings_are_read(monkeypatch):
    from tests import conftest, test_wider_search as wide
    offer = {**load("recruitee_offers.json")["offers"][1], "title": "AI Engineer", "slug": "ai-engineer", "description": conftest.DESC,
             "careers_url": "https://newco.recruitee.com/o/ai-engineer", "careers_apply_url": "https://newco.recruitee.com/o/ai-engineer/c/new",
             "company_name": "NewCo"}
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith(".recruitee.com"):
            asked.append(str(request.url))
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-Agent: *\n\nDisallow: /v/\n")
            if request.url.host == "newco.recruitee.com" and request.url.path == "/api/offers/":
                return httpx.Response(200, json={"offers": [offer]})
            return httpx.Response(404, json={})
        return wide.handler(request)

    wide.SEARX_ANSWERS["*"] = ["https://newco.recruitee.com/o/ai-engineer", "https://ghostco.recruitee.com/", "https://careers.recruitee.com/o/x"]
    db = Database(":memory:")
    settings = wide.wide_settings(searxng_sites="recruitee.com")
    engine = wide.SearchEngine(settings, db, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), seeds=[],
                               ai_factory=lambda: ai, sources=["recruitee", "searxng"], browser=conftest.FakeBrowser(available=False))
    ai = wide.WideAI(available=True)
    engine.limiter.intervals = {}
    engine.ai = ai
    try:
        state = run(wide.search(engine, result_count=50))
    finally:
        wide.SEARX_ANSWERS.clear()
    found = [j for j in state.results if j.board == "newco"]
    assert len(found) == 1 and found[0].source == "Recruitee" and found[0].company == "NewCo"
    assert found[0].verification == "Listed in the employer's live Recruitee careers site API"
    assert db.known_boards() == [("recruitee", "newco")] and db.dead_boards() == {("recruitee", "ghostco")}
    assert asked.count("https://newco.recruitee.com/api/offers/") == 1 and not any("careers.recruitee.com" in u for u in asked)
