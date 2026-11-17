"""The 1,000-results-per-candidate limit through the portal API, and what a run reports about rounds and discovery."""
from tests.portal.test_search_flow import wait_done


async def test_form_limits_come_from_the_options_and_one_thousand_is_allowed(world):
    admin = await world.as_admin()
    a1 = str(world.ids["cand_a1"])
    limits = (await admin.get("/api/searches/options")).json()["limits"]
    assert limits["max_target_count"] == 1000 and limits["max_results_per_search_run"] == 500      # the form reads both from here

    over = await admin.post("/api/searches", json={"candidate_ids": [a1], "target_count": 1001, "deep_search": False})
    assert over.status_code == 422
    # 1,000 for one candidate exceeds the default "results per search run" (500): refused with the existing message ...
    blocked = await admin.post("/api/searches", json={"candidate_ids": [a1], "target_count": 1000, "deep_search": False})
    assert blocked.status_code == 422 and "limit of 500 results per run" in blocked.json()["error"]
    # ... until the Admin raises that setting, which the existing settings page already allows (up to 5000).
    saved = await admin.patch("/api/settings", json={"max_results_per_search_run": 1000, "default_target_count": 1000})
    assert saved.status_code == 200 and saved.json()["default_target_count"] == 1000
    assert (await admin.patch("/api/settings", json={"default_target_count": 1001})).status_code == 422
    assert (await admin.get("/api/searches/options")).json()["limits"]["max_results_per_search_run"] == 1000

    started = await admin.post("/api/searches", json={"candidate_ids": [a1], "target_count": 1000, "deep_search": False})
    assert started.status_code == 202, started.text
    run = await wait_done(world, admin, started.json()["search"]["id"])
    # Asked for 1,000; 18 real postings match: 18 is what is stored and shown. Nothing is padded.
    assert run["status"] == "completed" and run["target_count"] == 1000 and run["matched_count"] == 18
    target = run["targets"][0]
    assert target["matched"] == 18 and target["round"] == 1
    assert target["discovery"]["rounds"][0]["matches"] == 18 and target["discovery"]["searx_queries"] == 0
    assert "every discovery budget" in target["discovery"]["stop_reason"]
    results = (await admin.get(f"/api/searches/{run['id']}/results", params={"page_size": 200})).json()
    assert results["total"] == 18 and len({r["job"]["id"] for r in results["items"]}) == 18
    assert all(r["match"]["matched_query"] == "AI Engineer" or r["match"]["matched_query"] for r in results["items"])
    assert all(r["job"]["verification_status"] == "verified" for r in results["items"])


async def test_the_servers_own_ceiling_is_reported_and_enforced(world):
    admin = await world.as_admin()
    world.settings.search_max_results = 300                # as SEARCH_MAX_RESULTS=300 in the environment would
    try:
        assert (await admin.get("/api/searches/options")).json()["limits"]["max_target_count"] == 300
        refused = await admin.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a1"])], "target_count": 400, "deep_search": False})
        assert refused.status_code == 422 and "At most 300 jobs per candidate" in refused.json()["error"]
    finally:
        world.settings.search_max_results = 1000


async def test_a_job_known_only_from_google_jobs_is_stored_as_unverified(world):
    from app.portal.services import jobs as jobs_service
    from app.schemas import Job
    listed = Job(title="AI Engineer", company="Listed Only Inc", location="Austin, TX", url="https://www.linkedin.com/jobs/view/7",
                 source="Google Jobs", sources=["Google Jobs (via Dice)"], aggregator=True, verification_level="listed",
                 verification="Listed on Google Jobs via Dice (SerpApi API); not checked on the employer's site")
    async with world.sessions() as db:
        posting = await jobs_service.upsert_engine_job(db, listed)
        assert posting.verification_status == "unverified" and "not checked on the employer's site" in posting.verification_note
        verified = await jobs_service.upsert_engine_job(db, listed.model_copy(update={"verification_level": "verified", "url": "https://x.example/job/1", "company": "Other"}))
        assert verified.verification_status == "verified"
        await db.rollback()
