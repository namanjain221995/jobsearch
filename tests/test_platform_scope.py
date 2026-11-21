"""ATS platform filter, startup focus and the per-platform / per-provider statistics of the search engine.
No network: the synthetic world of tests/conftest.py and tests/test_wider_search.py."""
import asyncio
import dataclasses
import json

import pytest

from app import ats_platforms
from app.ats_platforms import Platform
from app.providers.registry import Unit
from app.providers.searxng import build_queries
from app.schemas import Candidate, Job, SearchRequest
from app.search_engine import SEARX, SearchEngine, SearchState
from app.search_plan import plan_for
from app.search_scope import Scope, coverage, host_platform, platform_of_job, platform_of_url
from tests import conftest
from tests.conftest import CALLS, JSONLD, make_request
from tests.test_wider_search import HOSTS, SEARX_ANSWERS, SEARX_LOG, SEARX_MODE, WideAI, search, wide_engine

GH_ACME = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
GH_NEWCO = "https://boards-api.greenhouse.io/v1/boards/newco/jobs"
LEVER_BETA = "https://api.lever.co/v0/postings/beta"
REMOTEOK = "https://remoteok.com/api"
WD_SEED = ("workday", "wdco|wd5|Careers")
ALL = ("greenhouse", "lever", "ashby", "workday", "remoteok", "amazon", "searxng")
SEEDS = [*conftest.DEFAULT_SEEDS, WD_SEED]
READERS = frozenset({"greenhouse", "lever", "ashby", "workday", "workable", "recruitee"})


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset():
    SEARX_ANSWERS.clear()
    SEARX_LOG.clear()
    SEARX_MODE.update(status=200, unresponsive=[])


@pytest.fixture
def extra_platforms(monkeypatch):
    """Two platforms without a board reader, added to the registry for one test: public pages only, and credentials."""
    monkeypatch.setitem(ats_platforms.PLATFORMS, "pagesats", Platform(
        "pagesats", "Pages ATS", "public_pages", ("jobs.pagesats.example",), note="Public career pages only, no feed."))
    monkeypatch.setitem(ats_platforms.PLATFORMS, "lockedats", Platform(
        "lockedats", "Locked ATS", "credentials", (), note="Its API needs the employer's credentials"))


def site_of(query: str) -> str:
    return query.split(" ", 1)[0].removeprefix("site:")


# ---------------------------------------------------------------- request
def test_request_accepts_known_platforms_and_refuses_unknown_ones():
    assert make_request().ats_platforms == [] and make_request().startup_focus is False
    assert make_request(ats_platforms=["Lever", "lever", " greenhouse "]).ats_platforms == ["lever", "greenhouse"]
    with pytest.raises(ValueError, match="Unknown ATS platform: nosuchats"):
        make_request(ats_platforms=["lever", "nosuchats"])
    assert set(SearchRequest.model_fields) >= {"ats_platforms", "startup_focus"}


def test_platform_of_a_job_and_of_an_address_come_from_the_registry():
    assert platform_of_url("https://jobs.lever.co/beta/11111111-1111-4111-8111-111111111111") == "lever"
    assert platform_of_url("https://acme.wd5.myworkdayjobs.com/Careers") == "workday" and host_platform("myworkdayjobs.com") == "workday"
    assert platform_of_url("https://careers.example.com/jobs/ml-1") == "" and platform_of_url("not a url") == ""
    assert platform_of_job(Job(title="t", company="c", url="https://x.example/1", source="Remote OK", ats="remoteok")) == ""
    assert platform_of_job(Job(title="t", company="c", url="https://x.example/1", source="Greenhouse", ats="greenhouse")) == "greenhouse"


# ---------------------------------------------------------------- platform filter
def test_filter_reads_and_queries_only_the_chosen_platform_and_drops_everything_else():
    SEARX_ANSWERS["*"] = ["https://boards.greenhouse.io/newco", "https://jobs.lever.co/beta"]
    leads = [Candidate(url="https://careers.example.com/jobs/ml-1", provider="OpenAI", cited=True),          # no ATS platform
             Candidate(url="https://boards.greenhouse.io/thirdco", provider="Claude", cited=True)]             # another platform
    state = run(search(wide_engine(WideAI(leads, available=True), sources=ALL, seeds=SEEDS), result_count=50, ats_platforms=["lever"]))
    assert state.status == "completed" and state.results and {platform_of_job(j) for j in state.results} == {"lever"}
    assert CALLS[LEVER_BETA] == 1
    assert CALLS[GH_ACME] == 0 and CALLS[GH_NEWCO] == 0 and CALLS[REMOTEOK] == 0                  # not read, not registered
    assert CALLS["https://careers.example.com/jobs/ml-1"] == 0                                    # dropped before verification
    assert not any("thirdco" in url or "myworkdayjobs" in url or "amazon.jobs" in url for url in CALLS)
    assert SEARX_LOG and {site_of(q) for q, _ in SEARX_LOG} == {"jobs.lever.co"}                  # search engines asked for that host only
    stats = state.stats
    assert stats["platform_filter"] == ["lever"] and list(stats["platforms"]) == ["lever"] and stats["filtered_out"] >= 2
    assert stats["new_boards"] == 0 and list(state.sources) == ["Lever"]
    assert stats["total_discovered"] == 2 + stats["ai_leads"] + stats["searx_leads"]              # the two Lever postings, not the whole index


def test_filter_applies_to_postings_the_index_already_holds():
    from app.database import Database
    db = Database(":memory:")
    everything = run(search(wide_engine(db=db, sources=ALL, seeds=SEEDS), result_count=50))
    assert {platform_of_job(j) for j in everything.results} >= {"greenhouse", "lever", "workday", ""}
    CALLS.clear()
    only = run(search(wide_engine(db=db, sources=ALL, seeds=SEEDS), result_count=50, ats_platforms=["workday", "lever"]))
    assert {platform_of_job(j) for j in only.results} == {"workday", "lever"} and len(only.results) == 3
    assert CALLS[GH_ACME] == 0 and set(only.stats["platforms"]) == {"workday", "lever"}
    assert {j.url for j in only.results} <= {j.url for j in everything.results}                    # the same postings, fewer of them


def test_no_filter_is_the_search_as_before():
    state = run(search(wide_engine(sources=ALL, seeds=SEEDS), result_count=50))
    assert state.stats["platform_filter"] == [] and state.stats["filtered_out"] == 0 and state.stats["coverage_warnings"] == []
    sites = {site_of(q) for q, _ in SEARX_LOG if q.startswith("site:")}
    assert len(sites) > 1 and sites <= set(HOSTS) and CALLS[GH_ACME] == 1 and CALLS[REMOTEOK] == 1


# ---------------------------------------------------------------- startup focus
def job(title, company, score, ats, source="heuristic"):
    return Job(title=title, company=company, url=f"https://x.example/{company}/{title}", source=ats, ats=ats, score=score, score_source=source)


def test_startup_focus_is_a_tie_break_and_never_outranks_a_better_score():
    jobs = [job("A", "Aaa", 80, "workday"), job("B", "Zzz", 80, "lever"), job("C", "Mmm", 90, "workday"), job("D", "Bbb", 79, "ashby"),
            job("E", "Ccc", 70, "workday", "ai"), job("F", "Ddd", 95, "greenhouse")]
    plain = sorted(jobs, key=Scope.of([], False, READERS).order_key(SearchEngine._order))
    focused = sorted(jobs, key=Scope.of([], True, READERS).order_key(SearchEngine._order))
    assert [j.title for j in plain] == ["E", "F", "C", "A", "B", "D"]             # as before: AI-judged first, then score, then employer
    assert [j.title for j in focused] == ["E", "F", "C", "B", "A", "D"]           # only the 80 / 80 tie changed
    assert Scope.of([], False, READERS).order_key(SearchEngine._order) is SearchEngine._order


def test_startup_focus_in_a_search_changes_the_order_of_ties_only(monkeypatch):
    plain = run(search(wide_engine(sources=ALL, seeds=SEEDS), result_count=50))
    first = [platform_of_job(j) for j in plain.results].index("workday")
    assert first > 0 and platform_of_job(plain.results[first - 1]) == "greenhouse" and plain.results[first].score == plain.results[0].score == 92
    same = run(search(wide_engine(sources=ALL, seeds=SEEDS), result_count=50, startup_focus=True))
    assert [j.url for j in same.results] == [j.url for j in plain.results]         # Greenhouse (startup) already led that tie
    # The registry decides what a startup platform is: with Workday marked instead of Greenhouse, the tie turns round.
    monkeypatch.setitem(ats_platforms.PLATFORMS, "workday", dataclasses.replace(ats_platforms.PLATFORMS["workday"], startup=True))
    monkeypatch.setitem(ats_platforms.PLATFORMS, "greenhouse", dataclasses.replace(ats_platforms.PLATFORMS["greenhouse"], startup=False))
    monkeypatch.setattr(ats_platforms, "STARTUP_PLATFORMS", tuple(p.id for p in ats_platforms.PLATFORMS.values() if p.startup))
    focused = run(search(wide_engine(sources=ALL, seeds=SEEDS), result_count=50, startup_focus=True))
    assert focused.stats["startup_focus"] is True and platform_of_job(focused.results[0]) == "workday" and focused.results[0].score == 92
    assert {j.url: j.score for j in focused.results} == {j.url: j.score for j in plain.results}     # no score changed, nothing added or lost
    scores = [j.score for j in focused.results]
    assert scores == sorted(scores, reverse=True)                                  # a weaker match never moved above a stronger one


def test_startup_focus_puts_startup_platforms_first_in_units_and_queries():
    focus = Scope.of([], True, READERS)
    units = [Unit("workday", "w", "q"), Unit("greenhouse", "g"), Unit("remoteok", "*"), Unit("lever", "l")]
    assert [u.src for u in focus.order_units(units)] == ["greenhouse", "lever", "workday", "remoteok"]
    assert Scope.of([], False, READERS).order_units(units) == units
    hosts = focus.hosts(HOSTS)
    flags = [host_platform(h) in ats_platforms.STARTUP_PLATFORMS for h in hosts]
    assert flags == sorted(flags, reverse=True) and True in flags and False in flags and sorted(hosts) == sorted(HOSTS)
    queries = focus.order_queries(build_queries(["AI Engineer", "ML Engineer"], hosts, ["Texas"]))
    startup = [host_platform(site_of(q)) in ats_platforms.STARTUP_PLATFORMS for q in queries]
    assert startup[:8] == [True, True, True, False, True, True, True, False]       # three startup queries, then one other
    assert sum(startup[:12]) == 9 and sorted(queries) == sorted(build_queries(["AI Engineer", "ML Engineer"], HOSTS, ["Texas"]))


def test_startup_focus_gives_other_platforms_the_smaller_share_of_new_boards():
    engine = wide_engine(sources=ALL, seeds=[])
    urls = [f"https://co{i}.wd5.myworkdayjobs.com/Careers" for i in range(5)] + [f"https://boards.greenhouse.io/startup{i}" for i in range(5)]

    def leads(focus: bool) -> list[str]:
        state = SearchState("s", 50)
        state.plan = dataclasses.replace(plan_for(engine.settings, 50), new_boards=8)
        state.scope = Scope.of([], focus, engine.specs)
        out: dict = {}
        engine._hits_to_leads(state, urls, HOSTS, SEARX, (set(), set(), set()), out)
        return [platform_of_url(u) for u in out]

    assert leads(False) == ["workday"] * 5 + ["greenhouse"] * 3                    # first come, first served
    assert leads(True) == ["workday"] * 2 + ["greenhouse"] * 5                     # a quarter of the budget for other platforms
    run(engine.aclose())


def test_filter_wins_and_the_focus_orders_within_it():
    both = Scope.of(["workday", "lever"], True, READERS)
    hosts = both.hosts(HOSTS)
    assert both.preferred == {"lever"} and host_platform(hosts[0]) == "lever" and {host_platform(h) for h in hosts} == {"lever", "workday"}
    assert Scope.of(["lever", "ashby"], True, READERS).preferred == frozenset()     # everything chosen is a startup platform
    assert Scope.of(["workday"], True, READERS).preferred == frozenset() and Scope.of(["workday"], True, READERS).other_boards(40) is None
    state = run(search(wide_engine(sources=ALL, seeds=SEEDS), result_count=50, ats_platforms=["workday", "lever"], startup_focus=True))
    assert {platform_of_job(j) for j in state.results} == {"workday", "lever"} and CALLS[GH_ACME] == 0
    assert [site_of(q) for q, _ in SEARX_LOG][:4] == ["jobs.lever.co"] * 3 + ["myworkdayjobs.com"]
    assert [j.score for j in state.results] == sorted((j.score for j in state.results), reverse=True)


def test_hosts_nobody_can_use_are_never_asked():
    # A platform whose board reader is switched off gets no `site:` query (and no AI web search), with or without a
    # filter; a platform whose posting pages are verified one by one keeps its host.
    few = Scope.of([], False, {"greenhouse", "lever", "searxng"})
    assert few.hosts(HOSTS) == ["boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co", "jobs.smartrecruiters.com", "applytojob.com"]
    assert not few.readable("jobs.ashbyhq.com") and not few.readable("myworkdayjobs.com") and not few.readable("acme.recruitee.com")
    assert few.readable("jobs.smartrecruiters.com") and few.readable("acme.applytojob.com") and few.readable("careers.example.com")
    assert Scope().hosts(HOSTS) == HOSTS and Scope().readable("jobs.ashbyhq.com")           # no engine behind the scope: nothing is known
    assert Scope.of(["lever"], False, {"greenhouse"}).hosts(HOSTS) == []                     # chosen, but its reader is off: not asked at all
    # A chosen platform is searched even when SEARXNG_SITES does not name its host: the registry does.
    configured = [h for h in HOSTS if h != "recruitee.com"]
    assert Scope.of(["rippling"], False, READERS | {"rippling"}).hosts(configured) == ["ats.rippling.com"]
    assert Scope.of(["rippling"], False, READERS).hosts(configured) == []                    # ... unless its reader is off
    assert Scope.of(["recruitee", "lever"], False, READERS).hosts(configured) == ["jobs.lever.co", "recruitee.com"]
    assert Scope.of(["greenhouse"], False, READERS).hosts(HOSTS) == ["boards.greenhouse.io", "job-boards.greenhouse.io"]      # configured hosts are kept as they are
    assert Scope.of(["smartrecruiters", "jobvite"], False, READERS).hosts(HOSTS) == ["jobs.smartrecruiters.com", "jobs.jobvite.com"]
    assert Scope.of(["icims", "bullhorn"], False, READERS).hosts(HOSTS) == []                # restricted / credentials only: nothing to ask


def test_a_reader_that_is_off_is_not_queried_by_any_search_engine():
    SEARX_ANSWERS["*"] = ["https://jobs.ashbyhq.com/someco"]
    state = run(search(wide_engine(sources=("greenhouse", "lever", "searxng"), seeds=conftest.DEFAULT_SEEDS), result_count=50))
    sites = {site_of(q) for q, _ in SEARX_LOG if q.startswith("site:")}
    assert sites and "jobs.ashbyhq.com" not in sites and "myworkdayjobs.com" not in sites and "recruitee.com" not in sites
    assert sites <= {"boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co", "jobs.smartrecruiters.com", "applytojob.com"}
    assert state.status == "completed" and not any("someco" in url for url in CALLS)


# ---------------------------------------------------------------- statistics
def test_statistics_per_platform_and_per_provider_are_counts_of_this_search():
    SEARX_ANSWERS["*"] = ["https://boards.greenhouse.io/newco"]
    lead = Candidate(url="https://job-boards.greenhouse.io/acme/jobs/1001", provider="Claude", cited=True)
    state = run(search(wide_engine(WideAI([lead], available=True), sources=ALL, seeds=SEEDS), result_count=50))
    stats, rows = state.stats, state.stats["platforms"]
    assert set(rows) == {"greenhouse", "lever", "workday", "other"}
    gh = rows["greenhouse"]
    assert gh["label"] == "Greenhouse" and gh["startup"] is True and gh["coverage"] == "full"
    assert (gh["considered"], gh["verified"], gh["unique"], gh["rejected"], gh["duplicates"]) == (27, 23, 23, 4, 0) and gh["reasons"] == {"non_us": 3, "eligibility": 1}
    assert rows["other"]["label"] == "Other sources" and rows["other"]["duplicates"] == 1 and rows["other"]["coverage"] == ""
    for row in rows.values():
        assert row["rejected"] == sum(row["reasons"].values()) and row["verified"] == row["unique"] + row["duplicates"]
        assert row["verified"] + row["rejected"] <= row["considered"]
    assert sum(r["unique"] for r in rows.values()) == stats["verified_active"] == stats["unique_candidates"]
    assert sum(r["matched"] for r in rows.values()) == len(state.results) == stats["final_matches"]
    assert {p: r["matched"] for p, r in rows.items()} == {p: sum(1 for j in state.results if (platform_of_job(j) or "other") == p) for p in rows}
    assert sum(r["duplicates"] for r in rows.values()) == stats["rejected_duplicates"]
    providers = stats["providers"]
    assert providers["SearXNG"] == {"kind": "search_engine", "queries": stats["searx_queries"] + stats["searx_cached"], "results": stats["searx_results"],
                                    "leads": 1, "new_boards": 1, "new_board_postings": 6, "verified": 0, "matched": 0}
    assert providers["Claude"]["kind"] == "ai" and providers["Claude"]["leads"] == 1 and providers["Claude"]["verified"] == 1
    assert providers["Claude"]["queries"] is None and "OpenAI" not in providers                    # nothing is reported for a provider that gave nothing
    assert isinstance(stats["seconds_to_first_verified"], float) and 0 <= stats["seconds_to_first_verified"] <= stats["total_seconds"]
    assert stats["total_seconds"] == state.timings["total"]
    kept = json.dumps({"stats": stats, "events": state.events[-1]})
    assert "ignored title" not in kept and "ignored snippet" not in kept                           # no text of a search result anywhere
    live = [e["stats"]["platforms"] for e in state.events if e["stats"]["platforms"]]
    assert live and live[0]["greenhouse"]["matched"] == 0 and state.events[-1]["stats"]["platforms"]["greenhouse"]["matched"] == 23


def test_nothing_verified_leaves_the_time_to_first_verified_results_empty():
    state = run(search(wide_engine(sources=("greenhouse", "lever"), seeds=conftest.DEFAULT_SEEDS), job_titles="Underwater Basket Weaver"))
    assert state.results == [] and state.stats["seconds_to_first_verified"] is None and state.stats["total_seconds"] is not None


# ---------------------------------------------------------------- limited coverage
def test_a_platform_without_a_reader_is_searched_through_page_leads_and_says_so(extra_platforms, monkeypatch):
    page = "https://jobs.pagesats.example/acme/ai-engineer-77"
    monkeypatch.setitem(conftest.PAGES, page, (200, "<html><head><title>AI Engineer</title>" + JSONLD % ("AI Engineer", "Pages Employer Inc")
                                               + "</head><body>Apply now</body></html>"))
    SEARX_ANSWERS["site:jobs.pagesats.example"] = [page, "https://jobs.pagesats.example/", "http://jobs.pagesats.example/acme/insecure",
                                                   "https://boards.greenhouse.io/newco"]
    state = run(search(wide_engine(sources=ALL, seeds=SEEDS), result_count=50, ats_platforms=["pagesats", "lockedats", "lever"]))
    assert {site_of(q) for q, _ in SEARX_LOG} == {"jobs.lever.co", "jobs.pagesats.example"}
    found = [j for j in state.results if platform_of_job(j) == "pagesats"]
    assert len(found) == 1 and found[0].company == "Pages Employer Inc" and found[0].url == page and "JobPosting" in found[0].verification
    assert CALLS[page] == 1 and CALLS[GH_NEWCO] == 0 and CALLS["https://jobs.pagesats.example/acme/insecure"] == 0
    rows = state.stats["platforms"]
    assert list(rows) == ["lever", "pagesats", "lockedats"]
    assert rows["pagesats"]["coverage"] == "limited" and rows["pagesats"]["unique"] == 1 and rows["pagesats"]["leads"] == 1
    assert rows["lockedats"]["coverage"] == "none" and rows["lockedats"]["considered"] == 0 and rows["lever"]["coverage"] == "full"
    warnings = state.stats["coverage_warnings"]
    assert warnings == ["Locked ATS could not be searched: Its API needs the employer's credentials.",
                        "Pages ATS has limited coverage: Public career pages only, no feed. They are found only through web search results."]
    assert all(w in state.warnings for w in warnings) and not any("Lever" in w for w in warnings)


def test_a_chosen_platform_that_yields_nothing_is_named_with_the_reason(extra_platforms):
    state = run(search(wide_engine(sources=ALL, seeds=SEEDS), result_count=50, ats_platforms=["pagesats"]))
    assert state.status == "completed" and state.results == [] and CALLS[GH_ACME] == 0 and CALLS[LEVER_BETA] == 0
    assert state.stats["coverage_warnings"] == ["Pages ATS has limited coverage: Public career pages only, no feed. They are found only "
                                                "through web search results. Nothing was found for it in this search."]
    assert state.stats["coverage_warnings"][0] in state.warnings
    live = next(e["stats"]["coverage_warnings"] for e in state.events if e["stage"] == "analyze")
    assert live == ["Pages ATS has limited coverage: Public career pages only, no feed. They are found only through web search results."]


def test_a_reader_that_is_switched_off_is_not_replaced_by_page_reading():
    state = run(search(wide_engine(sources=("greenhouse", "searxng"), seeds=conftest.DEFAULT_SEEDS), result_count=50, ats_platforms=["lever"]))
    assert state.results == [] and CALLS[LEVER_BETA] == 0 and CALLS[GH_ACME] == 0
    assert state.stats["coverage_warnings"] == ["Lever could not be searched: Its board reader is switched off or not installed on this server."]
    assert state.stats["platforms"]["lever"]["coverage"] == "none"


def test_coverage_levels_follow_access_and_reader():
    pages = Platform("p", "P", "public_pages", ("jobs.p.example",))
    assert coverage(pages, False)[0] == "limited" and coverage(pages, True)[0] == "full"
    assert coverage(Platform("c", "C", "credentials"), False) == ("none", ats_platforms.ACCESS["credentials"] + ".")
    assert coverage(Platform("r", "R", "restricted", (".r.example",), "r"), False)[0] == "none"
    assert Scope.of(["nosuch", "lever"], False, READERS).chosen == {"lever"}
