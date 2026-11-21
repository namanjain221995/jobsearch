"""The order in which catalogue boards are read (PgJobIndex.catalog_due), the growing wait after misses, the read
history columns of migration f2a9c4d7e1b3, and what the ATS health view derives from account and board reads."""
import time

import asyncpg

from app import ats_platforms
from app.portal.index_pg import PgJobIndex
from app.portal.services import ats_health
from tests.portal.conftest import PLAIN_URL

HOUR, DAY = 3600.0, 86400.0


async def fresh_index() -> PgJobIndex:
    index = await PgJobIndex(PLAIN_URL).connect()
    await index.pool.execute("TRUNCATE index_postings, index_units, index_boards, index_meta RESTART IDENTITY CASCADE")
    return index


async def put(index, ats, slug, *, ok=True, jobs=0, us=None, misses=0, checked=None, tried=None, origin="catalog"):
    """One board row; `checked` / `tried` are ages in seconds (None = never)."""
    now = time.time()
    await index.pool.execute(
        "INSERT INTO index_boards (ats, slug, ok, job_count, checked_at, origin, tried_at, us_count, misses) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
        ats, slug, ok, jobs, None if checked is None else now - checked, origin, None if tried is None else now - tried, us, misses)


async def age(index, slug, seconds):
    """Let time pass for one board."""
    await index.pool.execute("UPDATE index_boards SET checked_at = checked_at - $2, tried_at = tried_at - $2 WHERE slug = $1", slug, seconds)


async def row(index, slug):
    return dict(await index.pool.fetchrow("SELECT ok, job_count, us_count, misses, checked_at, tried_at FROM index_boards WHERE slug=$1", slug))


async def test_the_migration_added_three_optional_columns(database):
    conn = await asyncpg.connect(PLAIN_URL)
    try:
        cols = {r["column_name"]: (r["data_type"], r["is_nullable"], r["column_default"]) for r in await conn.fetch(
            "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns WHERE table_name='index_boards'")}
        assert cols["tried_at"] == ("double precision", "YES", None) and cols["us_count"] == ("integer", "YES", None)
        assert cols["misses"] == ("integer", "NO", "0")
        assert await conn.fetchval("SELECT version_num FROM alembic_version") is not None
        # The previous release's statements still work: it names its columns and never mentions the new ones.
        await conn.execute("DELETE FROM index_boards WHERE ats='zz-old'")
        await conn.execute("INSERT INTO index_boards (ats, slug, ok, job_count, checked_at, origin) VALUES ('zz-old','a',true,0,NULL,'catalog') "
                           "ON CONFLICT (ats, slug) DO UPDATE SET ok=excluded.ok, job_count=excluded.job_count, checked_at=excluded.checked_at")
        assert await conn.fetchval("SELECT misses FROM index_boards WHERE ats='zz-old'") == 0
        await conn.execute("DELETE FROM index_boards WHERE ats='zz-old'")
    finally:
        await conn.close()


async def test_order_live_boards_first_then_unread_then_retries_then_dead(database):
    index = await fresh_index()
    try:
        r = "recruitee"
        await put(index, r, "live-30h", jobs=5, us=3, checked=30 * HOUR)
        await put(index, r, "live-50h", jobs=9, us=1, checked=50 * HOUR)
        await put(index, r, "live-fresh", jobs=5, us=3, checked=2 * HOUR)                       # read 2 hours ago: not due
        await put(index, r, "legacy-40h", jobs=4, us=None, checked=40 * HOUR)                    # read before US postings were counted
        await put(index, r, "new-a")
        await put(index, r, "new-b")
        await put(index, r, "no-us-8d", jobs=6, us=0, misses=1, checked=8 * DAY)                 # postings, none in the US
        await put(index, r, "no-us-twice-8d", jobs=6, us=0, misses=2, checked=8 * DAY)           # second miss: 14 days
        await put(index, r, "empty-8d", jobs=0, us=0, misses=1, checked=9 * DAY)
        await put(index, r, "failed-7h", tried=7 * HOUR, misses=1)                               # the read failed: 6 hours
        await put(index, r, "failed-twice-7h", tried=7 * HOUR, misses=2)                         # 12 hours
        await put(index, r, "was-live-failed-7h", jobs=5, us=3, misses=1, checked=40 * HOUR, tried=7 * HOUR)
        await put(index, r, "dead-31d", ok=False, misses=1, checked=31 * DAY)
        await put(index, r, "dead-5d", ok=False, misses=1, checked=5 * DAY)
        await put(index, r, "found-by-search", jobs=7, us=7, checked=90 * DAY, origin="searxng")   # not a catalogue row
        await put(index, "lever", "other-ats")

        everything = await index.catalog_due({r: 50})
        assert everything == [(r, s) for s in (
            "live-50h", "legacy-40h", "live-30h",                    # live with US postings, the longest unread first
            "new-a", "new-b",                                        # never read
            "empty-8d", "no-us-8d", "failed-7h", "was-live-failed-7h",   # misses whose wait is over, the longest waiting first
            "dead-31d")]                                             # dead boards last
        # A small allowance: refreshes take at most half (rounded up) while unread boards wait.
        assert await index.catalog_due({r: 4}) == [(r, "live-50h"), (r, "legacy-40h"), (r, "new-a"), (r, "new-b")]
        assert await index.catalog_due({r: 3}) == [(r, "live-50h"), (r, "legacy-40h"), (r, "new-a")]
        assert await index.catalog_due({r: 1}) == [(r, "live-50h")]
        # Every ATS has its own allowance; one that is switched off (0) or not asked for is not read.
        assert sorted(await index.catalog_due({r: 1, "lever": 5, "ashby": 5})) == [("lever", "other-ats"), (r, "live-50h")]
        assert await index.catalog_due({r: 0, "lever": 0}) == [] and await index.catalog_due({}) == []
        # The waits are settings: with a 60-hour refresh nothing live is due, with a 1-hour retry the second failure is.
        assert (r, "live-50h") not in await index.catalog_due({r: 50}, 7 * DAY, 60 * HOUR)
        assert (r, "failed-twice-7h") in await index.catalog_due({r: 50}, 7 * DAY, 24 * HOUR, 1 * HOUR)
    finally:
        await index.close()


async def test_misses_push_a_board_back_with_a_growing_wait(database):
    index = await fresh_index()
    try:
        g = "greenhouse"

        async def due():
            return [slug for _, slug in await index.catalog_due({g: 10})]

        # A board whose reads fail: 6 hours, then 12, then 24.
        await index.register_catalog([(g, "flaky")])
        assert await due() == ["flaky"]
        await index.catalog_touch([(g, "flaky")])
        assert await due() == []                                                # being read: not picked again a minute later
        before = await row(index, "flaky")
        assert before["checked_at"] is None and before["tried_at"] is not None  # an attempt is not an answer
        for misses, wait_hours in ((1, 6), (2, 12), (3, 24)):
            await index.catalog_touch([(g, "flaky")])
            await index.fail_unit(g, "flaky", "", "ConnectTimeout")
            await index.catalog_result(g, "flaky", None)
            assert (await row(index, "flaky"))["misses"] == misses
            await age(index, "flaky", (wait_hours - 1) * HOUR)
            assert await due() == [], (misses, wait_hours)
            await age(index, "flaky", 2 * HOUR)
            assert await due() == ["flaky"], (misses, wait_hours)
        # It answers with US postings: the misses are forgotten and it is refreshed daily like every live board.
        await index.catalog_touch([(g, "flaky")])
        await index.record_board(g, "flaky", True, 12, "catalog")
        await index.catalog_result(g, "flaky", 9)
        healed = await row(index, "flaky")
        assert (healed["misses"], healed["us_count"], healed["job_count"]) == (0, 9, 12)
        assert await due() == []
        await age(index, "flaky", 23 * HOUR)
        assert await due() == []
        await age(index, "flaky", 2 * HOUR)
        assert await due() == ["flaky"]

        # A board with postings but none in the US: 7 days, then 14, then 28.
        await index.pool.execute("DELETE FROM index_boards")
        await index.register_catalog([(g, "abroad")])
        for misses, wait_days in ((1, 7), (2, 14), (3, 28)):
            await index.catalog_touch([(g, "abroad")])
            await index.record_board(g, "abroad", True, 20, "catalog")
            await index.catalog_result(g, "abroad", 0)
            assert ((await row(index, "abroad"))["misses"], (await row(index, "abroad"))["us_count"]) == (misses, 0)
            await age(index, "abroad", (wait_days - 1) * DAY)
            assert await due() == [], (misses, wait_days)
            await age(index, "abroad", 2 * DAY)
            assert await due() == ["abroad"], (misses, wait_days)
        assert await index.known_boards() == [(g, "abroad")]                    # searches still know it: it does have postings

        # A dead board (404) is left alone for 30 days, then 60; no wait grows beyond 90 days.
        await index.pool.execute("DELETE FROM index_boards")
        await index.register_catalog([(g, "gone")])
        for misses, wait_days in ((1, 30), (2, 60), (3, 90), (9, 90)):
            await index.catalog_touch([(g, "gone")])
            await index.record_board(g, "gone", False, 0, "catalog")
            await index.catalog_result(g, "gone", None)
            await index.pool.execute("UPDATE index_boards SET misses=$1 WHERE slug='gone'", misses)
            await age(index, "gone", (wait_days - 1) * DAY)
            assert await due() == [], (misses, wait_days)
            await age(index, "gone", 2 * DAY)
            assert await due() == ["gone"], (misses, wait_days)
        await index.pool.execute("UPDATE index_boards SET misses=1000 WHERE slug='gone'")      # never overflows
        await index.catalog_result(g, "gone", None)
        assert (await row(index, "gone"))["misses"] == 1000 and await due() == ["gone"]

        # Boards read by the previous release keep their old meaning: an empty one is re-checked after 7 days.
        await index.pool.execute("DELETE FROM index_boards")
        await put(index, g, "old-empty", jobs=0, checked=6 * DAY)
        assert await due() == []
        await age(index, "old-empty", 2 * DAY)
        assert await due() == ["old-empty"]
        # A read that was started and never finished (the process stopped) is tried again after 6 hours.
        await index.pool.execute("DELETE FROM index_boards")
        await put(index, g, "interrupted", tried=5 * HOUR)
        assert await due() == []
        await index.pool.execute("UPDATE index_boards SET tried_at = tried_at - $1", 2 * HOUR)
        assert await due() == ["interrupted"]
        await index.catalog_result(g, "no-such-board", 3)                       # a board the index does not know: nothing happens
    finally:
        await index.close()


async def test_health_counts_workable_accounts_and_an_attempt_is_not_a_read(world):
    admin = await world.as_admin()
    index = world.engine.index

    async def health():
        ats_health.invalidate(world.app.state)
        response = await admin.get("/api/integrations/ats")
        assert response.status_code == 200, response.text
        return {p["id"]: p for p in response.json()["platforms"]}

    # Production before the fix: thousands of catalogue accounts, none ever read, no cross-company read either.
    await index.register_catalog([("workable", "acme"), ("workable", "beta"), ("workable", "gone")])
    w = (await health())["workable"]
    assert w["state"] == "configured_unverified" and (w["boards_known"], w["boards_read_ok"], w["open_postings"]) == (3, 0, 0)
    assert "3 boards known, none read successfully yet" in w["reason"]
    # The trickle starts a read: an attempt alone proves nothing.
    await index.catalog_touch([("workable", "acme"), ("workable", "gone")])
    assert (await health())["workable"]["state"] == "configured_unverified"
    # One account answers with postings, one does not exist.
    await index.replace_unit("workable", "acme", "", [("workable:AAAA1111", "AI Engineer", "ai engineer", "", b"x"),
                                                      ("workable:BBBB2222", "ML Engineer", "ml engineer", "", b"x")], 3600)
    await index.record_board("workable", "acme", True, 2, "catalog")
    await index.catalog_result("workable", "acme", 2)
    await index.record_board("workable", "gone", False, 0, "catalog")
    await index.fail_unit("workable", "gone", "", "HTTP 404 from apply.workable.com")
    await index.catalog_result("workable", "gone", None)
    w = (await health())["workable"]
    assert w["state"] == "verified_working" and "1 board read successfully with postings" in w["reason"]
    assert (w["boards_known"], w["boards_read_ok"], w["boards_failed"], w["open_postings"]) == (3, 1, 1, 2)
    assert w["docs"] == "https://workable.readme.io/reference/jobs-1" and w["reader_enabled"] is True
    assert await index.known_boards() == [("workable", "acme")]                 # searches plan the account from now on

    # A live board whose refresh is only attempted keeps the time of its last answer.
    conn = await asyncpg.connect(PLAIN_URL)
    try:
        await conn.execute("UPDATE index_boards SET checked_at = $1 WHERE ats='workable' AND slug='acme'", time.time() - 8 * 86400)
        await conn.execute("UPDATE index_units SET fetched_at = $1, expires_at = $1 WHERE src='workable' AND key='acme'", time.time() - 8 * 86400)
        await index.catalog_touch([("workable", "acme")])
        stale = (await health())["workable"]
        assert stale["boards_read_ok"] == 0 and stale["state"] != "verified_working"       # the old cause of a false "verified"
    finally:
        await conn.close()


async def test_one_workable_vacancy_is_stored_once_whichever_reader_found_it_first(world):
    """Across searches: the job board's listing and the employer account's posting are one stored posting."""
    from sqlalchemy import func, select

    from app.portal.models import JobPosting
    from app.portal.services import jobs as jobs_service
    from app.schemas import Job
    common = dict(title="Data Engineer", company="Acme Inc", location="Denver, Colorado, United States", source="Workable",
                  sources=["Workable"], ats="workable", country_hint="United States")
    listing = Job(url="https://jobs.workable.com/view/abc123/data-engineer-in-denver-at-acme-inc", ats_id="", board="*",
                  verification="Listed in Workable's live job search API", **common)
    posting = Job(url="https://apply.workable.com/j/AAAA1111", apply_url="https://apply.workable.com/j/AAAA1111/apply", ats_id="AAAA1111",
                  board="acme", verification="Listed in the employer's live Workable careers API", **common)
    other = posting.model_copy(update={"ats_id": "BBBB2222", "url": "https://apply.workable.com/j/BBBB2222",
                                       "apply_url": "https://apply.workable.com/j/BBBB2222/apply"})
    for first, second in ((listing, posting), (posting, listing)):
        async with world.sessions() as db:
            a = await jobs_service.upsert_engine_job(db, first)
            b = await jobs_service.upsert_engine_job(db, second)
            again = await jobs_service.upsert_engine_job(db, listing)
            assert a.id == b.id == again.id and b.apply_url == "https://apply.workable.com/j/AAAA1111/apply"
            assert (await db.execute(select(func.count()).select_from(JobPosting))).scalar_one() == 1
            await db.rollback()
    # A second requisition of the same employer with the same title and place stays a posting of its own.
    async with world.sessions() as db:
        a = await jobs_service.upsert_engine_job(db, posting)
        c = await jobs_service.upsert_engine_job(db, other)
        assert c.id != a.id and (await db.execute(select(func.count()).select_from(JobPosting))).scalar_one() == 2
        await db.rollback()


async def test_health_reports_rippling_as_not_supported_with_the_reason(world):
    admin = await world.as_admin()
    # Even with boards and postings in the index from the time the reader was on.
    await world.engine.index.record_board("rippling", "acme-co", True, 7, "catalog")
    ats_health.invalidate(world.app.state)
    found = {p["id"]: p for p in (await admin.get("/api/integrations/ats")).json()["platforms"]}
    rippling = found["rippling"]
    assert rippling["state"] == "unsupported" and rippling["reader_enabled"] is False
    assert rippling["reason"] == "Its reader is switched off in the server settings."
    assert "switched off until Rippling's terms are reviewed" in rippling["note"] and "RIPPLING_ENABLED=1" in rippling["note"]
    assert rippling["note"] == ats_platforms.PLATFORMS["rippling"].note and rippling["startup"] is True
    # Oracle and Pinpoint are off exactly as before; Recruitee stays on; all 21 platforms are listed.
    assert found["pinpoint"]["state"] == "unsupported" and found["pinpoint"]["reader_enabled"] is False
    assert found["oracle"]["state"] == "public_discovery_only" and found["oracle"]["reader_enabled"] is False
    assert found["recruitee"]["state"] == "configured_unverified" and found["recruitee"]["reader_enabled"] is True
    assert len(found) == 21


async def test_remembered_company_pages_are_kept_current_by_the_background_reader(world):
    """A posting verified on an employer's own site is recorded like a catalogue board: known to searches while it
    lists a posting, read again by the background reader when a day old, dropped from the known boards when gone."""
    import time

    from app.providers.registry import CAREER_PAGE
    index = world.engine.index
    live, gone = "https://careers.example.com/jobs/ai-9", "https://careers.example.com/jobs/old-1"
    for url in (live, gone):
        await index.record_board(CAREER_PAGE, url, True, 1, "catalog")
        await index.catalog_result(CAREER_PAGE, url, 1)
    assert {(CAREER_PAGE, live), (CAREER_PAGE, gone)} <= set(await index.known_boards())
    assert await index.catalog_due({CAREER_PAGE: 5}) == []                                   # just read: not due
    await index.pool.execute("UPDATE index_boards SET checked_at = $1, tried_at = NULL WHERE ats = $2", time.time() - 2 * 86400, CAREER_PAGE)
    assert sorted(await index.catalog_due({CAREER_PAGE: 5})) == sorted([(CAREER_PAGE, gone), (CAREER_PAGE, live)])
    await index.record_board(CAREER_PAGE, gone, True, 0, "catalog")                           # its page no longer proves a posting
    await index.catalog_result(CAREER_PAGE, gone, 0)
    known = set(await index.known_boards())
    assert (CAREER_PAGE, live) in known and (CAREER_PAGE, gone) not in known
