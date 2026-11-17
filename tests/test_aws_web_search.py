"""AWS Bedrock AgentCore Web Search: request shape (botocore Stubber against the official schema), error mapping,
retries, the monthly allowance, pacing, and the source inside the engine's rounds. AWS is never called."""
import asyncio
import threading
from datetime import datetime, timezone

import boto3
import pytest
from botocore.exceptions import ConnectTimeoutError, NoCredentialsError, ReadTimeoutError
from botocore.stub import Stubber

from app.config import Settings
from app.database import Database
from app.providers import aws_web_search as aws
from app.providers.aws_web_search import (AwsWebSearch, AwsWebSearchError, MemoryQuota, build_request, employer_posting_address,
                                          paced_calls, query_text, working_days_left)
from app.providers.paid import aggregator_hosts
from app.search_plan import plan_for
from tests import test_wider_search as wide

AGG = aggregator_hosts(Settings(_env_file=None).aggregator_hosts)
MARK = "ZQXAWSONLYTEXT"            # appears only in AWS result titles / text: must never reach storage


def run(coro):
    return asyncio.run(coro)


def settings(**over) -> Settings:
    base = dict(_env_file=None, aws_web_search_enabled=True, aws_web_search_published_days=0, aws_web_search_max_attempts=3)
    base.update(over)
    return Settings(**base)


def stubbed():
    client = boto3.client("bedrock-agentcore", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
    return client, Stubber(client)


def no_sleep():
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
    return sleep, slept


# ---------------------------------------------------------------- request shape
def test_request_matches_the_official_schema_and_never_combines_include_and_exclude():
    client, stub = stubbed()
    after = datetime(2026, 9, 19, tzinfo=timezone.utc)
    stub.add_response("invoke_web_search", {"results": [
        {"title": "AI Engineer", "url": "https://job-boards.greenhouse.io/acme/jobs/1001", "text": "extract",
         "publishedAt": datetime(2026, 11, 1, tzinfo=timezone.utc)},
        {"title": "no https", "url": "http://x.example/1", "text": "t"}]},
        {"webSearchId": "web-search.v1", "searchQuery": "AI Engineer jobs Austin, TX remote", "maxResults": 25,
         "filters": {"domainFilter": {"include": ["job-boards.greenhouse.io", "jobs.lever.co"]}, "publishedDateFilter": {"publishedAfter": after}}})
    stub.add_response("invoke_web_search", {"results": []},
                      {"webSearchId": "web-search.v1", "searchQuery": "x jobs United States", "maxResults": 1,
                       "filters": {"domainFilter": {"exclude": ["linkedin.com"]}, "publishedDateFilter": {"publishedAfter": after}}})
    search = AwsWebSearch(settings(), MemoryQuota(), client=client)
    search.published_after = lambda: after
    with stub:
        first = run(search.search(query_text("AI Engineer", "Austin, TX", "remote"), include=["job-boards.greenhouse.io", "jobs.lever.co"],
                                  exclude=["linkedin.com"]))                               # include wins: never both
        second = run(search.search("x jobs United States", exclude=["www.linkedin.com", "bad host!"], max_results=0))
        stub.assert_no_pending_responses()
    assert [r.url for r in first.results] == ["https://job-boards.greenhouse.io/acme/jobs/1001"] and first.queries == 1
    assert first.results[0].published_at.year == 2026 and second.results == [] and search.calls == 2
    # bounds of the schema: query 1-200 characters, maxResults 1-25, at most 100 domains
    req = build_request("web-search.v1", "q " * 300, 999, include=[f"h{i}.example.com" for i in range(150)])
    assert len(req["searchQuery"]) <= 200 and req["maxResults"] == 25 and len(req["filters"]["domainFilter"]["include"]) == 100
    assert "filters" not in build_request("web-search.v1", "q", 5)
    with pytest.raises(AwsWebSearchError):
        build_request("web-search.v1", "   ", 5)
    with pytest.raises(AwsWebSearchError):
        build_request("Bad Id!", "q", 5)


def test_published_after_filter_follows_the_setting():
    on = AwsWebSearch(settings(aws_web_search_published_days=60), MemoryQuota())
    assert round((datetime.now(timezone.utc) - on.published_after()).total_seconds() / 86400) == 60
    assert AwsWebSearch(settings(aws_web_search_published_days=0), MemoryQuota()).published_after() is None


def test_query_text_says_place_and_work_mode():
    assert query_text("Data Engineer", "Texas", "hybrid") == "Data Engineer jobs Texas hybrid"
    assert query_text("Data Engineer", "", "onsite") == "Data Engineer jobs United States on-site"
    assert query_text("Data Engineer", "remote", "remote") == "Data Engineer jobs remote"


# ---------------------------------------------------------------- errors and retries
@pytest.mark.parametrize("code,status,kind", [("ThrottlingException", 429, "throttled"), ("AccessDeniedException", 403, "access_denied"),
                                              ("ValidationException", 400, "validation"), ("ResourceNotFoundException", 404, "not_found"),
                                              ("InternalServerException", 500, "unavailable"), ("ServiceUnavailableException", 503, "unavailable")])
def test_client_errors_map_to_clean_internal_errors(code, status, kind):
    client, stub = stubbed()
    for _ in range(3):
        stub.add_client_error("invoke_web_search", service_error_code=code, service_message="query text echoed AI Engineer", http_status_code=status)
    sleep, slept = no_sleep()
    quota = MemoryQuota()
    search = AwsWebSearch(settings(), quota, client=client, sleep=sleep)
    with stub, pytest.raises(AwsWebSearchError) as info:
        run(search.search("AI Engineer jobs Texas"))
    err = info.value
    assert err.kind == kind and "AI Engineer" not in err.message
    retried = kind in ("throttled", "unavailable")
    assert err.queries == (3 if retried else 1) == run(quota.used())          # every attempt that reached AWS is counted
    assert len(slept) == (2 if retried else 0)


def test_throttling_then_success_is_retried_and_each_attempt_counts():
    client, stub = stubbed()
    stub.add_client_error("invoke_web_search", service_error_code="ThrottlingException", http_status_code=429)
    stub.add_response("invoke_web_search", {"results": [{"url": "https://jobs.lever.co/acme/" + "a" * 8 + "-1111-2222-3333-" + "b" * 12, "text": "t"}]}, None)
    sleep, slept = no_sleep()
    quota = MemoryQuota()
    with stub:
        answer = run(AwsWebSearch(settings(), quota, client=client, sleep=sleep).search("q"))
    assert len(answer.results) == 1 and answer.queries == 2 and run(quota.used()) == 2 and len(slept) == 1


class Raising:
    def __init__(self, exc):
        self.exc, self.calls = exc, 0

    def invoke_web_search(self, **params):
        self.calls += 1
        raise self.exc


@pytest.mark.parametrize("exc,kind,counted", [(ReadTimeoutError(endpoint_url="https://x"), "timeout", 3),
                                              (ConnectTimeoutError(endpoint_url="https://x"), "timeout", 3),
                                              (NoCredentialsError(), "credentials", 0)])
def test_timeouts_retry_and_missing_credentials_give_the_unit_back(exc, kind, counted):
    sleep, _ = no_sleep()
    quota = MemoryQuota()
    fake = Raising(exc)
    with pytest.raises(AwsWebSearchError) as info:
        run(AwsWebSearch(settings(), quota, client=fake, sleep=sleep).search("q"))
    assert info.value.kind == kind and run(quota.used()) == counted
    assert fake.calls == (1 if kind == "credentials" else 3)


def test_disabled_or_without_a_counter_nothing_is_sent():
    fake = Raising(AssertionError("must not be called"))
    for search in (AwsWebSearch(settings(aws_web_search_enabled=False), MemoryQuota(), client=fake), AwsWebSearch(settings(), None, client=fake)):
        assert not search.enabled
        with pytest.raises(AwsWebSearchError) as info:
            run(search.search("q"))
        assert info.value.kind == "disabled"
    assert fake.calls == 0


def test_boto_client_uses_region_timeouts_and_no_botocore_retries():
    client = aws._boto_client("us-east-1", 5.0, 20.0)
    assert client is aws._boto_client("us-east-1", 5.0, 20.0)                         # one client per process
    assert client.meta.region_name == "us-east-1" and client.meta.config.connect_timeout == 5.0 and client.meta.config.read_timeout == 20.0
    assert client.meta.config.retries["total_max_attempts"] == 1


# ---------------------------------------------------------------- allowance and pacing
def test_memory_quota_never_passes_the_ceiling_under_concurrency_and_keeps_the_reserve():
    quota = MemoryQuota(limit=4000, reserve=300)

    async def many(n, interactive):
        return await asyncio.gather(*(quota.reserve(interactive=interactive) for _ in range(n)))

    granted = run(many(4500, False))
    assert sum(granted) == 3700 and run(quota.used()) == 3700                          # background stops at limit - reserve
    assert sum(run(many(500, True))) == 300
    assert run(quota.used()) == 4000 and not run(quota.reserve(interactive=True))
    results = []
    threads = [threading.Thread(target=lambda: results.append(run(quota.reserve(True)))) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not any(results)


def test_quota_rolls_over_with_the_month():
    now = [datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc).timestamp()]
    quota = MemoryQuota(limit=2, clock=lambda: now[0])
    assert run(quota.reserve()) and run(quota.reserve()) and not run(quota.reserve())
    now[0] = datetime(2026, 12, 1, 0, 0, 1, tzinfo=timezone.utc).timestamp()
    assert run(quota.used()) == 0 and run(quota.reserve()) and run(quota.usage())["month"] == "2026-12"


def test_pacing_scales_calls_down_when_the_month_runs_low():
    start = datetime(2026, 12, 1, tzinfo=timezone.utc)                  # 23 working days in December 2026
    assert working_days_left(start) == 23 and working_days_left(datetime(2026, 12, 31, tzinfo=timezone.utc)) == 1
    assert paced_calls(4, 3700, 45, start) == 4                          # ceil(3700 / (45 x 23)) = 4
    assert paced_calls(4, 1000, 45, start) == 1                          # ceil(1000 / 1035) = 1: thinner, not stopped
    assert paced_calls(4, 2, 45, start) == 1 and paced_calls(4, 0, 45, start) == 0 and paced_calls(0, 100, 45, start) == 0
    late = datetime(2026, 12, 31, tzinfo=timezone.utc)
    assert paced_calls(4, 3000, 45, late) == 4                          # unused allowance at the end: the cap applies


def test_plan_scales_the_per_search_cap_with_the_request():
    s = settings()
    assert plan_for(s, 50).aws_calls == 4 and plan_for(s, 100).aws_calls == 8 and plan_for(s, 1000).aws_calls == 10
    assert plan_for(settings(aws_web_search_enabled=False), 50).aws_calls == 0


def test_generic_pages_are_never_posting_addresses():
    assert employer_posting_address("https://careers.acme.com/jobs/12345-ai-engineer", AGG)
    assert employer_posting_address("https://acme.com/careers/ai-engineer-austin", AGG)
    for url in ("https://www.linkedin.com/jobs/view/1", "https://www.indeed.com/viewjob?jk=1", "https://careers.acme.com/jobs/search?q=ai",
                "https://careers.acme.com/jobs?keywords=ai", "https://acme.com/about", "http://careers.acme.com/jobs/1",
                "https://careers.acme.com/jobs/category/engineering/search", "https://www.google.com/search?q=jobs"):
        assert not employer_posting_address(url, AGG), url


# ---------------------------------------------------------------- inside the engine
class FakeAws:
    """Answers by domain filter. Records the parameters (the engine must send a valid request)."""

    def __init__(self, by_group=None, error=None):
        self.by_group, self.error, self.params = by_group or {}, error, []

    def invoke_web_search(self, **params):
        self.params.append(params)
        if self.error:
            raise self.error
        include = (params.get("filters") or {}).get("domainFilter", {}).get("include")
        urls = self.by_group.get("ats" if include else "general", [])
        return {"results": [{"title": f"{MARK} title", "url": u, "text": f"{MARK} extract " * 20} for u in urls]}


ATS_URLS = ["https://boards.greenhouse.io/newco/jobs/5001",          # live posting -> registers the whole board
            "https://boards.greenhouse.io/newco/jobs/5999",          # not in the live board: stale, dropped
            "https://boards.greenhouse.io/newco/jobs/5001",          # duplicate
            "https://boards.greenhouse.io/embed/job_board?for=",     # generic
            "https://www.linkedin.com/jobs/search?keywords=ai"]      # aggregator


def aws_engine(fake, quota=None, db=None, **over):
    s = wide.wide_settings(searxng_url="", aws_web_search_enabled=True, aws_web_search_published_days=0, **over)
    engine = wide.wide_engine(ai=wide.WideAI(available=False), settings=s, db=db, sources=("greenhouse", "lever", "ashby", "aws_web_search"))
    engine.aws = AwsWebSearch(s, quota if quota is not None else MemoryQuota(), client=fake)
    return engine


def test_engine_uses_aws_in_round_one_verifies_against_the_feed_and_stores_no_aws_text():
    fake = FakeAws({"ats": ATS_URLS, "general": ["https://careers.example.com/search?q=ai", "https://www.indeed.com/viewjob?jk=1"]})
    db = Database(":memory:")
    quota = MemoryQuota()
    state = run(wide.search(aws_engine(fake, quota, db), result_count=50))
    stats = state.stats
    assert fake.params and stats["aws_queries"] == len(fake.params) == run(quota.used()) <= stats["aws_budget"] <= 4
    assert stats["rounds"][0]["leads"] >= 1                                              # used in round 1
    for params in fake.params:
        assert params["webSearchId"] == "web-search.v1" and 1 <= params["maxResults"] <= 25 and len(params["searchQuery"]) <= 200
        assert len((params.get("filters") or {}).get("domainFilter", {})) <= 1
    assert any("include" in p.get("filters", {}).get("domainFilter", {}) for p in fake.params)
    newco = [j for j in state.results if j.board == "newco"]
    assert len(newco) == 6 and all(j.verification_level == "verified" and j.source == "Greenhouse" for j in newco)
    assert any("AWS web search" in j.sources for j in newco if j.ats_id == "5001")
    assert all(j.url.startswith("https://") and "linkedin" not in j.url for j in state.results)
    assert stats["aws_duplicates"] >= 1 and stats["aws_generic"] >= 2 and stats["rejected_unverified"] >= 1
    with db._lock:
        assert db._db.execute("SELECT origin FROM boards WHERE slug='newco'").fetchone()[0] == "aws_web_search"
        dump = "\n".join(db._db.iterdump())
    assert MARK not in dump and MARK not in repr(state.summary()) and MARK not in repr([j.model_dump() for j in state.results])


def test_engine_skips_aws_when_disabled_switched_off_or_exhausted():
    fake = FakeAws({"ats": ATS_URLS})
    s = wide.wide_settings(searxng_url="", aws_web_search_enabled=False)
    off = wide.wide_engine(ai=wide.WideAI(available=False), settings=s, sources=("greenhouse", "lever", "ashby", "aws_web_search"))
    off.aws = AwsWebSearch(s, MemoryQuota(), client=fake)
    state = run(wide.search(off, result_count=50))
    assert state.status == "completed" and state.stats["aws_queries"] == 0 and not fake.params
    not_listed = aws_engine(fake)
    not_listed.specs.pop("aws_web_search")                                               # switched off in Settings
    assert run(wide.search(not_listed, result_count=50)).stats["aws_queries"] == 0 and not fake.params
    full = MemoryQuota(limit=10, reserve=2)
    for _ in range(8):
        run(full.reserve())
    state = run(wide.search(aws_engine(fake, full), result_count=50))
    assert state.status == "completed" and state.results and not fake.params and state.stats["aws_budget"] == 0
    assert "allowance" in state.stats["aws_skipped"] and any("AWS web search was skipped" in w for w in state.warnings)
    assert run(full.used()) == 8                                                         # the reserve is left for the endpoint


def test_engine_goes_on_when_aws_refuses_access():
    client, stub = stubbed()
    stub.add_client_error("invoke_web_search", service_error_code="AccessDeniedException", http_status_code=403)
    engine = aws_engine(client)
    with stub:
        state = run(wide.search(engine, result_count=50))
    assert state.status == "completed" and state.results and state.stats["aws_queries"] == 1
    assert "refused access" in state.stats["aws_skipped"] and any("AWS web search was skipped" in w for w in state.warnings)


def test_engine_keeps_using_aws_in_later_rounds_while_the_target_is_not_met():
    fake = FakeAws({"ats": ["https://boards.greenhouse.io/thirdco"]})
    state = run(wide.search(aws_engine(fake, search_stale_rounds=5), result_count=500))
    assert state.stats["aws_queries"] >= 2 and len(state.stats["rounds"]) >= 2
    assert state.stats["aws_queries"] <= state.stats["aws_budget"]
    assert len([j for j in state.results if j.board == "thirdco"]) == 4                  # board root read through its API
