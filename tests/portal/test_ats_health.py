"""GET /api/integrations/ats: Admin only, and every state comes from rows in the job index, never from a claim.

The state tests use their own small platform list (the shared one in app/ats_platforms.py is still being edited) and
the readers the registry always has; the rows are written straight into the index tables of the test database."""
import time

import asyncpg

from app import ats_platforms
from app.ats_platforms import Platform
from app.portal.services import ats_health
from tests.portal.conftest import PLAIN_URL

URL = "/api/integrations/ats"
DAY = 86400.0
STATES = {"verified_working", "configured_unverified", "public_discovery_only", "temporarily_unavailable",
          "unsupported"}
SAMPLE = {p.id: p for p in [
    Platform("greenhouse", "Greenhouse", "public_api", ("boards.greenhouse.io",), "greenhouse", True),
    Platform("lever", "Lever", "public_api", ("jobs.lever.co",), "lever", True),
    Platform("ashby", "Ashby", "public_api", ("jobs.ashbyhq.com",), "ashby", True),
    Platform("workday", "Workday", "public_api", (".myworkdayjobs.com",), "workday"),
    Platform("workable", "Workable", "public_api", ("apply.workable.com",), "workable", True),
    Platform("icims", "iCIMS", "public_pages", (".icims.com",)),
    Platform("smartrecruiters", "SmartRecruiters", "restricted", ("jobs.smartrecruiters.com",), "smartrecruiters"),
    Platform("notbuilt", "Not Built Yet", "public_api", (), "notbuilt"),         # names a reader the registry does not have
]}


async def board(ats: str, slug: str, ok: bool, job_count: int, age_days: float | None) -> None:
    conn = await asyncpg.connect(PLAIN_URL)
    await conn.execute("INSERT INTO index_boards (ats, slug, ok, job_count, checked_at, origin) VALUES ($1,$2,$3,$4,$5,'found')",
                       ats, slug, ok, job_count, None if age_days is None else time.time() - age_days * DAY)
    await conn.close()


async def unit(src: str, key: str, ok: bool, count: int, age_days: float, error: str = "", scope: str = "") -> None:
    conn = await asyncpg.connect(PLAIN_URL)
    at = time.time() - age_days * DAY
    await conn.execute("INSERT INTO index_units (src, key, scope, fetched_at, expires_at, ok, count, error) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                       src, key, scope, at, at + 3600, ok, count, error)
    await conn.close()


async def states(world, admin) -> dict:
    ats_health.invalidate(world.app.state)                      # the evidence is kept for a minute
    response = await admin.get(URL)
    assert response.status_code == 200, response.text
    return {p["id"]: p for p in response.json()["platforms"]}


async def test_admin_only(world):
    assert (await (await world.client()).get(URL)).status_code == 401
    assert (await (await world.as_tl()).get(URL)).status_code == 403
    assert (await (await world.as_member()).get(URL)).status_code == 403
    assert (await (await world.as_admin()).get(URL)).status_code == 200


async def test_lists_every_platform_in_order_and_claims_nothing_on_an_empty_index(world):
    body = (await (await world.as_admin()).get(URL)).json()
    listed = body["platforms"]
    assert [p["id"] for p in listed] == list(ats_platforms.PLATFORMS) and len(listed) == 17
    assert body["window_days"] == 7
    for item in listed:
        source = ats_platforms.PLATFORMS[item["id"]]
        assert item["state"] in STATES and item["reason"].strip()
        assert item["state"] != "verified_working" and item["state"] != "temporarily_unavailable"      # nothing was read
        assert (item["label"], item["startup"], item["access"], item["hosts"], item["docs"], item["note"], item["reader"]) == (
            source.label, source.startup, source.access, list(source.hosts), source.docs, source.note, source.reader)
        assert item["access_note"] == ats_platforms.ACCESS[source.access]
        assert (item["boards_known"], item["boards_read_ok"], item["open_postings"], item["last_success_at"]) == (0, 0, 0, None)
        if source.access == "restricted":
            assert item["state"] == "unsupported"
        elif not source.reader:
            assert item["state"] == "public_discovery_only"
        else:
            assert item["state"] in ("configured_unverified", "unsupported", "public_discovery_only")
            assert (item["state"] == "configured_unverified") == item["reader_enabled"]
    assert {p["id"] for p in body["providers"]} >= {"openai", "claude", "techsara", "aws_web_search", "searxng", "serpapi",
                                                    "tinyfish", "techsara_web", "common_crawl"}
    assert all(p["state"] in ("configured", "not_configured", "switched_off") for p in body["providers"])
    by_id = {p["id"]: p for p in body["providers"]}
    assert by_id["openai"]["state"] == "not_configured" and by_id["common_crawl"]["state"] == "not_configured"   # the test world has no keys


async def test_each_state_comes_from_its_evidence(world, monkeypatch):
    monkeypatch.setattr(ats_platforms, "PLATFORMS", SAMPLE)
    admin = await world.as_admin()

    found = await states(world, admin)
    assert list(found) == list(SAMPLE)
    assert {key: item["state"] for key, item in found.items()} == {
        "greenhouse": "configured_unverified", "lever": "configured_unverified", "ashby": "configured_unverified",
        "workday": "configured_unverified", "workable": "configured_unverified", "icims": "public_discovery_only",
        "smartrecruiters": "unsupported", "notbuilt": "unsupported"}
    assert found["notbuilt"]["reader_enabled"] is False and found["greenhouse"]["reader_enabled"] is True

    # Greenhouse: one board read yesterday with postings, one unread catalogue board, one dead board.
    await board("greenhouse", "acme", True, 5, 1)
    await unit("greenhouse", "acme", True, 5, 1)
    await board("greenhouse", "unread", True, 0, None)
    await board("greenhouse", "gone", False, 0, 1)
    # Lever: the only successful read is ten days old.
    await board("lever", "old", True, 3, 10)
    # Ashby: a board is known and its reads fail.
    await board("ashby", "down", True, 0, 2)
    await unit("ashby", "down", False, 0, 0.1, error="HTTP 503")
    # Workday: reads that worked but brought nothing prove nothing.
    await board("workday", "tenant|wd1|site", True, 0, 1)
    await unit("workday", "tenant|wd1|site", True, 0, 1, scope="engineer")
    # Workable: the cross-company search (key "*") was read with postings; it is a read, not a board.
    await unit("workable", "*", True, 4, 0.5, scope="engineer")
    # Evidence never overrides the access class or a missing reader.
    await board("smartrecruiters", "acme", True, 9, 1)
    await unit("notbuilt", "acme", True, 9, 1)
    await unit("bullhorn", "acme", True, 9, 1)                 # a removed platform's old rows: ignored, not listed
    await board("ceipal", "acme", True, 9, 1)
    await unit("icims", "acme", True, 9, 1)

    found = await states(world, admin)
    g = found["greenhouse"]
    assert g["state"] == "verified_working"
    assert (g["boards_known"], g["boards_read_ok"], g["boards_failed"], g["open_postings"]) == (3, 1, 1, 5)
    assert g["last_success_at"] is not None and "1 board read successfully" in g["reason"]
    assert found["lever"]["state"] == "configured_unverified" and found["lever"]["last_success_at"] is not None
    assert found["lever"]["boards_read_ok"] == 0 and "more than 7 days" in found["lever"]["reason"]
    a = found["ashby"]
    assert a["state"] == "temporarily_unavailable" and a["boards_failed"] == 1 and "HTTP 503" in a["reason"]
    assert a["last_success_at"] is None
    assert found["workday"]["state"] == "configured_unverified" and found["workday"]["boards_known"] == 1
    w = found["workable"]
    assert w["state"] == "verified_working" and (w["boards_known"], w["boards_read_ok"], w["open_postings"]) == (0, 0, 4)
    assert found["smartrecruiters"]["state"] == "unsupported" and found["notbuilt"]["state"] == "unsupported"
    assert "bullhorn" not in found and "ceipal" not in found and found["icims"]["state"] == "public_discovery_only"
    for item in found.values():                                 # the invariant: no "verified" without a successful read
        if item["state"] == "verified_working":
            assert item["reader_enabled"] and item["last_success_at"] and item["open_postings"] > 0

    # A reader that is switched off is "not supported", whatever the index holds; switched back on, it is verified again.
    assert (await admin.patch("/api/settings", json={"disabled_sources": ["greenhouse"]})).status_code == 200
    response = (await admin.get(URL)).json()                    # no invalidate: the switch is not cached
    off = {p["id"]: p for p in response["platforms"]}["greenhouse"]
    assert off["state"] == "unsupported" and off["reader_enabled"] is False and off["boards_read_ok"] == 1
    assert (await admin.patch("/api/settings", json={"disabled_sources": []})).status_code == 200
    assert (await states(world, admin))["greenhouse"]["state"] == "verified_working"

    # A failing board next to a working one does not make the platform unavailable; once every read fails, it does.
    await unit("greenhouse", "flaky", False, 0, 0.1, error="HTTP 500")
    assert (await states(world, admin))["greenhouse"]["state"] == "verified_working"
    conn = await asyncpg.connect(PLAIN_URL)
    await conn.execute("UPDATE index_units SET ok=false, error='HTTP 502' WHERE src='greenhouse'")
    await conn.execute("UPDATE index_boards SET ok=false, job_count=0 WHERE ats='greenhouse' AND slug='acme'")
    await conn.close()
    down = (await states(world, admin))["greenhouse"]
    assert down["state"] == "temporarily_unavailable" and down["boards_read_ok"] == 0 and down["open_postings"] == 0


async def test_the_index_evidence_is_cached_for_a_minute(world, monkeypatch):
    monkeypatch.setattr(ats_platforms, "PLATFORMS", SAMPLE)
    admin = await world.as_admin()
    assert (await states(world, admin))["lever"]["state"] == "configured_unverified"
    await board("lever", "acme", True, 2, 0.5)
    again = {p["id"]: p for p in (await admin.get(URL)).json()["platforms"]}
    assert again["lever"]["state"] == "configured_unverified"                         # still the kept answer
    monkeypatch.setattr(ats_health, "CACHE_SECONDS", 0.0)
    fresh = {p["id"]: p for p in (await admin.get(URL)).json()["platforms"]}
    assert fresh["lever"]["state"] == "verified_working" and fresh["lever"]["boards_read_ok"] == 1


async def test_common_crawl_and_switched_off_providers(world):
    admin = await world.as_admin()
    conn = await asyncpg.connect(PLAIN_URL)
    await conn.execute("INSERT INTO index_boards (ats, slug, ok, job_count, checked_at, origin) VALUES "
                       "('lever','a',true,0,NULL,'commoncrawl'), ('lever','b',true,3,$1,'commoncrawl')", time.time())
    await conn.close()
    assert (await admin.patch("/api/settings", json={"disabled_sources": ["searxng"]})).status_code == 200
    ats_health.invalidate(world.app.state)
    providers = {p["id"]: p for p in (await admin.get(URL)).json()["providers"]}
    crawl = providers["common_crawl"]
    assert crawl["state"] == "configured" and "2 boards imported, 1 read so far, 1 with postings" in crawl["reason"]
    assert providers["searxng"]["state"] == "switched_off"
    assert providers["serpapi"]["state"] == "not_configured"
