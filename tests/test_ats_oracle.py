"""Oracle Recruiting Cloud career-site reader (app/providers/ats_oracle.py). No network: mocked transport; the payloads
have the shape of the live answers of 2026-11-21 (Oracle's own career site), cut down to the fields that are read."""
import asyncio

import httpx
import pytest

from app.ats_platforms import PLATFORMS
from app.providers import ats_oracle, registry
from app.providers.base import BoardNotFound, Ctx, HostLimiter, SourceError

KEY = "eeho.fa.us2.oraclecloud.com|CX_45001"
HOME = '<html><head><meta property="og:site_name" content="Oracle"/><title>Oracle</title></head><body></body></html>'


def row(i, title="Senior Data Engineer", country="US", place="Austin, TX, United States", **extra):
    return {"Id": str(i), "Title": title, "PostedDate": "2026-11-18", "PrimaryLocationCountry": country,
            "PrimaryLocation": place, "WorkplaceType": "", "ShortDescriptionStr": "Short text.",
            "secondaryLocations": [{"Name": "Nashville, TN, United States", "CountryCode": "US"}], **extra}


def page(rows, total):
    return {"items": [{"SearchId": 1, "SiteNumber": "CX_45001", "TotalJobsCount": total, "requisitionList": rows}],
            "count": 1, "hasMore": False}


DETAIL = {"items": [{"Id": "338589", "Title": "Senior Data Engineer", "ExternalPostedStartDate": "2026-11-19T14:09:23+00:00",
                     "ExternalDescriptionStr": "<p>Build pipelines.</p>", "ExternalResponsibilitiesStr": "<p>Own them.</p>",
                     "ExternalQualificationsStr": "<p>Five years.</p>", "CorporateDescriptionStr": "<p>About us.</p>"}]}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def ok(url):
        return "93.184.216.34"
    monkeypatch.setattr(ats_oracle, "_guard", ok)
    monkeypatch.setattr(ats_oracle, "_site_names", {})
    monkeypatch.setattr(ats_oracle, "DISPLAY_NAMES", {})


def make_ctx(routes, calls=None, options=None):
    def handler(request: httpx.Request):
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        if request.url.path == "/robots.txt":
            robots = routes.get("robots", httpx.Response(404, text="Not Found"))
            return robots if isinstance(robots, httpx.Response) else httpx.Response(200, text=robots)
        for match, resp in routes.items():
            if match != "robots" and match in url:
                return resp if isinstance(resp, httpx.Response) else httpx.Response(200, json=resp)
        return httpx.Response(404, json={})
    return Ctx(httpx.AsyncClient(transport=httpx.MockTransport(handler)), HostLimiter(intervals={"oracle": 0.0}), options=options or {})


def run(coro):
    return asyncio.run(coro)


def test_list_is_paged_deduplicated_and_normalized():
    calls = []
    first = [row(338589), row(338590, "Data Engineer II", "IN", "Bengaluru, India"), row(338589), {"Id": "", "Title": "x"}, "junk"]
    ctx = make_ctx({"offset=0,": page(first, 27), "offset=25,": page([row(338591, WorkplaceType="Remote")], 27),
                    "/sites/CX_45001": httpx.Response(200, text=HOME)}, calls)
    jobs = run(ats_oracle.fetch(ctx, KEY, 'data "engineer"; x=1,y'))
    assert [j.ats_id for j in jobs] == ["eeho:CX_45001:338589", "eeho:CX_45001:338590", "eeho:CX_45001:338591"]
    job = jobs[0]
    assert job.title == "Senior Data Engineer" and job.company == "Oracle" and job.source == "Oracle Recruiting Cloud"
    assert job.url == "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/job/338589"
    assert job.location == "Austin, TX, United States; Nashville, TN, United States" and job.country_hint == "United States"
    assert job.posted_at == "2026-11-18" and job.ats == "oracle" and job.board == KEY and job.needs_detail and job.detail_ref == "338589"
    assert jobs[1].country_hint == "IN" and jobs[2].work_mode == "Remote"
    lists = [c for c in calls if "recruitingCEJobRequisitions" in c]
    assert len(lists) == 2 and all(c.startswith("https://eeho.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/") for c in lists)
    assert "finder=findReqs;siteNumber=CX_45001,limit=25,offset=0,sortBy=POSTING_DATES_DESC,keyword=%22data%20engineer%20x%201%20y%22" in lists[0]
    assert calls[0].endswith("/robots.txt") and sum(c.endswith("/robots.txt") for c in calls) == 1
    assert sum(c.endswith("/sites/CX_45001") for c in calls) == 1                # the employer name is read once
    assert {httpx.URL(c).host for c in calls} == {"eeho.fa.us2.oraclecloud.com"}


def test_no_title_reads_the_newest_postings_and_stops_at_the_page_limit():
    calls = []
    ctx = make_ctx({"recruitingCEJobRequisitions": page([row(1), row(2)], 5000), "/sites/": httpx.Response(200, text=HOME)},
                   calls, options={"oracle_pages": 2})
    jobs = run(ats_oracle.fetch(ctx, KEY, ""))
    lists = [c for c in calls if "recruitingCEJobRequisitions" in c]
    assert len(jobs) == 2 and len(lists) == 2 and "keyword" not in lists[0]


def test_empty_site_makes_one_request_and_reads_no_name():
    calls = []
    ctx = make_ctx({"recruitingCEJobRequisitions": page([], 0)}, calls)
    assert run(ats_oracle.fetch(ctx, KEY, "nurse")) == []
    assert len(calls) == 2                                                     # robots.txt and one list request


def test_employer_name_falls_back_to_the_seed_list_or_the_pod_label():
    ctx = make_ctx({"recruitingCEJobRequisitions": page([row(1)], 1), "/sites/": httpx.Response(503, text="")})
    assert run(ats_oracle.fetch(ctx, KEY, ""))[0].company == "Eeho"
    ats_oracle.DISPLAY_NAMES["jpmc.fa.oraclecloud.com|CX_1001"] = "JPMorgan Chase"
    calls = []
    ctx = make_ctx({"recruitingCEJobRequisitions": page([row(1)], 1)}, calls)
    assert run(ats_oracle.fetch(ctx, "jpmc.fa.oraclecloud.com|CX_1001", ""))[0].company == "JPMorgan Chase"
    assert not any("/hcmUI/" in c for c in calls)


def test_detail_fills_the_description_and_a_removed_posting_is_none():
    ctx = make_ctx({"offset=0,": page([row(338589), row(7)], 2), "/sites/CX_45001": httpx.Response(200, text=HOME),
                    "Id=%22338589%22,siteNumber=CX_45001": DETAIL, "Id=%227%22": {"items": []}})
    jobs = run(ats_oracle.fetch(ctx, KEY, ""))
    done = run(ats_oracle.detail(ctx, jobs[0]))
    assert "Build pipelines." in done.raw_html and "Five years." in done.raw_html and "About us." in done.raw_html
    assert not done.needs_detail and done.posted_at == "2026-11-19"
    assert run(ats_oracle.detail(ctx, jobs[1])) is None
    jobs[0].detail_ref = '1",siteNumber=x'
    assert run(ats_oracle.detail(ctx, jobs[0])) is None                        # nothing but a plain id reaches the address


def test_missing_board_is_board_not_found_and_other_failures_are_source_errors():
    with pytest.raises(BoardNotFound):
        run(ats_oracle.fetch(make_ctx({}), KEY, ""))                           # HTTP 404
    with pytest.raises(SourceError):
        run(ats_oracle.fetch(make_ctx({"recruitingCE": {"items": "nope"}}), KEY, ""))
    with pytest.raises(SourceError):
        run(ats_oracle.fetch(make_ctx({"recruitingCE": httpx.Response(403, text="W4S-402: Blocked by WAF4SaaS")}), KEY, ""))
    with pytest.raises(SourceError):
        run(ats_oracle.fetch(make_ctx({"recruitingCE": httpx.Response(200, text="<html>")}), KEY, ""))


def test_robots_disallow_is_respected():
    calls = []
    ctx = make_ctx({"robots": "User-agent: *\nDisallow: /hcmRestApi/\n", "recruitingCE": page([row(1)], 1)}, calls)
    with pytest.raises(SourceError, match="robots.txt disallows"):
        run(ats_oracle.fetch(ctx, KEY, ""))
    assert all(c.endswith("/robots.txt") for c in calls)


def test_requests_pass_the_ssrf_guard(monkeypatch):
    seen = []

    async def guard(url):
        seen.append(url)
        raise SourceError("blocked")
    monkeypatch.setattr(ats_oracle, "_guard", guard)
    calls = []
    with pytest.raises(SourceError, match="blocked"):
        run(ats_oracle.fetch(make_ctx({"recruitingCE": page([row(1)], 1)}, calls), KEY, ""))
    assert len(seen) == 1 and calls == []


@pytest.mark.parametrize("key", [
    "", "eeho.fa.us2.oraclecloud.com", "eeho.fa.us2.oraclecloud.com|", "|CX_1", "eeho.fa.us2.oraclecloud.com|CX_1|x",
    "evil.example|CX_1", "eeho.fa.us2.oraclecloud.com.evil.example|CX_1", "oraclecloud.com|CX_1", "cloud.oraclecloud.com|CX_1",
    "127.0.0.1|CX_1", "localhost|CX_1", "eeho.fa.us2.oraclecloud.com:8443|CX_1", "user@eeho.fa.us2.oraclecloud.com|CX_1",
    "eeho.fa.us2.oraclecloud.com/x|CX_1", "eeho.fa.us2.oraclecloud.com|CX_1/../x", "eeho.fa.us2.oraclecloud.com|CX 1",
    "eeho.fa.us2.oraclecloud.com|CX_1,limit=9", "eeho.fa.us2.oraclecloud.com|CX_1;x", "EEHO.fa.us2.oraclecloud.com|CX_1",
    "eeho.fa.us2.oraclecloud.com|" + "x" * 80,
])
def test_hostile_keys_are_rejected_before_any_request(key):
    calls = []
    assert not ats_oracle.valid_key(key)
    with pytest.raises(BoardNotFound):
        run(ats_oracle.fetch(make_ctx({}, calls), key, "engineer"))
    assert calls == []


def test_valid_keys():
    for key in (KEY, "jpmc.fa.oraclecloud.com|CX_1001", "hcwp.fa.em2.oraclecloud.com|CX", "abc-test.fa.ocs.oraclecloud.com|My_Site-2"):
        assert ats_oracle.valid_key(key)


def test_the_reader_is_off_by_default_and_named_like_its_platform():
    assert PLATFORMS["oracle"].reader == "oracle" and PLATFORMS["oracle"].access == "public_pages"
    assert ats_oracle.ENABLED == ("oracle" in registry.SPECS)
    spec = registry.SPECS.get("oracle")
    if spec is not None:                                                       # only with BOARD_SOURCES_ORACLE=1
        assert spec.keyed and spec.kind == "search" and spec.detail is ats_oracle.detail and spec.min_ttl >= 3600
