"""Revision c9e4b7a2d615 (an invitation may carry the role ADMIN) on a database that holds data: upgrade from the
previous head, what the database then accepts and still refuses, downgrade with Admin invitations present, and
upgrade again. It runs in a database of its own, created and dropped here."""
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from tests.portal.conftest import PLAIN_URL, ROOT, TEST_URL

NAME = PLAIN_URL.rsplit("/", 1)[1] + "_admin_invite_migration"
URL, PLAIN = TEST_URL.rsplit("/", 1)[0] + "/" + NAME, PLAIN_URL.rsplit("/", 1)[0] + "/" + NAME
OLD, NEW = "a8c5e3f1d2b7", "c9e4b7a2d615"
INVITE = ("INSERT INTO invitations (id, email, name, invited_role, team_id, user_id, token_hash, invited_by, expires_at) "
          "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)")


def alembic(*args: str) -> str:
    result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, capture_output=True, text=True,
                            env={**os.environ, "ALEMBIC_DATABASE_URL": URL})
    assert result.returncode == 0, result.stderr[-1500:]
    return result.stdout


async def test_admin_invitation_revision_upgrades_and_downgrades_with_data(database):
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
    team, owner, lead, member, second, third = (uuid.uuid4() for _ in range(6))
    later = datetime.now(timezone.utc) + timedelta(hours=72)
    conn = await asyncpg.connect(PLAIN)
    await conn.execute("INSERT INTO teams (id, name) VALUES ($1, 'Team One')", team)
    await conn.executemany("INSERT INTO users (id, name, email, role, team_id, password_hash) VALUES ($1, $2, $3, $4, $5, $6)", [
        (owner, "Olga Owner", "owner@example.com", "ADMIN", None, "hash"), (lead, "Lena Lead", "lead@example.com", "TEAM_LEAD", team, None),
        (member, "Mia Member", "member@example.com", "MEMBER", team, None), (second, "Sam Second", "second@example.com", "ADMIN", None, None),
        (third, "Tina Third", "third@example.com", "ADMIN", None, "hash")])
    await conn.execute(INVITE, uuid.uuid4(), "lead@example.com", "Lena Lead", "TEAM_LEAD", team, lead, "a" * 64, owner, later)
    await conn.execute(INVITE, uuid.uuid4(), "member@example.com", "Mia Member", "MEMBER", team, member, "b" * 64, lead, later)
    # Before the revision the database itself refuses an Admin invitation.
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(INVITE, uuid.uuid4(), "second@example.com", "Sam Second", "ADMIN", None, second, "c" * 64, owner, later)
    await conn.close()

    # ---- upgrade: nothing about the existing rows changes; ADMIN is accepted, anything else is still refused
    alembic("upgrade", NEW)
    conn = await asyncpg.connect(PLAIN)
    assert await conn.fetchval("SELECT version_num FROM alembic_version") == NEW
    assert [tuple(r) for r in await conn.fetch("SELECT email, invited_role FROM invitations ORDER BY email")] == [
        ("lead@example.com", "TEAM_LEAD"), ("member@example.com", "MEMBER")]
    constraint = await conn.fetchrow("SELECT convalidated, pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conname = 'ck_invitations_role'")
    assert constraint["convalidated"] is True and all(role in constraint["definition"] for role in ("'ADMIN'", "'TEAM_LEAD'", "'MEMBER'"))
    await conn.execute(INVITE, uuid.uuid4(), "second@example.com", "Sam Second", "ADMIN", None, second, "c" * 64, owner, later)
    await conn.execute(INVITE, uuid.uuid4(), "third@example.com", "Tina Third", "ADMIN", None, third, "d" * 64, owner, later)
    await conn.execute("UPDATE invitations SET accepted_at = now() WHERE email = 'third@example.com'")
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(INVITE, uuid.uuid4(), "x@example.com", "X Y", "OWNER", None, second, "e" * 64, owner, later)
    await conn.close()

    # ---- downgrade: the narrower rule cannot hold Admin invitations; they go, and an Admin who never chose a
    # password is deactivated (the previous release could not send a new link). Everybody else is untouched.
    alembic("downgrade", OLD)
    conn = await asyncpg.connect(PLAIN)
    assert [tuple(r) for r in await conn.fetch("SELECT email, invited_role FROM invitations ORDER BY email")] == [
        ("lead@example.com", "TEAM_LEAD"), ("member@example.com", "MEMBER")]
    assert {r["email"]: r["is_active"] for r in await conn.fetch("SELECT email, is_active FROM users")} == {
        "owner@example.com": True, "lead@example.com": True, "member@example.com": True, "second@example.com": False, "third@example.com": True}
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(INVITE, uuid.uuid4(), "second@example.com", "Sam Second", "ADMIN", None, second, "f" * 64, owner, later)
    await conn.close()

    # ---- and up again
    alembic("upgrade", "head")
    conn = await asyncpg.connect(PLAIN)
    await conn.execute(INVITE, uuid.uuid4(), "second@example.com", "Sam Second", "ADMIN", None, second, "f" * 64, owner, later)
    assert await conn.fetchval("SELECT count(*) FROM invitations") == 3
    await conn.close()
