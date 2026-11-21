"""Workable employer accounts (the documented public jobs endpoint), the switch of the cross-company search, the cut of
a search's unit budget, and the catalogue hooks that read accounts and note how a board read ended. No network.

tests/fixtures/boards/workable_account.json is a real answer of 2026-11-21 (Hugging Face's account, the address the
documented endpoint redirects to), reduced to four jobs with the long description HTML cut."""
import asyncio
import json
from collections import Counter
from pathlib import Path

import httpx
import pytest

from app import board_catalog
from app.config import Settings
from app.database import Database
from app.job_normalizer import ats_key, parse_ats, us_location_status
from app.portal.index_pg import DEAD, LIVE, NEW, RETRY, allot_due
from app.providers import registry, workable
from app.providers.base import BoardNotFound, Ctx, HostLimiter, SourceError
from app.providers.registry import SPECS, Unit
from app.search_plan import plan_for

FIX = Path(__file__).parent / "fixtures" / "boards"
API = "https://apply.workable.com/api/v1/widget/accounts/huggingface?details=true"


def load(name="workable_account.json"):
    return json.loads((FIX / name).read_text())


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    async def ok(url):
        return None
    monkeypatch.setattr(workable, "_guard", ok)


def make_ctx(routes, calls=None, limiter=None):
    def handler(request: httpx.Request):
        if calls is not None:
            calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=routes.get("robots", "User-agent: *\nContent-Signal: search=yes, ai-input=yes, ai-train=no\nDisallow:\n"))
        for match, resp in routes.items():
            if match != "robots" and match in str(request.url):
                r = resp(request) if callable(resp) else resp
                return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)
        return httpx.Response(404, json={})
    return Ctx(httpx.AsyncClient(transport=httpx.MockTransport(handler)), limiter or HostLimiter(), options={})


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- the account reader
def test_account_is_read_in_one_request_and_normalized():
    calls = []
    jobs = run(workable.fetch_account(make_ctx({"/widget/accounts/huggingface": load()}, calls), "huggingface"))
    assert calls == ["https://apply.workable.com/robots.txt", API]                 # robots.txt first, then the one request
    assert [j.ats_id for j in jobs] == ["81B46579FE", "19A136F8E2", "F88446C814", "0BD8C06DB3"]
    paris, new_york, hidden, wild = jobs
    assert paris.title == "Open-Source Machine Learning Engineer - EMEA Remote" and paris.company == "Hugging Face"
    assert paris.location == "Paris, Île-de-France, France" and paris.country_hint == "France" and paris.work_mode == "Remote"
    assert paris.url == "https://apply.workable.com/j/81B46579FE" and paris.apply_url == "https://apply.workable.com/j/81B46579FE/apply"
    assert paris.posted_at == "2026-05-29" and paris.raw_html.startswith("<p>") and "Hugging Face" in paris.raw_html
    assert (paris.source, paris.sources, paris.ats, paris.board, paris.ats_id) == ("Workable", ["Workable"], "workable", "huggingface", "81B46579FE")
    assert paris.verification == "Listed in the employer's live Workable careers API" and not paris.needs_detail and paris.verified_at
    assert not us_location_status(paris.location, paris.country_hint)[0]
    assert new_york.location == "New York, New York, United States" and new_york.country_hint == "United States"
    assert us_location_status(new_york.location, new_york.country_hint)[0]
    assert hidden.location == "United States" and hidden.country_hint == "United States"                     # a hidden location shows the country only
    assert wild.title == "Wild Card" and wild.posted_at == "2024-10-17"            # the publishing date, not created_at (2022-10-19)
    assert board_catalog.us_postings(jobs) == 3
    # The posting address carries the same id a search-engine lead on the account's own address names.
    assert parse_ats(paris.url) == ("workable", "", "81B46579FE")
    lead = parse_ats("https://apply.workable.com/huggingface/j/81B46579FE/apply")
    assert lead == ("workable", "huggingface", "81B46579FE") and ats_key(lead[0], lead[2]) == ats_key("workable", paris.ats_id)


def test_account_rows_without_a_date_place_or_code():
    base = load()["jobs"][1]
    job = workable.account_job("acme", "", {**base, "published_on": "", "telecommuting": False, "application_url": "http://x.example/apply"}, "T")
    assert job.posted_at == "" and job.work_mode == "Not listed" and job.apply_url == ""       # nothing is invented
    assert job.company == "Acme"                                                                # no account name in the answer
    assert workable.account_job("acme", "Acme Inc", {**base, "workplace_type": "hybrid"}, "T").work_mode == "Hybrid"
    assert workable.account_job("acme", "Acme Inc", {**base, "workplace_type": "on_site", "telecommuting": True}, "T").work_mode == "On-site"
    two = workable.account_job("acme", "Acme Inc", {**base, "locations": [
        {"country": "Germany", "countryCode": "DE", "city": "Berlin", "region": "Berlin", "hidden": False},
        {"country": "United States", "countryCode": "US", "city": "Austin", "region": "Texas", "hidden": False}]}, "T")
    assert two.location == "Berlin, Berlin, Germany; Austin, Texas, United States" and two.country_hint == "United States"
    old_shape = workable.account_job("acme", "Acme Inc", {k: v for k, v in base.items() if k != "locations"}, "T")
    assert old_shape.location == "New York, New York, United States" and old_shape.country_hint == "United States"
    nowhere = workable.account_job("acme", "Acme Inc", {**base, "locations": [], "city": "", "state": "", "country": ""}, "T")
    assert nowhere.location == "Not listed" and nowhere.country_hint == ""
    only_short = workable.account_job("acme", "Acme Inc", {**base, "url": "/j/19A136F8E2"}, "T")
    assert only_short.url == "https://apply.workable.com/j/19A136F8E2"                          # the shortlink is an https address
    for broken in ({**base, "title": " "}, {**base, "shortcode": ""}, {**base, "shortcode": "../x"}, {**base, "url": "http://a/b", "shortlink": ""},
                   {**base, "url": "javascript:alert(1)", "shortlink": None}, "junk", None):
        assert workable.account_job("acme", "Acme Inc", broken, "T") is None
    twice = {"name": "Acme Inc", "jobs": [base, base, "junk", {**base, "shortcode": "AAAA1111", "title": "Second"}]}
    assert [j.ats_id for j in run(workable.fetch_account(make_ctx({"/widget/accounts/acme": twice}), "acme"))] == ["19A136F8E2", "AAAA1111"]
    assert run(workable.fetch_account(make_ctx({"/widget/accounts/quiet": {"name": "Quiet", "jobs": []}}), "quiet")) == []


@pytest.mark.parametrize("key", ["", ".", "..", "a/b", "a/../b", "a%2fb", "a b", "A", "-a", "a-", "api", "j", "é", "a" * 101, "x?y", "a..b", None, 7])
def test_hostile_account_ids_are_refused_before_any_request(key):
    calls = []
    with pytest.raises(BoardNotFound, match="Malformed Workable account id"):
        run(workable.fetch_account(make_ctx({"/widget/accounts/": load()}, calls), key))
    assert calls == []


def test_unknown_account_and_malformed_answers():
    with pytest.raises(BoardNotFound, match="HTTP 404"):
        run(workable.fetch_account(make_ctx({}), "gone-co"))                       # an account that does not exist
    for bad in ([], {"name": "x"}, {"jobs": "many"}, {"jobs": None}):
        with pytest.raises(SourceError, match="Unexpected Workable payload"):
            run(workable.fetch_account(make_ctx({"/widget/accounts/acme": httpx.Response(200, json=bad)}), "acme"))
    with pytest.raises(SourceError, match="Invalid JSON"):
        run(workable.fetch_account(make_ctx({"/widget/accounts/acme": httpx.Response(200, text="<html>")}), "acme"))
    with pytest.raises(SourceError, match="Invalid JSON"):                         # a redirect is never followed
        run(workable.fetch_account(make_ctx({"/widget/accounts/acme": httpx.Response(302, headers={"location": "https://evil.example/"}, text="<html>")}), "acme"))


def test_robots_txt_is_respected_by_both_readers():
    calls = []
    ctx = make_ctx({"robots": "User-agent: *\nDisallow: /api/\n", "/widget/accounts/": load(), "/api/v1/jobs": {"jobs": []}}, calls)
    with pytest.raises(SourceError, match="robots.txt disallows apply.workable.com"):
        run(workable.fetch_account(ctx, "huggingface"))
    with pytest.raises(SourceError, match="robots.txt disallows jobs.workable.com"):
        run(workable.fetch(ctx, "*", "ai engineer"))
    assert calls == ["https://apply.workable.com/robots.txt", "https://jobs.workable.com/robots.txt"]       # neither API was asked


def test_every_account_request_passes_the_address_guard_and_the_shared_pace(monkeypatch):
    calls = []

    async def refuse(url):
        raise SourceError("blocked address")
    monkeypatch.setattr(workable, "_guard", refuse)
    with pytest.raises(SourceError, match="blocked address"):
        run(workable.fetch_account(make_ctx({"/widget/accounts/": load()}, calls), "huggingface"))
    assert calls == []
    seen = []

    async def ok(url):
        seen.append(url)
    monkeypatch.setattr(workable, "_guard", ok)
    limiter = HostLimiter()
    run(workable.fetch_account(make_ctx({"/widget/accounts/": load()}, limiter=limiter), "huggingface"))
    assert seen == ["https://apply.workable.com/api/v1/widget/accounts/huggingface"]
    assert limiter.intervals["workable-accounts"] == 1.0 and limiter.limits["workable-accounts"] == 2
    slower = HostLimiter(limits={"workable-accounts": 1}, intervals={"workable-accounts": 0.01})
    run(workable.fetch_account(make_ctx({"/widget/accounts/": load()}, limiter=slower), "huggingface"))
    assert slower.intervals["workable-accounts"] == 0.01 and slower.limits["workable-accounts"] == 1       # an engine setting is kept


def test_429_is_retried_then_pauses_all_account_reads(monkeypatch):
    async def fast(_):
        return None
    monkeypatch.setattr("app.providers.base.asyncio.sleep", fast)
    state = {"n": 0}

    def flaky(request):
        state["n"] += 1
        return httpx.Response(429, headers={"retry-after": "1"}) if state["n"] == 1 else httpx.Response(200, json=load())
    assert len(run(workable.fetch_account(make_ctx({"/widget/accounts/": flaky}), "huggingface"))) == 4 and state["n"] == 2
    always = make_ctx({"/widget/accounts/": httpx.Response(429)})

    async def go():
        with pytest.raises(SourceError, match="HTTP 429"):
            await workable.fetch_account(always, "huggingface")
        return always.limiter._next["workable-accounts"] - asyncio.get_running_loop().time()
    assert run(go()) > 500                                   # the ten-minute pause of the paced group


def test_the_registry_entry_reads_an_account_for_a_key_and_searches_for_the_star():
    calls = []
    search = {"jobs": [{"id": "wk-1", "title": "AI Engineer", "state": "published", "url": "https://jobs.workable.com/view/abc/x",
                        "company": {"title": "Gizmo Labs"}, "locations": ["Denver, Colorado, United States"]}]}
    ctx = make_ctx({"/widget/accounts/huggingface": load(), "/api/v1/jobs": search}, calls)
    assert SPECS["workable"].fetch is workable.fetch and not SPECS["workable"].keyed
    assert [j.board for j in run(workable.fetch(ctx, "huggingface", ""))] == ["huggingface"] * 4
    found = run(workable.fetch(ctx, "*", "ai engineer"))
    assert [(j.board, j.company, j.apply_url) for j in found] == [("*", "Gizmo Labs", "")]
    assert any(c.startswith("https://jobs.workable.com/api/v1/jobs?query=ai+engineer") for c in calls)


def test_an_account_read_through_the_engine_is_stored_and_a_missing_account_is_marked_dead():
    from tests import conftest, test_wider_search as wide

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "apply.workable.com":
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow:\n")
            if request.url.path == "/api/v1/widget/accounts/huggingface":
                return httpx.Response(200, json=load())
            return httpx.Response(404, json={})
        return wide.handler(request)

    db = Database(":memory:")
    ai = wide.WideAI(available=False)
    engine = wide.SearchEngine(wide.wide_settings(searxng_url=""), db, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                               seeds=[], ai_factory=lambda: ai, sources=["workable"], browser=conftest.FakeBrowser(available=False))
    engine.limiter.intervals["workable-accounts"] = 0.0

    async def go():
        try:
            good = await engine._refresh_unit(Unit("workable", "huggingface", ""), "catalog")
            gone = await engine._refresh_unit(Unit("workable", "gone-co", ""), "catalog")
            return good, gone, await engine.index.index_stats()
        finally:
            await engine.aclose()
    good, gone, stats = run(go())
    assert good == (Unit("workable", "huggingface", ""), 4, "") and gone[1] == 0 and gone[2].startswith("HTTP 404")
    assert db.known_boards() == [("workable", "huggingface")] and db.dead_boards() == {("workable", "gone-co")}
    assert stats["by_source"]["workable"] == {"units": 1, "postings": 4}


def test_the_same_vacancy_read_both_ways_is_one_result_and_the_account_record_wins():
    """The job board numbers its listings itself; those numbers are not the employer's posting codes. Kept as posting
    ids of the same ATS, the two copies of one vacancy could never be merged ("different ids = different requisitions")."""
    from app.deduplicator import deduplicate
    from app.search_scope import platform_of_job
    account = run(workable.fetch_account(make_ctx({"/widget/accounts/huggingface": load()}), "huggingface"))
    posting = account[1]                                   # "... - US Remote", New York
    listing = workable._search_job({
        "id": "0b7c9c8e-1111-4222-8333-444455556666", "title": posting.title, "state": "published", "description": "<p>About the role</p>",
        "url": "https://jobs.workable.com/view/abcDEF123/remote-open-source-machine-learning-engineer-in-new-york-at-hugging-face",
        "locations": ["TELECOMMUTE", "New York, New York, United States"], "created": "2026-05-29T10:00:00.000Z",
        "location": {"city": "New York", "subregion": "New York", "countryName": "United States"},
        "company": {"title": "Hugging Face"}, "workplace": "remote"}, "T")
    assert listing.ats == "workable" and listing.ats_id == "" and platform_of_job(listing) == "workable" == platform_of_job(posting)
    for order in ([listing, posting], [posting, listing]):
        unique, removed = deduplicate([job.model_copy(deep=True) for job in order])
        assert removed == 1 and len(unique) == 1
        kept = unique[0]
        assert kept.url == "https://apply.workable.com/j/19A136F8E2" and kept.apply_url.endswith("/19A136F8E2/apply")
        assert kept.ats_id == "19A136F8E2" and kept.board == "huggingface" and kept.sources == ["Workable"]
    # Two different postings of one employer stay two, with or without a listing of one of them.
    second = posting.model_copy(update={"ats_id": "ZZZZ9999", "url": "https://apply.workable.com/j/ZZZZ9999"})
    unique, removed = deduplicate([posting.model_copy(deep=True), second, listing.model_copy(deep=True)])
    assert removed == 1 and sorted(job.ats_id for job in unique) == ["19A136F8E2", "ZZZZ9999"]
    assert deduplicate([job.model_copy(deep=True) for job in account])[1] == 0


# ---------------------------------------------------------------- registry: the search switch and the budget cut
def test_the_cross_company_search_can_be_switched_off_without_the_account_reader():
    queries = ["ai engineer", "ml engineer"]
    on = registry.enabled_specs(Settings(_env_file=None))
    assert Settings(_env_file=None).workable_search_enabled is True
    assert [u for u in registry.plan_units(on, {}, queries) if u.src == "workable"] == [Unit("workable", "*", q) for q in queries]
    off = registry.enabled_specs(Settings(_env_file=None, workable_search_enabled=False))
    assert "workable" in off and off["workable"].fetch is workable.fetch            # accounts are still read
    assert not [u for u in registry.plan_units(off, {}, queries) if u.src == "workable"]
    assert SPECS["workable"].default_key == "*"                                     # the shared entry is not changed
    assert "workable" not in registry.enabled_specs(Settings(_env_file=None, disabled_sources="workable"))


def test_the_unit_budget_cuts_boards_only_never_the_query_and_feed_units():
    """Production, 2026-11-20: 6,629 Greenhouse, 3,707 Ashby and 1,772 Lever boards were known and the budget of a
    25-job search is 1,500 units. Cut in registry order, every unit left was a Greenhouse board."""
    settings = Settings(_env_file=None)
    specs = registry.enabled_specs(settings)
    keys = {"greenhouse": [f"g{i}" for i in range(6629)], "ashby": [f"a{i}" for i in range(3707)],
            "lever": [f"l{i}" for i in range(1772)], "recruitee": [f"r{i}" for i in range(30)]}
    planned = [*registry.plan_units(specs, keys, ["data engineer", "analytics engineer"]), Unit("workable", "acme", "")]
    limit = plan_for(settings, 25).boards
    assert limit == 1500 and Counter(u.src for u in planned[:limit]) == {"greenhouse": 1500}              # the old cut
    kept = registry.within_budget(planned, limit)
    by_source = Counter(u.src for u in kept)
    assert len(kept) == limit
    assert (by_source["workable"], by_source["amazon"], by_source["himalayas"], by_source["remoteok"], by_source["jobicy"]) == (2, 2, 2, 1, 1)
    assert by_source["greenhouse"] == limit - 8 and Unit("workable", "acme", "") not in kept              # an account is a board
    boards = [u for u in kept if u.src == "greenhouse"]
    assert boards == [Unit("greenhouse", f"g{i}", "") for i in range(limit - 8)]                          # their order is unchanged
    small = registry.plan_units(specs, {"greenhouse": ["a", "b"]}, ["data engineer"])
    assert registry.within_budget(small, limit) == small                                                  # under the budget: untouched
    assert registry.within_budget(planned, 0) == []


# ---------------------------------------------------------------- catalogue hooks
class FakeIndex:
    def __init__(self, due):
        self.due, self.asked, self.touched, self.results = due, [], [], []

    async def catalog_due(self, per_ats, recheck, *waits):
        self.asked.append((dict(per_ats), recheck, *waits))
        return [pair for pair in self.due if pair[0] in per_ats]

    async def catalog_touch(self, pairs):
        self.touched += pairs

    async def catalog_result(self, ats, slug, us_count):
        self.results.append((ats, slug, us_count))


def test_the_trickle_reads_workable_accounts_and_asks_with_the_waiting_times():
    class Engine:
        def __init__(self):
            self.index = FakeIndex([("workable", "huggingface"), ("greenhouse", "acme"), ("rippling", "acme-co")])
            self.specs = registry.enabled_specs(Settings(_env_file=None))
            self._inflight, self.spawned = {}, []

        def _spawn(self, unit, origin):
            self.spawned.append((unit, origin))
    eng = Engine()
    assert run(board_catalog.catalog_tick(eng)) == 2
    assert eng.spawned == [(Unit("workable", "huggingface", ""), "catalog"), (Unit("greenhouse", "acme", ""), "catalog")]
    boards, queried = eng.index.asked
    assert {ats: boards[0][ats] for ats in ("greenhouse", "lever", "ashby", "workable", "recruitee")} == {
        "greenhouse": 20, "lever": 8, "ashby": 8, "workable": 4, "recruitee": 15}
    assert "workday" not in boards[0]
    assert "rippling" not in boards[0] and "pinpoint" not in boards[0]              # readers that are off are never asked for
    assert boards[1:] == (7 * 86400.0, 24 * 3600.0, 6 * 3600.0, 30 * 86400.0, 90 * 86400.0)
    # Workday is searched per title; its catalogue read only proves a tenant is alive: weekly, at the unchanged rate of 2.
    assert queried == ({"workday": 2}, 7 * 86400.0, 7 * 86400.0, 6 * 3600.0, 30 * 86400.0, 90 * 86400.0)
    assert eng.index.touched == [("workable", "huggingface"), ("greenhouse", "acme")]
    eng.specs = registry.enabled_specs(Settings(_env_file=None, disabled_sources="workable"))
    eng.spawned.clear()
    assert run(board_catalog.catalog_tick(eng)) == 1 and eng.spawned[0][0].src == "greenhouse"


def test_default_rates_only_recruitee_changed():
    assert board_catalog.DEFAULT_PER_MINUTE == {"greenhouse": 20, "lever": 8, "ashby": 8, "workable": 4, "workday": 2,
                                                "smartrecruiters": 4, "bamboohr": 6, "recruitee": 15, "pinpoint": 6, "rippling": 4,
                                                "career_page": 12}      # remembered postings on employers' own sites, one page each
    # One read of a Recruitee board is two requests (robots.txt, the board); the platform's limiter allows one a second.
    from app.providers import ats_startup
    assert 2 * board_catalog.DEFAULT_PER_MINUTE["recruitee"] * ats_startup.HOST_INTERVAL_SECONDS["recruitee"] <= 30


def test_note_read_records_us_postings_or_a_miss_and_never_raises():
    jobs = run(workable.fetch_account(make_ctx({"/widget/accounts/huggingface": load()}), "huggingface"))
    index = FakeIndex([])

    async def go():
        await board_catalog.note_read(index, Unit("workable", "huggingface", ""), jobs)        # an account: 3 of 4 in the US
        await board_catalog.note_read(index, Unit("recruitee", "acme", ""), [])                # answered, nothing there
        await board_catalog.note_read(index, Unit("greenhouse", "down", ""), None)             # the read failed
        await board_catalog.note_read(index, Unit("greenhouse", "big", ""), jobs * 60)         # a large board is counted off the loop
        await board_catalog.note_read(index, Unit("workable", "*", "ai engineer"), jobs)       # a query is not a board
        await board_catalog.note_read(index, Unit("workday", "t|wd1|s", "ai engineer"), None)  # one title of a tenant is not the board
        await board_catalog.note_read(index, Unit("remoteok", "x", ""), jobs)                  # a feed is not a board
        await board_catalog.note_read(object(), Unit("greenhouse", "acme", ""), jobs)          # an index without read history

        class Broken(FakeIndex):
            async def catalog_result(self, ats, slug, us_count):
                raise RuntimeError("column misses does not exist")
        await board_catalog.note_read(Broken([]), Unit("greenhouse", "acme", ""), jobs)        # never fails the read
    run(go())
    assert index.results == [("workable", "huggingface", 3), ("recruitee", "acme", 0), ("greenhouse", "down", None), ("greenhouse", "big", 180)]


# ---------------------------------------------------------------- sharing one tick between the classes of due boards
def rows(ats, **classes):
    names = {"live": LIVE, "new": NEW, "retry": RETRY, "dead": DEAD}
    return [(ats, f"{kind}{i}", names[kind]) for kind, n in classes.items() for i in range(n)]


def slugs(picked):
    return [slug for _, slug in picked]


def test_allot_due_live_first_but_never_at_the_cost_of_unread_boards():
    everything = rows("recruitee", live=9, new=9, retry=9, dead=9)
    assert slugs(allot_due(everything, {"recruitee": 6})) == ["live0", "live1", "live2", "new0", "new1", "new2"]
    assert slugs(allot_due(everything, {"recruitee": 5})) == ["live0", "live1", "live2", "new0", "new1"]      # half, rounded up
    assert slugs(allot_due(everything, {"recruitee": 1})) == ["live0"]
    # What a class does not need goes to the next one, in the order live, new, retry, dead.
    assert slugs(allot_due(rows("recruitee", live=1, new=9), {"recruitee": 6})) == ["live0", "new0", "new1", "new2", "new3", "new4"]
    assert slugs(allot_due(rows("recruitee", live=9, new=1), {"recruitee": 6})) == ["live0", "live1", "live2", "live3", "live4", "new0"]
    assert slugs(allot_due(rows("recruitee", live=9), {"recruitee": 6})) == [f"live{i}" for i in range(6)]    # nothing unread: all for refreshes
    assert slugs(allot_due(rows("recruitee", live=1, new=1, retry=9, dead=9), {"recruitee": 6})) == ["live0", "new0", "retry0", "retry1", "retry2", "retry3"]
    assert slugs(allot_due(rows("recruitee", retry=2, dead=9), {"recruitee": 6})) == ["retry0", "retry1", "dead0", "dead1", "dead2", "dead3"]
    assert allot_due(everything, {"recruitee": 0}) == [] and allot_due([], {"recruitee": 6}) == []
    both = allot_due([*rows("recruitee", new=5), *rows("workday", new=5)], {"recruitee": 3, "workday": 2})
    assert Counter(ats for ats, _ in both) == {"recruitee": 3, "workday": 2}                                  # each ATS keeps its own allowance
