"""Paid sources (SerpApi Google Jobs, TinyFish) and their spending guards. No network: mock transports only."""
import asyncio
import json

import httpx
import pytest

from app.database import Database
from app.job_index import as_index
from app.providers.paid import SerpApiClient, TinyFishClient, aggregator_hosts, is_aggregator, serp_item
from app.config import Settings
from app.search_engine import SearchEngine
from tests import conftest
from tests import test_wider_search as wide
from tests.conftest import CALLS, FakeBrowser, RenderResult

HOSTS = aggregator_hosts(Settings(_env_file=None).aggregator_hosts)
LOG: list = []
MODE = {"serp_status": 200, "account_left": 249, "wallet": "10", "serp_items": [], "tiny_urls": [], "tiny_html": ""}


def run(coro):
    return asyncio.run(coro)


def item(title, company, links, via="LinkedIn", location="Austin, TX"):
    return {"title": title, "company_name": company, "location": location, "via": via, "job_id": "x" * 40,
            "description": "Build ML systems with Python and PyTorch. 3+ years of experience.",
            "job_highlights": [{"title": "Qualifications", "items": ["Python", "PyTorch"]}],
            "apply_options": [{"title": "Apply", "link": l} for l in links], "detected_extensions": {"salary": "$150K a year"}}


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url).split("?")[0]
    if url == "https://serpapi.com/account.json":
        LOG.append(("account", ""))
        return httpx.Response(200, json={"total_searches_left": MODE["account_left"], "searches_per_month": 250}) if MODE["account_left"] is not None else httpx.Response(500)
    if url == "https://serpapi.com/search":
        LOG.append(("serp", request.url.params.get("q"), request.url.params.get("location"), request.url.params.get("next_page_token")))
        assert request.url.params["engine"] == "google_jobs" and request.url.params["api_key"] == "serp-secret"
        if MODE["serp_status"] != 200:
            return httpx.Response(MODE["serp_status"], text="no")
        page2 = bool(request.url.params.get("next_page_token"))
        items = [] if page2 else MODE["serp_items"]
        return httpx.Response(200, json={"jobs_results": items, "serpapi_pagination": {} if page2 else {"next_page_token": "tok"}})
    if url == "https://agent.tinyfish.ai/v1/wallet":
        return httpx.Response(200, json={"available_balance": MODE["wallet"], "currency": "USD"})
    if url == "https://api.search.tinyfish.ai/":
        LOG.append(("tinysearch", request.url.params.get("query")))
        assert request.headers["x-api-key"] == "tiny-secret"
        return httpx.Response(200, json={"results": [{"url": u, "title": "t", "snippet": "s", "position": i} for i, u in enumerate(MODE["tiny_urls"])]})
    if url == "https://api.fetch.tinyfish.ai/":
        body = json.loads(request.content)
        LOG.append(("tinyfetch", body["urls"][0]))
        return httpx.Response(200, json={"results": [{"url": body["urls"][0], "title": "AI Engineer", "text": MODE["tiny_html"], "format": "html"}], "errors": []})
    assert "agent.tinyfish.ai/v1/automation" not in url and "/run" not in url          # agent endpoints are never called
    return wide.handler(request)


@pytest.fixture(autouse=True)
def _reset():
    LOG.clear()
    MODE.update(serp_status=200, account_left=249, wallet="10", serp_items=[], tiny_urls=[], tiny_html="")


def http():
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def engine_for(ai=None, db=None, sources=("greenhouse", "lever", "ashby", "serpapi", "tinyfish"), browser=None, **over):
    settings = wide.wide_settings(searxng_url="", serpapi_api_key="serp-secret", tinyfish_api_key="tiny-secret", **over)
    ai = ai or wide.WideAI(available=False)
    engine = SearchEngine(settings, db or Database(":memory:"), client=http(), seeds=conftest.DEFAULT_SEEDS, ai_factory=lambda: ai,
                          sources=list(sources), browser=browser or FakeBrowser(available=False))
    engine.limiter.intervals = {}
    return engine


# ---------------------------------------------------------------- SerpApi: budget
def test_serpapi_counts_every_billed_call_per_month_and_serves_repeats_from_cache():
    index = as_index(Database(":memory:"))
    client = SerpApiClient("serp-secret", http(), index, monthly_limit=250, reserve=25)
    MODE["serp_items"] = [item("AI Engineer", "Acme", ["https://www.linkedin.com/jobs/view/1"])]

    async def go():
        first = await client.search("AI Engineer", "United States")
        again = await client.search("AI Engineer", "United States")
        page2 = await client.search("AI Engineer", "United States", first[1])
        return first, again, page2, await client.used_this_month(), await client.remaining()

    first, again, page2, used, remaining = run(go())
    assert first[2] == "" and len(first[0]) == 1 and first[1] == "tok" and again[2] == "cached" and again[0] == first[0]
    assert page2[0] == [] and used == 2 and client.calls == 2                         # the cached repeat cost nothing
    assert remaining == 249 - 2 - 25 and [e[0] for e in LOG].count("account") == 1     # account read once, not per call
    assert "job_id" not in first[0][0] and "api_key" not in json.dumps(first[0])


def test_serpapi_stops_at_the_monthly_allowance_minus_the_reserve():
    index = as_index(Database(":memory:"))
    MODE["account_left"] = 27                                       # the account says 27 left; 25 are kept back

    async def go():
        client = SerpApiClient("serp-secret", http(), index, monthly_limit=250, reserve=25, cache_hours=0)
        notes = [(await client.search(f"q{i}", "United States"))[2] for i in range(4)]
        return notes, await client.used_this_month()

    notes, used = run(go())
    assert notes == ["", "", "budget", "budget"] and used == 2 and [e[0] for e in LOG].count("serp") == 2
    # Account endpoint unreachable: our own count against SERPAPI_MONTHLY_LIMIT decides.
    MODE["account_left"] = None
    own = SerpApiClient("serp-secret", http(), as_index(Database(":memory:")), monthly_limit=3, reserve=1, cache_hours=0)
    assert [run(own.search(f"y{i}", "US"))[2] for i in range(4)] == ["", "", "budget", "budget"]
    assert run(SerpApiClient("", http(), index).remaining()) == 0
    # A failed call was still spent, and failures never raise.
    MODE.update(account_left=249, serp_status=500)
    failing = SerpApiClient("serp-secret", http(), as_index(Database(":memory:")), cache_hours=0)
    assert run(failing.search("q", "US")) == ([], "", "HTTP 500") and run(failing.used_this_month()) == 1


# ---------------------------------------------------------------- SerpApi: handling rules
def test_google_jobs_items_become_leads_or_listed_jobs_and_aggregators_are_never_leads():
    lead, job = serp_item(item("AI Engineer", "Acme", ["https://www.linkedin.com/jobs/view/1", "https://boards.greenhouse.io/acme/jobs/1001"]), HOSTS, True)
    assert lead.url == "https://boards.greenhouse.io/acme/jobs/1001" and lead.provider == "Google Jobs" and job is None      # rule 1: readable ATS
    lead, job = serp_item(item("AI Engineer", "GM", ["https://www.indeed.com/viewjob?jk=1", "https://search-careers.gm.com/en/jobs/jr-1/"], via="General Motors Careers"), HOSTS, True)
    assert lead.url.startswith("https://search-careers.gm.com/") and job is None                                             # rule 1: employer site
    links = ["https://www.linkedin.com/jobs/view/1", "https://www.dice.com/job-detail/1", "https://www.ziprecruiter.com/c/x", "https://www.talent.com/view?id=1",
             "https://jobleads.com/1", "https://www.tealhq.com/job/1", "https://www.theladders.com/job/1", "https://us.bebee.com/job/1", "https://www.recruit.net/job/1",
             "https://www.simplyhired.com/job/1", "https://www.glassdoor.com/job-listing/1"]
    assert all(is_aggregator(l, HOSTS) for l in links) and not is_aggregator("https://careers.fedex.com/job/1", HOSTS)
    lead, job = serp_item(item("AI Engineer", "Acme", links, via="Dice"), HOSTS, True)                                       # rule 3: listed only
    assert lead is None and job.verification_level == "listed" and job.aggregator and job.url == links[0] and job.source == "Google Jobs"
    assert job.verification == "Listed on Google Jobs via Dice (SerpApi API); not checked on the employer's site"
    assert job.sources == ["Google Jobs (via Dice)"] and "PyTorch" in job.description and job.salary == "$150K a year"
    assert serp_item(item("AI Engineer", "Acme", links), HOSTS, False) == (None, None)                                        # setting: exclude such jobs
    for bad in (item("", "Acme", links), item("AI Engineer", "", links), item("AI Engineer", "Acme", []), item("AI Engineer", "Acme", ["http://x.example/1", "javascript:alert(1)"])):
        assert serp_item(bad, HOSTS, True) == (None, None)


def test_engine_uses_google_jobs_only_after_the_free_sources_and_applies_the_rules():
    MODE["serp_items"] = [
        item("AI Engineer 1", "Newco", ["https://www.linkedin.com/jobs/view/9", "https://boards.greenhouse.io/newco/jobs/5001"]),      # verified through the board
        item("AI Engineer", "Listed Only Inc", ["https://www.linkedin.com/jobs/view/7", "https://www.dice.com/job-detail/7"], via="Dice"),
        item("AI Engineer", "Gone Corp", ["https://careers.example.com/jobs/gone"], via="Gone Careers"),                                   # employer page answers 404
        item("AI Engineer", "Abroad Ltd", ["https://www.linkedin.com/jobs/view/8"], location="Berlin, Germany"),                            # filtered like any other
        item("Account Executive", "Sales Co", ["https://www.linkedin.com/jobs/view/6"]),
    ]
    db = Database(":memory:")
    state = run(wide.search(engine_for(db=db, serpapi_max_calls_per_search=4), result_count=50))
    serp_calls = [e for e in LOG if e[0] == "serp"]
    assert serp_calls and state.stats["serp_calls"] == len(serp_calls) <= 4 and state.stats["rounds"][0]["leads"] == 0       # nothing paid in round 1
    assert serp_calls[0][1:3] == ("AI Engineer", "United States")
    by_company = {j.company: j for j in state.results}
    listed = by_company["Listed Only Inc"]
    assert listed.verification_level == "listed" and "not checked on the employer's site" in listed.verification and listed.url.startswith("https://www.linkedin.com/")
    newco = [j for j in state.results if j.board == "newco"]
    assert len(newco) == 6 and all(j.verification_level == "verified" and j.source == "Greenhouse" for j in newco)          # the lead registered the whole board
    assert "Gone Corp" not in by_company and "Abroad Ltd" not in by_company and "Sales Co" not in by_company
    assert not any(h in u for u in CALLS for h in ("linkedin", "dice.com", "indeed"))                                         # job-board pages never fetched
    assert state.stats["serp_listed"] >= 1 and state.stats["serp_jobs"] == 5 and db.known_boards() == [("greenhouse", "newco")]
    assert len([j for j in state.results if j.verification_level == "listed"]) == 1
    off = run(wide.search(engine_for(serpapi_include_listed_only=False, serpapi_cache_hours=0), result_count=50))
    assert not any(j.verification_level == "listed" for j in off.results)
    LOG.clear()
    enough = run(wide.search(engine_for(), result_count=10))                                                                   # target met by free sources
    assert not [e for e in LOG if e[0] in ("serp", "tinysearch")] and enough.stats["serp_calls"] == 0 and len(enough.results) == 10


def test_engine_says_when_google_jobs_was_skipped_because_the_allowance_is_used_up():
    MODE["account_left"] = 20                                       # below the reserve of 25
    state = run(wide.search(engine_for(), result_count=50))
    assert state.status == "completed" and state.results and state.stats["serp_calls"] == 0
    assert "allowance is used up" in state.stats["serp_skipped"] and any("Google Jobs (SerpApi) was skipped" in w for w in state.warnings)
    assert not [e for e in LOG if e[0] == "serp"]


# ---------------------------------------------------------------- TinyFish
def test_tinyfish_spending_guards():
    index = as_index(Database(":memory:"))
    MODE["tiny_urls"] = ["https://boards.greenhouse.io/newco"]

    async def go():
        client = TinyFishClient("tiny-secret", http(), index, daily_usd=0.012)
        out = [await client.search(f"q{i}") for i in range(4)]
        return client, out, await client.spent_today()

    client, out, spent = run(go())
    assert [o[1] for o in out] == ["", "", "daily spending ceiling reached", "daily spending ceiling reached"]
    assert abs(spent - 0.010) < 1e-9 and abs(client.spent - 0.010) < 1e-9 and client.searches == 2 and len([e for e in LOG if e[0] == "tinysearch"]) == 2
    MODE.update(wallet="2.004", tiny_html="<p>page</p>")
    low = TinyFishClient("tiny-secret", http(), as_index(Database(":memory:")), wallet_reserve_usd=2.0)
    assert run(low.search("q"))[1] == "wallet balance is at the reserve" and run(low.fetch_html("https://x.example/1"))[1] == ""       # 0.001 still fits
    assert run(TinyFishClient("", http(), index).search("q")) == ([], "not configured")


def test_tinysearch_is_a_second_discovery_engine_and_tinyfetch_reads_pages_the_browser_could_not():
    MODE["tiny_urls"] = ["https://boards.greenhouse.io/thirdco", "https://www.linkedin.com/jobs/view/1", "https://evil.example/boards.greenhouse.io/x"]
    MODE["tiny_html"] = "<h1>AI Engineer</h1>" + conftest.JSONLD % ("AI Engineer", "JS Example Inc") + "<p>Apply now</p>"
    leads = {2: [wide.Candidate(url="https://careers.example.com/jobs/js-1", title="AI Engineer", company="JS Example Inc", provider="OpenAI", cited=True)]}
    ai = wide.WideAI(available=True, round_leads=leads)
    db = Database(":memory:")
    state = run(wide.search(engine_for(ai, db=db, sources=("greenhouse", "lever", "ashby", "tinyfish")), result_count=50))
    assert len([j for j in state.results if j.board == "thirdco"]) == 4 and db.known_boards() == [("greenhouse", "thirdco")]
    with db._lock:
        assert db._db.execute("SELECT origin FROM boards WHERE slug='thirdco'").fetchone()[0] == "tinyfish"
    fetched = [j for j in state.results if j.company == "JS Example Inc"]
    assert len(fetched) == 1 and "read through TinyFetch" in fetched[0].verification and ("tinyfetch", "https://careers.example.com/jobs/js-1") in LOG
    assert state.stats["tiny_searches"] >= 1 and state.stats["tiny_fetches"] == 1
    assert abs(state.stats["tiny_usd"] - (state.stats["tiny_searches"] * 0.005 + 0.001)) < 1e-6
    assert not any("linkedin" in u or "evil.example" in u for u in CALLS)
    # A site that refused our automated browser is not read through the service either.
    LOG.clear()
    blocked = {2: [wide.Candidate(url="https://careers.example.com/jobs/blocked-1", title="AI Engineer", company="X", provider="OpenAI", cited=True)]}
    state = run(wide.search(engine_for(wide.WideAI(available=True, round_leads=blocked), sources=("greenhouse", "lever", "ashby", "tinyfish"),
                                       browser=FakeBrowser()), result_count=50))
    assert not [e for e in LOG if e[0] == "tinyfetch"] and state.stats["tiny_fetches"] == 0
