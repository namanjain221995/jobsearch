"""Postings verified on employers' own career sites stay in the index (source "career_page"): later searches find
them without a web search, each is checked again on its own page, and a posting that is gone is forgotten.
No network: the pages are the synthetic ones of tests/conftest.py."""
import asyncio

import pytest

from app import board_catalog
from app.database import Database
from app.providers.base import SourceError
from app.providers.registry import CAREER_PAGE, SPECS, Unit
from app.schemas import Candidate, Job
from app.search_engine import CAREER_PAGE_SOURCE, CAREER_PAGES_PER_SEARCH
from app.search_scope import NO_SCOPE
from tests import conftest
from tests.conftest import CALLS, make_request, run_search
from tests.test_reading_plan import PlanIndex

LIVE = "https://careers.example.com/jobs/ai-9"            # JobPosting "AI Engineer - Platform" at Example Careers Inc, Denver
GONE = "https://careers.example.com/jobs/gone"            # answers 404
SOURCES = ("greenhouse", "lever", "ashby", CAREER_PAGE)


def run(coro):
    return asyncio.run(coro)


def page_job(url: str, **over) -> Job:
    base = dict(id="x", title="AI Engineer - Platform", company="Example Careers Inc", url=url, source="OpenAI web search",
                location="Denver, CO", verification="Posting page fetched; schema.org JobPosting JSON-LD present and not expired")
    base.update(over)
    return Job(**base)


def test_a_posting_verified_on_a_company_site_is_found_again_without_a_web_search(make_engine):
    db = Database(":memory:")
    lead = Candidate(url=LIVE, title="AI Engineer - Platform", company="Example Careers Inc", provider="OpenAI")
    first = run(run_search(make_engine(leads=[lead], ai_available=True, sources=SOURCES, db=db), make_request()))
    assert LIVE in {j.url for j in first.results} and first.stats["pages_remembered"] == 1
    fetched = CALLS[LIVE]
    # The next search has no AI and no leads at all: the posting comes from the index, still fresh, so its page is not asked.
    second = run(run_search(make_engine(ai_available=False, sources=SOURCES, db=db), make_request()))
    again = [j for j in second.results if j.url == LIVE]
    assert len(again) == 1 and again[0].company == "Example Careers Inc" and CALLS[LIVE] == fetched
    assert second.stats["pages_remembered"] == 0
    # Without the memory (CAREER_PAGE_MEMORY=false, here: the source is not among the engine's) nothing is kept.
    plain_db = Database(":memory:")
    kept = run(run_search(make_engine(leads=[lead], ai_available=True, db=plain_db), make_request()))
    assert LIVE in {j.url for j in kept.results} and kept.stats["pages_remembered"] == 0
    assert LIVE not in {j.url for j in run(run_search(make_engine(ai_available=False, db=plain_db), make_request())).results}


def test_a_remembered_page_is_checked_on_its_own_page_and_forgotten_when_it_no_longer_proves_a_posting(make_engine):
    engine = make_engine(sources=SOURCES)

    async def go():
        assert await engine._remember_pages([page_job(LIVE), page_job(GONE)]) == 2
        assert {(CAREER_PAGE, LIVE), (CAREER_PAGE, GONE)} <= set(await engine.index.known_boards())
        await engine._keys()
        assert sorted(engine._pages) == sorted([LIVE, GONE]) and CAREER_PAGE not in (await engine._keys())     # addresses, not employer names
        jobs = await engine._read_career_page(LIVE)
        assert len(jobs) == 1 and jobs[0].title == "AI Engineer - Platform" and jobs[0].company == "Example Careers Inc"
        assert jobs[0].source == CAREER_PAGE_SOURCE and jobs[0].verification.startswith("Posting page fetched")
        assert await engine._read_career_page(GONE) == []                      # closed or removed: an answer, and an empty one
        # Through the normal read path: the gone posting leaves the index, the live one stays.
        for url in (LIVE, GONE):
            await engine._refresh_unit(Unit(CAREER_PAGE, url, ""), "catalog")
        assert len(await engine.index.unit_rows(CAREER_PAGE, LIVE, "")) == 1 and await engine.index.unit_rows(CAREER_PAGE, GONE, "") == {}
        # A page that cannot be reached is a failed read (tried again later), not "the posting is gone".
        conftest.PAGES["https://careers.example.com/jobs/down"] = (503, "maintenance")
        try:
            with pytest.raises(SourceError):
                await engine._read_career_page("https://careers.example.com/jobs/down")
        finally:
            del conftest.PAGES["https://careers.example.com/jobs/down"]
        await engine.aclose()

    run(go())


def test_only_plain_page_checks_of_postings_without_a_board_reader_are_remembered(make_engine):
    engine = make_engine(sources=SOURCES)

    async def go():
        skipped = [
            page_job("https://careers.example.com/jobs/r", verification="Posting page rendered in a headless browser; visible text matches"),
            page_job("https://boards.greenhouse.io/acme/jobs/1", ats="greenhouse"),                 # the board reader covers it
            page_job("https://careers-acme.icims.com/jobs/1/job"),                                   # a platform that is never fetched
            page_job("http://careers.example.com/jobs/plain"),                                       # not https
            page_job("https://careers.example.com/jobs/nameless", company=""),
            page_job("https://careers.example.com/jobs/" + "x" * 600),
        ]
        assert await engine._remember_pages(skipped) == 0 and not [b for b in await engine.index.known_boards() if b[0] == CAREER_PAGE]
        await engine.aclose()

    run(go())
    assert SPECS[CAREER_PAGE].keyed and board_catalog.DEFAULT_PER_MINUTE[CAREER_PAGE] > 0      # the background reader keeps them current


def test_a_search_checks_only_the_remembered_pages_whose_title_fits(make_engine):
    engine = make_engine(sources=SOURCES)
    pages = [Unit(CAREER_PAGE, f"https://careers.example.com/jobs/p{i}", "") for i in range(CAREER_PAGES_PER_SEARCH + 30)]
    boards = [Unit("greenhouse", "acme", ""), Unit("lever", "beta", "")]
    fitting = [(CAREER_PAGE, u.key, "AI Engineer") for u in pages[10:]]                              # the first ten list another title
    fresh = pages[-5:]
    engine.index = PlanIndex(rows=[*fitting, ("greenhouse", "acme", "AI Engineer")], fresh=fresh)

    async def go():
        try:
            return await engine._reading_plan([*boards, *pages], ["AI Engineer"], [], 1500, NO_SCOPE)
        finally:
            await engine.client.aclose()

    plan = run(go())
    planned_pages = [u for u in plan if u.src == CAREER_PAGE]
    assert len(planned_pages) == CAREER_PAGES_PER_SEARCH and not set(planned_pages) & set(pages[:10]) and not set(planned_pages) & set(fresh)
    assert plan[:2] == boards                                                                        # pages do not use up the budget of boards
    budget_of_one = run_plan_with_limit(make_engine(sources=SOURCES), boards, pages, fitting)
    assert [u for u in budget_of_one if u.src != CAREER_PAGE] == boards[:1] and len([u for u in budget_of_one if u.src == CAREER_PAGE]) == CAREER_PAGES_PER_SEARCH


def run_plan_with_limit(engine, boards, pages, rows):
    engine.index = PlanIndex(rows=[*rows, ("greenhouse", "acme", "AI Engineer")])

    async def go():
        try:
            return await engine._reading_plan([*boards, *pages], ["AI Engineer"], [], 1, NO_SCOPE)
        finally:
            await engine.client.aclose()

    return run(go())
