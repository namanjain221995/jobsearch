"""Analytics cache (Redis) and structured request logs. Uses Redis database 15 on the local test server."""
import json
import logging
import uuid

import pytest
import redis.asyncio as redis_asyncio
from sqlalchemy import select, update

from app.portal import cache as cache_module
from app.portal.cache import AnalyticsCache
from app.portal.db import keep_analytics_cache, make_sessionmaker
from app.portal.logs import JsonFormatter
from app.portal.models import Candidate, CandidateProfile, JobAssignment, SearchRun, Session
from app.portal.schemas import Message
from tests.portal.conftest import add_job

REDIS_URL = "redis://127.0.0.1:56379/15"


@pytest.fixture
async def cached(world):
    """The same seven accounts as every portal test, with the analytics cache switched on."""
    client = redis_asyncio.from_url(REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    try:
        await client.flushdb()
    except Exception:
        await client.aclose()
        pytest.skip("Redis is not reachable on 127.0.0.1:56379")
    cache = AnalyticsCache.for_database(client, world.settings.database_url, 60)
    world.app.state.cache = cache
    cache_module.install(cache)
    yield cache
    cache_module.install(None)
    await client.flushdb()
    await client.aclose()


async def assign(world, candidate: str, title: str = "AI Engineer") -> str:
    """Assign a new job to the candidate with that code ("a1" = Candidate A1, whose responsible member is Member A1)."""
    admin = await world.as_admin()
    job_id = await add_job(world, title=title)
    response = await admin.post("/api/assignments", json={"job_id": job_id, "candidate_id": str(world.ids[f"cand_{candidate}"])})
    assert response.status_code == 201, response.text
    return response.json()["assignment_ids"][0]


async def test_second_identical_request_is_served_from_redis(world, cached):
    admin = await world.as_admin()
    first = (await admin.get("/api/analytics/admin")).json()
    assert (cached.hits, cached.misses) == (0, 1)
    assert (await admin.get("/api/analytics/admin")).json() == first
    assert (cached.hits, cached.misses) == (1, 1)
    await admin.get("/api/analytics/admin", params={"date_from": "2020-01-01"})          # another range: another entry
    assert (cached.hits, cached.misses) == (1, 2)
    await admin.get("/api/analytics/admin", params={"company": "acme"})                  # free text: never stored
    assert (cached.hits, cached.misses) == (1, 2)
    team, lead = str(world.ids["team_alpha"]), await world.as_tl("alpha")                # signing in changes "last sign-in": entries retire
    one = (await admin.get(f"/api/teams/{team}/analytics")).json()
    assert (await lead.get(f"/api/teams/{team}/analytics")).json() == one                 # the team's own lead reads the same entry
    assert cached.hits == 2


async def test_a_status_change_is_visible_on_the_very_next_dashboard_read(world, cached):
    assignment_id = await assign(world, "a1")
    admin, lead, member = await world.as_admin(), await world.as_tl("alpha"), await world.as_member("a1")
    team = str(world.ids["team_alpha"])
    before_admin = (await admin.get("/api/analytics/admin")).json()["stats"]
    before_team = (await lead.get(f"/api/teams/{team}/analytics")).json()["stats"]
    assert before_admin["assigned"] == before_team["assigned"] == 1 and before_admin["applied"] == 0
    own = f"/api/users/{world.ids['member_a1']}/dashboard"                                # totals over the member's candidates
    before_member = (await member.get(own)).json()["stats"]
    assert before_member["assigned"] == 1 and before_member["applied"] == 0
    await admin.get("/api/analytics/admin")                                               # both answers are now stored
    hits = cached.hits
    assert hits >= 1
    assert (await member.patch(f"/api/assignments/{assignment_id}/status", json={"status": "APPLIED"})).status_code == 200
    after_admin = (await admin.get("/api/analytics/admin")).json()["stats"]
    after_team = (await lead.get(f"/api/teams/{team}/analytics")).json()["stats"]
    assert after_admin["applied"] == after_team["applied"] == 1                           # no stale answer
    assert (await member.get(own)).json()["stats"]["applied"] == 1
    assert cached.hits == hits                                                            # both were recomputed
    # A member only opening the assignment (viewed_at is set by a GET) also retires the stored answers.
    second = await assign(world, "a1", title="ML Engineer")
    await admin.get("/api/analytics/admin")
    bumps = cached.bumps
    assert (await member.get(f"/api/assignments/{second}")).status_code == 200
    assert cached.bumps == bumps + 1
    assert (await admin.get("/api/analytics/admin")).json()["stats"]["viewed"] >= 1


async def test_a_stored_answer_is_never_a_way_around_authorization(world, cached):
    admin = await world.as_admin()
    alpha, beta = str(world.ids["team_alpha"]), str(world.ids["team_beta"])
    assert (await admin.get(f"/api/teams/{beta}/analytics")).status_code == 200          # stored now
    lead_alpha, member = await world.as_tl("alpha"), await world.as_member("b1")
    assert (await lead_alpha.get(f"/api/teams/{beta}/analytics")).status_code == 403
    assert (await member.get(f"/api/teams/{beta}/analytics")).status_code == 403
    assert (await member.get("/api/analytics/admin")).status_code == 403
    assert (await lead_alpha.get("/api/analytics/admin")).status_code == 403
    assert (await lead_alpha.get(f"/api/teams/{alpha}/analytics")).status_code == 200


async def test_only_commits_that_touch_dashboard_tables_retire_entries(world, cached):
    sessions = make_sessionmaker(world.app.state.db_engine)                               # e.g. the worker's own sessions
    assignment_id = uuid.UUID(await assign(world, "b1"))
    bumps = cached.bumps
    async with sessions() as db:                                                          # a read commits nothing relevant
        await db.execute(select(JobAssignment.id))
        await db.commit()
    async with sessions() as db:                                                          # a table the dashboards never read
        await db.execute(update(Session).values(last_seen_at=Session.last_seen_at))
        await db.commit()
    async with sessions() as db:                                                          # rolled back: nothing changed
        await db.execute(update(JobAssignment).where(JobAssignment.id == assignment_id).values(status="APPLYING"))
        await db.rollback()
        await db.commit()
    async with sessions() as db:                                                          # heartbeat explicitly kept out
        await db.execute(update(SearchRun).values(heartbeat_at=None))
        keep_analytics_cache(db)
        await db.commit()
    assert cached.bumps == bumps
    async with sessions() as db:                                                          # Core statement
        await db.execute(update(JobAssignment).where(JobAssignment.id == assignment_id).values(status="APPLYING"))
        await db.commit()
    assert cached.bumps == bumps + 1
    async with sessions() as db:                                                          # ORM object
        row = await db.get(JobAssignment, assignment_id)
        row.status = "APPLIED"
        await db.commit()
    assert cached.bumps == bumps + 2
    candidate = world.ids["cand_b1"]                                                      # candidates and their profiles feed the dashboards too
    async with sessions() as db:
        await db.execute(update(Candidate).where(Candidate.id == candidate).values(assigned_member_id=None))
        await db.commit()
    assert cached.bumps == bumps + 3
    async with sessions() as db:
        await db.execute(update(CandidateProfile).where(CandidateProfile.candidate_id == candidate).values(years_experience=4))
        await db.commit()
    assert cached.bumps == bumps + 4


async def test_redis_failure_falls_back_to_live_numbers(world, cached):
    admin = await world.as_admin()
    assert (await admin.get("/api/analytics/admin")).status_code == 200                   # stored
    working = cached.redis
    broken = redis_asyncio.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2, socket_timeout=0.2)
    cached.redis = broken
    try:
        response = await admin.get("/api/analytics/admin")
        assert response.status_code == 200 and response.json()["stats"]["assigned"] == 0
        assert cached.errors == 1
        await assign(world, "a2")                                                         # its invalidation cannot be sent: no error for the user
        assert (await admin.get("/api/analytics/admin")).json()["stats"]["assigned"] == 1
        assert cached.errors == 1                                                         # while distrusted, Redis is not asked again
    finally:
        await broken.aclose()
    # Redis is back and the distrust period is over: the invalidation that was owed goes out before any read,
    # so the answer stored before the outage (assigned = 0) is not served.
    cached.redis, cached._distrust_until = working, 0.0
    bumps = cached.bumps
    assert (await admin.get("/api/analytics/admin")).json()["stats"]["assigned"] == 1
    assert cached.bumps == bumps + 1 and cached.hits == 0


async def test_disabled_cache_computes_every_time():
    calls = []

    async def compute() -> Message:
        calls.append(1)
        return Message(message="live")

    for cache in (AnalyticsCache(None, 60), AnalyticsCache(object(), 0)):                 # no Redis; switched off
        assert (await cache.cached("kind", {}, Message, compute)).message == "live"
        assert (await cache.cached("kind", {}, Message, compute)).message == "live"
        assert cache.hits == 0
    assert len(calls) == 4
    await AnalyticsCache(None, 60).bump()                                                 # nothing to do, nothing raised


async def test_request_log_is_structured_and_never_holds_tokens_or_ids(world, caplog):
    logger = logging.getLogger("jobfinder")
    logger.propagate = True                                                               # let pytest see the records
    try:
        with caplog.at_level(logging.DEBUG, logger="jobfinder.request"):
            admin = await world.as_admin()
            response = await admin.get(f"/api/teams/{world.ids['team_alpha']}")
            secret = "s3cr3t-token-value-that-must-not-be-logged"
            await admin.get("/api/invitations/validate", params={"token": secret})
            await admin.get("/api/health")
    finally:
        logger.propagate = False
    records = [r for r in caplog.records if r.name == "jobfinder.request"]
    by_route = {r.route: r for r in records}
    team = by_route["/api/teams/{team_id}"]
    assert team.status == 200 and team.method == "GET" and team.role == "ADMIN" and team.user_id == str(world.ids["admin"])
    assert team.duration_ms >= 0 and response.headers["x-request-id"] == team.request_id
    assert by_route["/api/invitations/validate"].status == 404 and by_route["/api/invitations/validate"].user_id is None
    assert by_route["/api/health"].levelno == logging.DEBUG                               # probes do not flood the log
    lines = [JsonFormatter().format(r) for r in records]
    assert all(json.loads(line)["message"] == "request" for line in lines)
    joined = "\n".join(lines)
    assert secret not in joined and str(world.ids["team_alpha"]) not in joined and "Correct-Horse" not in joined
    assert {"time", "level", "logger", "message", "request_id", "method", "route", "status", "duration_ms", "user_id", "role"} <= set(json.loads(lines[0]))
