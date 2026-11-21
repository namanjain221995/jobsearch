"""Board catalogue import and the extra board readers (SmartRecruiters, BambooHR). No network: mocked transports."""
import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from app import board_catalog
from app.portal import import_boards
from app.providers import ats_extra, boards
from app.providers.base import BoardNotFound, Ctx, HostLimiter, SourceError

FIX = Path(__file__).parent / "fixtures" / "boards"


def load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    async def ok(url):
        return "93.184.216.34"
    monkeypatch.setattr(ats_extra, "_guard", ok)


def make_ctx(routes, calls=None, limiter=None):
    def handler(request: httpx.Request):
        if calls is not None:
            calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=routes.get("robots", "User-agent: *\nDisallow:\n"))
        for match, resp in routes.items():
            if match != "robots" and match in str(request.url):
                r = resp(request) if callable(resp) else resp
                return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)
        return httpx.Response(404, json={})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Ctx(client, limiter or HostLimiter(), options={})


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- SmartRecruiters
def test_smartrecruiters_paginates_dedupes_and_parses_locations():
    calls = []
    ctx = make_ctx({"offset=0": load("smartrecruiters_page1.json"), "offset=100": load("smartrecruiters_page2.json")}, calls)
    jobs = run(ats_extra.fetch_smartrecruiters(ctx, "ExampleCo"))
    assert [j.ats_id for j in jobs] == ["744000012345678", "744000012345679", "744000012345680"]
    first = jobs[0]
    assert first.location == "Austin, TX, United States" and first.country_hint == "United States" and first.work_mode == "Hybrid"
    assert first.url == "https://jobs.smartrecruiters.com/ExampleCo/744000012345678" and first.needs_detail
    assert first.source == "SmartRecruiters" and first.ats == "smartrecruiters" and first.posted_at == "2026-11-10"
    assert jobs[1].country_hint == "DE" and jobs[2].work_mode == "Remote"
    assert sum("postings?" in c for c in calls) == 2


def test_smartrecruiters_detail_and_closed_posting():
    ctx = make_ctx({"offset=0": load("smartrecruiters_page1.json"), "offset=100": {"content": []},
                    "/postings/744000012345678": load("smartrecruiters_detail.json")})
    job = run(ats_extra.fetch_smartrecruiters(ctx, "ExampleCo"))[0]
    done = run(ats_extra.detail_smartrecruiters(ctx, job))
    assert "Train models" in done.raw_html and done.apply_url.startswith("https://jobs.smartrecruiters.com/") and not done.needs_detail
    job2 = run(ats_extra.fetch_smartrecruiters(ctx, "ExampleCo"))[1]
    assert run(ats_extra.detail_smartrecruiters(ctx, job2)) is None          # 404 = no longer open


def test_smartrecruiters_respects_robots_disallow():
    ctx = make_ctx({"robots": "User-agent: LinkedInBot\nAllow: /v1/companies/\nUser-agent: *\nDisallow: /\n",
                    "offset=0": load("smartrecruiters_page1.json")})
    with pytest.raises(SourceError, match="robots.txt disallows"):
        run(ats_extra.fetch_smartrecruiters(ctx, "ExampleCo"))


@pytest.mark.parametrize("key", ["../x", "a/b", "", "a b", "x" * 200, "é"])
def test_hostile_keys_are_rejected_before_any_request(key):
    calls = []
    ctx = make_ctx({}, calls)
    with pytest.raises(BoardNotFound):
        run(ats_extra.fetch_smartrecruiters(ctx, key))
    with pytest.raises(BoardNotFound):
        run(ats_extra.fetch_bamboohr(ctx, key))
    assert calls == []


def test_malformed_payloads_raise_source_error():
    ctx = make_ctx({"postings": ["not", "a", "dict"], "careers/list": {"result": "nope"}})
    with pytest.raises(SourceError):
        run(ats_extra.fetch_smartrecruiters(ctx, "ExampleCo"))
    with pytest.raises(SourceError):
        run(ats_extra.fetch_bamboohr(ctx, "acme"))
    bad = make_ctx({"careers/list": httpx.Response(200, text="<html>")})
    with pytest.raises(SourceError, match="Invalid JSON"):
        run(ats_extra.fetch_bamboohr(bad, "acme"))


def test_429_is_retried_then_pauses_the_group(monkeypatch):
    async def fast(_):
        return None
    monkeypatch.setattr("app.providers.base.asyncio.sleep", fast)
    state = {"n": 0}

    def flaky(request):
        state["n"] += 1
        return httpx.Response(429, headers={"retry-after": "1"}) if state["n"] == 1 else httpx.Response(200, json=load("bamboohr_list.json"))
    ctx = make_ctx({"careers/list": flaky})
    assert len(run(ats_extra.fetch_bamboohr(ctx, "10squared"))) == 2 and state["n"] == 2

    always = make_ctx({"careers/list": httpx.Response(429)})

    async def go():
        with pytest.raises(SourceError, match="HTTP 429"):
            await ats_extra.fetch_bamboohr(always, "acme")
        return always.limiter._next["bamboohr"] - asyncio.get_running_loop().time()
    assert run(go()) > 500                                   # the ten-minute pause of the paced group


# ---------------------------------------------------------------- BambooHR (recorded 2026-11-18)
def test_bamboohr_list_and_detail():
    ctx = make_ctx({"careers/list": load("bamboohr_list.json"), "careers/28/detail": load("bamboohr_detail.json")})
    jobs = run(ats_extra.fetch_bamboohr(ctx, "10squared"))
    assert [j.ats_id for j in jobs] == ["10squared:28", "10squared:29"]
    assert jobs[0].location == "Austin, Texas" and jobs[0].url == "https://10squared.bamboohr.com/careers/28" and jobs[0].needs_detail
    done = run(ats_extra.detail_bamboohr(ctx, jobs[0]))
    assert done.country_hint == "United States" and "WHO WE ARE" in done.raw_html and done.location == "Austin, Texas, United States"
    assert run(ats_extra.detail_bamboohr(ctx, jobs[1])) is None


def test_extra_sources_off_by_default():
    assert boards.extra_specs() == {} or ats_extra.SMARTRECRUITERS_ENABLED or ats_extra.BAMBOOHR_ENABLED
    specs = boards.extra_specs(include_disabled=True)
    assert set(specs) == {"smartrecruiters", "bamboohr"} and all(s.keyed and s.kind == "board" and s.detail for s in specs.values())


# ---------------------------------------------------------------- catalogue
@pytest.mark.parametrize("ats,slug,url,expected", [
    ("workday", "3m/search", "https://3m.wd1.myworkdayjobs.com/search", "3m|wd1|search"),
    ("workday", "x", "https://NVIDIA.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite", "nvidia|wd5|NVIDIAExternalCareerSite"),
    ("workday", "x", "https://evil.example.com/search", None),
    ("workday", "x", "http://3m.wd1.myworkdayjobs.com/search", None),
    ("workday", "x", "https://3m.wd1.myworkdayjobs.com/", None),
    ("workday", "x", "https://3m.wd1.myworkdayjobs.com/..%2Fadmin", None),
    ("greenhouse", "Stripe", "", "stripe"),
    ("greenhouse", "../etc/passwd", "", None),
    ("lever", "a b", "", None),
    ("ashby", "marathon%20fusion", "", None),
    ("lever", "https://jobs.eu.lever.co/x", "", None),
    ("greenhouse", "café", "", None),
    ("smartrecruiters", "BoschGroup", "", "BoschGroup"),
    ("smartrecruiters", "../x", "", None),
    ("bamboohr", "Acme-Co", "", "acme-co"),
    ("bamboohr", "acme.evil", "", None),
    ("unknown", "x", "", None),
])
def test_normalize(ats, slug, url, expected):
    assert board_catalog.normalize(ats, slug, url) == expected


def test_load_csv_counts_duplicates_and_malformed(tmp_path):
    p = tmp_path / "greenhouse.csv"
    p.write_text("name,slug,url\nA,acme,u\nA2,ACME,u\nB,../b,u\nC,beta,u\nD,,u\n", encoding="utf-8")
    loaded = board_catalog.load_csv("greenhouse", p)
    assert loaded.keys == ["acme", "beta"] and loaded.rows == 5 and loaded.duplicates == 1 and loaded.malformed == 2


def test_shipped_lists_load():
    wd = board_catalog.load_csv("workday")
    assert len(wd.keys) > 5000 and all(k.count("|") == 2 for k in wd.keys)
    assert (board_catalog.DATA_DIR / "LICENSE-ats-scrapers").read_text().startswith("MIT License")


def test_simplify_keys_only_keeps_ids():
    listings = [{"url": "https://job-boards.greenhouse.io/acme/jobs/123", "active": True, "title": "x"},
                {"url": "https://acme.wd5.myworkdayjobs.com/External/job/US/X_R1", "active": True},
                {"url": "https://jobs.smartrecruiters.com/BoschGroup/744", "active": True},
                {"url": "https://jobs.lever.co/old", "active": False}, "junk"]
    assert board_catalog.simplify_keys(listings) == {"greenhouse": ["acme"], "workday": ["acme|wd5|External"],
                                                     "smartrecruiters": ["BoschGroup"]}


class FakeIndex:
    def __init__(self, rows=()):
        self.rows = {(a, s): {"origin": o, "ok": True, "job_count": 0, "checked_at": None} for a, s, o in rows}
        self.recorded = []

    async def board_slugs(self, ats):
        return {s for a, s in self.rows if a == ats}

    async def register_catalog(self, pairs):
        n = 0
        for p in pairs:
            if p not in self.rows:
                self.rows[p] = {"origin": "catalog", "ok": True, "job_count": 0, "checked_at": None}
                n += 1
        return n

    async def record_board(self, ats, slug, ok, job_count=0, origin="seed"):
        self.recorded.append((ats, slug, ok, job_count))

    async def catalog_due(self, per_ats, recheck, *waits):          # waits: refresh, retry, dead, longest back-off
        out = []
        for ats, n in per_ats.items():
            out += [(a, s) for (a, s), r in self.rows.items() if a == ats and r["origin"] == "catalog" and r["checked_at"] is None][:n]
        return out

    async def catalog_touch(self, pairs):
        for p in pairs:
            self.rows[p]["checked_at"] = time.time()


def test_import_is_idempotent_and_dry_run_writes_nothing():
    index = FakeIndex([("greenhouse", "stripe", "seed"), ("lever", "100ms", "searxng")])
    dry = run(import_boards.main(["--dry-run", "--only", "greenhouse,lever"], index=index))
    assert dry["ats"]["greenhouse"]["registered"] == 0 and len(index.rows) == 2
    first = run(import_boards.main(["--only", "greenhouse,lever"], index=index))
    gh = first["ats"]["greenhouse"]
    assert gh["registered"] == gh["new"] > 7000 and first["ats"]["lever"]["existing"] >= 1
    assert gh["new"] == dry["ats"]["greenhouse"]["new"]
    again = run(import_boards.main(["--only", "greenhouse,lever"], index=index))
    assert again["ats"]["greenhouse"]["new"] == 0 and again["ats"]["greenhouse"]["registered"] == 0


def test_import_rejects_unknown_ats():
    with pytest.raises(SystemExit):
        run(import_boards.main(["--only", "nope"], index=FakeIndex()))


def test_validate_reports_live_ratio_without_writing_in_dry_run(monkeypatch):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if "bamboohr" in request.url.host and request.url.host.startswith("10squared"):
            return httpx.Response(200, json=load("bamboohr_list.json"))
        return httpx.Response(404, json={})
    monkeypatch.setattr(board_catalog, "load_csv", lambda ats, path=None: board_catalog.Loaded(keys=["10squared", "gone"], rows=2))
    monkeypatch.setattr(import_boards, "VALIDATE_INTERVALS", {})
    index = FakeIndex()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await import_boards.main(["--dry-run", "--only", "bamboohr", "--validate", "5"], index=index, client=client)
    v = run(go())["ats"]["bamboohr"]["validation"]
    assert v["sampled"] == 2 and v["live"] == 1 and v["dead"] == 1 and v["live_ratio"] == 0.5 and v["postings_per_live_board"] == 2
    assert v["us_postings"] == 2 and index.recorded == []


def test_trickle_tick_spawns_bounded_units_through_the_engine():
    from app.providers.registry import SPECS

    class Engine:
        def __init__(self):
            self.index = FakeIndex([("greenhouse", f"b{i}", "catalog") for i in range(10)] + [("lever", "l1", "catalog")])
            self.specs = {"greenhouse": SPECS["greenhouse"]}          # lever switched off: not read
            self._inflight, self.spawned = {}, []

        def _spawn(self, unit, origin):
            self.spawned.append((unit, origin))
    eng = Engine()
    assert run(board_catalog.catalog_tick(eng, {"greenhouse": 3, "lever": 3})) == 3
    assert [u.key for u, _ in eng.spawned] == ["b0", "b1", "b2"] and all(o == "catalog" and u.scope == "" for u, o in eng.spawned)
    assert run(board_catalog.catalog_tick(eng, {"greenhouse": 3})) == 3 and eng.spawned[3][0].key == "b3"   # touched rows move on
    eng._inflight = {i: None for i in range(board_catalog.MAX_INFLIGHT + 1)}
    assert run(board_catalog.catalog_tick(eng, {"greenhouse": 3})) == 0
