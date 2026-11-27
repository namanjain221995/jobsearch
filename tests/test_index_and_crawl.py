"""Local index (speed, freshness), lazy details, cross-source duplicates, browser crawler and AI extraction."""
import asyncio
import time

from app.database import Database
from app.schemas import Candidate
from tests.conftest import CALLS, POSTED_ISO, FakeBrowser, make_request, run_search

GH = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
ALL = ("greenhouse", "lever", "ashby", "workday", "workable", "amazon", "remoteok", "jobicy", "himalayas")
SEEDS = [("greenhouse", "acme"), ("lever", "beta"), ("workday", "wdco|wd5|Careers")]


def run(coro):
    return asyncio.run(coro)


def lead(url, title="AI Engineer", company="Example", provider="Claude"):
    return Candidate(url=url, title=title, company=company, provider=provider, cited=True)


def test_second_search_is_served_from_the_index_without_refetching(make_engine):
    db = Database(":memory:")
    first = run(run_search(make_engine(db=db), make_request()))
    assert CALLS[GH] == 1 and "first_results" in first.timings
    second = run(run_search(make_engine(db=db), make_request(result_count=10)))
    assert CALLS[GH] == 1                                     # board is still fresh: no network
    assert len(second.results) == 10 and second.stats["boards_failed"] == 1      # the failed board is remembered, not hammered
    assert CALLS["https://api.ashbyhq.com/posting-api/job-board/gamma"] == 3      # one attempt with two retries, first search only
    partial_events = [e for e in second.events if e["partial"]]
    assert partial_events and partial_events[0]["stage"] == "titles"              # preliminary matches before any source is read


def test_stale_index_is_refreshed_and_removed_postings_disappear(make_engine, settings):
    db = Database(":memory:")
    run(run_search(make_engine(db=db), make_request()))
    with db._lock:                                            # age every unit beyond its TTL
        db._db.execute("UPDATE units SET expires_at = ?", (time.time() - 1,))
        db._db.commit()
    state = run(run_search(make_engine(db=db), make_request()))
    assert CALLS[GH] == 2 and state.results
    assert db.index_stats()["stored_postings"] == 24          # replaced, not duplicated (22 Greenhouse + 2 Lever)


def test_all_sources_together_with_cross_source_duplicate(make_engine):
    state = run(run_search(make_engine(seeds=SEEDS, sources=ALL), make_request(result_count=0)))
    by_company = {}
    for job in state.results:
        by_company.setdefault(job.company, []).append(job)
    acme_ai = [j for j in by_company["Acme AI"] if j.title == "AI Engineer"]
    assert len(acme_ai) == 1 and acme_ai[0].source == "Greenhouse"             # employer's own record wins
    assert set(acme_ai[0].sources) == {"Greenhouse", "Remote OK"} and acme_ai[0].apply_url.startswith("https://job-boards.greenhouse.io/")
    assert {"Gizmo Labs", "Widget Works", "Amazon", "Hooli", "Pied Piper", "Initech", "Wdco"} <= set(by_company)
    assert "Euro Co" not in by_company and "Globex" not in by_company and "Old Co" not in by_company   # France, worldwide remote, expired
    assert {j.source for j in state.results} >= {"Greenhouse", "Lever", "Workday", "Workable", "Amazon Jobs", "Remote OK", "Jobicy", "Himalayas"}
    assert set(state.sources) >= {"Greenhouse", "Workday", "Workable", "Amazon Jobs", "Remote OK", "Jobicy", "Himalayas"}
    ids = [j.id for j in state.results]
    assert len(ids) == len(set(ids)) and state.stats["rejected_duplicates"] >= 1


def test_workday_details_are_fetched_lazily_and_filters_apply(make_engine):
    engine = make_engine(seeds=[("workday", "wdco|wd5|Careers")], sources=("workday",))
    state = run(run_search(engine, make_request(job_titles="AI Engineer, Machine Learning Engineer"), close=False))
    titles = sorted((j.title, j.location) for j in state.results)
    assert titles == [("AI Engineer", "US, TX, Austin"), ("Machine Learning Engineer", "US, WA, Seattle; US, NY, New York")]
    assert state.stats["rejected_eligibility"] == 1          # the TS/SCI role is excluded for a non-citizen with the filter on
    assert state.stats["detail_fetches"] == 3
    assert all(j.verification.startswith("Listed in the employer's live Workday") and j.posted_at == POSTED_ISO for j in state.results)
    assert engine.db.live_akeys(["workday:wdco:R102"]) == set()                 # closed posting removed from the index
    detail_calls = sum(n for url, n in CALLS.items() if "/Careers/job/" in url)
    run(run_search(engine, make_request(job_titles="AI Engineer, Machine Learning Engineer")))
    assert sum(n for url, n in CALLS.items() if "/Careers/job/" in url) == detail_calls      # descriptions are cached in the index


def test_browser_renders_javascript_pages_and_blocked_sites_are_not_bypassed(make_engine):
    browser = FakeBrowser()
    leads = [lead("https://careers.example.com/jobs/js-1"), lead("https://careers.example.com/jobs/blocked-1"),
             lead("https://forbidden.example.com/jobs/1"), lead("https://careers.example.com/jobs/ml-1", "Machine Learning Engineer")]
    state = run(run_search(make_engine(leads, ai_available=True, browser=browser), make_request()))
    rendered = [j for j in state.results if j.company == "JS Example Inc"]
    assert len(rendered) == 1 and "headless browser" in rendered[0].verification
    assert browser.rendered == ["https://careers.example.com/jobs/js-1", "https://careers.example.com/jobs/blocked-1"]   # static hit needs no browser
    assert not any("forbidden" in j.url or "blocked-1" in j.url for j in state.results)
    assert CALLS["https://forbidden.example.com/jobs/1"] == 0                   # robots.txt disallows: never fetched
    assert state.stats["browser_pages"] == 2 and state.stats["rejected_unverified"] == 2


def test_listing_page_links_are_crawled_and_each_one_verified(make_engine):
    state = run(run_search(make_engine([lead("https://careers.example.com/openings", "Careers", "")], ai_available=True, browser=FakeBrowser()),
                           make_request()))
    crawled = sorted(j.url for j in state.results if j.company == "Example Careers Inc")
    assert crawled == ["https://careers.example.com/jobs/ai-9", "https://careers.example.com/jobs/ml-1"]
    assert state.stats["crawled_links"] == 2                  # "About us" and "Account Executive" are not followed


def test_ai_extraction_is_accepted_only_when_grounded_in_page_text(make_engine):
    url = "https://careers.example.com/jobs/plain-1"
    good = {url: {"is_open_job_posting": True, "title": "AI Engineer", "company": "Plain Example Co", "location": "Boston, MA"}}
    state = run(run_search(make_engine([lead(url, "AI Engineer", "Plain Example Co")], ai_available=True, browser=FakeBrowser(), extractions=good),
                           make_request()))
    job = [j for j in state.results if j.url == url]
    assert len(job) == 1 and job[0].location == "Boston, MA" and "extracted by AI" in job[0].verification
    assert state.stats["ai_extracted"] == 1
    for bad in ({url: {"is_open_job_posting": True, "title": "Chief AI Officer", "company": "Plain Example Co", "location": ""}},
                {url: {"is_open_job_posting": True, "title": "AI Engineer", "company": "Invented Corp", "location": ""}},
                {url: {"is_open_job_posting": False, "title": "AI Engineer", "company": "Plain Example Co", "location": ""}}, {}):
        state = run(run_search(make_engine([lead(url, "AI Engineer", "Plain Example Co")], ai_available=True, browser=FakeBrowser(), extractions=bad),
                               make_request()))
        assert not any(j.url == url for j in state.results) and state.stats["rejected_unverified"] == 1


def test_fast_mode_skips_ai_web_search(make_engine):
    engine = make_engine([lead("https://careers.example.com/jobs/ml-1")], ai_available=True)
    state = run(run_search(engine, make_request(deep_search=False)))
    assert engine.fake_ai.discover_calls == 0 and state.stats["ai_leads"] == 0
    assert any("switched off" in w for w in state.warnings) and state.results


def test_slow_source_does_not_block_the_search(make_engine, settings, monkeypatch):
    from app.providers import registry
    spec = registry.SPECS["lever"]

    async def slow(ctx, key, scope):
        await asyncio.sleep(30)
        return []
    monkeypatch.setitem(registry.SPECS, "lever", spec.__class__(**{**spec.__dict__, "fetch": slow}))
    settings.fetch_deadline_seconds = 0.5
    started = time.time()
    state = run(run_search(make_engine(), make_request()))
    assert time.time() - started < 10 and state.results
    assert state.sources["Lever"]["pending"] == 1 and any("still loading" in w for w in state.warnings)
    assert all(j.source == "Greenhouse" for j in state.results)                 # partial coverage is reported, not hidden
