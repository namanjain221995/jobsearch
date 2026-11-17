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
    assert refused.status_code == 401 and refused.json()["error"] == "Not authenticated"
    assert sent["bytes"] == MIB                                    # at most 1 MB is read (and dropped), never parsed
    sent["bytes"] = 0
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


async def test_admin_defined_profile_fields_are_typed_and_reach_every_role(world):
    admin, member, lead = await world.as_admin(), await world.as_member("a1"), await world.as_tl("alpha")
    fields = [{"key": "notice_period", "label": "Notice period (weeks)", "type": "number"},
              {"key": "relocate", "label": "Willing to relocate", "type": "yes_no"},
              {"key": "shift", "label": "Preferred shift", "type": "choice", "options": ["Day", "Night"]},
              {"key": "linkedin", "label": "LinkedIn headline", "type": "text"}]
    assert (await admin.patch("/api/settings", json={"profile_extra_fields": fields})).status_code == 200
    assert (await admin.patch("/api/settings", json={"profile_extra_fields": [{"key": "Bad Key", "label": "x"}]})).status_code == 422
    assert (await admin.patch("/api/settings", json={"profile_extra_fields": [fields[0], fields[0]]})).status_code == 422
    assert (await admin.patch("/api/settings", json={"profile_extra_fields": [{"key": "c", "label": "C", "type": "choice", "options": ["one"]}]})).status_code == 422
    assert (await lead.patch("/api/settings", json={"profile_extra_fields": []})).status_code == 403
    me = (await member.get("/api/auth/me")).json()["settings"]
    assert [f["key"] for f in me["profile_extra_fields"]] == ["notice_period", "relocate", "shift", "linkedin"]
    assert me["password_min_length"] == world.settings.password_min_length and me["deep_search_default"] is True
    own = f"/api/users/{world.ids['member_a1']}/profile"
    saved = await member.patch(own, json={"extra": {"notice_period": "4", "relocate": True, "shift": "Night", "linkedin": "  ML   engineer ", "made_up": "x"}})
    assert saved.status_code == 200
    assert saved.json()["extra"] == {"notice_period": 4, "relocate": True, "shift": "Night", "linkedin": "ML engineer"}      # typed; unknown key dropped
    assert (await member.patch(own, json={"extra": {"shift": "Afternoon"}})).status_code == 422
    assert (await member.patch(own, json={"extra": {"notice_period": "soon"}})).status_code == 422
    assert (await member.patch(own, json={"extra": {"relocate": "yes"}})).status_code == 422
    partial = await member.patch(own, json={"extra": {"shift": "Day"}})                     # other values stay
    assert partial.json()["extra"] == {"notice_period": 4, "relocate": True, "shift": "Day", "linkedin": "ML engineer"}
    assert (await lead.get(own)).json()["extra"]["shift"] == "Day"                           # the Team Lead sees them
    assert (await (await world.client()).get("/api/auth/password-policy")).json() == {
        "min_length": world.settings.password_min_length, "max_length": 128, "character_kinds_required": 3}


async def test_stored_answers_always_match_the_fields_that_exist(world):
    """An answer is tied to its field. Removing a field, changing its type or taking an option away deletes the answers
    that no longer fit, so nothing comes back when a field with the same key is added later."""
    admin, member, other = await world.as_admin(), await world.as_member("a1"), await world.as_member("b1")
    fields = [{"key": "shift", "label": "  Preferred   shift ", "type": "choice", "options": [" Day ", "Night", "Night", "Flexible"]},
              {"key": "badge", "label": "Badge number", "type": "text"},
              {"key": "notice", "label": "Notice (weeks)", "type": "text"},
              {"key": "relocate", "label": "Willing to relocate", "type": "yes_no"}]
    saved = await admin.patch("/api/settings", json={"profile_extra_fields": fields})
    assert saved.status_code == 200
    shift = saved.json()["profile_extra_fields"][0]
    assert shift["label"] == "Preferred shift" and shift["options"] == ["Day", "Night", "Flexible"]       # tidied, repeats dropped
    blank = await admin.patch("/api/settings", json={"profile_extra_fields": [{"key": "x", "label": "   "}]})
    assert blank.status_code == 422                                                                        # a label of spaces is no label
    twins = await admin.patch("/api/settings", json={"profile_extra_fields": [{"key": "x", "label": "X", "type": "choice", "options": ["A", " A "]}]})
    assert twins.status_code == 422                                                                        # one option, listed twice

    own, theirs = f"/api/users/{world.ids['member_a1']}/profile", f"/api/users/{world.ids['member_b1']}/profile"
    answers = {"shift": "Night", "badge": "A-17", "notice": "4", "relocate": True}
    assert (await member.patch(own, json={"extra": answers})).json()["extra"] == answers
    assert (await other.patch(theirs, json={"extra": {"shift": "Day", "badge": "B-2"}})).json()["extra"] == {"shift": "Day", "badge": "B-2"}

    # Answers that cannot be stored are refused or cleared, never kept in another shape.
    assert (await member.patch(own, json={"extra": {"badge": "x" * 501}})).status_code == 422             # too long: said, not cut
    assert (await member.patch(own, json={"extra": {"badge": ["A-17"]}})).status_code == 422
    assert (await member.patch(own, json={"extra": {"badge": "   "}})).json()["extra"] == {k: v for k, v in answers.items() if k != "badge"}
    assert (await member.patch(own, json={"extra": {"badge": "A-17"}})).json()["extra"] == answers

    # "badge" is removed, "notice" becomes a number, "Night" is no longer offered, "relocate" becomes text.
    changed = [{"key": "shift", "label": "Preferred shift", "type": "choice", "options": ["Day", "Flexible"]},
               {"key": "notice", "label": "Notice (weeks)", "type": "number"},
               {"key": "relocate", "label": "Willing to relocate", "type": "text"}]
    assert (await admin.patch("/api/settings", json={"profile_extra_fields": changed})).status_code == 200
    assert (await member.get(own)).json()["extra"] == {"notice": 4}               # "4" still fits as a number; the rest went
    assert (await other.get(theirs)).json()["extra"] == {"shift": "Day"}          # an answer that still fits is untouched
    async with world.sessions() as db:
        deleted = (await db.execute(select(AuditLog.meta).where(AuditLog.action == "settings.extra_answers_deleted"))).scalars().all()
    assert deleted == [{"answers": 4}]                                            # badge twice, a shift and a relocate

    # The removed field is added again under the same key: it starts empty for everyone.
    assert (await admin.patch("/api/settings", json={"profile_extra_fields": [*changed, fields[1]]})).status_code == 200
    assert "badge" not in (await member.get(own)).json()["extra"] and "badge" not in (await other.get(theirs)).json()["extra"]
    # Saving the same fields again changes nothing and records nothing.
    assert (await admin.patch("/api/settings", json={"profile_extra_fields": [*changed, fields[1]], "watermark_enabled": True})).status_code == 200
    assert (await member.get(own)).json()["extra"] == {"notice": 4}
    async with world.sessions() as db:
        assert len((await db.execute(select(AuditLog).where(AuditLog.action == "settings.extra_answers_deleted"))).scalars().all()) == 1


async def test_a_resume_carries_no_fixed_sentence_about_ai_services(world):
    """Which outside AI services receive resume text is said in one place, from the configuration (settings.ai_providers)."""
    member = await world.as_member("a1")
    resume = await member.get(f"/api/members/{world.ids['member_a1']}/resume")
    assert resume.status_code == 200 and "ai_processing_notice" not in resume.json()
    assert (await member.get("/api/auth/me")).json()["settings"]["ai_providers"] == []       # the test server has no AI keys


async def test_team_lead_is_told_which_accounts_only_an_admin_can_reactivate(world):
    admin, lead = await world.as_admin(), await world.as_tl("alpha")
    a1, a2 = world.ids["member_a1"], world.ids["member_a2"]
    assert (await lead.patch(f"/api/users/{a1}", json={"is_active": False})).json()["deactivated_by_admin"] is False
    assert (await admin.patch(f"/api/users/{a2}", json={"is_active": False})).json()["deactivated_by_admin"] is True
    listed = {u["name"]: u["deactivated_by_admin"] for u in (await lead.get("/api/users", params={"status": "deactivated"})).json()["items"]}
    assert listed == {"Member A1": False, "Member A2": True}


async def test_unassigned_only_filters_before_paging_and_first_view_is_recorded_once(world):
    import asyncio

    from sqlalchemy import func

    from app.portal.models import ApplicationEvent
    lead, member = await world.as_tl("alpha"), await world.as_member("a1")
    started = await lead.post("/api/searches", json={"member_ids": [str(world.ids["member_a1"])], "mode": "review", "target_count": 6, "deep_search": False})
    run_id = started.json()["search"]["id"]
    await world.queue.drain()
    rows = (await lead.get(f"/api/searches/{run_id}/results")).json()
    assert rows["total"] == 6
    picked = [r["job"]["id"] for r in rows["items"][:2]]
    assert (await lead.post("/api/assignments/bulk", json={"job_ids": picked, "member_ids": [str(world.ids["member_a1"])], "search_run_id": run_id})).status_code == 201
    left = (await lead.get(f"/api/searches/{run_id}/results", params={"unassigned_only": True, "page_size": 3})).json()
    assert left["total"] == 4 and len(left["items"]) == 3 and not {r["job"]["id"] for r in left["items"]} & set(picked)
    second_page = (await lead.get(f"/api/searches/{run_id}/results", params={"unassigned_only": True, "page_size": 3, "page": 2})).json()
    assert len(second_page["items"]) == 1
    assignment = (await member.get("/api/assignments")).json()["items"][0]["id"]
    answers = await asyncio.gather(*[member.get(f"/api/assignments/{assignment}") for _ in range(4)])       # a page and its prefetches
    assert {a.status_code for a in answers} == {200}
    async with world.sessions() as db:
        viewed = (await db.execute(select(func.count()).select_from(ApplicationEvent).where(ApplicationEvent.event_type == "viewed"))).scalar_one()
    assert viewed == 1


async def test_open_job_problem_is_a_page_for_a_browser_tab_and_json_for_scripts(world):
    member = await world.as_member("a1")
    missing = "/api/assignments/00000000-0000-0000-0000-000000000000/open-job"
    page = await member.get(missing, headers={"accept": "text/html,application/xhtml+xml"}, follow_redirects=False)
    assert page.status_code == 403 and page.headers["content-type"].startswith("text/html")
    assert "<h1>This job cannot be opened from here</h1>" in page.text and "<script" not in page.text
    data = await member.get(missing, headers={"accept": "application/json"}, follow_redirects=False)
    assert data.status_code == 403 and data.json()["error"]
    signed_out = await (await world.client()).get(missing, headers={"accept": "text/html"}, follow_redirects=False)
    assert signed_out.status_code == 401 and 'href="/login"' in signed_out.text


async def test_every_role_is_told_which_outside_ai_services_receive_resume_text(world):
    member = await world.as_member("a1")
    assert (await member.get("/api/auth/me")).json()["settings"]["ai_providers"] == []            # none configured in tests
    world.settings.openai_api_key, world.settings.anthropic_api_key = "test-key-not-real", ""
    assert (await member.get("/api/auth/me")).json()["settings"]["ai_providers"] == ["OpenAI"]
    world.settings.anthropic_api_key = "test-key-not-real"
    told = (await member.get("/api/auth/me")).json()
    assert told["settings"]["ai_providers"] == ["OpenAI", "Anthropic"] and "test-key-not-real" not in str(told)      # names only, never a key
