"""Acceptance scenarios 1 to 7 of the brief, and scenario 8 (a candidate brought in from Salesforce), driven through the
real web app in a browser.

Members are recruiters (accounts); candidates are the people who need a job (records, never accounts). A Team Lead
creates candidates with their resume, assigns each to one member, runs searches for candidates; the responsible
member works the candidate's jobs.

Every step a person would take is taken in the page (sign in, dialogs, buttons). The API is only asked directly to
read numbers the test compares the screen with, and to probe addresses a person would have to type by hand.

Needs the demo stack: the demo API with seeded data and a production build of the web app (tests/e2e/run.sh starts
both). Skipped unless E2E_BASE_URL is set. The scenarios build on each other and run in the order of this file."""
import os
import re
import tempfile
import time
from urllib.parse import urlparse

import pytest

BASE = os.environ.get("E2E_BASE_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not BASE, reason="set E2E_BASE_URL, or run tests/e2e/run.sh")

DEMO_PASSWORD = "Demo-Password-42!"
NEW_PASSWORD = "Scenario-Pass-77!"
STAMP = str(int(time.time()))[-6:]
# The Find Jobs candidate list has two groups (the demo stack has a fake Salesforce): the portal's own candidates are here.
PICKER = "Candidates in the portal"
STATE: dict = {}                     # what one scenario leaves for the next
RESUME = ("Casey Example\nMachine Learning Engineer with 3 years of experience.\n"
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
        line = f"{kind}: {text[:300]}"
        if not any(part in line for part in self.allowed):
            self.problems.append(line)

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
    for path in ("/admin/dashboard", "/tl/members", "/admin/teams", "/tl/candidates", "/admin/candidates"):   # only their own area opens
        mpage.goto(BASE + path)
        mpage.wait_for_url(re.compile(r"/member/dashboard$"), timeout=15000)
    member.allowed += ["http 403: GET", "status of 403"]                       # the refusals below are the point
    assert member.api("/api/users")[0] == 403 and member.api("/api/teams")[0] == 403 and member.api("/api/analytics/admin")[0] == 403
    assert member.api(f"/api/teams/{STATE['team_id']}/members")[0] == 403
    status, own = member.api("/api/assignments")
    assert status == 200 and own["total"] == 0
    # A member is a recruiter: no candidate is theirs until the Team Lead assigns one, and there is no profile or resume of their own.
    status, mine = member.api("/api/candidates")
    assert status == 200 and mine["total"] == 0
    mpage.goto(BASE + "/member/candidates")
    mpage.get_by_text("Your Team Lead has not assigned a candidate to you yet").first.wait_for(timeout=15000)
    mpage.goto(BASE + "/member/profile")                                              # the old profile page only leads to the account settings
    mpage.wait_for_url(re.compile(r"/member/settings$"), timeout=15000)
    assert mpage.locator('input[type="file"]').count() == 0
    STATE.update(member_email=email, member_name=name, member_id=me["id"])
    member.close()
    lead.close()


SALESFORCE_OFF = {"configured": False, "connected": None, "message": "", "checked_at": None, "api_version": "v68.0", "team_lead_scope": "all"}


def without_salesforce(session) -> None:
    """Scenarios 1 to 7 are those of the brief, in which a Team Lead types a candidate in and uploads the resume. A portal
    that is connected to Salesforce (the demo stack has a fake one) takes candidates from there only, which is scenario 8.
    This person's browser is told that Salesforce is not configured, so the portal offers "Add Candidate" as it does then."""
    session.context.route("**/api/salesforce/status", lambda route: route.fulfill(status=200, json=SALESFORCE_OFF))


def start_search(session, candidate: str, titles: str, per_candidate: str, mode: str, picker: str = PICKER):
    """Team Lead: Find Jobs for one candidate of the own team. Returns the progress window."""
    page = session.page
    page.goto(BASE + "/tl/search")
    page.wait_for_load_state("networkidle")
    group = page.get_by_role("group", name=picker, exact=True)
    group.get_by_role("checkbox").first.wait_for(timeout=15000)
    group.get_by_role("checkbox", name=re.compile(re.escape(candidate))).check()
    page.get_by_role("region", name="Selected candidates").get_by_text(candidate).first.wait_for(timeout=5000)
    field = page.get_by_label(re.compile("^Job titles"))
    field.fill(titles)
    field.press("Enter")
    page.get_by_label("Results per candidate").fill(per_candidate)
    page.get_by_role("radio", name=re.compile(mode)).check()
    page.get_by_role("button", name="Start Search").click()
    window = page.get_by_role("dialog", name="AI Job Search Started")                # the progress popup appears at once
    window.wait_for(timeout=15000)
    return window


def test_scenario_3_team_lead_adds_a_candidate_with_resume_and_the_search_assigns_verified_jobs(browser, problems):
    need("member_email")
    lead = Session(browser, problems)
    without_salesforce(lead)
    page = lead.login(STATE["lead_email"], NEW_PASSWORD, "/tl/dashboard")
    candidate, contact = f"Casey Candidate {STAMP}", f"casey.{STAMP}@example.com"
    # "Resume is available": the Team Lead creates the candidate, uploads the resume and chooses the responsible member in one dialog.
    path = os.path.join(tempfile.mkdtemp(prefix="jobfinder-acceptance-"), "casey-resume.txt")
    with open(path, "w") as handle:
        handle.write(RESUME)
    page.goto(BASE + "/tl/candidates")
    page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="Add Candidate", exact=True).first.click()
    dialog = page.get_by_role("dialog", name="Add Candidate")
    dialog.get_by_label("Full name").fill(candidate)
    dialog.get_by_label(re.compile("^E-mail")).fill(contact)
    dialog.get_by_label(re.compile("^Phone")).fill("+1 555 0199")
    dialog.get_by_label(re.compile("^Notes")).fill("Available from next month.")
    dialog.get_by_role("combobox", name=re.compile("Responsible member")).click()
    options = page.get_by_role("option")
    page.get_by_role("option", name=STATE["member_name"]).wait_for(timeout=15000)
    assert sorted(options.all_inner_texts()) == sorted(["Nobody yet (unassigned)", STATE["member_name"]])   # only this team's members
    page.get_by_role("option", name=STATE["member_name"]).click()
    dialog.locator('input[type="file"]').set_input_files(path)
    dialog.get_by_text("casey-resume.txt").wait_for(timeout=5000)
    dialog.get_by_role("button", name="Add Candidate").click()
    lead.toast(f"{candidate} added")
    page.wait_for_url(re.compile(r"/tl/candidates/[0-9a-f-]{36}\?tab=profile$"), timeout=15000)   # the new candidate's own page
    candidate_id = page.url.rsplit("/", 1)[1].split("?")[0]
    page.get_by_role("heading", level=1, name=candidate).wait_for(timeout=15000)
    status, record = lead.api(f"/api/candidates/{candidate_id}")
    assert status == 200 and record["has_resume"] is True and record["resume_has_file"] is True
    assert record["assigned_member"]["id"] == STATE["member_id"] and record["team"]["name"] == STATE["team"]
    assert record["email"] == contact and record["is_active"] is True
    status, resume = lead.api(f"/api/candidates/{candidate_id}/resume")
    assert status == 200 and resume["filename"] == "casey-resume.txt" and "Machine Learning Engineer" in resume["text"]
    # A candidate is a record, never an account: nobody can sign in with the candidate's address, and no user was created.
    status, users = lead.api(f"/api/users?q={STAMP}&page_size=50")
    assert contact not in [u["email"] for u in users["items"]] and candidate not in [u["name"] for u in users["items"]]

    # The member finds the candidate under My Candidates without signing in again, with resume and contact details.
    member = Session(browser, problems)
    mpage = member.login(STATE["member_email"], NEW_PASSWORD, "/member/dashboard")
    mpage.goto(BASE + "/member/candidates")
    card = mpage.locator('[data-slot="candidate-card"]').filter(has_text=candidate)
    card.wait_for(timeout=15000)
    assert mpage.locator('[data-slot="candidate-card"]').count() == 1 and contact in card.inner_text()
    status, mine = member.api("/api/candidates")
    assert status == 200 and [c["id"] for c in mine["items"]] == [candidate_id]
    assert member.api(f"/api/candidates/{candidate_id}/resume/download")[0] == 200   # the file to attach when applying

    # The Team Lead searches for the candidate.
    page.goto(BASE + "/tl/search")
    page.wait_for_load_state("networkidle")
    group = page.get_by_role("group", name="Candidates", exact=True)                  # one group: no Salesforce for this person
    group.get_by_role("checkbox").first.wait_for(timeout=15000)
    assert group.get_by_role("checkbox").count() == 1                                 # only this Team Lead's team is offered
    assert "Jordan Rivera" not in group.inner_text() and "Aisha Rahman" not in group.inner_text()
    window = start_search(lead, candidate, "Machine Learning Engineer", "50", "Find & Assign Matching Jobs", picker="Candidates")
    assert window.get_by_text(candidate).first.is_visible()
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
    assert newest["candidate_count"] == 1
    status, detail = lead.api(f"/api/searches/{newest['id']}")
    assert [t["candidate"]["id"] for t in detail["targets"]] == [candidate_id]
    assert detail["targets"][0]["resume_id"] == resume["id"]                         # the search ran with the uploaded resume
    lead.close()

    # The jobs belong to the candidate; the responsible member sees exactly those, without signing in again.
    mpage.goto(BASE + "/member/jobs")
    mpage.locator('[data-slot="job-card"]').filter(visible=True).first.wait_for(timeout=15000)
    assert candidate in mpage.locator('[data-slot="job-card"]').filter(visible=True).first.inner_text()      # each card names the candidate
    status, own = member.api("/api/assignments?page_size=200")
    assert own["total"] == assigned
    assert all(item["candidate"]["id"] == candidate_id and item["member"]["id"] == STATE["member_id"] for item in own["items"])
    found = [item["job"]["title"] for item in own["items"]]
    assert any("Machine Learning Engineer" in title for title in found)
    assert not any("Account Executive" in title for title in found), found           # the unrelated posting in the demo data is not assigned
    assert all(item["match_score"] >= newest["min_match_score"] for item in own["items"])         # nothing below the threshold to fill the quota
    assert all(item["status"] == "ASSIGNED" and item["applied_at"] is None for item in own["items"])
    mpage.goto(BASE + f"/member/candidates/{candidate_id}")                          # the candidate's page lists the same jobs
    mpage.locator('[data-slot="job-card"]').filter(visible=True).first.wait_for(timeout=15000)
    assert member.kpi("Total Assigned") == assigned
    STATE.update(assigned=assigned, candidate_id=candidate_id, candidate_name=candidate)
    member.close()


def test_scenario_4_member_applies_for_the_candidate_and_every_dashboard_updates(browser, problems):
    need("assigned")
    lead, admin, member = Session(browser, problems), Session(browser, problems), Session(browser, problems)
    lead.login(STATE["lead_email"], NEW_PASSWORD, "/tl/dashboard")
    admin.login("admin@example.com", DEMO_PASSWORD, "/admin/dashboard")
    page = member.login(STATE["member_email"], NEW_PASSWORD, "/member/dashboard")
    before = {"member": member.kpi("Applied"), "lead": lead.kpi("Applied"), "admin": admin.kpi("Applied")}
    assert before["member"] == 0 and before["lead"] == 0
    # The three dashboards agree before anything happens: the member's totals are the candidate's, and so are the team's.
    assert member.kpi("Total Assigned") == lead.kpi("Total Assigned") == STATE["assigned"]

    page.goto(BASE + f"/member/candidates/{STATE['candidate_id']}")                   # My Candidates -> the candidate -> a job
    page.locator('[data-slot="job-card"]').filter(visible=True).first.wait_for(timeout=15000)
    # "Open Job" refuses a stored address that is not a public one (409), and some synthetic postings live on a host that
    # does not exist. The member cannot see addresses, so the Team Lead's view of the job says which posting can be opened.
    target = None
    for item in member.api(f"/api/assignments?candidate_id={STATE['candidate_id']}&page_size=200")[1]["items"]:
        address = lead.api(f"/api/jobs/{item['job']['id']}")[1].get("apply_url") or lead.api(f"/api/jobs/{item['job']['id']}")[1]["canonical_url"]
        if urlparse(address).hostname == "job-boards.greenhouse.io":
            target = item
            break
    assert target, "no assigned job with a public posting address in the demo data"
    page.get_by_label("Search jobs").fill(target["job"]["company"])
    card = page.locator('[data-slot="job-card"]').filter(visible=True).filter(has=page.get_by_role("link", name=target["job"]["title"], exact=True)).first
    card.wait_for(timeout=15000)
    card.get_by_role("link", name=target["job"]["title"], exact=True).click()
    page.wait_for_url(re.compile(r"/member/jobs/[0-9a-f-]{36}(\?candidate=[0-9a-f-]{36})?$"), timeout=15000)
    assignment_id = page.url.rsplit("/", 1)[1].split("?")[0]
    assert assignment_id == target["id"]
    description = page.locator('[data-slot="job-description"]')
    description.wait_for(timeout=15000)
    assert len(description.inner_text()) > 80                                         # the complete available description
    assert STATE["candidate_name"] in page.locator("main").inner_text()               # whose job this is
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
    assert detail["candidate"]["id"] == STATE["candidate_id"]

    page.get_by_role("button", name="Mark as Applied").click()                       # after applying on the employer's site
    dialog = page.get_by_role("dialog", name="Mark as Applied")
    dialog.wait_for(timeout=5000)
    dialog.get_by_role("button", name="Mark as Applied").click()
    member.toast("Marked as applied")
    page.locator('[data-slot="status-badge"][data-status="APPLIED"]').filter(visible=True).first.wait_for(timeout=10000)

    assert member.dashboard_kpi("/member/dashboard", "Applied") == before["member"] + 1
    assert member.kpi("Not Applied") == STATE["assigned"] - 1
    assert lead.dashboard_kpi("/tl/dashboard", "Applied") == before["lead"] + 1       # the Team Lead's analytics follow
    assert lead.kpi("Not Applied") == STATE["assigned"] - 1
    assert admin.dashboard_kpi("/admin/dashboard", "Applied") == before["admin"] + 1  # and the Admin's
    # The candidate's own figures say the same to the member and to the Team Lead.
    seen_by_member = member.api(f"/api/candidates/{STATE['candidate_id']}")[1]["stats"]
    seen_by_lead = lead.api(f"/api/candidates/{STATE['candidate_id']}")[1]["stats"]
    assert seen_by_member == seen_by_lead and seen_by_member["applied"] == 1 and seen_by_member["assigned"] == STATE["assigned"]

    # The employer answers: the member records the next status, and the dashboards follow again.
    page.goto(BASE + f"/member/jobs/{assignment_id}")
    page.get_by_role("button", name="Change Status").click()
    page.get_by_role("menuitem", name=re.compile("^Interviewing")).click()
    dialog = page.get_by_role("dialog", name="Change status to Interviewing")
    dialog.wait_for(timeout=5000)
    dialog.get_by_role("button", name="Change to Interviewing").click()
    page.locator('[data-slot="status-badge"][data-status="INTERVIEWING"]').filter(visible=True).first.wait_for(timeout=10000)
    assert member.dashboard_kpi("/member/dashboard", "Interviews") == 1 and member.kpi("Applied") == before["member"] + 1
    assert lead.dashboard_kpi("/tl/dashboard", "Interviews") == 1
    status, detail = lead.api(f"/api/assignments/{assignment_id}")                    # the Team Lead sees who did it, for which candidate
    assert detail["status"] == "INTERVIEWING" and detail["candidate"]["name"] == STATE["candidate_name"]
    assert detail["member"]["id"] == STATE["member_id"]
    for session in (lead, admin, member):
        session.close()


def test_scenario_5_admin_delegates_ten_jobs_and_team_lead_beta_distributes_them_to_candidates(browser, problems):
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
    members = {u["name"]: u["id"] for u in people["items"]}
    status, records = lead.api("/api/candidates?page_size=50")
    candidates = {c["name"]: c for c in records["items"]}
    # Aisha Rahman is Priya's candidate, Grace Liu is Quinn's.
    plan = (("Aisha Rahman", "Priya Nair", titles[:5]), ("Grace Liu", "Quinn Lopez", titles[5:]))
    assert all(candidates[c]["assigned_member"]["name"] == m for c, m, _ in plan)
    before = {m: lead.api(f"/api/assignments?member_id={members[m]}&page_size=1")[1]["total"] for _, m, _ in plan}
    for name, _, share in plan:                                                       # five each
        lpage.goto(BASE + "/tl/jobs")
        lpage.wait_for_load_state("networkidle")
        for title in share:
            box = lpage.get_by_role("checkbox", name=f"Select {title} at {company}")
            box.wait_for(timeout=15000)                                               # the delegated jobs wait in this team's queue
            box.check()
        lpage.get_by_role("button", name=re.compile(r"Distribute to Candidates \(5\)")).click()
        dialog = lpage.get_by_role("dialog", name="Assign to Candidates")
        group = dialog.get_by_role("group", name="Candidates")
        group.get_by_role("checkbox", name=re.compile(name)).wait_for(timeout=15000)
        offered = group.inner_text()
        assert "Jordan Rivera" not in offered and "Samuel Okafor" not in offered     # only this team's candidates can receive them
        assert "Victor Hale" not in offered                                           # and no deactivated candidate
        group.get_by_role("checkbox", name=re.compile(name)).check()
        dialog.get_by_role("button", name=re.compile("Review 5 Assignment")).click()
        lpage.get_by_role("button", name=re.compile("^Assign 5 Job")).click()
        lead.toast("Jobs assigned")
    lpage.goto(BASE + "/tl/jobs")
    lpage.wait_for_load_state("networkidle")
    assert lpage.get_by_role("checkbox", name=re.compile(f"at {re.escape(company)}$")).count() == 0      # the queue is empty again

    for name, member_name, _ in plan:
        member = Session(browser, problems)
        member.login(f"{member_name.split()[0].lower()}@example.com", DEMO_PASSWORD, "/member/dashboard")
        assert member.kpi("Total Assigned") == before[member_name] + 5               # the responsible member's dashboard shows the five
        mine = member.api(f"/api/assignments?q={company.replace(' ', '%20')}&page_size=50")[1]
        assert mine["total"] == 5 and all(item["job"]["company"] == company and item["candidate"]["name"] == name for item in mine["items"])
        member.close()
    # The Admin can trace every one of them: which candidate received which job, who is responsible, and that Team Beta's lead assigned it.
    status, traced = admin.api(f"/api/assignments?company={company.replace(' ', '%20')}&page_size=50")
    received = sorted((item["job"]["title"], item["candidate"]["name"], item["member"]["name"], item["assigned_by"]["name"], item["team"]["name"])
                      for item in traced["items"])
    assert traced["total"] == 10
    assert received == sorted([(t, c, m, "Ben Beta", "Team Beta") for c, m, share in plan for t in share])
    STATE.update(beta_member_id=members["Priya Nair"], beta_team_id=traced["items"][0]["team"]["id"], beta_assignment_id=traced["items"][0]["id"],
                 beta_candidate_id=candidates["Aisha Rahman"]["id"])
    lead.close()
    admin.close()


def move_candidate(session, candidate_id: str, candidate: str, to: str, button: str) -> None:
    """Team Lead: on the candidate's page, hand the candidate to another member."""
    page = session.page
    page.goto(BASE + f"/tl/candidates/{candidate_id}")
    page.get_by_role("heading", level=1, name=candidate).wait_for(timeout=15000)
    page.get_by_role("button", name=button).click()
    dialog = page.get_by_role("dialog", name=button)
    dialog.get_by_role("combobox", name=re.compile("Responsible member")).click()
    page.get_by_role("option", name=to).click()
    dialog.get_by_role("button", name="Move Candidate").click()
    session.toast(f"{to} is now responsible for {candidate}")
    dialog.wait_for(state="hidden", timeout=10000)


def test_scenario_6_nobody_reaches_another_team_or_another_members_candidate_by_changing_the_address(browser, problems):
    need("beta_member_id")
    lead = Session(browser, problems)
    page = lead.login("lead.alpha@example.com", DEMO_PASSWORD, "/tl/dashboard")
    member, team, assignment, candidate = STATE["beta_member_id"], STATE["beta_team_id"], STATE["beta_assignment_id"], STATE["beta_candidate_id"]
    lead.allowed += ["http 403: GET", "http 404: GET", "status of 403", "status of 404", member, team, assignment, candidate]               # refusals are expected here
    page.goto(BASE + f"/tl/members/{member}")                                         # Member B1's record, by typing the address
    page.wait_for_load_state("networkidle")
    text = page.locator("body").inner_text()
    assert "Priya" not in text and "priya@example.com" not in text                   # nothing of the other team is shown
    assert re.search(r"do not have access|not found|cannot be shown|no access|access denied", text, re.I), text[:300]
    page.goto(BASE + f"/tl/candidates/{candidate}")                                   # a candidate of Team Beta, by typing the address
    page.get_by_text("This candidate is not available to you").wait_for(timeout=15000)
    text = page.locator("body").inner_text()
    assert "Aisha" not in text and "aisha.candidate@example.com" not in text
    refused = {path: lead.api(path)[0] for path in (
        f"/api/users/{member}", f"/api/users/{member}/dashboard", f"/api/users/{member}/activity",
        f"/api/teams/{team}", f"/api/teams/{team}/members", f"/api/teams/{team}/analytics",
        f"/api/teams/{team}/queue", f"/api/analytics/member/{member}", f"/api/assignments/{assignment}")}
    assert set(refused.values()) == {403}, refused                                    # the backend refuses each one
    # A candidate outside the caller's scope answers exactly like one that does not exist.
    hidden = {path: lead.api(path)[0] for path in (
        f"/api/candidates/{candidate}", f"/api/candidates/{candidate}/profile", f"/api/candidates/{candidate}/resume",
        f"/api/candidates/{candidate}/resume/download", f"/api/candidates/{candidate}/analytics", f"/api/candidates/{candidate}/activity",
        "/api/candidates/00000000-0000-4000-8000-000000000000")}
    assert set(hidden.values()) == {404}, hidden
    status, filtered = lead.api(f"/api/assignments?member_id={member}&team_id={team}&page_size=50")
    assert status == 200 and filtered["total"] == 0                                   # a filter cannot widen the scope either
    status, filtered = lead.api(f"/api/assignments?candidate_id={candidate}&page_size=50")
    assert status == 200 and filtered["total"] == 0
    status, everyone = lead.api("/api/users?page_size=200")
    assert all(u["team"]["name"] == "Team Alpha" for u in everyone["items"])
    status, records = lead.api(f"/api/candidates?team_id={team}&page_size=200")
    assert status == 200 and all(c["team"]["name"] == "Team Alpha" for c in records["items"])
    status, records = lead.api("/api/candidates?page_size=200")
    # the five seeded by hand and Amara Nwosu, brought in from the (fake) Salesforce when the demo data was seeded
    assert records["total"] == 6 and all(c["team"]["name"] == "Team Alpha" for c in records["items"])
    alpha = {c["name"]: c for c in records["items"]}
    jordan, samuel = alpha["Jordan Rivera"], alpha["Samuel Okafor"]

    # Inside one team: a member sees the candidates assigned to them and nobody else's.
    maya, omar = Session(browser, problems), Session(browser, problems)
    mpage = maya.login("maya@example.com", DEMO_PASSWORD, "/member/dashboard")
    opage = omar.login("omar@example.com", DEMO_PASSWORD, "/member/dashboard")
    for session in (maya, omar):
        session.allowed += ["http 403: GET", "http 404: GET", "status of 403", "status of 404", jordan["id"], samuel["id"]]
    status, mine = maya.api("/api/candidates?page_size=50")
    assert sorted(c["name"] for c in mine["items"]) == ["Elena Petrova", "Jordan Rivera"]
    status, jobs = maya.api(f"/api/assignments?candidate_id={jordan['id']}&page_size=1")
    assert jobs["total"] > 0
    jordan_job = jobs["items"][0]["id"]
    status, foreign = lead.api(f"/api/assignments?candidate_id={samuel['id']}&page_size=1")
    samuel_job = foreign["items"][0]["id"]
    mpage.goto(BASE + f"/member/candidates/{samuel['id']}")                           # Noah's candidate, by typing the address
    mpage.get_by_text("This candidate is not assigned to you").wait_for(timeout=15000)
    text = mpage.locator("body").inner_text()
    assert "Samuel" not in text and "samuel.candidate@example.com" not in text
    hidden = {path: maya.api(path)[0] for path in (
        f"/api/candidates/{samuel['id']}", f"/api/candidates/{samuel['id']}/profile", f"/api/candidates/{samuel['id']}/resume",
        f"/api/candidates/{samuel['id']}/resume/download", f"/api/candidates/{samuel['id']}/analytics", f"/api/candidates/{samuel['id']}/activity",
        f"/api/candidates/{candidate}")}
    assert set(hidden.values()) == {404}, hidden
    assert maya.api(f"/api/assignments/{samuel_job}")[0] == 403
    assert maya.api(f"/api/assignments?candidate_id={samuel['id']}&page_size=50")[1]["total"] == 0
    assert omar.api("/api/candidates")[1]["total"] == 0 and omar.api("/api/assignments")[1]["total"] == 0
    assert omar.api(f"/api/candidates/{jordan['id']}")[0] == 404 and omar.api(f"/api/assignments/{jordan_job}")[0] == 403

    # The Team Lead hands Jordan to Omar: Maya loses the candidate with all jobs at once, Omar gains them. No one signs in again.
    total = jobs["total"]
    move_candidate(lead, jordan["id"], "Jordan Rivera", "Omar Haddad", "Move to Another Member")
    assert maya.api(f"/api/candidates/{jordan['id']}")[0] == 404
    assert maya.api(f"/api/candidates/{jordan['id']}/resume/download")[0] == 404
    assert maya.api(f"/api/assignments/{jordan_job}")[0] == 403
    assert maya.api(f"/api/assignments?candidate_id={jordan['id']}&page_size=1")[1]["total"] == 0
    assert [c["name"] for c in maya.api("/api/candidates")[1]["items"]] == ["Elena Petrova"]
    mpage.goto(BASE + f"/member/candidates/{jordan['id']}")
    mpage.get_by_text("This candidate is not assigned to you").wait_for(timeout=15000)
    assert "Jordan" not in mpage.locator("main").inner_text()
    mpage.goto(BASE + "/member/candidates")
    mpage.locator('[data-slot="candidate-card"]').first.wait_for(timeout=15000)
    assert mpage.locator('[data-slot="candidate-card"]').count() == 1 and "Jordan" not in mpage.locator("main").inner_text()
    mpage.goto(BASE + f"/member/jobs/{jordan_job}")
    mpage.wait_for_load_state("networkidle")
    assert mpage.locator('[data-slot="job-description"]').count() == 0               # the job page shows nothing of it any more
    assert omar.api(f"/api/candidates/{jordan['id']}")[0] == 200 and omar.api(f"/api/assignments/{jordan_job}")[0] == 200
    assert omar.api(f"/api/assignments?candidate_id={jordan['id']}&page_size=1")[1]["total"] == total   # all jobs and history went along
    opage.goto(BASE + "/member/candidates")
    opage.locator('[data-slot="candidate-card"]').filter(has_text="Jordan Rivera").wait_for(timeout=15000)
    assert omar.dashboard_kpi("/member/dashboard", "Total Assigned") == jordan["stats"]["assigned"]
    # And back again, so the seeded picture is restored: Maya regains everything, Omar holds nothing.
    move_candidate(lead, jordan["id"], "Jordan Rivera", "Maya Chen", "Move to Another Member")
    assert omar.api(f"/api/candidates/{jordan['id']}")[0] == 404 and omar.api(f"/api/assignments/{jordan_job}")[0] == 403
    assert omar.api("/api/assignments")[1]["total"] == 0
    assert maya.api(f"/api/candidates/{jordan['id']}")[0] == 200
    assert maya.api(f"/api/assignments?candidate_id={jordan['id']}&page_size=1")[1]["total"] == total
    for session in (maya, omar, lead):
        session.close()


def test_scenario_7_a_review_search_continues_when_the_popup_is_closed_and_assigns_only_what_is_chosen(browser, problems):
    lead = Session(browser, problems)
    page = lead.login("lead.alpha@example.com", DEMO_PASSWORD, "/tl/dashboard")
    status, records = lead.api("/api/candidates?page_size=50")
    samuel = next(c for c in records["items"] if c["name"] == "Samuel Okafor")       # Noah Patel's candidate
    count = lambda: lead.api(f"/api/assignments?candidate_id={samuel['id']}&page_size=1")[1]["total"]
    before = count()
    known = {r["id"] for r in lead.api("/api/searches?page_size=50")[1]["items"]}
    window = start_search(lead, "Samuel Okafor", "LLM Engineer", "4", "Search first, review, then assign")
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
    assert run["mode"] == "review" and run["assigned_count"] == 0 and count() == before          # review mode: nothing is assigned by itself
    page.goto(BASE + f"/tl/searches/{run_id}")
    page.get_by_role("heading", name="Results", exact=True).wait_for(timeout=20000)
    rows = page.get_by_role("checkbox", name=re.compile("^Select .* for Samuel Okafor"))
    rows.first.wait_for(timeout=15000)
    status, results = lead.api(f"/api/searches/{run_id}/results?page_size=50")
    assert results["total"] == run["matched_count"] and rows.count() == results["total"]          # the results were kept
    assert all(r["candidate"]["id"] == samuel["id"] for r in results["items"])
    free = [r for r in results["items"] if not r["assignment_id"]]
    assert free, "every result of the review search is already one of Samuel's jobs"
    chosen = free[:2]
    for r in chosen:                                                                  # the Team Lead assigns the ones they choose
        page.get_by_role("checkbox", name=f"Select {r['job']['title']} at {r['job']['company']} for Samuel Okafor").check()
    page.get_by_role("button", name=f"Assign Selected ({len(chosen)})").click()
    confirm = page.get_by_role("alertdialog").or_(page.get_by_role("dialog")).filter(has_text="Samuel Okafor").last
    confirm.get_by_role("button", name=re.compile(rf"^Assign {len(chosen)} Jobs?$")).click()
    lead.toast("Jobs assigned")
    assert count() == before + len(chosen)                                            # exactly those, and nothing else
    # Noah Patel, the responsible member, has them in his list.
    noah = Session(browser, problems)
    noah.login("noah@example.com", DEMO_PASSWORD, "/member/dashboard")
    mine = noah.api(f"/api/assignments?candidate_id={samuel['id']}&page_size=200")[1]
    assert mine["total"] == before + len(chosen)
    assert {r["job"]["id"] for r in chosen} <= {item["job"]["id"] for item in mine["items"]}
    noah.close()
    lead.close()


def test_scenario_8_team_lead_brings_a_candidate_in_from_salesforce_and_the_member_finds_resume_and_jobs(browser, problems):
    """Salesforce is the in-process fake of the demo stack (tests/e2e/salesforce_fake.py): no request leaves this machine."""
    name = "Ines Carvalho"                                                            # Active, with a PDF resume, not in the portal yet
    lead = Session(browser, problems)
    page = lead.login("lead.alpha@example.com", DEMO_PASSWORD, "/tl/dashboard")
    status, before = lead.api(f"/api/candidates?q={name.replace(' ', '%20')}&page_size=50")
    assert status == 200 and before["total"] == 0, "the demo data already holds the candidate: start from freshly seeded data"
    status, people = lead.api("/api/users?role=MEMBER&page_size=50")
    omar_id = next(u["id"] for u in people["items"] if u["email"] == "omar@example.com")
    omar = Session(browser, problems)                                                 # Omar Haddad holds no candidate so far
    opage = omar.login("omar@example.com", DEMO_PASSWORD, "/member/dashboard")
    asked = []                                                                        # a member's pages never ask about Salesforce
    omar.context.on("request", lambda r: asked.append(r.url) if "/api/salesforce" in r.url else None)
    assert omar.api("/api/candidates")[1]["total"] == 0 and omar.api("/api/assignments")[1]["total"] == 0

    # Find Jobs: the Team Lead types the name; the portal does not know her, Salesforce does.
    page.goto(BASE + "/tl/search")
    page.wait_for_load_state("networkidle")
    salesforce = page.locator('[data-slot="salesforce-import"]')
    salesforce.get_by_role("heading", name="From Salesforce").wait_for(timeout=15000)
    assert page.get_by_role("button", name=re.compile("^Add (Candidate|Manually)")).count() == 0   # candidates come from Salesforce only
    page.get_by_role("searchbox", name="Search candidates").fill("ines")
    page.get_by_role("group", name=PICKER, exact=True).get_by_text("Nobody in the portal matches this search").wait_for(timeout=15000)
    row = salesforce.locator('[data-slot="salesforce-row"]').filter(has_text=name)
    row.wait_for(timeout=15000)
    found = row.inner_text()
    assert "Resume in Salesforce" in found and "Bring in" in found and "In the portal" not in found, found
    with page.expect_response(lambda r: r.url.endswith("/api/candidates/import-salesforce") and r.request.method == "POST") as info:
        row.get_by_role("button").click()                                             # bring her in, with the resume
    assert info.value.status == 201
    sent, answer = info.value.request.post_data_json, info.value.json()
    assert sent["salesforce_id"] == "001E2E000000009AAA" and sent["team_id"] is None  # a Team Lead's own team is used
    assert answer["sync"]["created"] is True and answer["sync"]["resume"] == "imported"
    candidate_id = answer["id"]
    lead.toast(f"{name} brought in from Salesforce")
    chosen = page.get_by_role("region", name="Selected candidates").get_by_role("listitem").filter(has_text=name)
    chosen.wait_for(timeout=15000)                                                    # chosen for the search at once
    assert chosen.locator('[data-slot="tag"]').filter(has_text="Resume uploaded").is_visible()
    assert page.get_by_text(re.compile(re.escape(name) + r" has no\s+resume")).count() == 0
    page.get_by_text(re.compile(re.escape(name) + r" has no\s+responsible member")).wait_for(timeout=5000)
    row.get_by_text("Selected").wait_for(timeout=15000)                               # the Salesforce row says where she is now
    assert "In the portal" in row.inner_text()

    # The responsible member is chosen right there, and saved to the candidate.
    chosen.get_by_role("combobox", name="Responsible member, who handles the jobs found").click()
    page.get_by_role("option", name="Omar Haddad", exact=True).click()
    lead.toast(f"Omar Haddad is now responsible for {name}")
    status, record = lead.api(f"/api/candidates/{candidate_id}")
    assert status == 200 and record["source"] == "salesforce" and record["salesforce_id"] == "001E2E000000009AAA"
    assert record["salesforce_url"] == "https://salesforce.example.com/001E2E000000009AAA"
    assert record["team"]["name"] == "Team Alpha" and record["assigned_member"]["id"] == omar_id
    assert record["has_resume"] is True and record["resume_has_file"] is True and record["email"] == "ines.carvalho@example.com"
    status, resume = lead.api(f"/api/candidates/{candidate_id}/resume")
    assert status == 200 and resume["source"] == "salesforce" and resume["filename"].lower().endswith(".pdf")
    assert name in resume["text"] and "Machine Learning Engineer" in resume["text"]  # the file's text was read

    # The search runs with that resume.
    field = page.get_by_label(re.compile("^Job titles"))
    field.fill("Machine Learning Engineer")
    field.press("Enter")
    page.get_by_label("Results per candidate").fill("50")
    page.get_by_role("radio", name=re.compile("Find & Assign Matching Jobs")).check()
    page.get_by_role("button", name="Start Search").click()
    window = page.get_by_role("dialog", name="AI Job Search Started")
    window.wait_for(timeout=15000)
    assert window.get_by_text(name).first.is_visible()
    window.locator('[data-slot="tag"][data-status="completed"]').first.wait_for(timeout=120000)
    assigned = int(window.locator('[data-slot="run-counter"][data-label="Assigned"] [data-slot="run-counter-value"]').first.inner_text().replace(",", ""))
    assert assigned > 0
    status, runs = lead.api("/api/searches?page_size=1")
    newest = runs["items"][0]
    assert newest["status"] == "completed" and newest["assigned_count"] == assigned and newest["candidate_count"] == 1
    status, detail = lead.api(f"/api/searches/{newest['id']}")
    assert [t["candidate"]["id"] for t in detail["targets"]] == [candidate_id]
    assert detail["targets"][0]["resume_id"] == resume["id"]                         # the resume that came from Salesforce
    lead.close()

    # The member finds the candidate under My Candidates, with the resume and the jobs, without signing in again.
    opage.goto(BASE + "/member/candidates")
    card = opage.locator('[data-slot="candidate-card"]').filter(has_text=name)
    card.wait_for(timeout=15000)
    assert opage.locator('[data-slot="candidate-card"]').count() == 1
    assert card.locator('[data-slot="source-tag"]').filter(visible=True).count() == 1  # marked as brought in from Salesforce
    status, mine = omar.api("/api/candidates")
    assert status == 200 and [c["id"] for c in mine["items"]] == [candidate_id]
    assert mine["items"][0]["source"] == "salesforce" and mine["items"][0]["has_resume"] is True
    assert mine["items"][0]["salesforce_id"] is None and mine["items"][0]["salesforce_url"] is None     # no way into Salesforce for a member
    assert omar.api(f"/api/candidates/{candidate_id}/resume/download")[0] == 200     # the file to attach when applying
    opage.goto(BASE + f"/member/candidates/{candidate_id}?tab=resume")
    opage.get_by_role("link", name="Download File").filter(visible=True).first.wait_for(timeout=15000)
    assert opage.get_by_text("Open in Salesforce").count() == 0 and opage.get_by_role("button", name="Refresh from Salesforce").count() == 0
    opage.goto(BASE + f"/member/candidates/{candidate_id}")                          # the candidate's page lists the jobs
    opage.locator('[data-slot="job-card"]').filter(visible=True).first.wait_for(timeout=15000)
    assert omar.kpi("Total Assigned") == assigned
    opage.goto(BASE + "/member/jobs")
    opage.locator('[data-slot="job-card"]').filter(visible=True).first.wait_for(timeout=15000)
    assert name in opage.locator('[data-slot="job-card"]').filter(visible=True).first.inner_text()
    status, own = omar.api("/api/assignments?page_size=200")
    assert own["total"] == assigned
    assert all(item["candidate"]["id"] == candidate_id and item["member"]["id"] == omar_id for item in own["items"])
    assert any("Machine Learning Engineer" in item["job"]["title"] for item in own["items"])
    assert all(item["match_score"] >= newest["min_match_score"] for item in own["items"])
    assert asked == [], asked
    omar.allowed += ["http 403: GET", "status of 403"]                                # and the API refuses a member who asks all the same
    assert omar.api("/api/salesforce/status")[0] == 403 and omar.api("/api/salesforce/candidates?q=ines")[0] == 403
    omar.close()
