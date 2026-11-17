"""Authentication, sessions, CSRF, throttling, password reset and the invitation flow."""
import asyncio
from datetime import timedelta

from sqlalchemy import select, update

from app.portal.models import AuditLog, Invitation, PasswordReset, Session, User
from app.portal.security import utcnow
from tests.portal.conftest import PASSWORD, token_of


async def test_each_role_signs_in_and_gets_its_own_home(world):
    for email, role, home in (("admin@example.com", "ADMIN", "/admin/dashboard"), ("tl.alpha@example.com", "TEAM_LEAD", "/tl/dashboard"),
                              ("member.a1@example.com", "MEMBER", "/member/dashboard")):
        client = await world.client()
        r = await client.post("/api/auth/login", json={"email": email.upper(), "password": PASSWORD})
        assert r.status_code == 200 and r.json()["role"] == role and r.json()["home"] == home
        cookie = r.headers.get_list("set-cookie")
        session_cookie = next(c for c in cookie if c.startswith("jf_session="))
        assert "HttpOnly" in session_cookie and "SameSite=lax" in session_cookie and PASSWORD not in r.text
        assert (await client.get("/api/auth/me")).json()["email"] == email


async def test_wrong_password_unknown_user_and_inactive_user_get_the_same_answer(world):
    client = await world.client()
    wrong = await client.post("/api/auth/login", json={"email": "admin@example.com", "password": "nope-nope-nope"})
    unknown = await client.post("/api/auth/login", json={"email": "ghost@example.com", "password": PASSWORD})
    async with world.sessions() as db:
        await db.execute(update(User).where(User.email == "member.b2@example.com").values(is_active=False))
        await db.commit()
    inactive = await client.post("/api/auth/login", json={"email": "member.b2@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == inactive.status_code == 401
    assert wrong.json() == unknown.json() == inactive.json()
    assert (await client.get("/api/auth/me")).status_code == 401
    async with world.sessions() as db:
        actions = [a for (a,) in (await db.execute(select(AuditLog.action))).all()]
    assert actions.count("auth.login_failed") == 3


async def test_login_is_throttled_after_repeated_failures(world):
    client = await world.client()
    for _ in range(world.settings.login_max_failures):
        assert (await client.post("/api/auth/login", json={"email": "admin@example.com", "password": "bad"})).status_code == 401
    blocked = await client.post("/api/auth/login", json={"email": "admin@example.com", "password": PASSWORD})
    assert blocked.status_code == 429                                  # even the right password waits out the window


async def test_logout_expiry_and_revocation_invalidate_the_session(world):
    client = await world.as_admin()
    assert (await client.post("/api/auth/logout")).status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 401       # the cookie is useless after logout
    second = await world.as_admin()
    async with world.sessions() as db:
        await db.execute(update(Session).where(Session.revoked_at.is_(None)).values(expires_at=utcnow() - timedelta(seconds=1)))
        await db.commit()
    assert (await second.get("/api/auth/me")).status_code == 401       # expired server-side, whatever the cookie says


async def test_state_changing_requests_need_the_csrf_token_and_same_origin(world):
    client = await world.as_admin()
    token = client.headers.pop("x-csrf-token")
    assert (await client.post("/api/teams", json={"name": "No Token"})).status_code == 403
    client.headers["x-csrf-token"] = "forged-" + token
    assert (await client.post("/api/teams", json={"name": "Forged"})).status_code == 403
    client.headers["x-csrf-token"] = token
    assert (await client.post("/api/teams", json={"name": "Evil"}, headers={"origin": "https://evil.example"})).status_code == 403
    assert (await client.post("/api/teams", json={"name": "Fine"}, headers={"origin": "http://testserver"})).status_code == 201
    anonymous = await world.client()
    cross = await anonymous.post("/api/auth/login", json={"email": "admin@example.com", "password": PASSWORD}, headers={"origin": "https://evil.example"})
    assert cross.status_code == 403


async def test_password_reset_is_single_use_expires_and_reveals_nothing(world):
    anonymous = await world.client()
    # No mail server: the answer says so (it never claims a mail was sent), is the same for every address, and no link is made.
    known = await anonymous.post("/api/auth/forgot-password", json={"email": "member.a1@example.com"})
    unknown = await anonymous.post("/api/auth/forgot-password", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 200 and known.json() == unknown.json()
    assert "cannot send e-mail" in known.json()["message"] and "sent to" not in known.json()["message"]
    async with world.sessions() as db:
        assert (await db.execute(select(PasswordReset))).scalars().all() == []

    class Mailer:                                   # stands in for a configured mail server
        backend, sent = "smtp", []

        async def send(self, to, subject, body):
            self.sent.append(to)
            return True

    real_mailer, world.app.state.emailer = world.app.state.emailer, Mailer()
    known = await anonymous.post("/api/auth/forgot-password", json={"email": "member.a2@example.com"})
    unknown = await anonymous.post("/api/auth/forgot-password", json={"email": "nobody.else@example.com"})
    assert known.status_code == unknown.status_code == 200 and known.json() == unknown.json() and "is being e-mailed" in known.json()["message"]
    await asyncio.sleep(0.05)                       # delivery happens after the answer
    assert Mailer.sent == ["member.a2@example.com"]
    async with world.sessions() as db:
        assert len((await db.execute(select(PasswordReset))).scalars().all()) == 1      # only the real account got a token
    world.app.state.emailer = real_mailer

    tl = await world.as_tl("alpha")
    link = await tl.post(f"/api/users/{world.ids['member_a1']}/reset-link")
    assert link.status_code == 200 and link.json()["email_sent"] is False            # no SMTP: reported truthfully
    other_team = await tl.post(f"/api/users/{world.ids['member_b1']}/reset-link")
    assert other_team.status_code == 403
    token = token_of(link.json()["reset_url"])
    member = await world.as_member("a1")
    weak = await anonymous.post("/api/auth/reset-password", json={"token": token, "password": "short"})
    assert weak.status_code == 422
    done = await anonymous.post("/api/auth/reset-password", json={"token": token, "password": "Brand-New-Secret-7"})
    assert done.status_code == 200
    assert (await member.get("/api/auth/me")).status_code == 401                      # existing sessions were revoked
    assert (await anonymous.post("/api/auth/reset-password", json={"token": token, "password": "Another-Secret-8!"})).status_code == 400
    assert (await anonymous.post("/api/auth/login", json={"email": "member.a1@example.com", "password": PASSWORD})).status_code == 401
    assert (await anonymous.post("/api/auth/login", json={"email": "member.a1@example.com", "password": "Brand-New-Secret-7"})).status_code == 200


async def test_admin_creates_team_lead_with_team_and_tl_invites_member(world):
    admin = await world.as_admin()
    created = await admin.post("/api/invitations", json={"name": "Gina Gamma", "email": "tl.gamma@example.com", "role": "TEAM_LEAD",
                                                         "team_name": "Team Gamma", "team_description": "third team"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["email_sent"] is False and body["delivery"] == "manual" and body["status"] == "pending" and body["team"]["name"] == "Team Gamma"
    token = token_of(body["invite_url"])
    async with world.sessions() as db:
        stored = (await db.execute(select(Invitation).where(Invitation.email == "tl.gamma@example.com"))).scalar_one()
        assert token not in (stored.token_hash,) and len(stored.token_hash) == 64     # only a hash is stored

    anonymous = await world.client()
    preview = await anonymous.get("/api/invitations/validate", params={"token": token})
    assert preview.status_code == 200 and preview.json()["team_name"] == "Team Gamma" and preview.json()["role"] == "TEAM_LEAD"
    assert (await anonymous.post("/api/auth/login", json={"email": "tl.gamma@example.com", "password": PASSWORD})).status_code == 401
    assert (await anonymous.post("/api/invitations/accept", json={"token": token, "password": "weak"})).status_code == 422
    assert (await anonymous.post("/api/invitations/accept", json={"token": token, "password": PASSWORD})).status_code == 200
    assert (await anonymous.post("/api/invitations/accept", json={"token": token, "password": PASSWORD})).status_code == 400   # single use
    assert (await anonymous.get("/api/invitations/validate", params={"token": token})).status_code == 404

    gamma = await world.client("tl.gamma@example.com")
    me = (await gamma.get("/api/auth/me")).json()
    assert me["role"] == "TEAM_LEAD" and me["team"]["name"] == "Team Gamma"
    invited = await gamma.post("/api/invitations", json={"name": "Greg Member", "email": "greg@example.com"})
    assert invited.status_code == 201 and invited.json()["team"]["name"] == "Team Gamma" and invited.json()["role"] == "MEMBER"
    member_token = token_of(invited.json()["invite_url"])
    pending = (await gamma.get("/api/users", params={"status": "invited"})).json()
    assert [u["email"] for u in pending["items"]] == ["greg@example.com"]
    assert (await anonymous.post("/api/invitations/accept", json={"token": member_token, "password": PASSWORD})).status_code == 200
    greg = await world.client("greg@example.com")
    me = (await greg.get("/api/auth/me")).json()
    assert me["role"] == "MEMBER" and me["team"]["name"] == "Team Gamma" and me["home"] == "/member/dashboard"
    teams = {t["name"]: t for t in (await admin.get("/api/teams")).json()}
    assert teams["Team Gamma"]["member_count"] == 1 and teams["Team Gamma"]["team_lead"]["email"] == "tl.gamma@example.com"


async def test_invitation_rules(world):
    tl = await world.as_tl("alpha")
    admin = await world.as_admin()
    # A Team Lead cannot invite into another team, cannot create leads, and cannot learn who exists elsewhere.
    assert (await tl.post("/api/invitations", json={"name": "X Y", "email": "x@example.com", "team_id": str(world.ids["team_beta"])})).status_code == 403
    assert (await tl.post("/api/invitations", json={"name": "X Y", "email": "x@example.com", "role": "TEAM_LEAD"})).status_code == 403
    taken = await tl.post("/api/invitations", json={"name": "Dup", "email": "member.b1@example.com"})
    assert taken.status_code == 409 and "Beta" not in taken.text
    member = await world.as_member("a1")
    assert (await member.post("/api/invitations", json={"name": "X Y", "email": "x@example.com"})).status_code == 403
    assert (await admin.post("/api/invitations", json={"name": "No Team", "email": "noteam@example.com", "role": "MEMBER"})).status_code == 422
    assert (await admin.post("/api/invitations", json={"name": "Second Lead", "email": "lead2@example.com", "role": "TEAM_LEAD",
                                                       "team_id": str(world.ids["team_alpha"])})).status_code == 409

    first = await tl.post("/api/invitations", json={"name": "New Person", "email": "new@example.com"})
    old_token = token_of(first.json()["invite_url"])
    again = await tl.post(f"/api/invitations/{first.json()['id']}/resend")
    assert again.status_code == 200
    anonymous = await world.client()
    assert (await anonymous.get("/api/invitations/validate", params={"token": old_token})).status_code == 404     # replaced link is dead
    new_token = token_of(again.json()["invite_url"])
    async with world.sessions() as db:                                 # expire it
        await db.execute(update(Invitation).where(Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None))
                         .values(expires_at=utcnow() - timedelta(minutes=1)))
        await db.commit()
    expired = await anonymous.post("/api/invitations/accept", json={"token": new_token, "password": PASSWORD})
    assert expired.status_code == 400
    listing = (await tl.get("/api/invitations")).json()["items"]
    assert {i["status"] for i in listing} == {"expired", "revoked"} and all(i["team"]["name"] == "Team Alpha" for i in listing)
    assert (await (await world.as_tl("beta")).post(f"/api/invitations/{first.json()['id']}/resend")).status_code == 403
    # A tampered or unknown token gives the same generic answer.
    assert (await anonymous.post("/api/invitations/accept", json={"token": "A" * 43, "password": PASSWORD})).status_code == 400
    assert (await anonymous.post("/api/auth/login", json={"email": "new@example.com", "password": PASSWORD})).status_code == 401
