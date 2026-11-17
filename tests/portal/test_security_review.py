"""Security regression tests, written during an adversarial review of the portal API (2026-11-17).

`test_finding_*`  one test per weakness the review found. Each failed when it was written and passes since the
                  weakness was fixed; its docstring describes the weakness as it was (file and line numbers are
                  those of that day) and the secure behaviour the assertions expect.
`test_sound_*`    areas that were attacked and held, and that the other test files do not cover.

Nothing here talks to the outside network: where a fetch is needed it goes to loopback sockets the test opens,
and every host name is answered by the test itself. While the two resume findings are open, their tests cost about
1.5 s / 300 MB (PDF) and 2.7 s / 150 MB (DOCX) inside the test process; that cost is the finding.

Run:  TEST_DATABASE_URL=postgresql+asyncpg://.../<a database of your own> python -m pytest -q tests/portal/test_security_review.py"""
import asyncio
import io
import ipaddress
import re
import socket
import time
import uuid
import zipfile
from datetime import datetime, timezone

import httpx
import pytest
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import func, select, update

from app import netguard
from app.config import Settings
from app.portal import security
from app.portal.app import create_app
from app.portal.models import (ApplicationEvent, AuditLog, Candidate, CandidateProfile, JobAssignment, JobPosting, Notification, Resume,
                               SearchResult, SearchRun, SearchTarget, TeamJobQueue, User)
from app.portal.services import jobs as jobs_service
from app.schemas import Job as EngineJob
from tests.portal.conftest import PASSWORD, add_job, token_of

MIB = 1024 * 1024
REFUSED = (400, 403, 404, 409, 413, 422)


# ---------------------------------------------------------------- helpers
async def quiet_client(world, email: str) -> httpx.AsyncClient:
    """Like world.client(email), but an unhandled server error comes back as the 500 response a browser would get."""
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app, raise_app_exceptions=False), base_url="http://testserver")
    world.clients.append(client)
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers["x-csrf-token"] = client.cookies["jf_csrf"]
    return client


async def assign(admin, job: str, candidate_id) -> str:
    response = await admin.post("/api/assignments", json={"job_id": job, "candidate_id": str(candidate_id)})
    assert response.status_code == 201, response.text
    return response.json()["assignment_ids"][0]


async def seed_run(world, *, initiated_by, team_id, results, status: str = "completed") -> str:
    """A review-mode search run with stored results, exactly the rows the background runner writes."""
    async with world.sessions() as db:
        run = SearchRun(initiated_by=initiated_by, team_id=team_id, mode="review", search_filters={}, target_count=5,
                        deep_search=False, min_match_score=50, status=status, progress={})
        db.add(run)
        await db.flush()
        for candidate_id in {c for c, _ in results}:
            db.add(SearchTarget(search_run_id=run.id, candidate_id=candidate_id, candidate_specific_filters={}, assignment_mode="review",
                                status="completed", stats={}))
        for rank, (candidate_id, job_id) in enumerate(results, start=1):
            db.add(SearchResult(search_run_id=run.id, candidate_id=candidate_id, job_id=uuid.UUID(str(job_id)), rank=rank, match_score=80,
                                match_explanation={"reason": "Strong Python match", "matching_skills": ["Python"],
                                                   "authorization_status": "Eligible", "eligibility_note": "Green card holder: eligible"}))
        await db.commit()
        return str(run.id)


def allow_job_links(monkeypatch) -> None:
    """The fixture job links point at careers.northwind.example; skip the DNS lookup the redirect guard would make."""
    async def public(url):
        return None
    monkeypatch.setattr("app.portal.routers.assignments.assert_public_url", public)


def pdf_with_one_huge_page(text_lines: int) -> bytes:
    """A valid one-page PDF whose (deflated) content stream repeats one text operator `text_lines` times."""
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Python engineer with PyTorch experience", fontsize=8)
    font = page.get_fonts()[0][4]
    stream = b"BT /" + font.encode() + b" 8 Tf 72 700 Td " + b"(Python engineer with PyTorch experience ) Tj 0 -1 Td " * text_lines + b"ET"
    doc.update_stream(page.get_contents()[0], stream)
    return doc.tobytes(deflate=True, garbage=3)


def docx_with_many_paragraphs(paragraphs: int) -> tuple[bytes, int]:
    """(a valid .docx, size of its word/document.xml once inflated)."""
    import docx
    blank = io.BytesIO()
    docx.Document().save(blank)
    source = zipfile.ZipFile(io.BytesIO(blank.getvalue()))
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + "<w:p><w:r><w:t>Python engineer with PyTorch experience</w:t></w:r></w:p>" * paragraphs + "</w:body></w:document>")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for item in source.infolist():
            archive.writestr(item, xml if item.filename == "word/document.xml" else source.read(item.filename))
    return out.getvalue(), len(xml)


class DeliveringMailer:
    """Stands in for a configured SMTP server that accepts every message (optionally slowly)."""
    backend = "smtp"

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.sent: list[tuple[str, str]] = []          # (recipient, body)

    async def send(self, to: str, subject: str, body: str) -> bool:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append((to, body))
        return True


# ================================================================ findings: sign-in, sessions, links
async def test_finding_login_throttle_is_bypassed_by_rotating_x_forwarded_for(world):
    """HIGH - app/portal/deps.py:58 client_ip() takes the LEFT-MOST X-Forwarded-For entry, which is whatever the
    caller sent; routers/auth.py:48 keys both failure counters on that value, and there is no counter per account.
    A new value per request means the throttle never triggers: unlimited password guessing against any account.
    EXPECTED: after LOGIN_MAX_FAILURES failures for one account from one real client the next attempt gets 429,
    whatever the client put in the header."""
    world.settings.trust_forwarded_for = True          # a reverse proxy in front appends the address it saw
    anonymous = await world.client()
    limit = world.settings.login_max_failures
    codes = []
    for attempt in range(limit + 3):
        # What the API receives behind a proxy that appends: "<value sent by the client>, <address the proxy saw>".
        response = await anonymous.post("/api/auth/login", json={"email": "admin@example.com", "password": f"guess-{attempt}"},
                                        headers={"x-forwarded-for": f"198.51.100.{attempt + 1}, 203.0.113.7"})
        codes.append(response.status_code)
    assert 429 in codes, f"{len(codes)} wrong passwords for one account from one client were all answered {set(codes)}: never throttled"


async def test_finding_spoofed_x_forwarded_for_locks_out_another_address_and_forges_the_audit_ip(world):
    """HIGH (same root cause) - naming somebody else's address in X-Forwarded-For spends THEIR failure budget
    (5 per account and address, 30 per address for every account behind it) and writes that address into the
    audit trail (audit_logs.ip).
    EXPECTED: failures are counted against, and logged with, the address the trusted proxy observed."""
    world.settings.trust_forwarded_for = True          # a reverse proxy in front appends the address it saw
    victim_ip, attacker_ip = "203.0.113.50", "198.51.100.9"
    attacker = await world.client()
    for attempt in range(world.settings.login_max_failures):
        refused = await attacker.post("/api/auth/login", json={"email": "admin@example.com", "password": f"guess-{attempt}"},
                                      headers={"x-forwarded-for": f"{victim_ip}, {attacker_ip}"})
        assert refused.status_code == 401
    genuine = await (await world.client()).post("/api/auth/login", json={"email": "admin@example.com", "password": PASSWORD},
                                                headers={"x-forwarded-for": victim_ip})
    async with world.sessions() as db:
        logged = set((await db.execute(select(AuditLog.ip).where(AuditLog.action == "auth.login_failed"))).scalars())
    assert genuine.status_code == 200, f"the real user at {victim_ip} is answered {genuine.status_code} after an attacker elsewhere guessed 5 times"
    assert victim_ip not in logged, f"the audit trail blames {logged} for the attacker's attempts"


async def test_finding_password_hashing_blocks_the_event_loop(world, monkeypatch):
    """MEDIUM - routers/auth.py:55 calls security.verify_password() (Argon2id, 64 MiB, about 47 ms on this machine, more
    in a small container) directly inside the async handler, also for unknown e-mails; the same for hash_password() in
    accept / reset / change-password. While it runs, no other request of the process is served, so a few sign-in
    attempts per second (unthrottled, see above) stall the whole API for everybody.
    EXPECTED: the hash runs off the event loop (thread pool), so the loop keeps turning while it is computed."""
    class SlowHasher:
        """Argon2 on a slow host. time.sleep() releases the GIL just like the C implementation does."""
        def verify(self, stored, password):
            time.sleep(0.4)
            raise VerifyMismatchError()

        def hash(self, password):
            time.sleep(0.4)
            return "$argon2id$stand-in"

        def check_needs_rehash(self, stored):
            return False

    monkeypatch.setattr(security, "_hasher", SlowHasher())
    anonymous = await world.client()
    gaps, stop = [], asyncio.Event()

    async def heartbeat():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    response = await anonymous.post("/api/auth/login", json={"email": "nobody@example.com", "password": "whatever-it-is"})
    stop.set()
    await beat
    assert response.status_code == 401
    assert max(gaps) < 0.2, f"one anonymous sign-in attempt froze every other request for {max(gaps):.2f} s (the full hash time)"


async def test_finding_request_body_is_read_in_full_before_authentication(world):
    """MEDIUM - nothing caps the request size, and FastAPI parses the body before it resolves dependencies, so the
    10 MB check in routers/users.py:187 (and the 2 MB one in routers/jobs.py:71) runs only after the whole upload was
    spooled to disk, and only for signed-in callers. An anonymous client can stream any amount to
    POST /api/candidates/{id}/resume (disk; was POST /api/resumes/upload) or to a JSON route such as POST /api/auth/login (memory).
    EXPECTED: reading stops at a fixed cap; an oversized or unauthenticated body is refused without being consumed."""
    anonymous = await world.client()
    megabyte, read = b"A" * MIB, {"upload": 0, "json": 0}

    def counted(kind: str, head: bytes, tail: bytes):
        async def body():
            for part in (head, *[megabyte] * 24, tail):
                read[kind] += len(part)
                yield part
        return body()

    head = b'--b\r\nContent-Disposition: form-data; name="file"; filename="cv.txt"\r\nContent-Type: text/plain\r\n\r\n'
    upload = await anonymous.post(f"/api/candidates/{world.ids['cand_a1']}/resume", content=counted("upload", head, b"\r\n--b--\r\n"),
                                  headers={"content-type": "multipart/form-data; boundary=b"})
    login = await anonymous.post("/api/auth/login", content=counted("json", b'{"email": "admin@example.com", "password": "', b'"}'),
                                 headers={"content-type": "application/json"})
    assert upload.status_code in (401, 413) and login.status_code in (413, 422)
    cap = world.settings.max_upload_bytes + MIB
    assert read["upload"] <= cap and read["json"] <= cap, (
        f"the server read {read['upload'] // MIB} MB of an anonymous upload and {read['json'] // MIB} MB of an anonymous JSON body before answering")


async def test_finding_former_team_lead_takes_over_pending_member_moved_to_another_team(world):
    """MEDIUM - invitations are scoped by Invitation.team_id (routers/invitations.py:20), which transfer_member()
    (services/people.py:142) never updates or revokes. After an Admin moves a not-yet-activated member from Alpha to
    Beta, Alpha's lead still lists, resends and revokes that invitation; resend returns a fresh link, so the lead can
    set the password and sign in as a member of Team Beta. Beta's lead cannot even see the invitation.
    EXPECTED: a transfer ends the old team's control: its lead gets 403/409 and the link they hold stops working."""
    lead_alpha, admin = await world.as_tl("alpha"), await world.as_admin()
    invited = (await lead_alpha.post("/api/invitations", json={"name": "New Person", "email": "new@example.com"})).json()
    user = (await admin.get("/api/users", params={"q": "new@example.com"})).json()["items"][0]
    moved = await admin.patch(f"/api/users/{user['id']}", json={"team_id": str(world.ids["team_beta"])})
    assert moved.status_code == 200 and moved.json()["team"]["name"] == "Team Beta"

    anonymous = await world.client()
    still_listed = [i["email"] for i in (await lead_alpha.get("/api/invitations")).json()["items"]]
    old_link = await anonymous.get("/api/invitations/validate", params={"token": token_of(invited["invite_url"])})
    resent = await lead_alpha.post(f"/api/invitations/{invited['id']}/resend")
    if resent.status_code == 200:
        accepted = await anonymous.post("/api/invitations/accept", json={"token": token_of(resent.json()["invite_url"]),
                                                                         "password": "Chosen-By-Old-Lead-1!"})
        assert accepted.status_code == 200
        hijacked = await world.client("new@example.com", "Chosen-By-Old-Lead-1!")
        team = (await hijacked.get("/api/auth/me")).json()["team"]["name"]
        pytest.fail(f"Team Alpha's lead re-issued the link, set the password and is signed in as a member of {team}")
    assert resent.status_code in (403, 404, 409)
    assert still_listed == [] and old_link.status_code == 404


async def test_finding_reset_link_survives_member_transfer_and_issuer_deactivation(world):
    """MEDIUM - a reset link a manager created (routers/users.py:108, the URL is in the response) stays valid for
    RESET_MINUTES whatever happens next. (1) Alpha's lead creates one for a member, the Admin moves the member to Beta,
    the lead resets the password and owns an account of another team. (2) A lead creates links, is deactivated, and
    still takes the accounts over anonymously. Invitation links issued by a deactivated manager behave the same.
    EXPECTED: links issued by a manager die when the member leaves that manager's scope or the manager is deactivated."""
    lead_alpha, admin, anonymous = await world.as_tl("alpha"), await world.as_admin(), await world.client()
    for_moved = (await lead_alpha.post(f"/api/users/{world.ids['member_a1']}/reset-link")).json()
    for_later = (await lead_alpha.post(f"/api/users/{world.ids['member_a2']}/reset-link")).json()
    assert (await admin.patch(f"/api/users/{world.ids['member_a1']}", json={"team_id": str(world.ids["team_beta"])})).status_code == 200
    after_transfer = await anonymous.post("/api/auth/reset-password", json={"token": token_of(for_moved["reset_url"]),
                                                                           "password": "Old-Lead-Was-Here-9"})
    assert (await admin.patch(f"/api/users/{world.ids['tl_alpha']}", json={"is_active": False})).status_code == 200
    after_deactivation = await anonymous.post("/api/auth/reset-password", json={"token": token_of(for_later["reset_url"]),
                                                                               "password": "Fired-Lead-Was-Here-9"})
    assert (after_transfer.status_code, after_deactivation.status_code) == (400, 400), (
        "a former lead reset the password of a member who is now in another team, and a deactivated lead reset a member's password")


async def test_finding_older_reset_links_stay_valid_after_reset_or_password_change(world):
    """LOW/MEDIUM - services/invitations.py:161 issue_reset() never retires earlier tokens and reset_password() (:172)
    marks only the one it used; routers/auth.py:115 change-password leaves every outstanding token alive. Whoever holds
    an older link (mailbox, browser history, the manager who generated it) can still take the account after its owner
    has just "secured" it.
    EXPECTED: a successful reset or password change invalidates every other outstanding reset token of that user."""
    admin, anonymous = await world.as_admin(), await world.client()
    member_id = world.ids["member_a1"]
    older = (await admin.post(f"/api/users/{member_id}/reset-link")).json()
    newer = (await admin.post(f"/api/users/{member_id}/reset-link")).json()
    assert (await anonymous.post("/api/auth/reset-password", json={"token": token_of(newer["reset_url"]), "password": "Brand-New-Secret-7"})).status_code == 200
    replay = await anonymous.post("/api/auth/reset-password", json={"token": token_of(older["reset_url"]), "password": "Attacker-Chosen-8!"})

    outstanding = (await admin.post(f"/api/users/{world.ids['member_a2']}/reset-link")).json()
    member = await world.as_member("a2")
    assert (await member.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": "My-Own-Secret-55!"})).status_code == 200
    after_change = await anonymous.post("/api/auth/reset-password", json={"token": token_of(outstanding["reset_url"]), "password": "Attacker-Again-9!"})
    assert (replay.status_code, after_change.status_code) == (400, 400), "an older reset link still set a new password"


async def test_finding_links_are_handed_to_the_manager_even_when_emailed(world):
    """MEDIUM - routers/users.py:120 and routers/invitations.py:27 always put the one-time URL in the response, also when
    the mail server accepted the message (email_sent = true). The README says the creator sees the link only "without
    SMTP". With SMTP on, a Team Lead still collects a working reset link for every member (and an Admin for every other
    Admin) and can sign in as them; the audit trail then shows the reset as done by the victim.
    EXPECTED: when the link was delivered by e-mail the response does not contain the token."""
    world.app.state.emailer = DeliveringMailer()
    lead, admin = await world.as_tl("alpha"), await world.as_admin()
    reset = (await lead.post(f"/api/users/{world.ids['member_a1']}/reset-link")).json()
    invite = (await lead.post("/api/invitations", json={"name": "New Person", "email": "new@example.com"})).json()
    again = (await admin.post(f"/api/invitations/{invite['id']}/resend")).json()
    assert reset["email_sent"] is True and invite["email_sent"] is True and again["email_sent"] is True
    leaked = [name for name, url in (("reset_url", reset["reset_url"]), ("invite_url", invite["invite_url"]),
                                     ("invite_url (resend)", again["invite_url"])) if "token=" in url]
    assert leaked == [], f"delivered by e-mail, yet the requester also received a working token in: {leaked}"


async def test_finding_invitation_mail_carries_attacker_text_and_is_not_rate_limited(world):
    """LOW/MEDIUM (needs SMTP) - services/emailer.py:54 opens the invitation with "Hello {name}" and schemas.py:86 lets
    `name` be any 160 characters, line breaks and links included; POST /api/invitations has no limit and re-issues for
    the same pending address every time. A Team Lead, or a stolen lead session, can make the portal's own mail server
    send text of their choice to any address, as often as they like. (Headers are safe: the name is body only.)
    EXPECTED: a name with control characters or a URL is refused (422), and repeated invitations to one address are
    throttled."""
    mailer = world.app.state.emailer = DeliveringMailer()
    lead = await world.as_tl("alpha")
    lure = "customer,\n\nYour mailbox is suspended. Verify it at https://evil.example/login within 24 hours.\n\n--"
    codes = [(await lead.post("/api/invitations", json={"name": lure, "email": "victim@elsewhere.example"})).status_code for _ in range(12)]
    delivered = [body for recipient, body in mailer.sent if recipient == "victim@elsewhere.example"]
    assert not any("evil.example" in body for body in delivered), (
        f"{len(delivered)} portal e-mails went to an outside address, starting: {delivered[0][:90]!r} (answers: {set(codes)})")
    assert len(delivered) <= 3


async def test_finding_forgot_password_answers_slower_for_existing_accounts(world):
    """LOW/MEDIUM - routers/auth.py:99: for a real account the handler awaits the mail delivery
    (services/invitations.py:168) plus three writes before it answers; for an unknown address it answers at once. The
    body is identical, the response time is not: e-mail enumeration (the 10/hour address limit falls to the
    X-Forwarded-For trick above).
    EXPECTED: the answer does not wait for delivery (send in the background), so both cases take the same time."""
    world.app.state.emailer = DeliveringMailer(delay=0.6)             # an SMTP round trip
    anonymous = await world.client()
    started = time.perf_counter()
    assert (await anonymous.post("/api/auth/forgot-password", json={"email": "nobody@example.com"})).status_code == 200
    unknown = time.perf_counter() - started
    started = time.perf_counter()
    assert (await anonymous.post("/api/auth/forgot-password", json={"email": "member.a1@example.com"})).status_code == 200
    known = time.perf_counter() - started
    assert known - unknown < 0.3, f"unknown address answered in {unknown * 1000:.0f} ms, existing account in {known * 1000:.0f} ms"


async def test_finding_api_docs_and_schema_are_public_in_production(world):
    """LOW - app/portal/app.py:67 serves /api/docs and /api/openapi.json to anyone in every environment. Through the
    Next.js proxy they sit on the portal's own origin: the full route map for anonymous visitors, and a Swagger page
    that runs scripts from cdn.jsdelivr.net inside the origin that holds the session.
    EXPECTED: with APP_ENV=production the interactive docs and the schema are off (or need an Admin session)."""
    production = create_app(Settings(_env_file=None, database_url=world.settings.database_url, app_env="production",
                                     public_base_url="https://portal.example"), queue=world.queue, use_redis=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=production), base_url="https://portal.example") as anonymous:
        codes = {path: (await anonymous.get(path)).status_code for path in ("/api/docs", "/api/openapi.json")}
    assert 200 not in codes.values(), f"served to an anonymous caller in production: {codes}"


# ================================================================ findings: uploads and outbound requests
async def test_finding_tiny_pdf_resume_makes_the_parser_work_without_bound(world):
    """HIGH (availability, any MEMBER) - app/resume_parser.py:51 limits pages (60) but not what a page holds, and the
    100 000 character cap is applied after extraction. Measured here: a 79 KB PDF costs 2 s CPU and 460 MB of memory, a
    315 KB one 8 s and 1.6 GB (linear over four sizes), i.e. about 5 GB per megabyte uploaded; the limit is 10 MB. It
    runs inside the API process while the request holds a database connection.
    EXPECTED: content that inflates beyond the upload limit is refused (or parsed in a sandboxed, limited process).
    This sample is about 46 KB on the wire and inflates to about 16 MB; today it is accepted with 201.
    Since the candidate model, resumes are uploaded by a manager to POST /api/candidates/{id}/resume (a MEMBER is
    refused before anything is parsed), so the attacker is a Team Lead; the parser and its limits are the same."""
    lead, member = await world.as_tl("alpha"), await world.as_member("a1")
    url = f"/api/candidates/{world.ids['cand_a1']}/resume"
    data = pdf_with_one_huge_page(300_000)
    assert len(data) < 100_000
    assert (await member.post(url, files={"file": ("cv.pdf", data, "application/pdf")})).status_code == 403
    # The route, the session and the candidate are right: an ordinary file is stored. So a refusal below is about the content.
    assert (await lead.post(url, files={"file": ("cv.txt", b"Python engineer with PyTorch experience. " * 10, "text/plain")})).status_code == 201
    response = await lead.post(url, files={"file": ("cv.pdf", data, "application/pdf")})
    assert response.status_code in REFUSED, f"a {len(data) // 1024} KB PDF holding 16 MB of page content was parsed and stored ({response.status_code})"
    assert (await lead.get(url)).json()["filename"] == "cv.txt"                            # nothing of it was stored


async def test_finding_tiny_docx_resume_inflates_far_beyond_the_upload_limit(world):
    """MEDIUM (availability, any MEMBER) - app/resume_parser.py:11 allows a DOCX to expand to 60 MB of XML, six times
    the 10 MB upload limit; python-docx then builds the whole tree. Measured: 60 KB uploaded -> 7 MB of XML -> 1.4 s CPU
    and 75 MB; extrapolated, the 60 MB ceiling is reached with about 0.5 MB uploaded (roughly 12 s CPU and 600 MB per
    request).
    EXPECTED: a document that inflates beyond the upload limit is refused. This one is about 75 KB -> 12 MB.
    Since the candidate model the upload is a manager's POST /api/candidates/{id}/resume; a MEMBER is refused outright."""
    lead, member = await world.as_tl("alpha"), await world.as_member("a1")
    url = f"/api/candidates/{world.ids['cand_a1']}/resume"
    data, inflated = docx_with_many_paragraphs(170_000)
    assert len(data) < 200_000 and inflated > world.settings.max_upload_bytes
    assert (await member.post(url, files={"file": ("cv.docx", data)})).status_code == 403
    # The route, the session and the candidate are right: an ordinary file is stored. So a refusal below is about the content.
    assert (await lead.post(url, files={"file": ("cv.txt", b"Python engineer with PyTorch experience. " * 10, "text/plain")})).status_code == 201
    response = await lead.post(url, files={"file": ("cv.docx", data)})
    assert response.status_code in REFUSED, f"a {len(data) // 1024} KB DOCX inflating to {inflated // MIB} MB was parsed and stored ({response.status_code})"
    assert (await lead.get(url)).json()["filename"] == "cv.txt"                            # nothing of it was stored


async def test_finding_ssrf_guard_allows_shared_address_space(world):
    """MEDIUM - app/netguard.py:15 _ip_allowed() tests is_private / is_loopback / ..., not is_global, so 100.64.0.0/10
    (RFC 6598: carrier-grade NAT, Tailscale, the Alibaba Cloud metadata service at 100.100.100.200, some Kubernetes pod
    networks) counts as public for page verification, URL import, the headless browser's guard proxy and the Open Job
    redirect.
    EXPECTED: only globally routable addresses pass."""
    passed = []
    for url in ("http://100.100.100.200/latest/meta-data/", "http://100.64.0.1/", "https://[::ffff:100.64.0.1]/"):
        try:
            await netguard.assert_public_url(url)             # IP literals: no DNS query is made
            passed.append(url)
        except netguard.UnsafeURL:
            pass
    job = await add_job(world, canonical_url="http://100.100.100.200/latest/meta-data/", apply_url="")
    admin, member = await world.as_admin(), await world.as_member("a1")
    assignment = await assign(admin, job, world.ids["cand_a1"])
    redirect = await member.get(f"/api/assignments/{assignment}/open-job", follow_redirects=False)
    assert passed == [], f"the SSRF guard lets these through: {passed}"
    assert redirect.status_code == 409, f"Open Job redirects to {redirect.headers.get('location')}"


async def test_finding_dns_rebinding_between_ssrf_check_and_fetch(world, monkeypatch):
    """MEDIUM - app/job_verifier.py:93 checks the host (resolve, compare addresses) and the next line lets httpx resolve
    the same name again for the real connection. A name that answers "public" once and "internal" afterwards walks
    through the guard: POST /api/jobs/import-url and POST /api/jobs/{id}/verify (Admin), and the worker's verification of
    web-search leads and crawled links (third-party content). resolve_public() even returns the vetted address; only
    GuardProxy uses it.
    Note: tests/conftest.py replaces job_verifier.assert_public_url with a no-op for every test, so no existing test
    runs the import or verify routes with the guard on. This test puts it back.
    EXPECTED: the connection goes to the address that was checked. Here 127.0.0.2 plays the public site and 127.0.0.1
    the internal service; the internal one must never be contacted."""
    hits = {"internal": [], "public": []}

    def serve(name: str):
        async def handle(reader, writer):
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                hits[name].append(head.split(b"\r\n")[0].decode())
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
            finally:
                writer.close()
        return handle

    internal = await asyncio.start_server(serve("internal"), "127.0.0.1", 0)
    port = internal.sockets[0].getsockname()[1]
    try:
        public = await asyncio.start_server(serve("public"), "127.0.0.2", port)
    except OSError:
        internal.close()
        pytest.skip("this host cannot bind 127.0.0.2")
    lookups, real_getaddrinfo = [], socket.getaddrinfo

    def rebinding_dns(host, service, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if name == "rebind.attacker.test":
            lookups.append(name)
            address = "127.0.0.2" if len(lookups) == 1 else "127.0.0.1"          # first answer "public", then "internal"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, int(service or 0)))]
        try:
            ipaddress.ip_address(name)
        except ValueError:
            raise socket.gaierror(socket.EAI_NONAME, "no DNS in this test") from None
        return real_getaddrinfo(host, service, *args, **kwargs)

    monkeypatch.setattr("app.job_verifier.assert_public_url", netguard.assert_public_url)      # the real guard
    monkeypatch.setattr(socket, "getaddrinfo", rebinding_dns)
    monkeypatch.setattr(netguard, "ALLOWED_PORTS", (80, 443, port))
    monkeypatch.setattr(netguard, "_ip_allowed", lambda ip: ip == "127.0.0.2")
    client = httpx.AsyncClient(follow_redirects=False, trust_env=False)      # same kind of client app.py builds
    monkeypatch.setattr(world.app.state.verifier, "client", client)
    admin = await world.as_admin()
    try:
        response = await admin.post("/api/jobs/import-url", json={"url": f"http://rebind.attacker.test:{port}/jobs/1"})
    finally:
        await client.aclose()
        internal.close()
        public.close()
    assert response.status_code == 200
    assert hits["internal"] == [], f"the guard approved 127.0.0.2, the fetch went to the internal service instead: {hits['internal']}"


# ================================================================ findings: isolation between teams
async def test_finding_reassigned_team_lead_keeps_access_to_former_teams_searches(world):
    """MEDIUM - routers/searches.py:29 and services/searches.py:107 let a Team Lead see every run they initiated,
    whatever team it belongs to; services/jobs.py:286 does the same for the jobs those runs found. A lead the Admin
    removed from Team Alpha, or moved to Team Beta, keeps Alpha's runs: member names and e-mails, per-member match
    explanations (skills gaps, work-authorization notes), posting URLs, the current assignment state, and the right to
    cancel a running Alpha search.
    EXPECTED: a lead sees only runs of the team they lead now; one without a team sees none (403, empty list)."""
    job = await add_job(world)
    alpha, beta, lead_id = world.ids["team_alpha"], world.ids["team_beta"], world.ids["tl_alpha"]
    finished = await seed_run(world, initiated_by=lead_id, team_id=alpha, results=[(world.ids["cand_a1"], job)])
    running = await seed_run(world, initiated_by=lead_id, team_id=alpha, results=[(world.ids["cand_a1"], job)], status="running")
    admin, lead = await world.as_admin(), await world.as_tl("alpha")
    assert (await lead.get(f"/api/searches/{finished}/results")).status_code == 200            # their team today
    seen = {}

    async def look(stage: str):
        seen[f"{stage}: run"] = (await lead.get(f"/api/searches/{finished}")).status_code
        seen[f"{stage}: results"] = (await lead.get(f"/api/searches/{finished}/results")).status_code
        seen[f"{stage}: listed runs"] = (await lead.get("/api/searches")).json()["total"]

    assert (await admin.patch(f"/api/teams/{alpha}", json={"remove_team_lead": True})).status_code == 200
    await look("no team")
    assert (await admin.patch(f"/api/teams/{beta}", json={"team_lead_id": str(lead_id)})).status_code == 200
    assert (await lead.get("/api/auth/me")).json()["team"]["name"] == "Team Beta"
    await look("leads Beta")
    seen["leads Beta: job found by Alpha's search"] = (await lead.get(f"/api/jobs/{job}")).status_code
    seen["leads Beta: cancel Alpha's running search"] = (await lead.post(f"/api/searches/{running}/cancel")).status_code
    expected = {key: (0 if "listed" in key else 403) for key in seen}
    assert seen == expected, f"a lead who no longer leads Team Alpha still reaches its searches: {seen}"


async def test_finding_search_results_reveal_assignment_state_of_another_team(world):
    """LOW/MEDIUM - services/searches.py:162 results_out() looks up assignments by (job, member) with no scope and takes
    the job counters from list_items(db, postings, []) (:164), also unscoped. The lead of the team a member (today: a
    candidate, whose jobs follow it) left still
    reads, through an old run's results, the id and current status of assignments that now belong to the new team, plus
    organisation-wide assigned / applied counts per job.
    EXPECTED: assignment_id / assignment_status are filled only for assignments the caller may open, and the counters
    cover the caller's team only (as GET /api/jobs already does)."""
    job = await add_job(world)
    candidate_id = world.ids["cand_a1"]
    run = await seed_run(world, initiated_by=world.ids["tl_alpha"], team_id=world.ids["team_alpha"], results=[(candidate_id, job)])
    admin, old_lead = await world.as_admin(), await world.as_tl("alpha")
    # Since the candidate model it is the candidate that changes team (a member who moves leaves the candidates behind).
    moved = await admin.patch(f"/api/candidates/{candidate_id}", json={"team_id": str(world.ids["team_beta"]),
                                                                      "assigned_member_id": str(world.ids["member_b1"])})
    assert moved.status_code == 200 and moved.json()["team"]["name"] == "Team Beta"
    assignment = await assign(admin, job, candidate_id)                                   # now Team Beta's assignment
    assert (await (await world.as_member("b1")).patch(f"/api/assignments/{assignment}/status", json={"status": "APPLIED"})).status_code == 200
    assert (await old_lead.get(f"/api/assignments/{assignment}")).status_code == 403      # the assignment itself is off limits
    row = (await old_lead.get(f"/api/searches/{run}/results")).json()["items"][0]
    exposed = {key: value for key, value in (("assignment_id", row["assignment_id"]), ("assignment_status", row["assignment_status"]),
                                             ("job.assignment_count", row["job"]["assignment_count"]),
                                             ("job.applied_count", row["job"]["applied_count"])) if value}
    assert exposed == {}, f"Team Alpha's lead learns about Team Beta's assignment through the result list: {exposed}"


async def test_finding_team_queue_reveals_other_teams_assignment_counts(world):
    """LOW - routers/teams.py:110 queue_out() builds the job rows with list_items(db, postings, []): no team filter. A
    Team Lead reading (or updating) their own queue sees how many people in the whole organisation hold each job and
    how many applied, although GET /api/jobs and GET /api/jobs/{id} limit the same numbers to the lead's team.
    EXPECTED: (0, 0) here: nobody in Team Alpha has the job."""
    job = await add_job(world)
    admin, lead_alpha = await world.as_admin(), await world.as_tl("alpha")
    assignment = await assign(admin, job, world.ids["cand_b1"])                          # Team Beta only
    assert (await (await world.as_member("b1")).patch(f"/api/assignments/{assignment}/status", json={"status": "APPLIED"})).status_code == 200
    assert (await admin.post(f"/api/teams/{world.ids['team_alpha']}/queue", json={"job_ids": [job]})).status_code == 201
    item = (await lead_alpha.get(f"/api/teams/{world.ids['team_alpha']}/queue")).json()["items"][0]
    scoped = (await lead_alpha.get("/api/jobs")).json()["items"][0]
    assert (scoped["assignment_count"], scoped["applied_count"]) == (0, 0)                 # the job list is scoped correctly
    assert (item["job"]["assignment_count"], item["job"]["applied_count"]) == (0, 0), (
        f"Team Alpha's queue reports Team Beta's activity: assigned={item['job']['assignment_count']}, applied={item['job']['applied_count']}")


async def test_finding_search_keeps_assigning_for_a_deactivated_team_lead(world):
    """MEDIUM - app/portal/search_runner.py:95 loads the initiator once and :126 re-checks only the member before
    auto-assigning. A lead who was deactivated (or removed from the team; same result) after queueing searches keeps
    producing assignments in their own name until every queued run has finished, and nothing limits how many they
    queued (next test). Deactivation revokes sessions, not the work already handed to the worker.
    EXPECTED: a run whose initiator is no longer an active manager of that team assigns nothing."""
    lead, admin = await world.as_tl("alpha"), await world.as_admin()
    gate, real_run = asyncio.Event(), world.engine.run

    async def held(state, request, resume):
        await gate.wait()
        return await real_run(state, request, resume)

    world.engine.run = held
    started = await lead.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a1"])], "target_count": 3, "deep_search": False})
    assert started.status_code == 202
    run_id = started.json()["search"]["id"]
    for _ in range(100):
        if (await admin.get(f"/api/searches/{run_id}")).json()["status"] == "running":
            break
        await asyncio.sleep(0.05)
    off = await admin.patch(f"/api/users/{world.ids['tl_alpha']}", json={"is_active": False})
    assert off.status_code == 200 and off.json()["status"] == "deactivated"
    gate.set()
    await world.queue.drain()
    async with world.sessions() as db:
        created = (await db.execute(select(func.count()).select_from(JobAssignment).where(JobAssignment.assigned_by == world.ids["tl_alpha"]))).scalar_one()
    assert created == 0, f"{created} jobs were assigned in the name of a Team Lead after that account had been deactivated"


async def test_finding_event_stream_keeps_sending_after_the_account_is_deactivated(world):
    """LOW - routers/searches.py:96 authorizes once; stream() (:99) then re-reads the run every second through its own
    database session until the run ends (a run may take an hour) and never looks at the caller again. A manager whose
    session was revoked (sign-out, deactivation) or who lost the team keeps receiving the run's snapshots: member names
    and e-mails, per-member progress, counts.
    EXPECTED: the stream ends once the session is no longer valid or the run is out of the caller's scope."""
    job = await add_job(world)
    run_id = await seed_run(world, initiated_by=world.ids["tl_alpha"], team_id=world.ids["team_alpha"],
                            results=[(world.ids["cand_a1"], job)], status="running")
    lead, admin = await world.as_tl("alpha"), await world.as_admin()
    stream = asyncio.create_task(lead.get(f"/api/searches/{run_id}/events"))
    await asyncio.sleep(0.3)                                                                  # the first snapshot is out
    assert (await admin.patch(f"/api/users/{world.ids['tl_alpha']}", json={"is_active": False})).status_code == 200
    assert (await lead.get("/api/auth/me")).status_code == 401                                # every session is revoked
    async with world.sessions() as db:                                                        # the run moves on
        await db.execute(update(SearchRun).where(SearchRun.id == uuid.UUID(run_id)).values(matched_count=5))
        await db.commit()
    await asyncio.sleep(1.3)
    async with world.sessions() as db:
        await db.execute(update(SearchRun).where(SearchRun.id == uuid.UUID(run_id)).values(status="completed", completed_at=datetime.now(timezone.utc)))
        await db.commit()
    response = await asyncio.wait_for(stream, 10)
    assert '"matched_count":5' not in response.text, "progress made after the deactivation was still streamed to the deactivated account"


async def test_finding_no_limit_on_queued_searches(world, monkeypatch):
    """LOW/MEDIUM - services/searches.py:44 create_search() limits members and results per run but not the number of
    runs. The single-user app had MAX_CONCURRENT_SEARCHES and SEARCHES_PER_10_MINUTES (still in app/config.py:99); the
    portal uses neither. One Team Lead, or one stolen lead session, can queue searches without end: every team waits
    behind them (the worker runs 2 at a time) and each deep search spends paid AI web-search calls.
    EXPECTED: a per-user / per-team cap on queued and running searches, answered with 429 or 409."""
    async def accepted(run_id):          # keep the runs queued instead of executing twelve real searches
        return True
    monkeypatch.setattr(world.queue, "enqueue", accepted)
    lead = await world.as_tl("alpha")
    codes = [(await lead.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a1"])], "target_count": 50, "deep_search": True})).status_code
             for _ in range(12)]
    waiting = (await lead.get("/api/searches", params={"status": "active"})).json()["total"]
    assert any(code in (409, 429) for code in codes), f"{waiting} deep searches accepted from one Team Lead within a second, none refused"


# ================================================================ findings: what a member receives
async def test_finding_member_learns_about_private_notes_and_manager_only_events(world, monkeypatch):
    """LOW - the text of a private note is hidden, its existence is not: services/assignments.py:145 counts private
    notes in note_count (list and detail), :184 returns every event including note_added with metadata.private = true
    and its author, and routers/users.py:141 leaves the same event in the member's activity feed. The detail view also
    shows link_opened_by_manager, which the activity feed deliberately hides.
    EXPECTED: for a MEMBER, private notes are not counted and manager-only events are not listed anywhere."""
    allow_job_links(monkeypatch)
    job = await add_job(world)
    admin, lead, member = await world.as_admin(), await world.as_tl("alpha"), await world.as_member("a1")
    assignment = await assign(admin, job, world.ids["cand_a1"])
    assert (await lead.post(f"/api/assignments/{assignment}/notes", json={"note": "weak candidate, do not push", "is_private": True})).status_code == 201
    assert (await lead.get(f"/api/assignments/{assignment}/open-job", follow_redirects=False)).status_code == 303
    detail = (await member.get(f"/api/assignments/{assignment}")).json()
    listed = (await member.get("/api/assignments")).json()["items"][0]
    feeds = [await member.get(f"/api/users/{world.ids['member_a1']}/activity"), await member.get(f"/api/candidates/{world.ids['cand_a1']}/activity")]
    assert [f.status_code for f in feeds] == [200, 200] and all(f.json()["total"] >= 1 for f in feeds)     # both feeds show this job
    feed = [item["event_type"] for f in feeds for item in f.json()["items"]]
    assert detail["notes"] == []                                                           # the text itself is withheld
    told = {
        "note_count (detail)": detail["note_count"], "note_count (list)": listed["note_count"],
        "private note event in detail": [e["actor"]["name"] for e in detail["events"] if e["event_type"] == "note_added" and e["metadata"].get("private")],
        "note_added in activity feed": feed.count("note_added"),
        "link_opened_by_manager in activity feed": feed.count("link_opened_by_manager"),
        "link_opened_by_manager in detail": sum(e["event_type"] == "link_opened_by_manager" for e in detail["events"]),
    }
    assert {key: value for key, value in told.items() if value} == {}, f"the member can tell: {told}"


async def test_finding_posting_url_in_description_reaches_the_member(world):
    """LOW/MEDIUM - JobSummary / JobDetail leave the URL fields out, but services/jobs.py:318 detail_out() passes the
    description through untouched (services/assignments.py:180). Postings very often spell out their own link
    ("apply at https://..."), so the member receives the raw URL in GET /api/assignments/{id} and can open the job
    without the audited redirect; no link-open is recorded.
    EXPECTED: links in the member's copy of the description are removed or masked (managers keep the original)."""
    link = "https://careers.northwind.example/apply/777"
    job = await add_job(world, apply_url=link, description=f"Build ML systems with Python.\n\nTo apply, go to {link} and upload your CV.")
    admin, member = await world.as_admin(), await world.as_member("a1")
    assignment = await assign(admin, job, world.ids["cand_a1"])
    detail = await member.get(f"/api/assignments/{assignment}")
    assert detail.status_code == 200 and detail.json()["job_urls"] is None
    found = re.findall(r"https?://[^ \"\\]+", detail.text)
    assert found == [], f"the member's response contains the posting link: {found}"


async def test_finding_members_receive_manager_sign_in_emails(world):
    """LOW - services/common.py:15 user_ref() always includes the e-mail address, which is the sign-in name. Every member
    therefore gets the Admin's and the Team Lead's login identifiers in assigned_by, event actors and note authors of
    their own assignments: exactly what the unthrottled sign-in (first finding) needs.
    EXPECTED: a MEMBER sees the names of managers, not their sign-in addresses."""
    job = await add_job(world)
    admin, lead, member = await world.as_admin(), await world.as_tl("alpha"), await world.as_member("a1")
    assignment = await assign(admin, job, world.ids["cand_a1"])
    assert (await lead.post(f"/api/assignments/{assignment}/notes", json={"note": "Please apply this week"})).status_code == 201
    seen = "\n".join([(await member.get("/api/assignments")).text, (await member.get(f"/api/assignments/{assignment}")).text,
                      (await member.get(f"/api/users/{world.ids['member_a1']}/activity")).text])
    candidate = f"/api/candidates/{world.ids['cand_a1']}"            # created and its resume uploaded by the Team Lead (fixture)
    for path in ("/api/candidates", candidate, f"{candidate}/profile", f"{candidate}/resume", f"{candidate}/activity", f"{candidate}/analytics",
                 "/api/notifications", f"/api/users/{world.ids['member_a1']}/dashboard"):
        page = await member.get(path)
        assert page.status_code == 200, path
        seen += "\n" + page.text
    assert "Lead Alpha" in seen and "Ada Admin" in seen                                    # the managers are named in what was read
    leaked = [address for address in ("admin@example.com", "tl.alpha@example.com") if address in seen]
    assert leaked == [], f"sign-in addresses of managers sent to a member: {leaked}"


async def test_finding_cross_site_get_changes_tracking_state(world, monkeypatch):
    """LOW - deps.py:99 checks Origin and the CSRF token for POST/PUT/PATCH/DELETE only, but two GET routes write:
    GET /api/assignments/{id}/open-job records a link_opened event and moves ASSIGNED to VIEWED, and
    GET /api/assignments/{id} sets viewed_at / VIEWED. The session cookie is SameSite=Lax, which browsers attach to
    cross-site top-level navigations, so any page that knows an assignment id (a Team Lead does) can make a member's
    browser "open" and "view" jobs, inflating the numbers the dashboards report.
    EXPECTED: a request that says it is cross-site (Origin / Sec-Fetch-Site) is refused or served without recording."""
    allow_job_links(monkeypatch)
    admin, member = await world.as_admin(), await world.as_member("a1")
    first = await assign(admin, await add_job(world), world.ids["cand_a1"])
    second = await assign(admin, await add_job(world, title="ML Engineer"), world.ids["cand_a1"])
    del member.headers["x-csrf-token"]
    foreign = {"origin": "https://evil.example", "referer": "https://evil.example/", "sec-fetch-site": "cross-site",
               "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
    opened = await member.get(f"/api/assignments/{first}/open-job", headers=foreign, follow_redirects=False)
    viewed = await member.get(f"/api/assignments/{second}", headers=foreign)
    async with world.sessions() as db:
        events = sorted((await db.execute(select(ApplicationEvent.event_type).where(ApplicationEvent.event_type != "assigned"))).scalars())
        statuses = sorted((await db.execute(select(JobAssignment.status))).scalars())
    assert events == [] and statuses == ["ASSIGNED", "ASSIGNED"], (
        f"cross-site GETs answered {opened.status_code} / {viewed.status_code} and recorded {events}; statuses are now {statuses}")


# ================================================================ findings: input handling
async def test_finding_assignment_accepts_foreign_or_unknown_search_run_id(world):
    """LOW - search_run_id in POST /api/assignments and /api/assignments/bulk (schemas.py:541, :553) is stored as sent
    (services/assignments.py:84): never checked against the caller's scope, the member or the job. A Team Lead can tie
    their assignment to another team's run (the id then also goes to the member in the notification), and an id that
    does not exist ends in a foreign-key error: HTTP 500.
    EXPECTED: a run the caller cannot see is refused or ignored; an unknown id is a 4xx."""
    job = await add_job(world)
    admin = await world.as_admin()
    assert (await admin.post(f"/api/teams/{world.ids['team_alpha']}/queue", json={"job_ids": [job]})).status_code == 201
    beta_run = await seed_run(world, initiated_by=world.ids["tl_beta"], team_id=world.ids["team_beta"], results=[(world.ids["cand_b1"], job)])
    lead_alpha = await quiet_client(world, "tl.alpha@example.com")
    assert (await lead_alpha.get(f"/api/searches/{beta_run}")).status_code == 403
    foreign = await lead_alpha.post("/api/assignments", json={"job_id": job, "candidate_id": str(world.ids["cand_a1"]), "search_run_id": beta_run})
    unknown = await lead_alpha.post("/api/assignments", json={"job_id": job, "candidate_id": str(world.ids["cand_a2"]), "search_run_id": str(uuid.uuid4())})
    async with world.sessions() as db:
        linked = (await db.execute(select(func.count()).select_from(JobAssignment).where(JobAssignment.search_run_id == uuid.UUID(beta_run)))).scalar_one()
    assert linked == 0, f"POST answered {foreign.status_code}: Team Alpha's assignment is now linked to Team Beta's search run"
    assert unknown.status_code < 500, "an unknown search_run_id produced an Internal Server Error"


async def test_finding_application_date_is_not_sanity_checked(world):
    """LOW - services/assignments.py:235 only refuses dates in the future. A member may record an application years
    before the job was assigned (the "applied" time series then shows work that cannot have happened), and a timestamp
    without a time zone raises TypeError at :236: HTTP 500.
    EXPECTED: 422 for a date earlier than assigned_at; a zone-less timestamp is rejected or read as UTC, never a 500."""
    admin, member = await world.as_admin(), await quiet_client(world, "member.a1@example.com")
    assignment = await assign(admin, await add_job(world), world.ids["cand_a1"])
    zoneless = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()
    naive = await member.patch(f"/api/assignments/{assignment}/status", json={"status": "APPLIED", "applied_at": zoneless})
    early = await member.patch(f"/api/assignments/{assignment}/status", json={"status": "APPLIED", "applied_at": "1999-01-01T00:00:00Z"})
    assert naive.status_code != 500, "applied_at without a time zone caused an Internal Server Error"
    assert early.status_code == 422, f"an application dated 1999 for a job assigned today was accepted ({early.status_code})"


async def test_finding_malformed_input_causes_internal_server_errors(world):
    """LOW - several inputs reach PostgreSQL or Python arithmetic unvalidated and end as HTTP 500: a page number whose
    offset leaves int64 (services/common.py:69), a NUL character in any text or search value (PostgreSQL refuses it),
    date_to=9999-12-31 (+1 day overflows, routers/assignments.py:51 and services/analytics.py:26) and preset filters
    that fail validation outside FastAPI's handler (routers/searches.py:160). The answer is generic, but app.py:86 logs
    the traceback with the SQL parameters unless APP_ENV=production, which docker-compose.yml does not set.
    EXPECTED: 4xx (or a normal answer) for every one of them."""
    admin = await quiet_client(world, "admin@example.com")
    lead = await quiet_client(world, "tl.alpha@example.com")
    member = await quiet_client(world, "member.a1@example.com")
    assignment = await assign(admin, await add_job(world), world.ids["cand_a1"])
    own, candidate = world.ids["member_a1"], world.ids["cand_a1"]
    answers = {
        "GET /api/assignments?page=2^63-1": await member.get("/api/assignments", params={"page": 2**63 - 1}),
        "GET /api/assignments?date_to=9999-12-31": await member.get("/api/assignments", params={"date_to": "9999-12-31"}),
        "GET /api/users/{id}/dashboard?date_to=9999-12-31": await member.get(f"/api/users/{own}/dashboard", params={"date_to": "9999-12-31"}),
        "GET /api/assignments?status=<NUL>": await member.get("/api/assignments", params={"status": "APPLIED,\x00"}),
        "POST /api/assignments/{id}/notes with NUL": await member.post(f"/api/assignments/{assignment}/notes", json={"note": "a\u0000b"}),
        "PATCH /api/users/{id} name with NUL": await member.patch(f"/api/users/{own}", json={"name": "A\u0000B"}),
        "GET /api/users?q=<NUL>": await lead.get("/api/users", params={"q": "a\x00b"}),
        "GET /api/candidates?page=2^63-1": await member.get("/api/candidates", params={"page": 2**63 - 1}),
        "GET /api/candidates?q=<NUL>": await lead.get("/api/candidates", params={"q": "a\x00b"}),
        "GET /api/candidates/{id}/activity?page=2^63-1": await member.get(f"/api/candidates/{candidate}/activity", params={"page": 2**63 - 1}),
        "GET /api/candidates/{id}/analytics?date_to=9999-12-31": await member.get(f"/api/candidates/{candidate}/analytics", params={"date_to": "9999-12-31"}),
        "PATCH /api/candidates/{id} name with NUL": await lead.patch(f"/api/candidates/{candidate}", json={"name": "A\u0000B"}),
        "PATCH /api/candidates/{id} notes and phone with NUL": await lead.patch(f"/api/candidates/{candidate}", json={"notes": "a\u0000b", "phone": "1\u00002"}),
        "POST /api/candidates e-mail with NUL": await lead.post("/api/candidates", json={"name": "New Candidate", "email": "a\u0000b@example.com"}),
        "PATCH /api/candidates/{id}/profile with NUL": await lead.patch(f"/api/candidates/{candidate}/profile", json={"skills": ["Py\u0000thon"], "desired_titles": ["A\u0000I"]}),
        "POST /api/candidates/{id}/resume file name with NUL": await lead.post(f"/api/candidates/{candidate}/resume", files={"file": ("c\x00v.txt", b"Python engineer with PyTorch experience. " * 10, "text/plain")}),
        "POST /api/candidates/{id}/resume text with NUL": await lead.post(f"/api/candidates/{candidate}/resume", files={"file": ("cv.txt", b"Python engineer \x00 with PyTorch experience. " * 10, "text/plain")}),
        "POST /api/search-presets with target_count 0": await lead.post("/api/search-presets", json={"name": "bad", "filters": {"target_count": 0}}),
    }
    crashed = [name for name, response in answers.items() if response.status_code >= 500]
    assert crashed == [], f"Internal Server Error for: {crashed}"


async def test_finding_engine_urls_are_stored_without_scheme_check(world):
    """LOW - a manually entered job goes through safe_url() (services/jobs.py:98); a posting from the search engine does
    not: upsert_engine_job() (:137) stores job.url / job.apply_url as received. The Greenhouse, Lever and Ashby
    providers pass the URL fields of the employer's board through unchecked, so a board can deliver javascript: or
    data: values. Members are protected (Open Job answers 409), but managers get the raw value in canonical_url,
    apply_url and job_urls; if the UI links it, it runs in the Admin's or Team Lead's session.
    EXPECTED: only http(s) URLs are stored or returned, whichever way the posting came in."""
    hostile = EngineJob(title="AI Engineer", company="Hostile Board", url="javascript:alert(document.domain)",
                        apply_url="data:text/html,<script>alert(1)</script>", source="Greenhouse", sources=["Greenhouse"],
                        ats="greenhouse", ats_id="666", description="Plain text",
                        verification="Listed in the employer's live Greenhouse job board API")
    try:
        async with world.sessions() as db:
            posting = await jobs_service.upsert_engine_job(db, hostile)
            await db.commit()
            job = str(posting.id)
    except Exception:
        return                                                             # refusing to store it is a valid fix
    admin = await world.as_admin()
    view = (await admin.get(f"/api/jobs/{job}")).json()
    unsafe = {field: view[field] for field in ("canonical_url", "apply_url") if view[field] and not view[field].startswith(("http://", "https://"))}
    assert unsafe == {}, f"returned to managers as posting links: {unsafe}"


async def test_finding_description_cleaning_lets_markup_through(world):
    """LOW / informational - descriptions are promised as "plain text, never HTML" (schemas.py:348). services/jobs.py:92
    clean_text() and job_normalizer.html_to_structured_text() decode entities twice (so "&amp;lt;img ...&amp;gt;",
    which is text in the source, becomes a real <img ...> tag) and leave a tag that never closes untouched. Harmless
    while every client escapes the field; an HTML e-mail, an export or one dangerouslySetInnerHTML away from XSS.
    EXPECTED: no tag-like markup in a stored description (the existing job test asserts '"<" not in description')."""
    admin = await world.as_admin()
    survived = {}
    for raw in ("<p>Hello</p>&amp;lt;img src=x onerror=alert(1)&amp;gt;", "<p>Hi</p><img src=x onerror=alert(1)"):
        job = await add_job(world, description=raw)
        text = (await admin.get(f"/api/jobs/{job}")).json()["description"]
        if "<img" in text:
            survived[raw] = text
    assert survived == {}, f"markup in 'plain text' descriptions: {survived}"


# ================================================================ areas that held
def test_sound_every_state_changing_route_is_behind_session_and_csrf_or_the_origin_check():
    """Structural, over every operation (71 when written; the candidate routes replaced the profile and resume ones): whatever is not one of the six public ones needs the session, which
    enforces the CSRF token and Origin on POST/PUT/PATCH/DELETE; the four public forms carry the same-origin check.
    A new route that forgets the dependency shows up here as an unexpected public operation."""
    from fastapi.routing import APIRoute
    from app.portal import deps
    import importlib
    import pkgutil
    from app.portal import routers
    from app.portal.routers import admin, assignments, auth, candidates, invitations, jobs, salesforce, searches, teams, users, web_search

    def reached(dependant, found):
        for inner in dependant.dependencies:
            found.add(inner.call)
            reached(inner, found)
        return found

    protection = {}
    modules = (auth, invitations, teams, users, candidates, jobs, searches, assignments, salesforce, admin, web_search)
    with_routes = {info.name for info in pkgutil.iter_modules(routers.__path__)
                   if hasattr(importlib.import_module(f"{routers.__name__}.{info.name}"), "router")}
    assert with_routes == {module.__name__.rsplit(".", 1)[1] for module in modules}       # a new router module cannot stay outside this test
    for module in modules:
        for route in module.router.routes:
            assert isinstance(route, APIRoute)
            used = reached(route.dependant, set())
            level = "session" if deps.authenticated in used else ("origin" if deps.same_origin in used else "public")
            protection.update({(method, route.path): level for method in route.methods})
    assert len(protection) >= 71
    # Every way to change a candidate, its profile or its resume needs the session (and with it the CSRF token and Origin).
    changing = {key for key in protection if key[0] != "GET" and key[1].startswith("/api/candidates")}
    assert changing == {("POST", "/api/candidates"), ("PATCH", "/api/candidates/{candidate_id}"), ("PATCH", "/api/candidates/{candidate_id}/profile"),
                        ("POST", "/api/candidates/{candidate_id}/resume"), ("DELETE", "/api/candidates/{candidate_id}/resume"),
                        ("POST", "/api/candidates/import-salesforce"), ("POST", "/api/candidates/{candidate_id}/salesforce-refresh")}
    # Salesforce: two read routes, both behind the session; the portal has no route that could write to Salesforce.
    assert {key: level for key, level in protection.items() if key[1].startswith("/api/salesforce")} == {
        ("GET", "/api/salesforce/status"): "session", ("GET", "/api/salesforce/candidates"): "session"}
    assert {protection[key] for key in protection if key[1].startswith("/api/candidates")} == {"session"}
    assert not any(path.startswith(("/api/resumes", "/api/members")) or path.endswith("/profile") and path.startswith("/api/users")
                   for _, path in protection)                                              # the routes the candidate ones replaced are gone
    assert {key for key, level in protection.items() if level == "public"} == {("GET", "/api/health"), ("GET", "/api/invitations/validate"), ("GET", "/api/auth/password-policy")}
    assert {key for key, level in protection.items() if level == "origin"} == {
        ("POST", "/api/auth/login"), ("POST", "/api/auth/forgot-password"), ("POST", "/api/auth/reset-password"), ("POST", "/api/invitations/accept")}


async def test_sound_object_level_checks_hold_across_teams_and_members(world):
    """Every id-taking route not covered by the older tests, tried with an id of another team or another member.
    Candidates (details, profile, resume, its file, numbers, activity) answer 404 outside the caller's scope, exactly
    like an id that does not exist; a member who can see a candidate still changes nothing (403)."""
    job = await add_job(world)
    admin, lead_a, lead_b = await world.as_admin(), await world.as_tl("alpha"), await world.as_tl("beta")
    a1, a2 = await world.as_member("a1"), await world.as_member("a2")
    id_a1, id_a2, id_b1 = (str(world.ids[key]) for key in ("member_a1", "member_a2", "member_b1"))
    c_a1, c_a2, c_b1 = (str(world.ids[key]) for key in ("cand_a1", "cand_a2", "cand_b1"))
    made = await admin.post("/api/assignments/bulk", json={"job_ids": [job], "candidate_ids": [c_a1, c_a2, c_b1]})
    assert made.status_code == 201 and made.json()["created"] == 3
    async with world.sessions() as db:
        of = {str(candidate): str(row) for row, candidate in (await db.execute(select(JobAssignment.id, JobAssignment.candidate_id))).all()}
        notice = (await db.execute(select(Notification.id).where(Notification.user_id == uuid.UUID(id_a2)))).scalars().first()
    cv = {"file": ("cv.txt", b"Python engineer " * 10)}

    async def candidate_routes(client, candidate: str) -> dict:
        """Status of every candidate route for this caller: the reads first, then every way to change something."""
        base = f"/api/candidates/{candidate}"
        return {
            "GET": (await client.get(base)).status_code, "GET profile": (await client.get(f"{base}/profile")).status_code,
            "GET resume": (await client.get(f"{base}/resume")).status_code, "GET download": (await client.get(f"{base}/resume/download")).status_code,
            "GET analytics": (await client.get(f"{base}/analytics")).status_code, "GET activity": (await client.get(f"{base}/activity")).status_code,
            "PATCH": (await client.patch(base, json={"name": "Renamed", "notes": "x", "is_active": False})).status_code,
            "PATCH member": (await client.patch(base, json={"assigned_member_id": None})).status_code,
            "PATCH profile": (await client.patch(f"{base}/profile", json={"skills": ["Go"]})).status_code,
            "POST resume": (await client.post(f"{base}/resume", files=cv)).status_code,
            "DELETE resume": (await client.delete(f"{base}/resume")).status_code,
        }

    reads = ("GET", "GET profile", "GET resume", "GET download", "GET analytics", "GET activity")
    hidden = dict.fromkeys((*reads, "PATCH", "PATCH member", "PATCH profile", "POST resume", "DELETE resume"), 404)
    read_only = {**dict.fromkeys(reads, 200), **dict.fromkeys(("PATCH", "PATCH member", "PATCH profile", "POST resume", "DELETE resume"), 403)}

    async def stored() -> list:
        """Everything the refused calls below tried to change."""
        async with world.sessions() as db:
            candidates = (await db.execute(select(Candidate.name, Candidate.notes, Candidate.is_active, Candidate.assigned_member_id).order_by(Candidate.name))).all()
            resumes = (await db.execute(select(Resume.candidate_id, Resume.filename, Resume.version, Resume.is_current).order_by(Resume.candidate_id, Resume.version))).all()
            skills = (await db.execute(select(CandidateProfile.candidate_id, CandidateProfile.skills).order_by(CandidateProfile.candidate_id))).all()
        return [[tuple(row) for row in rows] for rows in (candidates, resumes, skills)]

    before = await stored()
    assert len(before[0]) == 4 and len(before[1]) == 4 and all(row[1] == ["Python", "PyTorch"] for row in before[2])

    # A Team Lead against another team's assignment, candidate (with profile and resume) and queue.
    other = of[c_b1]
    assert (await lead_a.get(f"/api/assignments/{other}")).status_code == 403
    assert (await lead_a.patch(f"/api/assignments/{other}/status", json={"status": "APPLIED"})).status_code == 403
    assert (await lead_a.post(f"/api/assignments/{other}/notes", json={"note": "x"})).status_code == 403
    assert (await lead_a.get(f"/api/assignments/{other}/open-job", follow_redirects=False)).status_code == 403
    assert (await lead_a.delete(f"/api/assignments/{other}")).status_code == 403
    assert await candidate_routes(lead_a, c_b1) == hidden
    assert await candidate_routes(lead_a, str(uuid.uuid4())) == hidden                         # unknown and foreign look the same
    assert (await lead_a.get("/api/assignments", params={"team_id": str(world.ids["team_beta"])})).json()["total"] == 0
    assert (await lead_a.get("/api/assignments", params={"member_id": id_b1})).json()["total"] == 0
    assert (await lead_a.get("/api/assignments", params={"candidate_id": c_b1})).json()["total"] == 0
    assert (await lead_a.get("/api/assignments/export.csv", params={"member_id": id_b1})).text.count("\n") == 1      # header only
    assert (await lead_a.get("/api/assignments/export.csv", params={"candidate_id": c_b1})).text.count("\n") == 1    # header only
    assert (await lead_a.get("/api/candidates", params={"team_id": str(world.ids["team_beta"])})).json()["total"] == 0
    assert (await lead_a.get("/api/candidates", params={"assigned_member_id": id_b1})).json()["total"] == 0
    assert (await lead_a.get("/api/candidates", params={"q": "candidate.b1@example.com"})).json()["total"] == 0
    assert {c["name"] for c in (await lead_a.get("/api/candidates", params={"q": "%"})).json()["items"]} == {"Candidate A1", "Candidate A2"}
    assert (await lead_a.get("/api/searches", params={"candidate_id": c_b1})).json()["total"] == 0
    assert (await lead_a.get("/api/invitations", params={"team_id": str(world.ids["team_beta"])})).json()["total"] == 0
    queued = (await admin.post(f"/api/teams/{world.ids['team_beta']}/queue", json={"job_ids": [job]})).json()[0]
    assert (await lead_a.patch(f"/api/team-queue/{queued['id']}", json={"status": "dismissed"})).status_code == 403
    assert (await lead_a.post(f"/api/teams/{world.ids['team_alpha']}/queue", json={"job_ids": [job]})).status_code == 403    # Admin only
    assert (await lead_a.post("/api/assignments/bulk", json={"job_ids": [job], "candidate_ids": [c_a2], "queue_item_ids": [queued["id"]]})).status_code == 201
    async with world.sessions() as db:
        assert (await db.get(TeamJobQueue, uuid.UUID(queued["id"]))).status == "pending"       # another team's queue entry is untouched
    # Candidate ids of another team in a body: 403, and nothing is created for them.
    assert (await lead_a.post("/api/assignments", json={"job_id": job, "candidate_id": c_b1})).status_code == 403
    assert (await lead_a.post("/api/assignments/bulk", json={"job_ids": [job], "candidate_ids": [c_a2, c_b1]})).status_code == 403
    assert (await lead_a.post("/api/searches", json={"candidate_ids": [c_b1], "target_count": 3, "deep_search": False})).status_code == 403
    preset = (await lead_b.post("/api/search-presets", json={"name": "Beta preset", "filters": {"target_count": 20}})).json()
    assert (await lead_a.delete(f"/api/search-presets/{preset['id']}")).status_code == 404
    assert (await lead_a.get("/api/search-presets")).json() == []

    # A Team Lead against other leads, the Admin and their own account.
    assert (await lead_a.patch(f"/api/users/{world.ids['tl_beta']}", json={"name": "Renamed"})).status_code == 403
    assert (await lead_a.get(f"/api/users/{world.ids['admin']}")).status_code == 403
    assert (await lead_a.patch(f"/api/users/{world.ids['admin']}", json={"is_active": False})).status_code == 403
    assert (await lead_a.post(f"/api/users/{world.ids['admin']}/reset-link")).status_code == 403
    assert (await lead_a.post(f"/api/users/{world.ids['tl_alpha']}/reset-link")).status_code == 403
    assert (await lead_a.patch(f"/api/users/{world.ids['tl_alpha']}", json={"is_active": False})).status_code == 409
    assert (await lead_a.get("/api/users", params={"role": "ADMIN"})).json()["total"] == 0
    assert {u["email"] for u in (await lead_a.get("/api/users", params={"q": "%"})).json()["items"]} == {"member.a1@example.com", "member.a2@example.com"}
    # A Team Lead and their own candidates: not into another team, not to a member of another team, a lead or an Admin.
    assert (await lead_a.post("/api/candidates", json={"name": "Elsewhere", "team_id": str(world.ids["team_beta"])})).status_code == 403
    assert (await lead_a.patch(f"/api/candidates/{c_a1}", json={"team_id": str(world.ids["team_beta"])})).status_code == 403
    for holder in (id_b1, str(world.ids["tl_alpha"]), str(world.ids["admin"]), str(uuid.uuid4())):
        assert (await lead_a.patch(f"/api/candidates/{c_a1}", json={"assigned_member_id": holder})).status_code == 422
        assert (await lead_a.post("/api/candidates", json={"name": "Handed Out", "assigned_member_id": holder})).status_code == 422
    # The routes that took a resume or a profile for a user account are gone, so nobody has one outside a candidate.
    assert (await lead_a.post("/api/resumes/upload", files=cv)).status_code == 404
    assert (await lead_a.get(f"/api/users/{id_a1}/profile")).status_code == 404
    assert (await lead_a.get(f"/api/members/{id_a1}/resume")).status_code == 404

    # A member against another member's candidate, their own candidate, manager routes and their own withdrawn assignment.
    assert await candidate_routes(a1, c_a2) == hidden                                          # same team, another member's
    assert await candidate_routes(a1, c_b1) == hidden
    assert await candidate_routes(a1, str(uuid.uuid4())) == hidden                             # unknown and foreign look the same
    assert await candidate_routes(a1, c_a1) == read_only                                       # their own: they read, managers write
    assert (await a1.post("/api/candidates", json={"name": "Made By Member"})).status_code == 403
    assert [c["id"] for c in (await a1.get("/api/candidates")).json()["items"]] == [c_a1]
    for narrowing in ({"team_id": str(world.ids["team_beta"])}, {"assigned_member_id": id_a2}, {"unassigned": "true"}, {"q": "Candidate A2"}):
        assert (await a1.get("/api/candidates", params=narrowing)).json()["total"] == 0
    assert (await a1.get("/api/assignments", params={"candidate_id": c_a2})).json()["total"] == 0
    assert (await a1.get("/api/assignments", params={"member_id": id_a2})).json()["total"] == 0
    assert (await a1.post("/api/resumes/upload", data={"member_id": id_a2}, files=cv)).status_code == 404     # removed route
    assert await stored() == before, "a refused call changed a candidate, a profile or a resume"
    assert (await a1.get(f"/api/users/{id_a2}/activity")).status_code == 403
    assert (await a1.get(f"/api/analytics/member/{id_a2}")).status_code == 403
    assert (await a1.post(f"/api/notifications/{notice}/read")).status_code == 404
    assert (await a1.post(f"/api/users/{id_a1}/reset-link")).status_code == 403
    assert (await a1.get(f"/api/jobs/{job}")).status_code == 403
    assert (await a1.post("/api/assignments/bulk", json={"job_ids": [job], "candidate_ids": [c_a1]})).status_code == 403
    assert (await a1.post("/api/assignments", json={"job_id": job, "candidate_id": c_a1})).status_code == 403
    assert (await a1.delete(f"/api/assignments/{of[c_a1]}")).status_code == 403
    private = await a2.post(f"/api/assignments/{of[c_a2]}/notes", json={"note": "mine", "is_private": True})
    assert private.status_code == 201 and private.json()["is_private"] is False               # only managers write private notes
    assert (await admin.delete(f"/api/assignments/{of[c_a1]}")).status_code == 200             # withdrawn by a manager
    # 410: the member is told their candidate's job was taken back; someone else's assignment stays an anonymous 403.
    assert (await a1.patch(f"/api/assignments/{of[c_a1]}/status", json={"status": "APPLIED"})).status_code == 410
    assert (await a1.post(f"/api/assignments/{of[c_a1]}/notes", json={"note": "x"})).status_code == 410
    assert (await a1.get(f"/api/assignments/{of[c_a1]}/open-job", follow_redirects=False)).status_code == 410
    assert (await a2.get(f"/api/assignments/{of[c_a1]}")).status_code == 403
    assert (await a1.get("/api/assignments", params={"include_inactive": True})).json()["total"] == 0
    assert (await lead_a.patch(f"/api/assignments/{of[c_a1]}/status", json={"status": "APPLIED"})).status_code == 409

    # A candidate handed to another member: the old member loses it at once (candidate, file, jobs), the new one gains it.
    assert (await a2.get(f"/api/assignments/{of[c_a2]}")).status_code == 200
    assert (await lead_a.patch(f"/api/candidates/{c_a2}", json={"assigned_member_id": id_a1})).status_code == 200
    assert await candidate_routes(a2, c_a2) == hidden
    assert (await a2.get(f"/api/assignments/{of[c_a2]}")).status_code == 403
    assert (await a2.patch(f"/api/assignments/{of[c_a2]}/status", json={"status": "APPLIED"})).status_code == 403
    assert (await a2.get(f"/api/assignments/{of[c_a2]}/open-job", follow_redirects=False)).status_code == 403
    assert (await a2.get("/api/assignments", params={"include_inactive": True})).json()["total"] == 0
    assert (await a2.get("/api/candidates")).json()["total"] == 0
    assert await candidate_routes(a1, c_a2) == read_only
    assert (await a1.get(f"/api/assignments/{of[c_a2]}")).status_code == 200
    # ... and the same when the member's account leaves the team: the candidate stays behind, unassigned.
    assert (await admin.patch(f"/api/users/{id_a1}", json={"team_id": str(world.ids["team_beta"])})).status_code == 200
    assert await candidate_routes(a1, c_a2) == hidden and await candidate_routes(a1, c_a1) == hidden
    assert (await a1.get(f"/api/assignments/{of[c_a2]}")).status_code == 403
    assert (await a1.get("/api/candidates")).json()["total"] == 0 and (await a1.get("/api/assignments")).json()["total"] == 0
    assert (await lead_b.get(f"/api/candidates/{c_a2}")).status_code == 404 and (await lead_b.get(f"/api/assignments/{of[c_a2]}")).status_code == 403
    assert (await lead_a.get(f"/api/candidates/{c_a2}")).json()["assigned_member"] is None


async def test_sound_request_bodies_cannot_change_role_team_or_identity(world):
    """Mass assignment: fields the caller must not control are ignored, whoever sends them."""
    member, lead = await world.as_member("a1"), await world.as_tl("alpha")
    own = world.ids["member_a1"]
    tried = await member.patch(f"/api/users/{own}", json={"name": "Member A1", "role": "ADMIN", "email": "evil@example.com",
                                                         "password_hash": "x", "status": "active", "team": {"id": str(world.ids["team_beta"])}})
    assert tried.status_code == 200
    assert (tried.json()["role"], tried.json()["email"], tried.json()["team"]["name"]) == ("MEMBER", "member.a1@example.com", "Team Alpha")
    promoted = await lead.patch(f"/api/users/{own}", json={"role": "TEAM_LEAD", "email": "evil@example.com"})
    assert (promoted.json()["role"], promoted.json()["email"]) == ("MEMBER", "member.a1@example.com")
    assert (await lead.patch(f"/api/users/{world.ids['tl_alpha']}", json={"role": "ADMIN", "name": "Lead Alpha"})).json()["role"] == "TEAM_LEAD"
    # The profile belongs to the candidate now and only a manager writes it: the member is refused, the lead's body cannot redirect it.
    cand, other = world.ids["cand_a1"], world.ids["cand_a2"]
    body = {"candidate_id": str(other), "user_id": str(world.ids["member_a2"]), "skills": ["Go"], "completion": 100}
    assert (await member.patch(f"/api/candidates/{cand}/profile", json=body)).status_code == 403
    assert (await member.get(f"/api/candidates/{cand}/profile")).json()["skills"] == ["Python", "PyTorch"]
    profile = await lead.patch(f"/api/candidates/{cand}/profile", json=body)
    assert profile.status_code == 200 and profile.json()["candidate_id"] == str(cand) and profile.json()["skills"] == ["Go"]
    untouched = await (await world.as_member("a2")).get(f"/api/candidates/{other}/profile")
    assert untouched.json()["skills"] == ["Python", "PyTorch"] and untouched.json()["candidate_id"] == str(other)
    # The candidate record itself: identity, creator, team and derived fields are not the caller's to set.
    forged = {"id": str(other), "created_by": str(world.ids["admin"]), "team": {"id": str(world.ids["team_beta"])}, "has_resume": False,
              "profile_completion": 100, "stats": {"assigned": 99}}
    assert (await member.patch(f"/api/candidates/{cand}", json={"name": "Renamed By Member", **forged})).status_code == 403
    kept = await lead.patch(f"/api/candidates/{cand}", json={"name": "Candidate A1", **forged})
    assert kept.status_code == 200
    assert (kept.json()["id"], kept.json()["team"]["name"], kept.json()["created_by"]["name"], kept.json()["has_resume"]) == (
        str(cand), "Team Alpha", "Lead Alpha", True)
    assert (await lead.patch(f"/api/candidates/{cand}", json={"team_id": str(world.ids["team_beta"])})).status_code == 403    # Admin only
    assert (await member.post("/api/candidates", json={"name": "Made By Member"})).status_code == 403
    created = await lead.post("/api/candidates", json={"name": "New Candidate", **forged, "is_active": False})
    assert created.status_code == 201 and created.json()["id"] != str(other)
    assert (created.json()["team"]["name"], created.json()["created_by"]["name"], created.json()["is_active"], created.json()["has_resume"]) == (
        "Team Alpha", "Lead Alpha", True, False)
    assert (await lead.post("/api/candidates", json={"name": "Elsewhere", "team_id": str(world.ids["team_beta"])})).status_code == 403
    async with world.sessions() as db:
        names = sorted((await db.execute(select(Candidate.name))).scalars())
    assert names == ["Candidate A1", "Candidate A2", "Candidate B1", "Candidate B2", "New Candidate"]
    invited = await lead.post("/api/invitations", json={"name": "New Person", "email": "new@example.com", "role": "MEMBER",
                                                        "invited_by": str(world.ids["admin"]), "is_active": False, "password": "x" * 12})
    assert invited.status_code == 201 and invited.json()["invited_by"]["name"] == "Lead Alpha" and invited.json()["team"]["name"] == "Team Alpha"


async def test_sound_engine_job_urls_never_reach_a_member(world):
    """For postings the search engine stored (the older test uses a manual job): no URL and no ATS host in anything a
    member can read. Descriptions that spell out a link are the exception reported above."""
    lead = await world.as_tl("alpha")
    own, candidate = str(world.ids["member_a1"]), f"/api/candidates/{world.ids['cand_a1']}"
    started = await lead.post("/api/searches", json={"candidate_ids": [str(world.ids["cand_a1"])], "target_count": 6, "deep_search": False})
    assert started.status_code == 202
    await world.queue.drain()
    async with world.sessions() as db:
        stored = (await db.execute(select(JobPosting.canonical_url))).scalars().all()
    assert len(stored) == 6 and all(url.startswith("https://") for url in stored)
    member = await world.as_member("a1")
    listing = await member.get("/api/assignments", params={"page_size": 50})
    assert listing.json()["total"] == 6
    pages = [listing.text, (await member.get(f"/api/users/{own}/activity")).text, (await member.get("/api/notifications")).text,
             (await member.get(f"/api/users/{own}/dashboard")).text, (await member.get("/api/auth/me")).text]
    pages += [(await member.get(f"/api/assignments/{item['id']}")).text for item in listing.json()["items"]]
    for path in ("/api/candidates", candidate, f"{candidate}/activity", f"{candidate}/analytics", f"{candidate}/profile", f"{candidate}/resume"):
        page = await member.get(path)
        assert page.status_code == 200, path
        pages.append(page.text)
    assert (await member.get(f"{candidate}/activity")).json()["total"] >= 6 and (await member.get(candidate)).json()["stats"]["assigned"] == 6
    everything = "\n".join(pages)
    assert re.findall(r"https?://", everything) == []
    assert not any(marker in everything for marker in ("greenhouse.io", "lever.co", "ashbyhq.com", "canonical_url", "apply_url"))


async def test_sound_csv_export_neutralises_formulas(world):
    """Spreadsheet formulas in candidate and member names and in posting titles / companies are prefixed, and the file
    is an attachment. Columns: candidate, responsible_member, team, company, title, ..."""
    job = await add_job(world, title='=HYPERLINK("http://evil.example","x")', company="@SUM(1+1)")
    admin = await world.as_admin()
    assert (await admin.patch(f"/api/users/{world.ids['member_a1']}", json={"name": "+cmd|' /C calc'!A0"})).status_code == 422   # refused at the door
    assert (await admin.patch(f"/api/candidates/{world.ids['cand_a1']}", json={"name": "+cmd|' /C calc'!A0"})).status_code == 422
    assert (await admin.post("/api/candidates", json={"name": "=cmd|' /C calc'!A0", "team_id": str(world.ids["team_alpha"])})).status_code == 422
    async with world.sessions() as db:                                                # a row written before that rule could still hold one
        await db.execute(update(Candidate).where(Candidate.id == world.ids["cand_a1"]).values(name="+cmd|' /C calc'!A0"))
        await db.execute(update(User).where(User.id == world.ids["member_a1"]).values(name="-cmd|' /C calc'!B1"))
        await db.commit()
    await assign(admin, job, world.ids["cand_a1"])
    export = await admin.get("/api/assignments/export.csv")
    assert export.text.splitlines()[0].startswith("candidate,responsible_member,team,company,title,")
    row = export.text.splitlines()[1]
    assert row.startswith("'+cmd|' /C calc'!A0,'-cmd|' /C calc'!B1,Team Alpha,") and ",'@SUM(1+1)," in row and "\"'=HYPERLINK(" in row
    assert export.headers["content-disposition"].startswith("attachment") and export.headers["x-content-type-options"] == "nosniff"
