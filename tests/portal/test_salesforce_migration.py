"""Revision e7b31f08a5c2 (Salesforce link on candidates and resumes) on a database that holds data: upgrade from the
previous head, what existing rows look like afterwards, the rules the database itself enforces, downgrade, and
upgrade again. It runs in a database of its own, created and dropped here."""
import os
import subprocess
import sys
import uuid

import asyncpg
import pytest

from tests.portal.conftest import PLAIN_URL, ROOT, TEST_URL

NAME = PLAIN_URL.rsplit("/", 1)[1] + "_sf_migration"
URL, PLAIN = TEST_URL.rsplit("/", 1)[0] + "/" + NAME, PLAIN_URL.rsplit("/", 1)[0] + "/" + NAME
OLD, NEW = "c4d2a9e71f36", "e7b31f08a5c2"
CANDIDATE_COLUMNS = {"salesforce_id", "salesforce_synced_at", "salesforce_snapshot"}
RESUME_COLUMNS = {"source", "salesforce_content_version_id"}
SF_ID = "001000000000001AAA"


def alembic(*args: str) -> str:
    result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, capture_output=True, text=True,
                            env={**os.environ, "ALEMBIC_DATABASE_URL": URL})
    assert result.returncode == 0, result.stderr[-1500:]
    return result.stdout


async def columns(conn, table: str) -> set[str]:
    return {r["column_name"] for r in await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = $1", table)}


async def test_salesforce_revision_upgrades_and_downgrades_with_data(database):
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
    team, one, two, resume = (uuid.uuid4() for _ in range(4))
    conn = await asyncpg.connect(PLAIN)
    assert not CANDIDATE_COLUMNS & await columns(conn, "candidates") and not RESUME_COLUMNS & await columns(conn, "resumes")
    await conn.execute("INSERT INTO teams (id, name) VALUES ($1, 'Team One')", team)
    await conn.executemany("INSERT INTO candidates (id, team_id, name, email, notes) VALUES ($1, $2, $3, $4, $5)",
                           [(one, team, "Olga Old", "olga@example.com", "Typed in by hand"), (two, team, "Otto Old", "", "")])
    await conn.execute("INSERT INTO candidate_profiles (candidate_id, desired_titles) VALUES ($1, '[\"AI Engineer\"]')", one)
    await conn.execute("INSERT INTO resumes (id, candidate_id, filename, size_bytes, sha256, text, file_data) VALUES ($1, $2, 'cv.pdf', 4, $3, 'text', $4)",
                       resume, one, "a" * 64, b"%PDF")
    before = await conn.fetchrow("SELECT * FROM candidates WHERE id = $1", one)
    await conn.close()

    # ---- upgrade: every existing candidate is a manual one, every existing resume an upload
    alembic("upgrade", NEW)
    assert NEW in alembic("current") and "(head)" in alembic("current")
    conn = await asyncpg.connect(PLAIN)
    assert CANDIDATE_COLUMNS <= await columns(conn, "candidates") and RESUME_COLUMNS <= await columns(conn, "resumes")
    after = await conn.fetchrow("SELECT * FROM candidates WHERE id = $1", one)
    assert {k: after[k] for k in before.keys()} == dict(before)                           # nothing that was there changed
    assert (after["salesforce_id"], after["salesforce_synced_at"], after["salesforce_snapshot"]) == (None, None, "{}")
    stored = await conn.fetchrow("SELECT * FROM resumes WHERE id = $1", resume)
    assert (stored["source"], stored["salesforce_content_version_id"], stored["filename"], bytes(stored["file_data"])) == ("upload", None, "cv.pdf", b"%PDF")
    assert await conn.fetchval("SELECT count(*) FROM candidates WHERE salesforce_id IS NULL") == 2      # many manual candidates are fine
    # One portal candidate per Salesforce candidate, an id of at most 18 characters, and only the two resume sources.
    await conn.execute("UPDATE candidates SET salesforce_id = $2, salesforce_synced_at = now(), salesforce_snapshot = '{\"fields\": {}}' WHERE id = $1", one, SF_ID)
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute("UPDATE candidates SET salesforce_id = $2 WHERE id = $1", two, SF_ID)
    with pytest.raises(asyncpg.StringDataRightTruncationError):
        await conn.execute("UPDATE candidates SET salesforce_id = $2 WHERE id = $1", two, SF_ID + "X")
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("UPDATE resumes SET source = 'elsewhere' WHERE id = $1", resume)
    await conn.execute("UPDATE resumes SET source = 'salesforce', salesforce_content_version_id = '068000000000001AAA' WHERE id = $1", resume)
    assert await conn.fetchval("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_candidates_salesforce_id'") == \
        "CREATE UNIQUE INDEX uq_candidates_salesforce_id ON public.candidates USING btree (salesforce_id) WHERE (salesforce_id IS NOT NULL)"
    await conn.close()

    # ---- downgrade with Salesforce data present: the links go, candidates, profiles and resumes stay
    alembic("downgrade", OLD)
    conn = await asyncpg.connect(PLAIN)
    assert not CANDIDATE_COLUMNS & await columns(conn, "candidates") and not RESUME_COLUMNS & await columns(conn, "resumes")
    assert await conn.fetchval("SELECT 1 FROM pg_indexes WHERE indexname = 'uq_candidates_salesforce_id'") is None
    assert await conn.fetchval("SELECT 1 FROM pg_constraint WHERE conname = 'ck_resumes_source'") is None
    down = await conn.fetchrow("SELECT * FROM candidates WHERE id = $1", one)
    assert {k: down[k] for k in before.keys() if k != "updated_at"} == {k: before[k] for k in before.keys() if k != "updated_at"}
    assert await conn.fetchval("SELECT count(*) FROM candidates") == 2 and await conn.fetchval("SELECT count(*) FROM candidate_profiles") == 1
    assert bytes(await conn.fetchval("SELECT file_data FROM resumes WHERE id = $1", resume)) == b"%PDF"
    await conn.close()

    # ---- and forward again: the same rows, as manual candidates and uploads
    alembic("upgrade", "head")
    conn = await asyncpg.connect(PLAIN)
    assert await conn.fetchval("SELECT count(*) FROM candidates WHERE salesforce_id IS NULL AND salesforce_snapshot = '{}'::jsonb") == 2
    assert await conn.fetchval("SELECT source FROM resumes WHERE id = $1", resume) == "upload"
    await conn.close()
    # The models and the migrated schema agree: autogenerate finds nothing to add or drop.
    check = subprocess.run([sys.executable, "-m", "alembic", "check"], cwd=ROOT, capture_output=True, text=True, env={**os.environ, "ALEMBIC_DATABASE_URL": URL})
    assert check.returncode == 0 and "No new upgrade operations detected" in check.stdout, check.stdout[-800:] + check.stderr[-800:]
