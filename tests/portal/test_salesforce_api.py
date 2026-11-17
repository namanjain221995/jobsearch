"""Salesforce through the portal API: who may search and import, the scope setting, import and its resume, duplicates,
team isolation and the refresh rules. The real client runs against a synthetic org (tests/portal/sf_org.py)."""
import uuid

from sqlalchemy import select

from app.portal.models import AuditLog, Candidate, Resume
from app.portal.services import salesforce as sf
from app.portal.services import salesforce_sync as sync
from tests.portal.sf_org import INSTANCE, install, sf_id, standard_org
from tests.salesforce_samples import make_pdf

ANA, ANABEL, ANDRE, ANNA, ANTON, ANYA, ANSELM, ANNIKA, BUSINESS = (sf_id("001", n) for n in range(1, 10))
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def bring_in(client, salesforce_id: str, **body):
    return await client.post("/api/candidates/import-salesforce", json={"salesforce_id": salesforce_id, **body})


async def test_members_never_reach_salesforce_and_without_settings_nothing_is_asked(world):
    admin, tl, member = await world.as_admin(), await world.as_tl("alpha"), await world.as_member("a1")
    anonymous = await world.client()
    cand = str(world.ids["cand_a1"])
    # Not configured (the default): the status says so, searching answers 503, nothing breaks.
    for client in (admin, tl):
        status = await client.get("/api/salesforce/status")
        assert status.status_code == 200 and status.json() == {"configured": False, "connected": None, "checked_at": None, "api_version": "v68.0",
                                                               "message": "Salesforce is not configured on this server.", "team_lead_scope": "all"}
    assert (await admin.get("/api/integrations")).json()["salesforce"]["configured"] is False
    off = await tl.get("/api/salesforce/candidates", params={"q": "ana"})
    assert off.status_code == 503 and off.json()["error"] == "Salesforce is not configured on this server."
    assert (await bring_in(tl, ANA)).status_code == 503
    assert (await tl.get("/api/salesforce/candidates")).json() == {"items": [], "limit_reached": False}
    org = standard_org()
    install(world, org)
    # A member is refused everywhere, before Salesforce is asked anything; so is a caller without a session.
    calls = [("GET", "/api/salesforce/status", None), ("GET", "/api/salesforce/candidates?q=ana", None),
             ("POST", "/api/candidates/import-salesforce", {"salesforce_id": ANA}), ("POST", f"/api/candidates/{cand}/salesforce-refresh", None)]
    for method, path, body in calls:
        assert (await member.request(method, path, json=body)).status_code == 403, path
        assert (await anonymous.request(method, path, json=body)).status_code == 401, path
    assert org.requests == []
    # Managers: connected, with one cheap check that is remembered.
    status = (await tl.get("/api/salesforce/status")).json()
    assert status["configured"] is True and status["connected"] is True and status["message"] == "Connected" and status["checked_at"]
    seen = len(org.requests)
    assert (await admin.get("/api/integrations")).json()["salesforce"]["connected"] is True and len(org.requests) == seen
    assert "token" not in str((await admin.get("/api/integrations")).json()["salesforce"]).lower()


async def test_search_lists_key_details_caches_and_is_rate_limited(world):
    org = standard_org()
    install(world, org)
    admin, tl = await world.as_admin(), await world.as_tl("alpha")
    found = await tl.get("/api/salesforce/candidates", params={"q": " an "})
    assert found.status_code == 200, found.text
    body = found.json()
    assert [i["name"] for i in body["items"]] == ["Ana Example", "Anabel Sample", "Andre Docx", "Anna Noresume", "Annika Twofiles",
                                                  "Anselm Wrongtype", "Anya Bigfile"] and body["limit_reached"] is False
    ana = body["items"][0]
    assert ana == {"salesforce_id": ANA, "name": "Ana Example", "email": "ana.1@example.com", "phone": "(555) 010-2001", "candidate_status": "Active",
                   "technology": "AI/ML Engineer", "niche": "AI ML Engineer", "work_authorization": "OPT", "visa_status": "STEM OPT Extension",
                   "years_experience": 4.0, "preferred_job_type": "Remote", "preferred_locations": "Texas, California",
                   "experience_level": "Mid-Senior_level", "assigned_recruiter": "Rita Recruiter", "marketing_team_lead": "Lead Alpha",
                   "has_resume": True, "imported_candidate_id": None, "imported_elsewhere": False, "importable": True}
    assert {i["name"]: i["has_resume"] for i in body["items"]}["Anna Noresume"] is False
    # Typing: fewer than two characters asks nothing; the same search within a few seconds is answered from memory.
    queries = len(org.queries)
    for q in ("", " ", "a", " a "):
        assert (await tl.get("/api/salesforce/candidates", params={"q": q})).json()["items"] == []
    assert (await tl.get("/api/salesforce/candidates", params={"q": "AN"})).json()["items"][0]["name"] == "Ana Example"
    assert len(org.queries) == queries
    # status=all adds the candidates that are not active; the limit is honoured and reported.
    everyone = (await admin.get("/api/salesforce/candidates", params={"q": "an", "status": "all"})).json()
    assert "Anton Hold" in [i["name"] for i in everyone["items"]] and "Ana Business Inc" not in [i["name"] for i in everyone["items"]]
    short = (await admin.get("/api/salesforce/candidates", params={"q": "an", "limit": 3})).json()
    assert len(short["items"]) == 3 and short["limit_reached"] is True
    for bad in ({"q": "an", "limit": 0}, {"q": "an", "limit": 26}, {"q": "an", "status": "placed"}, {"q": "x" * 81}):
        assert (await admin.get("/api/salesforce/candidates", params=bad)).status_code == 422, bad
    # Salesforce trouble is a plain 502/503, never Salesforce's own words.
    org.fail_with = 500
    broken = await admin.get("/api/salesforce/candidates", params={"q": "anab"})
    assert broken.status_code == 503 and "Zed" not in broken.text and broken.json()["error"].startswith("Salesforce is busy")
    org.fail_with = 400
    assert (await admin.get("/api/salesforce/candidates", params={"q": "anab"})).status_code == 502
    org.fail_with = 0
    # A budget per user and minute.
    world.settings.salesforce_searches_per_minute = 3
    other = await world.as_tl("beta")
    codes = [(await other.get("/api/salesforce/candidates", params={"q": f"an{n}"})).status_code for n in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    limited = await other.get("/api/salesforce/candidates", params={"q": "ana"})
    assert limited.status_code == 429 and limited.headers["retry-after"] and limited.json()["error"].startswith("Too many Salesforce searches")
    assert (await other.get("/api/salesforce/candidates", params={"q": "a"})).status_code == 200          # nothing is asked: not counted


async def test_scope_setting_decides_which_candidates_a_team_lead_finds(world):
    org = standard_org()
    install(world, org)
    admin, alpha, beta = await world.as_admin(), await world.as_tl("alpha"), await world.as_tl("beta")

    async def names(client):
        world.app.state.salesforce_cache.clear()
        return [i["name"] for i in (await client.get("/api/salesforce/candidates", params={"q": "ana"})).json()["items"]]

    assert (await admin.get("/api/settings")).json()["salesforce_team_lead_scope"] == "all"
    assert await names(alpha) == await names(beta) == await names(admin) == ["Ana Example", "Anabel Sample"]
    # Only an Admin changes the setting; it takes two values.
    assert (await alpha.patch("/api/settings", json={"salesforce_team_lead_scope": "own"})).status_code == 403
    assert (await admin.patch("/api/settings", json={"salesforce_team_lead_scope": "team"})).status_code == 422
    changed = await admin.patch("/api/settings", json={"salesforce_team_lead_scope": "own"})
    assert changed.status_code == 200 and changed.json()["salesforce_team_lead_scope"] == "own"
    assert (await alpha.get("/api/salesforce/status")).json()["team_lead_scope"] == "own"
    # own: the marketing team lead's e-mail in Salesforce must be the Team Lead's sign-in address (any case).
    assert await names(alpha) == ["Ana Example"] and await names(beta) == ["Anabel Sample"] and await names(admin) == ["Ana Example", "Anabel Sample"]
    assert any("Assigned_Marketing_Team_Lead__r.Email__c = 'tl.alpha@example.com'" in q for q in org.queries)
    # The same rule guards import and refresh: knowing an id is not enough.
    hidden = await bring_in(beta, ANA)
    assert hidden.status_code == 404 and hidden.json()["error"] == "Salesforce candidate not found"
    assert (await bring_in(alpha, ANABEL)).status_code == 404
    mine = await bring_in(alpha, ANA)
    assert mine.status_code == 201
    org.accounts[ANA]["Assigned_Marketing_Team_Lead__r"] = {"Name": "Lead Beta", "Email__c": "tl.beta@example.com"}     # reassigned in Salesforce
    assert (await alpha.post(f"/api/candidates/{mine.json()['id']}/salesforce-refresh")).status_code == 404
    assert (await admin.post(f"/api/candidates/{mine.json()['id']}/salesforce-refresh")).status_code == 200
    # The portal checks every returned candidate itself; it does not rely on the query alone.
    ana = await world.app.state.salesforce.get_candidate(ANA)
    assert sync.visible(ana, None) and sync.visible(ana, "tl.beta@example.com")
    assert not sync.visible(ana, "tl.alpha@example.com") and not sync.visible(ana, "")
    # A candidate with no team lead in Salesforce belongs to nobody's "own".
    org.accounts[ANNA]["Assigned_Marketing_Team_Lead__r"] = None
    assert (await bring_in(alpha, ANNA)).status_code == 404 and (await bring_in(admin, ANNA, team_id=str(world.ids["team_alpha"]))).status_code == 201
    back = await admin.patch("/api/settings", json={"salesforce_team_lead_scope": "all"})
    assert back.json()["salesforce_team_lead_scope"] == "all" and "Anabel Sample" in await names(alpha)


async def test_import_creates_the_candidate_with_profile_resume_and_audit(world):
    org = standard_org()
    org.accounts[ANA].update({"Preferred_Job_Locations__c": "Texas, NJ, St. Louis (Open to relocate)", "Sponsorship_Required_For_VISA__c": True})
    install(world, org)
    admin, tl, member = await world.as_admin(), await world.as_tl("alpha"), await world.as_member("a1")
    alpha, a1 = str(world.ids["team_alpha"]), str(world.ids["member_a1"])
    made = await bring_in(tl, ANA, assigned_member_id=a1)
    assert made.status_code == 201, made.text
    body = made.json()
    cid = body["id"]
    assert (body["name"], body["email"], body["phone"]) == ("Ana Example", "ana.1@example.com", "(555) 010-2001")
    assert body["team"]["id"] == alpha and body["assigned_member"]["id"] == a1 and body["is_active"] is True and body["created_by"]["name"] == "Lead Alpha"
    assert (body["source"], body["salesforce_id"], body["salesforce_url"]) == ("salesforce", ANA, f"{INSTANCE}/{ANA}") and body["salesforce_synced_at"]
    assert body["notes"] == "Preferred job locations (from Salesforce): Texas, NJ, St. Louis (Open to relocate)"
    assert body["sync"] == {"created": True, "updated_fields": [], "kept_fields": [], "resume": "imported"}
    assert body["has_resume"] is True and body["resume_has_file"] is True
    profile = (await tl.get(f"/api/candidates/{cid}/profile")).json()
    assert (profile["desired_titles"], profile["years_experience"], profile["work_mode"], profile["work_authorization"], profile["sponsorship_needed"],
            profile["seniority"], profile["preferred_states"]) == (["AI/ML Engineer"], 4.0, "Remote", "Employment Authorization Document (EAD)", "Yes",
                                                                   "Mid-Senior level", ["TX", "NJ"])
    assert "Python" in profile["skills"] and profile["salary_min"] is None and profile["preferred_cities"] == []       # skills from the resume; nothing invented
    # The resume went through the normal pipeline: text extracted, file kept, marked as coming from Salesforce.
    resume = (await tl.get(f"/api/candidates/{cid}/resume")).json()
    assert (resume["source"], resume["filename"], resume["version"], resume["has_file"]) == ("salesforce", "Ana Example - Resume.pdf", 1, True)
    assert "Machine Learning Engineer" in resume["text"] and resume["uploaded_by"]["name"] == "Lead Alpha"
    download = await member.get(f"/api/candidates/{cid}/resume/download")
    assert download.status_code == 200 and download.content == org.files[sf_id("a0R", 1)][0]["data"] and download.headers["content-type"] == "application/pdf"
    async with world.sessions() as db:
        stored = (await db.execute(select(Resume).where(Resume.candidate_id == uuid.UUID(cid)))).scalar_one()
        assert (stored.source, stored.salesforce_content_version_id) == ("salesforce", sf_id("068", 1))
        actions = {a.action: a for a in (await db.execute(select(AuditLog).where(AuditLog.target_id == cid))).scalars()}
    assert set(actions) == {"candidate.imported_salesforce", "candidate.assigned", "resume.imported_salesforce"}
    assert actions["candidate.imported_salesforce"].meta["salesforce_id"] == ANA and actions["candidate.imported_salesforce"].meta["resume"] == "imported"
    assert "ana.1@example.com" not in str(actions["candidate.imported_salesforce"].meta)
    # The responsible member works with the candidate as with any other; the Salesforce id and link are for managers.
    seen = (await member.get(f"/api/candidates/{cid}")).json()
    assert (seen["source"], seen["salesforce_id"], seen["salesforce_url"], seen["email"]) == ("salesforce", None, None, "ana.1@example.com")
    assert (await admin.get(f"/api/candidates/{cid}")).json()["salesforce_url"] == f"{INSTANCE}/{ANA}"
    # The Candidates page can show what was brought in from Salesforce.
    assert [c["name"] for c in (await tl.get("/api/candidates", params={"source": "salesforce"})).json()["items"]] == ["Ana Example"]
    assert "Ana Example" not in [c["name"] for c in (await tl.get("/api/candidates", params={"source": "manual"})).json()["items"]]
    manual = (await tl.get(f"/api/candidates/{world.ids['cand_a1']}")).json()
    assert (manual["source"], manual["salesforce_id"], manual["salesforce_url"], manual["salesforce_synced_at"]) == ("manual", None, None, None)
    assert (await tl.get(f"/api/candidates/{world.ids['cand_a1']}/resume")).json()["source"] == "upload"
    # It can be searched for and given jobs at once, and a resume can still be uploaded over the Salesforce one.
    assert (await tl.post("/api/searches", json={"candidate_ids": [cid], "target_count": 3})).status_code in (200, 201, 202)
    upload = await tl.post(f"/api/candidates/{cid}/resume", files={"file": ("own.txt", b"Ana Example. Python and SQL engineer with FastAPI. " * 5)})
    assert upload.status_code == 201 and (upload.json()["source"], upload.json()["version"]) == ("upload", 2)
    # An Admin names the team; a Team Lead cannot name another team; team and member rules are those of a manual candidate.
    assert (await bring_in(admin, ANDRE)).status_code == 422
    assert (await bring_in(admin, ANDRE, team_id=str(uuid.uuid4()))).status_code == 404
    assert (await bring_in(tl, ANDRE, team_id=str(world.ids["team_beta"]))).status_code == 403
    assert (await bring_in(tl, ANDRE, assigned_member_id=str(world.ids["member_b1"]))).status_code == 422
    async with world.sessions() as db:
        assert (await db.execute(select(Candidate).where(Candidate.salesforce_id == ANDRE))).first() is None      # nothing half-made
    docx = await bring_in(admin, ANDRE, team_id=str(world.ids["team_beta"]))
    assert docx.status_code == 201 and docx.json()["team"]["name"] == "Team Beta" and docx.json()["sync"]["resume"] == "imported"
    andre = docx.json()["id"]
    assert (await admin.get(f"/api/candidates/{andre}/profile")).json()["desired_titles"] == ["Product Manager"]      # no technology: the niche
    file = await admin.get(f"/api/candidates/{andre}/resume/download")
    assert file.headers["content-type"] == DOCX_TYPE and sf.file_kind(file.content) == "docx"
    assert (await admin.get(f"/api/candidates/{andre}/resume")).json()["filename"] == "Andre Docx - Resume.docx"
    # Ids that are not candidates.
    for missing in (BUSINESS, sf_id("001", 99)):
        gone = await bring_in(admin, missing, team_id=alpha)
        assert gone.status_code == 404 and gone.json()["error"] == "Salesforce candidate not found"
    for bad in ("", "001", "001000000000001AA'", "x" * 19, None):
        assert (await bring_in(admin, bad, team_id=alpha)).status_code == 422, bad


async def test_a_resume_that_cannot_be_taken_over_never_blocks_or_breaks_the_import(world):
    org = standard_org()
    org.add(10, "Arno Corrupt")
    org.add(11, "Asta Mislabel")
    org.add(12, "Aki Lying")
    org.add_resume(sf_id("001", 10), 10, data=b"%PDF-1.7\nthis is not really a pdf")
    org.add_resume(sf_id("001", 11), 11, ext="pdf", data=b"\x89PNG\r\n\x1a\n" + b"0" * 64)            # a picture named .pdf
    org.add_resume(sf_id("001", 12), 12, data=b"%PDF-" + b"0" * (world.settings.max_upload_bytes + 10), size=1000)   # declared small, really huge
    install(world, org)
    tl = await world.as_tl("alpha")
    expected = {ANNA: "none", ANYA: "refused_size", ANSELM: "refused_type", sf_id("001", 10): "unreadable", sf_id("001", 11): "refused_type",
                sf_id("001", 12): "refused_size", ANNIKA: "imported"}
    for salesforce_id, outcome in expected.items():
        made = await bring_in(tl, salesforce_id)
        assert made.status_code == 201, made.text
        body = made.json()
        assert body["sync"]["resume"] == outcome, (body["name"], body["sync"])
        assert body["has_resume"] is (outcome == "imported") and body["resume_has_file"] is (outcome == "imported")
        assert ((await tl.get(f"/api/candidates/{body['id']}/resume")).json() is None) is (outcome != "imported")       # never a broken file
        assert (await tl.get(f"/api/candidates/{body['id']}/profile")).json()["desired_titles"] == ["AI/ML Engineer"]      # the rest arrived
        if outcome != "imported":                                          # the UI offers the normal upload, and it works
            assert (await tl.get(f"/api/candidates/{body['id']}/resume/download")).status_code == 404
    downloads = [p for m, p, _ in org.requests if p.endswith("/VersionData")]
    assert not any(sf_id("068", 6) in p or sf_id("068", 7) in p for p in downloads)                    # refused before any byte was read
    async with world.sessions() as db:
        resumes = list((await db.execute(select(Resume).where(Resume.source == "salesforce"))).scalars())
    assert [r.salesforce_content_version_id for r in resumes] == [sf_id("068", 82)]                    # the newest active usable record's file
    anna = (await tl.get("/api/candidates", params={"q": "Anna Noresume"})).json()["items"][0]["id"]
    assert (await tl.post(f"/api/candidates/{anna}/resume", files={"file": ("cv.pdf", make_pdf("Anna Noresume"), "application/pdf")})).status_code == 201
    # Salesforce failing while the file is read: the candidate still arrives, and a later refresh fetches the file.
    org.fail_downloads = True
    late = await bring_in(tl, ANA)
    assert late.status_code == 201 and late.json()["sync"]["resume"] == "unavailable" and late.json()["has_resume"] is False
    org.fail_downloads = False
    again = await tl.post(f"/api/candidates/{late.json()['id']}/salesforce-refresh")
    assert again.status_code == 200 and again.json()["sync"]["resume"] == "imported" and again.json()["has_resume"] is True


async def test_duplicates_and_team_isolation(world):
    org = standard_org()
    install(world, org)
    admin, alpha, beta, member_b = await world.as_admin(), await world.as_tl("alpha"), await world.as_tl("beta"), await world.as_member("b1")
    first = await bring_in(alpha, ANA)
    assert first.status_code == 201
    cid = first.json()["id"]
    # Again in the same team: the same candidate, refreshed, 200. Never a second row.
    again = await bring_in(alpha, ANA, assigned_member_id=str(world.ids["member_a2"]))
    assert again.status_code == 200 and again.json()["id"] == cid and again.json()["assigned_member"] is None
    assert again.json()["sync"] == {"created": False, "updated_fields": [], "kept_fields": [], "resume": "unchanged"}
    by_admin = await bring_in(admin, ANA, team_id=str(world.ids["team_beta"]))
    assert by_admin.status_code == 200 and by_admin.json()["id"] == cid and by_admin.json()["team"]["name"] == "Team Alpha"     # not moved
    async with world.sessions() as db:
        assert len(list((await db.execute(select(Candidate).where(Candidate.salesforce_id == ANA))).scalars())) == 1
        assert len(list((await db.execute(select(Resume).where(Resume.candidate_id == uuid.UUID(cid)))).scalars())) == 1
    # The other team's lead: a neutral 409. No team, no id, no name; nothing was changed or created.
    refused = await bring_in(beta, ANA)
    assert refused.status_code == 409 and refused.json() == {"error": "This candidate is already in the portal.", "detail": ""}
    assert "Alpha" not in refused.text and cid not in refused.text
    for path in (f"/api/candidates/{cid}", f"/api/candidates/{cid}/profile", f"/api/candidates/{cid}/resume", f"/api/candidates/{cid}/resume/download"):
        assert (await beta.get(path)).status_code == 404, path
    assert (await beta.post(f"/api/candidates/{cid}/salesforce-refresh")).status_code == 404
    assert (await member_b.post(f"/api/candidates/{cid}/salesforce-refresh")).status_code == 403
    assert all(c["id"] != cid for c in (await beta.get("/api/candidates", params={"source": "salesforce"})).json()["items"])
    async with world.sessions() as db:
        assert (await db.execute(select(Candidate.team_id).where(Candidate.salesforce_id == ANA))).scalar_one() == world.ids["team_alpha"]
    # In the search each caller learns exactly what they may: the own team the local id, the other team "elsewhere", the Admin everything.
    def ana_of(response):
        return next(i for i in response.json()["items"] if i["salesforce_id"] == ANA)

    own = ana_of(await alpha.get("/api/salesforce/candidates", params={"q": "ana"}))
    assert (own["imported_candidate_id"], own["imported_elsewhere"], own["importable"]) == (cid, False, False)
    other = ana_of(await beta.get("/api/salesforce/candidates", params={"q": "ana"}))
    assert (other["imported_candidate_id"], other["imported_elsewhere"], other["importable"]) == (None, True, False)
    assert cid not in str(other) and "Alpha" not in str({k: v for k, v in other.items() if k != "marketing_team_lead"})
    everything = ana_of(await admin.get("/api/salesforce/candidates", params={"q": "ana"}))
    assert (everything["imported_candidate_id"], everything["imported_elsewhere"], everything["importable"]) == (cid, False, False)
    fresh = next(i for i in (await beta.get("/api/salesforce/candidates", params={"q": "ana"})).json()["items"] if i["salesforce_id"] == ANABEL)
    assert (fresh["imported_candidate_id"], fresh["imported_elsewhere"], fresh["importable"]) == (None, False, True)
    # The database itself holds one portal candidate per Salesforce candidate.
    async with world.sessions() as db:
        db.add(Candidate(name="Second Ana", team_id=world.ids["team_beta"], salesforce_id=ANA))
        try:
            await db.commit()
            raise AssertionError("a second candidate with the same Salesforce id was stored")
        except Exception as problem:
            assert "uq_candidates_salesforce_id" in str(problem)
    # A deactivated team takes no imports; a Team Lead without a team has no access.
    assert (await admin.patch(f"/api/teams/{world.ids['team_beta']}", json={"is_active": False})).status_code == 200
    assert (await bring_in(admin, ANABEL, team_id=str(world.ids["team_beta"]))).status_code == 409


async def test_refresh_keeps_local_edits_and_takes_what_changed_in_salesforce(world):
    org = standard_org()
    install(world, org)
    admin, tl = await world.as_admin(), await world.as_tl("alpha")
    cid = (await bring_in(tl, ANA)).json()["id"]
    refresh = f"/api/candidates/{cid}/salesforce-refresh"
    # Nothing changed anywhere: nothing is written, the file is not fetched again.
    downloads = len([1 for _, p, _ in org.requests if p.endswith("/VersionData")])
    same = await tl.post(refresh)
    assert same.status_code == 200 and same.json()["sync"] == {"created": False, "updated_fields": [], "kept_fields": [], "resume": "unchanged"}
    assert len([1 for _, p, _ in org.requests if p.endswith("/VersionData")]) == downloads
    # A manager edits some fields in the portal; Salesforce changes those and others.
    assert (await tl.patch(f"/api/candidates/{cid}", json={"phone": "555 0199", "notes": "Call after 5 pm\n\nPreferred job locations (from Salesforce): Texas, California"})).status_code == 200
    assert (await tl.patch(f"/api/candidates/{cid}/profile", json={"desired_titles": ["Staff ML Engineer"], "work_mode": "Hybrid", "skills": ["Python", "Rust"],
                                                                 "salary_min": 150000})).status_code == 200
    org.accounts[ANA].update({"Name": "Ana Maria Example", "PersonEmail": "ana.new@example.com", "Phone": "(555) 010-9999", "Technology__c": "Data Engineer",
                              "Preferred_Job_Type__c": "On-site", "Total_Years_of_Experience__c": 6.5, "Current_Visa_Status__c": "H1B",
                              "Targeted_Experience_Level__c": "Director", "Preferred_Job_Locations__c": "Ohio or nearby states",
                              "Sponsorship_Required_For_VISA__c": True})
    done = await tl.post(refresh)
    assert done.status_code == 200, done.text
    body = done.json()
    assert sorted(body["sync"]["kept_fields"]) == ["desired_titles", "phone", "work_mode"]
    assert sorted(body["sync"]["updated_fields"]) == ["email", "name", "notes", "preferred_states", "seniority", "sponsorship_needed",
                                                      "work_authorization", "years_experience"]
    assert (body["name"], body["email"], body["phone"]) == ("Ana Maria Example", "ana.new@example.com", "555 0199")
    assert body["notes"] == "Call after 5 pm\n\nPreferred job locations (from Salesforce): Ohio or nearby states"      # the manager's own note stays
    profile = (await tl.get(f"/api/candidates/{cid}/profile")).json()
    assert (profile["desired_titles"], profile["work_mode"], profile["skills"], profile["salary_min"]) == (["Staff ML Engineer"], "Hybrid", ["Python", "Rust"], 150000)
    assert (profile["years_experience"], profile["work_authorization"], profile["seniority"], profile["preferred_states"], profile["sponsorship_needed"]) == \
        (6.5, "Current Work Visa", "Director", ["OH"], "Yes")
    # The kept fields stay kept on every later refresh, also when Salesforce changes them once more.
    org.accounts[ANA].update({"Phone": "(555) 010-7777", "Technology__c": "Java Developer"})
    later = (await tl.post(refresh)).json()
    assert sorted(later["sync"]["kept_fields"]) == ["desired_titles", "phone", "work_mode"] and later["sync"]["updated_fields"] == [] and later["phone"] == "555 0199"
    # A value that becomes empty in Salesforce never erases the portal's value; an unticked box does not turn "Yes" into "No".
    org.accounts[ANA].update({"PersonEmail": None, "Total_Years_of_Experience__c": None, "Current_Visa_Status__c": None, "Work_Authorization__c": None,
                              "Preferred_Job_Locations__c": None, "Sponsorship_Required_For_VISA__c": False, "Targeted_Experience_Level__c": None})
    emptied = (await tl.post(refresh)).json()
    assert emptied["sync"]["updated_fields"] == [] and emptied["email"] == "ana.new@example.com" and "Ohio or nearby states" in emptied["notes"]
    profile = (await tl.get(f"/api/candidates/{cid}/profile")).json()
    assert (profile["years_experience"], profile["work_authorization"], profile["sponsorship_needed"], profile["seniority"], profile["preferred_states"]) == \
        (6.5, "Current Work Visa", "Yes", "Director", ["OH"])
    # A field the manager empties again is filled from Salesforce the next time.
    assert (await tl.patch(f"/api/candidates/{cid}", json={"phone": ""})).status_code == 200
    assert (await tl.post(refresh)).json()["phone"] == "(555) 010-7777"
    # Notes the manager rewrote are left alone; a changed Salesforce text is added, nothing is removed.
    assert (await tl.patch(f"/api/candidates/{cid}", json={"notes": "Only my own words"})).status_code == 200
    assert (await tl.post(refresh)).json()["notes"] == "Only my own words"
    org.accounts[ANA]["Preferred_Job_Locations__c"] = "Florida"
    assert (await tl.post(refresh)).json()["notes"] == "Only my own words\n\nPreferred job locations (from Salesforce): Florida"
    # Audit: who refreshed, what was written and kept. No personal values.
    async with world.sessions() as db:
        entries = list((await db.execute(select(AuditLog).where(AuditLog.action == "candidate.salesforce_refreshed", AuditLog.target_id == cid)
                                         .order_by(AuditLog.created_at))).scalars())
    assert len(entries) >= 6 and sorted(entries[1].meta["kept"]) == ["desired_titles", "phone", "work_mode"] and "ana.new" not in str([e.meta for e in entries])
    # Refresh is for Salesforce candidates, and for records that still exist there.
    manual = await tl.post(f"/api/candidates/{world.ids['cand_a1']}/salesforce-refresh")
    assert manual.status_code == 409 and manual.json()["error"] == "This candidate did not come from Salesforce"
    del org.accounts[ANA]
    gone = await admin.post(refresh)
    assert gone.status_code == 404 and gone.json()["error"] == "Salesforce candidate not found"
    assert (await admin.get(f"/api/candidates/{cid}")).json()["name"] == "Ana Maria Example"           # the portal's copy is untouched
    org.fail_with = 500
    assert (await admin.post(refresh)).status_code == 503


async def test_refresh_replaces_a_resume_only_with_a_newer_salesforce_file(world):
    org = standard_org()
    install(world, org)
    tl = await world.as_tl("alpha")
    cid = (await bring_in(tl, ANA)).json()["id"]
    refresh, resume = f"/api/candidates/{cid}/salesforce-refresh", f"/api/candidates/{cid}/resume"

    async def current():
        body = (await tl.get(resume)).json()
        return body["source"], body["version"]

    assert await current() == ("salesforce", 1)
    # Salesforce gets a new file: it replaces the Salesforce copy.
    org.add_resume(ANA, 31, ext="docx", created="2026-10-10T09:00:00.000+0000", modified="2026-10-10T09:00:00.000+0000")
    assert (await tl.post(refresh)).json()["sync"]["resume"] == "imported" and await current() == ("salesforce", 2)
    assert (await tl.get(resume)).json()["filename"] == "Ana Example - Resume.docx"
    # A manager uploads a resume in the portal. The Salesforce file the portal already had is never put back over it...
    upload = await tl.post(resume, files={"file": ("better.txt", b"Ana Example. Senior Python engineer, PyTorch, SQL, AWS. " * 5)})
    assert upload.status_code == 201 and await current() == ("upload", 3)
    assert (await tl.post(refresh)).json()["sync"]["resume"] == "unchanged" and await current() == ("upload", 3)
    # ...nor is another Salesforce file that is older than the upload...
    org.add_resume(ANA, 32, created="2026-10-20T09:00:00.000+0000", modified="2026-10-20T09:00:00.000+0000")
    kept = await tl.post(refresh)
    assert kept.json()["sync"]["resume"] == "kept_local" and await current() == ("upload", 3) and "Senior Python engineer" in (await tl.get(resume)).json()["text"]
    # ...but a Salesforce file changed after the upload is the newer one and is taken.
    org.add_resume(ANA, 33, created="2099-01-01T09:00:00.000+0000", modified="2099-01-01T09:00:00.000+0000")
    assert (await tl.post(refresh)).json()["sync"]["resume"] == "imported" and await current() == ("salesforce", 4)
    async with world.sessions() as db:
        rows = list((await db.execute(select(Resume).where(Resume.candidate_id == uuid.UUID(cid)).order_by(Resume.version))).scalars())
    assert [(r.source, r.salesforce_content_version_id, r.is_current) for r in rows] == [
        ("salesforce", sf_id("068", 1), False), ("salesforce", sf_id("068", 31), False), ("upload", None, False), ("salesforce", sf_id("068", 33), True)]
    # A resume deleted in the portal comes back only when Salesforce has a file the candidate has not had yet.
    assert (await tl.delete(resume)).status_code == 200
    assert (await tl.post(refresh)).json()["sync"]["resume"] == "imported" and await current() == ("salesforce", 1)
    # A newer file of a kind the portal does not take leaves the current resume in place.
    org.add_resume(ANA, 34, ext="png", data=b"\x89PNG\r\n\x1a\n" + b"0" * 32, created="2099-02-01T09:00:00.000+0000", modified="2099-02-01T09:00:00.000+0000")
    assert (await tl.post(refresh)).json()["sync"]["resume"] == "refused_type" and await current() == ("salesforce", 1)
