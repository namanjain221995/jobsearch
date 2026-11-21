"""New sources: Workday, Workable, Amazon, remote boards, Adzuna. Network is mocked with synthetic payloads."""
import asyncio

import httpx
import pytest

from app.job_normalizer import parse_ats
from app.providers import adzuna, amazon, remote_boards, workable, workday
from app.providers.base import Ctx, RobotsRules, SourceError, iso_date
from app.providers.registry import SPECS, plan_units
from tests.conftest import CALLS, handler


def run(coro):
    return asyncio.run(coro)


async def with_ctx(fn, **options):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await fn(Ctx(client, options=options))


def test_robots_rules_wildcards_and_longest_match():
    rules = RobotsRules("User-agent: *\nDisallow: /*search=\nDisallow: /jobs*&page=\nAllow: /search/*\nDisallow: /search\n"
                        "User-agent: LinkedInBot\nAllow: /\n")
    assert rules.allows("https://x.test/api/remote-jobs") and not rules.allows("https://x.test/api/remote-jobs?search=ml")
    assert rules.allows("https://x.test/jobs/api/search?q=a") and not rules.allows("https://x.test/jobs/api/search?q=a&page=2")
    assert rules.allows("https://x.test/search/abc") and not rules.allows("https://x.test/search")
    assert not RobotsRules("User-agent: *\nDisallow: /\n").allows("https://x.test/v1/companies/a")       # other bots' Allow is not ours
    assert RobotsRules("").allows("https://x.test/anything") and not RobotsRules(deny_all=True).allows("https://x.test/")


def test_iso_date_formats():
    assert iso_date("November  6, 2026") == "2026-11-06" and iso_date(1794441600) == "2026-11-12"
    assert iso_date("2026-10-30T10:00:00Z") == "2026-10-30" and iso_date("Posted 3 Days Ago") == "" and iso_date(None) == ""


@pytest.mark.parametrize("url,expected", [
    ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Senior-ML-Engineer_JR2023124",
     ("workday", "nvidia|wd5|NVIDIAExternalCareerSite", "nvidia:JR2023124")),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA/X_JR2023124-1?source=li",
     ("workday", "nvidia|wd5|NVIDIAExternalCareerSite", "nvidia:JR2023124")),
    ("https://apply.workable.com/huggingface/j/81B46579FE/apply", ("workable", "huggingface", "81B46579FE")),
])
def test_parse_new_ats_urls(url, expected):
    assert parse_ats(url) == expected


def test_workday_search_uses_us_facet_and_returns_list_only_records():
    jobs = run(with_ctx(lambda ctx: workday.fetch(ctx, "wdco|wd5|Careers", "ai engineer")))
    assert [j.ats_id for j in jobs] == ["wdco:R100", "wdco:R101", "wdco:R102", "wdco:R103"]     # India posting filtered by facet
    first = jobs[0]
    assert first.needs_detail and first.detail_ref == "/job/US-TX-Austin/AI-Engineer_R100" and first.country_hint == "United States"
    assert first.url == "https://wdco.wd5.myworkdayjobs.com/Careers/job/US-TX-Austin/AI-Engineer_R100" and first.apply_url == ""
    assert workday.find_us_facet([]) is None


def test_workday_detail_fills_description_and_detects_closed_postings():
    async def go(ctx):
        jobs = await workday.fetch(ctx, "wdco|wd5|Careers", "ai engineer")
        return [await workday.detail(ctx, j) for j in jobs]
    open_job, multi, closed, cleared = run(with_ctx(go))
    assert open_job.needs_detail is False and "PyTorch" in open_job.raw_html and open_job.posted_at == "2026-11-03"
    assert multi.location == "US, WA, Seattle; US, NY, New York"
    assert closed is None                                           # 404 from the live site = no longer open
    assert "TS/SCI" in cleared.raw_html


def test_workday_respects_robots_txt():
    with pytest.raises(SourceError, match="robots.txt disallows"):
        run(with_ctx(lambda ctx: workday.fetch(ctx, "blockedco|wd1|Site", "ai engineer")))
    assert CALLS["https://blockedco.wd1.myworkdayjobs.com/wday/cxs/blockedco/Site/jobs"] == 0


def test_workday_lead_verification_by_url():
    job = run(with_ctx(lambda ctx: workday.lead(ctx, "wdco|wd5|Careers", "https://wdco.wd5.myworkdayjobs.com/en-US/Careers/job/US-TX-Austin/AI-Engineer_R100?src=x")))
    assert job and job.title == "AI Engineer" and job.ats_id == "wdco:R100"
    gone = run(with_ctx(lambda ctx: workday.lead(ctx, "wdco|wd5|Careers", "https://wdco.wd5.myworkdayjobs.com/Careers/job/US/Nope_R999")))
    assert gone is None


def test_workable_search_follows_page_tokens():
    jobs = run(with_ctx(lambda ctx: workable.fetch(ctx, "*", "ai engineer")))
    assert [j.url.split("/view/")[1].split("/")[0] for j in jobs] == ["abc", "def", "ghi"] and jobs[0].company == "Gizmo Labs"
    # The job board's listing ids are not the employers' posting codes: none is stored as an ATS posting id.
    assert all(j.ats == "workable" and j.ats_id == "" and j.board == "*" for j in jobs)
    assert jobs[0].work_mode == "Hybrid" and jobs[0].country_hint == "United States" and jobs[1].country_hint == "France"
    assert jobs[0].apply_url == ""                                  # the search API gives no application link: none is invented


def test_amazon_remote_boards_and_adzuna_normalization():
    az = run(with_ctx(lambda ctx: amazon.fetch(ctx, "*", "machine learning engineer")))[0]
    assert az.company == "Amazon" and az.url == "https://www.amazon.jobs/en/jobs/900/machine-learning-engineer"
    assert az.apply_url.endswith("/jobs/900/apply") and az.posted_at == "2026-11-06" and az.country_hint == "United States"
    rok = run(with_ctx(lambda ctx: remote_boards.fetch_remoteok(ctx, "*", "")))
    assert [j.company for j in rok] == ["Acme AI", "Globex", "Initech"] and all(j.aggregator and j.source == "Remote OK" for j in rok)
    assert rok[0].salary == "USD 150,000 - 190,000" and rok[1].salary == "Not listed"
    jb = run(with_ctx(lambda ctx: remote_boards.fetch_jobicy(ctx, "usa", "")))[0]
    assert jb.apply_url == jb.url == "https://jobicy.com/jobs/501-ai-engineer" and jb.country_hint == "United States"
    hm = run(with_ctx(lambda ctx: remote_boards.fetch_himalayas(ctx, "*", "ai engineer")))
    assert len(hm) == 1 and hm[0].company == "Pied Piper"           # the expired listing is dropped
    assert hm[0].apply_url.endswith("?utm_source=api") and hm[0].url.endswith("/jobs/ai-engineer")
    ad = run(with_ctx(lambda ctx: adzuna.fetch(ctx, "us", "ai engineer"), adzuna_app_id="id", adzuna_app_key="key"))[0]
    assert ad.title == "AI Engineer" and ad.salary == "Not listed" and ad.aggregator      # predicted salary is never shown
    with pytest.raises(SourceError):
        run(with_ctx(lambda ctx: adzuna.fetch(ctx, "us", "ai engineer")))


def test_plan_units_per_source_kind():
    specs = {n: SPECS[n] for n in ("greenhouse", "workday", "workable", "remoteok", "jobicy")}
    units = plan_units(specs, {"greenhouse": ["acme"], "workday": ["wdco|wd5|Careers"]}, ["ai engineer", "ml engineer"])
    got = {(u.src, u.key, u.scope) for u in units}
    assert got == {("greenhouse", "acme", ""), ("workday", "wdco|wd5|Careers", "ai engineer"),       # Workday: first query only
                   ("workable", "*", "ai engineer"), ("workable", "*", "ml engineer"), ("remoteok", "*", ""), ("jobicy", "usa", "")}
    assert plan_units(specs, {"greenhouse": ["acme"]}, []) == [u for u in units if u.scope == "" and u.src != "workday"]


def test_host_limiter_paces_requests_and_backs_off_after_429():
    from app.providers.base import HostLimiter

    async def go():
        limiter = HostLimiter(default=4, limits={"paced": 2}, intervals={"paced": 0.15})
        loop = asyncio.get_running_loop()
        started = []

        async def one():
            async with limiter.slot("paced"):
                started.append(loop.time())

        t0 = loop.time()
        await asyncio.gather(*(one() for _ in range(4)))
        gaps = [b - a for a, b in zip(started, started[1:])]
        assert all(g >= 0.14 for g in gaps) and started[-1] - t0 >= 0.44        # one request per interval, not a burst
        limiter.penalize("paced", 0.5)
        t1 = loop.time()
        await one()
        assert loop.time() - t1 >= 0.45                                          # held back after "slow down"
        limiter.penalize("unpaced", 5)                                           # groups without pacing are unaffected
        t2 = loop.time()
        async with limiter.slot("unpaced"):
            pass
        assert loop.time() - t2 < 0.1
    run(go())
