"""Candidates: who may create, read, change and hand them over; team isolation; the resume file; what happens to a
member's candidates when the member is deactivated or leaves the team."""
import uuid

from sqlalchemy import select

from app.portal.models import AuditLog, Candidate, Resume
from tests.portal.conftest import add_job

PDF_TYPE = "application/pdf"


async def test_candidate_crud_and_permissions_per_role(world):
    admin, tl, member = await world.as_admin(), await world.as_tl("alpha"), await world.as_member("a1")
    alpha, beta, a1, a2 = (str(world.ids[k]) for k in ("team_alpha", "team_beta", "member_a1", "member_a2"))
    # Create: a Team Lead in their own team (team taken from the session), an Admin in a named team, a member never.
    made = await tl.post("/api/candidates", json={"name": "Nora Quist", "email": "Nora.Quist@Example.com", "phone": "+1 (555) 010-2000",
                                                  "notes": "Available from March", "assigned_member_id": a1})
    assert made.status_code == 201, made.text
    body = made.json()
    assert (body["name"], body["email"], body["phone"], body["notes"]) == ("Nora Quist", "nora.quist@example.com", "+1 (555) 010-2000", "Available from March")
    assert body["team"]["id"] == alpha and body["assigned_member"]["id"] == a1 and body["is_active"] is True
    assert body["has_resume"] is False and body["resume_has_file"] is False and body["stats"]["assigned"] == 0
    assert body["created_by"]["name"] == "Lead Alpha"
    cid = body["id"]
    assert (await admin.post("/api/candidates", json={"name": "No Team"})).status_code == 422            # an Admin has to name the team
    assert (await admin.post("/api/candidates", json={"name": "Ghost Team", "team_id": str(uuid.uuid4())})).status_code == 404
    by_admin = await admin.post("/api/candidates", json={"name": "Bea Beta", "team_id": beta})
    assert by_admin.status_code == 201 and by_admin.json()["assigned_member"] is None and by_admin.json()["email"] == ""
    assert (await member.post("/api/candidates", json={"name": "Not Allowed"})).status_code == 403
    assert (await tl.post("/api/candidates", json={"name": "Bad Phone", "phone": "call me <script>"})).status_code == 422
    assert (await tl.post("/api/candidates", json={"name": "Bad Mail", "email": "not-an-address"})).status_code == 422
    # Read: the member responsible for the candidate sees details (e-mail and phone are needed to apply).
    seen = await member.get(f"/api/candidates/{cid}")
    assert seen.status_code == 200 and seen.json()["email"] == "nora.quist@example.com" and seen.json()["phone"] == "+1 (555) 010-2000"
    assert seen.json()["created_by"]["email"] == ""                                                       # not the lead's sign-in address
    assert {c["name"] for c in (await member.get("/api/candidates")).json()["items"]} == {"Candidate A1", "Nora Quist"}
    # Change: managers only; only what was sent changes; an empty e-mail removes it.
    for change in ({"name": "Renamed"}, {"notes": "x"}, {"assigned_member_id": a2}, {"assigned_member_id": None}, {"is_active": False}):
        assert (await member.patch(f"/api/candidates/{cid}", json=change)).status_code == 403, change
    edited = await tl.patch(f"/api/candidates/{cid}", json={"phone": "555 0199", "email": ""})
    assert edited.status_code == 200 and (edited.json()["phone"], edited.json()["email"], edited.json()["name"]) == ("555 0199", "", "Nora Quist")
    assert edited.json()["assigned_member"]["id"] == a1 and edited.json()["notes"] == "Available from March"   # untouched fields stay
    # Deactivate: logical. No searches and no new jobs until reactivated; nothing is deleted.
    job = await add_job(world)
    off = await tl.patch(f"/api/candidates/{cid}", json={"is_active": False})
    assert off.status_code == 200 and off.json()["is_active"] is False
    assert (await admin.post("/api/assignments", json={"job_id": job, "candidate_id": cid})).status_code == 409
    assert (await tl.post("/api/searches", json={"candidate_ids": [cid]})).status_code == 409
    assert (await tl.get("/api/candidates", params={"active": False})).json()["total"] == 1
    assert (await tl.get("/api/candidates", params={"active": True})).json()["total"] == 2
    assert (await tl.patch(f"/api/candidates/{cid}", json={"is_active": True})).json()["is_active"] is True
    # List filters.
    assert {c["name"] for c in (await tl.get("/api/candidates", params={"assigned_member_id": a1})).json()["items"]} == {"Candidate A1", "Nora Quist"}
    assert [c["name"] for c in (await tl.get("/api/candidates", params={"has_resume": False})).json()["items"]] == ["Nora Quist"]
    assert [c["name"] for c in (await tl.get("/api/candidates", params={"q": "nora"})).json()["items"]] == ["Nora Quist"]
    assert (await tl.get("/api/candidates", params={"unassigned": True})).json()["total"] == 0
    assert [c["name"] for c in (await admin.get("/api/candidates", params={"unassigned": True})).json()["items"]] == ["Bea Beta"]
    assert (await admin.get("/api/candidates")).json()["total"] == 6
    # Every change is in the audit trail.
    async with world.sessions() as db:
        actions = [a for (a,) in (await db.execute(select(AuditLog.action).where(AuditLog.target_id == cid).order_by(AuditLog.created_at))).all()]
    assert actions == ["candidate.created", "candidate.assigned", "candidate.updated", "candidate.deactivated", "candidate.activated"]


async def test_candidates_are_isolated_by_team_and_assignment_stays_inside_the_team(world):
    admin, tl_a, tl_b = await world.as_admin(), await world.as_tl("alpha"), await world.as_tl("beta")
    alpha, beta = str(world.ids["team_alpha"]), str(world.ids["team_beta"])
    cand_a1, cand_b1 = str(world.ids["cand_a1"]), str(world.ids["cand_b1"])
    assert {c["name"] for c in (await tl_a.get("/api/candidates")).json()["items"]} == {"Candidate A1", "Candidate A2"}
    assert (await tl_a.get("/api/candidates", params={"team_id": beta})).json()["items"] == []            # a filter cannot widen the scope
    # Another team's candidate answers exactly like one that does not exist.
    for target in (cand_b1, str(uuid.uuid4())):
        for path in ("", "/profile", "/resume", "/resume/download", "/analytics", "/activity"):
            response = await tl_a.get(f"/api/candidates/{target}{path}")
            assert response.status_code == 404 and "Candidate B1" not in response.text, path
        assert (await tl_a.patch(f"/api/candidates/{target}", json={"notes": "mine now"})).status_code == 404
        assert (await tl_a.patch(f"/api/candidates/{target}/profile", json={"skills": ["x"]})).status_code == 404
        assert (await tl_a.post(f"/api/candidates/{target}/resume", files={"file": ("r.txt", b"Python " * 40)})).status_code == 404
        assert (await tl_a.delete(f"/api/candidates/{target}/resume")).status_code == 404
    assert (await tl_a.post("/api/candidates", json={"name": "Wrong Team", "team_id": beta})).status_code == 403
    assert (await tl_a.patch(f"/api/candidates/{cand_a1}", json={"team_id": beta})).status_code == 403    # only an Admin moves candidates
    # A candidate goes to an active MEMBER of its own team, for a Team Lead and for an Admin alike.
    for client in (tl_a, admin):
        for wrong in ("member_b1", "tl_alpha", "admin"):
            refused = await client.patch(f"/api/candidates/{cand_a1}", json={"assigned_member_id": str(world.ids[wrong])})
            assert refused.status_code == 422 and "own team" in refused.json()["error"], wrong
        assert (await client.patch(f"/api/candidates/{cand_a1}", json={"assigned_member_id": str(uuid.uuid4())})).status_code == 422
        assert (await client.post("/api/candidates", json={"name": "Cross Team", "team_id": alpha,
                                                           "assigned_member_id": str(world.ids["member_b1"])})).status_code == 422
    assert (await tl_a.get(f"/api/candidates/{cand_a1}")).json()["assigned_member"]["name"] == "Member A1"   # nothing changed
    async with world.sessions() as db:
        assert (await db.execute(select(Candidate).where(Candidate.name.in_(("Wrong Team", "Cross Team"))))).first() is None
    # An Admin moves a candidate to another team: jobs and history go along, the old team loses sight of it.
    job = await add_job(world)
    assert (await admin.post("/api/assignments", json={"job_id": job, "candidate_id": cand_a1})).status_code == 201
    moved = await admin.patch(f"/api/candidates/{cand_a1}", json={"team_id": beta})
    assert moved.status_code == 200 and moved.json()["team"]["id"] == beta and moved.json()["assigned_member"] is None
    assert (await tl_a.get(f"/api/candidates/{cand_a1}")).status_code == 404
    assert (await tl_a.get("/api/assignments", params={"candidate_id": cand_a1})).json()["total"] == 0
    assert (await tl_b.get("/api/assignments", params={"candidate_id": cand_a1})).json()["total"] == 1
    assert (await (await world.as_member("a1")).get("/api/assignments")).json()["total"] == 0


async def test_member_sees_only_own_candidates_and_loses_access_when_the_candidate_is_moved(world, monkeypatch):
    async def public(url):
        return None
    monkeypatch.setattr("app.portal.routers.assignments.assert_public_url", public)
    admin, tl = await world.as_admin(), await world.as_tl("alpha")
    m1, m2 = await world.as_member("a1"), await world.as_member("a2")
    cand = str(world.ids["cand_a1"])
    a1, a2 = str(world.ids["member_a1"]), str(world.ids["member_a2"])
    job = await add_job(world)
    assignment = (await admin.post("/api/assignments", json={"job_id": job, "candidate_id": cand})).json()["assignment_ids"][0]
    reads = ("", "/profile", "/resume", "/resume/download", "/analytics", "/activity")
    for path in reads:
        assert (await m1.get(f"/api/candidates/{cand}{path}")).status_code == 200, path
        assert (await m2.get(f"/api/candidates/{cand}{path}")).status_code == 404, path                    # 404, not 403: hidden
    assert (await m2.get(f"/api/assignments/{assignment}")).status_code == 403
    assert (await m1.patch(f"/api/assignments/{assignment}/status", json={"status": "APPLIED", "note": "sent"})).status_code == 200
    before = (await m1.get(f"/api/users/{a1}/dashboard")).json()
    assert before["stats"]["applied"] == 1 and [r["candidate"]["name"] for r in before["candidates"]] == ["Candidate A1"]
    assert (await m1.get(f"/api/candidates/{cand}/analytics")).json()["stats"] == before["stats"]

    # The Team Lead hands the candidate to another member: one responsible member at a time.
    moved = await tl.patch(f"/api/candidates/{cand}", json={"assigned_member_id": a2})
    assert moved.status_code == 200 and moved.json()["assigned_member"]["id"] == a2
    for path in reads:
        assert (await m1.get(f"/api/candidates/{cand}{path}")).status_code == 404, path
        assert (await m2.get(f"/api/candidates/{cand}{path}")).status_code == 200, path
    for call in (m1.get(f"/api/assignments/{assignment}"), m1.get(f"/api/assignments/{assignment}/open-job", follow_redirects=False),
                 m1.patch(f"/api/assignments/{assignment}/status", json={"status": "INTERVIEWING"}),
                 m1.post(f"/api/assignments/{assignment}/notes", json={"note": "still here"})):
        assert (await call).status_code == 403
    assert (await m1.get("/api/assignments")).json()["total"] == 0 and (await m1.get("/api/candidates")).json()["total"] == 0
    assert (await m1.get(f"/api/users/{a1}/dashboard")).json()["stats"]["assigned"] == 0
    assert (await m1.get(f"/api/users/{a1}/activity")).json()["total"] == 0
    # The new member has the job with its whole history, and can go on with it.
    taken = (await m2.get(f"/api/assignments/{assignment}")).json()
    assert taken["status"] == "APPLIED" and taken["candidate"]["name"] == "Candidate A1" and taken["member"]["name"] == "Member A2"
    assert [n["note"] for n in taken["notes"]] == ["sent"] and any(e["new_status"] == "APPLIED" for e in taken["events"])
    assert (await m2.patch(f"/api/assignments/{assignment}/status", json={"status": "INTERVIEWING"})).status_code == 200
    assert (await m2.get(f"/api/assignments/{assignment}/open-job", follow_redirects=False)).status_code == 303
    after = (await m2.get(f"/api/users/{a2}/dashboard")).json()
    assert (after["stats"]["assigned"], after["stats"]["applied"], after["stats"]["interviews"]) == (1, 1, 1)
    assert {r["candidate"]["name"] for r in after["candidates"]} == {"Candidate A1", "Candidate A2"}
    team = (await tl.get(f"/api/teams/{world.ids['team_alpha']}/analytics")).json()
    assert team["stats"] == after["stats"] == (await admin.get("/api/analytics/admin")).json()["stats"]     # numbers agree across roles
    assert {m["member"]["name"]: (m["candidates"], m["stats"]["assigned"]) for m in team["members"]} == {"Member A1": (0, 0), "Member A2": (2, 1)}
    assert {c["candidate"]["name"]: c["stats"]["assigned"] for c in team["candidates"]} == {"Candidate A1": 1, "Candidate A2": 0}
    notes = [n for n in (await m2.get("/api/notifications")).json()["items"] if n["type"] == "candidate_assigned"]
    assert len(notes) == 1 and notes[0]["payload"] == {"candidate_id": cand, "candidate_name": "Candidate A1", "assigned_by": "Lead Alpha"}
    # New jobs for the candidate are announced to the member responsible now, not to the previous one.
    await admin.post("/api/assignments", json={"job_id": await add_job(world), "candidate_id": cand})
    assert (await m2.get("/api/notifications")).json()["items"][0]["type"] == "candidate_jobs_assigned"
    assert sum(1 for n in (await m1.get("/api/notifications")).json()["items"] if n["type"] == "candidate_jobs_assigned") == 1
    # Unassigning takes it away from everyone but the managers.
    assert (await tl.patch(f"/api/candidates/{cand}", json={"assigned_member_id": None})).json()["assigned_member"] is None
    assert (await m2.get(f"/api/candidates/{cand}")).status_code == 404 and (await m2.get(f"/api/assignments/{assignment}")).status_code == 403
    assert [c["name"] for c in (await tl.get("/api/candidates", params={"unassigned": True})).json()["items"]] == ["Candidate A1"]
    assert (await tl.get(f"/api/assignments/{assignment}")).json()["member"] is None


async def test_resume_file_download_permissions_and_headers(world):
    admin, tl, tl_b = await world.as_admin(), await world.as_tl("alpha"), await world.as_tl("beta")
    m1, m2 = await world.as_member("a1"), await world.as_member("a2")
    cand = str(world.ids["cand_a1"])
    content = ("Senior Python engineer. PyTorch, SQL, AWS. " * 10).encode()
    hostile = '../../etc/pass"wd\r\nSet-Cookie: x=1; <b>résumé</b>.final.TXT'
    assert (await m1.post(f"/api/candidates/{cand}/resume", files={"file": ("cv.txt", content)})).status_code == 403
    up = await tl.post(f"/api/candidates/{cand}/resume", files={"file": (hostile, content, "text/html")})
    assert up.status_code == 201, up.text
    assert up.json()["has_file"] is True and up.json()["version"] == 2 and up.json()["candidate_id"] == cand and up.json()["size_bytes"] == len(content)
    assert (await tl.get(f"/api/candidates/{cand}")).json()["resume_has_file"] is True
    for client in (admin, tl, m1):                                   # exactly the people who may see the candidate
        got = await client.get(f"/api/candidates/{cand}/resume/download")
        assert got.status_code == 200 and got.content == content
        assert got.headers["content-type"] == "text/plain; charset=utf-8"        # by extension; never the type the uploader claimed
        assert got.headers["x-content-type-options"] == "nosniff" and "no-store" in got.headers["cache-control"]
        disposition = got.headers["content-disposition"]
        assert disposition.startswith('attachment; filename="') and disposition.endswith('.txt"')
        name = disposition[len('attachment; filename="'):-1]
        assert name and all(c.isascii() and (c.isalnum() or c in " ._()-") for c in name) and "/" not in name and ".." not in name
        assert "set-cookie" not in got.headers
    for client in (m2, tl_b, await world.as_member("b1")):
        assert (await client.get(f"/api/candidates/{cand}/resume/download")).status_code == 404
    assert (await (await world.client()).get(f"/api/candidates/{cand}/resume/download")).status_code == 401
    # The replaced version keeps its metadata only: neither its text nor its file.
    async with world.sessions() as db:
        old = (await db.execute(select(Resume.text, Resume.file_data).where(Resume.candidate_id == world.ids["cand_a1"], Resume.is_current.is_(False)))).one()
        assert old.text == "" and old.file_data is None
        # A resume uploaded before files were kept: the API says so and the download answers 404.
        await db.execute(Resume.__table__.update().where(Resume.candidate_id == world.ids["cand_a2"]).values(file_data=None))
        await db.commit()
    legacy = str(world.ids["cand_a2"])
    shown = (await m2.get(f"/api/candidates/{legacy}/resume")).json()
    assert shown["has_file"] is False and shown["text"] and (await tl.get(f"/api/candidates/{legacy}")).json()["resume_has_file"] is False
    missing = await m2.get(f"/api/candidates/{legacy}/resume/download")
    assert missing.status_code == 404 and "before files were kept" in missing.json()["error"]
    # PDF goes out as PDF; deleting removes text and file.
    import pymupdf
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "Python engineer with PyTorch and SQL experience", fontsize=10)
    pdf = document.tobytes()
    assert (await tl.post(f"/api/candidates/{legacy}/resume", files={"file": ("My CV (2026).pdf", pdf, "text/plain")})).status_code == 201
    got = await m2.get(f"/api/candidates/{legacy}/resume/download")
    assert got.content == pdf and got.headers["content-type"] == PDF_TYPE and got.headers["content-disposition"] == 'attachment; filename="My CV (2026).pdf"'
    assert (await m2.delete(f"/api/candidates/{legacy}/resume")).status_code == 403
    assert (await tl.delete(f"/api/candidates/{legacy}/resume")).status_code == 200
    assert (await tl.get(f"/api/candidates/{legacy}/resume")).json() is None
    assert (await m2.get(f"/api/candidates/{legacy}/resume/download")).status_code == 404
    assert (await tl.delete(f"/api/candidates/{legacy}/resume")).status_code == 404
    trail = (await admin.get("/api/audit-logs", params={"action": "resume."})).json()["items"]
    assert sorted(t["action"] for t in trail) == ["resume.deleted", "resume.uploaded", "resume.uploaded"]
    assert all(t["target_type"] == "candidate" for t in trail)
    assert (await admin.get("/api/audit-logs", params={"action": "resume.download"})).json()["total"] == 0   # downloads are not audited


async def test_members_candidates_become_unassigned_when_the_member_is_deactivated_or_moved(world):
    admin, tl_a, tl_b = await world.as_admin(), await world.as_tl("alpha"), await world.as_tl("beta")
    a1, a2 = str(world.ids["member_a1"]), str(world.ids["member_a2"])
    extra = (await tl_a.post("/api/candidates", json={"name": "Second Candidate", "assigned_member_id": a1})).json()["id"]
    job = await add_job(world)
    assert (await admin.post("/api/assignments/bulk", json={"job_ids": [job], "candidate_ids": [str(world.ids["cand_a1"]), extra]})).json()["created"] == 2
    team_before = (await tl_a.get(f"/api/teams/{world.ids['team_alpha']}/analytics")).json()
    assert team_before["unassigned_candidates"] == 0 and team_before["candidate_count"] == 3
    # Deactivated by the Team Lead: both candidates are unassigned; jobs and history stay with the candidates.
    assert (await tl_a.patch(f"/api/users/{a1}", json={"is_active": False})).status_code == 200
    unassigned = (await tl_a.get("/api/candidates", params={"unassigned": True})).json()
    assert {c["name"] for c in unassigned["items"]} == {"Candidate A1", "Second Candidate"}
    assert all(c["is_active"] and c["team"]["name"] == "Team Alpha" and c["stats"]["assigned"] == 1 for c in unassigned["items"])
    team = (await tl_a.get(f"/api/teams/{world.ids['team_alpha']}/analytics")).json()
    assert team["unassigned_candidates"] == 2 and team["stats"]["assigned"] == 2 and team["stats"] == team_before["stats"]
    assert (await tl_a.get("/api/teams")).json()[0]["unassigned_candidates"] == 2
    listed = next(u for u in (await tl_a.get("/api/users")).json()["items"] if u["id"] == a1)
    assert listed["candidate_count"] == 0 and listed["stats"]["assigned"] == 0 and "has_resume" not in listed and "profile_completion" not in listed
    # Reactivation does not hand them back by itself; a deactivated member cannot be given a candidate.
    assert (await admin.patch(f"/api/users/{a2}", json={"is_active": False})).status_code == 200
    assert (await tl_a.patch(f"/api/candidates/{extra}", json={"assigned_member_id": a2})).status_code == 409
    assert (await tl_a.patch(f"/api/users/{a1}", json={"is_active": True})).status_code == 200
    assert (await tl_a.get("/api/candidates", params={"unassigned": True})).json()["total"] == 3
    assert (await tl_a.patch(f"/api/candidates/{extra}", json={"assigned_member_id": a1})).json()["assigned_member"]["id"] == a1
    # Moved to another team by the Admin: the candidate stays in Team Alpha, unassigned; the member takes nothing along.
    member = await world.as_member("a1")
    assert (await member.get("/api/candidates")).json()["total"] == 1
    assert (await admin.patch(f"/api/users/{a1}", json={"team_id": str(world.ids["team_beta"])})).status_code == 200
    assert (await member.get("/api/candidates")).json()["total"] == 0 and (await member.get("/api/assignments")).json()["total"] == 0
    assert (await member.get(f"/api/candidates/{extra}")).status_code == 404
    assert (await tl_b.get(f"/api/candidates/{extra}")).status_code == 404
    still = (await tl_a.get(f"/api/candidates/{extra}")).json()
    assert still["assigned_member"] is None and still["team"]["name"] == "Team Alpha" and still["stats"]["assigned"] == 1
    assert (await tl_a.patch(f"/api/candidates/{extra}", json={"assigned_member_id": a1})).status_code == 422   # no longer of this team
    # One audit entry per candidate, with the reason; the Team Lead is told when someone else caused it.
    async with world.sessions() as db:
        rows = (await db.execute(select(AuditLog.target_id, AuditLog.meta).where(AuditLog.action == "candidate.unassigned"))).all()
    reasons = sorted((r.meta["reason"], r.meta["from_member"]) for r in rows)
    assert reasons == sorted([("member_deactivated", a1), ("member_deactivated", a1), ("member_deactivated", a2), ("member_moved_to_another_team", a1)])
    told = [n["payload"] for n in (await tl_a.get("/api/notifications")).json()["items"] if n["type"] == "candidates_unassigned"]
    assert sorted((p["reason"], p["count"], p["member_name"]) for p in told) == [("member_deactivated", 1, "Member A2"),
                                                                                  ("member_moved_to_another_team", 1, "Member A1")]


async def test_a_search_needs_no_responsible_member_and_records_the_resume_it_ran_with(world):
    from tests.portal.test_search_flow import wait_done
    tl = await world.as_tl("alpha")
    cand = str(world.ids["cand_a1"])
    assert (await tl.patch(f"/api/candidates/{cand}", json={"assigned_member_id": None})).status_code == 200
    started = await tl.post("/api/searches", json={"candidate_ids": [cand], "target_count": 3, "deep_search": False})
    assert started.status_code == 202 and started.json()["search"]["targets"][0]["resume_id"] is None      # not started yet
    run = await wait_done(world, tl, started.json()["search"]["id"])
    assert run["status"] == "completed" and run["assigned_count"] == 3
    current = (await tl.get(f"/api/candidates/{cand}/resume")).json()
    assert run["targets"][0]["resume_id"] == current["id"] and run["targets"][0]["candidate"] == {"id": cand, "name": "Candidate A1"}
    listed = (await tl.get("/api/assignments", params={"candidate_id": cand})).json()
    assert listed["total"] == 3 and all(a["member"] is None for a in listed["items"])
    assert (await (await world.as_member("a1")).get("/api/assignments")).json()["total"] == 0
    assert (await tl.get("/api/searches", params={"candidate_id": cand})).json()["total"] == 1
    assert (await tl.get("/api/searches", params={"candidate_id": str(world.ids["cand_a2"])})).json()["total"] == 0


async def test_candidate_cards_need_no_extra_requests_and_sort_by_work_to_do(world, monkeypatch):
    """The list carries what a card shows (profile summary, newest job) and orders by it in the list query itself."""
    admin, tl, member = await world.as_admin(), await world.as_tl("alpha"), await world.as_member("a1")
    a1, a2 = str(world.ids["cand_a1"]), str(world.ids["cand_a2"])
    empty = (await tl.post("/api/candidates", json={"name": "Aaron Empty"})).json()
    assert empty["last_assigned_at"] is None and empty["profile_summary"] == {
        "desired_titles": [], "years_experience": None, "work_authorization": "Other / Prefer Not to Say", "preferred_states": []}
    assert (await tl.patch(f"/api/candidates/{a1}/profile", json={"preferred_states": ["TX", "NY"], "years_experience": 4.5})).status_code == 200
    jobs = [await add_job(world, title=f"AI Engineer {n}") for n in range(3)]
    first = (await admin.post("/api/assignments/bulk", json={"job_ids": jobs, "candidate_ids": [a1]})).json()["assignment_ids"]
    later = (await admin.post("/api/assignments", json={"job_id": jobs[0], "candidate_id": a2})).json()["assignment_ids"][0]
    card = (await member.get(f"/api/candidates/{a1}")).json()
    assert card["profile_summary"] == {"desired_titles": ["AI Engineer"], "years_experience": 4.5,
                                       "work_authorization": "Green Card / US Permanent Resident", "preferred_states": ["TX", "NY"]}
    newest = max(a["assigned_at"] for a in (await tl.get("/api/assignments", params={"candidate_id": a1})).json()["items"])
    assert card["last_assigned_at"] == newest and card["stats"]["not_applied"] == 3
    listed = (await member.get("/api/candidates")).json()["items"]
    assert [(c["name"], c["last_assigned_at"], c["profile_summary"]) for c in listed] == [("Candidate A1", newest, card["profile_summary"])]

    async def order(sort, client=tl):
        response = await client.get("/api/candidates", params={"sort": sort})
        assert response.status_code == 200, response.text
        return [c["name"] for c in response.json()["items"]]

    assert await order("name") == ["Aaron Empty", "Candidate A1", "Candidate A2"]
    assert await order("newest_job") == ["Candidate A2", "Candidate A1", "Aaron Empty"]                  # no jobs: last
    assert await order("not_applied") == ["Candidate A1", "Candidate A2", "Aaron Empty"]
    # The orders follow the same definitions as the numbers: applied, skipped and withdrawn jobs are not "to do".
    assert (await member.patch(f"/api/assignments/{first[0]}/status", json={"status": "APPLIED"})).status_code == 200
    assert (await member.patch(f"/api/assignments/{first[1]}/status", json={"status": "SKIPPED"})).status_code == 200
    assert (await tl.delete(f"/api/assignments/{first[2]}")).status_code == 200
    after = {c["name"]: c for c in (await tl.get("/api/candidates")).json()["items"]}
    assert after["Candidate A1"]["stats"]["not_applied"] == 0 and after["Candidate A2"]["stats"]["not_applied"] == 1
    assert await order("not_applied") == ["Candidate A2", "Aaron Empty", "Candidate A1"]                 # ties by name
    # A withdrawn assignment is not the newest job any more.
    assert (await tl.delete(f"/api/assignments/{later}")).status_code == 200
    assert (await tl.get(f"/api/candidates/{a2}")).json()["last_assigned_at"] is None
    assert await order("newest_job") == ["Candidate A1", "Aaron Empty", "Candidate A2"]
    assert (await tl.get("/api/candidates", params={"sort": "id; drop table candidates"})).status_code == 422
    # Sorting never widens the scope, and paging keeps the order.
    assert await order("newest_job", member) == ["Candidate A1"]
    pages = [(await tl.get("/api/candidates", params={"sort": "newest_job", "page": p, "page_size": 1})).json() for p in (1, 2, 3)]
    assert [p["items"][0]["name"] for p in pages] == ["Candidate A1", "Aaron Empty", "Candidate A2"] and pages[0]["total"] == 3
    # One page of candidates costs the same number of statements whether it holds one candidate or many.
    from sqlalchemy import event
    counted = []

    def count(conn, cursor, statement, parameters, context, executemany):
        counted.append(statement)

    engine = world.app.state.db_engine.sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        await tl.get("/api/candidates", params={"sort": "newest_job", "page_size": 1})      # one candidate (with a resume)
        one = len(counted)
        counted.clear()
        await tl.get("/api/candidates", params={"sort": "newest_job", "page_size": 50})     # all three
        many = len(counted)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert one == many
