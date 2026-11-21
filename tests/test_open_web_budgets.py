"""The AI web search at full strength on both jobs (2026-11-21): calls on employers' own career sites come on top of
the calls on ATS hosts and every round makes its calls again; follow-up calls on the career sites found; the open-web
queries of the plain search engines and of AWS web search; and the lead check, which asks every site for a few pages
first, has a time box, and lets what is still on its way finish in the background and be remembered.
No network: providers are stand-ins and every page is a synthetic one of tests/conftest.py."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.ai_orchestrator import FOLLOW_UP, recency_words
from app.config import Settings
from app.database import Database
from app.providers import aws_web_search
from app.providers.aws_web_search import AwsWebSearch, MemoryQuota
from app.providers.registry import CAREER_PAGE
from app.schemas import Candidate, Profile
from app.search_engine import OPEN_WEB_QUERY_FORMS, PROBES_PER_SITE
from app.search_plan import ROUND_GROWTH, Plan, plan_for
from app.search_scope import Scope
from tests import conftest
from tests import test_wider_search as wide
from tests.conftest import CALLS, JSONLD, make_request
from tests.test_ai_discovery import READERS, Recorder, orchestrator, searches_of

LIVE = "https://careers.example.com/jobs/ai-9"            # JobPosting "AI Engineer - Platform" at Example Careers Inc, Denver


def run(coro):
    return asyncio.run(coro)


def posting(title: str, company: str) -> tuple[int, str]:
    return 200, f"<html><head><title>{title}</title>" + JSONLD % (title, company) + "</head><body>Apply</body></html>"


@pytest.fixture
def pages():
    """Synthetic pages of this test only."""
    added: list[str] = []

    def add(url: str, page: tuple[int, str]) -> str:
        conftest.PAGES[url] = page
        added.append(url)
        return url
    yield add
    for url in added:
        conftest.PAGES.pop(url, None)


# ---------------------------------------------------------------- the plan: nothing is divided, nothing is rationed
def test_a_round_makes_its_calls_again_until_a_safety_ceiling():
    s = Settings(_env_file=None)
    plan = plan_for(s, 25)
    # One round of a normal search: OpenAI and Claude on the ATS hosts, and on top OpenAI and Claude on employers' own sites.
    assert plan.base == (6, 6, 3, 6) and plan.ai_round("OpenAI", 1) == (6, 3) and plan.ai_round("Claude", 1) == (6, 6)
    assert (plan.web_searches, plan.open_searches, plan.followup_calls, plan.open_engine_queries, plan.page_verifications) == (5, 10, 3, 12, 400)
    # Every further round makes them again; the totals are safety ceilings of one search, far above a normal one.
    assert plan.ai_round("Claude", 5) == plan.ai_round("Claude", 16) == (6, 6) and plan.ai_round("Claude", 17) == (0, 0)
    capped = plan_for(Settings(_env_file=None, claude_search_calls_max=9, claude_open_web_calls_max=6), 25)
    assert [capped.ai_round("Claude", r) for r in (1, 2, 3)] == [(6, 6), (3, 0), (0, 0)]         # each kind stops at its own ceiling
    # A larger request makes up to twice a round's calls, and more rounds; never more a round (they would only queue).
    assert ROUND_GROWTH == 2.0 and plan_for(s, 60).base == (8, 8, 4, 8) and plan_for(s, 175).base == plan_for(s, 1000).base == (12, 12, 6, 12)
    assert plan_for(s, 175).followup_calls == 6
    # ... and the lead check of such a round gets up to twice the time (LEAD_VERIFICATION_SECONDS; 0 = no time box).
    assert (plan.lead_seconds, plan_for(s, 60).lead_seconds, plan_for(s, 175).lead_seconds) == (120.0, 144.0, 240.0)
    assert plan_for(Settings(_env_file=None, lead_verification_seconds=0), 175).lead_seconds == 0.0
    # The old share setting is the switch it also was: 0 = nothing looks at the open web.
    off = plan_for(Settings(_env_file=None, ai_open_web_share=0), 25)
    assert off.base == (6, 6, 0, 0) and (off.openai_open_calls, off.claude_open_calls, off.followup_calls, off.open_engine_queries, off.aws_open_share) == (0, 0, 0, 0, 0.0)
    # A single pass (SEARCH_MAX_ROUNDS=1) makes one round's calls.
    single = plan_for(Settings(_env_file=None, search_max_rounds=1), 25)
    assert single.rounds == 1 and (single.openai_calls, single.claude_open_calls) == (6, 6) and single.ai_round("OpenAI", 2) == (0, 0)
    # The web search leaves the calls the judging of the result list needs.
    assert plan.ai_reserve == 22 and plan.ai_calls - plan.ai_reserve >= 96 + 96 + 2 * 48 + 96
    # A plan built by hand (no round values) spreads its totals evenly over the rounds, as before.
    hand = Plan(target=50, rounds=4, openai_calls=8, claude_calls=4, web_searches=5, ai_calls=30, ai_ranked=100, searx_queries=12, new_boards=40,
                page_verifications=160, detail_fetches=150)
    assert hand.ai_round("OpenAI", 1) == (2, 0) and hand.ai_round("Claude", 4) == (1, 0)
    # AWS web search: the paced share of the month's allowance, the base part of it in the first round.
    assert [plan.first_round(15, 12, r) for r in (1, 2, 3, 4, 5)] == [12, 1, 1, 1, 0] and single.first_round(15, 12, 1) == 15


# ---------------------------------------------------------------- follow-up calls
def test_follow_up_calls_search_the_career_sites_found_once_more():
    posts = lambda site, n: [f"https://{site}/job/R-{i}/ai-engineer" for i in range(n)]
    stale = posts("careers.old.example", 2)
    leads = [*posts("careers.a.example", 3), *posts("jobs.b.example", 2), *stale, "https://careers.a.example/search?q=ai"]

    class Dated(Recorder):
        async def web_discover(self, *args, **kwargs):
            found, searches = await super().web_discover(*args, **kwargs)
            found.ages = {u: 400 for u in stale}
            return found, searches

    budgets = dict(openai_search_calls=0, claude_search_calls=0, claude_open_web_calls=1, ai_open_web_followup_calls=2, ai_open_web_searches_per_call=4)
    claude = Dated("Claude", leads, picks=0)

    class Sited(Dated):                                   # the sites the executed searches named with `site:` (the tool's own record)
        async def web_discover(self, *args, **kwargs):
            found, searches = await super().web_discover(*args, **kwargs)
            found.sites = frozenset({"careers.a.example", "jobs.b.example", "careers.old.example"})
            return found, searches

    claude = Sited("Claude", leads, picks=0)
    ai = orchestrator(target=50, openai=Recorder("OpenAI", []), claude=claude, **budgets)
    ai.note_verified(["https://careers.known.example/job/1-ai-engineer"], relevant=True)       # remembered from an earlier search, title fits
    ai.note_verified(["https://jobs.b.example/job/R-9/data-analyst"])                          # verified before, another title
    found = run(ai.discover(Profile(), make_request(locations=["Texas"]), [], []))
    first, follow = claude.calls[0], claude.calls[1:]
    assert not first["restrict"] and len(follow) == 1 and ai.status["Claude"]["follow_up_calls"] == 1
    call = follow[0]
    # Held to exactly the sites: first the site with a verified posting of a wanted title, then a site whose pages were
    # verified before, then the site with the most postings the search tool does not date long ago. The site whose
    # postings are all dated long ago is not searched again, and neither is anything twice.
    sites = ["careers.known.example", "jobs.b.example", "careers.a.example"]
    assert call["restrict"] is True and call["allowed_domains"] == sites and "blocked_domains" not in call and call["own_searches"] is False
    assert searches_of(call["prompt"]) == ['site:careers.known.example "AI Engineer" Texas', 'site:jobs.b.example "AI Engineer" remote',
                                           'site:careers.a.example "AI Engineer" Texas']          # the requested title with the candidate's places
    assert FOLLOW_UP in call["prompt"] and "Run exactly these 3 web searches" in call["prompt"] and "DIRECT posting pages" in call["prompt"]
    assert ai._followed == set(sites) and ai.calls == ai.discovery_calls == 2 and ai._opened == {"Claude": 2}
    # The leads of both calls are cleaned like those of any open-web call: postings on the call's sites, no search page.
    assert sorted(lead.url for lead in found) == sorted(leads[:-1]) and [lead.url for lead in found][-2:] == stale
    # The next round follows up only what is new: nothing here, so no call is made and no unit is spent.
    run(ai.discover_round(2, Profile(), make_request(locations=["Texas"]), ["AI Engineer"], ["Texas"], []))
    assert len(claude.calls) == 3 and not claude.calls[2]["restrict"] and ai.calls == 3
    # A candidate without a place: the current month and year make the second search of a site a different one.
    anywhere = orchestrator(target=50, openai=Recorder("OpenAI", []), claude=Sited("Claude", leads, picks=0), **budgets)
    run(anywhere.discover(Profile(), make_request(work_mode="Remote"), [], []))
    assert searches_of(anywhere.claude.calls[1]["prompt"]) == [f'site:careers.a.example "AI Engineer" {recency_words()}',
                                                               f'site:jobs.b.example "AI Engineer" {recency_words()}']
    # Never under a platform filter, and not with the open web switched off.
    for held in (orchestrator(target=50, chosen=["lever"], claude=Sited("Claude", leads, picks=0), **{**budgets, "claude_search_calls": 1}),
                 orchestrator(target=50, claude=Sited("Claude", leads, picks=0), **{**budgets, "claude_search_calls": 1, "ai_open_web_share": 0})):
        run(held.discover(Profile(), make_request(), [], []))
        assert len(held.claude.calls) == 1 and held.claude.calls[0]["restrict"] and held._follow_up(2, 5) == []


# ---------------------------------------------------------------- the lead check
def spy_on(engine) -> list[tuple[str, bool]]:
    """Every page the verifier is asked for, with whether the browser was allowed."""
    asked: list[tuple[str, bool]] = []
    verify = engine.verifier.verify

    async def spy(cand, budget=None, **kwargs):
        asked.append((cand.url, budget is not None))
        return await verify(cand, budget, **kwargs)
    engine.verifier.verify = spy
    return asked


def test_the_lead_check_asks_every_site_for_a_few_pages_first_and_leaves_a_site_that_refuses(pages):
    good = [pages(f"https://jobs.open.example/job/{i}-ai-engineer", posting(f"AI Engineer {i}", "Open Example Co")) for i in range(1, 5)]
    refused = [pages(f"https://careers.refuses.example/job/{i}-ai-engineer", (403, "forbidden")) for i in range(1, 6)]
    leads = [Candidate(url=u, provider="Claude", cited=True) for u in [*refused, *good]]         # the refusing site comes first
    engine = wide.wide_engine(wide.WideAI(leads, available=True), wide.wide_settings(searxng_url=""))
    asked = spy_on(engine)
    state = run(wide.search(engine, result_count=50))
    order = [url for url, _ in asked]
    # First two pages of each site (PROBES_PER_SITE), in lead order; then the other pages of the site that verified.
    # The three other pages of the site that answered 403 twice are never requested.
    assert PROBES_PER_SITE == 2 and order == [*refused[:2], *good[:2], *good[2:]] and not any(browser for _, browser in asked)
    assert all(CALLS[u] == 0 for u in refused[2:]) and state.stats["pages_site_refused"] == 3
    assert {j.url for j in state.results} >= set(good) and state.stats["rejected_unverified"] >= 5
    assert state.stats["lead_check_seconds"] >= 0 and state.stats["pages_late"] == 0


def test_the_lead_check_has_a_time_box_and_what_finishes_later_is_remembered(pages):
    slow = pages("https://careers.slow.example/job/7-ai-engineer", posting("AI Engineer - Search", "Slow Example Co"))
    leads = [Candidate(url=slow, provider="Claude", cited=True), Candidate(url=LIVE, provider="Claude", cited=True)]
    sources = ("greenhouse", "lever", "ashby", CAREER_PAGE)

    def engine_with(box: float, db=None):
        engine = wide.wide_engine(wide.WideAI(leads, available=True), wide.wide_settings(searxng_url="", lead_verification_seconds=box,
                                                                                          lead_verification_background_seconds=5.0),
                                  db=db, sources=sources)
        verify = engine.verifier.verify

        async def delayed(cand, *args, **kwargs):
            if cand.url == slow:
                await asyncio.sleep(1.2)
            return await verify(cand, *args, **kwargs)
        engine.verifier.verify = delayed
        return engine

    async def go():
        db = Database(":memory:")
        engine = engine_with(0.6, db)                                # a plain page check takes 0.3 s here (the verifier's own pacing)
        state = await wide.search(engine, close=False, result_count=50)
        found = {j.url for j in state.results}
        assert LIVE in found and slow not in found                     # what was verified in time is in this search
        assert state.stats["pages_late"] == 1 and state.stats["pages_remembered"] == 1 and state.status == "completed"
        await asyncio.gather(*list(engine._background))                # the plain request still on its way finishes in the background
        assert state.stats["pages_late_verified"] == 1 and state.stats["pages_late_remembered"] == 1
        assert (CAREER_PAGE, slow) in set(await engine.index.known_boards())
        await engine.aclose()
        # The next search finds it in the index, without any web search.
        again = wide.wide_engine(wide.WideAI(available=False), wide.wide_settings(searxng_url=""), db=db, sources=sources)
        later = await wide.search(again, result_count=50)
        assert slow in {j.url for j in later.results}
        # Without a time box every check is waited for.
        patient = await wide.search(engine_with(0.0), result_count=50)
        assert {slow, LIVE} <= {j.url for j in patient.results} and patient.stats["pages_late"] == 0
        # A check that is still running when the engine closes is stopped cleanly.
        closing = engine_with(0.05)
        await wide.search(closing, close=False, result_count=50)
        waiting = list(closing._background)
        await closing.aclose()
        assert waiting and all(task.done() for task in waiting)
    run(go())
    assert Settings(_env_file=None).lead_verification_seconds == 120.0    # correct jobs before speed: the default is generous


def test_browser_and_ai_page_checks_run_only_while_time_is_left_and_time_never_accepts_a_page(monkeypatch):
    monkeypatch.setattr("app.search_engine.MIN_EXTRACTION_SECONDS", 0.05)
    claimed = Candidate(url="https://careers.example.com/jobs/plain-1", title="AI Engineer", company="Plain Example Co", location="Boston, MA",
                        provider="Claude", cited=True)                 # no structured data: only a model that reads the page can accept it
    answer = {claimed.url: {"is_open_job_posting": True, "title": "AI Engineer", "company": "Plain Example Co", "location": "Boston, MA"}}
    script = Candidate(url="https://careers.example.com/jobs/js-1", provider="Claude", cited=True)       # needs the browser

    class SlowModel(wide.WideAI):
        async def extract_pages(self, pages):
            await asyncio.sleep(self.delay)
            return await super().extract_pages(pages)

    def engine_with(box: float, delay: float, leads):
        ai = SlowModel(leads, available=True, extractions=answer)
        ai.delay = delay
        engine = wide.wide_engine(ai, wide.wide_settings(searxng_url="", lead_verification_seconds=box))
        engine.browser = engine.verifier.browser = conftest.FakeBrowser(available=True)
        return engine

    # The model does not answer in time: the page is not a job of this search. Time never accepts a page.
    cut = run(wide.search(engine_with(1.0, 5.0, [claimed]), result_count=50))
    assert claimed.url not in {j.url for j in cut.results} and cut.stats["page_checks_cut"] == 1 and cut.stats["ai_extracted"] == 0
    # With time left the model reads the page, and its answer is checked against the page as before.
    kept = run(wide.search(engine_with(30.0, 0.0, [claimed]), result_count=50))
    assert claimed.url in {j.url for j in kept.results} and kept.stats["page_checks_cut"] == 0 and kept.stats["ai_extracted"] == 1
    # The browser comes after the plain requests, for the page a plain request could not show.
    engine = engine_with(30.0, 0.0, [script, Candidate(url=LIVE, provider="Claude", cited=True)])
    asked = spy_on(engine)
    shown = run(wide.search(engine, result_count=50))
    assert asked == [(script.url, False), (LIVE, False), (script.url, True)] and engine.browser.rendered == [script.url]
    assert {script.url, LIVE} <= {j.url for j in shown.results} and shown.stats["browser_pages"] == 1
    # No time left after the plain requests: the browser is not started, and the page is not accepted.
    hurried = engine_with(0.0001, 0.0, [script])
    late = run(wide.search(hurried, result_count=50))
    assert hurried.browser.rendered == [] and script.url not in {j.url for j in late.results}


def test_every_round_may_check_its_own_page_leads():
    engine = wide.wide_engine(settings=wide.wide_settings(searxng_url="", max_page_verifications=3, max_page_verifications_max=7))
    plan = plan_for(engine.settings, 50)
    assert plan.page_verifications == 3
    # round 1: 3 pages; round 2: 3 more (and what round 1 left); the ceiling of the whole search is 7.
    budgets = []
    checked = 0
    for round_no, leads in ((1, 2), (2, 9), (3, 9)):
        total = min(plan.page_verifications * round_no, max(plan.page_verifications, engine.settings.max_page_verifications_max))
        budget = max(0, total - checked)
        checked += min(budget, leads)
        budgets.append(budget)
    assert budgets == [3, 4, 1]
    run(engine.aclose())


# ---------------------------------------------------------------- open-web queries of the plain search engines
def test_plain_search_engines_get_open_web_queries_and_only_single_postings_on_employer_sites_become_leads():
    results = [LIVE,                                                         # one posting on an employer's own site
               "https://www.indeed.com/viewjob?jk=1", "https://www.linkedin.com/jobs/view/1",        # job boards: never leads, never fetched
               "https://careers.example.com/jobs/search?q=ai", "https://careers.example.com/openings",       # a search page, a list
               "https://careers-acme.icims.com/jobs/1234/ai-engineer/job",         # a platform that may not be read
               "https://blog.example.com/how-to-become-an-ai-engineer",
               "https://boards.greenhouse.io/newco"]                             # a board on an ATS host: the usual handling
    wide.SEARX_ANSWERS["job opening"] = results
    wide.SEARX_ANSWERS["careers apply"] = results[:2]
    wide.SEARX_ANSWERS["*"] = []
    settings = wide.wide_settings(search_engine_open_web_queries=2)
    engine = wide.wide_engine(wide.WideAI(available=True), settings)
    asked = spy_on(engine)
    state = run(wide.search(engine, result_count=50))
    open_web = [q for q, _ in wide.SEARX_LOG if not q.startswith("site:")]
    assert open_web == [form.format(title="AI Engineer", place="United States") for form in OPEN_WEB_QUERY_FORMS]
    assert '"AI Engineer" job opening United States -linkedin -indeed' in open_web[0] and "site:" not in "".join(open_web)
    assert [url for url, _ in asked] == [LIVE]                                # the only page that was requested
    assert not any(host in url for url in CALLS for host in ("indeed.com", "linkedin.com", "icims.com", "blog.example.com"))
    st = state.stats
    assert (st["open_engine_queries"], st["open_engine_results"], st["open_engine_leads"]) == (2, 10, 1)
    assert LIVE in {j.url for j in state.results} and len([j for j in state.results if j.board == "newco"]) == 6
    assert st["query_ledger"]["by_engine"]["SearXNG"] >= 3 and len(wide.SEARX_LOG) == len(set(wide.SEARX_LOG))       # no query twice
    # The strict test of an address, before anything is requested.
    scope = Scope.of([], False, READERS)
    assert engine._own_site_posting("https://careers.acme.example/jobs/12345-ai-engineer", scope)
    for url in results[1:]:
        assert not engine._own_site_posting(url, scope), url
    assert not engine._own_site_posting("https://acme.bamboohr.com/careers/55", scope) and not engine._own_site_posting("http://careers.acme.example/jobs/1", scope)
    # Never under a platform filter, not with the open web switched off, not without the AI web search (a fast search).
    for over, req in ((dict(), dict(ats_platforms=["greenhouse"])), (dict(ai_open_web_share=0), {}), (dict(), dict(deep_search=False))):
        wide.SEARX_LOG.clear()
        quiet = run(wide.search(wide.wide_engine(wide.WideAI(available=True), wide.wide_settings(search_engine_open_web_queries=2, **over)),
                                result_count=50, **req))
        assert quiet.stats["open_engine_queries"] == 0 and all(q.startswith("site:") for q, _ in wide.SEARX_LOG)
    assert Settings(_env_file=None).search_engine_open_web_queries == 12


def test_tinyfish_takes_open_web_queries_within_its_own_limits():
    class Tiny:
        spent = 0.0
        enabled = True

        def __init__(self, note=""):
            self.queries, self.note = [], note

        async def search(self, query):
            self.queries.append(query)
            self.spent += 0.005
            return ([LIVE, "https://www.glassdoor.com/job/1"], "") if not self.note else ([], self.note)

    def engine_with(tiny, **over):
        s = wide.wide_settings(searxng_url="", tinyfish_api_key="tf-test", search_engine_open_web_queries=2, **over)
        engine = wide.wide_engine(wide.WideAI(available=True), s, sources=("greenhouse", "lever", "ashby", "tinyfish"))
        engine.tinyfish = tiny
        return engine

    tiny = Tiny()
    state = run(wide.search(engine_with(tiny), result_count=10))
    assert len(tiny.queries) == 2 and all("site:" not in q for q in tiny.queries) and state.stats["tiny_searches"] == 2
    assert state.stats["open_engine_leads"] == 1 and LIVE in {j.url for j in state.results} and CALLS["https://www.glassdoor.com/job/1"] == 0
    # Its wallet reserve and daily ceiling stay what they are: a refused query is not counted and nothing more is asked.
    empty = Tiny("wallet balance is at the reserve")
    held = run(wide.search(engine_with(empty), result_count=10))
    assert len(empty.queries) == 1 and held.stats["tiny_searches"] == 0 and held.stats["tiny_skipped"] == "wallet balance is at the reserve"
    capped = Tiny()
    run(wide.search(engine_with(capped, tinyfish_search_queries_per_search=1), result_count=10))
    assert len(capped.queries) == 1


# ---------------------------------------------------------------- AWS web search
class FakeAws:
    def __init__(self, general):
        self.general, self.params = general, []

    def invoke_web_search(self, **params):
        self.params.append(params)
        include = (params.get("filters") or {}).get("domainFilter", {}).get("include")
        return {"results": [] if include else [{"title": "t", "url": u, "text": "x", **({"publishedAt": when} if when else {})}
                                                for u, when in self.general]}


def test_aws_rotation_gives_the_any_site_query_its_share():
    hosts = ["job-boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com", "apply.workable.com"]
    groups = aws_web_search.host_groups(hosts)
    assert aws_web_search.rotation(hosts, 0.0) == groups                       # no open web: one job-board host per query, as before
    half = aws_web_search.rotation(hosts, 0.5)
    assert half == [groups[0], None, groups[1], None, groups[2], None, groups[3], None]
    quarter = aws_web_search.rotation(hosts, 0.25)
    assert quarter.count(None) == 1 and len(quarter) == 5 and quarter[0] == groups[0]
    assert aws_web_search.rotation(hosts, 1.0) == [None] and aws_web_search.rotation([], 0.5) == [None] and aws_web_search.rotation([], 0.0) == []
    assert aws_web_search.rotation(hosts, 0.01).count(None) == 1               # any share above 0: at least one


def test_aws_asks_any_site_for_employer_postings_in_round_one_and_keeps_nothing():
    now = datetime.now(timezone.utc)
    newest, older = "https://careers.example.com/jobs/ai-9", "https://careers.example.com/jobs/ml-1"
    general = [(older, now - timedelta(days=40)), ("https://www.indeed.com/viewjob?jk=1", now), ("https://careers.example.com/jobs/search?q=ai", now),
               ("https://careers-acme.icims.com/jobs/1/ai/job", now), (newest, now - timedelta(days=2)), ("https://other.example.com/jobs/none-1", None)]
    fake = FakeAws(general)
    s = wide.wide_settings(searxng_url="", aws_web_search_enabled=True, searxng_page_leads_per_search=2)
    engine = wide.wide_engine(ai=wide.WideAI(available=False), settings=s, sources=("greenhouse", "lever", "ashby", "aws_web_search"))
    quota = MemoryQuota()
    engine.aws = AwsWebSearch(s, quota, client=fake)
    asked = spy_on(engine)
    state = run(wide.search(engine, result_count=50))
    st = state.stats
    # The whole paced budget of a normal search in the first round, half of it "any site" queries.
    first_round = [p for p in fake.params][:st["rounds"][0]["leads"] and 12]
    assert st["aws_budget"] == 12 and len(fake.params) >= 12 and st["aws_queries"] == len(fake.params) == run(quota.used())
    any_site = [p for p in first_round if "exclude" in p["filters"]["domainFilter"]]
    on_hosts = [p for p in first_round if "include" in p["filters"]["domainFilter"]]
    assert len(any_site) == len(on_hosts) == 6 and st["aws_open_queries"] >= 6
    for params in any_site:
        excluded = set(params["filters"]["domainFilter"]["exclude"])
        assert {"linkedin.com", "indeed.com", "icims.com", "bamboohr.com", "jobs.lever.co", "boards.greenhouse.io"} <= excluded and len(excluded) <= 100
        assert "include" not in params["filters"]["domainFilter"]
    # The newest results first, up to the page leads a search may take; job boards, search pages and unreadable hosts are
    # dropped before anything is requested.
    assert [url for url, _ in asked][:2] == [newest, older] and st["aws_open_leads"] == 2
    assert not any(host in url for url in CALLS for host in ("indeed.com", "icims.com", "other.example.com"))
    assert newest in {j.url for j in state.results}
    # Under a platform filter there is no "any site" query.
    filtered = FakeAws(general)
    held = wide.wide_engine(ai=wide.WideAI(available=False), settings=s, sources=("greenhouse", "lever", "ashby", "aws_web_search"))
    held.aws = AwsWebSearch(s, MemoryQuota(), client=filtered)
    run(wide.search(held, result_count=50, ats_platforms=["lever"]))
    assert filtered.params and all("include" in p["filters"]["domainFilter"] for p in filtered.params)
    d = Settings(_env_file=None)
    assert (d.aws_web_search_max_calls_per_search, d.aws_web_search_max_calls_per_search_max, d.aws_web_search_open_web_share) == (12, 30, 0.5)
    assert aws_web_search.paced_calls(12, 3700, 45, datetime(2026, 12, 1, tzinfo=timezone.utc)) == 4        # the month's allowance still paces it
    assert aws_web_search.paced_calls(12, 3700, 45, datetime(2026, 12, 23, tzinfo=timezone.utc)) == 12
