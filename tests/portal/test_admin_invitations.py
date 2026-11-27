"""Inviting a further Admin. This is the one path that hands out every right in the portal, so each rule has its test:
who may create, resend and revoke such an invitation, that the role comes from the stored invitation and never from
the request, that a dead link gives no account, who can see Admin invitations and Admin accounts, and that the portal
cannot be left without an Admin."""
import asyncio
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text, update

from app.portal.models import AuditLog, Invitation, User
from app.portal.security import utcnow
from app.portal.services import people
from app.portal.services import settings as settings_service
from app.portal.services.emailer import invitation_mail
from tests.portal.conftest import PASSWORD, token_of

NEW_ADMIN = {"name": "Nora Second", "email": "nora.second@example.com", "role": "ADMIN"}


async def users_with(world, email: str) -> list[User]:
    async with world.sessions() as db:
        return list((await db.execute(select(User).where(User.email == email))).scalars())


async def invite_admin(world, admin=None, **fields) -> dict:
    admin = admin or await world.as_admin()
    created = await admin.post("/api/invitations", json={**NEW_ADMIN, **fields})
    assert created.status_code == 201, created.text
    return created.json()


async def test_admin_invites_an_admin_who_accepts_and_gets_the_admin_home(world):
    admin = await world.as_admin()
    body = await invite_admin(world, admin)
    assert (body["role"], body["team"], body["status"], body["invited_by"]["name"]) == ("ADMIN", None, "pending", "Ada Admin")
    token = token_of(body["invite_url"])
    anonymous = await world.client()
    preview = await anonymous.get("/api/invitations/validate", params={"token": token})
    assert preview.status_code == 200 and (preview.json()["role"], preview.json()["team_name"]) == ("ADMIN", None)
    # Before the link is used the account cannot sign in and shows as invited.
    assert (await anonymous.post("/api/auth/login", json={"email": NEW_ADMIN["email"], "password": PASSWORD})).status_code == 401
    listed = (await admin.get("/api/users", params={"role": "ADMIN"})).json()["items"]
    assert {(u["email"], u["status"]) for u in listed} == {("admin@example.com", "active"), (NEW_ADMIN["email"], "invited")}
    assert (await anonymous.post("/api/invitations/accept", json={"token": token, "password": PASSWORD})).status_code == 200
    nora = await world.client(NEW_ADMIN["email"])
    me = (await nora.get("/api/auth/me")).json()
    assert (me["role"], me["team"], me["home"]) == ("ADMIN", None, "/admin/dashboard")
    assert (await nora.get("/api/settings")).status_code == 200                 # an Admin-only screen
    pending = (await admin.get("/api/invitations", params={"role": "ADMIN"})).json()["items"]
    assert [(i["email"], i["status"]) for i in pending] == [(NEW_ADMIN["email"], "accepted")]
    async with world.sessions() as db:
        logged = (await db.execute(select(AuditLog.action, AuditLog.meta).where(AuditLog.action.like("invitation.%"))
                                   .order_by(AuditLog.created_at))).all()
    assert [(action, details.get("role")) for action, details in logged] == [("invitation.created", "ADMIN"), ("invitation.accepted", "ADMIN")]


async def test_only_an_admin_creates_resends_or_revokes_an_admin_invitation(world):
    lead, member, admin = await world.as_tl("alpha"), await world.as_member("a1"), await world.as_admin()
    for client in (lead, member):
        # More often than the hourly allowance of one address: a refusal must not use the allowance up, or a Team
        # Lead could keep an Admin from inviting this person.
        for _ in range(world.settings.invitations_per_address_per_hour + 1):
            refused = await client.post("/api/invitations", json=NEW_ADMIN)
            assert refused.status_code == 403, refused.text
        assert (await client.post("/api/users", json=NEW_ADMIN)).status_code == 403          # the other door to the same code
    async with world.sessions() as db:                      # nothing was created for the refused requests
        assert (await db.execute(select(func.count()).select_from(Invitation))).scalar_one() == 0
        assert (await db.execute(select(func.count()).select_from(AuditLog).where(AuditLog.action == "invitation.created"))).scalar_one() == 0
    assert await users_with(world, NEW_ADMIN["email"]) == []
    # An Admin belongs to no team: a team in the request is refused, not silently dropped.
    for extra in ({"team_id": str(world.ids["team_alpha"])}, {"team_name": "Team Omega"}):
        assert (await admin.post("/api/invitations", json={**NEW_ADMIN, **extra})).status_code == 422
    assert await users_with(world, NEW_ADMIN["email"]) == []

    body = await invite_admin(world, admin)
    token = token_of(body["invite_url"])
    for client in (lead, member, await world.as_tl("beta")):
        assert (await client.post(f"/api/invitations/{body['id']}/resend")).status_code == 403
        assert (await client.post(f"/api/invitations/{body['id']}/revoke")).status_code == 403
    anonymous = await world.client()
    assert (await anonymous.get("/api/invitations/validate", params={"token": token})).status_code == 200      # untouched
    # The Admin can do both, and each is recorded with the role.
    again = await admin.post(f"/api/invitations/{body['id']}/resend")
    assert again.status_code == 200 and again.json()["role"] == "ADMIN"
    assert (await anonymous.get("/api/invitations/validate", params={"token": token})).status_code == 404      # replaced
    revoked = await admin.post(f"/api/invitations/{again.json()['id']}/revoke")
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    async with world.sessions() as db:
        logged = (await db.execute(select(AuditLog.action, AuditLog.meta, AuditLog.actor_id).where(AuditLog.action.like("invitation.%"))
                                   .order_by(AuditLog.created_at))).all()
    assert [(a, d.get("role")) for a, d, _ in logged] == [("invitation.created", "ADMIN"), ("invitation.resent", "ADMIN"), ("invitation.revoked", "ADMIN")]
    assert {actor for _, _, actor in logged} == {world.ids["admin"]}


async def test_the_role_comes_from_the_invitation_and_not_from_the_accept_request(world):
    admin, lead, anonymous = await world.as_admin(), await world.as_tl("alpha"), await world.client()
    # A member's link with a body that asks for more: the account is a member of the lead's team.
    member_invite = (await lead.post("/api/invitations", json={"name": "Mia Member", "email": "mia@example.com"})).json()
    forged = {"token": token_of(member_invite["invite_url"]), "password": PASSWORD, "role": "ADMIN", "invited_role": "ADMIN",
              "team_id": None, "is_admin": True, "user": {"role": "ADMIN"}}
    assert (await anonymous.post("/api/invitations/accept", json=forged)).status_code == 200
    mia = (await (await world.client("mia@example.com")).get("/api/auth/me")).json()
    assert (mia["role"], mia["team"]["name"], mia["home"]) == ("MEMBER", "Team Alpha", "/member/dashboard")
    assert (await (await world.client("mia@example.com")).get("/api/settings")).status_code == 403
    # An Admin's link with a body that asks for less or for a team: still an Admin without a team.
    body = await invite_admin(world, admin)
    lesser = {"token": token_of(body["invite_url"]), "password": PASSWORD, "role": "MEMBER", "team_id": str(world.ids["team_beta"])}
    assert (await anonymous.post("/api/invitations/accept", json=lesser)).status_code == 200
    nora = (await (await world.client(NEW_ADMIN["email"])).get("/api/auth/me")).json()
    assert (nora["role"], nora["team"]) == ("ADMIN", None)
    # A row whose role no longer matches its account (only possible by tampering with the database) gives nothing.
    other = (await lead.post("/api/invitations", json={"name": "Otto Other", "email": "otto@example.com"})).json()
    async with world.sessions() as db:
        await db.execute(update(User).where(User.email == "otto@example.com").values(role="ADMIN", team_id=None))
        await db.commit()
    assert (await anonymous.post("/api/invitations/accept", json={"token": token_of(other["invite_url"]), "password": PASSWORD})).status_code == 400
    assert (await users_with(world, "otto@example.com"))[0].password_hash is None


async def test_an_expired_used_or_revoked_admin_link_gives_no_account(world):
    admin, anonymous = await world.as_admin(), await world.client()

    async def dead(token: str) -> None:
        assert (await anonymous.get("/api/invitations/validate", params={"token": token})).status_code == 404
        refused = await anonymous.post("/api/invitations/accept", json={"token": token, "password": "Another-Secret-8!"})
        assert refused.status_code == 400 and refused.json()["error"] == "This invitation link is invalid or has expired."

    # Revoked.
    first = await invite_admin(world, admin)
    assert (await admin.post(f"/api/invitations/{first['id']}/revoke")).status_code == 200
    await dead(token_of(first["invite_url"]))
    # Replaced by a newer link, which then expires.
    second = (await admin.post(f"/api/invitations/{first['id']}/resend")).json()
    third = (await admin.post(f"/api/invitations/{second['id']}/resend")).json()
    await dead(token_of(second["invite_url"]))
    async with world.sessions() as db:
        await db.execute(update(Invitation).where(Invitation.id == third["id"]).values(expires_at=utcnow() - timedelta(minutes=1)))
        await db.commit()
    await dead(token_of(third["invite_url"]))
    account = (await users_with(world, NEW_ADMIN["email"]))[0]
    assert account.password_hash is None and account.role == "ADMIN"
    assert (await anonymous.post("/api/auth/login", json={"email": NEW_ADMIN["email"], "password": "Another-Secret-8!"})).status_code == 401
    # Used: works once.
    fourth = (await admin.post(f"/api/invitations/{third['id']}/resend")).json()
    token = token_of(fourth["invite_url"])
    assert (await anonymous.post("/api/invitations/accept", json={"token": token, "password": PASSWORD})).status_code == 200
    await dead(token)
    assert (await anonymous.post("/api/auth/login", json={"email": NEW_ADMIN["email"], "password": "Another-Secret-8!"})).status_code == 401
    assert (await anonymous.post("/api/auth/login", json={"email": NEW_ADMIN["email"], "password": PASSWORD})).status_code == 200
    # An unknown token of the right shape gets the same answer.
    await dead("A" * 43)


async def test_an_admin_link_dies_with_the_admin_who_issued_it_and_with_the_invited_account(world):
    admin, anonymous = await world.as_admin(), await world.client()
    await invite_admin(world, admin)
    nora_invite = (await admin.get("/api/invitations", params={"role": "ADMIN"})).json()["items"][0]
    fresh = (await admin.post(f"/api/invitations/{nora_invite['id']}/resend")).json()
    assert (await anonymous.post("/api/invitations/accept", json={"token": token_of(fresh["invite_url"]), "password": PASSWORD})).status_code == 200
    nora = await world.client(NEW_ADMIN["email"])
    third = (await nora.post("/api/invitations", json={"name": "Theo Third", "email": "theo.third@example.com", "role": "ADMIN"})).json()
    token = token_of(third["invite_url"])
    assert (await anonymous.get("/api/invitations/validate", params={"token": token})).status_code == 200
    # The first Admin deactivates the second: the link the second one issued stops working.
    nora_id = (await users_with(world, NEW_ADMIN["email"]))[0].id
    assert (await admin.patch(f"/api/users/{nora_id}", json={"is_active": False})).status_code == 200
    assert (await anonymous.post("/api/invitations/accept", json={"token": token, "password": PASSWORD})).status_code == 400
    # A link whose issuer is unknown is not honoured for this role either.
    mine = (await admin.post(f"/api/invitations/{third['id']}/resend")).json()
    async with world.sessions() as db:
        await db.execute(update(Invitation).where(Invitation.id == mine["id"]).values(invited_by=None))
        await db.commit()
    assert (await anonymous.post("/api/invitations/accept", json={"token": token_of(mine["invite_url"]), "password": PASSWORD})).status_code == 400
    # Deactivating the invited account kills its link as well.
    last = (await admin.post(f"/api/invitations/{mine['id']}/resend")).json()
    theo_id = (await users_with(world, "theo.third@example.com"))[0].id
    assert (await admin.patch(f"/api/users/{theo_id}", json={"is_active": False})).status_code == 200
    assert (await anonymous.post("/api/invitations/accept", json={"token": token_of(last["invite_url"]), "password": PASSWORD})).status_code == 400
    assert (await users_with(world, "theo.third@example.com"))[0].password_hash is None


async def test_team_leads_and_members_see_no_admin_invitations_and_no_admin_accounts(world):
    admin, lead, member = await world.as_admin(), await world.as_tl("alpha"), await world.as_member("a1")
    body = await invite_admin(world, admin)
    await lead.post("/api/invitations", json={"name": "Mia Member", "email": "mia@example.com"})
    nora_id = str((await users_with(world, NEW_ADMIN["email"]))[0].id)
    admin_ids = {nora_id, str(world.ids["admin"])}
    # Invitations: the lead sees the one of their own member and nothing else, whatever filter is sent.
    for params in ({}, {"role": "ADMIN"}, {"status": "pending"}, {"team_id": str(world.ids["team_beta"])}):
        listing = await lead.get("/api/invitations", params=params)
        assert listing.status_code == 200
        assert all(i["role"] == "MEMBER" and i["team"]["name"] == "Team Alpha" for i in listing.json()["items"]), params
        assert NEW_ADMIN["email"] not in listing.text and body["id"] not in listing.text
    assert (await lead.get("/api/invitations", params={"role": "ADMIN"})).json()["total"] == 0
    assert (await member.get("/api/invitations")).status_code == 403
    assert (await member.get("/api/invitations", params={"role": "ADMIN"})).status_code == 403
    # A Team Lead who leads no team sees nothing at all (and not "the invitations without a team").
    async with world.sessions() as db:
        await db.execute(update(User).where(User.id == world.ids["tl_beta"]).values(team_id=None))
        await db.commit()
    teamless = await world.as_tl("beta")
    assert (await teamless.get("/api/invitations")).json()["items"] == []
    assert (await teamless.post(f"/api/invitations/{body['id']}/revoke")).status_code == 403
    # Accounts.
    for params in ({}, {"role": "ADMIN"}, {"q": "admin"}, {"q": "nora"}, {"status": "invited"}):
        listing = await lead.get("/api/users", params=params)
        assert listing.status_code == 200 and all(u["role"] == "MEMBER" for u in listing.json()["items"]), params
        assert not admin_ids & {u["id"] for u in listing.json()["items"]}
        assert "admin@example.com" not in listing.text and NEW_ADMIN["email"] not in listing.text
    assert (await teamless.get("/api/users", params={"role": "ADMIN"})).json()["items"] == []
    assert (await member.get("/api/users")).status_code == 403
    for client in (lead, member, teamless):
        for admin_id in admin_ids:
            assert (await client.get(f"/api/users/{admin_id}")).status_code == 403
            assert (await client.patch(f"/api/users/{admin_id}", json={"is_active": False})).status_code == 403
            if client is not member:
                assert (await client.post(f"/api/users/{admin_id}/reset-link")).status_code == 403
    assert all(u.is_active for u in await users_with(world, "admin@example.com") + await users_with(world, NEW_ADMIN["email"]))


async def test_the_portal_is_never_left_without_an_admin_who_can_sign_in(world):
    admin = await world.as_admin()
    own = world.ids["admin"]
    # Not oneself (that alone would lock the only Admin out).
    assert (await admin.patch(f"/api/users/{own}", json={"is_active": False})).status_code == 409
    # No request changes a role: an Admin cannot demote themselves or anyone.
    assert (await admin.patch(f"/api/users/{own}", json={"role": "MEMBER", "name": "Ada Admin"})).json()["role"] == "ADMIN"
    # An invited Admin who has not chosen a password does not count as an Admin who can sign in.
    body = await invite_admin(world, admin)
    async with world.sessions() as db:
        with pytest.raises(HTTPException) as refused:
            await people.ensure_another_admin(db, await db.get(User, own))
        assert refused.value.status_code == 409 and "only Admin account" in refused.value.detail
    anonymous = await world.client()
    assert (await anonymous.post("/api/invitations/accept", json={"token": token_of(body["invite_url"]), "password": PASSWORD})).status_code == 200
    nora = await world.client(NEW_ADMIN["email"])
    nora_id = (await users_with(world, NEW_ADMIN["email"]))[0].id
    async with world.sessions() as db:                      # with two Admins either may go
        await people.ensure_another_admin(db, await db.get(User, own))
    # Both deactivate each other at the same moment: one of them stays.
    answers = await asyncio.gather(admin.patch(f"/api/users/{nora_id}", json={"is_active": False}),
                                   nora.patch(f"/api/users/{own}", json={"is_active": False}))
    assert sorted(a.status_code for a in answers)[0] == 200 and sorted(a.status_code for a in answers)[1] in (401, 409)
    async with world.sessions() as db:
        able = (await db.execute(select(func.count()).select_from(User).where(
            User.role == "ADMIN", User.is_active, User.password_hash.is_not(None)))).scalar_one()
    assert able == 1


async def test_the_second_of_two_mutual_deactivations_is_refused_once_the_first_is_committed(world):
    """The same race, step by step: the second request was authenticated before the first one committed."""
    admin = await world.as_admin()
    body = await invite_admin(world, admin)
    anonymous = await world.client()
    assert (await anonymous.post("/api/invitations/accept", json={"token": token_of(body["invite_url"]), "password": PASSWORD})).status_code == 200
    async with world.sessions() as first, world.sessions() as second:
        ada, nora = await first.get(User, world.ids["admin"]), (await first.execute(select(User).where(User.email == NEW_ADMIN["email"]))).scalar_one()
        await people.ensure_another_admin(first, nora)           # Ada deactivates Nora: holds the lock
        nora.is_active = False
        waiting = asyncio.create_task(people.ensure_another_admin(second, await second.get(User, ada.id)))      # Nora deactivates Ada
        await asyncio.sleep(0.2)
        assert not waiting.done()                                 # it waits for the first transaction
        await first.commit()
        with pytest.raises(HTTPException) as refused:
            await waiting
        assert refused.value.status_code == 409
        await second.rollback()


async def test_admin_invitation_mail_and_go_live(world):
    subject, text_body = invitation_mail("Nora Second", None, "ADMIN", "http://testserver/invite/accept?token=x", 72)
    assert "as an Administrator" in text_body and "see and change everything" in text_body and "team member" not in text_body
    assert "to join Team Alpha as a Team Lead." in invitation_mail("L", "Team Alpha", "TEAM_LEAD", "u", 72)[1]
    assert "to join Team Alpha as a team member." in invitation_mail("M", "Team Alpha", "MEMBER", "u", 72)[1]

    # Ending test mode keeps the Admin accounts, so it also keeps the invitation of an Admin who has not accepted yet.
    admin, lead = await world.as_admin(), await world.as_tl("alpha")
    body = await invite_admin(world, admin)
    await lead.post("/api/invitations", json={"name": "Mia Member", "email": "mia@example.com"})
    async with world.sessions() as db:
        await settings_service.set_test_mode(db, world.settings, True)
        await db.commit()
    assert (await admin.post("/api/admin/go-live", json={"confirm": "REAL DATA"})).status_code == 200
    async with world.sessions() as db:
        left = (await db.execute(select(Invitation.email, Invitation.invited_role))).all()
        assert [tuple(row) for row in left] == [(NEW_ADMIN["email"], "ADMIN")]
        assert (await db.execute(text("SELECT count(*) FROM users"))).scalar_one() == 2
    anonymous = await world.client()
    assert (await anonymous.post("/api/invitations/accept", json={"token": token_of(body["invite_url"]), "password": PASSWORD})).status_code == 200
