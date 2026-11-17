"""Role isolation, assignments, application tracking and dashboard accuracy."""
import uuid

from sqlalchemy import select

from app.portal.models import ApplicationEvent, AuditLog, JobAssignment
from tests.portal.conftest import add_job


async def test_team_lead_cannot_reach_another_team(world):
    tl = await world.as_tl("alpha")
    beta_team, beta_member, beta_candidate = world.ids["team_beta"], world.ids["member_b1"], world.ids["cand_b1"]
    for path in (f"/api/teams/{beta_team}", f"/api/teams/{beta_team}/members", f"/api/teams/{beta_team}/analytics",
                 f"/api/teams/{beta_team}/queue", f"/api/users/{beta_member}",
                 f"/api/users/{beta_member}/dashboard", f"/api/users/{beta_member}/activity",
                 f"/api/analytics/member/{beta_member}"):
        response = await tl.get(path)
        assert response.status_code == 403, (path, response.status_code)
        assert "Member B1" not in response.text and "member.b1" not in response.text
    # Profile and resume now live on the candidate: another team's candidate does not exist for this Team Lead.
    for path in (f"/api/candidates/{beta_candidate}", f"/api/candidates/{beta_candidate}/profile", f"/api/candidates/{beta_candidate}/resume",
                 f"/api/candidates/{beta_candidate}/resume/download", f"/api/candidates/{beta_candidate}/analytics",
                 f"/api/candidates/{beta_candidate}/activity"):
        response = await tl.get(path)
        assert response.status_code == 404, (path, response.status_code)
        assert "Candidate B1" not in response.text and "candidate.b1" not in response.text and "Member B1" not in response.text
    candidates = (await tl.get("/api/candidates")).json()
    assert {c["name"] for c in candidates["items"]} == {"Candidate A1", "Candidate A2"}
    assert (await tl.get("/api/candidates", params={"team_id": str(beta_team)})).json()["items"] == []  # the filter cannot widen the scope
    listed = (await tl.get("/api/users")).json()
    assert {u["email"] for u in listed["items"]} == {"member.a1@example.com", "member.a2@example.com"}
    assert (await tl.get("/api/users", params={"team_id": str(beta_team)})).json()["items"] == []     # the filter cannot widen the scope
    assert [t["name"] for t in (await tl.get("/api/teams")).json()] == ["Team Alpha"]
    assert (await tl.patch(f"/api/users/{beta_member}", json={"is_active": False})).status_code == 403
    assert (await tl.patch(f"/api/users/{world.ids['member_a1']}", json={"team_id": str(beta_team)})).status_code == 403
    assert (await tl.patch(f"/api/candidates/{beta_candidate}/profile", json={"skills": ["x"]})).status_code == 404
    upload = await tl.post(f"/api/candidates/{beta_candidate}/resume", files={"file": ("r.txt", b"x" * 100)})
    assert upload.status_code == 404
    assert (await tl.patch(f"/api/candidates/{beta_candidate}", json={"notes": "x"})).status_code == 404
    assert (await tl.delete(f"/api/candidates/{beta_candidate}/resume")).status_code == 404
    beta_lead = await world.as_tl("beta")                                              # nothing above changed the candidate
    assert (await beta_lead.get(f"/api/candidates/{beta_candidate}/profile")).json()["skills"] == ["Python", "PyTorch"]
    assert (await beta_lead.get(f"/api/candidates/{beta_candidate}/resume")).json()["version"] == 1


async def test_member_is_limited_to_their_own_data(world):
    member = await world.as_member("a1")
    for path in ("/api/users", "/api/teams", "/api/jobs", "/api/searches", "/api/invitations", "/api/analytics/admin", "/api/audit-logs",
                 "/api/settings", "/api/integrations", f"/api/teams/{world.ids['team_alpha']}/analytics", "/api/searches/options"):
        assert (await member.get(path)).status_code == 403, path
    other = world.ids["member_a2"]
    for path in (f"/api/users/{other}", f"/api/users/{other}/dashboard"):
        assert (await member.get(path)).status_code == 403, path
    # A candidate assigned to another member (same team) does not exist for this member: reading and writing answer 404.
    own_candidate, other_candidate = world.ids["cand_a1"], world.ids["cand_a2"]
    for path in (f"/api/candidates/{other_candidate}", f"/api/candidates/{other_candidate}/profile", f"/api/candidates/{other_candidate}/resume",
                 f"/api/candidates/{other_candidate}/resume/download"):
        response = await member.get(path)
        assert response.status_code == 404, path
        assert "Candidate A2" not in response.text and "candidate.a2" not in response.text
    assert (await member.patch(f"/api/candidates/{other_candidate}/profile", json={"skills": ["x"]})).status_code == 404
    assert (await member.post(f"/api/candidates/{other_candidate}/resume", files={"file": ("r.txt", b"x" * 100)})).status_code == 404
    assert [c["name"] for c in (await member.get("/api/candidates")).json()["items"]] == ["Candidate A1"]
    assert (await member.post("/api/searches", json={"candidate_ids": [str(own_candidate)]})).status_code == 403
    assert (await member.post("/api/jobs", json={"company": "X", "title": "Engineer"})).status_code == 403
    assert (await member.patch(f"/api/users/{world.ids['member_a1']}", json={"team_id": str(world.ids["team_beta"])})).status_code == 403
    assert (await member.patch(f"/api/users/{world.ids['member_a1']}", json={"is_active": False})).status_code == 403
    # The member reads their own candidate's profile and resume, but only a manager writes them.
    own = await member.get(f"/api/candidates/{own_candidate}/profile")
    assert own.status_code == 200 and own.json()["desired_titles"] == ["AI Engineer"]
    assert (await member.get(f"/api/candidates/{own_candidate}/resume")).json()["version"] == 1
    profile_change = {"preferred_states": ["TX"], "skills": ["Python", "SQL"]}
    cv = {"file": ("cv.txt", ("Python engineer. " * 20).encode())}
    assert (await member.patch(f"/api/candidates/{own_candidate}/profile", json=profile_change)).status_code == 403
    assert (await member.post(f"/api/candidates/{own_candidate}/resume", files=cv)).status_code == 403
    assert (await member.post(f"/api/candidates/{own_candidate}/resume", files={"file": ("cv.exe", b"MZ" * 50)})).status_code == 403
    assert (await member.delete(f"/api/candidates/{own_candidate}/resume")).status_code == 403
    assert (await member.patch(f"/api/candidates/{own_candidate}", json={"notes": "x"})).status_code == 403
    assert (await member.post("/api/candidates", json={"name": "New Candidate"})).status_code == 403
    unchanged = (await member.get(f"/api/candidates/{own_candidate}/profile")).json()
    assert unchanged["skills"] == ["Python", "PyTorch"] and (await member.get(f"/api/candidates/{own_candidate}/resume")).json()["version"] == 1
    tl = await world.as_tl("alpha")
    saved = await tl.patch(f"/api/candidates/{own_candidate}/profile", json=profile_change)
    assert saved.status_code == 200 and saved.json()["completion"] == 100 and saved.json()["skills"] == ["Python", "SQL"]
    resume = await tl.post(f"/api/candidates/{own_candidate}/resume", files=cv)
    assert resume.status_code == 201 and resume.json()["version"] == 2 and "Python" in resume.json()["parsed_profile"]["skills"]
    assert (await tl.post(f"/api/candidates/{own_candidate}/resume", files={"file": ("cv.exe", b"MZ" * 50)})).status_code == 400
    seen = await member.get(f"/api/candidates/{own_candidate}/profile")                # the member sees what the Team Lead saved
    assert seen.json()["skills"] == ["Python", "SQL"] and seen.json()["completion"] == 100
    assert (await member.get(f"/api/candidates/{own_candidate}/resume")).json()["version"] == 2
    anonymous = await world.client()
    assert (await anonymous.get("/api/users")).status_code == 401


async def test_assignment_rules_and_scope(world):
    job = await add_job(world)
    admin, tl_a, tl_b = await world.as_admin(), await world.as_tl("alpha"), await world.as_tl("beta")
    a1, a2, b1 = (str(world.ids[k]) for k in ("cand_a1", "cand_a2", "cand_b1"))
    # A job the Team Lead has no relation to is not assignable by them (and not visible).
    assert (await tl_a.post("/api/assignments", json={"job_id": job, "candidate_id": a1})).status_code == 403
    assert (await tl_a.get(f"/api/jobs/{job}")).status_code == 403
    # Admin delegates to Team Alpha's queue; now its Team Lead can distribute it.
    queued = await admin.post(f"/api/teams/{world.ids['team_alpha']}/queue", json={"job_ids": [job]})
    assert queued.status_code == 201
    assert (await tl_b.get(f"/api/teams/{world.ids['team_alpha']}/queue")).status_code == 403
    queue = (await tl_a.get(f"/api/teams/{world.ids['team_alpha']}/queue")).json()
    assert queue["total"] == 1 and queue["items"][0]["delegated_by"]["name"] == "Ada Admin"
    assert (await tl_a.post("/api/assignments", json={"job_id": job, "candidate_id": b1})).status_code == 403  # other team's candidate
    done = await tl_a.post("/api/assignments/bulk", json={"job_ids": [job], "candidate_ids": [a1, a2], "queue_item_ids": [queue["items"][0]["id"]]})
    assert done.status_code == 201 and done.json()["created"] == 2
    again = await tl_a.post("/api/assignments/bulk", json={"job_ids": [job], "candidate_ids": [a1, a2]})
    assert again.json() == {"created": 0, "already_assigned": 2, "assignment_ids": []}                         # never duplicated
    assert (await tl_a.get(f"/api/teams/{world.ids['team_alpha']}/queue")).json()["total"] == 0
    # Admin assigns the same job directly to a candidate of another team: allowed, one row per (job, candidate).
    direct = await admin.post("/api/assignments", json={"job_id": job, "candidate_id": b1})
    assert direct.status_code == 201 and direct.json()["created"] == 1
    async with world.sessions() as db:
        rows = (await db.execute(select(JobAssignment).where(JobAssignment.job_id == uuid.UUID(job)))).scalars().all()
    assert len(rows) == 3 and len({r.candidate_id for r in rows}) == 3
    beta_items, alpha_items = (await tl_b.get("/api/assignments")).json()["items"], (await tl_a.get("/api/assignments")).json()["items"]
    assert {a["candidate"]["name"] for a in beta_items} == {"Candidate B1"}
    assert {a["candidate"]["name"] for a in alpha_items} == {"Candidate A1", "Candidate A2"}
    assert {a["member"]["name"] for a in beta_items} == {"Member B1"}                                           # the responsible member
    assert {a["member"]["name"] for a in alpha_items} == {"Member A1", "Member A2"}
    view = (await admin.get(f"/api/jobs/{job}")).json()
    assert view["assignment_count"] == 3 and {a["team"]["name"] for a in view["assignees"]} == {"Team Alpha", "Team Beta"}
    assert len((await tl_a.get(f"/api/jobs/{job}")).json()["assignees"]) == 2                                   # only own team's assignees
    trail = (await admin.get("/api/audit-logs", params={"action": "assignments"})).json()
    assert trail["total"] == 2 and {t["actor"]["name"] for t in trail["items"]} == {"Lead Alpha", "Ada Admin"}


async def test_members_never_see_other_assignments_or_raw_links(world):
    job = await add_job(world, apply_url="https://careers.northwind.example/apply/777")
    admin = await world.as_admin()
    ids = (await admin.post("/api/assignments/bulk", json={"job_ids": [job], "candidate_ids": [str(world.ids["cand_a1"]), str(world.ids["cand_a2"])]})).json()["assignment_ids"]
    a1 = await world.as_member("a1")
    mine = (await a1.get("/api/assignments")).json()
    assert mine["total"] == 1 and mine["items"][0]["candidate"]["name"] == "Candidate A1" and mine["items"][0]["member"]["name"] == "Member A1"
    own_id = mine["items"][0]["id"]
    other_id = next(i for i in ids if i != own_id)
    assert (await a1.get(f"/api/assignments/{other_id}")).status_code == 403
    assert (await a1.get(f"/api/assignments/{other_id}/open-job")).status_code == 403
    assert (await a1.patch(f"/api/assignments/{other_id}/status", json={"status": "APPLIED"})).status_code == 403
    assert (await a1.post(f"/api/assignments/{other_id}/notes", json={"note": "hi"})).status_code == 403
    assert (await a1.get("/api/assignments", params={"member_id": str(world.ids["member_a2"])})).json()["total"] == 0
    assert (await a1.get("/api/assignments", params={"candidate_id": str(world.ids["cand_a2"])})).json()["total"] == 0
    detail = await a1.get(f"/api/assignments/{own_id}")
    assert detail.status_code == 200
    assert "northwind.example" not in detail.text and detail.json()["job_urls"] is None and detail.json()["other_assignees"] == []
    assert detail.json()["job"]["has_apply_link"] is True and "Responsibilities" in detail.json()["job"]["description"]
    tl = await world.as_tl("alpha")
    managed = (await tl.get(f"/api/assignments/{own_id}")).json()
    assert managed["job_urls"]["apply_url"].endswith("/apply/777") and len(managed["other_assignees"]) == 1


async def test_opening_a_link_is_not_applying_and_dashboards_agree(world, monkeypatch):
    async def public(url):
        return None
    monkeypatch.setattr("app.portal.routers.assignments.assert_public_url", public)
    admin, tl, member = await world.as_admin(), await world.as_tl("alpha"), await world.as_member("a1")
    a1, candidate = str(world.ids["member_a1"]), str(world.ids["cand_a1"])
    jobs = [await add_job(world, title=f"AI Engineer {n}") for n in range(5)]
    assert (await admin.post("/api/assignments/bulk", json={"job_ids": jobs, "candidate_ids": [candidate]})).json()["created"] == 5
    listing = (await member.get("/api/assignments", params={"sort": "oldest"})).json()["items"]
    assert [a["status"] for a in listing] == ["ASSIGNED"] * 5

    async def numbers():
        m = (await member.get(f"/api/users/{a1}/dashboard")).json()["stats"]
        t = (await tl.get(f"/api/teams/{world.ids['team_alpha']}/analytics")).json()["stats"]
        o = (await admin.get("/api/analytics/admin")).json()["stats"]
        assert m == t == o                                           # one definition of every metric, everywhere
        return m

    assert (await numbers()) | {} == {"assigned": 5, "not_applied": 5, "applied": 0, "interviews": 0, "offers": 0, "closed": 0,
                                      "viewed": 0, "link_opens": 0, "completion_rate": 0.0}
    # Opening three job links: tracked separately, nothing counts as applied.
    for a in listing[:3]:
        r = await member.get(f"/api/assignments/{a['id']}/open-job", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("https://careers.northwind.example/jobs/")
    after_open = await numbers()
    assert after_open["applied"] == 0 and after_open["not_applied"] == 5 and after_open["link_opens"] == 3 and after_open["viewed"] == 3
    # Confirming two applications.
    first = await member.patch(f"/api/assignments/{listing[0]['id']}/status", json={"status": "APPLIED", "reference": "REQ-123", "note": "Sent today"})
    assert first.status_code == 200 and first.json()["applied_at"] and first.json()["application_reference"] == "REQ-123"
    await member.patch(f"/api/assignments/{listing[1]['id']}/status", json={"status": "APPLIED"})
    applied = await numbers()
    assert (applied["assigned"], applied["applied"], applied["not_applied"], applied["completion_rate"]) == (5, 2, 3, 40.0)
    # Later stages keep the original submission date and are not double counted.
    original = first.json()["applied_at"]
    interview = await member.patch(f"/api/assignments/{listing[0]['id']}/status", json={"status": "INTERVIEWING"})
    offer = await member.patch(f"/api/assignments/{listing[0]['id']}/status", json={"status": "OFFER"})
    assert interview.json()["applied_at"] == offer.json()["applied_at"] == original
    staged = await numbers()
    assert (staged["applied"], staged["interviews"], staged["offers"], staged["not_applied"]) == (2, 1, 1, 3)
    # A skipped job leaves the eligible set; a mistaken "applied" can be corrected, with history kept.
    await member.patch(f"/api/assignments/{listing[4]['id']}/status", json={"status": "SKIPPED"})
    undone = await member.patch(f"/api/assignments/{listing[1]['id']}/status", json={"status": "VIEWED"})
    assert undone.json()["applied_at"] is None
    final = await numbers()
    assert (final["assigned"], final["applied"], final["not_applied"], final["closed"], final["completion_rate"]) == (5, 1, 3, 1, 25.0)
    history = (await member.get(f"/api/assignments/{listing[1]['id']}")).json()["events"]
    kinds = [(e["event_type"], e["old_status"], e["new_status"]) for e in reversed(history)]
    assert ("assigned", None, "ASSIGNED") in kinds and ("link_opened", "ASSIGNED", "VIEWED") in kinds
    assert ("status_changed", "VIEWED", "APPLIED") in kinds and ("status_changed", "APPLIED", "VIEWED") in kinds
    assert any(e["metadata"].get("correction") for e in history)
    assert (await member.patch(f"/api/assignments/{listing[2]['id']}/status", json={"status": "APPLIED", "applied_at": "2999-01-01T00:00:00Z"})).status_code == 422
    # Team Lead sees per-member numbers; a private manager note stays hidden from the member.
    row = next(m for m in (await tl.get(f"/api/teams/{world.ids['team_alpha']}/analytics")).json()["members"] if m["member"]["name"] == "Member A1")
    assert (row["stats"]["assigned"], row["stats"]["applied"], row["stats"]["link_opens"]) == (5, 1, 3)
    assert (await tl.post(f"/api/assignments/{listing[0]['id']}/notes", json={"note": "internal only", "is_private": True})).status_code == 201
    visible = (await member.get(f"/api/assignments/{listing[0]['id']}")).json()["notes"]
    assert [n["note"] for n in visible] == ["Sent today"]
    assert len((await tl.get(f"/api/assignments/{listing[0]['id']}")).json()["notes"]) == 2
    async with world.sessions() as db:
        assert (await db.execute(select(ApplicationEvent).where(ApplicationEvent.event_type == "link_opened"))).scalars().all().__len__() == 3
        assert "assignment.status_changed" in [a for (a,) in (await db.execute(select(AuditLog.action))).all()]
    activity = (await member.get(f"/api/users/{a1}/activity")).json()
    assert activity["total"] >= 12 and all(i["member"]["name"] == "Member A1" for i in activity["items"])
    assert all(i["candidate"]["name"] == "Candidate A1" for i in activity["items"])
    funnel = {p["name"]: p["value"] for p in (await member.get(f"/api/users/{a1}/dashboard")).json()["funnel"]}
    assert funnel == {"Assigned": 5, "Viewed": 4, "Applied": 1, "Interview": 1, "Offer": 1}
    csv_export = await tl.get("/api/assignments/export.csv")
    assert csv_export.status_code == 200 and csv_export.text.count("\n") == 6 and "Member A1" in csv_export.text
    assert "Candidate A1" in csv_export.text
    assert (await member.get("/api/assignments/export.csv")).status_code == 403


async def test_unsafe_job_links_are_never_redirected(world):
    admin, member = await world.as_admin(), await world.as_member("a1")
    job = await add_job(world, canonical_url="http://127.0.0.1:8000/internal", apply_url="")
    assignment = (await admin.post("/api/assignments", json={"job_id": job, "candidate_id": str(world.ids["cand_a1"])})).json()["assignment_ids"][0]
    blocked = await member.get(f"/api/assignments/{assignment}/open-job", follow_redirects=False)
    assert blocked.status_code == 409 and "location" not in blocked.headers
    # The destination only ever comes from the stored job: query parameters cannot redirect elsewhere.
    tampered = await member.get(f"/api/assignments/{assignment}/open-job", params={"url": "https://evil.example", "next": "https://evil.example"}, follow_redirects=False)
    assert tampered.status_code == 409
    assert (await admin.post("/api/jobs", json={"company": "X", "title": "Engineer", "canonical_url": "javascript:alert(1)"})).status_code == 422


async def test_transfer_deactivation_and_team_management(world):
    admin, tl_a, tl_b = await world.as_admin(), await world.as_tl("alpha"), await world.as_tl("beta")
    a1, cand_a1, cand_a2 = str(world.ids["member_a1"]), str(world.ids["cand_a1"]), str(world.ids["cand_a2"])
    job = await add_job(world)
    assert (await admin.post("/api/assignments", json={"job_id": job, "candidate_id": cand_a1})).status_code == 201
    old_member = await world.as_member("a1")
    assert (await old_member.get("/api/assignments")).json()["total"] == 1
    assert (await tl_a.get("/api/assignments", params={"member_id": a1})).json()["total"] == 1
    moved = await admin.patch(f"/api/users/{a1}", json={"team_id": str(world.ids["team_beta"])})
    assert moved.status_code == 200 and moved.json()["team"]["name"] == "Team Beta"
    assert (await tl_a.get(f"/api/users/{a1}")).status_code == 403                    # previous Team Lead lost access
    # The pipeline does not move with the member: the candidate stays in Team Alpha, unassigned, with the job.
    kept = await tl_a.get(f"/api/candidates/{cand_a1}")
    assert kept.status_code == 200 and kept.json()["team"]["name"] == "Team Alpha" and kept.json()["assigned_member"] is None
    assert (await tl_b.get(f"/api/candidates/{cand_a1}")).status_code == 404
    assert (await tl_b.get("/api/assignments", params={"member_id": a1})).json()["total"] == 0
    assert (await tl_a.get("/api/assignments", params={"member_id": a1})).json()["total"] == 0   # nobody is responsible for it now
    assert (await tl_b.get("/api/assignments")).json()["total"] == 0
    assert (await tl_b.get("/api/assignments", params={"candidate_id": cand_a1})).json()["total"] == 0
    still = (await tl_a.get("/api/assignments", params={"candidate_id": cand_a1})).json()
    assert still["total"] == 1 and still["items"][0]["candidate"]["name"] == "Candidate A1" and still["items"][0]["member"] is None
    assert still["items"][0]["team"]["name"] == "Team Alpha" and still["items"][0]["job"]["id"] == job
    assert (await tl_a.get("/api/assignments")).json()["total"] == 1
    for client in (old_member, await world.as_member("a1")):                           # the session from before the move and a new one
        assert (await client.get("/api/assignments")).json()["total"] == 0
        assert (await client.get("/api/candidates")).json()["total"] == 0
        assert (await client.get(f"/api/candidates/{cand_a1}")).status_code == 404
    # Deactivation is logical: sessions end, history stays, the account can be restored.
    member = await world.as_member("a2")
    assert (await admin.post("/api/assignments", json={"job_id": job, "candidate_id": cand_a2})).status_code == 201
    off = await tl_a.patch(f"/api/users/{world.ids['member_a2']}", json={"is_active": False})
    assert off.status_code == 200 and off.json()["status"] == "deactivated"
    assert (await member.get("/api/auth/me")).status_code == 401
    # The deactivated member's candidate stays in the team with its job, unassigned; the member cannot be given a candidate.
    left = (await tl_a.get(f"/api/candidates/{cand_a2}")).json()
    assert left["team"]["name"] == "Team Alpha" and left["assigned_member"] is None and left["is_active"] is True
    assert (await tl_a.get("/api/assignments", params={"candidate_id": cand_a2})).json()["total"] == 1
    assert (await tl_a.get("/api/assignments", params={"member_id": str(world.ids["member_a2"])})).json()["total"] == 0
    assert (await admin.patch(f"/api/candidates/{cand_a2}", json={"assigned_member_id": str(world.ids["member_a2"])})).status_code == 409
    # A deactivated candidate cannot receive jobs.
    other_job = await add_job(world)
    assert (await admin.patch(f"/api/candidates/{cand_a2}", json={"is_active": False})).json()["is_active"] is False
    assert (await admin.post("/api/assignments", json={"job_id": other_job, "candidate_id": cand_a2})).status_code == 409
    assert (await admin.patch(f"/api/candidates/{cand_a2}", json={"is_active": True})).json()["is_active"] is True
    assert (await admin.patch(f"/api/users/{world.ids['admin']}", json={"is_active": False})).status_code == 409
    # A lead may restore a member the lead deactivated, but not one an Admin deactivated.
    assert (await tl_a.patch(f"/api/users/{world.ids['member_a2']}", json={"is_active": True})).json()["status"] == "active"
    assert (await admin.patch(f"/api/users/{world.ids['member_a2']}", json={"is_active": False})).status_code == 200
    assert (await tl_a.patch(f"/api/users/{world.ids['member_a2']}", json={"is_active": True})).status_code == 403
    assert (await admin.patch(f"/api/users/{world.ids['member_a2']}", json={"is_active": True})).json()["status"] == "active"
    assert (await tl_a.patch(f"/api/teams/{world.ids['team_alpha']}", json={"name": "Mine"})).status_code == 403     # leads do not edit teams
    renamed = await admin.patch(f"/api/teams/{world.ids['team_alpha']}", json={"name": "Team Apex", "is_active": False})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Team Apex" and renamed.json()["is_active"] is False
    assert (await admin.patch(f"/api/teams/{world.ids['team_beta']}", json={"name": "team apex"})).status_code == 409
    # The lead of a deactivated team can still read, but changes nothing until an Admin reactivates the team.
    assert (await tl_a.post("/api/assignments", json={"job_id": other_job, "candidate_id": cand_a2})).status_code == 409
    assert (await tl_a.post("/api/invitations", json={"name": "Late Joiner", "email": "late.joiner@example.com"})).status_code == 409
    assert (await tl_a.post("/api/searches", json={"candidate_ids": [cand_a2]})).status_code == 409
    assert (await tl_a.get(f"/api/teams/{world.ids['team_alpha']}/members")).status_code == 200
    assert (await tl_a.post("/api/auth/change-password", json={"current_password": "wrong", "new_password": "x"})).status_code in (403, 422)   # own account still works
    swapped = await admin.patch(f"/api/teams/{world.ids['team_alpha']}", json={"remove_team_lead": True})
    assert swapped.json()["team_lead"] is None
    assert (await tl_a.get("/api/users")).json()["items"] == []                       # a lead without a team sees nobody


async def test_account_creation_and_team_analytics_have_a_second_address_with_the_same_rules(world):
    admin, tl_a, member = await world.as_admin(), await world.as_tl("alpha"), await world.as_member("a1")
    alpha, beta = str(world.ids["team_alpha"]), str(world.ids["team_beta"])
    # POST /api/users creates an invited account (no password is ever set for someone else).
    assert (await tl_a.post("/api/users", json={"name": "New Lead", "email": "new.lead@example.com", "role": "TEAM_LEAD"})).status_code == 403
    assert (await tl_a.post("/api/users", json={"name": "Other Team", "email": "other.team@example.com", "team_id": beta})).status_code == 403
    made = await tl_a.post("/api/users", json={"name": "New Person", "email": "new.person@example.com"})
    assert made.status_code == 201 and made.json()["role"] == "MEMBER" and made.json()["team"]["id"] == alpha      # the lead's own team
    assert "token=" in made.json()["invite_url"] and made.json()["email_sent"] is False
    assert (await member.post("/api/users", json={"name": "Nope Nope", "email": "nope@example.com"})).status_code == 403
    user = next(u for u in (await admin.get("/api/users", params={"q": "new.person"})).json()["items"])
    assert user["status"] == "invited"
    # GET /api/analytics/team/{id} answers exactly like GET /api/teams/{id}/analytics.
    assert (await tl_a.get(f"/api/analytics/team/{alpha}")).json() == (await tl_a.get(f"/api/teams/{alpha}/analytics")).json()
    assert (await tl_a.get(f"/api/analytics/team/{beta}")).status_code == 403
    assert (await member.get(f"/api/analytics/team/{alpha}")).status_code == 403
    assert (await admin.get(f"/api/analytics/team/{beta}")).status_code == 200
