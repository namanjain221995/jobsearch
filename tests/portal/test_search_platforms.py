"""ATS platform filter and startup focus in the portal: validation, persistence (searches and presets, old and new
stored filters), the options endpoint, the statistics a finished search reports, and team isolation."""
import asyncio

from sqlalchemy import select, update

from app.ats_platforms import PLATFORMS, STARTUP_PLATFORMS
from app.portal.models import SearchPreset, SearchRun, SearchTarget
from tests.conftest import CALLS
from tests.portal.test_search_flow import GH, wait_done

LEVER = "https://api.lever.co/v0/postings/beta"


def hold_worker(world) -> asyncio.Event:
    """The background worker picks a new search up only after the returned event is set."""
    gate, original = asyncio.Event(), world.queue.runner

    async def held(run_id):
        await gate.wait()
        await original(run_id)

    world.queue.runner = held
    return gate


async def test_options_list_the_platforms_with_their_coverage(world):
    tl = await world.as_tl("alpha")
    options = (await tl.get("/api/searches/options")).json()
    rows = {p["id"]: p for p in options["platforms"]}
    assert list(rows) == list(PLATFORMS)                                # the registry, in its order
    assert all(set(p) == {"id", "label", "startup", "reader", "reader_enabled", "access", "coverage", "hint"} for p in rows.values())
    assert {i for i, p in rows.items() if p["startup"]} == set(STARTUP_PLATFORMS)
    enabled = {s["name"] for s in options["sources"] if s["enabled"]}
    for platform in PLATFORMS.values():
        row = rows[platform.id]
        assert row["label"] == platform.label and row["access"] == platform.access and row["reader"] == platform.reader
        assert row["reader_enabled"] is (bool(platform.reader) and platform.reader in enabled)
        assert (row["coverage"] == "full") is row["reader_enabled"] and row["coverage"] in ("full", "limited", "none")
        assert row["reader_enabled"] or row["hint"]                    # limited or no coverage always says why
    assert rows["greenhouse"]["reader_enabled"] and rows["lever"]["coverage"] == "full"
    assert any(p["coverage"] == "limited" for p in rows.values()) and any(p["coverage"] == "none" for p in rows.values())
    assert (await (await world.as_admin()).get("/api/searches/options")).json()["platforms"] == options["platforms"]
    assert (await (await world.as_member("a1")).get("/api/searches/options")).status_code == 403


async def test_unknown_platform_is_refused_and_isolation_is_unchanged(world):
    tl = await world.as_tl("alpha")
    a1, b1 = str(world.ids["cand_a1"]), str(world.ids["cand_b1"])
    bad = await tl.post("/api/searches", json={"candidate_ids": [a1], "filters": {"ats_platforms": ["lever", "nosuchats"]}})
    assert bad.status_code == 422 and "Unknown ATS platform: nosuchats" in bad.json()["detail"]
    assert bad.json()["fields"] == {"filters.ats_platforms": "Unknown ATS platform: nosuchats"}
    assert (await tl.post("/api/searches", json={"candidate_ids": [a1], "filters": {"ats_platforms": "lever"}})).status_code == 422
    assert (await tl.post("/api/searches", json={"candidate_ids": [a1], "filters": {"startup_focus": "perhaps"}})).status_code == 422
    # The new options open nothing: another team's candidates and team stay off limits, members cannot search at all.
    new = {"filters": {"ats_platforms": ["lever"], "startup_focus": True}}
    assert (await tl.post("/api/searches", json={"candidate_ids": [b1], **new})).status_code == 403
    assert (await tl.post("/api/searches", json={"team_id": str(world.ids["team_beta"]), **new})).status_code == 403
    assert (await (await world.as_member("a1")).post("/api/searches", json={"candidate_ids": [a1], **new})).status_code == 403
    async with world.sessions() as db:
        assert (await db.execute(select(SearchRun))).scalars().all() == []


async def test_filtered_search_is_stored_run_and_reported(world):
    tl = await world.as_tl("alpha")
    a1 = str(world.ids["cand_a1"])
    started = await tl.post("/api/searches", json={"candidate_ids": [a1], "target_count": 10, "deep_search": False,
                                                   "filters": {"ats_platforms": ["Lever"], "startup_focus": True}})
    assert started.status_code == 202, started.text
    run = await wait_done(world, tl, started.json()["search"]["id"])
    assert run["status"] == "completed"
    assert run["filters"] == {"ats_platforms": ["lever"], "startup_focus": True, "posted_within_days": 7, "include_undated": True}
    target = run["targets"][0]
    assert target["filters"]["ats_platforms"] == ["lever"] and target["filters"]["startup_focus"] is True
    assert CALLS[LEVER] == 1 and CALLS[GH] == 0                          # only the chosen platform was read
    results = (await tl.get(f"/api/searches/{run['id']}/results")).json()
    assert results["total"] == run["matched_count"] == 1
    assert all(r["job"]["canonical_url"].startswith("https://jobs.lever.co/") for r in results["items"])
    discovery = target["discovery"]
    assert discovery["platform_filter"] == ["lever"] and discovery["startup_focus"] is True and list(discovery["platforms"]) == ["lever"]
    lever = discovery["platforms"]["lever"]
    assert (lever["label"], lever["coverage"], lever["considered"], lever["verified"], lever["unique"], lever["matched"]) == ("Lever", "full", 2, 1, 1, 1)
    assert lever["rejected"] == 1 and lever["reasons"] == {"non_us": 1} and lever["duplicates"] == 0
    assert discovery["providers"] == {} and discovery["coverage_warnings"] == []
    assert discovery["seconds_to_first_verified"] is not None and discovery["total_seconds"] >= discovery["seconds_to_first_verified"]
    assert list(run["sources"]) == ["Lever"] and run["discovered_count"] == 2
    # Team isolation of what the search stored: the other team's lead reads neither the run nor its statistics.
    other = await world.as_tl("beta")
    assert (await other.get(f"/api/searches/{run['id']}")).status_code == 403
    assert (await other.get(f"/api/searches/{run['id']}/results")).status_code == 403
    assert (await other.get("/api/searches")).json()["total"] == 0


async def test_search_without_the_options_is_stored_as_before_and_old_targets_still_run(world):
    tl = await world.as_tl("alpha")
    a1 = str(world.ids["cand_a1"])
    worker = hold_worker(world)
    started = await tl.post("/api/searches", json={"candidate_ids": [a1], "target_count": 5, "deep_search": False,
                                                   "filters": {"job_titles": ["AI Engineer"]}})
    run_id = started.json()["search"]["id"]
    # No platform keys when those options are not used; the posting-age limit is always stored (tests/portal/test_search_freshness.py).
    assert started.json()["search"]["filters"] == {"job_titles": ["AI Engineer"], "posted_within_days": 7, "include_undated": True}
    async with world.sessions() as db:                                   # a target written before the options existed
        target = (await db.execute(select(SearchTarget).where(SearchTarget.search_run_id == run_id))).scalar_one()
        old = {k: v for k, v in target.candidate_specific_filters.items() if k not in ("ats_platforms", "startup_focus")}
        await db.execute(update(SearchTarget).where(SearchTarget.search_run_id == target.search_run_id).values(candidate_specific_filters=old))
        await db.commit()
    worker.set()
    run = await wait_done(world, tl, run_id)
    assert run["status"] == "completed" and run["matched_count"] == 5 and CALLS[GH] == 1 and CALLS[LEVER] == 1
    discovery = run["targets"][0]["discovery"]
    assert discovery["platform_filter"] == [] and discovery["startup_focus"] is False and discovery["filtered_out"] == 0
    assert set(discovery["platforms"]) == {"greenhouse", "lever"} and sum(p["matched"] for p in discovery["platforms"].values()) == 5


async def test_a_stored_platform_that_left_the_registry_is_not_searched_as_everything(world):
    tl = await world.as_tl("alpha")
    worker = hold_worker(world)
    started = await tl.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a1"])], "target_count": 5, "deep_search": False,
                                                   "filters": {"ats_platforms": ["lever"]}})
    run_id = started.json()["search"]["id"]
    async with world.sessions() as db:
        target = (await db.execute(select(SearchTarget).where(SearchTarget.search_run_id == run_id))).scalar_one()
        await db.execute(update(SearchTarget).where(SearchTarget.search_run_id == target.search_run_id).values(
            candidate_specific_filters={**target.candidate_specific_filters, "ats_platforms": ["retiredats"]}))
        await db.commit()
    worker.set()
    run = await wait_done(world, tl, run_id)
    assert run["status"] == "failed" and run["matched_count"] == 0 and CALLS[GH] == 0 and CALLS[LEVER] == 0
    assert run["targets"][0]["message"] == "The ATS platforms chosen for this search are no longer available"


async def test_presets_keep_the_options_and_old_presets_still_load(world):
    tl = await world.as_tl("alpha")
    saved = await tl.post("/api/search-presets", json={"name": "Startups", "filters": {
        "target_count": 20, "filters": {"ats_platforms": ["ashby", "lever"], "startup_focus": True}}})
    assert saved.status_code == 201, saved.text
    assert saved.json()["filters"]["filters"]["ats_platforms"] == ["ashby", "lever"] and saved.json()["filters"]["filters"]["startup_focus"] is True
    bad = await tl.post("/api/search-presets", json={"name": "Bad", "filters": {"filters": {"ats_platforms": ["nosuchats"]}}})
    assert bad.status_code == 422 and "Unknown ATS platform: nosuchats" in bad.json()["detail"]
    async with world.sessions() as db:                                   # saved before the options existed
        db.add(SearchPreset(owner_id=world.ids["tl_alpha"], team_id=world.ids["team_alpha"], name="Old preset",
                            filters={"mode": "review", "target_count": 5, "deep_search": False, "min_match_score": None,
                                     "filters": {"job_titles": ["AI Engineer"], "remote_only": None}}))
        await db.commit()
    presets = {p["name"]: p["filters"] for p in (await tl.get("/api/search-presets")).json()}
    assert set(presets) == {"Startups", "Old preset"} and "ats_platforms" not in presets["Old preset"]["filters"]
    assert (await (await world.as_tl("beta")).get("/api/search-presets")).json() == []          # presets stay with their owner
    # The old preset's values, sent back the way the form sends them, start a normal search of every source.
    old = presets["Old preset"]
    started = await tl.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a1"])], "mode": old["mode"],
                                                   "target_count": old["target_count"], "deep_search": old["deep_search"], "filters": old["filters"]})
    assert started.status_code == 202, started.text
    run = await wait_done(world, tl, started.json()["search"]["id"])
    assert run["status"] == "completed" and run["matched_count"] == 5 and "ats_platforms" not in run["filters"] and CALLS[GH] == 1


async def test_a_removed_platform_in_a_stored_search_or_preset_is_treated_as_absent(world):
    """Bullhorn, CEIPAL, JobDiva and SmartSearch were removed on 2026-11-22: searches and presets stored before still
    name them. Such an id is skipped when the stored search runs and dropped when a preset is sent back."""
    assert "bullhorn" not in PLATFORMS
    tl = await world.as_tl("alpha")
    worker = hold_worker(world)
    started = await tl.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a1"])], "target_count": 5, "deep_search": False,
                                                   "filters": {"ats_platforms": ["lever"]}})
    run_id = started.json()["search"]["id"]
    async with world.sessions() as db:
        target = (await db.execute(select(SearchTarget).where(SearchTarget.search_run_id == run_id))).scalar_one()
        await db.execute(update(SearchTarget).where(SearchTarget.search_run_id == target.search_run_id).values(
            candidate_specific_filters={**target.candidate_specific_filters, "ats_platforms": ["bullhorn", "lever"]}))
        await db.commit()
    worker.set()
    run = await wait_done(world, tl, run_id)
    assert run["status"] == "completed" and CALLS[LEVER] == 1 and CALLS[GH] == 0          # as if only "lever" was stored
    assert run["targets"][0]["discovery"]["platform_filter"] == ["lever"]
    # A preset saved before the removal: it lists, and sent back as the form sends it, the removed id is dropped.
    async with world.sessions() as db:
        db.add(SearchPreset(owner_id=world.ids["tl_alpha"], team_id=world.ids["team_alpha"], name="Staffing",
                            filters={"mode": "review", "target_count": 5, "deep_search": False,
                                     "filters": {"job_titles": ["AI Engineer"], "ats_platforms": ["ceipal", "lever"]}}))
        await db.commit()
    presets = {p["name"]: p["filters"] for p in (await tl.get("/api/search-presets")).json()}
    old = presets["Staffing"]
    again = await tl.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a1"])], "mode": old["mode"],
                                                 "target_count": old["target_count"], "deep_search": old["deep_search"], "filters": old["filters"]})
    assert again.status_code == 202, again.text
    assert again.json()["search"]["filters"]["ats_platforms"] == ["lever"]
    only = await tl.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a1"])], "filters": {"ats_platforms": ["jobdiva"]}})
    assert only.status_code == 422 and "no longer available" in only.json()["detail"]  # never widened to every platform
