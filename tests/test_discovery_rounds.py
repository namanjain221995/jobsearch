"""Discovery rounds of the engine: one record of what a search already asked (search_plan.QueryLedger), a different
part of the query plan for every search engine, take-over when one stops answering, and the AI web search held to the
hosts the search can use. No network: SearXNG, the company model's web search, TinyFish and AWS are stand-ins."""
import asyncio
import dataclasses
import json

import httpx
import pytest

from app.ai_orchestrator import AIOrchestrator
from app.config import Settings
from app.database import Database
from app.providers.aws_web_search import AwsWebSearch, MemoryQuota, query_text
from app.providers.openai_provider import ProviderError
from app.providers.searxng import site_hosts
from app.schemas import Candidate
from app.search_engine import AWS, SEARX, TECHSARA_WEB, SearchEngine, SearchState
from app.search_plan import QueryLedger, plan_for, query_key, site_query
from app.search_scope import Scope
from tests import conftest
from tests import test_wider_search as wide
from tests.conftest import FakeBrowser, make_request
from tests.test_aws_web_search import FakeAws

TS_URL = "https://ai.techsarasolutions.com/v1/responses"
TS_LOG: list[str] = []
TS_MODE = {"status": 200, "urls": ["https://jobs.lever.co/beta"]}
TINY_LOG: list[str] = []
KNOWN = ["https://boards.greenhouse.io/acme"]          # a board the index already reads: no search ever reaches its target


def run(coro):
    return asyncio.run(coro)


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url).split("?")[0]
    if url == TS_URL:
        query = json.loads(request.content)["input"].split("\n")[0].removeprefix("Search the web once for: ")
        TS_LOG.append(query)
        if TS_MODE["status"] != 200:
            return httpx.Response(TS_MODE["status"], json={"error": {"code": "model_unavailable"}})
        text = "\n".join(f"[result]({u})" for u in TS_MODE["urls"])
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                                         "usage": {"input_tokens": 10, "output_tokens": 5}})
    if url == "https://agent.tinyfish.ai/v1/wallet":
        return httpx.Response(200, json={"available_balance": 10})
    if url == "https://api.search.tinyfish.ai/":
        TINY_LOG.append(request.url.params.get("query"))
        return httpx.Response(200, json={"results": []})
    return wide.handler(request)


@pytest.fixture(autouse=True)
def _reset():
    wide.SEARX_ANSWERS.clear()
    wide.SEARX_LOG.clear()
    wide.SEARX_MODE.update(status=200, unresponsive=[])
    TS_LOG.clear()
    TINY_LOG.clear()
    TS_MODE.update(status=200, urls=["https://jobs.lever.co/beta"])


def engine_for(ai=None, sources=("greenhouse", "lever", "ashby", "searxng", "techsara_web"), **over) -> SearchEngine:
    settings = wide.wide_settings(techsara_api_key="tsk_test_key", **over)
    ai = ai or wide.WideAI(available=False)
    engine = SearchEngine(settings, Database(":memory:"), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                          seeds=conftest.DEFAULT_SEEDS, ai_factory=lambda: ai, sources=list(sources), browser=FakeBrowser(available=False))
    engine.limiter.intervals = {}
    if engine.tsweb is not None:
        async def no_wait(seconds):
            return None
        engine.tsweb._sleep = no_wait                  # the one repeat after a 429 / 503 does not wait in tests
    return engine


def keys(queries) -> set:
    return {query_key(q) for q in queries}


# ---------------------------------------------------------------- the record itself
def test_ledger_counts_the_same_request_once_whatever_its_spelling():
    assert query_key('site:jobs.lever.co "Data  Engineer" Texas') == ("data engineer texas", "jobs.lever.co")
    assert query_key('site:www.jobs.lever.co  Data Engineer jobs') == query_key('site:jobs.lever.co "data engineer"')     # TinyFish's spelling
    assert query_key(query_text("Data Engineer", "", ""), "Jobs.Lever.co") == ("data engineer", "jobs.lever.co")          # AWS: "... jobs United States"
    assert query_key(query_text("Data Engineer", "Texas", "remote"), "jobs.lever.co") == ("data engineer texas remote", "jobs.lever.co")
    assert site_query('Bad "quote" Title', "jobs.lever.co", "Texas") == 'site:jobs.lever.co "Bad quote Title" Texas'
    plan = [site_query("Data Engineer", host) for host in ("a.example", "b.example", "c.example", "d.example")]
    ledger = QueryLedger()
    assert ledger.claim(SEARX, plan[0]) and not ledger.claim(TECHSARA_WEB, plan[0]) and not ledger.claim(SEARX, plan[0])
    assert ledger.take(TECHSARA_WEB, plan, 2) == plan[1:3] and ledger.take("TinyFish", plan, 5) == plan[3:] and ledger.free(plan) == 0
    assert ledger.claim(AWS, "Data Engineer jobs United States", "e.example") and not ledger.claim(AWS, "data engineer", "a.example")
    assert not ledger.claim(SEARX, "", "a.example")
    # A second chance: only for a query another engine failed on or got nothing for, and only once.
    assert ledger.retry_for(TECHSARA_WEB, plan, 5) == []
    ledger.note(plan[0], "empty")
    ledger.note(plan[1], "failed")                     # the model's own failure is not its own second chance
    assert ledger.retry_for(TECHSARA_WEB, plan, 5) == [plan[0]] and ledger.retry_for(TECHSARA_WEB, plan, 5) == []
    assert ledger.retry_for("TinyFish", plan, 5) == [plan[1]]
    stand_in = QueryLedger()
    stand_in.claim(SEARX, plan[0]), stand_in.claim(SEARX, plan[1]), stand_in.note(plan[0], "failed"), stand_in.note(plan[1], "ok")
    assert stand_in.take_or_retry(TECHSARA_WEB, plan, 9) == [plan[0], plan[2], plan[3]]       # failed: second chance; answered: left; free: taken
    assert stand_in.take_or_retry(TECHSARA_WEB, plan, 9) == []
    ledger.claim("TinyFish", "site:z.example x")
    ledger.release("TinyFish", "site:z.example x")     # never sent: free again
    assert ledger.claim(SEARX, "site:z.example x")
    stats = ledger.stats()
    assert stats["asked"] == 6 and stats["by_engine"] == {AWS: 1, SEARX: 2, TECHSARA_WEB: 2, "TinyFish": 1}
    assert stats["duplicates_avoided"] == 5 and stats["retried_on_another_engine"] == 2


def test_budget_of_the_company_model_scales_with_the_request():
    on = Settings(_env_file=None, techsara_api_key="k")
    assert [plan_for(on, n).tsweb_searches for n in (25, 50, 100, 300, 1000)] == [12, 12, 24, 40, 40]
    assert plan_for(Settings(_env_file=None), 50).tsweb_searches == 0
    assert plan_for(Settings(_env_file=None, techsara_api_key="k", techsara_web_search_enabled=False), 50).tsweb_searches == 0
    assert plan_for(Settings(_env_file=None, techsara_api_key="k", techsara_web_searches_per_search=5), 300).tsweb_searches == 5      # the ceiling wins
    assert (on.techsara_web_searches_base, on.techsara_web_search_concurrency) == (12, 2)


# ---------------------------------------------------------------- SearXNG and the company model
def test_the_company_model_asks_what_searxng_will_not_reach_and_nothing_is_sent_twice():
    wide.SEARX_ANSWERS["*"] = KNOWN
    engine = engine_for(searxng_queries_per_search=8, techsara_web_searches_base=8)
    state = run(wide.search(engine, result_count=50))
    searx = [q for q, _ in wide.SEARX_LOG]
    assert len(state.stats["rounds"]) == 3 and len(searx) == 6 and len(TS_LOG) == 6                # 2 + 2 a round, three rounds
    assert not keys(searx) & keys(TS_LOG) and len(keys(TS_LOG)) == len(TS_LOG)                      # before this change: the very same queries
    plan = state.query_plan
    assert searx == plan[:6] and set(TS_LOG) == set(plan[8:14])                                     # the model starts where SearXNG's budget ends
    assert state.query_tail == plan[8:] and engine.tsweb._web_sem._value == 2
    ledger = state.stats["query_ledger"]
    assert ledger == {"asked": 12, "by_engine": {SEARX: 6, TECHSARA_WEB: 6}, "duplicates_avoided": 0, "retried_on_another_engine": 0}
    assert state.stats["tsweb_searches"] == 6 and state.stats["searx_queries"] == 6


def test_a_query_searxng_got_nothing_for_gets_one_second_chance_on_the_company_model():
    wide.SEARX_ANSWERS['site:job-boards.greenhouse.io "AI Engineer"'] = []                          # nothing at all
    wide.SEARX_ANSWERS['site:jobs.lever.co "AI Engineer"'] = ["https://example.com/blog/ai"]          # nothing usable
    wide.SEARX_ANSWERS["*"] = KNOWN
    state = run(wide.search(engine_for(searxng_queries_per_search=12, techsara_web_searches_base=12), result_count=50))
    plan = state.query_plan
    assert plan[1] == 'site:job-boards.greenhouse.io "AI Engineer"' and plan[2] == 'site:jobs.lever.co "AI Engineer"'
    assert TS_LOG.count(plan[1]) == 1 and TS_LOG.count(plan[2]) == 1 and plan[0] not in TS_LOG     # asked again once; an answered query never
    assert state.stats["query_ledger"]["retried_on_another_engine"] == 2
    again = [q for q in TS_LOG if q in (plan[1], plan[2])]
    assert len(TS_LOG) - len(again) == len(keys(TS_LOG) - keys(q for q, _ in wide.SEARX_LOG))       # everything else was nobody's query


def test_the_company_model_takes_over_when_searxng_stops_answering():
    wide.SEARX_MODE["status"] = 500
    state = run(wide.search(engine_for(searxng_queries_per_search=12, techsara_web_searches_base=12), result_count=50))
    plan = state.query_plan
    assert [q for q, _ in wide.SEARX_LOG] == plan[:3] and state.stats["searx_failed"] == 3          # three failures: SearXNG is left alone
    assert any("SearXNG" in w and "went on without it" in w for w in state.warnings)
    assert len(state.stats["rounds"]) >= 2 and set(plan[3:6]) <= set(TS_LOG)                        # round 2: SearXNG's slice goes to the model
    assert TS_LOG[2] == plan[0] and all(TS_LOG.count(q) == 1 for q in plan[:3])                     # what SearXNG failed on is asked once more, in plan order
    assert set(TS_LOG[:2]) == set(plan[12:14])                                                      # round 1 began beside SearXNG, beyond its budget
    assert len(TS_LOG) == len(set(TS_LOG)) and state.status == "completed"
    # SearXNG stops in the middle of its slice (three of six): the model's two spare searches of the round go to that
    # slice in plan order, and from the next round on it stands in for SearXNG from the top of the plan.
    wide.SEARX_LOG.clear()
    TS_LOG.clear()
    state = run(wide.search(engine_for(searxng_queries_per_search=24, techsara_web_searches_base=24), result_count=50))
    plan = state.query_plan
    assert [q for q, _ in wide.SEARX_LOG] == plan[:3] and set(TS_LOG[:4]) == set(plan[24:28])           # its own four, beside SearXNG
    assert set(TS_LOG[4:6]) == set(plan[:2]) and set(TS_LOG[6:12]) == set(plan[2:8])                     # then nothing of the plan is skipped
    assert len(TS_LOG) == len(set(TS_LOG))


def test_without_searxng_the_company_model_starts_at_the_top_of_the_plan():
    state = run(wide.search(engine_for(sources=("greenhouse", "lever", "ashby", "techsara_web"), searxng_url="", searxng_queries_per_search=12),
                            result_count=50))
    assert wide.SEARX_LOG == [] and set(TS_LOG[:3]) == set(state.query_plan[:3]) and len(TS_LOG) == len(set(TS_LOG))
    assert state.query_tail == state.query_plan and state.stats["query_ledger"]["by_engine"] == {TECHSARA_WEB: len(TS_LOG)}


def test_a_model_that_does_not_answer_is_left_alone_and_its_queries_stay_free():
    TS_MODE["status"] = 503
    wide.SEARX_ANSWERS["*"] = KNOWN
    engine = engine_for(searxng_queries_per_search=12, techsara_web_searches_base=12)
    state = run(wide.search(engine, result_count=50))
    assert state.status == "completed" and state.stats["tsweb_failed"] == 3 and state.stats["tsweb_searches"] == 3
    assert len(TS_LOG) == 6 and engine.tsweb.usage["retries"] == 3                                  # every search was repeated once after the 503
    assert any("company model's web search did not answer" in w for w in state.warnings)
    assert len(wide.SEARX_LOG) >= 6                                                                 # SearXNG went on with its own plan


def test_tinyfish_gets_queries_no_other_engine_asked():
    wide.SEARX_ANSWERS["*"] = KNOWN
    engine = engine_for(sources=("greenhouse", "lever", "ashby", "searxng", "techsara_web", "tinyfish"), tinyfish_api_key="tiny-secret",
                        searxng_queries_per_search=8, techsara_web_searches_base=8, tinyfish_search_queries_per_search=8)
    state = run(wide.search(engine, result_count=50))
    searx = [q for q, _ in wide.SEARX_LOG]
    assert TINY_LOG and all(q.endswith(" jobs") and '"' not in q for q in TINY_LOG)                 # its own spelling of a plan query
    assert not keys(TINY_LOG) & keys(searx) and not keys(TINY_LOG) & keys(TS_LOG) and not keys(searx) & keys(TS_LOG)
    assert keys(TINY_LOG) <= keys(state.query_tail) and state.stats["tiny_searches"] == len(TINY_LOG)
    by_engine = state.stats["query_ledger"]["by_engine"]
    assert by_engine == {SEARX: len(searx), TECHSARA_WEB: len(TS_LOG), "TinyFish": len(TINY_LOG)}
    assert state.stats["query_ledger"]["asked"] == len(keys([*searx, *TS_LOG, *TINY_LOG]))          # every request of the search was a different one


# ---------------------------------------------------------------- AWS
def test_aws_moves_to_a_host_nobody_asked_and_spends_nothing_on_a_query_asked_everywhere():
    s = wide.wide_settings(searxng_url="", aws_web_search_enabled=True, aws_web_search_published_days=0)
    engine = wide.wide_engine(ai=wide.WideAI(available=False), settings=s, sources=("greenhouse", "lever", "aws_web_search"))
    fake = FakeAws({"ats": [], "general": []})
    quota = MemoryQuota()
    engine.aws = AwsWebSearch(s, quota, client=fake)

    async def go():
        state = SearchState("s", 50)
        state.plan = dataclasses.replace(plan_for(s, 50), aws_calls=4)
        state.scope = Scope.of(["greenhouse", "lever"], False, engine.specs)               # a filter: ATS hosts only, no open-web query
        await engine._aws_prepare(state, make_request(), ["AI Engineer"], ["remote", "Texas"])
        hosts = engine._hosts(state)
        ledger = engine._ledger(state)
        ledger.claim(SEARX, site_query("AI Engineer", "job-boards.greenhouse.io", "remote"))          # SearXNG asked this one already
        for host in hosts:
            ledger.claim(SEARX, site_query("AI Engineer", host, "Texas"))                             # ... and this pair on every host
        await engine._aws_round(state, 4)
        await engine.aclose()
        return state, hosts

    state, hosts = run(go())
    assert len(fake.params) == 1 and state.stats["aws_queries"] == 1 == run(quota.used())            # the Texas pair cost nothing
    sent = fake.params[0]
    assert sent["searchQuery"] == "AI Engineer jobs remote" and sent["filters"]["domainFilter"]["include"] == ["jobs.lever.co"]
    assert hosts[0] == "boards.greenhouse.io" or "job-boards.greenhouse.io" in hosts
    assert state.stats["query_ledger"]["by_engine"][AWS] == 1 and state.stats["query_ledger"]["duplicates_avoided"] >= 3


# ---------------------------------------------------------------- AI web search inside the engine
class EngineProvider:
    """Stands in for OpenAI / Claude inside a real orchestrator: web search is recorded, every other call fails."""
    model = "fake"

    def __init__(self, name, fail=0):
        self.name, self.fail, self.calls, self.usage = name, fail, [], {}

    async def json_call(self, *args, **kwargs):
        raise ProviderError("not part of this test")

    async def web_discover(self, system, prompt, max_searches, restrict, **extra):
        self.calls.append({"prompt": prompt, **extra})
        if self.fail:
            self.fail -= 1
            raise ProviderError("HTTP 500: request failed")
        return [Candidate(url=f"https://jobs.lever.co/{self.name.lower()}{len(self.calls)}", provider=self.name, cited=True)], max_searches


def real_ai(settings, **kw) -> AIOrchestrator:
    return AIOrchestrator(settings, EngineProvider("OpenAI", **kw), EngineProvider("Claude"))


def test_the_engine_holds_the_ai_web_search_to_the_hosts_the_search_can_use():
    settings = wide.wide_settings(searxng_url="")
    ai = real_ai(settings)
    state = run(wide.search(wide.wide_engine(ai, settings, sources=("greenhouse", "lever", "ashby")), result_count=50, ats_platforms=["lever"]))
    calls = [*ai.openai.calls, *ai.claude.calls]
    assert calls and all(c["allowed_domains"] == ["jobs.lever.co"] and "blocked_domains" not in c for c in calls)
    assert all(line.split(". ", 1)[1].startswith("site:jobs.lever.co ") for c in calls for line in c["prompt"].splitlines() if ". site:" in line)
    assert state.stats["filtered_out"] == 0 and state.status == "completed"       # nothing was paid for and then dropped
    # A chosen platform nothing can be read of: the AI web search is not asked at all.
    idle = real_ai(settings)
    run(wide.search(wide.wide_engine(idle, settings, sources=("greenhouse",)), result_count=50, ats_platforms=["lever"]))
    assert not idle.openai.calls and not idle.claude.calls and idle.discovery_calls == 0
    # No filter: the hosts of the readers that are switched on and of the page-verified platforms, nothing else.
    free = real_ai(settings.model_copy(update={"ai_open_web_share": 0.0}))
    run(wide.search(wide.wide_engine(free, free.settings, sources=("greenhouse", "lever", "ashby")), result_count=50))
    usable = {"boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com", "jobs.smartrecruiters.com", "applytojob.com"}
    assert {h for c in free.openai.calls + free.claude.calls for h in c.get("allowed_domains", [])} == usable
    assert usable < set(site_hosts(settings.searxng_sites))                       # Workable, Workday, Recruitee: their readers are off here
    # ... and the calls about employers' own career sites (half of them by default) block those hosts, the hosts of
    # the readers that are off, and the platforms that may not be read at all.
    mixed = real_ai(settings)
    run(wide.search(wide.wide_engine(mixed, settings, sources=("greenhouse", "lever", "ashby")), result_count=50))
    open_web = [c for c in mixed.openai.calls + mixed.claude.calls if "blocked_domains" in c]
    assert open_web and len(open_web) * 2 >= len(mixed.openai.calls + mixed.claude.calls)
    for call in open_web:
        assert usable | {"myworkdayjobs.com", "apply.workable.com", "recruitee.com", "icims.com", "successfactors.com", "bamboohr.com",
                         "pinpointhq.com", "ats.rippling.com", "linkedin.com", "indeed.com"} <= set(call["blocked_domains"])
        assert "allowed_domains" not in call


def test_a_lead_on_a_platform_that_may_not_be_read_goes_nowhere():
    """iCIMS, SAP SuccessFactors and BambooHR forbid automated reading; the Rippling and Pinpoint readers are off. A lead
    on one of their hosts is dropped in the lead routing: no board read, no page verification, no robots.txt request."""
    refused = ["https://careers-acme.icims.com/jobs/1234/ai-engineer/job", "https://career5.successfactors.com/career?company=acme&career_job_req_id=9",
               "https://acme.bamboohr.com/careers/55", "https://ats.rippling.com/acme/jobs/11111111-1111-4111-8111-111111111111",
               "https://acme.pinpointhq.com/en/postings/22222222-2222-4222-8222-222222222222", "https://careers.smartrecruiters.com/Acme"]
    allowed = "https://careers.example.com/jobs/777-ai-engineer"
    leads = [Candidate(url=u, title="AI Engineer", company="Acme", provider="OpenAI", cited=True) for u in [*refused, allowed]]
    engine = wide.wide_engine(wide.WideAI(leads=leads, available=True), wide.wide_settings(searxng_url=""))
    opened: list[str] = []
    verify = engine.verifier.verify

    async def spy(cand, *args, **kwargs):
        opened.append(cand.url)
        return await verify(cand, *args, **kwargs)

    engine.verifier.verify = spy
    state = run(wide.search(engine, result_count=50))
    assert opened == [allowed] and state.stats["leads_not_readable"] == len(refused)
    requested = [str(url) for url in conftest.CALLS]                             # every address this test's engine asked for
    assert not any(host in call for call in requested for host in ("icims.com", "successfactors.com", "bamboohr.com", "rippling.com",
                                                                    "pinpointhq.com", "careers.smartrecruiters.com"))
    assert state.stats["providers"]["OpenAI"]["leads"] == 1 and state.status == "completed"        # only the usable lead is counted as one
    scope = Scope.of([], False, engine.specs)
    assert not any(scope.readable_url(u) for u in refused) and scope.readable_url(allowed) and not scope.readable_url("::not an address::")
    assert set(scope.unreadable_hosts()) >= {"icims.com", "successfactors.com", "successfactors.eu", "bamboohr.com", "pinpointhq.com", "ats.rippling.com"}
    assert "jobs.smartrecruiters.com" not in scope.unreadable_hosts() and scope.readable("jobs.jobvite.com")      # verified page by page


def test_the_first_ai_calls_are_told_which_employers_the_index_already_matches():
    settings = wide.wide_settings(searxng_url="")
    db = Database(":memory:")
    cold = real_ai(settings)
    run(wide.search(wide.wide_engine(cold, settings, db=db), result_count=50))
    assert "prefer other employers in your list: acme, beta, gamma." in cold.claude.calls[0]["prompt"]        # an empty index: the known boards
    assert "choose other employers: acme, beta, gamma." in cold.openai.calls[0]["prompt"]                     # the open-web call is told too
    warm = real_ai(settings)
    state = run(wide.search(wide.wide_engine(warm, settings, db=db), result_count=50, ats_platforms=["lever"]))
    assert "prefer other employers in your list: beta." in warm.openai.calls[0]["prompt"]                     # the Lever employer with matching postings
    assert "acme" not in warm.openai.calls[0]["prompt"] and state.known_employers.result() == ["lever:beta"]
    assert SearchEngine._known_employers([]) == []


def test_a_provider_that_fails_is_reported_and_the_search_goes_on_with_the_other():
    settings = wide.wide_settings(searxng_url="", openai_search_concurrency=1)
    ai = real_ai(settings, fail=99)
    state = run(wide.search(wide.wide_engine(ai, settings), result_count=50))
    assert state.status == "completed" and state.results
    assert any("OpenAI web search was switched off" in w and "Claude took over its searches" in w for w in state.warnings)
    assert len(ai.openai.calls) == 2 and ai.status["Claude"]["takeovers"] >= 2
    assert {c["prompt"] for c in ai.openai.calls} <= {c["prompt"] for c in ai.claude.calls}       # the very same searches went to Claude
