"""AWS web search in the portal: the PostgreSQL monthly counter (coroutines and processes at once, reserve, month
rollover), its Alembic revision, POST /api/web-search/jobs (permissions, CSRF, validation, per-user limits,
verification against the employer feed, no AWS data stored) and the integrations status. AWS is never called."""
import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone

import asyncpg
import httpx

from app.portal.db import make_engine
from app.providers.aws_web_search import AwsWebSearch, PgQuota
from tests import test_wider_search as wide
from tests.portal.conftest import PLAIN_URL, ROOT, TEST_URL
from tests.test_aws_web_search import ATS_URLS, MARK, FakeAws

FUTURE = datetime(2099, 1, 15, tzinfo=timezone.utc).timestamp()          # a month of its own: no other test touches it


async def pool():
    return await asyncpg.create_pool(PLAIN_URL, min_size=1, max_size=10)


async def test_pg_counter_never_passes_the_limit_under_concurrency_and_keeps_the_reserve(database):
    db = await pool()
    try:
        await db.execute("DELETE FROM aws_web_search_usage WHERE month = '2099-01'")
        quota = PgQuota(db, limit=50, reserve=5, clock=lambda: FUTURE)
        granted = await asyncio.gather(*(quota.reserve(interactive=False) for _ in range(200)))
        assert sum(granted) == 45 and await quota.used() == 45                         # background: limit - reserve
        assert sum(await asyncio.gather(*(quota.reserve(interactive=True) for _ in range(30)))) == 5
        assert await quota.used() == 50 and (await quota.usage())["remaining"] == 0
        await quota.release()
        assert await quota.used() == 49
        # The API's SQLAlchemy engine reserves from the same row.
        engine = make_engine(TEST_URL, pool_size=2)
        try:
            api = PgQuota(engine, limit=50, reserve=5, clock=lambda: FUTURE)
            assert await api.reserve(interactive=True) and not await api.reserve(interactive=True) and await api.used() == 50
        finally:
            await engine.dispose()
        # Month rollover: a new month starts at zero; the old row stays as it was.
        march = PgQuota(db, limit=50, clock=lambda: datetime(2099, 3, 1, tzinfo=timezone.utc).timestamp())
        await db.execute("DELETE FROM aws_web_search_usage WHERE month = '2099-03'")
        assert await march.used() == 0 and await march.reserve() and await march.used() == 1 and await quota.used() == 50
        row = await db.fetchrow("SELECT * FROM aws_web_search_usage WHERE month = '2099-01'")
        assert set(row.keys()) == {"month", "used", "updated_at"}                      # a number only: no query, no result
    finally:
        await db.execute("DELETE FROM aws_web_search_usage WHERE month LIKE '2099-%'")
        await db.close()


WORKER = """
import asyncio, sys, asyncpg
from app.providers.aws_web_search import PgQuota
async def main():
    pool = await asyncpg.create_pool(sys.argv[1], min_size=1, max_size=5)
    quota = PgQuota(pool, limit=60, reserve=0, clock=lambda: float(sys.argv[2]))
    got = await asyncio.gather(*(quota.reserve() for _ in range(40)))
    print(sum(got))
    await pool.close()
asyncio.run(main())
"""


async def test_pg_counter_holds_across_processes(database):
    db = await pool()
    try:
        await db.execute("DELETE FROM aws_web_search_usage WHERE month = '2099-01'")
        procs = [await asyncio.create_subprocess_exec(sys.executable, "-c", WORKER, PLAIN_URL, str(FUTURE), cwd=ROOT,
                                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(5)]
        outs = [await p.communicate() for p in procs]
        assert all(p.returncode == 0 for p in procs), [o[1][-500:] for o in outs]
        assert sum(int(o[0]) for o in outs) == 60                                      # 200 attempts, exactly the limit granted
        assert await db.fetchval("SELECT used FROM aws_web_search_usage WHERE month = '2099-01'") == 60
    finally:
        await db.execute("DELETE FROM aws_web_search_usage WHERE month LIKE '2099-%'")
        await db.close()


# ---------------------------------------------------------------- migration
NAME = PLAIN_URL.rsplit("/", 1)[1] + "_aws_migration"
URL, PLAIN = TEST_URL.rsplit("/", 1)[0] + "/" + NAME, PLAIN_URL.rsplit("/", 1)[0] + "/" + NAME


def alembic(*args: str) -> str:
    result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, capture_output=True, text=True,
                            env={**os.environ, "ALEMBIC_DATABASE_URL": URL})
    assert result.returncode == 0, result.stderr[-1500:]
    return result.stdout


async def test_usage_revision_upgrades_and_downgrades_without_touching_data(database):
    admin = await asyncpg.connect(PLAIN_URL.rsplit("/", 1)[0] + "/postgres")
    await admin.execute(f'DROP DATABASE IF EXISTS "{NAME}"')
    await admin.execute(f'CREATE DATABASE "{NAME}"')
    try:
        alembic("upgrade", "e7b31f08a5c2")
        conn = await asyncpg.connect(PLAIN)
        await conn.execute("INSERT INTO teams (id, name) VALUES (gen_random_uuid(), 'Kept')")
        assert not await conn.fetchval("SELECT to_regclass('aws_web_search_usage') IS NOT NULL")
        await conn.close()
        alembic("upgrade", "b5e8d2c47a19")
        assert "b5e8d2c47a19 (head)" in alembic("current")
        conn = await asyncpg.connect(PLAIN)
        await conn.execute("INSERT INTO aws_web_search_usage (month) VALUES ('2026-11')")
        assert await conn.fetchval("SELECT used FROM aws_web_search_usage") == 0
        for bad in ("UPDATE aws_web_search_usage SET used = -1", "INSERT INTO aws_web_search_usage (month) VALUES ('Nov 26')"):
            try:
                await conn.execute(bad)
                raise AssertionError(bad)
            except asyncpg.CheckViolationError:
                pass
        await conn.close()
        alembic("downgrade", "e7b31f08a5c2")
        conn = await asyncpg.connect(PLAIN)
        assert not await conn.fetchval("SELECT to_regclass('aws_web_search_usage') IS NOT NULL")
        assert await conn.fetchval("SELECT count(*) FROM teams") == 1                   # nothing else touched
        await conn.close()
        alembic("upgrade", "head")
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{NAME}" WITH (FORCE)')
        await admin.close()


# ---------------------------------------------------------------- endpoint
def enable(world, fake, limit=4000, **over):
    s = world.settings
    for key, value in {"aws_web_search_enabled": True, "aws_web_search_published_days": 0, "aws_web_search_monthly_limit": limit,
                       "aws_web_search_monthly_reserve": 0, **over}.items():
        setattr(s, key, value)
    world.app.state.aws_search = AwsWebSearch(s, PgQuota(world.app.state.db_engine, s.aws_web_search_monthly_limit,
                                                         s.aws_web_search_monthly_reserve), client=fake)
    world.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(wide.handler))
    world.app.state.verifier.client = world.app.state.http
    world.clients.append(world.app.state.http)


BODY = {"keywords": "AI Engineer", "locations": ["Austin, TX"], "work_mode": "any", "limit": 20}


async def test_endpoint_permissions_csrf_and_validation(world):
    enable(world, FakeAws({"ats": ATS_URLS}))
    member = await world.as_member("a1")
    assert (await member.post("/api/web-search/jobs", json=BODY)).status_code == 403
    tl = await world.as_tl("alpha")
    other = {**BODY, "candidate_id": str(world.ids["cand_b1"])}
    assert (await tl.post("/api/web-search/jobs", json=other)).status_code == 404       # another team's candidate
    no_csrf = await world.as_tl("alpha")
    del no_csrf.headers["x-csrf-token"]
    assert (await no_csrf.post("/api/web-search/jobs", json=BODY)).status_code == 403
    anonymous = await world.client()
    assert (await anonymous.post("/api/web-search/jobs", json=BODY)).status_code == 401
    for bad in ({**BODY, "limit": 51}, {**BODY, "limit": 0}, {**BODY, "locations": ["Berlin, Germany"]},
                {**BODY, "locations": ["TX", "CA", "NY", "WA", "MA", "IL"]}, {**BODY, "work_mode": "office"},
                {**BODY, "keywords": "x"}, {**BODY, "keywords": "<script>"}, {**BODY, "keywords": "https://evil.example"}):
        assert (await tl.post("/api/web-search/jobs", json=bad)).status_code == 422, bad
    assert world.app.state.aws_search.client().params == []                           # nothing was sent for refused requests


async def test_endpoint_returns_only_verified_jobs_and_stores_no_aws_data(world):
    fake = FakeAws({"ats": ATS_URLS, "general": ["https://careers.example.com/search?q=ai", "https://www.indeed.com/viewjob?jk=1"]})
    enable(world, fake)
    tl = await world.as_tl("alpha")
    response = await tl.post("/api/web-search/jobs", json={**BODY, "candidate_id": str(world.ids["cand_a1"]), "work_mode": "any"})
    assert response.status_code == 200, response.text
    out = response.json()
    jobs, stats = out["jobs"], out["stats"]
    assert [j["employer_url"] for j in jobs] == ["https://job-boards.greenhouse.io/newco/jobs/5001"]
    job = jobs[0]
    assert job["discovered_via"] == "aws_web_search" and job["source"] == "Greenhouse" and job["title"] == "AI Engineer 1"
    assert job["company"] and job["location"] and "Greenhouse" in job["verification"] and job["score_source"] == "heuristic"
    assert stats["queries_used"] == len(fake.params) <= world.settings.aws_web_search_endpoint_max_calls
    assert stats["dropped_unverifiable"] >= 1 and stats["dropped_generic"] >= 4 and stats["duplicates"] >= 1
    assert stats["monthly_used"] == stats["queries_used"] and stats["monthly_remaining"] == 4000 - stats["queries_used"]
    for params in fake.params:
        assert params["searchQuery"].startswith("AI Engineer jobs Austin, TX") and len(params.get("filters", {}).get("domainFilter", {})) <= 1
    assert MARK not in response.text
    conn = await asyncpg.connect(PLAIN_URL)
    try:
        tables = [r["tablename"] for r in await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]
        for table in tables:
            rows = await conn.fetch(f'SELECT t::text AS row FROM "{table}" t')
            assert not any(MARK in r["row"] or MARK.encode().hex() in r["row"] for r in rows), table
        assert await conn.fetchval("SELECT count(*) FROM job_postings") == 0 if "job_postings" in tables else True
        assert await conn.fetchval("SELECT count(*) FROM index_postings") == 0                 # nothing indexed by the endpoint
    finally:
        await conn.close()
    admin = await world.as_admin()
    status = (await admin.get("/api/integrations")).json()["aws_web_search"]
    assert status["enabled"] and status["used"] == stats["queries_used"] and status["limit"] == 4000 and status["counter_available"]
    assert status["region"] == "us-east-1" and status["month"] == datetime.now(timezone.utc).strftime("%Y-%m")
    assert any(s["name"] == "aws_web_search" for s in (await admin.get("/api/integrations")).json()["sources"])


async def test_endpoint_drops_a_posting_the_employer_feed_no_longer_lists(world):
    enable(world, FakeAws({"ats": ["https://boards.greenhouse.io/newco/jobs/5999"]}))
    tl = await world.as_tl("alpha")
    out = (await tl.post("/api/web-search/jobs", json=BODY)).json()
    assert out["jobs"] == [] and out["stats"]["dropped_unverifiable"] == 1 and out["stats"]["verified"] == 0


async def test_endpoint_limits_per_user_and_month_and_when_off(world):
    enable(world, FakeAws({"ats": []}), aws_web_search_user_per_minute=2)
    tl = await world.as_tl("alpha")
    assert [(await tl.post("/api/web-search/jobs", json=BODY)).status_code for _ in range(3)] == [200, 200, 429]
    admin = await world.as_admin()
    assert (await admin.post("/api/web-search/jobs", json=BODY)).status_code == 200      # limits are per user
    used = world.app.state.aws_search.calls
    enable(world, FakeAws({"ats": []}), limit=used, aws_web_search_user_per_minute=20)  # the month is used up
    assert (await admin.post("/api/web-search/jobs", json=BODY)).status_code == 429
    world.settings.aws_web_search_enabled = False
    assert (await admin.post("/api/web-search/jobs", json=BODY)).status_code == 503
    status = (await admin.get("/api/integrations")).json()["aws_web_search"]
    assert status["enabled"] is False and status["used"] == used
