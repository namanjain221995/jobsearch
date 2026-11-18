"""AI auto-apply through the API and the worker runner, against the local fake ATS (no network).

Covers: the whole flow (consent -> recorded answers -> run -> dry run -> review -> approve -> one submission ->
assignment APPLIED, admin-only event, audit, notifications), permissions (Admin only; Team Lead / member 403;
unknown candidate 404), consent required and revocation, limits (one open run per assignment, one submission per
posting, employer daily cap), approval checks, admin edits, recovery after a worker stop, and the migration."""
import asyncio
import os
import subprocess
import sys
import uuid

import asyncpg
import pytest
from sqlalchemy import select, update

from app.autoapply import runner as aa_runner
from app.autoapply.answers import Engine
from app.autoapply.browser import ApplyBrowser
from app.portal.models import ApplicationEvent, AuditLog, AutoApplyRun, JobAssignment, Notification, Resume
from tests.autoapply.fake_ats import ASHBY_ID, FakeATS
from tests.portal.conftest import PLAIN_URL, ROOT, TEST_URL, add_job

pytest.importorskip("playwright.async_api")

RECORDED = {"authorized_us": "yes", "sponsorship_now_or_future": "no", "previous_employers_complete": True,
            "linkedin_url": "https://www.linkedin.com/in/jordan-example", "country": "United States"}
GOOD = {"answers": [{"key": "question_2", "cannot_answer": False,
                     "text": "I build and deploy models with Python and PyTorch, which fits the Machine Learning Engineer role at Acme.",
                     "claims": [{"claim": "Python", "evidence": "Skills: Python, PyTorch"}]}]}


class Writer:
    name = "FakeWriter"
    calls = 0

    async def json_call(self, system, user, schema, name):
        Writer.calls += 1
        return GOOD


@pytest.fixture(scope="module")
def fake():
    with FakeATS() as server:
        yield server


@pytest.fixture
async def aa(world, fake):
    """The world with auto-apply switched on and its worker jobs run inline."""
    world.settings.auto_apply_enabled = True
    world.settings.auto_apply_host_interval_seconds = 0
    world.settings.auto_apply_employer_daily_cap = 5
    deps = aa_runner.RunnerDeps(sessions=world.sessions, settings=world.settings,
                                browser=ApplyBrowser(resolver=fake.resolver, host_check=fake.host_check, ignore_https_errors=True),
                                engine_factory=lambda: Engine(writers=[Writer()]))

    async def run(kind, run_id):
        await (aa_runner.prepare if kind == "prepare" else aa_runner.submit)(run_id, deps)
    world.queue.auto_apply_runner = run
    world.aa_deps = deps
    try:
        async with deps.browser.session(dry_run=True):
            pass
    except Exception as exc:
        pytest.skip(f"Chromium cannot start here: {type(exc).__name__}")
    yield world
    world.queue.auto_apply_runner = None


async def setup_candidate(world, fake, *, consent=True, path="/acme/jobs/1001", host=0, company="Acme"):
    admin = await world.as_admin()
    cand = str(world.ids["cand_a1"])
    if consent:
        r = await admin.post(f"/api/candidates/{cand}/consents", json={"method": "written", "document_reference": "signed form 2026-11-18"})
        assert r.status_code == 201, r.text
    r = await admin.patch(f"/api/candidates/{cand}/application-answers", json=RECORDED)
    assert r.status_code == 200, r.text
    job = await add_job(world, company=company, title="Machine Learning Engineer", apply_url=fake.url(host, path))
    r = await admin.post("/api/assignments", json={"job_id": job, "candidate_id": cand})
    assert r.status_code == 201, r.text
    return admin, cand, r.json()["assignment_ids"][0]


async def start(world, admin, cand, assignment_id):
    r = await admin.post("/api/auto-apply/runs", json={"candidate_id": cand, "assignment_ids": [assignment_id]})
    assert r.status_code == 201, r.text
    await world.queue.drain()
    return r.json()


async def test_full_flow_dry_run_review_approve_submit(aa, fake):
    world = aa
    admin, cand, assignment_id = await setup_candidate(world, fake)
    r = await admin.get(f"/api/auto-apply/candidates/{cand}/eligibility")
    item = next(i for i in r.json()["items"] if i["assignment_id"] == assignment_id)
    assert (item["ats"], item["supported"], item["eligible"], r.json()["has_active_consent"], r.json()["has_resume_file"]) == \
        ("greenhouse", True, True, True, True)

    before = len(fake.submissions())
    created = await start(world, admin, cand, assignment_id)
    run_id = created["runs"][0]["id"]
    detail = (await admin.get(f"/api/auto-apply/runs/{run_id}")).json()
    assert detail["status"] == "ready_for_review", (detail["blocked_reasons"], detail["needs_input"], detail["error"])
    assert len(fake.submissions()) == before                                   # the dry run sent nothing
    assert detail["submission_guard"]["allowed_submissions"] == 0
    answers = {a["field_key"]: a for a in detail["answers"]}
    assert answers["email"]["value"] == "candidate.a1@example.com" and answers["email"]["source"] == "candidate"
    assert answers["question_3"]["value"] == "Yes" and answers["question_3"]["source"] == "application_answers"
    assert answers["gender"]["source"] == "eeo_decline" and answers["question_2"]["source"] == "ai_draft"
    assert answers["resume"]["source"] == "resume_file" and all(e["filled"] for e in detail["fill_report"])
    shot = detail["screenshots"][0]
    r = await admin.get(f"/api/auto-apply/runs/{run_id}/screenshots/{shot['id']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"

    r = await admin.post(f"/api/auto-apply/runs/{run_id}/approve", json={"answers_hash": "0" * 64})
    assert r.status_code == 409 and r.json()["error"].startswith("answers_changed")
    r = await admin.post(f"/api/auto-apply/runs/{run_id}/approve", json={"answers_hash": detail["answers_hash"]})
    assert r.status_code == 202, r.text
    await world.queue.drain()
    detail = (await admin.get(f"/api/auto-apply/runs/{run_id}")).json()
    assert detail["status"] == "submitted", (detail["error"], detail["blocked_reasons"])
    assert len(fake.submissions()) == before + 1                               # exactly one submission
    assert detail["confirmation"]["detected"] and detail["confirmation"]["reference"] == "GH-4711"
    assert {s["kind"] for s in detail["screenshots"]} == {"dry_run", "before_submit", "confirmation"}

    async with world.sessions() as db:
        a = await db.get(JobAssignment, uuid.UUID(assignment_id))
        assert a.status == "APPLIED" and a.applied_at is not None and a.application_reference == "GH-4711"
        events = {e.event_type: e for e in (await db.execute(select(ApplicationEvent).where(ApplicationEvent.assignment_id == a.id))).scalars()}
        assert events["status_changed"].new_status == "APPLIED"
        meta = events["auto_apply_submitted"].meta
        assert meta["source"] == "auto_apply" and len(meta["answers_hash"]) == 64 and "answers" not in meta
        actions = set((await db.execute(select(AuditLog.action))).scalars())
        assert {"consent.recorded", "application_answers.updated", "auto_apply.runs_created", "auto_apply.approved", "auto_apply.submitted"} <= actions
        notes = {(n.user_id, n.type) for n in (await db.execute(select(Notification))).scalars()}
        assert (world.ids["admin"], "auto_apply_ready") in notes and (world.ids["admin"], "auto_apply_submitted") in notes
        assert (world.ids["member_a1"], "application_submitted") in notes
        assert not [t for u, t in notes if u == world.ids["member_a1"] and t.startswith("auto_apply")]

    # The feature is invisible to Team Leads and members.
    for client in (await world.as_tl("alpha"), await world.as_member("a1")):
        r = await client.get(f"/api/assignments/{assignment_id}")
        assert r.status_code == 200 and "auto_apply_submitted" not in r.text and "auto_apply" not in r.text
        r = await client.get(f"/api/candidates/{cand}/activity")
        assert "auto_apply" not in r.text
    admin_view = await admin.get(f"/api/assignments/{assignment_id}")
    assert "auto_apply_submitted" in admin_view.text

    # Never twice: the assignment is applied, and the posting can no longer get a run.
    created = (await admin.post("/api/auto-apply/runs", json={"candidate_id": cand, "assignment_ids": [assignment_id]})).json()
    assert created["runs"] == [] and created["skipped"][0]["reason"] == "already_applied"


async def test_permissions(aa, fake):
    world = aa
    admin, cand, assignment_id = await setup_candidate(world, fake)
    run_id = (await admin.post("/api/auto-apply/runs", json={"candidate_id": cand, "assignment_ids": [assignment_id]})).json()["runs"][0]["id"]
    await world.queue.drain()
    for client in (await world.as_tl("alpha"), await world.as_member("a1")):
        for method, path, body in [
            ("get", "/api/auto-apply/status", None), ("get", "/api/auto-apply/runs", None), ("get", f"/api/auto-apply/runs/{run_id}", None),
            ("get", f"/api/auto-apply/candidates/{cand}/eligibility", None),
            ("post", "/api/auto-apply/runs", {"candidate_id": cand, "assignment_ids": [assignment_id]}),
            ("patch", f"/api/auto-apply/runs/{run_id}/answers", {"answers": [{"field_key": "phone", "value": "1"}]}),
            ("post", f"/api/auto-apply/runs/{run_id}/approve", {"answers_hash": "0" * 64}),
            ("post", f"/api/auto-apply/runs/{run_id}/cancel", None),
            ("post", "/api/auto-apply/runs/approve-batch", {"items": [{"run_id": run_id, "answers_hash": "0" * 64}]}),
            ("get", f"/api/candidates/{cand}/consents", None), ("post", f"/api/candidates/{cand}/consents", {"method": "email"}),
        ]:
            r = await getattr(client, method)(path, **({"json": body} if body is not None else {}))
            assert r.status_code == 403, (method, path, r.status_code)
    tl = await world.as_tl("alpha")
    assert (await tl.patch(f"/api/candidates/{cand}/application-answers", json={"willing_to_relocate": "yes"})).status_code == 200
    assert (await (await world.as_tl("beta")).get(f"/api/candidates/{cand}/application-answers")).status_code == 404
    member = await world.as_member("a1")
    assert (await member.get(f"/api/candidates/{cand}/application-answers")).status_code == 403
    unknown = str(uuid.uuid4())
    assert (await admin.get(f"/api/candidates/{unknown}/consents")).status_code == 404
    assert (await admin.post("/api/auto-apply/runs", json={"candidate_id": unknown, "assignment_ids": [assignment_id]})).status_code == 404
    assert (await admin.get(f"/api/auto-apply/runs/{unknown}")).status_code == 404
    other = (await admin.post("/api/auto-apply/runs", json={"candidate_id": str(world.ids["cand_b1"]), "assignment_ids": [assignment_id]}))
    assert other.status_code == 409                                       # candidate b1 has no consent


async def test_consent_required_revocation_cancels_and_disabled(aa, fake):
    world = aa
    admin, cand, assignment_id = await setup_candidate(world, fake, consent=False)
    r = await admin.post("/api/auto-apply/runs", json={"candidate_id": cand, "assignment_ids": [assignment_id]})
    assert r.status_code == 409 and "consent" in r.json()["error"]
    consent = (await admin.post(f"/api/candidates/{cand}/consents", json={"method": "e_signature"})).json()
    assert consent["active"] and consent["may_attest"] is False
    world.queue.auto_apply_runner, saved = None, world.queue.auto_apply_runner          # leave the run queued
    run = (await admin.post("/api/auto-apply/runs", json={"candidate_id": cand, "assignment_ids": [assignment_id]})).json()["runs"][0]
    world.queue.auto_apply_runner = saved
    r = await admin.post(f"/api/candidates/{cand}/consents/{consent['id']}/revoke", json={"note": "candidate asked"})
    assert r.status_code == 200 and r.json()["active"] is False
    detail = (await admin.get(f"/api/auto-apply/runs/{run['id']}")).json()
    assert detail["status"] == "cancelled" and detail["error"] == "consent_revoked"
    await aa_runner.prepare(uuid.UUID(run["id"]), world.aa_deps)             # a queued job arriving later does nothing
    assert (await admin.get(f"/api/auto-apply/runs/{run['id']}")).json()["status"] == "cancelled"
    world.settings.auto_apply_enabled = False
    r = await admin.post("/api/auto-apply/runs", json={"candidate_id": cand, "assignment_ids": [assignment_id]})
    assert r.status_code == 409 and "switched off" in r.json()["error"]


async def test_limits_and_batch_refusals(aa, fake):
    world = aa
    admin, cand, assignment_id = await setup_candidate(world, fake)
    world.queue.auto_apply_runner, saved = None, world.queue.auto_apply_runner
    first = (await admin.post("/api/auto-apply/runs", json={"candidate_id": cand, "assignment_ids": [assignment_id]})).json()
    second = (await admin.post("/api/auto-apply/runs", json={"candidate_id": cand, "assignment_ids": [assignment_id, str(uuid.uuid4())]})).json()
    world.queue.auto_apply_runner = saved
    assert len(first["runs"]) == 1 and {s["reason"] for s in second["skipped"]} == {"run_open", "not_found"}
    run_id = first["runs"][0]["id"]
    r = await admin.post("/api/auto-apply/runs/approve-batch", json={"items": [{"run_id": run_id, "answers_hash": "0" * 64},
                                                                              {"run_id": str(uuid.uuid4()), "answers_hash": "0" * 64}]})
    assert {x["reason"] for x in r.json()["refused"]} == {"not_ready", "not_found"} and r.json()["approved"] == []
    await aa_runner.prepare(uuid.UUID(run_id), world.aa_deps)
    detail = (await admin.get(f"/api/auto-apply/runs/{run_id}")).json()
    assert detail["status"] == "ready_for_review", detail["needs_input"]
    world.settings.auto_apply_employer_daily_cap = 0
    r = await admin.post("/api/auto-apply/runs/approve-batch", json={"items": [{"run_id": run_id, "answers_hash": detail["answers_hash"]}]})
    assert r.json()["refused"][0]["reason"] == "employer_daily_cap"
    world.settings.auto_apply_employer_daily_cap = 5
    world.queue.auto_apply_runner = None                         # run the submission by hand below
    r = await admin.post("/api/auto-apply/runs/approve-batch", json={"items": [{"run_id": run_id, "answers_hash": detail["answers_hash"]}]})
    world.queue.auto_apply_runner = saved
    assert r.json()["approved"] == [run_id]
    # A second posting-level submission is impossible even if a run were forced into "approved": the database refuses it.
    async with world.sessions() as db:
        run = await db.get(AutoApplyRun, uuid.UUID(run_id))
        db.add(AutoApplyRun(candidate_id=run.candidate_id, assignment_id=run.assignment_id, job_id=run.job_id, status="submitted",
                            ats="greenhouse", job_url=run.job_url))
        await db.commit()
    before = len(fake.submissions())
    await aa_runner.submit(uuid.UUID(run_id), world.aa_deps)
    detail = (await admin.get(f"/api/auto-apply/runs/{run_id}")).json()
    assert detail["status"] == "failed" and "already_submitted" in detail["error"] and len(fake.submissions()) == before


async def test_needs_input_then_admin_edit_and_blockers(aa, fake):
    world = aa
    admin, cand, assignment_id = await setup_candidate(world, fake, host=3, path="/unknown/j/UNK999/apply/", company="Unknown")
    run_id = (await start(world, admin, cand, assignment_id))["runs"][0]["id"]
    detail = (await admin.get(f"/api/auto-apply/runs/{run_id}")).json()
    assert detail["status"] == "blocked" and "data_processing_consent_not_permitted" in [b["code"] for b in detail["blocked_reasons"]]
    needs = {n["label"]: n["fill_in"] for n in detail["needs_input"]}
    assert needs["T-shirt size"] == "run_answer" and needs["What are your salary expectations?"] == "application_answers.expected_salary"
    # Consent covering the privacy notice, recorded answers, and the Admin answers the unknown field.
    await admin.post(f"/api/candidates/{cand}/consents", json={"method": "written", "may_accept_data_processing": True})
    await admin.patch(f"/api/candidates/{cand}/application-answers", json={"expected_salary": "$120,000", "notice_period": "2 weeks"})
    world.queue.auto_apply_runner, saved = None, world.queue.auto_apply_runner
    run_id = (await admin.post("/api/auto-apply/runs", json={"candidate_id": cand, "assignment_ids": [assignment_id]})).json()["runs"][0]["id"]
    world.queue.auto_apply_runner = saved
    await aa_runner.prepare(uuid.UUID(run_id), world.aa_deps)
    detail = (await admin.get(f"/api/auto-apply/runs/{run_id}")).json()
    assert detail["status"] == "needs_input" and [n["label"] for n in detail["needs_input"]] == ["T-shirt size"]
    r = await admin.patch(f"/api/auto-apply/runs/{run_id}/answers", json={"answers": [{"field_key": "privacy", "value": "checked"}]})
    assert r.status_code == 422                                  # certifications are never typed in by hand
    r = await admin.patch(f"/api/auto-apply/runs/{run_id}/answers", json={"answers": [{"field_key": "shirt", "value": "M"}]})
    assert r.status_code == 200 and r.json()["status"] == "preparing"
    await world.queue.drain()
    detail = (await admin.get(f"/api/auto-apply/runs/{run_id}")).json()
    assert detail["status"] == "ready_for_review", detail["needs_input"]
    assert next(a for a in detail["answers"] if a["field_key"] == "shirt")["source"] == "admin_edit"
    assert (await admin.post(f"/api/auto-apply/runs/{run_id}/cancel")).json()["status"] == "cancelled"


async def test_no_resume_file_and_unsupported_site(aa, fake):
    world = aa
    admin, cand, assignment_id = await setup_candidate(world, fake, host=2, path=f"/acme/{ASHBY_ID}/application")
    async with world.sessions() as db:
        await db.execute(update(Resume).where(Resume.candidate_id == uuid.UUID(cand)).values(file_data=None))
        await db.commit()
    run_id = (await start(world, admin, cand, assignment_id))["runs"][0]["id"]
    detail = (await admin.get(f"/api/auto-apply/runs/{run_id}")).json()
    assert detail["status"] == "blocked" and "no_resume_file" in [b["code"] for b in detail["blocked_reasons"]]
    job = await add_job(world, company="Workday Co", apply_url="https://acme.wd5.myworkdayjobs.com/en-US/careers/job/1")
    other = (await admin.post("/api/assignments", json={"job_id": job, "candidate_id": cand})).json()["assignment_ids"][0]
    item = next(i for i in (await admin.get(f"/api/auto-apply/candidates/{cand}/eligibility")).json()["items"] if i["assignment_id"] == other)
    assert (item["supported"], item["reason_code"]) == (False, "account_creation_required")
    run_id = (await start(world, admin, cand, other))["runs"][0]["id"]
    detail = (await admin.get(f"/api/auto-apply/runs/{run_id}")).json()
    assert detail["status"] == "blocked" and detail["blocked_reasons"][0]["code"] == "account_creation_required"


async def test_worker_stop_while_submitting_is_outcome_unknown_and_never_retried(aa, fake):
    world = aa
    admin, cand, assignment_id = await setup_candidate(world, fake)
    world.queue.auto_apply_runner, saved = None, world.queue.auto_apply_runner
    run_id = (await admin.post("/api/auto-apply/runs", json={"candidate_id": cand, "assignment_ids": [assignment_id]})).json()["runs"][0]["id"]
    world.queue.auto_apply_runner = saved
    async with world.sessions() as db:
        await db.execute(update(AutoApplyRun).where(AutoApplyRun.id == uuid.UUID(run_id)).values(status="submitting"))
        await db.commit()
    await aa_runner.recover(world.aa_deps)
    detail = (await admin.get(f"/api/auto-apply/runs/{run_id}")).json()
    assert detail["status"] == "failed" and detail["outcome_unknown"] is True
    created = (await admin.post("/api/auto-apply/runs", json={"candidate_id": cand, "assignment_ids": [assignment_id]})).json()
    assert created["skipped"][0]["reason"] == "already_submitted"
    before = len(fake.submissions())
    await aa_runner.submit(uuid.UUID(run_id), world.aa_deps)               # a stray submit job does nothing
    assert len(fake.submissions()) == before


async def test_screenshot_retention(aa, fake):
    world = aa
    admin, cand, assignment_id = await setup_candidate(world, fake)
    run_id = (await start(world, admin, cand, assignment_id))["runs"][0]["id"]
    assert (await admin.get(f"/api/auto-apply/runs/{run_id}")).json()["screenshots"]
    world.settings.auto_apply_screenshot_retention_days = 0
    assert await aa_runner.cleanup(world.aa_deps) >= 1
    assert (await admin.get(f"/api/auto-apply/runs/{run_id}")).json()["screenshots"] == []


# ---------------------------------------------------------------- migration
NAME = PLAIN_URL.rsplit("/", 1)[1] + "_aa_migration"
MURL, MPLAIN = TEST_URL.rsplit("/", 1)[0] + "/" + NAME, PLAIN_URL.rsplit("/", 1)[0] + "/" + NAME


def alembic(*args: str) -> str:
    result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, capture_output=True, text=True,
                            env={**os.environ, "ALEMBIC_DATABASE_URL": MURL})
    assert result.returncode == 0, result.stderr[-1500:]
    return result.stdout


async def test_auto_apply_migration_upgrades_and_downgrades_with_data(database):
    admin_conn = await asyncpg.connect(PLAIN_URL.rsplit("/", 1)[0] + "/postgres")
    await admin_conn.execute(f'DROP DATABASE IF EXISTS "{NAME}"')
    await admin_conn.execute(f'CREATE DATABASE "{NAME}"')
    try:
        alembic("upgrade", "b5e8d2c47a19")
        conn = await asyncpg.connect(MPLAIN)
        team, cand = uuid.uuid4(), uuid.uuid4()
        await conn.execute("INSERT INTO teams (id, name) VALUES ($1, 'T')", team)
        await conn.execute("INSERT INTO candidates (id, team_id, name) VALUES ($1, $2, 'C')", cand, team)
        await conn.execute("INSERT INTO candidate_profiles (candidate_id) VALUES ($1)", cand)
        await conn.close()
        alembic("upgrade", "d3f6a1b8c925")
        assert "d3f6a1b8c925 (head)" in alembic("current")
        conn = await asyncpg.connect(MPLAIN)
        assert await conn.fetchval("SELECT application_answers FROM candidate_profiles WHERE candidate_id = $1", cand) == "{}"
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("INSERT INTO candidate_consents (id, candidate_id, method) VALUES (gen_random_uuid(), $1, 'telepathy')", cand)
        await conn.execute("INSERT INTO candidate_consents (id, candidate_id, method) VALUES (gen_random_uuid(), $1, 'written')", cand)
        await conn.close()
        check = subprocess.run([sys.executable, "-m", "alembic", "check"], cwd=ROOT, capture_output=True, text=True,
                               env={**os.environ, "ALEMBIC_DATABASE_URL": MURL})
        assert "No new upgrade operations detected" in check.stdout, check.stdout[-800:] + check.stderr[-800:]
        alembic("downgrade", "b5e8d2c47a19")
        conn = await asyncpg.connect(MPLAIN)
        assert not await conn.fetchval("SELECT to_regclass('auto_apply_runs') IS NOT NULL")
        assert not await conn.fetchval("SELECT to_regclass('candidate_consents') IS NOT NULL")
        assert await conn.fetchval("SELECT count(*) FROM candidate_profiles") == 1
        await conn.close()
        alembic("upgrade", "head")
    finally:
        await admin_conn.execute(f'DROP DATABASE IF EXISTS "{NAME}" WITH (FORCE)')
        await admin_conn.close()
