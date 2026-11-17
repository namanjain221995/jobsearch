"""Rules added after the security review that its own tests do not pin down: which X-Forwarded-For entry is
believed, the per-account sign-in limit, request size limits, invitation limits and address pinning."""
import pytest
from sqlalchemy import select

from app.netguard import pinned
from app.portal.models import AuditLog
from tests.portal.conftest import PASSWORD, add_job

MIB = 1024 * 1024


async def failed_login_address(world, headers: dict) -> str:
    """Sign in with a wrong password and return the address the attempt was recorded under."""
    client = await world.client()
    email = f"probe{len(world.clients)}@example.com"
    assert (await client.post("/api/auth/login", json={"email": email, "password": "wrong-password"}, headers=headers)).status_code == 401
    async with world.sessions() as db:
        return (await db.execute(select(AuditLog.ip).where(AuditLog.action == "auth.login_failed", AuditLog.meta["email"].astext == email))).scalar_one()


async def test_without_a_trusted_proxy_the_forwarded_header_is_ignored(world):
    """The default: the web app passes a caller's own X-Forwarded-For through, so it proves nothing."""
    assert world.settings.trust_forwarded_for is False
    assert await failed_login_address(world, {"x-forwarded-for": "203.0.113.7"}) == "127.0.0.1"                     # the web app's address
    client = await world.client()
    limit = world.settings.login_max_failures
    codes = [(await client.post("/api/auth/login", json={"email": "member.b1@example.com", "password": f"guess-{n}"},
                                headers={"x-forwarded-for": f"203.0.113.{n + 1}"})).status_code for n in range(limit + 1)]
    assert codes == [401] * limit + [429]                           # the account limit holds whatever the header says
    # Everybody comes from the same address here, so nobody else is locked out by those failures.
    assert (await client.post("/api/auth/login", json={"email": "member.b2@example.com", "password": PASSWORD})).status_code == 200


async def test_caller_address_is_the_entry_the_trusted_proxy_appended(world):
    world.settings.trust_forwarded_for = True
    assert await failed_login_address(world, {"x-forwarded-for": "198.51.100.1, 203.0.113.7"}) == "203.0.113.7"     # left part: the caller's claim
    assert await failed_login_address(world, {"x-forwarded-for": "203.0.113.8"}) == "203.0.113.8"
    assert await failed_login_address(world, {"x-forwarded-for": "198.51.100.1, ::ffff:203.0.113.9"}) == "203.0.113.9"
    assert await failed_login_address(world, {"x-forwarded-for": "not-an-address"}) == "127.0.0.1"                   # the socket peer
    assert await failed_login_address(world, {}) == "127.0.0.1"
    world.settings.trusted_proxy_hops = 2                           # e.g. a load balancer in front of the web app
    assert await failed_login_address(world, {"x-forwarded-for": "9.9.9.9, 198.51.100.4, 203.0.113.7"}) == "198.51.100.4"
    assert await failed_login_address(world, {"x-forwarded-for": "198.51.100.4"}) == "127.0.0.1"                    # fewer entries than proxies
    world.settings.trusted_proxy_hops, world.settings.trust_forwarded_for = 1, False
    assert await failed_login_address(world, {"x-forwarded-for": "203.0.113.7"}) == "127.0.0.1"                     # API reached directly


async def test_an_account_is_locked_after_guesses_spread_over_many_addresses(world):
    world.settings.trust_forwarded_for = True
    client = await world.client()
    limit = world.settings.login_account_max_failures
    codes = [(await client.post("/api/auth/login", json={"email": "member.a1@example.com", "password": f"guess-{n}"},
                                headers={"x-forwarded-for": f"203.0.113.{n + 1}"})).status_code for n in range(limit + 1)]
    assert codes == [401] * limit + [429]                           # no single address ever reached its own limit of 5
    locked = await client.post("/api/auth/login", json={"email": "member.a1@example.com", "password": PASSWORD}, headers={"x-forwarded-for": "198.51.100.77"})
    assert locked.status_code == 429                                # the price: the real owner waits too
    other = await client.post("/api/auth/login", json={"email": "member.a2@example.com", "password": PASSWORD}, headers={"x-forwarded-for": "198.51.100.77"})
    assert other.status_code == 200                                 # other accounts are not affected


async def test_requests_are_refused_before_their_body_is_read(world):
    anonymous, admin = await world.client(), await world.as_admin()
    sent = {"bytes": 0}

    async def body():
        for _ in range(8):
            sent["bytes"] += MIB
            yield b"x" * MIB

    refused = await anonymous.post("/api/assignments", content=body(), headers={"content-type": "application/json"})
    assert refused.status_code == 401 and refused.json()["error"] == "Not authenticated" and sent["bytes"] == 0
    too_long = await admin.post("/api/jobs", json={"company": "Acme", "title": "Engineer", "description": "x" * (600 * 1024)})
    assert too_long.status_code == 413 and "too large" in too_long.json()["error"]
    streamed = await admin.post("/api/jobs", content=body(), headers={"content-type": "application/json"})
    assert streamed.status_code == 413 and sent["bytes"] == MIB     # stopped at the first chunk over the limit
    member = await world.as_member("a1")
    big = await member.post("/api/resumes/upload", files={"file": ("cv.txt", b"Python engineer. " * (700 * 1024), "text/plain")})
    assert big.status_code == 413
    ok = await member.post("/api/resumes/upload", files={"file": ("cv.txt", b"Python engineer with PyTorch and SQL experience. " * 30, "text/plain")})
    assert ok.status_code == 201
    assert (await admin.get("/api/users", params={"q": "a\x00b"})).status_code == 422
    assert (await admin.post("/api/teams", json={"name": "Nul\x00Team"})).status_code == 422


async def test_real_pdf_and_docx_resumes_are_read_in_the_limited_process(world):
    import io

    import docx
    import pymupdf
    pdf = pymupdf.open()
    page = pdf.new_page()
    for line in range(12):
        page.insert_text((72, 72 + line * 18), f"Line {line}: Python engineer with PyTorch, AWS and SQL experience", fontsize=10)
    document = docx.Document()
    for line in range(12):
        document.add_paragraph(f"Line {line}: Built ML systems with Python, PyTorch and Kubernetes")
    buffer = io.BytesIO()
    document.save(buffer)
    member = await world.as_member("a1")
    for name, data in (("cv.pdf", pdf.tobytes()), ("cv.docx", buffer.getvalue())):
        response = await member.post("/api/resumes/upload", files={"file": (name, data)})
        assert response.status_code == 201, response.text
        assert "Python" in response.json()["parsed_profile"]["skills"] and response.json()["truncated"] is False
    wrong = await member.post("/api/resumes/upload", files={"file": ("cv.pdf", b"this is not a pdf at all, just text " * 4)})
    assert wrong.status_code == 400 and "not a valid PDF" in wrong.json()["error"]


async def test_invitations_to_one_address_are_limited(world):
    admin = await world.as_admin()
    team = str(world.ids["team_alpha"])
    first = await admin.post("/api/invitations", json={"name": "New Person", "email": "new.person@example.com", "team_id": team})
    assert first.status_code == 201
    limit = world.settings.invitations_per_address_per_hour
    codes = [(await admin.post(f"/api/invitations/{first.json()['id']}/resend")).status_code for _ in range(limit)]
    assert codes[:limit - 1] == [200] * (limit - 1) and codes[-1] == 429
    assert (await admin.post("/api/invitations", json={"name": "Someone Else", "email": "someone.else@example.com", "team_id": team})).status_code == 201


async def test_search_queue_has_a_limit_per_person(world, monkeypatch):
    async def accepted(run_id):
        return True
    monkeypatch.setattr(world.queue, "enqueue", accepted)           # keep them queued
    lead, admin = await world.as_tl("alpha"), await world.as_admin()
    body = {"member_ids": [str(world.ids["member_a1"])], "target_count": 5, "deep_search": False}
    limit = world.settings.max_active_searches_per_user
    codes = [(await lead.post("/api/searches", json=body)).status_code for _ in range(limit + 1)]
    assert codes == [202] * limit + [429]
    assert (await admin.post("/api/searches", json={**body, "member_ids": [str(world.ids["member_b1"])]})).status_code == 202     # another person's allowance
    first = (await lead.get("/api/searches", params={"status": "active"})).json()["items"][0]["id"]
    assert (await lead.post(f"/api/searches/{first}/cancel")).status_code == 200
    assert (await lead.post("/api/searches", json=body)).status_code == 202                                                       # room again


@pytest.mark.parametrize("url,address,expected", [
    ("https://jobs.example.com/a/b?x=1", "93.184.216.34",
     ("https://93.184.216.34/a/b?x=1", {"Host": "jobs.example.com", "Connection": "close"}, {"sni_hostname": "jobs.example.com"})),
    ("http://jobs.example.com/a", "2606:4700::1111", ("http://[2606:4700::1111]/a", {"Host": "jobs.example.com", "Connection": "close"}, {})),
    ("https://jobs.example.com:443/", "93.184.216.34", ("https://93.184.216.34/", {"Host": "jobs.example.com", "Connection": "close"}, {"sni_hostname": "jobs.example.com"})),
])
def test_a_fetch_is_addressed_to_the_checked_address_with_the_original_host(url, address, expected):
    assert pinned(url, address) == expected


async def test_member_copy_of_a_posting_has_no_web_addresses_but_managers_keep_them(world):
    text = "Apply at https://careers.northwind.example/apply/9 or www.northwind.example/jobs today. Stack: Next.js, ASP.NET."
    job = await add_job(world, description=text)
    admin, member = await world.as_admin(), await world.as_member("a1")
    created = await admin.post("/api/assignments", json={"job_id": job, "member_id": str(world.ids["member_a1"])})
    assignment = created.json()["assignment_ids"][0]
    seen = (await member.get(f"/api/assignments/{assignment}")).json()["job"]["description"]
    assert "northwind.example" not in seen and seen.count("[link removed: use Open Job]") == 2 and "Next.js, ASP.NET." in seen
    assert (await admin.get(f"/api/assignments/{assignment}")).json()["job"]["description"] == text
