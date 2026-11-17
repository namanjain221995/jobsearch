"""Acceptance scenarios 1 to 7 of the brief, driven through the real web app in a browser.

Every step a person would take is taken in the page (sign in, dialogs, buttons). The API is only asked directly to
read numbers the test compares the screen with, and to probe addresses a person would have to type by hand.

Needs the demo stack: the demo API with seeded data and a production build of the web app (tests/e2e/run.sh starts
both). Skipped unless E2E_BASE_URL is set. The scenarios build on each other and run in the order of this file."""
import os
import re
import tempfile
import time

import pytest

BASE = os.environ.get("E2E_BASE_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not BASE, reason="set E2E_BASE_URL, or run tests/e2e/run.sh")

DEMO_PASSWORD = "Demo-Password-42!"
NEW_PASSWORD = "Scenario-Pass-77!"
STAMP = str(int(time.time()))[-6:]
STATE: dict = {}                     # what one scenario leaves for the next
RESUME = ("Jordan Example\nMachine Learning Engineer with 3 years of experience.\n"
          "Skills: Python, PyTorch, TensorFlow, scikit-learn, SQL, AWS, Docker, Kubernetes, MLOps, NLP.\n"
          "Built and deployed machine learning models, model serving and data pipelines. B.S. Computer Science.\n") * 3


# ---------------------------------------------------------------------------------------------- browser plumbing
@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        # Nothing may reach a real site: every host except this machine fails to resolve (employer pages included).
        chromium = pw.chromium.launch(args=["--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost"])
        yield chromium
        chromium.close()


class Session:
    """One person's browser window. Collects console errors, page errors and failed API calls."""

    def __init__(self, browser, problems: list, width: int = 1280, height: int = 900):
        self.problems, self.allowed = problems, []
        self.context = browser.new_context(viewport={"width": width, "height": height})
        self.context.on("page", self._watch)
        self.page = self.context.new_page()

    def _note(self, kind: str, text: str) -> None:
        if not any(part in text for part in self.allowed):
            self.problems.append(f"{kind}: {text[:300]}")

    def _watch(self, page) -> None:
        page.on("console", lambda m: self._note(f"console.{m.type}", m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: self._note("pageerror", str(e)))
        page.on("response", lambda r: self._note(f"http {r.status}", f"{r.request.method} {r.url}") if r.status >= 400 and "/api/" in r.url else None)

    def login(self, email: str, password: str, home: str):
        page = self.page
        page.goto(BASE + "/login")
        page.get_by_label("E-mail").fill(email)
        page.get_by_label("Password", exact=True).fill(password)
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url(re.compile(re.escape(home) + "$"), timeout=20000)      # each role lands on its own dashboard
        page.wait_for_load_state("networkidle")
        return page

    def api(self, path: str) -> tuple[int, dict]:
        """GET as the signed-in person, from inside the page (same cookies, same origin)."""
        result = self.page.evaluate(
            "async (p) => { const r = await fetch(p, {credentials: 'same-origin'}); let body = {}; "
            "try { body = await r.json() } catch (e) {} return [r.status, body] }", path)
        return result[0], result[1]

    def kpi(self, label: str) -> int:
        cell = self.page.locator(f'[data-slot="kpi"][data-label="{label}"]').filter(visible=True).first
        cell.wait_for(timeout=15000)
        return int(cell.locator('[data-slot="kpi-value"]').inner_text().strip().replace(",", ""))

    def dashboard_kpi(self, path: str, label: str) -> int:
        self.page.goto(BASE + path)
        self.page.wait_for_load_state("networkidle")
        return self.kpi(label)

    def toast(self, text: str) -> None:
        self.page.locator("[data-sonner-toast]").filter(has_text=text).first.wait_for(timeout=15000)

    def close(self) -> None:
        self.context.close()


@pytest.fixture
def problems():
    found: list[str] = []
    yield found
    assert found == [], "console errors or failed API requests during the scenario:\n" + "\n".join(found)


def need(*keys: str) -> None:
    missing = [k for k in keys if k not in STATE]
    if missing:
        pytest.skip(f"an earlier scenario did not finish (missing: {', '.join(missing)})")


def accept_invitation(browser, problems, link: str, name: str, home: str, email: str) -> None:
    """The invited person opens the link in a browser that is not signed in, sets a password and signs in."""
    guest = Session(browser, problems)
    page = guest.page
    page.goto(re.sub(r"^https?://[^/]+", BASE, link))
    page.get_by_role("heading", level=1, name=f"Welcome, {name}").wait_for(timeout=15000)
    page.get_by_label("Password", exact=True).fill(NEW_PASSWORD)
    page.get_by_label("Repeat the password").fill(NEW_PASSWORD)
    page.get_by_role("button", name="Create account").click()
    page.wait_for_url(re.compile(r"/login$"), timeout=15000)
    page.get_by_label("E-mail").fill(email)
    page.get_by_label("Password", exact=True).fill(NEW_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(re.compile(re.escape(home) + "$"), timeout=20000)
    guest.close()


def invitation_link(page) -> str:
    created = page.get_by_role("dialog", name="Invitation created")
    created.wait_for(timeout=15000)
    link = created.get_by_label("Invitation link").input_value()
    assert "/invite/accept?token=" in link
    assert created.get_by_text("No e-mail was sent").is_visible()          # the demo stack has no mail server, and says so
    created.get_by_role("button", name="Done").click()
    created.wait_for(state="hidden", timeout=5000)
    return link


# ---------------------------------------------------------------------------------------------- the scenarios
def test_scenario_1_admin_creates_teams_and_their_team_leads(browser, problems):
    admin = Session(browser, problems)
    page = admin.login("admin@example.com", DEMO_PASSWORD, "/admin/dashboard")
    leads = {}
    for letter in ("Gamma", "Delta"):
        team, lead_name, lead_email = f"Team {letter} {STAMP}", f"Lead {letter} {STAMP}", f"lead.{letter.lower()}.{STAMP}@example.com"
        page.goto(BASE + "/admin/teams")
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="Create Team", exact=True).first.click()
        dialog = page.get_by_role("dialog", name="Create Team", exact=True)
        dialog.get_by_label("Team name").fill(team)
        dialog.get_by_role("button", name="Create Team", exact=True).click()
        admin.toast(f"Team {team} created")
        page.get_by_role("link", name=team).first.wait_for(timeout=15000)
        page.get_by_role("button", name=f"Actions for {team}").click()
        page.get_by_role("menuitem", name="Create Team Lead").click()
        dialog = page.get_by_role("dialog", name=f"Create Team Lead for {team}")
        dialog.get_by_label("Full name").fill(lead_name)
        dialog.get_by_label("E-mail").fill(lead_email)
        dialog.get_by_role("button", name="Create Invitation").click()
        leads[letter] = (team, lead_name, lead_email, invitation_link(page))
    # Both invitations are secure one-time links; each Team Lead accepts and creates a password.
    assert leads["Gamma"][3] != leads["Delta"][3]
    for team, lead_name, lead_email, link in leads.values():
        accept_invitation(browser, problems, link, lead_name, "/tl/dashboard", lead_email)
    # The Admin sees both teams, each with its (now active) Team Lead.
    page.goto(BASE + "/admin/teams")
    page.wait_for_load_state("networkidle")
    for team, lead_name, _, _ in leads.values():
        row = page.locator("tr, li, article").filter(has_text=team).filter(visible=True).first
        row.wait_for(timeout=15000)
        assert lead_name in row.inner_text()
    status, teams = admin.api("/api/teams")
    listed = {t["name"]: t for t in teams}
    assert status == 200 and all(listed[team]["team_lead"]["name"] == lead_name for team, lead_name, _, _ in leads.values())
    status, users = admin.api(f"/api/users?role=TEAM_LEAD&q={STAMP}")
    assert {u["email"]: u["status"] for u in users["items"]} == {lead_email: "active" for _, _, lead_email, _ in leads.values()}
    STATE.update(team=leads["Gamma"][0], team_id=listed[leads["Gamma"][0]]["id"], lead_email=leads["Gamma"][2], lead_name=leads["Gamma"][1],
                 other_team_id=listed[leads["Delta"][0]]["id"])
    admin.close()


def test_scenario_2_team_lead_creates_a_member_who_sees_only_their_own_dashboard(browser, problems):
    need("lead_email")
    lead = Session(browser, problems)
    page = lead.login(STATE["lead_email"], NEW_PASSWORD, "/tl/dashboard")
    name, email = f"Member Gamma {STAMP}", f"member.gamma.{STAMP}@example.com"
    page.goto(BASE + "/tl/members")
    page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="Add Member").first.click()
    dialog = page.get_by_role("dialog", name="Add Member")
    dialog.get_by_label("Full name").fill(name)
    dialog.get_by_label("E-mail").fill(email)
    dialog.get_by_role("button", name="Create Invitation").click()
    link = invitation_link(page)
    page.get_by_role("link", name=name).first.wait_for(timeout=15000)                 # listed in the team at once (invited)
    accept_invitation(browser, problems, link, name, "/member/dashboard", email)

    member = Session(browser, problems)
    mpage = member.login(email, NEW_PASSWORD, "/member/dashboard")
    status, me = member.api("/api/auth/me")
    assert status == 200 and me["role"] == "MEMBER" and me["team"]["name"] == STATE["team"]       # joined that Team Lead's team
    for path in ("/admin/dashboard", "/tl/members", "/admin/teams"):                              # only their own area opens
        mpage.goto(BASE + path)
        mpage.wait_for_url(re.compile(r"/member/dashboard$"), timeout=15000)
    member.allowed += ["403", "/api/users", "/api/teams", "/api/analytics"]                       # the refusals below are the point
    assert member.api("/api/users")[0] == 403 and member.api("/api/teams")[0] == 403 and member.api("/api/analytics/admin")[0] == 403
    assert member.api(f"/api/teams/{STATE['team_id']}/members")[0] == 403
    status, own = member.api("/api/assignments")
    assert status == 200 and own["total"] == 0
    STATE.update(member_email=email, member_name=name, member_id=me["id"])
    member.close()
    lead.close()


def test_scenario_3_team_lead_search_assigns_verified_jobs_and_shows_the_real_count(browser, problems):
    need("member_email")
    # "Resume is available": the member uploads one on their profile page.
    member = Session(browser, problems)
    mpage = member.login(STATE["member_email"], NEW_PASSWORD, "/member/dashboard")
    mpage.goto(BASE + "/member/profile")
    mpage.wait_for_load_state("networkidle")
    path = os.path.join(tempfile.mkdtemp(prefix="jobfinder-acceptance-"), "resume.txt")
    with open(path, "w") as handle:
        handle.write(RESUME)
    mpage.locator('input[type="file"]').first.set_input_files(path)
    for _ in range(60):
        if member.api(f"/api/users/{STATE['member_id']}")[1].get("has_resume"):
            break
        mpage.wait_for_timeout(250)
    assert member.api(f"/api/users/{STATE['member_id']}")[1]["has_resume"] is True

    lead = Session(browser, problems)
    page = lead.login(STATE["lead_email"], NEW_PASSWORD, "/tl/dashboard")
    page.goto(BASE + "/tl/search")
    page.wait_for_load_state("networkidle")
    group = page.get_by_role("group", name="Members")
    group.wait_for(timeout=15000)
    assert "Maya Chen" not in group.inner_text() and "Priya Nair" not in group.inner_text()       # only this Team Lead's team is offered
    group.get_by_role("checkbox", name=re.compile(re.escape(STATE["member_name"]))).check()
    titles = page.get_by_label(re.compile("^Job titles"))
    titles.fill("Machine Learning Engineer")
    titles.press("Enter")
    page.get_by_label("Years of experience").fill("3")
    page.get_by_label("Results per member").fill("50")
    page.get_by_role("radio", name=re.compile("Find & Assign Matching Jobs")).check()
    page.get_by_role("button", name="Start Search").click()
    window = page.get_by_role("dialog", name="AI Job Search Started")                # the progress popup appears at once
    window.wait_for(timeout=15000)
    assert window.get_by_text(STATE["member_name"]).first.is_visible()
    for label in ("Requested", "Discovered", "Verified", "Duplicates removed", "Matched", "Assigned"):
        assert window.locator(f'[data-slot="run-counter"][data-label="{label}"]').is_visible(), label
    window.locator('[data-slot="tag"][data-status="completed"]').first.wait_for(timeout=120000)

    def counter(label: str) -> int:
        return int(window.locator(f'[data-slot="run-counter"][data-label="{label}"] [data-slot="run-counter-value"]').first.inner_text().replace(",", ""))

    requested, verified, matched, assigned = counter("Requested"), counter("Verified"), counter("Matched"), counter("Assigned")
    # Fewer than 50 postings qualify in the demo data: the real number is shown, nothing is made up to reach 50.
    assert requested == 50 and 0 < assigned <= matched < 50 and verified >= matched, (requested, verified, matched, assigned)
    status, run = lead.api("/api/searches?page_size=1")
    newest = run["items"][0]
    assert newest["status"] == "completed" and newest["assigned_count"] == assigned and newest["matched_count"] == matched
    lead.close()

    # The member sees exactly those jobs, without signing in again.
    mpage.goto(BASE + "/member/jobs")
    mpage.locator('[data-slot="job-card"]').filter(visible=True).first.wait_for(timeout=15000)
    status, own = member.api("/api/assignments?page_size=200")
    assert own["total"] == assigned
    found = [item["job"]["title"] for item in own["items"]]
    assert any("Machine Learning Engineer" in title for title in found)
    assert not any("Account Executive" in title for title in found), found           # the unrelated posting in the demo data is not assigned
    assert all(item["match_score"] >= newest["min_match_score"] for item in own["items"])         # nothing below the threshold to fill the quota
    assert all(item["status"] == "ASSIGNED" and item["applied_at"] is None for item in own["items"])
    STATE.update(assigned=assigned)
    member.close()


def test_scenario_4_member_applies_and_every_dashboard_updates(browser, problems):
    need("assigned")
    lead, admin, member = Session(browser, problems), Session(browser, problems), Session(browser, problems)
    lead.login(STATE["lead_email"], NEW_PASSWORD, "/tl/dashboard")
    admin.login("admin@example.com", DEMO_PASSWORD, "/admin/dashboard")
    page = member.login(STATE["member_email"], NEW_PASSWORD, "/member/dashboard")
    before = {"member": member.kpi("Applied"), "lead": lead.kpi("Applied"), "admin": admin.kpi("Applied")}
    assert before["member"] == 0 and before["lead"] == 0

    page.goto(BASE + "/member/jobs")
    card = page.locator('[data-slot="job-card"]').filter(visible=True).first
    card.wait_for(timeout=15000)
    card.get_by_role("link").first.click()
    page.wait_for_url(re.compile(r"/member/jobs/[0-9a-f-]{36}$"), timeout=15000)
    assignment_id = page.url.rsplit("/", 1)[1]
    description = page.locator('[data-slot="job-description"]')
    description.wait_for(timeout=15000)
    assert len(description.inner_text()) > 80                                         # the complete available description
    html = page.content()
    assert "greenhouse.io" not in html and "lever.co" not in html                     # no raw posting address for the member

    seen: dict[str, int] = {}
    member.context.on("response", lambda r: seen.__setitem__(r.url, r.status))
    with member.context.expect_page() as opened:                                      # "Apply on Company Website": a new tab
        page.get_by_role("button", name="Open Job / Apply").click()
    try:
        opened.value.wait_for_load_state(timeout=8000)
    except Exception:
        pass                                                                          # the employer's site is not reachable from the test
    opened.value.close()
    redirect = [status for url, status in seen.items() if f"/api/assignments/{assignment_id}/open-job" in url]
    assert redirect == [303]                                                          # audited redirect to the stored posting address
    page.get_by_text("Job posting opened by").first.wait_for(timeout=15000)
    # Opening the job is not applying: nothing moved yet.
    assert page.get_by_role("button", name="Mark as Applied").is_visible()
    status, detail = member.api(f"/api/assignments/{assignment_id}")
    assert detail["applied_at"] is None and detail["link_opens"] == 1 and detail["status"] == "VIEWED"

    page.get_by_role("button", name="Mark as Applied").click()                       # after applying on the employer's site
    dialog = page.get_by_role("dialog", name="Mark as Applied")
    dialog.wait_for(timeout=5000)
    dialog.get_by_role("button", name="Mark as Applied").click()
    member.toast("Marked as applied")
    page.locator('[data-slot="status-badge"][data-status="APPLIED"]').filter(visible=True).first.wait_for(timeout=10000)

    assert member.dashboard_kpi("/member/dashboard", "Applied") == before["member"] + 1
    assert member.kpi("Not Applied") == STATE["assigned"] - 1
    assert lead.dashboard_kpi("/tl/dashboard", "Applied") == before["lead"] + 1       # the Team Lead's analytics follow
    assert admin.dashboard_kpi("/admin/dashboard", "Applied") == before["admin"] + 1  # and the Admin's
    for session in (lead, admin, member):
        session.close()


def test_scenario_5_admin_delegates_ten_jobs_and_team_lead_beta_distributes_them(browser, problems):
    admin = Session(browser, problems)
    page = admin.login("admin@example.com", DEMO_PASSWORD, "/admin/dashboard")
    company = f"Delegation Co {STAMP}"
    titles = [f"Scenario Five Role {n:02d}" for n in range(1, 11)]
    page.goto(BASE + "/admin/jobs")
    page.wait_for_load_state("networkidle")
    for n, title in enumerate(titles):                                                # the Admin adds ten jobs by hand
        page.get_by_role("button", name="Add Job").click()
        dialog = page.get_by_role("dialog", name="Add Job")
        dialog.get_by_label("Job title").fill(title)
        dialog.get_by_label("Company", exact=True).fill(company)
        dialog.get_by_label(re.compile("^City")).fill("Austin")
        dialog.get_by_label(re.compile("^State")).fill("TX")
        dialog.get_by_label(re.compile("^Posting address")).fill(f"https://careers.example.org/s5/{STAMP}/{n}")
        dialog.get_by_label(re.compile("^Description")).fill("About the Role\nBuild and run services.\n\nResponsibilities\n- Write code\n- Review code")
        dialog.get_by_role("button", name="Save Job").click()
        dialog.wait_for(state="hidden", timeout=15000)
    status, jobs = admin.api(f"/api/jobs?q={company.replace(' ', '%20')}&page_size=50")
    assert status == 200 and jobs["total"] == 10
    for title in titles:
        page.get_by_role("checkbox", name=f"Select {title} at {company}").check()
    page.get_by_role("region", name="Selected jobs").get_by_role("button", name="Assign to Team Lead").click()
    dialog = page.get_by_role("dialog", name="Assign to Team Lead")
    dialog.get_by_role("combobox", name="Team").click()
    page.get_by_role("option", name="Team Beta").click()
    dialog.get_by_role("button", name="Send to Team Queue").click()
    admin.toast("sent to Team Beta's queue")

    lead = Session(browser, problems)
    lpage = lead.login("lead.beta@example.com", DEMO_PASSWORD, "/tl/dashboard")
    status, people = lead.api("/api/users?role=MEMBER&page_size=50")
    ids = {u["name"]: u["id"] for u in people["items"]}
    before = {name: lead.api(f"/api/assignments?member_id={ids[name]}&page_size=1")[1]["total"] for name in ("Priya Nair", "Quinn Lopez")}
    for name, share in (("Priya Nair", titles[:5]), ("Quinn Lopez", titles[5:])):      # five each
        lpage.goto(BASE + "/tl/jobs")
        lpage.wait_for_load_state("networkidle")
        for title in share:
            box = lpage.get_by_role("checkbox", name=f"Select {title} at {company}")
            box.wait_for(timeout=15000)                                               # the delegated jobs wait in this team's queue
            box.check()
        lpage.get_by_role("button", name=re.compile(r"Distribute to Members \(5\)")).click()
        dialog = lpage.get_by_role("dialog", name="Assign to Members")
        members = dialog.get_by_role("group", name="Members")
        members.get_by_role("checkbox", name=re.compile(name)).wait_for(timeout=15000)
        assert "Maya Chen" not in members.inner_text()                                # only this team's members can receive them
        members.get_by_role("checkbox", name=re.compile(name)).check()
        dialog.get_by_role("button", name=re.compile("Review 5 Assignment")).click()
        lpage.get_by_role("button", name=re.compile("^Assign 5 Job")).click()
        lead.toast("Jobs assigned")
    lpage.goto(BASE + "/tl/jobs")
    lpage.wait_for_load_state("networkidle")
    assert lpage.get_by_role("checkbox", name=re.compile(f"at {re.escape(company)}$")).count() == 0      # the queue is empty again

    for name, email in (("Priya Nair", "priya@example.com"), ("Quinn Lopez", "quinn@example.com")):
        member = Session(browser, problems)
        member.login(email, DEMO_PASSWORD, "/member/dashboard")
        assert member.kpi("Total Assigned") == before[name] + 5                      # each member's dashboard shows their five
        mine = member.api(f"/api/assignments?q={company.replace(' ', '%20')}&page_size=50")[1]
        assert mine["total"] == 5 and all(item["job"]["company"] == company for item in mine["items"])
        member.close()
    # The Admin can trace every one of them: who received which job, and that Team Beta's lead assigned it.
    status, traced = admin.api(f"/api/assignments?company={company.replace(' ', '%20')}&page_size=50")
    received = sorted((item["job"]["title"], item["member"]["name"], item["assigned_by"]["name"], item["team"]["name"]) for item in traced["items"])
    assert traced["total"] == 10
    assert received == sorted([(t, "Priya Nair", "Ben Beta", "Team Beta") for t in titles[:5]] + [(t, "Quinn Lopez", "Ben Beta", "Team Beta") for t in titles[5:]])
    STATE.update(beta_member_id=ids["Priya Nair"], beta_team_id=traced["items"][0]["team"]["id"], beta_assignment_id=traced["items"][0]["id"])
    lead.close()
    admin.close()


def test_scenario_6_a_team_lead_cannot_reach_another_team_by_changing_the_address(browser, problems):
    need("beta_member_id")
    lead = Session(browser, problems)
    page = lead.login("lead.alpha@example.com", DEMO_PASSWORD, "/tl/dashboard")
    lead.allowed += ["403", "404", STATE["beta_member_id"], STATE["beta_team_id"], STATE["beta_assignment_id"]]      # refusals are expected here
    member, team, assignment = STATE["beta_member_id"], STATE["beta_team_id"], STATE["beta_assignment_id"]
    page.goto(BASE + f"/tl/members/{member}")                                         # Member B1's record, by typing the address
    page.wait_for_load_state("networkidle")
    text = page.locator("body").inner_text()
    assert "Priya" not in text and "priya@example.com" not in text                   # nothing of the other team is shown
    assert re.search(r"do not have access|not found|cannot be shown|no access", text, re.I), text[:300]
    refused = {path: lead.api(path)[0] for path in (
        f"/api/users/{member}", f"/api/users/{member}/dashboard", f"/api/users/{member}/profile", f"/api/users/{member}/activity",
        f"/api/members/{member}/resume", f"/api/teams/{team}", f"/api/teams/{team}/members", f"/api/teams/{team}/analytics",
        f"/api/teams/{team}/queue", f"/api/analytics/member/{member}", f"/api/assignments/{assignment}")}
    assert set(refused.values()) == {403}, refused                                    # the backend refuses each one
    status, filtered = lead.api(f"/api/assignments?member_id={member}&team_id={team}&page_size=50")
    assert status == 200 and filtered["total"] == 0                                   # a filter cannot widen the scope either
    status, everyone = lead.api("/api/users?page_size=200")
    assert all(u["team"]["name"] == "Team Alpha" for u in everyone["items"])
    lead.close()


def test_scenario_7_a_search_continues_when_the_popup_is_closed_and_the_person_moves_on(browser, problems):
    lead = Session(browser, problems)
    page = lead.login("lead.alpha@example.com", DEMO_PASSWORD, "/tl/dashboard")
    status, people = lead.api("/api/users?role=MEMBER&page_size=50")
    noah = next(u for u in people["items"] if u["email"] == "noah@example.com")
    before = lead.api(f"/api/assignments?member_id={noah['id']}&page_size=1")[1]["total"]
    known = {r["id"] for r in lead.api("/api/searches?page_size=50")[1]["items"]}
    page.goto(BASE + "/tl/search")
    page.wait_for_load_state("networkidle")
    group = page.get_by_role("group", name="Members")
    group.wait_for(timeout=15000)
    group.get_by_role("checkbox", name=re.compile("Noah Patel")).check()
    titles = page.get_by_label(re.compile("^Job titles"))
    titles.fill("LLM Engineer")
    titles.press("Enter")
    page.get_by_label("Results per member").fill("4")
    page.get_by_role("button", name="Start Search").click()
    window = page.get_by_role("dialog", name="AI Job Search Started")
    window.wait_for(timeout=15000)
    window.get_by_role("button", name="Minimize", exact=True).last.click()            # the Team Lead puts the popup away at once ...
    page.locator('[data-slot="search-indicator"]').wait_for(timeout=5000)
    page.goto(BASE + "/tl/dashboard")                                                 # ... and goes to the dashboard
    page.wait_for_load_state("networkidle")
    page.goto(BASE + "/tl/searches")                                                  # later: Search History
    page.wait_for_load_state("networkidle")
    status, runs = lead.api("/api/searches?page_size=50")
    new = [r for r in runs["items"] if r["id"] not in known]
    assert len(new) == 1 and new[0]["status"] in ("queued", "running", "completed")   # still processing, or finished
    run_id = new[0]["id"]
    page.locator(f'a[href="/tl/searches/{run_id}"]').filter(visible=True).first.wait_for(timeout=15000)
    for _ in range(240):
        status, run = lead.api(f"/api/searches/{run_id}")
        if run["status"] not in ("queued", "running"):
            break
        page.wait_for_timeout(500)
    assert run["status"] == "completed" and run["matched_count"] > 0
    page.goto(BASE + f"/tl/searches/{run_id}")
    page.get_by_role("heading", name="Results", exact=True).wait_for(timeout=20000)
    rows = page.get_by_role("checkbox", name=re.compile("^Select .* for Noah Patel"))
    rows.first.wait_for(timeout=15000)
    status, results = lead.api(f"/api/searches/{run_id}/results?page_size=50")
    assert results["total"] == run["matched_count"] and rows.count() == results["total"]          # the results were kept
    after = lead.api(f"/api/assignments?member_id={noah['id']}&page_size=1")[1]["total"]
    assert after == before + run["assigned_count"]                                    # and so were the assignments
    lead.close()
