"""The career-site crawler against PostgreSQL: the tables of migration a8c5e3f1d2b7, the store's statements, a crawl
whose postings a later search finds through the normal index path with no web search, the Admin health line and the
seed-file import. The sites are synthetic (tests/test_career_sites.py); nothing leaves the machine."""
import asyncio
import time

import asyncpg
import httpx

from app import board_catalog, career_sites as cs, netguard
from app.config import Settings
from app.portal import import_boards
from app.portal.index_pg import PgJobIndex
from app.portal.services import ats_health
from app.providers.registry import CAREER_PAGE
from app.search_engine import SearchEngine
from tests.conftest import FakeAI, FakeBrowser, make_request, run_search
from tests.portal.conftest import PLAIN_URL
from tests.test_career_sites import BASE, HOST, Web, day, posting, urlset

DAY = 86400.0
TABLES = "index_career_crawls, index_career_sites, index_postings, index_units, index_boards, index_meta"


async def fresh_index() -> PgJobIndex:
    index = await PgJobIndex(PLAIN_URL).connect()
    await index.pool.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    return index


async def test_the_migration_added_two_tables_and_touched_nothing_else(database):
    conn = await asyncpg.connect(PLAIN_URL)
    try:
        cols = {(r["table_name"], r["column_name"]): r["data_type"] for r in await conn.fetch(
            "SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_name LIKE 'index_career_%'")}
        assert cols[("index_career_sites", "host")] == "text" and cols[("index_career_sites", "next_at")] == "double precision"
        assert cols[("index_career_sites", "seen")] == "ARRAY" and cols[("index_career_crawls", "requests")] == "integer"
        assert {t for t, _ in cols} == {"index_career_sites", "index_career_crawls"}
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "a8c5e3f1d2b7"
        # The previous release's statements on the index still work: none of them names the new tables.
        await conn.execute("INSERT INTO index_boards (ats, slug, ok, job_count, checked_at, origin) VALUES ('zz-old','a',true,0,NULL,'catalog') "
                           "ON CONFLICT (ats, slug) DO UPDATE SET ok=excluded.ok")
        await conn.execute("DELETE FROM index_boards WHERE ats='zz-old'")
    finally:
        await conn.close()


async def test_sites_are_kept_once_claimed_once_and_every_crawl_is_recorded(database):
    index = await fresh_index()
    store = cs.PgStore(index)
    try:
        assert await store.register("acme-robotics.com", "https://www.acme-robotics.com", "Acme Robotics", "search",
                                    "https://www.acme-robotics.com/jobs/4471823/x") is True
        assert await store.register("acme-robotics.com", "https://acme-robotics.com", "Someone else", "file") is False      # left alone
        assert await store.register("b-corp.com", "https://b-corp.com", "B Corp", "file", "", "https://b-corp.com/openings") is True
        assert await store.register("c_corp.com", "https://c_corp.com") is True
        now = time.time()                                # a new site is due from the moment it was registered
        await index.pool.execute("UPDATE index_career_sites SET next_at = $1 WHERE host = 'b-corp.com'", now + 3600)        # not due yet
        await index.pool.execute("UPDATE index_career_sites SET next_at = $1 WHERE host = 'c_corp.com'", now - 50)
        # Two processes ask at the same moment: each due site is handed out once.
        one, two = await asyncio.gather(store.claim(now, 5, 7 * DAY), cs.PgStore(index).claim(now, 5, 7 * DAY))
        claimed = [s.host for s in [*one, *two]]
        assert sorted(claimed) == ["acme-robotics.com", "c_corp.com"]
        site = next(s for s in [*one, *two] if s.host == "acme-robotics.com")
        assert (site.base_url, site.employer, site.origin, site.state, site.misses, site.seen) == (
            "https://www.acme-robotics.com", "Acme Robotics", "search", cs.NEW, 0, ())
        assert site.found_url.endswith("/jobs/4471823/x")
        assert [s.host for s in await store.claim(now + 3601, 5, 7 * DAY)] == ["b-corp.com"]
        assert await store.claim(now + 6 * DAY, 5, 7 * DAY) == []                       # all three wait a week from their claim
        assert len(await store.claim(now + 7 * DAY + 1, 1, 7 * DAY)) == 1               # ... and `limit` is kept

        record = cs.CrawlRecord(host="acme-robotics.com", started_at=now, finished_at=now + 40, method=cs.SITEMAP, state=cs.OK,
                                candidates=12, pages_seen=9, postings=5, us_postings=3, requests=14, browser_pages=1, error="")
        seen = (cs.page_hash("https://www.acme-robotics.com/jobs/1"), cs.page_hash("https://www.acme-robotics.com/jobs/2"), -5, 2 ** 62)
        await store.finish("acme-robotics.com", record, now + 7 * DAY, 0, seen)
        row = dict(await index.pool.fetchrow("SELECT * FROM index_career_sites WHERE host='acme-robotics.com'"))
        assert (row["state"], row["method"], row["candidates"], row["pages_seen"], row["postings"], row["us_postings"], row["misses"]) == (
            cs.OK, cs.SITEMAP, 12, 9, 5, 3, 0)
        assert row["crawled_at"] == now + 40 and row["next_at"] == now + 7 * DAY and tuple(row["seen"]) == seen
        again = (await store.claim(now + 8 * DAY, 5, 7 * DAY))
        assert next(s for s in again if s.host == "acme-robotics.com").seen == seen
        crawl = dict(await index.pool.fetchrow("SELECT * FROM index_career_crawls"))
        assert (crawl["host"], crawl["pages_seen"], crawl["postings"], crawl["us_postings"], crawl["requests"], crawl["browser_pages"]) == (
            "acme-robotics.com", 9, 5, 3, 14, 1)
        assert await store.pages_since(now - 10) == 9 and await store.pages_since(now + 1) == 0
        # Crawl rows older than the log's reach go with the next record.
        old = cs.CrawlRecord(host="b-corp.com", started_at=now - 400 * DAY, finished_at=now - 400 * DAY, state=cs.FAILED, error="x" * 900)
        await store.finish("b-corp.com", old, now, 1, ())
        await store.finish("b-corp.com", cs.CrawlRecord(host="b-corp.com", started_at=now, finished_at=now, state=cs.NO_US), now, 2, ())
        assert await index.pool.fetchval("SELECT count(*) FROM index_career_crawls WHERE host='b-corp.com'") == 1

        # Pages of a site that are already in the index, whatever their state; "_" in a host name is not a wildcard.
        for slug in ("https://www.acme-robotics.com/jobs/1", "https://acme-robotics.com/jobs/2", "https://c_corp.com/jobs/3",
                     "https://cxcorp.com/jobs/4", "https://evil.com/https://acme-robotics.com/jobs/5"):
            await index.record_board(CAREER_PAGE, slug, True, 1, "catalog")
        assert await store.known_pages("acme-robotics.com") == {"https://www.acme-robotics.com/jobs/1", "https://acme-robotics.com/jobs/2"}
        assert await store.known_pages("c_corp.com") == {"https://c_corp.com/jobs/3"}
    finally:
        await index.close()


def a_company_site() -> Web:
    jobs = {f"{BASE}/jobs/4471823/ai-engineer": posting("AI Engineer"), f"{BASE}/jobs/4471824/senior-ai-engineer": posting("Senior AI Engineer", "Denver", "CO"),
            f"{BASE}/jobs/4471825/ai-engineer-toronto": posting("AI Engineer", "Toronto", "ON", "CA"),
            f"{BASE}/jobs/4471826/account-executive": posting("Account Executive", "Chicago", "IL")}
    return Web({f"{BASE}/robots.txt": (200, f"User-agent: *\nDisallow: /admin\nSitemap: {BASE}/sitemap.xml\n"),
                f"{BASE}/sitemap.xml": (200, urlset([(u, day(i)) for i, u in enumerate(jobs)] + [(f"{BASE}/jobs", day(0))])),
                **{u: (200, body) for u, body in jobs.items()}})


async def test_a_crawled_site_is_found_by_a_search_through_the_index_without_any_web_search(database):
    index = await fresh_index()
    web = a_company_site()
    settings = Settings(_env_file=None, database_url="sqlite:///:memory:", openai_api_key="", anthropic_api_key="", prewarm_on_start=False,
                        browser_enabled=False, fetch_deadline_seconds=10)
    fake = FakeAI(available=False)
    engine = SearchEngine(settings, index, client=httpx.AsyncClient(transport=httpx.MockTransport(web.handler)), seeds=[],
                          ai_factory=lambda: fake, sources=[CAREER_PAGE], browser=FakeBrowser(available=False))
    engine.limiter.intervals = {}
    try:
        crawler = engine.career_sites
        assert isinstance(crawler, cs.Crawler) and isinstance(crawler.store, cs.PgStore)
        crawler.reader.host_gap = 0.0
        # A search once verified ONE posting on the company's site and kept it: that registers the site.
        first = (await engine.verifier.verify(cs.Candidate(url=f"{BASE}/jobs/4471823/ai-engineer", provider="Claude"))).job
        assert first is not None and await engine._remember_pages([first]) == 1
        assert await index.pool.fetchval("SELECT employer FROM index_career_sites WHERE host=$1", HOST) == "Acme Robotics"
        # The background reader's tick starts the crawl; nothing of it runs inside a search.
        await board_catalog.catalog_tick(engine, {})
        assert len(crawler._running) == 1
        (record,) = await asyncio.gather(*list(crawler._running))
        assert (record.state, record.method, record.candidates, record.known, record.pages_seen, record.postings, record.us_postings) == (
            cs.OK, cs.SITEMAP, 4, 1, 3, 3, 2)
        boards = await index.pool.fetch("SELECT slug, ok, job_count, origin, us_count FROM index_boards WHERE ats=$1 ORDER BY slug", CAREER_PAGE)
        assert [(r["slug"].rsplit("/", 2)[1], r["ok"], r["job_count"], r["origin"], r["us_count"]) for r in boards] == [
            ("4471823", True, 1, "catalog", 1), ("4471824", True, 1, "catalog", 1), ("4471826", True, 1, "catalog", 1)]       # the Toronto posting is not kept
        site = dict(await index.pool.fetchrow("SELECT state, method, pages_seen, postings, us_postings, misses, next_at, tried_at FROM index_career_sites"))
        assert (site["state"], site["method"], site["pages_seen"], site["postings"], site["us_postings"], site["misses"]) == (cs.OK, cs.SITEMAP, 3, 3, 2, 0)
        assert 6.9 * DAY < site["next_at"] - site["tried_at"] < 7.1 * DAY
        assert await index.pool.fetchval("SELECT requests FROM index_career_crawls") == record.requests == 5            # robots.txt, the sitemap, 3 pages
        assert await board_catalog.catalog_tick(engine, {}) == 0 and not [t for t in crawler._running if not t.done()]   # not due for a week
        # A search without AI and without any web search: the company's postings come from the index.
        asked = len(web.log)
        state = await run_search(engine, make_request(), close=False)
        found = {j.url.rsplit("/", 2)[1]: j for j in state.results}
        assert set(found) == {"4471823", "4471824"} and found["4471824"].company == "Acme Robotics" and found["4471824"].location.startswith("Denver")
        assert found["4471824"].source == "Company career site" and found["4471823"].source == "Claude web search"      # crawled / found by the search
        assert fake.discover_calls == 0 and state.stats["ai_leads"] == 0 and len(web.log) == asked          # fresh units: not one request
        # Three hours later the units are stale: the search checks the pages whose title fits on their own page again.
        await index.pool.execute("UPDATE index_units SET expires_at = $1 WHERE src = $2", time.time() - 1, CAREER_PAGE)
        state = await run_search(engine, make_request(), close=False)
        again = [u for u, _ in web.log[asked:]]
        assert {j.url.rsplit("/", 2)[1] for j in state.results} == {"4471823", "4471824"}
        assert sorted(u.rsplit("/", 2)[1] for u in again) == ["4471823", "4471824"]                          # not the sales posting, not the sitemap
    finally:
        await engine.aclose()
        await index.close()


async def test_company_career_sites_line_in_the_admin_health_data(world):
    admin = await world.as_admin()
    line = {p["id"]: p for p in (await admin.get(ats_health_url())).json()["providers"]}["career_sites"]
    assert (line["label"], line["kind"], line["state"]) == ("Company career sites", "catalogue", "not_configured")
    assert "No company career site is known yet" in line["reason"]
    now = time.time()
    conn = await asyncpg.connect(PLAIN_URL)
    await conn.execute(
        "INSERT INTO index_career_sites (host, base_url, employer, created_at, state, crawled_at, next_at) VALUES "
        "('a.example.com','https://a.example.com','A',$1,'ok',$1,$1), ('b.example.com','https://b.example.com','B',$1,'not_readable',$2,$1), "
        "('c.example.com','https://c.example.com','C',$1,'new',NULL,$1)", now, now - 9 * DAY)
    await conn.execute("INSERT INTO index_boards (ats, slug, ok, job_count, checked_at, origin) VALUES "
                       "('career_page','https://a.example.com/jobs/1',true,1,$1,'catalog'), ('career_page','https://a.example.com/jobs/2',true,1,$1,'catalog'), "
                       "('career_page','https://a.example.com/jobs/3',true,0,$1,'catalog')", now)
    await conn.close()
    ats_health.invalidate(world.app.state)
    line = {p["id"]: p for p in (await admin.get(ats_health_url())).json()["providers"]}["career_sites"]
    assert line["state"] == "configured"
    assert line["reason"] == "3 sites known, 1 read in the last 7 days, 2 postings in the index; 1 site not readable (left alone)."
    assert (await admin.patch("/api/settings", json={"disabled_sources": [CAREER_PAGE]})).status_code == 200
    ats_health.invalidate(world.app.state)
    line = {p["id"]: p for p in (await admin.get(ats_health_url())).json()["providers"]}["career_sites"]
    assert line["state"] == "switched_off" and "3 sites known" in line["reason"]
    assert ats_health._career_site_line(Settings(_env_file=None, career_site_crawl=False), set(), {"career_sites": 3}).state == "switched_off"
    assert ats_health._career_site_line(Settings(_env_file=None, career_page_memory=False), set(), {}).state == "switched_off"
    # Team Leads and Members do not see it (the whole view is Admin only).
    assert (await (await world.as_tl()).get(ats_health_url())).status_code == 403


def ats_health_url() -> str:
    return "/api/integrations/ats"


async def test_sites_are_seeded_from_a_file_and_every_host_is_validated(database, tmp_path, monkeypatch):
    index = await fresh_index()

    async def guard(url):
        host = httpx.URL(url).host
        if host.startswith("gone."):
            raise netguard.UnsafeURL("Hostname does not resolve")
        if host.startswith("inside."):
            raise netguard.UnsafeURL("Hostname resolves to a non-public address")
        return "93.184.216.34"
    monkeypatch.setattr(netguard, "assert_public_url", guard)
    path = tmp_path / "sites.csv"
    path.write_text("host,employer,listing\ncareers.acme-robotics.com,Acme Robotics,/openings\nwww.example-corp.com,Example Corp,\n"
                    "gone.example-corp.com,Gone,\ninside.example-corp.com,Inside,\nboards.greenhouse.io,Vendor,\nwww.indeed.com,Indeed,\n"
                    "192.168.1.10,Router,\n", encoding="utf-8")
    try:
        dry = await import_boards.main(["--career-sites", str(path), "--dry-run"], index=index)
        assert dry["career_sites"]["valid"] == 2 and dry["career_sites"]["registered"] == 0
        assert await index.pool.fetchval("SELECT count(*) FROM index_career_sites") == 0
        report = (await import_boards.main(["--career-sites", str(path)], index=index))["career_sites"]
        assert (report["rows"], report["valid"], report["registered"], report["existing"], len(report["refused"])) == (7, 2, 2, 0, 5)
        assert any("does not resolve" in r for r in report["refused"]) and any("non-public" in r for r in report["refused"])
        rows = [tuple(r) for r in await index.pool.fetch("SELECT host, base_url, employer, origin, listing_url, state FROM index_career_sites ORDER BY host")]
        assert rows == [("careers.acme-robotics.com", "https://careers.acme-robotics.com", "Acme Robotics", "file",
                         "https://careers.acme-robotics.com/openings", "new"),
                        ("example-corp.com", "https://www.example-corp.com", "Example Corp", "file", "", "new")]
        again = (await import_boards.main(["--career-sites", str(path)], index=index))["career_sites"]
        assert (again["registered"], again["existing"]) == (0, 2)                      # idempotent
        import_boards._print({"dry_run": False, "career_sites": report})
        # The shipped example has the form the importer reads.
        rows, refused = cs.read_seed_file(cs.__file__.replace("career_sites.py", "data/career_sites.example.csv"))
        assert len(rows) == 2 and refused == []
    finally:
        await index.close()
