"""Posting-age limit in the portal: what a search stores and runs with, searches and presets from before the limit
existed, validation, and the figures a finished search reports."""
from datetime import timedelta

from sqlalchemy import select, update

from app import freshness
from app.portal.models import SearchPreset, SearchTarget
from tests import conftest
from tests.portal.test_search_flow import wait_done
from tests.portal.test_search_platforms import hold_worker


def gh(job_id: int, days: int | None) -> dict:
    """A Greenhouse posting published `days` ago (None: the board gives no date at all)."""
    job = conftest.gh_job(job_id, f"AI Engineer, Team {job_id}", "Austin, TX")
    job["first_published"] = f"{(freshness.today() - timedelta(days=days)).isoformat()}T10:00:00-04:00" if days is not None else None
    return job


BOARD = {"jobs": [gh(1, 2), gh(2, 7), gh(3, 8), gh(4, 45), gh(5, None)]}


async def start(tl, world, **filters):
    body = {"candidate_ids": [str(world.ids["cand_a1"])], "target_count": 10, "deep_search": False, "mode": "review",
            "filters": {"job_titles": ["AI Engineer"], "ats_platforms": ["greenhouse"], **filters}}
    return await tl.post("/api/searches", json=body)


async def titles_of(tl, run) -> set[str]:
    results = (await tl.get(f"/api/searches/{run['id']}/results")).json()
    return {row["job"]["title"] for row in results["items"]}


async def test_a_search_runs_with_seven_days_unless_told_otherwise(world, monkeypatch):
    monkeypatch.setattr(conftest, "GREENHOUSE_ACME", BOARD)
    tl = await world.as_tl("alpha")
    started = await start(tl, world)
    assert started.status_code == 202, started.text
    assert started.json()["search"]["filters"]["posted_within_days"] == 7 and started.json()["search"]["filters"]["include_undated"] is True
    run = await wait_done(world, tl, started.json()["search"]["id"])
    target = run["targets"][0]
    assert (target["filters"]["posted_within_days"], target["filters"]["include_undated"]) == (7, True)
    # 2 and 7 days old are in, 8 and 45 days are out; the posting without a date is delivered and has no posting date.
    assert await titles_of(tl, run) == {"AI Engineer, Team 1", "AI Engineer, Team 2", "AI Engineer, Team 5"}
    discovery = target["discovery"]
    assert (discovery["posted_within_days"], discovery["include_undated"], discovery["too_old"], discovery["undated_dropped"]) == (7, True, 2, 0)
    assert (discovery["dated_delivered"], discovery["undated_delivered"]) == (2, 1)
    assert discovery["age_by_source"] == {"Greenhouse": {"fresh": 2, "too_old": 2, "undated": 1}}
    assert discovery["platforms"]["greenhouse"]["reasons"] == {"too_old": 2}
    results = (await tl.get(f"/api/searches/{run['id']}/results")).json()["items"]
    dates = {row["job"]["title"]: row["job"]["posted_at"] for row in results}
    assert dates["AI Engineer, Team 5"] is None and dates["AI Engineer, Team 1"] == (freshness.today() - timedelta(days=2)).isoformat()


async def test_the_limit_and_the_undated_switch_are_stored_validated_and_applied(world, monkeypatch):
    monkeypatch.setattr(conftest, "GREENHOUSE_ACME", BOARD)
    tl = await world.as_tl("alpha")
    for bad in ({"posted_within_days": 5}, {"posted_within_days": -1}, {"posted_within_days": "week"}, {"include_undated": "perhaps"}):
        refused = await start(tl, world, **bad)
        assert refused.status_code == 422, bad
    wide = await start(tl, world, posted_within_days=30, include_undated=False)
    run = await wait_done(world, tl, wide.json()["search"]["id"])
    assert (run["filters"]["posted_within_days"], run["filters"]["include_undated"]) == (30, False)
    assert await titles_of(tl, run) == {"AI Engineer, Team 1", "AI Engineer, Team 2", "AI Engineer, Team 3"}
    discovery = run["targets"][0]["discovery"]
    assert (discovery["too_old"], discovery["undated_dropped"], discovery["undated_delivered"]) == (1, 1, 0)
    anytime = await start(tl, world, posted_within_days=0)
    run = await wait_done(world, tl, anytime.json()["search"]["id"])
    assert len(await titles_of(tl, run)) == 5 and run["targets"][0]["discovery"]["too_old"] == 0
    assert (await (await world.as_member("a1")).post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a1"])],
                                                                           "filters": {"posted_within_days": 0}})).status_code == 403


async def test_a_search_stored_before_the_limit_existed_runs_with_seven_days(world, monkeypatch):
    monkeypatch.setattr(conftest, "GREENHOUSE_ACME", BOARD)
    tl = await world.as_tl("alpha")
    worker = hold_worker(world)
    started = await start(tl, world, posted_within_days=0)
    run_id = started.json()["search"]["id"]
    async with world.sessions() as db:                                   # a waiting search written by the release before
        target = (await db.execute(select(SearchTarget).where(SearchTarget.search_run_id == run_id))).scalar_one()
        old = {k: v for k, v in target.candidate_specific_filters.items() if k not in ("posted_within_days", "include_undated")}
        await db.execute(update(SearchTarget).where(SearchTarget.search_run_id == target.search_run_id).values(candidate_specific_filters=old))
        await db.commit()
    worker.set()
    run = await wait_done(world, tl, run_id)
    assert run["status"] == "completed" and "posted_within_days" not in run["targets"][0]["filters"]
    discovery = run["targets"][0]["discovery"]
    assert (discovery["posted_within_days"], discovery["include_undated"], discovery["too_old"]) == (7, True, 2)
    assert await titles_of(tl, run) == {"AI Engineer, Team 1", "AI Engineer, Team 2", "AI Engineer, Team 5"}


async def test_presets_keep_the_limit_and_presets_from_before_get_the_default(world, monkeypatch):
    monkeypatch.setattr(conftest, "GREENHOUSE_ACME", BOARD)
    tl = await world.as_tl("alpha")
    saved = await tl.post("/api/search-presets", json={"name": "Two weeks, dated only", "filters": {
        "target_count": 10, "filters": {"posted_within_days": 14, "include_undated": False}}})
    assert saved.status_code == 201, saved.text
    kept = saved.json()["filters"]["filters"]
    assert (kept["posted_within_days"], kept["include_undated"]) == (14, False)
    bad = await tl.post("/api/search-presets", json={"name": "Bad", "filters": {"filters": {"posted_within_days": 10}}})
    assert bad.status_code == 422
    async with world.sessions() as db:                                   # saved before the limit existed
        db.add(SearchPreset(owner_id=world.ids["tl_alpha"], team_id=world.ids["team_alpha"], name="Old preset",
                            filters={"mode": "review", "target_count": 10, "deep_search": False, "min_match_score": None,
                                     "filters": {"job_titles": ["AI Engineer"], "ats_platforms": ["greenhouse"]}}))
        await db.commit()
    presets = {p["name"]: p["filters"] for p in (await tl.get("/api/search-presets")).json()}
    old = presets["Old preset"]
    assert "posted_within_days" not in old["filters"] and "include_undated" not in old["filters"]       # loads as it was stored
    # Sent back the way the form sends it (no value): the search gets the defaults and runs with them.
    started = await tl.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a1"])], "mode": old["mode"],
                                                   "target_count": old["target_count"], "deep_search": old["deep_search"], "filters": old["filters"]})
    assert started.status_code == 202, started.text
    run = await wait_done(world, tl, started.json()["search"]["id"])
    assert (run["filters"]["posted_within_days"], run["filters"]["include_undated"]) == (7, True)
    assert await titles_of(tl, run) == {"AI Engineer, Team 1", "AI Engineer, Team 2", "AI Engineer, Team 5"}
