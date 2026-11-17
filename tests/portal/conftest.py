"""Portal test harness. Runs against a real PostgreSQL database (jobfinder_test) created from the Alembic
migrations, so the schema under test is the schema that ships. Tests are skipped when PostgreSQL is not reachable.
Searches use the real search engine and runner; only the network is replaced (synthetic fixtures from tests/conftest.py)."""
import asyncio
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.config import Settings
from app.portal import security
from app.portal.app import create_app
from app.portal.index_pg import PgJobIndex
from app.portal.models import MemberProfile, Resume, Team, TeamLeadAssignment, User
from app.portal.queue import InlineQueue
from app.portal.search_runner import RunnerDeps, execute_search
from app.search_engine import SearchEngine
from tests.conftest import DEFAULT_SEEDS, RESUME, FakeAI, FakeBrowser, handler

TEST_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://jobfinder:jobfinder@127.0.0.1:55432/jobfinder_test")
PLAIN_URL = TEST_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
PASSWORD = "Correct-Horse-42!"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _prepare_database() -> str | None:
    """Create jobfinder_test from scratch with `alembic upgrade head`. Returns a skip reason on failure."""
    async def go():
        admin_url, name = PLAIN_URL.rsplit("/", 1)
        try:
            conn = await asyncpg.connect(admin_url + "/postgres", timeout=5)
        except Exception as exc:
            return f"PostgreSQL is not reachable ({type(exc).__name__}); start it with: docker compose up -d postgres"
        try:
            if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", name):
                await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()
        conn = await asyncpg.connect(PLAIN_URL)
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.close()
        return None

    reason = asyncio.run(go())
    if reason:
        return reason
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, capture_output=True, text=True,
                            env={**os.environ, "ALEMBIC_DATABASE_URL": TEST_URL})
    return None if result.returncode == 0 else f"alembic upgrade failed: {result.stderr[-400:]}"


@pytest.fixture(scope="session")
def database():
    reason = _prepare_database()
    if reason:
        pytest.skip(reason)
    return TEST_URL


@dataclass
class World:
    """One Admin, two teams (Alpha, Beta), each with a Team Lead and two members. All active, password PASSWORD."""
    app: object
    settings: Settings
    sessions: object
    queue: InlineQueue
    engine: SearchEngine
    ids: dict = field(default_factory=dict)
    clients: list = field(default_factory=list)

    async def client(self, email: str | None = None, password: str = PASSWORD) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")
        self.clients.append(client)
        if email:
            response = await client.post("/api/auth/login", json={"email": email, "password": password})
            assert response.status_code == 200, response.text
            client.headers["x-csrf-token"] = client.cookies["jf_csrf"]
        return client

    async def as_admin(self):
        return await self.client("admin@example.com")

    async def as_tl(self, team: str = "alpha"):
        return await self.client(f"tl.{team}@example.com")

    async def as_member(self, name: str = "a1"):
        return await self.client(f"member.{name}@example.com")


def token_of(url: str) -> str:
    return re.search(r"token=([A-Za-z0-9_\-]+)", url).group(1)


@pytest_asyncio.fixture
async def world(database):
    settings = Settings(_env_file=None, database_url=TEST_URL, openai_api_key="", anthropic_api_key="",
                        public_base_url="http://testserver", prewarm_on_start=False, browser_enabled=False,
                        fetch_deadline_seconds=10, email_backend="", smtp_host="", app_env="test")
    conn = await asyncpg.connect(PLAIN_URL)
    tables = [r["tablename"] for r in await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'")]
    await conn.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE")
    await conn.close()

    index = await PgJobIndex(PLAIN_URL).connect()
    fake_ai = FakeAI(available=False)
    engine = SearchEngine(settings, index, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), seeds=DEFAULT_SEEDS,
                          ai_factory=lambda: fake_ai, sources=["greenhouse", "lever", "ashby"], browser=FakeBrowser(available=False))
    engine.limiter.intervals = {}
    engine.fake_ai = fake_ai
    holder = {}

    async def runner(run_id):
        await execute_search(run_id, holder["deps"])

    queue = InlineQueue(runner)
    app = create_app(settings, queue=queue, use_redis=False)
    async with app.router.lifespan_context(app):
        holder["deps"] = RunnerDeps(sessions=app.state.sessions, engine=engine, settings=settings)
        w = World(app=app, settings=settings, sessions=app.state.sessions, queue=queue, engine=engine)
        hashed = security.hash_password(PASSWORD)
        async with app.state.sessions() as db:
            admin = User(name="Ada Admin", email="admin@example.com", role="ADMIN", password_hash=hashed)
            db.add(admin)
            for key in ("alpha", "beta"):
                team = Team(name=f"Team {key.capitalize()}", description=f"{key} team")
                db.add(team)
                await db.flush()
                lead = User(name=f"Lead {key.capitalize()}", email=f"tl.{key}@example.com", role="TEAM_LEAD", team_id=team.id, password_hash=hashed)
                db.add(lead)
                await db.flush()
                db.add(TeamLeadAssignment(team_id=team.id, user_id=lead.id))
                w.ids[f"team_{key}"], w.ids[f"tl_{key}"] = team.id, lead.id
                for n in (1, 2):
                    code = f"{key[0]}{n}"
                    member = User(name=f"Member {code.upper()}", email=f"member.{code}@example.com", role="MEMBER", team_id=team.id, password_hash=hashed)
                    db.add(member)
                    await db.flush()
                    db.add(MemberProfile(user_id=member.id, desired_titles=["AI Engineer"], years_experience=3, skills=["Python", "PyTorch"],
                                         preferred_states=[], preferred_cities=[], excluded_companies=[], required_keywords=[], extra={},
                                         work_authorization="Green Card / US Permanent Resident", sponsorship_needed="No",
                                         exclude_citizenship_required=True))
                    db.add(Resume(user_id=member.id, filename="resume.txt", size_bytes=len(RESUME), sha256="0" * 64, text=RESUME,
                                  parsed_profile={"skills": ["Python"]}, version=1, is_current=True, uploaded_by=member.id))
                    w.ids[f"member_{code}"] = member.id
            w.ids["admin"] = admin.id
            await db.commit()
        try:
            yield w
        finally:
            await queue.close()
            for client in w.clients:
                await client.aclose()
            await engine.aclose()
            await index.close()


async def add_job(world: World, **fields) -> str:
    """A manually entered job (Admin). Returns its id."""
    admin = await world.as_admin()
    payload = {"company": "Northwind Labs", "title": "AI Engineer", "state": "TX", "city": "Austin", "work_mode": "Hybrid",
               "description": "Build ML systems with Python.\n\nResponsibilities:\n• Train models\n• Ship APIs",
               "canonical_url": f"https://careers.northwind.example/jobs/{uuid.uuid4().hex[:8]}", **fields}
    response = await admin.post("/api/jobs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]
