"""Ending test mode: an Admin deletes the test data once; without test mode nothing can be deleted from the screen."""
from sqlalchemy import func, select, text

from app.portal.models import AppSetting, AuditLog, Candidate, Session, Team, User
from app.portal.services import settings as settings_service


async def count(world, model, *where) -> int:
    async with world.sessions() as db:
        return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def test_go_live_removes_test_data_once_and_keeps_admin_and_settings(world):
    admin, tl, member = await world.as_admin(), await world.as_tl("alpha"), await world.as_member("a1")
    body = {"confirm": "REAL DATA"}
    # Not in test mode (the default): refused for everyone, nothing deleted. PATCH cannot switch test mode on.
    assert (await admin.get("/api/settings")).json()["test_mode"] is False
    assert (await admin.patch("/api/settings", json={"test_mode": True})).json()["test_mode"] is False
    refused = await admin.post("/api/admin/go-live", json=body)
    assert refused.status_code == 409 and "not in test mode" in refused.json()["error"]
    assert await count(world, Candidate) == 4 and await count(world, Team) == 2
    async with world.sessions() as db:
        await settings_service.set_test_mode(db, world.settings, True)
        await db.execute(text("INSERT INTO index_meta (key, value) VALUES ('rankcache:judge:abc', '{}'), ('searx:keep', '{}')"))
        await db.commit()
    assert (await admin.patch("/api/settings", json={"default_target_count": 40})).json()["test_mode"] is True
    # Only an Admin, only with the typed words.
    assert (await tl.post("/api/admin/go-live", json=body)).status_code == 403
    assert (await member.post("/api/admin/go-live", json=body)).status_code == 403
    assert (await admin.post("/api/admin/go-live", json={"confirm": "yes"})).status_code == 422
    assert await count(world, Candidate) == 4
    done = await admin.post("/api/admin/go-live", json=body)
    assert done.status_code == 200, done.text
    assert done.json()["removed"] == {"teams": 2, "users": 6, "candidates": 4, "searches": 0}
    assert await count(world, Candidate) == 0 and await count(world, Team) == 0 and await count(world, User) == 1
    assert await count(world, Session) == 1 and await count(world, AuditLog, AuditLog.action == "data.test_data_removed") == 1
    # The Admin is still signed in, the settings are kept, test mode is off, and a second call is refused.
    settings = await admin.get("/api/settings")
    assert settings.status_code == 200 and settings.json()["test_mode"] is False and settings.json()["default_target_count"] == 40
    assert (await tl.get("/api/auth/me")).status_code == 401
    assert (await admin.post("/api/admin/go-live", json=body)).status_code == 409
    assert await count(world, AppSetting) == 1
    async with world.sessions() as db:          # model judgements about the test candidates are gone; the rest of the job index stays
        assert [r[0] for r in await db.execute(text("SELECT key FROM index_meta ORDER BY key"))] == ["searx:keep"]
