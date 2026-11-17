"""Admin job management, settings, notifications, and the PostgreSQL job index contract."""
import time

from app.database import pack, unpack
from app.portal.index_pg import PgJobIndex
from app.schemas import Job
from tests.portal.conftest import PLAIN_URL, add_job


async def test_manual_jobs_csv_import_and_delegation_scenario(world):
    admin = await world.as_admin()
    first = await add_job(world, title="Data Engineer", description="<p>Build <b>pipelines</b></p><script>alert(1)</script><ul><li>SQL</li><li>Spark</li></ul>")
    view = (await admin.get(f"/api/jobs/{first}")).json()
    assert view["verification_status"] == "unverified" and view["origin"] == "manual"            # never "verified" without a check
    assert "<" not in view["description"] and "alert" not in view["description"] and "• SQL" in view["description"]
    duplicate = await admin.post("/api/jobs", json={"company": "Northwind Labs", "title": "Data Engineer", "canonical_url": view["canonical_url"] + "?utm_source=x"})
    assert duplicate.status_code == 409
    csv_text = ("company,title,state,city,work_mode,canonical_url,requirements,description\n"
                "Contoso,ML Engineer,WA,Seattle,Remote,https://contoso.example/jobs/1,Python;PyTorch,Train models\n"
                "Contoso,AI Engineer,CA,San Jose,Hybrid,https://contoso.example/jobs/2,Python,Build agents\n"
                ",Missing Company,,,,,,\n"
                "Contoso,ML Engineer,WA,Seattle,Remote,https://contoso.example/jobs/1,,dup\n"
                "=cmd,Bad Mode,TX,Austin,Underwater,,,\n")
    imported = await admin.post("/api/jobs/import-csv", files={"file": ("jobs.csv", csv_text.encode())})
    assert imported.status_code == 200 and imported.json()["created"] == 2 and imported.json()["skipped"] == 3 and len(imported.json()["errors"]) == 3
    listing = (await admin.get("/api/jobs", params={"company": "contoso"})).json()
    assert listing["total"] == 2 and {j["origin"] for j in listing["items"]} == {"import"}
    job_ids = [j["id"] for j in listing["items"]] + [first]
    # Scenario: Admin delegates to Team Beta, its lead splits the jobs between two members.
    tl = await world.as_tl("beta")
    assert (await admin.post(f"/api/teams/{world.ids['team_beta']}/queue", json={"job_ids": job_ids, "note": "Priority"})).status_code == 201
    assert (await tl.get("/api/notifications")).json()["items"][0]["type"] == "jobs_delegated"
    queue = (await tl.get(f"/api/teams/{world.ids['team_beta']}/queue")).json()["items"]
    assert len(queue) == 3 and all(q["note"] == "Priority" for q in queue)
    b1, b2 = str(world.ids["member_b1"]), str(world.ids["member_b2"])
    await tl.post("/api/assignments/bulk", json={"job_ids": [queue[0]["job"]["id"], queue[1]["job"]["id"]], "member_ids": [b1],
                                                 "queue_item_ids": [queue[0]["id"], queue[1]["id"]]})
    await tl.post("/api/assignments/bulk", json={"job_ids": [queue[2]["job"]["id"]], "member_ids": [b2], "queue_item_ids": [queue[2]["id"]]})
    assert (await (await world.as_member("b1")).get("/api/assignments")).json()["total"] == 2
    assert (await (await world.as_member("b2")).get("/api/assignments")).json()["total"] == 1
    assert (await tl.get(f"/api/teams/{world.ids['team_beta']}/queue")).json()["total"] == 0
    overview = (await admin.get("/api/analytics/admin")).json()
    beta = next(t for t in overview["teams"] if t["team"]["name"] == "Team Beta")
    assert beta["stats"]["assigned"] == 3 and beta["members"] == 2 and overview["totals"]["job_postings"] == 3
    assert overview["totals"]["teams"] == 2 and overview["totals"]["members"] == 4 and overview["totals"]["team_leads"] == 2
    filtered = (await admin.get("/api/analytics/admin", params={"company": "contoso"})).json()
    assert filtered["stats"]["assigned"] == 2
    assert (await admin.get("/api/analytics/admin", params={"team_id": str(world.ids["team_alpha"])})).json()["stats"]["assigned"] == 0
    patched = await admin.patch(f"/api/jobs/{first}", json={"title": "Senior Data Engineer", "salary_min": 150000, "salary_max": 190000})
    assert patched.status_code == 200 and patched.json()["title"] == "Senior Data Engineer"
    assert (await tl.patch(f"/api/jobs/{first}", json={"title": "Hacked"})).status_code == 403
    assert (await tl.post("/api/jobs", json={"company": "X", "title": "Engineer"})).status_code == 403


async def test_settings_presets_notifications_and_health(world):
    admin, tl = await world.as_admin(), await world.as_tl("alpha")
    assert (await admin.get("/api/settings")).json()["watermark_enabled"] is True
    saved = await admin.patch("/api/settings", json={"watermark_enabled": False, "max_members_per_search": 1, "disabled_sources": ["amazon"]})
    assert saved.status_code == 200 and saved.json()["disabled_sources"] == ["amazon"]
    assert (await admin.patch("/api/settings", json={"disabled_sources": ["nope"]})).status_code == 422
    assert (await tl.patch("/api/settings", json={"watermark_enabled": True})).status_code == 403
    me = (await (await world.as_member("a1")).get("/api/auth/me")).json()
    assert me["settings"]["watermark_enabled"] is False
    limited = await tl.post("/api/searches", json={"member_ids": [str(world.ids["member_a1"]), str(world.ids["member_a2"])], "target_count": 5})
    assert limited.status_code == 422 and "At most 1" in limited.json()["error"]
    options = (await tl.get("/api/searches/options")).json()
    assert next(s for s in options["sources"] if s["name"] == "amazon")["enabled"] is False and options["limits"]["max_members_per_search"] == 1
    preset = await tl.post("/api/search-presets", json={"name": "ML remote", "filters": {"target_count": 20, "filters": {"job_titles": ["ML Engineer"], "remote_only": True}}})
    assert preset.status_code == 201 and preset.json()["filters"]["filters"]["remote_only"] is True
    assert len((await tl.get("/api/search-presets")).json()) == 1 and (await admin.get("/api/search-presets")).json() == []
    integrations = (await admin.get("/api/integrations")).json()
    assert integrations["email"]["backend"] == "none" and integrations["ai_providers"]["openai"]["configured"] is False
    assert "sk-" not in str(integrations) and {s["name"] for s in integrations["sources"]} >= {"greenhouse", "workday", "adzuna"}
    health = await (await world.client()).get("/api/health")
    assert health.status_code == 200 and health.json()["database"] is True
    assert (await admin.post("/api/notifications/read-all")).status_code == 200


async def test_postgres_index_contract(database):
    index = await PgJobIndex(PLAIN_URL).connect()
    try:
        await index.pool.execute("TRUNCATE index_postings, index_units, index_boards, index_meta RESTART IDENTITY CASCADE")

        def job(n, title):
            return Job(title=title, company="Acme", url=f"https://acme.example/{n}", source="Greenhouse", ats="greenhouse", ats_id=str(n))

        rows = [(f"greenhouse:{n}", t, tokens, "sig", pack(job(n, t))) for n, t, tokens in
                ((1, "AI Engineer", "ai engineer"), (2, "ML Engineer", "ml engineer"), (3, "Account Executive", "account executive"))]
        await index.replace_unit("greenhouse", "acme", "", rows, ttl=3600)
        found = await index.candidates(["ai", "ml"], [])
        assert sorted(r[1] for r in found) == ["greenhouse:1", "greenhouse:2"] and all(r[3] == "greenhouse" for r in found)
        assert {r[1] for r in await index.candidates([], ["greenhouse:3"])} == {"greenhouse:3"}
        assert await index.candidates(["ai'); DROP TABLE index_units;--"], []) != None      # tokens are sanitized, never interpolated
        loaded = await index.load_postings([r[0] for r in found])
        assert sorted(j.title for j in loaded) == ["AI Engineer", "ML Engineer"] and all(j.row_id for j in loaded)
        loaded[0].description = "enriched"
        await index.update_postings(loaded[:1])
        assert (await index.load_postings([loaded[0].row_id]))[0].description == "enriched"
        assert (await index.unit_rows("greenhouse", "acme", ""))["greenhouse:1"][0] == "sig"
        assert await index.live_akeys(["greenhouse:1", "greenhouse:404"]) == {"greenhouse:1"}
        await index.replace_unit("greenhouse", "acme", "", rows[:1], ttl=3600)               # refresh: removed postings disappear
        stats = await index.index_stats()
        assert stats["stored_postings"] == 1 and stats["fresh_postings"] == 1 and stats["by_source"]["greenhouse"]["units"] == 1
        await index.delete_akeys(["greenhouse:1"])
        assert await index.live_akeys(["greenhouse:1"]) == set()
        await index.fail_unit("lever", "beta", "", "HTTP 500", retry_after=60)
        states = await index.unit_states()
        assert states[("lever", "beta", "")][2] == 0 and states[("greenhouse", "acme", "")][2] == 1
        assert states[("lever", "beta", "")][1] > time.time()
        await index.record_board("ashby", "newco", True, 12, "ai")
        await index.record_board("ashby", "deadco", False, 0, "ai")
        assert await index.known_boards() == [("ashby", "newco")] and await index.dead_boards() == {("ashby", "deadco")}
        await index.meta_set("wd_us_facet:x|wd1|S", '{"a": ["b"]}')
        await index.meta_set("other", "1")
        assert await index.meta_prefix("wd_us_facet:") == {"x|wd1|S": '{"a": ["b"]}'}
        assert unpack(pack(job(9, "X"))).title == "X"
    finally:
        await index.close()
