"""Team searches end to end: persistent run, background execution with the real engine, progress, auto-assignment,
review mode, cancellation and isolation. Only the network is mocked (synthetic postings from tests/conftest.py)."""
import asyncio
import uuid

from sqlalchemy import select, update

from app.portal.models import JobAssignment, JobPosting, SearchResult, SearchRun
from app.portal.search_runner import recover_interrupted
from app.schemas import Candidate
from tests.conftest import CALLS

GH = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


async def wait_done(world, client, run_id, timeout=60):
    await world.queue.drain()
    for _ in range(int(timeout * 10)):
        run = (await client.get(f"/api/searches/{run_id}")).json()
        if run["status"] not in ("queued", "running"):
            return run
        await asyncio.sleep(0.1)
    raise AssertionError("search did not finish")


async def test_tl_search_auto_assigns_only_to_selected_members(world):
    tl = await world.as_tl("alpha")
    a1, a2 = str(world.ids["cand_a1"]), str(world.ids["cand_a2"])
    started = await tl.post("/api/searches", json={"candidate_ids": [a1], "target_count": 10, "deep_search": False})
    assert started.status_code == 202, started.text                    # acknowledged immediately with an id
    body = started.json()
    run_id = body["search"]["id"]
    assert body["search"]["status"] in ("queued", "running") and body["skipped"] == [] and body["search"]["candidate_count"] == 1
    run = await wait_done(world, tl, run_id)
    assert run["status"] == "completed" and run["matched_count"] == 10 and run["assigned_count"] == 10
    assert run["discovered_count"] == 24 and run["verified_count"] >= 10 and run["targets"][0]["matched"] == 10
    assert run["sources"]["Greenhouse"]["postings"] == 22 and run["sources"]["Ashby"]["boards_failed"] == 1     # real per-source counters
    assert any("Ashby" in w for w in run["warnings"])

    member = await world.as_member("a1")
    mine = (await member.get("/api/assignments", params={"page_size": 50})).json()
    assert mine["total"] == 10 and all(a["source"] == "search" and a["match_score"] >= 50 for a in mine["items"])
    assert all(a["job"]["verification_status"] == "verified" and a["job"]["country"] == "United States" for a in mine["items"])
    assert not any("Toronto" in a["job"]["location_text"] or "London" in a["job"]["location_text"] for a in mine["items"])
    scores = [a["match_score"] for a in mine["items"]]
    assert scores == sorted(scores, reverse=True)                      # one batch: best match first, stable order
    acme = next(a for a in mine["items"] if a["job"]["company"] == "Acme AI")
    detail = (await member.get(f"/api/assignments/{acme['id']}")).json()
    assert detail["match"]["score_breakdown"] and detail["match"]["matching_skills"] and detail["assigned_by"]["name"] == "Lead Alpha"
    assert "PyTorch" in detail["job"]["description"]
    assert (await (await world.as_member("a2")).get("/api/assignments")).json()["total"] == 0        # not selected: gets nothing
    assert (await (await world.as_member("b1")).get("/api/assignments")).json()["total"] == 0
    note = (await member.get("/api/notifications")).json()["items"][0]
    assert note["type"] == "candidate_jobs_assigned" and note["payload"]["count"] == 10
    assert note["payload"]["candidate_id"] == a1 and note["payload"]["candidate_name"] == "Candidate A1"
    assert all(a["candidate"]["name"] == "Candidate A1" and a["member"]["name"] == "Member A1" for a in mine["items"])
    assert (await tl.get("/api/notifications")).json()["items"][0]["type"] == "search_finished"

    # Searching again never duplicates an assignment; the same job may go to another qualifying member.
    again = await tl.post("/api/searches", json={"candidate_ids": [a1, a2], "target_count": 10, "deep_search": False})
    second = await wait_done(world, tl, again.json()["search"]["id"])
    per_candidate = {t["candidate"]["name"]: t for t in second["targets"]}
    assert per_candidate["Candidate A1"]["assigned"] == 0 and per_candidate["Candidate A2"]["assigned"] == 10 and second["assigned_count"] == 10
    assert CALLS[GH] == 1                                              # second run reused the index: no refetch
    async with world.sessions() as db:
        rows = (await db.execute(select(JobAssignment.job_id, JobAssignment.candidate_id))).all()
        assert len(rows) == 20 == len(set(rows))
        assert (await db.execute(select(JobPosting))).scalars().all().__len__() == 10                # one global row per vacancy
    team = (await tl.get(f"/api/teams/{world.ids['team_alpha']}/analytics")).json()
    assert team["stats"]["assigned"] == 20 and team["active_searches"] == 0
    history = (await tl.get("/api/searches")).json()
    assert history["total"] == 2 and {s["status"] for s in history["items"]} == {"completed"}


async def test_search_scope_and_validation(world):
    tl = await world.as_tl("alpha")
    b1 = str(world.ids["cand_b1"])
    assert (await tl.post("/api/searches", json={"candidate_ids": [b1]})).status_code == 403                 # other team's member
    assert (await tl.post("/api/searches", json={"team_id": str(world.ids["team_beta"])})).status_code == 403
    assert (await tl.post("/api/searches", json={"candidate_ids": []})).status_code == 422
    assert (await tl.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a1"])], "target_count": 0})).status_code == 422
    too_many = await tl.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a1"]), str(world.ids["cand_a2"])], "target_count": 400})
    assert too_many.status_code == 422 and "limit" in too_many.json()["error"]
    admin = await world.as_admin()
    started = await admin.post("/api/searches", json={"team_id": str(world.ids["team_beta"]), "target_count": 3, "deep_search": False})
    assert started.status_code == 202 and started.json()["search"]["candidate_count"] == 2 and started.json()["search"]["team"]["name"] == "Team Beta"
    run = await wait_done(world, admin, started.json()["search"]["id"])
    assert run["assigned_count"] == 6
    assert (await tl.get(f"/api/searches/{run['id']}")).status_code == 403                               # Alpha's lead cannot read Beta's run
    assert (await tl.get(f"/api/searches/{run['id']}/results")).status_code == 403
    assert (await tl.post(f"/api/searches/{run['id']}/cancel")).status_code == 403
    assert (await (await world.as_tl("beta")).get(f"/api/searches/{run['id']}")).status_code == 200       # Beta's lead sees the Admin's run for their team
    assert (await tl.get("/api/searches")).json()["total"] == 0


async def test_review_mode_then_bulk_assign(world):
    tl = await world.as_tl("alpha")
    a1, a2 = str(world.ids["cand_a1"]), str(world.ids["cand_a2"])
    started = await tl.post("/api/searches", json={"candidate_ids": [a1, a2], "mode": "review", "target_count": 5, "deep_search": False,
                                                   "filters": {"job_titles": ["AI Engineer", "Machine Learning Engineer"]}})
    run = await wait_done(world, tl, started.json()["search"]["id"])
    assert run["status"] == "completed" and run["mode"] == "review" and run["matched_count"] == 10 and run["assigned_count"] == 0
    assert (await (await world.as_member("a1")).get("/api/assignments")).json()["total"] == 0             # nothing assigned yet
    results = (await tl.get(f"/api/searches/{run['id']}/results", params={"candidate_id": a1})).json()
    assert results["total"] == 5 and [r["rank"] for r in results["items"]] == [1, 2, 3, 4, 5]
    assert all(r["assignment_id"] is None and r["match"]["reason"] and r["job"]["canonical_url"].startswith("https://") for r in results["items"])
    chosen = [r["job"]["id"] for r in results["items"][:2]]
    assigned = await tl.post("/api/assignments/bulk", json={"job_ids": chosen, "candidate_ids": [a1, a2], "search_run_id": run["id"]})
    assert assigned.status_code == 201 and assigned.json()["created"] == 4
    after = (await tl.get(f"/api/searches/{run['id']}/results", params={"candidate_id": a1})).json()["items"]
    assert [bool(r["assignment_id"]) for r in after] == [True, True, False, False, False]
    mine = (await (await world.as_member("a1")).get("/api/assignments")).json()
    assert mine["total"] == 2 and all(a["match_score"] is not None and a["source"] == "search" for a in mine["items"])
    # A Team Lead of another team cannot assign these jobs: they never appeared in that team's scope.
    assert (await (await world.as_tl("beta")).post("/api/assignments", json={"job_id": chosen[0], "candidate_id": str(world.ids["cand_b1"])})).status_code == 403


async def test_members_without_resume_or_titles_are_reported_not_searched(world):
    from app.portal.models import CandidateProfile
    async with world.sessions() as db:
        await db.execute(update(CandidateProfile).where(CandidateProfile.candidate_id == world.ids["cand_a2"]).values(desired_titles=[]))
        await db.commit()
    tl = await world.as_tl("alpha")
    started = await tl.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a1"]), str(world.ids["cand_a2"])], "target_count": 2, "deep_search": False})
    assert started.status_code == 202
    assert [s["candidate"]["name"] for s in started.json()["skipped"]] == ["Candidate A2"] and "job title" in started.json()["skipped"][0]["reason"]
    run = await wait_done(world, tl, started.json()["search"]["id"])
    assert run["candidate_count"] == 1 and run["assigned_count"] == 2
    only = await tl.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a2"])], "deep_search": False})
    assert only.status_code == 422                                     # nothing searchable: refused, not silently empty


async def test_low_scores_are_not_assigned_to_fill_the_quota(world):
    tl = await world.as_tl("alpha")
    started = await tl.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a1"])], "target_count": 50, "min_match_score": 101 - 1,
                                                   "deep_search": False})
    run = await wait_done(world, tl, started.json()["search"]["id"])
    # The fixtures hold 18 qualifying US postings: 50 were asked for, 18 real matches is what is reported.
    assert run["status"] == "completed" and run["matched_count"] == 18 and run["assigned_count"] < run["matched_count"]
    assert run["target_count"] == 50
    assert (await tl.get(f"/api/searches/{run['id']}/results")).json()["total"] == 18


async def test_cancel_and_restart_recovery(world):
    tl = await world.as_tl("alpha")
    gate = asyncio.Event()
    original = world.engine.run

    async def slow(state, req, resume):
        await gate.wait()
        return await original(state, req, resume)

    world.engine.run = slow
    started = await tl.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a1"])], "target_count": 5, "deep_search": False})
    run_id = started.json()["search"]["id"]
    for _ in range(50):
        if (await tl.get(f"/api/searches/{run_id}")).json()["status"] == "running":
            break
        await asyncio.sleep(0.05)
    assert (await tl.get("/api/searches", params={"status": "active"})).json()["total"] == 1
    cancel = await tl.post(f"/api/searches/{run_id}/cancel")
    assert cancel.status_code == 200 and cancel.json()["cancel_requested"] is True
    run = await wait_done(world, tl, run_id)
    assert run["status"] == "cancelled" and run["assigned_count"] == 0 and run["targets"][0]["status"] == "cancelled"
    assert (await (await world.as_member("a1")).get("/api/assignments")).json()["total"] == 0
    # The worker died right after the cancel request: the run is closed as cancelled, never resumed.
    async with world.sessions() as db:
        await db.execute(update(SearchRun).where(SearchRun.id == uuid.UUID(run_id)).values(status="running", heartbeat_at=None))
        await db.commit()
    assert await recover_interrupted(world.sessions) == ([], 1)
    assert (await tl.get(f"/api/searches/{run_id}")).json()["status"] == "cancelled"


async def test_deep_search_leads_are_verified_and_progress_is_streamed(world, monkeypatch):
    world.engine.fake_ai.available = True
    world.engine.fake_ai.status["OpenAI"]["configured"] = world.engine.fake_ai.status["Claude"]["configured"] = True
    world.engine.fake_ai.leads = [
        Candidate(url="https://careers.example.com/jobs/ml-1", title="Machine Learning Engineer", company="Example", provider="Claude", cited=True),
        Candidate(url="https://careers.example.com/jobs/gone", title="AI Engineer", company="Example", provider="Claude", cited=True),
    ]
    admin = await world.as_admin()
    started = await admin.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_b1"])], "target_count": 50, "deep_search": True,
                                                      "filters": {"job_titles": ["AI Engineer", "Machine Learning Engineer"]}})
    run = await wait_done(world, admin, started.json()["search"]["id"])
    assert run["status"] == "completed"
    async with world.sessions() as db:
        web = (await db.execute(select(JobPosting).where(JobPosting.canonical_url == "https://careers.example.com/jobs/ml-1"))).scalar_one()
        assert web.company == "Example Careers Inc" and web.verification_status == "verified" and "JobPosting" in web.verification_note
        assert (await db.execute(select(JobPosting).where(JobPosting.canonical_url.like("%/gone")))).first() is None
        assert (await db.execute(select(SearchResult).where(SearchResult.job_id == web.id))).scalars().all().__len__() == 1
    stream = await admin.get(f"/api/searches/{run['id']}/events")
    assert stream.status_code == 200 and "event: progress" in stream.text and "event: done" in stream.text and '"status":"completed"' in stream.text
