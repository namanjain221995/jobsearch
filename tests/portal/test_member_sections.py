"""The sections of a member's job list: To apply, Applied, In progress, Closed. Their sizes come from
GET /api/assignments/sections and must agree with the lists and with the member's dashboard."""
from tests.portal.conftest import add_job

SECTIONS = {"to_apply": "ASSIGNED,VIEWED,APPLYING", "applied": "APPLIED", "in_progress": "INTERVIEWING,OFFER", "closed": "REJECTED,WITHDRAWN,SKIPPED"}


async def assign(world, candidate: str, **job) -> str:
    admin = await world.as_admin()
    job_id = await add_job(world, **job)
    created = await admin.post("/api/assignments", json={"job_id": job_id, "candidate_id": str(world.ids[candidate])})
    assert created.status_code == 201, created.text
    return created.json()["assignment_ids"][0]


async def sections(client, **params) -> dict:
    answer = await client.get("/api/assignments/sections", params=params)
    assert answer.status_code == 200, answer.text
    return answer.json()


async def listed(client, section: str, **params) -> list[str]:
    answer = await client.get("/api/assignments", params={"status": SECTIONS[section], "page_size": 100, **params})
    assert answer.status_code == 200, answer.text
    return [item["id"] for item in answer.json()["items"]]


async def test_marking_a_job_as_applied_moves_it_between_sections_and_undo_moves_it_back(world):
    member = await world.as_member("a1")
    first = await assign(world, "cand_a1", title="AI Engineer", company="Northwind Labs", posted_at="2026-11-20")
    second = await assign(world, "cand_a1", title="Data Engineer", company="Contoso Data")
    third = await assign(world, "cand_a1", title="ML Engineer", company="Fabrikam AI")
    await assign(world, "cand_a2", title="Somebody Else's Job")                              # another member's candidate
    assert await sections(member) == {"to_apply": 3, "applied": 0, "in_progress": 0, "closed": 0, "total": 3}
    assert set(await listed(member, "to_apply")) == {first, second, third}
    # The posting's date reaches the card (null when the posting did not state one).
    cards = {i["id"]: i["job"]["posted_at"] for i in (await member.get("/api/assignments")).json()["items"]}
    assert cards[first] == "2026-11-20" and cards[second] is None

    applied = await member.patch(f"/api/assignments/{first}/status", json={"status": "APPLIED"})
    assert applied.status_code == 200 and applied.json()["applied_at"]
    assert await sections(member) == {"to_apply": 2, "applied": 1, "in_progress": 0, "closed": 0, "total": 3}
    assert first not in await listed(member, "to_apply") and await listed(member, "applied") == [first]
    # Undo: back to where it was; the mistaken application date is gone and the history says it was a correction.
    undone = await member.patch(f"/api/assignments/{first}/status", json={"status": "ASSIGNED"})
    assert undone.status_code == 200 and undone.json()["applied_at"] is None
    assert undone.json()["events"][0]["metadata"].get("correction") is True
    assert await sections(member) == {"to_apply": 3, "applied": 0, "in_progress": 0, "closed": 0, "total": 3}

    # Every later stage has its section; each job is in exactly one.
    for assignment, status in ((first, "APPLIED"), (second, "INTERVIEWING"), (third, "SKIPPED")):
        assert (await member.patch(f"/api/assignments/{assignment}/status", json={"status": status})).status_code == 200
    assert await sections(member) == {"to_apply": 0, "applied": 1, "in_progress": 1, "closed": 1, "total": 3}
    assert (await listed(member, "applied"), await listed(member, "in_progress"), await listed(member, "closed")) == ([first], [second], [third])
    assert (await member.patch(f"/api/assignments/{second}/status", json={"status": "OFFER"})).status_code == 200
    assert (await sections(member))["in_progress"] == 1
    assert (await member.patch(f"/api/assignments/{second}/status", json={"status": "REJECTED"})).status_code == 200
    assert await sections(member) == {"to_apply": 0, "applied": 1, "in_progress": 0, "closed": 2, "total": 3}


async def test_section_sizes_follow_the_filters_the_scope_and_the_dashboard(world):
    member, other, lead, admin = await world.as_member("a1"), await world.as_member("a2"), await world.as_tl("alpha"), await world.as_admin()
    mine = [await assign(world, "cand_a1", title="AI Engineer", company="Northwind Labs"),
            await assign(world, "cand_a1", title="Data Engineer", company="Contoso Data"),
            await assign(world, "cand_a1", title="Data Analyst", company="Contoso Data")]
    theirs = await assign(world, "cand_a2", title="Data Engineer", company="Contoso Data")
    await assign(world, "cand_b1", title="Data Engineer", company="Contoso Data")
    assert (await member.patch(f"/api/assignments/{mine[1]}/status", json={"status": "APPLIED"})).status_code == 200
    # Search and the candidate filter narrow the sections exactly as they narrow the list.
    assert await sections(member, q="data") == {"to_apply": 1, "applied": 1, "in_progress": 0, "closed": 0, "total": 2}
    assert await sections(member, q="northwind") == {"to_apply": 1, "applied": 0, "in_progress": 0, "closed": 0, "total": 1}
    assert await sections(member, candidate_id=str(world.ids["cand_a1"])) == {"to_apply": 2, "applied": 1, "in_progress": 0, "closed": 0, "total": 3}
    for section in SECTIONS:
        for params in ({}, {"q": "data"}, {"candidate_id": str(world.ids["cand_a1"])}):
            assert len(await listed(member, section, **params)) == (await sections(member, **params))[section]
    # A member's sections hold their own candidates only; a filter cannot widen that.
    assert (await sections(member, candidate_id=str(world.ids["cand_a2"])))["total"] == 0
    assert (await sections(member, member_id=str(world.ids["member_a2"])))["total"] == 0
    assert (await sections(member, team_id=str(world.ids["team_beta"])))["total"] == 0
    assert await sections(other) == {"to_apply": 1, "applied": 0, "in_progress": 0, "closed": 0, "total": 1}
    assert await listed(other, "to_apply") == [theirs]
    assert (await sections(lead))["total"] == 4 and (await sections(admin))["total"] == 5
    assert (await sections(lead, team_id=str(world.ids["team_beta"])))["total"] == 0
    assert (await (await world.client()).get("/api/assignments/sections")).status_code == 401
    # The dashboard's "not applied" is the "To apply" section; its "applied" counts every confirmed submission.
    assert (await member.patch(f"/api/assignments/{mine[2]}/status", json={"status": "INTERVIEWING"})).status_code == 200
    stats = (await member.get(f"/api/users/{world.ids['member_a1']}/dashboard")).json()["stats"]
    now = await sections(member)
    assert stats["not_applied"] == now["to_apply"] == 1
    assert stats["applied"] == now["applied"] + now["in_progress"] == 2
    assert stats["assigned"] == now["total"] == 3
    # A job a manager took back is in no section.
    assert (await lead.delete(f"/api/assignments/{mine[0]}")).status_code == 200
    assert await sections(member) == {"to_apply": 0, "applied": 1, "in_progress": 1, "closed": 0, "total": 2}
