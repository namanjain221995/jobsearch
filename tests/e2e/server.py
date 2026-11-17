"""Deterministic stack for UI development and browser tests.

    python -m tests.e2e.server --reset          # API on http://127.0.0.1:8011, demo data re-created

- Uses its own database (must be named *_e2e; created and migrated automatically). Never a real one.
- Searches run inside this process with the real search engine and runner, but every HTTP call is answered
  from synthetic fixtures: no network, no AI cost, same result every time.
- Demo accounts (this database only), password "Demo-Password-42!":
    admin@example.com · lead.alpha@example.com · lead.beta@example.com
    maya@example.com · noah@example.com · omar@example.com (Team Alpha) · priya@example.com · quinn@example.com (Team Beta)
- Members are recruiters; the people who need a job are candidates (records, no accounts). Seeded candidates:
    Team Alpha: Jordan Rivera, Elena Petrova (Maya's) · Samuel Okafor (Noah's) · Hannah Kim (unassigned, has jobs from
                a search) · Diego Morales (unassigned, no resume, incomplete profile). Omar holds no candidate.
    Team Beta:  Aisha Rahman, Tom Becker (Priya's) · Grace Liu (Quinn's) · Lucas Meyer (unassigned) ·
                Victor Hale (unassigned and deactivated).
  Every candidate except Diego has a resume whose file can be downloaded (a .txt file). Jordan, Samuel and Aisha have
  jobs in varied statuses, recorded by their responsible member; Hannah's jobs are all still "ASSIGNED".
  Jordan also has a finished "review" search with nothing assigned from it.
- Salesforce is replaced by an in-process fake with twelve synthetic candidates (tests/e2e/salesforce_fake.py lists
  them): search "ch", "am", "de" ... as lead.alpha@example.com. Two are already in the portal: Amara Nwosu in Team
  Alpha and Elif Demir in Team Beta, both brought in through the real import code when the demo data is seeded."""
import argparse
import asyncio
import hashlib
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import timedelta

import asyncpg
import httpx
import uvicorn
from sqlalchemy import select, update

from app.config import Settings
from app.portal import security
from app.portal.app import create_app
from app.portal.deps import Principal
from app.portal.index_pg import PgJobIndex
from app.portal.models import (ApplicationEvent, Candidate, CandidateProfile, JobAssignment, Resume, SearchRun, SearchTarget, Team,
                               TeamJobQueue, TeamLeadAssignment, User)
from app.portal.queue import InlineQueue
from app.portal.schemas import JobCreate, StatusUpdate
from app.portal.search_runner import RunnerDeps, execute_search
from app.portal.security import utcnow
from app.portal.services import assignments as assignments_service
from app.portal.services import jobs as jobs_service
from app.portal.services import salesforce_sync
from app.search_engine import SearchEngine
from tests.conftest import RESUME, FakeAI, FakeBrowser
from tests.e2e.fixtures import SEEDS, handler
from tests.e2e.salesforce_fake import IMPORTED_ALPHA, IMPORTED_BETA, FakeSalesforce

URL = os.environ.get("E2E_DATABASE_URL", "postgresql+asyncpg://jobfinder:jobfinder@127.0.0.1:55432/jobfinder_e2e")
PLAIN = URL.replace("postgresql+asyncpg://", "postgresql://", 1)
PASSWORD = "Demo-Password-42!"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PEOPLE = {"alpha": ("Team Alpha", "Lena Alpha", [("Maya Chen", "maya"), ("Noah Patel", "noah"), ("Omar Haddad", "omar")]),
          "beta": ("Team Beta", "Ben Beta", [("Priya Nair", "priya"), ("Quinn Lopez", "quinn")])}
# team -> (candidate name, key, responsible member's slug or None, has a resume, active)
CANDIDATES = {"alpha": [("Jordan Rivera", "jordan", "maya", True, True), ("Elena Petrova", "elena", "maya", True, True),
                        ("Samuel Okafor", "samuel", "noah", True, True), ("Hannah Kim", "hannah", None, True, True),
                        ("Diego Morales", "diego", None, False, True)],
              "beta": [("Aisha Rahman", "aisha", "priya", True, True), ("Tom Becker", "tom", "priya", True, True),
                       ("Grace Liu", "grace", "quinn", True, True), ("Lucas Meyer", "lucas", None, True, True),
                       ("Victor Hale", "victor", None, True, False)]}


def settings() -> Settings:
    return Settings(_env_file=None, database_url=URL, openai_api_key="", anthropic_api_key="", app_env="e2e",
                    public_base_url=os.environ.get("E2E_PUBLIC_BASE_URL", "http://127.0.0.1:3011"),
                    allowed_origins="http://localhost:3011,http://127.0.0.1:3011,http://localhost:3010,http://127.0.0.1:3010",
                    prewarm_on_start=False, browser_enabled=False, fetch_deadline_seconds=10, login_max_failures=50)


async def prepare_database(reset: bool) -> None:
    if not PLAIN.rsplit("/", 1)[1].endswith("_e2e"):
        sys.exit("Refusing to run: the E2E database name must end with _e2e")
    base, name = PLAIN.rsplit("/", 1)
    conn = await asyncpg.connect(base + "/postgres")
    if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", name):
        await conn.execute(f'CREATE DATABASE "{name}"')
    await conn.close()
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, capture_output=True, text=True,
                            env={**os.environ, "ALEMBIC_DATABASE_URL": URL})
    if result.returncode != 0:
        sys.exit("alembic upgrade failed: " + result.stderr[-600:])
    if reset:
        conn = await asyncpg.connect(PLAIN)
        tables = [r["tablename"] for r in await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'")]
        await conn.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE")
        await conn.close()


async def seed(sessions, deps: RunnerDeps, salesforce: FakeSalesforce) -> None:
    hashed = security.hash_password(PASSWORD)
    ids: dict[str, object] = {}
    async with sessions() as db:
        if (await db.execute(select(User.id).limit(1))).first():
            return                                           # already seeded
        admin = User(name="Avery Admin", email="admin@example.com", role="ADMIN", password_hash=hashed)
        db.add(admin)
        for key, (team_name, lead_name, members) in PEOPLE.items():
            team = Team(name=team_name, description=f"Demo team {key}")
            db.add(team)
            await db.flush()
            lead = User(name=lead_name, email=f"lead.{key}@example.com", role="TEAM_LEAD", team_id=team.id, password_hash=hashed)
            db.add(lead)
            await db.flush()
            db.add(TeamLeadAssignment(team_id=team.id, user_id=lead.id))
            ids[f"team_{key}"], ids[f"lead_{key}"] = team, lead
            for name, slug in members:
                member = User(name=name, email=f"{slug}@example.com", role="MEMBER", team_id=team.id, password_hash=hashed)
                db.add(member)
                await db.flush()
                ids[slug] = member
            for i, (name, slug, owner, has_resume, active) in enumerate(CANDIDATES[key]):
                candidate = Candidate(name=name, email=f"{slug}.candidate@example.com" if i != 1 else "", team_id=team.id,
                                      phone=f"+1 555 01{len(ids):02d}" if i != 4 else "", is_active=active, created_by=lead.id,
                                      notes="Prefers employers with a clear remote policy." if i == 0 else "",
                                      assigned_member_id=ids[owner].id if owner else None)
                db.add(candidate)
                await db.flush()
                db.add(CandidateProfile(
                    candidate_id=candidate.id, desired_titles=["Machine Learning Engineer", "AI Engineer"] if i != 2 else ["Data Scientist"],
                    years_experience=3 + i, skills=["Python", "PyTorch", "SQL", "AWS"] if has_resume else [],
                    preferred_states=["NY", "WA"] if i == 0 else [], preferred_cities=[], excluded_companies=[], required_keywords=[],
                    extra={}, work_authorization="Green Card / US Permanent Resident", sponsorship_needed="No",
                    exclude_citizenship_required=True, work_mode="All"))
                if has_resume:
                    text = RESUME.replace("Jordan Rivera", name)
                    db.add(Resume(candidate_id=candidate.id, filename=f"{slug}-resume.txt", content_type="text/plain",
                                  size_bytes=len(text.encode()), sha256=hashlib.sha256(text.encode()).hexdigest(),
                                  text=text, file_data=text.encode(), version=1, is_current=True, uploaded_by=lead.id,
                                  parsed_profile={"skills": ["Python", "PyTorch", "TensorFlow", "SQL", "Docker", "AWS"],
                                                  "job_titles": ["Machine Learning Engineer", "Software Engineer"], "education": ["B.S. Computer Science"]}))
                ids[slug] = candidate
        ids["admin"] = admin
        await db.commit()

    # Two candidates that came from (the fake) Salesforce, one per team, through the real import code.
    async with sessions() as db:
        for salesforce_id, lead in ((IMPORTED_ALPHA, ids["lead_alpha"]), (IMPORTED_BETA, ids["lead_beta"])):
            principal = Principal(await db.get(User, lead.id), None)
            fetched = salesforce_sync.Fetched(candidate=await salesforce.get_candidate(salesforce_id), instance_url=salesforce.instance_url)
            await salesforce_sync.fetch_resume(salesforce, deps.settings, fetched, set())
            candidate = await salesforce_sync.new_candidate(db, principal, fetched, None, None, "")
            await salesforce_sync.apply(db, deps.settings, principal, candidate, fetched, "", created=True)
        await db.commit()

    # Real searches (synthetic network) so results, match explanations and assignments are genuine pipeline output.
    async def run(lead, candidates, mode="auto_assign", titles=None, count=14):
        async with sessions() as db:
            search = SearchRun(initiated_by=lead.id, team_id=lead.team_id, mode=mode, target_count=count, deep_search=False,
                               min_match_score=50, search_filters={"job_titles": titles} if titles else {}, status="queued", progress={})
            db.add(search)
            await db.flush()
            for c in candidates:
                profile = (await db.execute(select(CandidateProfile).where(CandidateProfile.candidate_id == c.id))).scalar_one()
                db.add(SearchTarget(search_run_id=search.id, candidate_id=c.id, assignment_mode=mode, status="queued", stats={},
                                    candidate_specific_filters={"job_titles": titles or profile.desired_titles, "years_experience": float(profile.years_experience or 0),
                                                                "work_authorization": profile.work_authorization, "sponsorship_needed": profile.sponsorship_needed,
                                                                "exclude_citizenship_required": True, "work_mode": "All",
                                                                "preferred_states": [], "excluded_companies": [], "skills": profile.skills}))
            await db.commit()
            run_id = search.id
        await execute_search(run_id, deps)
        return run_id

    await run(ids["lead_alpha"], [ids["jordan"], ids["samuel"]])
    await run(ids["lead_alpha"], [ids["jordan"]], mode="review", titles=["Data Scientist", "Backend Engineer"], count=6)
    await run(ids["lead_alpha"], [ids["hannah"]], count=5)        # a candidate needs no responsible member to be searched
    await run(ids["lead_beta"], [ids["aisha"]], count=8)

    # Application progress spread over the last two weeks (so charts have a shape), through the real status service.
    async with sessions() as db:
        now = utcnow()
        # Each candidate's jobs are worked on by the member responsible for that candidate.
        for slug, plan in (("jordan", ["OFFER", "INTERVIEWING", "APPLIED", "APPLIED", "APPLIED", "REJECTED", "SKIPPED", "VIEWED", "APPLYING"]),
                           ("samuel", ["APPLIED", "APPLIED", "INTERVIEWING", "VIEWED"]), ("aisha", ["APPLIED", "REJECTED", "VIEWED"])):
            candidate = ids[slug]
            member = await db.get(User, candidate.assigned_member_id)
            principal = Principal(member, None)
            rows = list((await db.execute(select(JobAssignment).where(JobAssignment.candidate_id == candidate.id)
                                          .order_by(JobAssignment.match_score.desc().nulls_last(), JobAssignment.id))).scalars())
            for n, (assignment, status) in enumerate(zip(rows, plan)):
                assigned = now - timedelta(days=13 - n % 6)
                assignment.assigned_at = assigned
                await assignments_service.mark_viewed(db, principal, assignment)
                if status in ("APPLIED", "INTERVIEWING", "OFFER", "REJECTED", "APPLYING"):
                    await assignments_service.record_link_open(db, principal, assignment)
                if status != "VIEWED":
                    applied_at = assigned + timedelta(days=1 + n % 4) if status in ("APPLIED", "INTERVIEWING", "OFFER", "REJECTED") else None
                    await assignments_service.set_status(db, principal, assignment, StatusUpdate(
                        status=status, applied_at=applied_at, reference=f"REF-{1000 + n}" if status == "APPLIED" else None,
                        note="Recruiter replied within a day." if status == "INTERVIEWING" else None))
            await db.execute(update(JobAssignment).where(JobAssignment.candidate_id == candidate.id, JobAssignment.assigned_at > now - timedelta(minutes=5))
                             .values(assigned_at=now - timedelta(days=2)))
        await db.flush()
        # Spread event dates to match (events were all written "now").
        events = list((await db.execute(select(ApplicationEvent, JobAssignment).join(JobAssignment, JobAssignment.id == ApplicationEvent.assignment_id))).all())
        for event, assignment in events:
            base = assignment.applied_at if event.new_status in ("APPLIED", "INTERVIEWING", "OFFER", "REJECTED") and assignment.applied_at else assignment.assigned_at
            event.created_at = min(now, base + timedelta(hours=2 + len(str(event.id)) % 5))
        # Manually entered jobs delegated by the Admin to Team Beta (pending in its queue).
        admin = await db.get(User, ids["admin"].id)
        for n, (company, title, city, state) in enumerate((("Northwind Labs", "AI Engineer", "Austin", "TX"), ("Contoso Health", "ML Engineer", "Seattle", "WA"),
                                                           ("Fabrikam Energy", "Data Scientist", "Houston", "TX"))):
            posting = await jobs_service.create_manual(db, admin, JobCreate(
                company=company, title=title, city=city, state=state, work_mode="Hybrid", salary_min=140000, salary_max=180000, salary_currency="USD",
                experience_text="3+ years", requirements=["Python", "SQL"], canonical_url=f"https://careers.example.org/{company.split()[0].lower()}/{n}",
                description=f"About the Company\n{company} is hiring.\n\nResponsibilities\n• Build models\n• Ship features\n\nRequired Qualifications\n• 3+ years of experience"))
            db.add(TeamJobQueue(team_id=ids["team_beta"].id, job_id=posting.id, delegated_by=admin.id, note="From the conference list"))
        await db.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="drop all demo data and seed again")
    parser.add_argument("--port", type=int, default=int(os.environ.get("E2E_API_PORT", "8011")))
    args = parser.parse_args()
    asyncio.run(prepare_database(args.reset))
    cfg = settings()
    holder: dict = {}

    async def runner(run_id):
        await execute_search(run_id, holder["deps"])

    app = create_app(cfg, queue=InlineQueue(runner), use_redis=False)
    portal_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app_):
        async with portal_lifespan(app_):
            index = await PgJobIndex(PLAIN).connect()
            fake_ai = FakeAI(available=False)
            engine = SearchEngine(cfg, index, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), seeds=SEEDS,
                                  ai_factory=lambda: fake_ai, sources=["greenhouse", "lever"], browser=FakeBrowser(available=False))
            engine.limiter.intervals = {}
            holder["deps"] = RunnerDeps(sessions=app_.state.sessions, engine=engine, settings=cfg)
            await app_.state.salesforce.aclose()
            app_.state.salesforce = FakeSalesforce()          # the demo stack never talks to the real Salesforce
            await seed(app_.state.sessions, holder["deps"], app_.state.salesforce)
            try:
                yield
            finally:
                await engine.aclose()
                await index.close()

    app.router.lifespan_context = lifespan

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
