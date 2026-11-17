"""Revision c4d2a9e71f36 (members stop being job seekers, candidates appear) on a database that holds data.

The old schema is built for real (`alembic upgrade a1c7e52b9d04`), filled with representative rows, upgraded, read
back through the API, downgraded with data made under the new model, and upgraded again. It runs in a database of
its own, created and dropped here."""
import os
import subprocess
import sys
import uuid

import asyncpg
import httpx

from app.config import Settings
from app.portal import security
from app.portal.app import create_app
from app.portal.queue import InlineQueue
from tests.portal.conftest import PASSWORD, PLAIN_URL, ROOT, TEST_URL

NAME = PLAIN_URL.rsplit("/", 1)[1] + "_migration"
URL, PLAIN = TEST_URL.rsplit("/", 1)[0] + "/" + NAME, PLAIN_URL.rsplit("/", 1)[0] + "/" + NAME
OLD, NEW = "a1c7e52b9d04", "c4d2a9e71f36"
COUNTED = ("users", "resumes", "search_runs", "search_targets", "search_results", "job_assignments", "application_events",
           "assignment_notes", "job_postings", "notifications", "audit_logs")


def alembic(*args: str) -> None:
    result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, capture_output=True, text=True,
                            env={**os.environ, "ALEMBIC_DATABASE_URL": URL})
    assert result.returncode == 0, result.stderr[-1500:]


async def counts(conn, profiles: str) -> dict:
    return {table: await conn.fetchval(f"SELECT count(*) FROM {table}") for table in (*COUNTED, profiles)}


async def columns(conn, table: str) -> set[str]:
    return {r["column_name"] for r in await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = $1", table)}


async def test_upgrade_keeps_every_row_reachable_and_downgrade_works_with_migrated_data(database):
    admin_conn = await asyncpg.connect(PLAIN_URL.rsplit("/", 1)[0] + "/postgres")
    await admin_conn.execute(f'DROP DATABASE IF EXISTS "{NAME}"')
    await admin_conn.execute(f'CREATE DATABASE "{NAME}"')
    try:
        await run_scenario()
    finally:
        await admin_conn.execute(f'DROP DATABASE IF EXISTS "{NAME}" WITH (FORCE)')
        await admin_conn.close()


async def run_scenario() -> None:
    alembic("upgrade", OLD)
    ids = {k: uuid.uuid4() for k in ("team", "other_team", "admin", "lead", "full", "off", "bare", "plain", "idle", "teamless", "job1", "job2", "run",
                                     "resume_old", "resume_cur", "resume_teamless", "result", "asg1", "asg2", "asg_off", "event1", "event2", "note")}
    hashed = security.hash_password(PASSWORD)
    conn = await asyncpg.connect(PLAIN)
    assert "member_id" in await columns(conn, "job_assignments") and not await columns(conn, "candidates")
    # ---- the old world: a member IS the job seeker
    await conn.executemany("INSERT INTO teams (id, name) VALUES ($1, $2)", [(ids["team"], "Team One"), (ids["other_team"], "Team Two")])
    people = [("admin", "Old Admin", "ADMIN", None, True), ("lead", "Old Lead", "TEAM_LEAD", "team", True),
              ("full", "Fiona Full", "MEMBER", "team", True),          # profile, two resume versions, search, result, jobs, history
              ("off", "Oscar Off", "MEMBER", "team", False),           # deactivated, has a job assignment only
              ("bare", "Bella Bare", "MEMBER", "team", True),          # nothing but a profile with ONE field filled in
              ("plain", "Paul Plain", "MEMBER", "team", True),         # only the all-default profile every invited member got
              ("idle", "Ian Idle", "MEMBER", "other_team", True),      # owns nothing at all
              ("teamless", "Tess Teamless", "MEMBER", None, True)]     # no team, has a resume
    await conn.executemany(
        "INSERT INTO users (id, name, email, role, team_id, is_active, password_hash) VALUES ($1, $2, $3, $4, $5, $6, $7)",
        [(ids[k], name, f"{k}@Example.com".lower(), role, ids[team] if team else None, active, hashed) for k, name, role, team, active in people])
    await conn.execute("INSERT INTO team_lead_assignments (team_id, user_id) VALUES ($1, $2)", ids["team"], ids["lead"])
    await conn.execute(
        """INSERT INTO member_profiles (user_id, desired_titles, years_experience, skills, preferred_states, work_authorization, extra)
           VALUES ($1, '["AI Engineer"]', 4.5, '["Python", "SQL"]', '["TX"]', 'US Citizen', '{"shift": "Day"}')""", ids["full"])
    await conn.execute("INSERT INTO member_profiles (user_id) VALUES ($1), ($2)", ids["plain"], ids["lead"])    # untouched rows: a member's, and a lead's stray one
    await conn.execute("INSERT INTO member_profiles (user_id, salary_min) VALUES ($1, 90000)", ids["bare"])
    untouched = await conn.fetchval("SELECT to_jsonb(p)::text FROM member_profiles p WHERE user_id = $1", ids["plain"])
    await conn.executemany(
        "INSERT INTO resumes (id, user_id, filename, size_bytes, sha256, text, version, is_current, uploaded_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $2)",
        [(ids["resume_old"], ids["full"], "cv-v1.pdf", 900, "a" * 64, "", 1, False),
         (ids["resume_cur"], ids["full"], "cv-v2.pdf", 1200, "b" * 64, "Fiona Full. Python engineer, SQL, PyTorch.", 2, True),
         (ids["resume_teamless"], ids["teamless"], "tess.txt", 300, "c" * 64, "Tess. Data analyst.", 1, True)])
    await conn.executemany("INSERT INTO job_postings (id, identity_key, company, title, canonical_url) VALUES ($1, $2, $3, $4, $5)",
                           [(ids["job1"], "manual:1", "Acme", "AI Engineer", "https://acme.example/1"),
                            (ids["job2"], "manual:2", "Globex", "ML Engineer", "https://globex.example/2")])
    await conn.execute(
        """INSERT INTO search_runs (id, initiated_by, team_id, target_count, status, matched_count, assigned_count, progress)
           VALUES ($1, $2, $3, 5, 'completed', 1, 1, $4)""", ids["run"], ids["lead"], ids["team"],
        '{"stage": "Finished", "members": [{"member_id": "%s", "name": "Fiona Full", "status": "completed", "matched": 1}]}' % ids["full"])
    await conn.execute(
        """INSERT INTO search_targets (search_run_id, member_id, member_specific_filters, status, stats)
           VALUES ($1, $2, '{"job_titles": ["AI Engineer"], "skills": ["Python"]}', 'completed', '{"matched": 1, "assigned": 1}')""",
        ids["run"], ids["full"])
    await conn.execute(
        """INSERT INTO search_results (id, search_run_id, member_id, job_id, rank, match_score, match_explanation)
           VALUES ($1, $2, $3, $4, 1, 82, '{"reason": "Strong Python match"}')""", ids["result"], ids["run"], ids["full"], ids["job1"])
    await conn.executemany(
        """INSERT INTO job_assignments (id, job_id, member_id, team_id, assigned_by, search_run_id, source, match_score, status, applied_at,
                                        application_reference) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
        [(ids["asg1"], ids["job1"], ids["full"], ids["team"], ids["lead"], ids["run"], "search", 82, "APPLIED", security.utcnow(), "REQ-9"),
         (ids["asg2"], ids["job2"], ids["full"], ids["team"], ids["admin"], None, "manual", None, "ASSIGNED", None, ""),
         (ids["asg_off"], ids["job2"], ids["off"], ids["team"], ids["lead"], None, "manual", None, "VIEWED", None, "")])
    await conn.executemany(
        "INSERT INTO application_events (id, assignment_id, actor_id, event_type, old_status, new_status) VALUES ($1, $2, $3, $4, $5, $6)",
        [(ids["event1"], ids["asg1"], ids["lead"], "assigned", None, "ASSIGNED"), (ids["event2"], ids["asg1"], ids["full"], "status_changed", "VIEWED", "APPLIED")])
    await conn.execute("INSERT INTO assignment_notes (id, assignment_id, author_id, note) VALUES ($1, $2, $3, 'Sent on Monday')",
                       ids["note"], ids["asg1"], ids["full"])
    await conn.execute("INSERT INTO notifications (id, user_id, type, payload) VALUES ($1, $2, 'jobs_assigned', '{\"count\": 2}')", uuid.uuid4(), ids["full"])
    before = await counts(conn, "member_profiles")
    assert before["member_profiles"] == 4 and before["job_assignments"] == 3
    await conn.close()

    # ---- upgrade
    alembic("upgrade", NEW)
    conn = await asyncpg.connect(PLAIN)
    after = await counts(conn, "candidate_profiles")
    assert {k: v for k, v in after.items() if k != "candidate_profiles"} == {k: v for k, v in before.items() if k != "member_profiles"}
    assert after["candidate_profiles"] == 2                                # two rows belong to no candidate: parked, not lost
    parked = {r["kind"]: (r["legacy_id"], r["data"]) for r in await conn.fetch(
        "SELECT kind, legacy_id, data::text AS data FROM legacy_records WHERE kind LIKE 'member_profile%'")}
    assert set(parked) == {"member_profile_without_member", "member_profile_all_default"}
    assert parked["member_profile_without_member"][0] == str(ids["lead"])
    # An employee whose only "job-seeker data" is the empty profile row made at invitation is not a candidate.
    assert parked["member_profile_all_default"] == (str(ids["plain"]), untouched)
    assert await conn.fetchval("SELECT count(*) FROM candidate_profiles WHERE candidate_id = $1", ids["plain"]) == 0
    assert await conn.fetchval("SELECT salary_min FROM candidate_profiles WHERE candidate_id = $1", ids["bare"]) == 90000   # one field is enough
    made = {r["id"]: r for r in await conn.fetch("SELECT * FROM candidates")}
    assert set(made) == {ids["full"], ids["off"], ids["bare"], ids["teamless"]}       # the user's UUID is reused; Ian and Paul own nothing: no candidate
    for key, name, team, active in (("full", "Fiona Full", "team", True), ("off", "Oscar Off", "team", False),
                                    ("bare", "Bella Bare", "team", True), ("teamless", "Tess Teamless", None, True)):
        c = made[ids[key]]
        assert (c["name"], c["email"], c["team_id"], c["assigned_member_id"], c["is_active"], c["phone"], c["notes"]) == \
            (name, f"{key}@example.com", ids[team] if team else None, ids[key], active, "", ""), key
    assert await columns(conn, "member_profiles") == set() and "candidate_id" in await columns(conn, "candidate_profiles")
    for table, gone in (("resumes", "user_id"), ("search_targets", "member_id"), ("search_results", "member_id"), ("job_assignments", "member_id")):
        names = await columns(conn, table)
        assert "candidate_id" in names and gone not in names, table
    assert {"resume_id", "candidate_specific_filters"} <= await columns(conn, "search_targets") and "file_data" in await columns(conn, "resumes")
    # Every foreign key that named a user as the job seeker now names the candidate, with the stored value unchanged.
    targets = {r["conrelid"]: r["confrelid"] for r in await conn.fetch(
        """SELECT conrelid::regclass::text AS conrelid, confrelid::regclass::text AS confrelid FROM pg_constraint
           WHERE contype = 'f' AND conname LIKE '%candidate_id_fkey'""")}
    assert targets == {t: "candidates" for t in ("candidate_profiles", "resumes", "search_targets", "search_results", "job_assignments")}
    assert await conn.fetchval("SELECT count(*) FROM job_assignments a JOIN candidates c ON c.id = a.candidate_id") == 3
    assert await conn.fetchval("SELECT candidate_id FROM job_assignments WHERE id = $1", ids["asg1"]) == ids["full"]
    assert await conn.fetchval("SELECT count(*) FROM resumes WHERE file_data IS NOT NULL") == 0        # old files were never stored
    assert await conn.fetchval("SELECT count(*) FROM search_targets WHERE resume_id IS NOT NULL") == 0  # not known for old searches: not invented
    for index in ("ix_resumes_candidate_id", "uq_resumes_current", "ix_search_targets_candidate_id", "ix_job_assignments_candidate_id",
                  "ix_candidates_team_id", "ix_candidates_assigned_member_id", "uq_job_assignments_job_candidate", "uq_search_results_run_candidate_job"):
        assert await conn.fetchval("SELECT 1 FROM pg_indexes WHERE indexname = $1", index) == 1, index
    await conn.close()

    # ---- the migrated rows through the API, under the new model
    alembic("upgrade", "head")              # the API needs today's schema; later revisions only add columns, and the
    #                                         downgrade below passes back through them
    settings = Settings(_env_file=None, database_url=URL, openai_api_key="", anthropic_api_key="", public_base_url="http://testserver",
                        prewarm_on_start=False, browser_enabled=False, email_backend="", smtp_host="", app_env="test")

    async def no_search(run_id):
        raise AssertionError("no search is started in this test")

    queue = InlineQueue(no_search)
    app = create_app(settings, queue=queue, use_redis=False)
    clients = []

    async def sign_in(key: str) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        clients.append(client)
        response = await client.post("/api/auth/login", json={"email": f"{key}@example.com", "password": PASSWORD})
        assert response.status_code == 200, response.text
        client.headers["x-csrf-token"] = client.cookies["jf_csrf"]
        return client

    full = str(ids["full"])
    async with app.router.lifespan_context(app):
        try:
            member, lead, admin = await sign_in("full"), await sign_in("lead"), await sign_in("admin")
            # The former job seeker is now the member responsible for the candidate made from their account.
            plain = await sign_in("plain")                               # still an account, a recruiter with nobody yet
            assert (await plain.get("/api/candidates")).json()["total"] == 0 and (await plain.get("/api/assignments")).json()["total"] == 0
            assert (await plain.get("/api/auth/me")).json()["role"] == "MEMBER"
            mine = (await member.get("/api/candidates")).json()
            assert mine["total"] == 1 and mine["items"][0]["id"] == full and mine["items"][0]["assigned_member"]["id"] == full
            assert mine["items"][0]["has_resume"] is True and mine["items"][0]["resume_has_file"] is False and mine["items"][0]["stats"]["applied"] == 1
            profile = (await member.get(f"/api/candidates/{full}/profile")).json()
            assert (profile["desired_titles"], profile["years_experience"], profile["skills"], profile["preferred_states"],
                    profile["work_authorization"], profile["extra"]) == (["AI Engineer"], 4.5, ["Python", "SQL"], ["TX"], "US Citizen", {"shift": "Day"})
            resume = (await member.get(f"/api/candidates/{full}/resume")).json()
            assert (resume["id"], resume["filename"], resume["version"], resume["has_file"]) == (str(ids["resume_cur"]), "cv-v2.pdf", 2, False)
            assert "Python engineer" in resume["text"]
            assert (await member.get(f"/api/candidates/{full}/resume/download")).status_code == 404
            jobs = (await member.get("/api/assignments")).json()
            assert {a["id"] for a in jobs["items"]} == {str(ids["asg1"]), str(ids["asg2"])}
            assert all(a["candidate"] == {"id": full, "name": "Fiona Full"} and a["member"]["id"] == full for a in jobs["items"])
            detail = (await member.get(f"/api/assignments/{ids['asg1']}")).json()
            assert detail["status"] == "APPLIED" and detail["application_reference"] == "REQ-9" and detail["match_score"] == 82
            assert [n["note"] for n in detail["notes"]] == ["Sent on Monday"] and {e["event_type"] for e in detail["events"]} >= {"assigned", "status_changed"}
            assert (await member.patch(f"/api/assignments/{ids['asg2']}/status", json={"status": "APPLYING"})).status_code == 200
            assert (await member.get(f"/api/users/{full}/dashboard")).json()["stats"]["assigned"] == 2
            assert [n["type"] for n in (await member.get("/api/notifications")).json()["items"]] == ["jobs_assigned"]
            # The Team Lead sees the team's candidates, the old search with its target and result, and every assignment.
            listed = {c["name"]: c for c in (await lead.get("/api/candidates")).json()["items"]}
            assert set(listed) == {"Fiona Full", "Oscar Off", "Bella Bare"} and listed["Oscar Off"]["is_active"] is False
            assert listed["Bella Bare"]["has_resume"] is False and listed["Bella Bare"]["assigned_member"]["name"] == "Bella Bare"
            run = (await lead.get(f"/api/searches/{ids['run']}")).json()
            assert run["candidate_count"] == 1 and run["targets"][0]["candidate"] == {"id": full, "name": "Fiona Full"}
            assert run["targets"][0]["resume_id"] is None and run["targets"][0]["matched"] == 1 and run["targets"][0]["filters"] == {"job_titles": ["AI Engineer"]}
            results = (await lead.get(f"/api/searches/{ids['run']}/results")).json()
            assert results["total"] == 1 and results["items"][0]["candidate"]["id"] == full and results["items"][0]["assignment_id"] == str(ids["asg1"])
            assert (await lead.get("/api/assignments")).json()["total"] == 3
            assert (await lead.get(f"/api/teams/{ids['team']}/analytics")).json()["stats"]["assigned"] == 3
            # The Admin reaches the candidate without a team and can give it one.
            everyone = {c["name"]: c for c in (await admin.get("/api/candidates")).json()["items"]}
            assert "Paul Plain" not in everyone and len(everyone) == 4 and everyone["Tess Teamless"]["team"] is None and everyone["Tess Teamless"]["has_resume"] is True
            # Data made under the new model, for the downgrade: a candidate without an account, with resume file and a job.
            new = await lead.post("/api/candidates", json={"name": "Nina New", "phone": "555 0142", "notes": "Came by referral",
                                                           "email": "nina@example.com", "assigned_member_id": full})
            assert new.status_code == 201, new.text
            nina = new.json()["id"]
            assert (await lead.post(f"/api/candidates/{nina}/resume", files={"file": ("nina.txt", b"Nina New. Python and SQL engineer. " * 10)})).status_code == 201
            assert (await admin.post("/api/assignments", json={"job_id": str(ids["job1"]), "candidate_id": nina})).status_code == 201
            assert (await lead.patch(f"/api/candidates/{ids['bare']}", json={"assigned_member_id": None, "notes": "Call after 5 pm"})).status_code == 200
        finally:
            await queue.close()
            for client in clients:
                await client.aclose()

    # ---- downgrade with migrated and new data
    conn = await asyncpg.connect(PLAIN)
    new_counts = await counts(conn, "candidate_profiles")
    await conn.close()
    alembic("downgrade", OLD)
    conn = await asyncpg.connect(PLAIN)
    down = await counts(conn, "member_profiles")
    assert await columns(conn, "candidates") == set() and await columns(conn, "candidate_profiles") == set()
    assert "member_id" in await columns(conn, "job_assignments") and "user_id" in await columns(conn, "resumes")
    assert not {"resume_id", "candidate_specific_filters"} & await columns(conn, "search_targets") and "file_data" not in await columns(conn, "resumes")
    for table in ("resumes", "search_targets", "search_results", "job_assignments", "application_events", "assignment_notes", "job_postings", "search_runs"):
        assert down[table] == new_counts[table], table                                 # no profile, resume, search or job row was lost
    assert down["member_profiles"] == new_counts["candidate_profiles"] + 2            # both parked rows are back
    assert await conn.fetchval("SELECT count(*) FROM member_profiles WHERE user_id = $1", ids["lead"]) == 1
    assert await conn.fetchval("SELECT to_jsonb(p)::text FROM member_profiles p WHERE user_id = $1", ids["plain"]) == untouched   # exactly as it was
    assert down["users"] == new_counts["users"] + 1                                   # one placeholder, for the candidate without an account
    placeholder = await conn.fetchrow("SELECT * FROM users WHERE id = $1", uuid.UUID(nina))
    assert (placeholder["name"], placeholder["role"], placeholder["is_active"], placeholder["password_hash"], placeholder["team_id"]) == \
        ("Nina New", "MEMBER", False, None, ids["team"])
    assert placeholder["email"] == f"candidate-{nina}@candidate.invalid"              # nobody can sign in to it
    assert await conn.fetchval("SELECT count(*) FROM job_assignments a JOIN users u ON u.id = a.member_id") == 4
    assert await conn.fetchval("SELECT count(*) FROM resumes r JOIN users u ON u.id = r.user_id WHERE u.id = $1", uuid.UUID(nina)) == 1
    assert await conn.fetchval("SELECT status FROM job_assignments WHERE id = $1", ids["asg2"]) == "APPLYING"
    kept = {r["legacy_id"]: r["data"] for r in await conn.fetch("SELECT legacy_id, data::text AS data FROM legacy_records WHERE kind = 'candidate'")}
    assert set(kept) == {full, str(ids["off"]), str(ids["bare"]), str(ids["teamless"]), nina}
    assert '"555 0142"' in kept[nina] and "Came by referral" in kept[nina] and "Call after 5 pm" in kept[str(ids["bare"])]
    assert await conn.fetchval("SELECT count(*) FROM legacy_records WHERE kind LIKE 'member_profile%'") == 0
    assert str(ids["plain"]) not in kept
    await conn.close()

    # ---- and forward again: the same candidates as before the downgrade (the stored files excepted)
    alembic("upgrade", NEW)
    conn = await asyncpg.connect(PLAIN)
    again = await counts(conn, "candidate_profiles")
    assert again == {**new_counts, "audit_logs": again["audit_logs"]}
    back = {r["id"]: r for r in await conn.fetch("SELECT * FROM candidates")}
    assert set(back) == {ids["full"], ids["off"], ids["bare"], ids["teamless"], uuid.UUID(nina)}     # Paul is still no candidate
    assert await conn.fetchval("SELECT count(*) FROM legacy_records WHERE kind = 'member_profile_all_default' AND legacy_id = $1", str(ids["plain"])) == 1
    n = back[uuid.UUID(nina)]
    assert (n["name"], n["email"], n["phone"], n["notes"], n["assigned_member_id"], n["is_active"], n["team_id"], n["created_by"]) == \
        ("Nina New", "nina@example.com", "555 0142", "Came by referral", ids["full"], True, ids["team"], ids["lead"])
    assert (back[ids["bare"]]["assigned_member_id"], back[ids["bare"]]["notes"]) == (None, "Call after 5 pm")
    assert back[ids["off"]]["is_active"] is False and back[ids["full"]]["assigned_member_id"] == ids["full"]
    assert await conn.fetchval("SELECT count(*) FROM users WHERE email LIKE '%@candidate.invalid'") == 0
    assert await conn.fetchval("SELECT count(*) FROM legacy_records WHERE kind = 'candidate'") == 0
    assert await conn.fetchval("SELECT count(*) FROM job_assignments WHERE candidate_id = $1", uuid.UUID(nina)) == 1
    await conn.close()
