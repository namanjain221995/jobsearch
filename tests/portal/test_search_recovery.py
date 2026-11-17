"""A search survives its worker: a run interrupted by a worker stop, crash or timeout is resumed once, members
that were already finished are kept, and a second interruption closes the run honestly."""
import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import delete, func, select, update

from app.portal.models import ApplicationEvent, AuditLog, JobAssignment, Notification, SearchResult, SearchRun, SearchTarget
from app.portal.search_runner import MAX_ATTEMPTS, WAITING, recover_interrupted
from app.portal.security import utcnow
from tests.portal.test_search_flow import wait_done


async def until_running(client, run_id) -> None:
    for _ in range(100):
        if (await client.get(f"/api/searches/{run_id}")).json()["status"] == "running":
            return
        await asyncio.sleep(0.05)
    raise AssertionError("search did not start")


async def test_run_left_by_a_dead_worker_resumes_once_and_keeps_finished_members(world):
    tl = await world.as_tl("alpha")
    a1, a2 = world.ids["cand_a1"], world.ids["cand_a2"]
    started = await tl.post("/api/searches", json={"candidate_ids": [str(a1), str(a2)], "target_count": 4, "deep_search": False})
    run_id = uuid.UUID(started.json()["search"]["id"])
    run = await wait_done(world, tl, run_id)
    assert run["status"] == "completed" and run["attempts"] == 1 and run["assigned_count"] == 8
    async with world.sessions() as db:
        kept = set((await db.execute(select(JobAssignment.id).where(JobAssignment.candidate_id ==a1))).scalars())
        # The state a crash leaves behind: A1 was saved, A2 was still being searched, nobody closed the run.
        lost = select(JobAssignment.id).where(JobAssignment.candidate_id ==a2)
        await db.execute(delete(ApplicationEvent).where(ApplicationEvent.assignment_id.in_(lost)))
        await db.execute(delete(JobAssignment).where(JobAssignment.candidate_id ==a2))
        await db.execute(delete(SearchResult).where(SearchResult.candidate_id ==a2))
        await db.execute(update(SearchTarget).where(SearchTarget.search_run_id == run_id, SearchTarget.candidate_id ==a2).values(status="running", stats={}))
        progress = (await db.execute(select(SearchRun.progress).where(SearchRun.id == run_id))).scalar_one()
        progress["candidates"] = [{**m, "status": "running", "stage": "Ranking matches"} if m["candidate_id"] == str(a2) else m for m in progress["candidates"]]
        await db.execute(update(SearchRun).where(SearchRun.id == run_id).values(
            status="running", completed_at=None, heartbeat_at=utcnow() - timedelta(minutes=10), progress=progress))
        await db.commit()
    assert len(kept) == 4

    searched = []
    original = world.engine.run

    async def counted(state, request, resume):
        searched.append(state.id)
        return await original(state, request, resume)

    world.engine.run = counted
    assert await recover_interrupted(world.sessions, exclude=frozenset({str(run_id)})) == ([], 0)    # executing in this worker: left alone
    assert await recover_interrupted(world.sessions, stale_seconds=3600) == ([], 0)                  # heartbeat still fresh enough
    assert await recover_interrupted(world.sessions) == ([run_id], 0)
    waiting = (await tl.get(f"/api/searches/{run_id}")).json()
    assert waiting["status"] == "queued" and waiting["stage"] == WAITING and waiting["completed_at"] is None
    assert {t["candidate"]["name"]: t["status"] for t in waiting["targets"]} == {"Candidate A1": "completed", "Candidate A2": "queued"}

    await world.queue.runner(run_id)                                                                 # the next worker picks it up
    done = (await tl.get(f"/api/searches/{run_id}")).json()
    assert done["status"] == "completed" and done["attempts"] == 2 and done["matched_count"] == 8 and done["assigned_count"] == 8
    assert searched == [f"{run_id}:{a2}"]                                                            # only the unfinished member
    assert any("resumed automatically" in w for w in done["warnings"])
    assert {t["candidate"]["name"]: (t["status"], t["assigned"]) for t in done["targets"]} == {"Candidate A1": ("completed", 4), "Candidate A2": ("completed", 4)}
    async with world.sessions() as db:
        assert set((await db.execute(select(JobAssignment.id).where(JobAssignment.candidate_id ==a1))).scalars()) == kept    # untouched, no duplicates
        assert (await db.execute(select(func.count()).select_from(JobAssignment).where(JobAssignment.candidate_id ==a2))).scalar_one() == 4
        # The same thing happens again: a run is resumed once only.
        progress = (await db.execute(select(SearchRun.progress).where(SearchRun.id == run_id))).scalar_one()
        progress["candidates"] = [{**m, "status": "running"} if m["candidate_id"] == str(a2) else m for m in progress["candidates"]]
        await db.execute(update(SearchTarget).where(SearchTarget.search_run_id == run_id, SearchTarget.candidate_id ==a2).values(status="running"))
        await db.execute(update(SearchRun).where(SearchRun.id == run_id).values(status="running", completed_at=None, heartbeat_at=None, progress=progress))
        await db.commit()
    assert await recover_interrupted(world.sessions) == ([], 1)
    closed = (await tl.get(f"/api/searches/{run_id}")).json()
    assert closed["status"] == "partial" and "twice" in closed["error_summary"] and closed["completed_at"]
    assert {t["candidate"]["name"]: t["status"] for t in closed["targets"]} == {"Candidate A1": "completed", "Candidate A2": "failed"}
    assert (await (await world.as_member("a1")).get("/api/assignments")).json()["total"] == 4


async def test_worker_shutdown_puts_the_run_back_in_the_queue_then_gives_up_the_second_time(world):
    tl = await world.as_tl("alpha")
    a1 = world.ids["cand_a1"]
    gate = asyncio.Event()
    original = world.engine.run

    async def slow(state, request, resume):
        await gate.wait()
        return await original(state, request, resume)

    world.engine.run = slow
    started = await tl.post("/api/searches", json={"candidate_ids": [str(a1)], "target_count": 3, "deep_search": False})
    run_id = uuid.UUID(started.json()["search"]["id"])
    await until_running(tl, run_id)
    for task in list(world.queue.tasks):                     # the worker process is told to stop: its jobs are cancelled
        task.cancel()
    await world.queue.drain()
    run = (await tl.get(f"/api/searches/{run_id}")).json()
    assert run["status"] == "queued" and run["attempts"] == 1 and run["completed_at"] is None and run["error_summary"] == ""
    assert run["targets"][0]["status"] == "queued" and run["targets"][0]["message"] == WAITING
    async with world.sessions() as db:                       # nobody is told "finished" for a run that is not
        assert (await db.execute(select(func.count()).select_from(Notification).where(Notification.type == "search_finished"))).scalar_one() == 0
        assert (await db.execute(select(AuditLog.meta).where(AuditLog.action == "search.interrupted"))).scalar_one()["will_resume"] is True

    second = asyncio.create_task(world.queue.runner(run_id))    # the restarted worker takes it again ...
    await until_running(tl, run_id)
    second.cancel()                                             # ... and is stopped again
    await asyncio.gather(second, return_exceptions=True)
    run = (await tl.get(f"/api/searches/{run_id}")).json()
    assert run["status"] == "failed" and run["attempts"] == MAX_ATTEMPTS == 2 and "twice" in run["error_summary"]
    assert run["targets"][0]["status"] == "failed" and run["completed_at"]
    async with world.sessions() as db:
        assert (await db.execute(select(func.count()).select_from(Notification).where(Notification.type == "search_finished"))).scalar_one() == 1
        assert (await db.execute(select(func.count()).select_from(JobAssignment))).scalar_one() == 0
    gate.set()
    await world.queue.runner(run_id)                            # delivered once more by mistake: nothing happens
    assert (await tl.get(f"/api/searches/{run_id}")).json()["status"] == "failed"


async def test_resumed_run_finishes_normally_after_one_shutdown(world):
    tl = await world.as_tl("alpha")
    a1 = world.ids["cand_a1"]
    gate = asyncio.Event()
    original = world.engine.run

    async def slow(state, request, resume):
        await gate.wait()
        return await original(state, request, resume)

    world.engine.run = slow
    started = await tl.post("/api/searches", json={"candidate_ids": [str(a1)], "target_count": 3, "deep_search": False})
    run_id = uuid.UUID(started.json()["search"]["id"])
    await until_running(tl, run_id)
    for task in list(world.queue.tasks):
        task.cancel()
    await world.queue.drain()
    assert (await tl.get(f"/api/searches/{run_id}")).json()["status"] == "queued"
    gate.set()
    await world.queue.runner(run_id)
    run = (await tl.get(f"/api/searches/{run_id}")).json()
    assert run["status"] == "completed" and run["attempts"] == 2 and run["assigned_count"] == 3 and run["error_summary"] == ""
    assert (await (await world.as_member("a1")).get("/api/assignments")).json()["total"] == 3
    # A person cancelling a run that waits for its second attempt wins over the resume.
    assert (await tl.post(f"/api/searches/{run_id}/cancel")).json()["status"] == "completed"       # finished runs are left alone
