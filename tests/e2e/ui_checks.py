"""Browser checks of the whole web app against the demo stack (see tests/e2e/run.sh, which starts it).

    python tests/e2e/ui_checks.py --base http://127.0.0.1:3011 --w 1280 --h 900
    python tests/e2e/ui_checks.py --base http://127.0.0.1:3011 --w 390 --h 844

The run changes data (it invites people, adds candidates, assigns jobs, marks applications), so start from freshly
seeded demo data.

Members are recruiters with an account; candidates are the people who need a job (records, never accounts). Jobs
belong to candidates, and the member responsible for a candidate works on them.

Every step is a named check. Console errors/warnings, page errors and failed API
requests are collected for the whole run; a step that provokes a failure on purpose
declares it (expect=...). Exit code 1 when a check failed or an unexpected problem was logged.
"""
import argparse
import datetime
import re
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect

ap = argparse.ArgumentParser()
ap.add_argument("--w", type=int, default=1280)
ap.add_argument("--h", type=int, default=900)
ap.add_argument("--base", default="http://127.0.0.1:3011")
ap.add_argument("--shots", default="")
ap.add_argument("--only", default="")
A = ap.parse_args()

BASE = A.base
WORK_DIR = tempfile.mkdtemp(prefix="jobfinder-ui-")      # files the run uploads (a resume, a CSV)
PASSWORD = "Demo-Password-42!"
STAMP = str(int(time.time()))[-7:]
MOBILE = A.w < 768
# The Find Jobs candidate list has two groups since candidates can be brought in from Salesforce (the demo stack has a
# fake one): this is the group of the portal's own candidates. Other pickers (dialogs) have one group, "Candidates".
PICKER = "Candidates in the portal"
SHARED = {}           # what one step leaves for the next (the member invited and the candidate added during the run)
results = []          # (ok, name, detail)
problems = []         # unexpected console / network problems
expected = []         # substrings that are allowed right now


skipped = []          # checks that could not be made, with the reason


def skip(name, reason):
    skipped.append((name, reason))
    print(f"  SKIP {name}  [{reason}]", flush=True)


def check(name, ok, detail=""):
    results.append((bool(ok), name, detail))
    print(("  PASS " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)
    return bool(ok)


@contextmanager
def expecting(*substrings):
    """Problems containing one of these substrings are expected inside the block."""
    expected.extend(substrings)
    try:
        yield
    finally:
        for s in substrings:
            expected.remove(s)


def note(kind, text):
    if any(s in text for s in expected):
        return
    problems.append(f"{kind}: {text[:500]}")
    print(f"  PROBLEM {kind}: {text[:300]}", flush=True)


def watch(page):
    page.on("console", lambda m: note(f"console.{m.type}", m.text) if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: note("pageerror", str(e)))
    page.on("response", lambda r: note(f"http {r.status}", f"{r.request.method} {r.url}") if r.status >= 400 and "/api/" in r.url else None)
    page.on("requestfailed", lambda r: note("requestfailed", f"{r.method} {r.url} {r.failure}")
            if "/api/" in r.url and "ERR_ABORTED" not in str(r.failure) else None)


SALESFORCE_OFF = {"configured": False, "connected": None, "message": "", "checked_at": None, "api_version": "v68.0", "team_lead_scope": "all"}


@contextmanager
def without_salesforce(page):
    """Where the portal is connected to Salesforce (the demo stack has a fake one) candidates come from there only, and
    nothing offers to type one in. The dialog for that, "Add Candidate", belongs to a portal without Salesforce: inside
    this block the page is told that Salesforce is not configured. Pages loaded inside it keep that answer until they
    are loaded again."""
    page.route("**/api/salesforce/status", lambda route: route.fulfill(status=200, json=SALESFORCE_OFF))
    try:
        yield
    finally:
        page.wait_for_load_state("networkidle")
        page.unroute("**/api/salesforce/status")


def new_context(browser):
    ctx = browser.new_context(viewport={"width": A.w, "height": A.h})
    ctx.on("page", watch)
    return ctx


def login(page, email, password=PASSWORD, expect_home=None):
    page.goto(BASE + "/login")
    page.get_by_label("E-mail").fill(email)
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
    page.wait_for_load_state("networkidle")
    if expect_home:
        check(f"login {email} lands on {expect_home}", page.url.endswith(expect_home), page.url)


def local(url):
    """The same link on the server under test."""
    return re.sub(r"^https?://[^/]+", BASE, url)


def api(page, path):
    return page.evaluate("async (p) => { const r = await fetch(p, {credentials: 'same-origin'}); return r.ok ? await r.json() : {__status: r.status} }", path)


def kpi(page, label):
    cell = page.locator(f'[data-slot="kpi"][data-label="{label}"]').filter(visible=True).first
    cell.wait_for(timeout=15000)
    text = cell.locator('[data-slot="kpi-value"]').inner_text().strip()
    return int(text.replace(",", "")) if text.replace(",", "").isdigit() else text


def counter(scope, label):
    text = scope.locator(f'[data-slot="run-counter"][data-label="{label}"] [data-slot="run-counter-value"]').first.inner_text().strip()
    return int(text.replace(",", ""))


def no_overflow(page, name):
    overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check(f"no horizontal page overflow: {name}", overflow <= 0, f"{overflow}px")


def shot(page, name):
    if A.shots:
        page.screenshot(path=f"{A.shots}/{A.w}_{name}.png", full_page=True)


def open_nav(page):
    """On small screens the sidebar is a drawer."""
    if MOBILE:
        page.get_by_role("button", name="Toggle navigation").click()
        page.get_by_role("dialog").get_by_role("link", name="Dashboard", exact=True).wait_for(timeout=5000)


def nav_link(page, name):
    scope = page.get_by_role("dialog") if MOBILE else page.get_by_role("navigation", name="Sections")
    return scope.get_by_role("link", name=name, exact=True)


def sign_out(page):
    page.get_by_role("button", name=re.compile("Account menu")).click()
    page.get_by_role("menuitem", name="Sign out").click()
    page.wait_for_url(re.compile(r"/login$"), timeout=15000)


def toast(page, text, timeout=15000):
    page.locator("[data-sonner-toast]").filter(has_text=text).first.wait_for(timeout=timeout)


def send(page, method, path, body=None):
    """A state-changing request as the signed-in person, with the CSRF header the app itself sends. Returns the status."""
    return page.evaluate(
        """async ([method, path, body]) => {
            const csrf = decodeURIComponent((document.cookie.split('; ').find((c) => c.startsWith('jf_csrf=')) || '').slice(8));
            const r = await fetch(path, { method, credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: body === null ? undefined : JSON.stringify(body) });
            return r.status;
        }""", [method, path, body])


def candidate_named(page, name):
    """The candidate record as the signed-in person receives it (None when it is not theirs to see)."""
    data = api(page, f"/api/candidates?q={name.replace(' ', '%20')}&page_size=50")
    return next((c for c in data.get("items", []) if c["name"] == name), None)


def openable_job(browser, candidate_name):
    """An open job of that candidate whose stored posting address is a public one.
    "Open Job" refuses anything else (409), and some synthetic postings live on a host that does not exist.
    A member is never shown an address, so an Admin's view of the jobs is asked."""
    ctx = browser.new_context()
    page = ctx.new_page()
    login(page, "admin@example.com")
    record = candidate_named(page, candidate_name)
    found = None
    for item in api(page, f"/api/assignments?candidate_id={record['id']}&applied=false&page_size=200")["items"]:
        if item["status"] not in ("ASSIGNED", "VIEWED"):
            continue
        job = api(page, f"/api/jobs/{item['job']['id']}")
        if urlparse(job.get("apply_url") or job.get("canonical_url") or "").hostname == "job-boards.greenhouse.io":
            found = item
            break
    ctx.close()
    assert found, f"no open job with a public posting address for {candidate_name}"
    return found


def open_job_card(page, job):
    """Find a job in a member's card list by its company and open it by its title."""
    page.get_by_role("searchbox", name="Search jobs").fill(job["company"])
    card = page.locator('[data-slot="job-card"]').filter(visible=True).filter(has=page.get_by_role("link", name=job["title"], exact=True)).first
    card.wait_for(timeout=15000)
    card.get_by_role("link", name=job["title"], exact=True).click()


def pick_option(page, scope, combobox, option):
    scope.get_by_role("combobox", name=combobox).click()
    page.get_by_role("option", name=option, exact=True).click()


def bell_text(page, pattern, timeout=15000):
    """Open the notification list and wait for a notification matching the pattern. Returns its locator."""
    bell = page.get_by_role("button", name=re.compile("Notifications"))
    bell.click()
    item = page.get_by_text(pattern).first
    item.wait_for(timeout=timeout)
    return item


# --------------------------------------------------------------------------------------------- steps
def step_auth(browser):
    print("== auth and role redirects")
    ctx = new_context(browser)
    page = ctx.new_page()
    page.goto(BASE + "/")
    check("signed out: / shows the landing page", page.url.rstrip("/") == BASE.rstrip("/"), page.url)
    expect(page.get_by_role("heading", level=1)).to_contain_text("Real US jobs")
    page.get_by_role("banner").get_by_role("link", name="Sign in").click()
    page.wait_for_url(lambda u: u.rstrip("/").endswith("/login"), timeout=10000)
    check("signed out: the landing page's Sign in leads to /login", page.url.rstrip("/").endswith("/login"), page.url)
    page.goto(BASE + "/member/jobs?applied=false")
    check("signed out: protected page goes to /login?next=...", "/login?next=%2Fmember%2Fjobs%3Fapplied%3Dfalse" in page.url, page.url)

    # wrong password: the API's own message is shown
    page.get_by_label("E-mail").fill("maya@example.com")
    page.get_by_label("Password", exact=True).fill("definitely-wrong")
    with expecting("/api/auth/login", "401"):
        page.get_by_role("button", name="Sign in").click()
        alert = page.get_by_role("alert").filter(has_text="Invalid e-mail or password.")
        alert.wait_for(timeout=10000)
    check("wrong password shows the API message", alert.is_visible())
    # show / hide password
    page.get_by_role("button", name="Show password").click()
    check("show password toggles the field", page.get_by_label("Password", exact=True).get_attribute("type") == "text")

    # correct password: back to the page that was asked for
    page.get_by_label("Password", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
    check("after sign-in the member returns to the page they asked for", "/member/jobs?applied=false" in page.url, page.url)
    page.wait_for_load_state("networkidle")

    page.goto(BASE + "/admin/dashboard")
    page.wait_for_load_state("networkidle")
    check("member opening /admin/dashboard is sent to their own dashboard", page.url.endswith("/member/dashboard"), page.url)
    page.goto(BASE + "/tl/members")
    check("member opening /tl/members is sent to their own dashboard", page.url.endswith("/member/dashboard"), page.url)
    page.goto(BASE + "/login")
    check("signed-in user opening /login goes home", page.url.endswith("/member/dashboard"), page.url)
    page.goto(BASE + "/")
    check("signed-in user opening / goes home", page.url.endswith("/member/dashboard"), page.url)
    ctx.close()

    for email, home in (("admin@example.com", "/admin/dashboard"), ("lead.alpha@example.com", "/tl/dashboard"), ("maya@example.com", "/member/dashboard")):
        ctx = new_context(browser)
        page = ctx.new_page()
        login(page, email, expect_home=home)
        expect(page.locator("h1").filter(visible=True).first).to_be_visible()
        no_overflow(page, home)
        ctx.close()


def step_member(browser):
    print("== member: dashboard, job list, job detail, open job, mark as applied")
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "maya@example.com", expect_home="/member/dashboard")
    applied_before = kpi(page, "Applied")
    not_applied_before = kpi(page, "Not Applied")
    check("member dashboard shows the six KPI cards", page.locator('[data-slot="kpi"]').filter(visible=True).count() == 6)
    labels = [page.locator('[data-slot="kpi"]').filter(visible=True).nth(i).get_attribute("data-label") for i in range(6)]
    check("KPI labels are the six asked for", labels == ["Total Assigned", "Not Applied", "Applied", "Interviews", "Offers", "Rejected/Closed"], str(labels))
    for title in ("Pipeline", "By candidate", "Applications over time", "Status distribution", "Weekly activity", "Pending vs applied"):
        check(f"dashboard panel present: {title}", page.get_by_role("heading", name=title, exact=True).is_visible())
    check("the dashboard says whose jobs these are (the member's candidates)", page.get_by_text("Where the jobs of your candidates stand").is_visible())

    # per-candidate table: one row per candidate of the member, adding up to the totals above
    me = api(page, "/api/auth/me")
    board = api(page, f"/api/users/{me['id']}/dashboard")
    rows = {r["candidate"]["name"]: r["stats"] for r in board["candidates"]}
    check("the API's dashboard lists the member's candidates", sorted(rows) == ["Elena Petrova", "Jordan Rivera"], str(sorted(rows)))
    check("the candidates' figures add up to the member's totals",
          all(sum(r[k] for r in rows.values()) == board["stats"][k] for k in ("assigned", "not_applied", "applied", "interviews", "offers")), str(board["stats"]))
    row = page.locator("tr, li").filter(has=page.get_by_role("link", name="Jordan Rivera")).filter(visible=True).last
    shown = row.inner_text()
    cells = row.locator("dd").all_inner_texts()[:5] if MOBILE else row.locator("td").all_inner_texts()[1:6]
    figures = [int(c.replace(",", "")) for c in cells]
    want = [rows["Jordan Rivera"][k] for k in ("assigned", "not_applied", "applied", "interviews", "offers")]
    check("the per-candidate table shows the API's figures for a candidate", figures == want, f"shown {figures}, API {want}")
    check("each candidate in the table links to the candidate's page",
          page.get_by_role("link", name="Jordan Rivera").filter(visible=True).first.get_attribute("href") == f"/member/candidates/{next(r['candidate']['id'] for r in board['candidates'] if r['candidate']['name'] == 'Jordan Rivera')}")
    check("a row without a resume would say so; these have one", "No resume uploaded" not in shown)
    check("the member's own table has no 'Responsible member' column", page.get_by_role("columnheader", name="Responsible member").count() == 0
          and "Responsible:" not in shown)
    check("charts are drawn (Recharts SVG present)", page.locator("svg.recharts-surface").filter(visible=True).count() >= 2)
    no_overflow(page, "member dashboard")
    shot(page, "member_dashboard")

    # job list
    page.goto(BASE + "/member/jobs?applied=false")
    page.wait_for_load_state("networkidle")
    cards = page.locator('[data-slot="job-card"]').filter(visible=True)
    cards.first.wait_for(timeout=15000)
    check("member job list shows cards", cards.count() > 0, f"{cards.count()} cards")
    check("watermark overlays the member job view", page.locator('[data-slot="watermark"]').filter(visible=True).count() == 1)
    wm = page.locator('[data-slot="watermark"]').first
    check("watermark is inert (aria-hidden, no pointer events)", wm.get_attribute("aria-hidden") == "true" and wm.evaluate("e => getComputedStyle(e).pointerEvents") == "none")
    no_overflow(page, "member jobs")
    shot(page, "member_jobs")

    # search + pagination controls exist
    check("job list has search, status filter and sort", page.get_by_role("searchbox", name="Search jobs").is_visible()
          and page.get_by_role("combobox", name="Status").is_visible() and page.get_by_role("combobox", name="Sort").is_visible())
    check("job list is paged", page.get_by_role("navigation", name="Pages of jobs").is_visible())
    check("the page is called All Jobs", page.get_by_role("heading", level=1, name="All Jobs").is_visible())
    check("every card names the candidate the job is for", all("For" in c.inner_text() and ("Jordan Rivera" in c.inner_text() or "Elena Petrova" in c.inner_text()) for c in cards.all()))

    # candidate filter: the member's candidates, sent to the API as candidate_id
    page.get_by_role("combobox", name="Candidate").click()
    offered = [o.inner_text().strip() for o in page.get_by_role("option").all()]
    check("the candidate filter offers exactly the member's candidates", offered == ["All candidates", "Elena Petrova", "Jordan Rivera"], str(offered))
    jordan_id = next(r["candidate"]["id"] for r in board["candidates"] if r["candidate"]["name"] == "Jordan Rivera")
    with page.expect_response(lambda r: "/api/assignments?" in r.url and f"candidate_id={jordan_id}" in r.url) as info:
        page.get_by_role("option", name="Jordan Rivera", exact=True).click()
    check("choosing a candidate sends candidate_id to the API", info.value.ok and f"candidate={jordan_id}" in page.url, page.url[-80:])
    cards.first.wait_for(timeout=15000)
    check("the filtered list holds that candidate's jobs only", cards.count() > 0 and all("Jordan Rivera" in c.inner_text() for c in cards.all()), f"{cards.count()} cards")
    page.get_by_role("combobox", name="Candidate").click()
    with page.expect_response(lambda r: "/api/assignments?" in r.url and "candidate_id=" in r.url):
        page.get_by_role("option", name="Elena Petrova", exact=True).click()
    page.get_by_text("No jobs match these filters").wait_for(timeout=15000)
    check("a candidate without jobs gives the 'no jobs match' state, with a way to clear", page.get_by_role("button", name="Clear filters").last.is_visible())
    page.get_by_role("button", name="Clear filters").last.click()
    cards.first.wait_for(timeout=15000)
    page.goto(BASE + "/member/jobs?applied=false")
    cards.first.wait_for(timeout=15000)

    job = openable_job(browser, "Jordan Rivera")
    title = job["job"]["title"]
    open_job_card(page, job["job"])
    page.wait_for_url(re.compile(r"/member/jobs/[0-9a-f-]{36}$"), timeout=15000)
    assignment_id = page.url.rsplit("/", 1)[1]
    page.get_by_role("heading", level=1, name=title).wait_for(timeout=15000)
    page.wait_for_load_state("networkidle")
    check("job detail page opens", assignment_id == job["id"], title)
    check("the job page says which candidate the job is for, linked to the candidate",
          page.get_by_role("link", name="Jordan Rivera").filter(visible=True).first.get_attribute("href") == f"/member/candidates/{jordan_id}")
    html = page.content()
    check("no posting URL is rendered for the member", "greenhouse.io" not in html and "careers.example" not in html and "lever.co" not in html)
    check("no copy-link control for the member", page.get_by_role("button", name=re.compile("copy", re.I)).count() == 0)
    addresses = set(re.findall(r"[\w.+-]+@example\.com", html)) - {"maya@example.com"}
    check("no other person's e-mail address is rendered for the member", not addresses, ", ".join(sorted(addresses)))
    description = page.locator('[data-slot="job-description"]').inner_text()
    check("the description shows no web address to the member", re.search(r"https?://|www\.", description) is None)
    for section in ("Overview", "Match", "Full description", "Tracking"):
        check(f"job detail section: {section}", page.get_by_role("heading", name=section, exact=True).is_visible())
    no_overflow(page, "member job detail")
    shot(page, "member_job_detail")

    page.get_by_text(re.compile(r"^Job details viewed by Maya Chen")).filter(visible=True).first.wait_for(timeout=15000)
    viewed = page.get_by_text(re.compile(r"^Job details viewed by Maya Chen")).filter(visible=True).count()
    check("opening the job details is written to the history once", viewed == 1, f"{viewed} entries")

    # Open Job / Apply: a new tab to the audited redirect, and it is not an application
    requests, answers = [], {}
    ctx.on("request", lambda r: requests.append(r.url))
    ctx.on("response", lambda r: answers.__setitem__(r.url, r.status))
    with ctx.expect_page() as popup_info:
        page.get_by_role("button", name="Open Job / Apply").click()
    popup = popup_info.value
    try:
        popup.wait_for_load_state(timeout=8000)
    except Exception:
        pass
    opened = [u for u in requests if f"/api/assignments/{assignment_id}/open-job" in u]
    check("Open Job opens a new tab to /api/assignments/{id}/open-job", len(opened) == 1 and opened[0].startswith(BASE), opened[0] if opened else "no such request")
    status = answers.get(opened[0]) if opened else None
    check("the API accepts it as a same-site request and redirects to the posting", status in (302, 303, 307), f"HTTP {status}")
    popup.close()
    page.get_by_text("Job posting opened by").first.wait_for(timeout=15000)
    check("opening the job is logged in the history", True)
    check("opening the job did not mark it as applied", page.get_by_role("button", name="Mark as Applied").is_visible())

    # Mark as Applied: separate, confirmed
    page.get_by_role("button", name="Mark as Applied").click()
    dialog = page.get_by_role("dialog", name="Mark as Applied")
    dialog.wait_for(timeout=5000)
    check("Mark as Applied asks for confirmation", dialog.get_by_text("Opening the job posting is not counted as applying").is_visible())
    dialog.get_by_label("Reference or confirmation number").fill(f"REF-{STAMP}")
    when = dialog.get_by_label("When was the application submitted?")
    when.fill("2020-01-01T10:00")
    dialog.get_by_role("button", name="Mark as Applied").click()
    try:
        dialog.get_by_role("alert").filter(has_text=re.compile("cannot be earlier than")).first.wait_for(timeout=5000)
        check("an application date before the job was assigned is refused in the form", True)
    except Exception:
        check("an application date before the job was assigned is refused in the form", False)
    when.fill((datetime.datetime.now() - datetime.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"))
    with page.expect_request(lambda r: r.method == "PATCH" and r.url.endswith(f"/api/assignments/{assignment_id}/status")) as sent:
        dialog.get_by_role("button", name="Mark as Applied").click()
    applied_at = (sent.value.post_data_json or {}).get("applied_at")
    check("the application date is sent with its time zone", isinstance(applied_at, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z", applied_at) is not None, str(applied_at))
    toast(page, "Marked as applied")
    page.locator('[data-slot="status-badge"][data-status="APPLIED"]').filter(visible=True).first.wait_for(timeout=10000)
    check("status becomes Applied", True)
    check("reference number is shown", page.get_by_text(f"REF-{STAMP}").first.is_visible())

    page.goto(BASE + "/member/dashboard")
    page.wait_for_load_state("networkidle")
    applied_after = kpi(page, "Applied")
    not_applied_after = kpi(page, "Not Applied")
    check("Applied KPI went up by one", applied_after == applied_before + 1, f"{applied_before} -> {applied_after}")
    check("Not Applied KPI went down by one", not_applied_after == not_applied_before - 1, f"{not_applied_before} -> {not_applied_after}")

    for path, heading in (("/member/applications", "Applications"), ("/member/activity", "Activity"), ("/member/candidates", "My Candidates"), ("/member/settings", "Settings")):
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        check(f"{path} loads", page.get_by_role("heading", level=1, name=heading).is_visible())
        no_overflow(page, path)
    # a member has no profile or resume of their own: the old address leads to the account settings, where nothing is uploaded
    page.goto(BASE + "/member/profile")
    page.wait_for_load_state("networkidle")
    check("/member/profile only leads to /member/settings", page.url.endswith("/member/settings"), page.url)
    check("the member's settings hold no resume upload and no job-search profile",
          page.locator('input[type="file"]').count() == 0 and page.get_by_role("button", name="Save Profile").count() == 0
          and page.get_by_text("Desired job titles").count() == 0)
    page.goto(BASE + "/member/applications")
    try:
        page.get_by_role("link", name=title).first.wait_for(timeout=15000)
        check("applications list includes the job just applied to", True)
    except Exception:
        check("applications list includes the job just applied to", False, title)
    check("the applications list names the candidate of each application", page.get_by_role("link", name="Jordan Rivera").filter(visible=True).count() >= 1)

    # navigation drawer on small screens / sidebar on wide ones
    open_nav(page)
    for label in ("My Candidates", "All Jobs", "Applications", "Activity", "Settings"):
        check(f"member navigation has: {label}", nav_link(page, label).count() == 1)
    check("member navigation has no 'My Jobs' and no 'Profile' any more", nav_link(page, "My Jobs").count() == 0 and nav_link(page, "Profile").count() == 0)
    nav_link(page, "All Jobs").click()
    page.wait_for_url(re.compile(r"/member/jobs$"), timeout=15000)
    check("navigation works from the " + ("drawer" if MOBILE else "sidebar"), True)

    sign_out(page)
    check("member signed out", page.url.endswith("/login"))
    page.goto(BASE + "/member/dashboard")
    check("after sign-out the dashboard needs a sign-in again", "/login?next=%2Fmember%2Fdashboard" in page.url, page.url)
    ctx.close()


NO_AI_TEXT = "No outside AI service is configured on this server. The resume text and the uploaded file stay on this server."
MANAGER_CONTROLS = re.compile(r"^(Edit Details|Manage Candidate|Move to Another Member|Assign to a Member|Save Profile|Save Details|Delete|Deactivate|Reactivate|"
                              r"Find Jobs for .*|Add Candidate|Add Manually|Add from Salesforce|Refresh from Salesforce|Open in Salesforce.*|Upload Resume.*|Replace Resume.*)$")


def download_headers(page, candidate_id):
    """GET the resume file with the page's own session; returns (status, headers, text)."""
    response = page.request.get(BASE + f"/api/candidates/{candidate_id}/resume/download")
    return response.status, response.headers, response.text() if response.status == 200 else ""


def step_my_candidates(browser):
    print("== member: My Candidates cards and sorts, the candidate page (read-only), resume download")
    actx = browser.new_context()
    apage = actx.new_page()
    login(apage, "admin@example.com")
    samuel = candidate_named(apage, "Samuel Okafor")
    actx.close()

    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "maya@example.com", expect_home="/member/dashboard")
    mine = {c["name"]: c for c in api(page, "/api/candidates?page_size=50")["items"]}
    check("the API gives the member exactly the candidates assigned to them", sorted(mine) == ["Elena Petrova", "Jordan Rivera"], str(sorted(mine)))
    jordan, elena = mine["Jordan Rivera"], mine["Elena Petrova"]

    open_nav(page)
    nav_link(page, "My Candidates").click()
    page.wait_for_url(re.compile(r"/member/candidates$"), timeout=15000)
    cards = page.locator('[data-slot="candidate-card"]').filter(visible=True)
    cards.first.wait_for(timeout=15000)
    names = lambda: [c.get_by_role("heading").inner_text().strip() for c in cards.all()]
    check("My Candidates shows one card per candidate", names() == ["Jordan Rivera", "Elena Petrova"], str(names()))
    check("the default order is newest job first (a candidate without jobs comes last)", page.get_by_role("combobox", name="Sort").inner_text().strip() == "Newest job first")
    card = cards.filter(has_text="Jordan Rivera").first
    text = card.inner_text()
    check("a card shows how to reach the candidate", "jordan.candidate@example.com" in text and jordan["phone"] in text
          and card.get_by_role("link", name=re.compile("jordan.candidate@example.com")).get_attribute("href") == "mailto:jordan.candidate@example.com"
          and card.get_by_role("link", name=re.compile(re.escape(jordan["phone"]))).get_attribute("href").startswith("tel:+1555"))
    check("a card shows what the candidate is looking for", all(t in text for t in ("Machine Learning Engineer, AI Engineer", "3 years", "Green Card / US Permanent Resident", "NY, WA")), text.replace("\n", " | ")[:200])
    figures = [int(v.replace(",", "")) for v in card.locator("dl").last.locator("dd").all_inner_texts()]
    want = [jordan["stats"][k] for k in ("assigned", "not_applied", "applied", "interviews", "offers")]
    check("a card shows the candidate's figures as the API states them", figures == want, f"shown {figures}, API {want}")
    check("a card says whether the resume is in and when the newest job came", card.locator('[data-slot="tag"]').filter(has_text="Resume").is_visible() and "Newest job assigned" in text)
    other = cards.filter(has_text="Elena Petrova").first.inner_text()
    check("a candidate without an e-mail address and without jobs is shown as such", "@" not in other and elena["phone"] in other and "No jobs assigned yet" in other)
    check("the whole card opens the candidate", card.get_by_role("link", name="Jordan Rivera").get_attribute("href") == f"/member/candidates/{jordan['id']}")
    check("the count below the cards is right", page.get_by_text("2 candidates", exact=True).is_visible())
    no_overflow(page, "my candidates")
    shot(page, "my_candidates")

    # sorts are done by the API
    with page.expect_response(lambda r: "/api/candidates?" in r.url and "sort=name" in r.url) as info:
        pick_option(page, page, "Sort", "Name, A to Z")
    expect(cards.first.get_by_role("heading")).to_have_text("Elena Petrova", timeout=10000)
    check("sorting by name asks the API (sort=name) and reorders the cards", info.value.ok and names() == ["Elena Petrova", "Jordan Rivera"] and "sort=name" in page.url, str(names()))
    with page.expect_response(lambda r: "/api/candidates?" in r.url and "sort=not_applied" in r.url) as info:
        pick_option(page, page, "Sort", "Most not applied first")
    expect(cards.first.get_by_role("heading")).to_have_text("Jordan Rivera", timeout=10000)
    check("sorting by 'most not applied' asks the API (sort=not_applied)", info.value.ok and names() == ["Jordan Rivera", "Elena Petrova"], str(names()))
    with page.expect_response(lambda r: "/api/candidates?" in r.url and "q=elena" in r.url):
        page.get_by_role("searchbox", name="Search candidates").fill("elena")
    expect(cards).to_have_count(1, timeout=10000)
    check("the search narrows the cards", names() == ["Elena Petrova"])
    page.get_by_role("searchbox", name="Search candidates").fill("nobody-like-this")
    page.get_by_text("No candidate matches this search").wait_for(timeout=15000)
    check("a search without a match says so and offers to clear it", page.get_by_role("button", name="Clear search").last.is_visible())
    page.get_by_role("button", name="Clear search").last.click()
    expect(cards).to_have_count(2, timeout=10000)

    # the candidate's page
    cards.filter(has_text="Jordan Rivera").first.get_by_role("link", name="Jordan Rivera").click()
    page.wait_for_url(re.compile(rf"/member/candidates/{jordan['id']}"), timeout=15000)
    page.get_by_role("heading", level=1, name="Jordan Rivera").wait_for(timeout=15000)
    page.wait_for_load_state("networkidle")
    tabs = [t.inner_text().strip() for t in page.get_by_role("tab").all()]
    check("the candidate page has the tabs Jobs, Details, Profile, Resume, Activity", tabs == ["Jobs", "Details", "Profile", "Resume", "Activity"], str(tabs))
    check("the candidate page shows the six figures of this candidate", page.locator('[data-slot="kpi"]').filter(visible=True).count() == 6
          and kpi(page, "Total Assigned") == jordan["stats"]["assigned"] and kpi(page, "Applied") == jordan["stats"]["applied"])
    jobs = page.locator('[data-slot="job-card"]').filter(visible=True)
    jobs.first.wait_for(timeout=15000)
    check("Jobs tab: the candidate's jobs as cards, without a candidate filter", jobs.count() > 0 and page.get_by_role("combobox", name="Candidate").count() == 0
          and page.get_by_role("searchbox", name="Search jobs").is_visible())
    check("Jobs tab: a job opened from here knows the way back to the candidate",
          re.search(rf"^/member/jobs/[0-9a-f-]{{36}}\?candidate={jordan['id']}$", jobs.first.get_by_role("link").first.get_attribute("href")) is not None)
    check("the member job view of a candidate carries the watermark", page.locator('[data-slot="watermark"]').filter(visible=True).count() == 1)
    no_overflow(page, "member candidate page, jobs")
    shot(page, "member_candidate_jobs")

    def read_only(tab):
        panel = page.get_by_role("tabpanel").filter(visible=True)
        controls = page.get_by_role("button", name=MANAGER_CONTROLS).filter(visible=True).count()
        inputs = sum(panel.get_by_role(role).count() for role in ("textbox", "combobox", "checkbox", "switch", "radio", "spinbutton"))
        inputs -= panel.get_by_role("navigation").get_by_role("combobox", name="Per page").count()      # paging a list changes nothing
        check(f"{tab} tab is read-only for the member (no edit controls, no fields, no upload)",
              controls == 0 and inputs == 0 and page.locator('input[type="file"]').count() == 0, f"{controls} manager buttons, {inputs} fields")

    page.get_by_role("tab", name="Details").click()
    panel = page.get_by_role("tabpanel").filter(visible=True)
    panel.get_by_text("Kept up to date by your Team Lead.").wait_for(timeout=15000)
    details = panel.inner_text()
    check("Details tab: name, contact, team, responsible member and the Team Lead's notes",
          all(t in details for t in ("Jordan Rivera", "jordan.candidate@example.com", jordan["phone"], "Team Alpha", "Maya Chen", "Prefers employers with a clear remote policy.")),
          details.replace("\n", " | ")[:260])
    read_only("Details")
    no_overflow(page, "member candidate page, details")

    page.get_by_role("tab", name="Profile").click()
    panel = page.get_by_role("tabpanel").filter(visible=True)
    panel.get_by_text("Desired job titles").wait_for(timeout=15000)
    profile = panel.inner_text()
    check("Profile tab: the candidate's job-search profile as facts",
          all(t in profile for t in ("Machine Learning Engineer", "AI Engineer", "3 years", "Green Card / US Permanent Resident", "NY", "WA", "Skills (4)", "PyTorch", "Left out of searches")),
          profile.replace("\n", " | ")[:300])
    read_only("Profile")
    no_overflow(page, "member candidate page, profile")

    page.get_by_role("tab", name="Resume").click()
    panel = page.get_by_role("tabpanel").filter(visible=True)
    link = panel.get_by_role("link", name="Download File")
    link.wait_for(timeout=15000)
    check("Resume tab: the file, with a download link to the API", "jordan-resume.txt" in panel.inner_text()
          and link.get_attribute("href") == f"/api/candidates/{jordan['id']}/resume/download" and link.get_attribute("download") == "jordan-resume.txt")
    check("Resume tab: the text of the resume to copy from", "Jordan Rivera" in panel.get_by_role("region", name="Resume text").inner_text())
    check("Resume tab: no notice about uploading, since a member uploads nothing", panel.get_by_text(re.compile("outside AI service|text from this resume")).count() == 0)
    read_only("Resume")
    with page.expect_download(timeout=20000) as download:
        link.click()
    check("Download File saves the file under its own name", download.value.suggested_filename == "jordan-resume.txt", download.value.suggested_filename)
    with open(download.value.path(), encoding="utf-8") as handle:
        saved = handle.read()
    check("the downloaded file is the candidate's resume", saved.startswith("Jordan Rivera") and len(saved) > 200, f"{len(saved)} characters")
    status, headers, body = download_headers(page, jordan["id"])
    check("resume download: 200 for the responsible member, with the stored content", status == 200 and body == saved, f"HTTP {status}")
    check("resume download: sent as an attachment with a quoted file name", headers.get("content-disposition", "") == 'attachment; filename="jordan-resume.txt"', headers.get("content-disposition", ""))
    check("resume download: X-Content-Type-Options is nosniff", headers.get("x-content-type-options") == "nosniff", str(headers.get("x-content-type-options")))
    check("resume download: never cached, and a content type fixed by the server", "no-store" in headers.get("cache-control", "") and headers.get("content-type", "").startswith("text/plain"),
          f"{headers.get('cache-control')} / {headers.get('content-type')}")
    no_overflow(page, "member candidate page, resume")
    shot(page, "member_candidate_resume")

    page.get_by_role("tab", name="Activity").click()
    panel = page.get_by_role("tabpanel").filter(visible=True)
    feed = panel.get_by_role("list", name="Activity, newest first")
    feed.get_by_role("listitem").first.wait_for(timeout=15000)
    said = feed.inner_text()
    check("Activity tab: what happened to the candidate's jobs, the member's own steps as 'You ...'",
          panel.locator('[data-slot="error-state"]').count() == 0 and feed.get_by_role("listitem").count() > 3
          and re.search(r"You (viewed the job details|opened the job posting|changed the status)", said) is not None and "Lena Alpha assigned this job" in said,
          said.replace("\n", " | ")[:200])
    check("Activity tab: each entry leads to the job, with the way back to the candidate",
          re.search(rf"^/member/jobs/[0-9a-f-]{{36}}\?candidate={jordan['id']}$", feed.get_by_role("link").first.get_attribute("href") or "") is not None)
    read_only("Activity")
    no_overflow(page, "member candidate page, activity")
    check("the tab is kept in the address", page.url.endswith("?tab=activity"), page.url)

    # the API, not only the screen, refuses a member any change to a candidate
    cid = jordan["id"]
    with expecting("403", "/api/candidates"):
        refused = {
            "PATCH details": send(page, "PATCH", f"/api/candidates/{cid}", {"notes": "changed by the member"}),
            "PATCH responsible member": send(page, "PATCH", f"/api/candidates/{cid}", {"assigned_member_id": None}),
            "PATCH profile": send(page, "PATCH", f"/api/candidates/{cid}/profile", {"seniority": "Senior"}),
            "DELETE resume": send(page, "DELETE", f"/api/candidates/{cid}/resume"),
            "POST candidate": send(page, "POST", "/api/candidates", {"name": "Made By A Member"}),
        }
        page.wait_for_timeout(300)
    check("the API answers 403 to every change a member tries on a candidate", set(refused.values()) == {403}, str(refused))
    check("and nothing changed", api(page, f"/api/candidates/{cid}")["notes"] == "Prefers employers with a clear remote policy." and api(page, f"/api/candidates/{cid}")["has_resume"] is True)

    # a candidate of another member of the same team: answered like one that does not exist
    with expecting("404", f"/api/candidates/{samuel['id']}"):
        with page.expect_response(lambda r: r.url.endswith(f"/api/candidates/{samuel['id']}")) as info:
            page.goto(BASE + f"/member/candidates/{samuel['id']}")
        page.locator('[data-slot="error-state"]').filter(visible=True).first.wait_for(timeout=15000)
        page.wait_for_load_state("networkidle")
        hidden = [download_headers(page, samuel["id"])[0]] + [api(page, f"/api/candidates/{samuel['id']}/{part}").get("__status") for part in ("profile", "resume", "activity", "analytics")]
        page.wait_for_timeout(300)
    body = page.locator("main").inner_text()
    check("a colleague's candidate is answered 404 everywhere (record, profile, resume, file, activity, figures)", info.value.status == 404 and set(hidden) == {404}, str(hidden))
    check("the screen says the candidate is not assigned to the member and shows nothing of them",
          "This candidate is not assigned to you" in body and "Samuel" not in body and page.get_by_role("link", name="Go to My Candidates").get_attribute("href") == "/member/candidates")
    no_overflow(page, "member opening a candidate that is not theirs")
    ctx.close()


def names_listed(page, known, want, timeout=10000):
    """The candidates of `known` that the list shows; waits until it is `want` (the list reloads after a filter change)."""
    deadline = time.time() + timeout / 1000
    while True:
        shown = {n for n in known if page.get_by_role("link", name=n, exact=True).filter(visible=True).count() > 0}
        if shown == set(want) or time.time() > deadline:
            return shown
        page.wait_for_timeout(200)


def step_candidates(browser):
    print("== candidates: Add Candidate with and without a resume, list filters, profile and resume, download, assign and move")
    resume_path = f"{WORK_DIR}/resume-{STAMP}.txt"
    with open(resume_path, "w") as handle:
        handle.write(RESUME_TEXT)
    bad_path = resume_path.replace(".txt", ".png")
    with open(bad_path, "wb") as handle:
        handle.write(b"not a resume")
    bare, full = f"Blake Bare {STAMP}", f"Casey Complete {STAMP}"

    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com", expect_home="/tl/dashboard")
    people = {u["name"]: u for u in api(page, "/api/users?role=MEMBER&page_size=50")["items"]}
    open_nav(page)
    nav_link(page, "Candidates").click()
    page.wait_for_url(re.compile(r"/tl/candidates$"), timeout=15000)
    page.get_by_role("link", name="Jordan Rivera").filter(visible=True).first.wait_for(timeout=15000)
    # Amara Nwosu (Team Alpha) and Elif Demir (Team Beta) were brought in from the fake Salesforce when the data was seeded.
    seeded = ["Jordan Rivera", "Elena Petrova", "Samuel Okafor", "Hannah Kim", "Diego Morales", "Amara Nwosu"]
    foreign = ["Aisha Rahman", "Tom Becker", "Grace Liu", "Lucas Meyer", "Victor Hale", "Elif Demir"]
    check("the Team Lead's candidate list holds the own team's candidates only", names_listed(page, seeded + foreign, seeded) == set(seeded))
    if not MOBILE:
        page.get_by_role("columnheader", name="Candidate").wait_for(timeout=15000)
        heads = [h.inner_text().strip() for h in page.get_by_role("columnheader").all()]
        check("the list has the columns Candidate, Responsible member, Resume and the job figures",
              heads[:7] == ["Candidate", "Responsible member", "Resume", "Jobs assigned", "Applied", "Interviews", "Newest job"], str(heads))
    row = page.locator("tr, li").filter(has=page.get_by_role("link", name="Diego Morales", exact=True)).filter(visible=True).last
    check("a row shows the resume state and who is responsible", "No resume" in row.inner_text() and "Unassigned" in row.inner_text(), row.inner_text().replace("\n", " | ")[:160])
    no_overflow(page, "tl candidates")
    shot(page, "tl_candidates")

    # ---- no candidate is typed in by hand while the portal is connected to Salesforce
    page.get_by_role("button", name="Add from Salesforce", exact=True).filter(visible=True).first.wait_for(timeout=15000)
    check("connected to Salesforce: the Candidates page offers 'Add from Salesforce' and nothing to type a candidate in with",
          page.get_by_role("button", name=re.compile("^Add (Candidate|Manually)")).count() == 0)
    # ---- Add Candidate (a portal without Salesforce), without a resume: the rules of the API are checked before anything is sent
    posts = []
    page.on("request", lambda r: posts.append(r) if r.method == "POST" and "/api/candidates" in r.url else None)
    manual = without_salesforce(page)
    manual.__enter__()                                   # until both candidates of this step are added (left below)
    page.goto(BASE + "/tl/candidates")
    page.get_by_role("button", name="Add Candidate", exact=True).filter(visible=True).first.click()
    dialog = page.get_by_role("dialog", name="Add Candidate")
    dialog.wait_for(timeout=5000)
    check("the dialog says a candidate has no account and receives nothing", dialog.get_by_text("A candidate has no account and receives nothing from the portal.").is_visible())
    dialog.get_by_role("button", name="Add Candidate").click()
    check("a candidate without a name is refused in the form", dialog.get_by_role("alert").filter(visible=True).count() == 1)
    dialog.get_by_label("Full name").fill("Blake <b>http://x.example</b>")
    dialog.get_by_label(re.compile("^E-mail")).fill("not-an-address")
    dialog.get_by_label(re.compile("^Phone")).fill("call me maybe!")
    dialog.get_by_role("button", name="Add Candidate").click()
    check("a name that could carry a link, a wrong e-mail address and a wrong phone number are each refused on their field",
          dialog.get_by_role("alert").filter(has_text=re.compile("letters, digits, spaces")).first.is_visible()
          and dialog.get_by_role("alert").filter(has_text="Enter a valid e-mail address, or leave it empty.").is_visible()
          and dialog.get_by_role("alert").filter(has_text="Use at most 40 characters: digits, spaces and + ( ) - . / # x only.").is_visible())
    check("nothing was sent while the form had problems", len(posts) == 0, f"{len(posts)} requests")
    dialog.get_by_label("Full name").fill(bare)
    dialog.get_by_label(re.compile("^E-mail")).fill("")
    dialog.get_by_label(re.compile("^Phone")).fill("")
    dialog.get_by_role("combobox", name=re.compile("Responsible member")).click()
    offered = [o.inner_text().strip() for o in page.get_by_role("option").all()]
    check("the responsible member is chosen among the active members of the own team, or nobody",
          offered[0] == "Nobody yet (unassigned)" and {"Maya Chen", "Noah Patel", "Omar Haddad"} <= set(offered) and not {"Priya Nair", "Quinn Lopez", "Lena Alpha"} & set(offered), str(offered))
    page.keyboard.press("Escape")
    check("the resume is optional, and no notice about it is shown before a file is chosen",
          dialog.get_by_role("button", name="Choose a File").is_visible() and dialog.get_by_text(re.compile("outside AI service")).count() == 0)
    if MOBILE:
        box = dialog.bounding_box()
        check("390 px: the Add Candidate dialog fits the screen", box["x"] >= 0 and box["x"] + box["width"] <= A.w + 0.5 and box["y"] + box["height"] <= A.h + 0.5, str(box))
    shot(page, "add_candidate")
    with page.expect_response(lambda r: r.url.endswith("/api/candidates") and r.request.method == "POST") as info:
        dialog.get_by_role("button", name="Add Candidate").click()
    sent = info.value.request.post_data_json
    check("the candidate is created with what was entered (no team from the browser for a Team Lead, nobody responsible)",
          info.value.status == 201 and sent == {"name": bare, "email": None, "phone": "", "notes": "", "team_id": None, "assigned_member_id": None}, str(sent))
    toast(page, f"{bare} added")
    page.wait_for_url(re.compile(r"/tl/candidates/[0-9a-f-]{36}\?tab=profile$"), timeout=15000)
    bare_id = page.url.rsplit("/", 1)[1].split("?")[0]
    page.get_by_role("heading", level=1, name=bare).wait_for(timeout=15000)
    page.wait_for_load_state("networkidle")
    record = api(page, f"/api/candidates/{bare_id}")
    check("without a resume: the new candidate opens on the profile, marked 'No resume', in the Team Lead's team, unassigned",
          page.locator('[data-slot="tag"]').filter(has_text="No resume").first.is_visible() and page.get_by_text("nobody yet: choose a responsible member").is_visible()
          and record["team"]["name"] == "Team Alpha" and record["assigned_member"] is None and record["has_resume"] is False and record["created_by"]["name"] == "Lena Alpha")
    check("without a resume: no upload was attempted", not any("/resume" in r.url for r in posts), str([r.url[-40:] for r in posts]))

    # ---- Add Candidate, with a resume and a responsible member
    page.goto(BASE + "/tl/candidates")
    page.get_by_role("button", name="Add Candidate", exact=True).filter(visible=True).first.click()
    dialog = page.get_by_role("dialog", name="Add Candidate")
    dialog.get_by_label("Full name").fill(full)
    dialog.get_by_label(re.compile("^E-mail")).fill(f"casey.{STAMP}@example.com")
    dialog.get_by_label(re.compile("^Phone")).fill("+1 (555) 010-7788 x12")
    dialog.get_by_label(re.compile("^Notes")).fill("Available from next month.\nPrefers mornings.")
    pick_option(page, dialog, re.compile("Responsible member"), "Omar Haddad")
    dialog.locator('input[type="file"]').set_input_files(bad_path)
    check("a file that is not PDF, DOCX or TXT is refused in the dialog", dialog.get_by_role("alert").filter(has_text="Choose a PDF, DOCX or TXT file.").is_visible()
          and dialog.get_by_role("button", name="Choose a File").is_visible())
    dialog.locator('input[type="file"]').set_input_files(resume_path)
    dialog.get_by_text(f"resume-{STAMP}.txt").wait_for(timeout=5000)
    check("the chosen file is named with its size and can be taken out again", dialog.get_by_role("button", name=f"Remove resume-{STAMP}.txt").is_visible()
          and dialog.get_by_role("alert").count() == 0)
    notice = dialog.get_by_role("status").filter(has_text="outside AI service")
    check("once a file is chosen the dialog says where the resume goes (no AI service on the demo stack)", notice.count() == 1 and notice.inner_text().strip() == NO_AI_TEXT)
    with page.expect_response(lambda r: r.url.endswith("/resume") and r.request.method == "POST") as info:
        dialog.get_by_role("button", name="Add Candidate").click()
    full_id = info.value.url.split("/api/candidates/")[1].split("/")[0]
    check("with a resume: the file is uploaded to the new candidate right after it is created", info.value.status == 201 and info.value.json()["has_file"] is True, f"HTTP {info.value.status}")
    toast(page, f"resume-{STAMP}.txt uploaded.")
    page.wait_for_url(re.compile(rf"/tl/candidates/{full_id}\?tab=profile$"), timeout=15000)
    page.get_by_role("heading", level=1, name=full).wait_for(timeout=15000)
    page.wait_for_load_state("networkidle")
    record = api(page, f"/api/candidates/{full_id}")
    check("with a resume: the candidate has resume, file, contact details, notes and the responsible member",
          record["has_resume"] is True and record["resume_has_file"] is True and record["assigned_member"]["name"] == "Omar Haddad"
          and record["email"] == f"casey.{STAMP}@example.com" and record["phone"] == "+1 (555) 010-7788 x12" and record["notes"] == "Available from next month.\nPrefers mornings.")
    check("the candidate's page says who handles the jobs", page.locator('[data-slot="tag"]').filter(has_text="Resume uploaded").first.is_visible()
          and page.get_by_role("link", name="Omar Haddad").filter(visible=True).first.get_attribute("href") == f"/tl/members/{people['Omar Haddad']['id']}")
    manual.__exit__(None, None, None)                    # from here on the portal is connected to Salesforce again
    check("a candidate is not an account: no user with that name or address exists", api(page, f"/api/users?q=casey.{STAMP}&page_size=5")["total"] == 0
          and api(page, f"/api/users?q=Casey%20Complete&page_size=5")["total"] == 0)
    SHARED.update(candidate=full, candidate_id=full_id)

    # ---- list filters, each done by the API
    page.goto(BASE + "/tl/candidates")
    page.get_by_role("link", name="Jordan Rivera").filter(visible=True).first.wait_for(timeout=15000)
    known = seeded + [bare, full]
    everyone = api(page, "/api/candidates?page_size=200")["items"]

    def filtered(combobox, option, param, want):
        with page.expect_response(lambda r: "/api/candidates?" in r.url and param in r.url) as info:
            pick_option(page, page, combobox, option)
        return info.value.ok, names_listed(page, known, want)

    want = {c["name"] for c in everyone if c["assigned_member"] is None}
    ok, shown = filtered("Responsible member", "Unassigned", "unassigned=true", want)
    check("filter 'Unassigned': sent as unassigned=true, lists the candidates nobody is responsible for",
          ok and shown == want == {"Hannah Kim", "Diego Morales", "Amara Nwosu", bare} and "member=unassigned" in page.url, str(sorted(shown)))
    want = {c["name"] for c in everyone if c["assigned_member"] is None and not c["has_resume"]}
    ok, shown = filtered("Resume", "No resume", "has_resume=false", want)
    check("filter 'No resume' on top of it: sent as has_resume=false", ok and shown == want == {"Diego Morales", bare}, str(sorted(shown)))
    page.get_by_role("button", name="Clear filters").click()
    names_listed(page, known, known)
    want = {c["name"] for c in everyone if c["has_resume"]}
    ok, shown = filtered("Resume", "Resume uploaded", "has_resume=true", want)
    check("filter 'Resume uploaded': sent as has_resume=true", ok and shown == want and len(want) == 6, str(sorted(shown)))
    page.get_by_role("button", name="Clear filters").click()
    names_listed(page, known, known)
    page.get_by_role("combobox", name="Responsible member").click()
    offered = [o.inner_text().strip() for o in page.get_by_role("option").all()]
    check("the member filter offers 'Unassigned' and the members of the own team", offered[:2] == ["Any responsible member", "Unassigned"]
          and {"Maya Chen", "Noah Patel", "Omar Haddad"} <= set(offered) and "Priya Nair" not in offered, str(offered))
    with page.expect_response(lambda r: "/api/candidates?" in r.url and f"assigned_member_id={people['Maya Chen']['id']}" in r.url) as info:
        page.get_by_role("option", name="Maya Chen", exact=True).click()
    shown = names_listed(page, known, {"Jordan Rivera", "Elena Petrova"})
    check("filter by member: sent as assigned_member_id, lists that member's candidates", info.value.ok and shown == {"Jordan Rivera", "Elena Petrova"}, str(sorted(shown)))
    page.get_by_role("button", name="Clear filters").click()
    names_listed(page, known, known)
    with page.expect_response(lambda r: "/api/candidates?" in r.url and "q=%2B1+%28555%29+010" in r.url.replace("%20", "+")):
        page.get_by_role("searchbox", name="Search candidates").fill("+1 (555) 010")
    shown = names_listed(page, known, {full})
    check("the search finds a candidate by phone number", shown == {full}, str(sorted(shown)))
    page.get_by_role("searchbox", name="Search candidates").fill("")
    names_listed(page, known, known)
    with page.expect_response(lambda r: "/api/candidates?" in r.url and "sort=not_applied" in r.url) as info:
        pick_option(page, page, "Sort", "Most not applied first")
    first = api(page, "/api/candidates?sort=not_applied&active=true&page_size=1")["items"][0]["name"]
    expect(page.locator("tbody tr, ul > li").filter(has=page.get_by_role("link", name=re.compile(r"^(%s)$" % "|".join(map(re.escape, known))))).filter(visible=True).first
           .get_by_role("link").first).to_have_text(first, timeout=10000)
    check("sorting is done by the API (sort=not_applied)", info.value.ok, first)
    no_overflow(page, "tl candidates, filtered and sorted")

    # ---- the candidate's page: resume and profile (the candidate added without a resume)
    page.goto(BASE + f"/tl/candidates/{bare_id}?tab=profile")
    page.get_by_text(f"Drop {bare}'s resume here, or choose a file").wait_for(timeout=15000)
    check("the Team Lead uploads the candidate's resume on the candidate's page", True)
    notice = page.get_by_role("status").filter(has_text="outside AI service").filter(visible=True)
    check("the notice about outside AI services is shown on the candidate's resume panel (none configured on the demo stack)",
          notice.count() == 1 and notice.first.inner_text().strip() == NO_AI_TEXT)
    page.get_by_role("progressbar", name="Profile completion").wait_for(timeout=15000)
    before = int(page.get_by_role("progressbar", name="Profile completion").get_attribute("aria-valuenow"))
    check("the profile shows what is still missing", page.get_by_text("Add at least one desired job title").first.is_visible(), f"{before}% complete")
    page.locator('input[type="file"]').first.set_input_files(resume_path)
    toast(page, "uploaded (version 1)")
    check("resume upload (multipart, through the /api proxy) succeeds", True)
    page.get_by_text("Skills found in the resume").wait_for(timeout=15000)
    check("extracted skills are shown for review", page.locator('[data-slot="tag"]').filter(has_text="Python").first.is_visible())
    check("the page now says 'Resume uploaded' and offers the file", page.locator('[data-slot="tag"]').filter(has_text="Resume uploaded").first.is_visible()
          and page.get_by_role("link", name="Download File").get_attribute("href") == f"/api/candidates/{bare_id}/resume/download")
    try:
        page.get_by_role("button", name="Remove Python").first.wait_for(timeout=10000)
        check("skills the API took from the resume appear in the open profile form", True)
    except Exception:
        check("skills the API took from the resume appear in the open profile form", False)
    no_overflow(page, "candidate detail, profile tab")
    shot(page, "candidate_detail_profile")
    page.locator('input[type="file"]').first.set_input_files(bad_path)
    check("a file that is not PDF, DOCX or TXT is refused with a clear message", page.get_by_role("alert").filter(has_text="Choose a PDF, DOCX or TXT file.").first.is_visible())

    titles = page.get_by_label(re.compile("^Desired job titles"))
    titles.fill("QA Engineer, Test Engineer")
    titles.press("Enter")
    page.get_by_label("Years of experience").fill("5")
    states = page.get_by_label(re.compile("^Preferred states"))
    states.fill("new york")
    states.press("Enter")
    with page.expect_response(lambda r: r.url.endswith(f"/api/candidates/{bare_id}/profile") and r.request.method == "PATCH") as info:
        page.get_by_role("button", name="Save Profile").click()
    toast(page, "Profile saved")
    expect(page.get_by_role("progressbar", name="Profile completion")).to_have_attribute("aria-valuenow", "100", timeout=15000)
    check("saving the candidate's profile raises the completion meter", info.value.status == 200, f"{before}% -> 100%")
    check("a state name is stored as its code", page.get_by_role("button", name="Remove NY").is_visible())
    check("the candidate record carries the completion and the summary used on the member's card",
          api(page, f"/api/candidates/{bare_id}")["profile_completion"] == 100 and api(page, f"/api/candidates/{bare_id}")["profile_summary"]["desired_titles"] == ["QA Engineer", "Test Engineer"])

    # drag and drop a new resume
    page.evaluate(
        """([text]) => {
            const zone = document.querySelector('label:has(input[type=file])');
            const data = new DataTransfer();
            data.items.add(new File([text], 'dropped-resume.txt', { type: 'text/plain' }));
            zone.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: data }));
            zone.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: data }));
        }""",
        [RESUME_TEXT + "\nGo, Rust\n"],
    )
    toast(page, "dropped-resume.txt uploaded (version 2)")
    check("a resume can be dropped onto the upload area (replaces the previous one)", True)
    page.get_by_text("dropped-resume.txt").first.wait_for(timeout=15000)

    # the file: a download, never something the browser renders
    link = page.get_by_role("link", name="Download File")
    with page.expect_download(timeout=20000) as download:
        link.click()
    with open(download.value.path(), encoding="utf-8") as handle:
        saved = handle.read()
    check("Download File gives back the file that was uploaded last", download.value.suggested_filename == "dropped-resume.txt" and saved == RESUME_TEXT + "\nGo, Rust\n",
          download.value.suggested_filename)
    status, headers, body = download_headers(page, bare_id)
    check("resume download (Team Lead): attachment with the file name, nosniff, no-store",
          status == 200 and headers.get("content-disposition") == 'attachment; filename="dropped-resume.txt"' and headers.get("x-content-type-options") == "nosniff"
          and "no-store" in headers.get("cache-control", ""), str({k: headers.get(k) for k in ("content-disposition", "x-content-type-options", "cache-control", "content-type")}))
    anonymous = browser.new_context()
    refused = anonymous.request.get(BASE + f"/api/candidates/{bare_id}/resume/download", max_redirects=0)
    check("resume download without a session is refused", refused.status == 401 and "Erin Example" not in refused.text(), f"HTTP {refused.status}")
    anonymous.close()

    for tab in ("Overview", "Jobs", "Activity", "Searches"):
        page.get_by_role("tab", name=tab, exact=True).click()
        page.wait_for_load_state("networkidle")
        check(f"candidate detail tab renders: {tab}", page.locator('[data-slot="error-state"]').filter(visible=True).count() == 0)
    page.get_by_role("tab", name="Activity", exact=True).click()
    try:
        page.get_by_role("tabpanel").filter(visible=True).get_by_text(re.compile("No .*(activity|happened)|Nothing", re.I)).first.wait_for(timeout=10000)
        check("a candidate without jobs has an empty activity, said in words", True)
    except Exception:
        check("a candidate without jobs has an empty activity, said in words", False, page.get_by_role("tabpanel").filter(visible=True).inner_text()[:120])
    no_overflow(page, "candidate detail, activity tab")

    # delete the resume again
    page.get_by_role("tab", name="Profile and Resume").click()
    page.get_by_role("button", name="Delete", exact=True).click()
    confirm = page.get_by_role("alertdialog", name="Delete this resume?")
    check("deleting a resume asks first and says the file goes too", confirm.get_by_text(re.compile("The stored text and the uploaded file are removed")).is_visible())
    confirm.get_by_role("button", name="Delete resume").click()
    page.get_by_text(f"Drop {bare}'s resume here, or choose a file").wait_for(timeout=15000)
    with expecting("404", f"/api/candidates/{bare_id}/resume/download"):
        gone = download_headers(page, bare_id)[0]
    check("after deleting, the candidate has no resume and the file is gone (404)",
          api(page, f"/api/candidates/{bare_id}")["has_resume"] is False and gone == 404 and page.get_by_role("link", name="Download File").count() == 0, f"HTTP {gone}")

    # ---- assign, move, unassign: the member's access follows at once
    octx, mctx = new_context(browser), new_context(browser)
    opage, mpage = octx.new_page(), mctx.new_page()
    login(opage, "omar@example.com")
    login(mpage, "maya@example.com")
    bell = opage.get_by_role("button", name=re.compile("Notifications"))
    check("the member who received a candidate has an unread notification", "unread" in (bell.get_attribute("aria-label") or ""))
    told = bell_text(opage, f"Lena Alpha made you responsible for the candidate {full}.")
    check("notification 'candidate assigned': who made the member responsible, and for whom", True)
    told.click()
    opage.wait_for_url(re.compile(rf"/member/candidates/{full_id}$"), timeout=15000)
    opage.get_by_role("heading", level=1, name=full).wait_for(timeout=15000)
    check("the notification opens the candidate's page", True)
    with expecting("404", f"/api/candidates/{full_id}"):
        maya_sees = api(mpage, f"/api/candidates/{full_id}").get("__status")
        mpage.wait_for_timeout(300)
    check("the responsible member can open the candidate; a colleague cannot (404)", api(opage, f"/api/candidates/{full_id}")["name"] == full and maya_sees == 404, f"colleague: {maya_sees}")

    page.goto(BASE + f"/tl/candidates/{full_id}")
    page.get_by_role("heading", level=1, name=full).wait_for(timeout=15000)
    page.get_by_role("button", name="Move to Another Member").click()
    dialog = page.get_by_role("dialog", name="Move to Another Member")
    check("the move dialog says who is responsible now and that jobs and history stay with the candidate",
          dialog.get_by_text(re.compile(rf"Omar Haddad is responsible for {re.escape(full)} now\.")).is_visible()
          and dialog.get_by_text(re.compile("jobs, notes and history stay with the candidate, and the new member is notified")).is_visible()
          and dialog.get_by_text("Active members of Team Alpha.").is_visible())
    check("nothing to save until another member is chosen", dialog.get_by_role("button", name="Move Candidate").is_disabled())
    dialog.get_by_role("combobox", name=re.compile("Responsible member")).click()
    offered = [o.inner_text().strip() for o in page.get_by_role("option").all()]
    check("only members of the candidate's own team are offered", {"Maya Chen", "Noah Patel", "Omar Haddad"} <= set(offered) and not {"Priya Nair", "Quinn Lopez"} & set(offered), str(offered))
    page.get_by_role("option", name="Maya Chen", exact=True).click()
    with page.expect_response(lambda r: r.url.endswith(f"/api/candidates/{full_id}") and r.request.method == "PATCH") as info:
        dialog.get_by_role("button", name="Move Candidate").click()
    check("moving sends the new responsible member and nothing else", info.value.status == 200 and info.value.request.post_data_json == {"assigned_member_id": people["Maya Chen"]["id"]},
          str(info.value.request.post_data_json))
    toast(page, f"Maya Chen is now responsible for {full}")
    with expecting("404", f"/api/candidates/{full_id}"):
        omar_sees = api(opage, f"/api/candidates/{full_id}").get("__status")
        omar_file = download_headers(opage, full_id)[0]
        opage.reload()
        opage.locator('[data-slot="error-state"]').filter(visible=True).first.wait_for(timeout=15000)
        opage.wait_for_load_state("networkidle")
        opage.wait_for_timeout(300)
    check("after the move the previous member has lost the candidate at once (record and resume file 404)", omar_sees == 404 and omar_file == 404, f"{omar_sees}, {omar_file}")
    check("the previous member's open page turns into 'not assigned to you' and shows nothing of the candidate",
          opage.get_by_text("This candidate is not assigned to you").is_visible() and "Casey" not in opage.locator("main").inner_text())
    check("the new member has gained the candidate without signing in again", api(mpage, f"/api/candidates/{full_id}")["assigned_member"]["name"] == "Maya Chen"
          and download_headers(mpage, full_id)[0] == 200)
    mpage.goto(BASE + "/member/candidates")
    mpage.locator('[data-slot="candidate-card"]').filter(has_text=full).wait_for(timeout=15000)
    check("the candidate is on the new member's My Candidates", mpage.locator('[data-slot="candidate-card"]').filter(visible=True).count() == 3)
    opage.goto(BASE + "/member/candidates")
    opage.get_by_text("Your Team Lead has not assigned a candidate to you yet").first.wait_for(timeout=15000)
    check("and gone from the previous member's, which is empty again", opage.locator('[data-slot="candidate-card"]').count() == 0)
    told = bell_text(mpage, f"Lena Alpha made you responsible for the candidate {full}.")
    check("the new member is notified about the candidate", True)
    mpage.keyboard.press("Escape")

    # unassign from the candidate's page
    page.get_by_role("button", name="Move to Another Member").click()
    dialog = page.get_by_role("dialog", name="Move to Another Member")
    pick_option(page, dialog, re.compile("Responsible member"), "Nobody yet (unassigned)")
    dialog.get_by_role("button", name="Unassign Candidate").click()
    toast(page, f"{full} is unassigned")
    with expecting("404", f"/api/candidates/{full_id}"):
        maya_sees = api(mpage, f"/api/candidates/{full_id}").get("__status")
        mpage.wait_for_timeout(300)
    check("an unassigned candidate is nobody's: the last member loses it too", maya_sees == 404 and api(page, f"/api/candidates/{full_id}")["assigned_member"] is None, str(maya_sees))
    page.get_by_text("nobody yet: choose a responsible member").wait_for(timeout=15000)
    check("the candidate's page says nobody is responsible and offers 'Assign to a Member'", page.get_by_role("button", name="Assign to a Member").is_visible())
    mctx.close()
    octx.close()

    # from the list, under the 'Unassigned' filter: the row menu, Edit Details, Assign to a Member
    page.goto(BASE + "/tl/candidates?member=unassigned")
    page.get_by_role("button", name=f"Actions for {full}").filter(visible=True).first.click()
    items = [i.inner_text().strip() for i in page.get_by_role("menuitem").all()]
    check("the row menu of a candidate: Find Jobs, Edit Details, Assign to a Member, Deactivate", items == ["Find Jobs", "Edit Details", "Assign to a Member", "Deactivate"], str(items))
    page.get_by_role("menuitem", name="Edit Details").click()
    dialog = page.get_by_role("dialog", name="Edit Details")
    check("Edit Details has nothing to save until something changes", dialog.get_by_role("button", name="Save Details").is_disabled())
    dialog.get_by_label(re.compile("^E-mail")).fill("")
    dialog.get_by_label(re.compile("^Notes")).fill("Available now.")
    with page.expect_response(lambda r: r.url.endswith(f"/api/candidates/{full_id}") and r.request.method == "PATCH") as info:
        dialog.get_by_role("button", name="Save Details").click()
    check("Edit Details sends only what changed; an emptied e-mail field removes the address", info.value.status == 200
          and info.value.request.post_data_json == {"email": None, "notes": "Available now."}, str(info.value.request.post_data_json))
    toast(page, "Details saved")
    receiver = SHARED.get("name", "Omar Haddad")       # the member invited earlier in the run, when there is one
    page.get_by_role("button", name=f"Actions for {full}").filter(visible=True).first.click()
    page.get_by_role("menuitem", name="Assign to a Member").click()
    dialog = page.get_by_role("dialog", name="Assign to a Member")
    check("the assign dialog says nobody is responsible yet", dialog.get_by_text(re.compile(rf"Nobody is responsible for {re.escape(full)} yet\.")).is_visible())
    pick_option(page, dialog, re.compile("Responsible member"), receiver)
    dialog.get_by_role("button", name="Assign Candidate").click()
    # The candidate leaves the 'Unassigned' list with this change; the confirmation must appear all the same.
    try:
        toast(page, f"{receiver} is now responsible for {full}")
        check("assigning from the 'Unassigned' list is confirmed although the row leaves the list", True)
    except Exception:
        check("assigning from the 'Unassigned' list is confirmed although the row leaves the list", False, "no toast")
    shown = names_listed(page, known, {"Hannah Kim", "Diego Morales", "Amara Nwosu", bare})
    check("the candidate is no longer listed as unassigned", shown == {"Hannah Kim", "Diego Morales", "Amara Nwosu", bare} and page.get_by_role("dialog").count() == 0, str(sorted(shown)))
    page.get_by_role("button", name="Clear filters").click()
    page.get_by_role("searchbox", name="Search candidates").fill(full)
    row = page.locator("tr, li").filter(has=page.get_by_role("link", name=full, exact=True)).filter(visible=True).last
    expect(row).to_contain_text(receiver, timeout=10000)
    check("the list shows the new responsible member in the candidate's row", True)

    # ---- deactivate and reactivate a candidate
    page.get_by_role("searchbox", name="Search candidates").fill(bare)
    page.get_by_role("button", name=f"Actions for {bare}").filter(visible=True).first.click()
    page.get_by_role("menuitem", name="Deactivate").click()
    confirm = page.get_by_role("alertdialog", name=f"Deactivate {bare}?")
    check("deactivating a candidate asks first and says what stays", confirm.get_by_text(re.compile("No new search can be run and no new job can be assigned")).is_visible())
    confirm.get_by_role("button", name="Deactivate candidate").click()
    # The row leaves the list of active candidates with this change; the confirmation must appear all the same.
    try:
        toast(page, f"{bare} was deactivated")
        check("deactivating a candidate is confirmed although the row leaves the list", True)
    except Exception:
        check("deactivating a candidate is confirmed although the row leaves the list", False, "no toast")
    page.get_by_text("No candidates match these filters").wait_for(timeout=15000)
    check("a deactivated candidate leaves the list of active candidates", api(page, f"/api/candidates/{bare_id}")["is_active"] is False)
    pick_option(page, page, "Active or deactivated", "Deactivated candidates")
    page.get_by_role("link", name=bare, exact=True).filter(visible=True).first.wait_for(timeout=15000)
    row = page.locator("tr, li").filter(has=page.get_by_role("link", name=bare, exact=True)).filter(visible=True).last
    check("it is listed under 'Deactivated candidates', marked as deactivated", row.locator('[data-slot="tag"]').filter(has_text="Deactivated").is_visible())
    spage = ctx.new_page()
    spage.goto(BASE + "/tl/search")
    spage.get_by_role("group", name=PICKER, exact=True).get_by_role("checkbox").first.wait_for(timeout=15000)
    spage.get_by_role("searchbox", name="Search candidates").fill(bare)
    spage.get_by_text("Nobody in the portal matches this search").wait_for(timeout=15000)
    check("a deactivated candidate is not offered in the search form", True)
    spage.goto(BASE + f"/tl/candidates/{bare_id}")
    spage.get_by_role("heading", level=1, name=bare).wait_for(timeout=15000)
    check("the page of a deactivated candidate says so and cannot start a search",
          spage.locator('[data-slot="tag"]').filter(has_text="Deactivated").first.is_visible() and spage.get_by_role("button", name="Find Jobs for Blake").is_disabled()
          and spage.get_by_role("link", name="Find Jobs for Blake").count() == 0)
    spage.close()
    page.get_by_role("button", name=f"Actions for {bare}").filter(visible=True).first.click()
    check("the row menu of a deactivated candidate offers Reactivate and no search", [i.inner_text().strip() for i in page.get_by_role("menuitem").all()] == ["Edit Details", "Assign to a Member", "Reactivate"])
    page.get_by_role("menuitem", name="Reactivate").click()
    page.get_by_role("alertdialog", name=f"Reactivate {bare}?").get_by_role("button", name="Reactivate candidate").click()
    try:
        toast(page, f"{bare} is active again")
        check("reactivating a candidate is confirmed although the row leaves the list of deactivated candidates", True)
    except Exception:
        check("reactivating a candidate is confirmed although the row leaves the list of deactivated candidates", False, "no toast")
    check("a candidate can be reactivated", api(page, f"/api/candidates/{bare_id}")["is_active"] is True)
    ctx.close()


def step_search_form(browser):
    print("== search form: candidate picker, Add Candidate inside it, inline resume upload and replace, responsible-member select")
    resume_path = f"{WORK_DIR}/search-resume-{STAMP}.txt"
    with open(resume_path, "w") as handle:
        handle.write(RESUME_TEXT)
    name = f"Sasha Search {STAMP}"
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com", expect_home="/tl/dashboard")
    limit = api(page, "/api/searches/options")["limits"]["max_members_per_search"]
    omar = next(u for u in api(page, "/api/users?role=MEMBER&page_size=50")["items"] if u["email"] == "omar@example.com")
    page.goto(BASE + "/tl/search")
    group = page.get_by_role("group", name=PICKER, exact=True)
    group.get_by_role("checkbox").first.wait_for(timeout=15000)
    page.wait_for_load_state("networkidle")
    check("connected to Salesforce: the candidate list of the search form has no button to type a candidate in with",
          page.get_by_role("button", name=re.compile("^Add (Candidate|Manually)")).count() == 0)
    check("the picker says how many candidates may be chosen (the API's limit)", page.get_by_text(f"0 selected of {limit} allowed").is_visible(), str(limit))
    check("nothing is selected yet, so there is no 'Selected candidates' section", page.get_by_role("region", name="Selected candidates").count() == 0)
    page.get_by_role("button", name="Start Search").click()
    check("a search without a candidate is refused in the form", page.get_by_role("alert").filter(has_text="Choose at least one candidate.").first.is_visible())

    # Add Candidate from inside the form: offered by a portal without Salesforce only
    searches = []
    page.on("request", lambda r: searches.append(r) if r.method == "POST" and r.url.endswith("/api/searches") else None)
    chosen = page.get_by_role("region", name="Selected candidates")
    with without_salesforce(page):
        page.goto(BASE + "/tl/search")
        page.get_by_role("group", name="Candidates", exact=True).get_by_role("checkbox").first.wait_for(timeout=15000)
        page.get_by_role("button", name="Add Candidate", exact=True).filter(visible=True).first.click()
        dialog = page.get_by_role("dialog", name="Add Candidate")
        dialog.get_by_label("Full name").fill(name)
        dialog.get_by_role("button", name="Add Candidate").click()
        toast(page, f"{name} added")
        dialog.wait_for(state="hidden", timeout=10000)
        chosen.get_by_text(name).first.wait_for(timeout=10000)
        check("a candidate added from the search form is chosen for the search at once, and the form stays where it was",
              page.url.endswith("/tl/search") and page.get_by_text(f"1 selected of {limit} allowed").is_visible() and len(searches) == 0
              and page.get_by_role("dialog", name="AI Job Search Started").count() == 0)
    # connected to Salesforce again: the new candidate is one of the portal's own, and is chosen from that group
    page.goto(BASE + "/tl/search")
    group.get_by_role("checkbox", name=re.compile(re.escape(name))).check(timeout=15000)
    chosen.get_by_text(name).first.wait_for(timeout=10000)
    record = candidate_named(page, name)
    entry = chosen.get_by_role("listitem").filter(has_text=name)
    member_box = entry.get_by_role("combobox", name="Responsible member, who handles the jobs found")
    check("each chosen candidate has its resume and its responsible member right there",
          entry.locator('[data-slot="tag"]').filter(has_text="No resume").is_visible() and entry.get_by_role("button", name=f"Upload Resume for {name}").is_visible()
          and member_box.inner_text().strip() == "Nobody yet (unassigned)")
    check("the section says both are saved to the candidate right away", chosen.get_by_text(re.compile("saved to the candidate right away")).is_visible())
    check("the form warns that the candidate has no resume and no responsible member",
          page.get_by_text(re.compile(re.escape(name) + r" has no\s+resume")).is_visible() and page.get_by_text(re.compile(re.escape(name) + r" has no\s+responsible member")).is_visible())
    no_overflow(page, "search form with a chosen candidate")
    shot(page, "search_form_selected")

    # resume: upload, wrong file, replace
    file_input = entry.get_by_label(f"Resume file for {name}")
    with page.expect_response(lambda r: r.url.endswith(f"/api/candidates/{record['id']}/resume") and r.request.method == "POST") as info:
        file_input.set_input_files(resume_path)
    toast(page, f"search-resume-{STAMP}.txt uploaded for {name} (version 1)")
    expect(entry.locator('[data-slot="tag"]').filter(has_text="Resume uploaded")).to_be_visible(timeout=10000)
    check("a resume uploaded in the search form is stored for the candidate (version 1)", info.value.status == 201 and api(page, f"/api/candidates/{record['id']}")["has_resume"] is True)
    check("the button turns into 'Replace Resume' and the warning about the resume goes away",
          entry.get_by_role("button", name=f"Replace Resume for {name}").is_visible() and page.get_by_text(re.compile(re.escape(name) + r" has no\s+resume")).count() == 0)
    bad = resume_path.replace(".txt", ".exe")
    with open(bad, "wb") as handle:
        handle.write(b"MZ")
    file_input.set_input_files(bad)
    check("a wrong file type is refused beside the candidate, and nothing is sent", entry.get_by_role("alert").filter(has_text="Choose a PDF, DOCX or TXT file.").is_visible())
    file_input.set_input_files(resume_path)
    toast(page, f"uploaded for {name} (version 2)")
    check("replacing the resume in the search form makes version 2", api(page, f"/api/candidates/{record['id']}/resume")["version"] == 2
          and entry.get_by_role("alert").count() == 0)

    # responsible member
    member_box.click()
    offered = [o.inner_text().strip() for o in page.get_by_role("option").all()]
    check("the responsible-member select offers the active members of the candidate's team",
          offered[0] == "Nobody yet (unassigned)" and {"Maya Chen", "Noah Patel", "Omar Haddad"} <= set(offered) and not {"Priya Nair", "Quinn Lopez"} & set(offered), str(offered))
    with page.expect_response(lambda r: r.url.endswith(f"/api/candidates/{record['id']}") and r.request.method == "PATCH") as info:
        page.get_by_role("option", name="Omar Haddad", exact=True).click()
    toast(page, f"Omar Haddad is now responsible for {name}")
    check("choosing a member saves it to the candidate at once", info.value.status == 200 and info.value.request.post_data_json == {"assigned_member_id": omar["id"]}
          and api(page, f"/api/candidates/{record['id']}")["assigned_member"]["name"] == "Omar Haddad")
    expect(member_box).to_have_text("Omar Haddad", timeout=10000)
    check("the select shows the member, and the warning about the missing member goes away",
          page.get_by_text(re.compile(re.escape(name) + r" has no\s+responsible member")).count() == 0)
    check("none of this started a search", len(searches) == 0 and page.get_by_role("dialog", name="AI Job Search Started").count() == 0)

    # selection handling
    group.get_by_role("checkbox", name=re.compile("Hannah Kim")).check()
    hannah = chosen.get_by_role("listitem").filter(has_text="Hannah Kim")
    check("a candidate who has a resume is offered 'Replace Resume'", hannah.get_by_role("button", name="Replace Resume for Hannah Kim").is_visible()
          and page.get_by_text(f"2 selected of {limit} allowed").is_visible())
    hannah.get_by_role("button", name="Remove Hannah Kim from this search").click()
    expect(chosen.get_by_role("listitem")).to_have_count(1, timeout=5000)
    check("Remove takes a candidate out of the search and unticks the row", group.get_by_role("checkbox", name=re.compile("Hannah Kim")).is_checked() is False)
    page.get_by_role("button", name="Clear all").click()
    expect(page.get_by_role("region", name="Selected candidates")).to_have_count(0, timeout=5000)
    check("Clear all empties the selection", page.get_by_text(f"0 selected of {limit} allowed").is_visible())
    page.get_by_role("searchbox", name="Search candidates").fill("sasha")
    expect(group.get_by_role("checkbox")).to_have_count(1, timeout=10000)
    check("the picker row now says who handles the candidate and that the resume is in",
          "Handled by Omar Haddad" in group.inner_text() and "No resume" not in group.inner_text(), group.inner_text().replace("\n", " | "))
    ctx.close()

    octx = new_context(browser)
    opage = octx.new_page()
    login(opage, "omar@example.com")
    bell_text(opage, f"Lena Alpha made you responsible for the candidate {name}.")
    check("the member chosen in the search form is notified about the candidate", True)
    opage.keyboard.press("Escape")
    opage.goto(BASE + "/member/candidates")
    opage.locator('[data-slot="candidate-card"]').filter(has_text=name).wait_for(timeout=15000)
    check("and finds the candidate under My Candidates", True)
    octx.close()



def step_invite(browser):
    print("== team lead invites a member; the link completes the accept flow")
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com", expect_home="/tl/dashboard")
    page.goto(BASE + "/tl/members")
    page.wait_for_load_state("networkidle")
    name = f"Erin E2E {STAMP}"
    email = f"erin.e2e.{STAMP}@example.com"
    page.get_by_role("button", name="Add Member").first.click()
    dialog = page.get_by_role("dialog", name="Add Member")
    dialog.get_by_label("Full name").fill("Erin <b>http://x.example</b>")
    dialog.get_by_label("E-mail").fill(email)
    dialog.get_by_role("button", name="Create Invitation").click()
    try:
        dialog.get_by_role("alert").filter(has_text=re.compile("letters, digits, spaces")).first.wait_for(timeout=5000)
        check("a name that could carry a link is refused in the form, with the API's rule", True)
    except Exception:
        check("a name that could carry a link is refused in the form, with the API's rule", False)
    dialog.get_by_label("Full name").fill(name)
    dialog.get_by_role("button", name="Create Invitation").click()
    created = page.get_by_role("dialog", name="Invitation created")
    created.wait_for(timeout=15000)
    invite_url = created.get_by_label("Invitation link").input_value()
    check("invite link is shown to the creator", "/invite/accept?token=" in invite_url, invite_url[:60])
    check("the dialog says truthfully that no e-mail was sent", created.get_by_text("No e-mail was sent").is_visible())
    check("a copy button is offered", created.get_by_role("button", name="Copy link").is_visible())
    shot(page, "invite_created")
    created.get_by_role("button", name="Done").click()
    page.get_by_role("link", name=name).first.wait_for(timeout=15000)
    check("the invited member appears in the team list", True)
    page.goto(BASE + "/tl/invitations")
    page.wait_for_load_state("networkidle")
    check("invitation is listed as pending", page.get_by_text(email).first.is_visible() and page.locator('[data-slot="tag"]').filter(has_text="Pending").first.is_visible())

    # the invited person, in a browser that is not signed in
    guest = new_context(browser)
    gpage = guest.new_page()
    gpage.goto(local(invite_url))
    gpage.get_by_role("heading", level=1, name=f"Welcome, {name}").wait_for(timeout=15000)
    body = gpage.locator("main").inner_text()
    check("accept page shows welcome, e-mail, team and role", email in body and "Team Alpha" in body and "Member" in body)
    check("accept page states the minimum length from the API", re.search(r"At least \d+ characters", body) is not None)
    no_overflow(gpage, "invite accept")
    shot(gpage, "invite_accept")
    gpage.get_by_label("Password", exact=True).fill("short")
    gpage.get_by_label("Repeat the password").fill("short")
    gpage.get_by_role("button", name="Create account").click()
    check("a weak password is refused before sending", gpage.get_by_role("alert").filter(has_text=re.compile("Use at least")).first.is_visible())
    new_password = f"Fresh-Start-{STAMP}!"
    gpage.get_by_label("Password", exact=True).fill(new_password)
    gpage.get_by_label("Repeat the password").fill(new_password)
    gpage.get_by_role("button", name="Create account").click()
    gpage.wait_for_url(re.compile(r"/login$"), timeout=15000)
    toast(gpage, "Your account is ready")
    check("accepting sends the person to /login", True)
    gpage.get_by_label("E-mail").fill(email)
    gpage.get_by_label("Password", exact=True).fill(new_password)
    gpage.get_by_role("button", name="Sign in").click()
    gpage.wait_for_url(re.compile(r"/member/dashboard$"), timeout=20000)
    gpage.wait_for_load_state("networkidle")
    check("the new member can sign in and lands on the member dashboard", True)
    SHARED.update(name=name, email=email, password=new_password)
    check("a new member sees empty states, not broken charts", gpage.get_by_text("No jobs in the pipeline yet").first.is_visible())
    guest.close()

    # the link works once
    again = new_context(browser)
    apage = again.new_page()
    with expecting("/api/invitations/validate", "404"):
        apage.goto(local(invite_url))
        apage.get_by_role("heading", name="This invitation cannot be used").wait_for(timeout=15000)
    check("a used invitation link is refused with the API's message", apage.get_by_text("This invitation link is invalid or has expired.").is_visible())
    again.close()

    page.reload()
    page.wait_for_load_state("networkidle")
    check("invitation now shows as accepted", page.locator("tr, li").filter(has_text=email).locator('[data-slot="tag"]').filter(has_text="Accepted").first.is_visible())

    # a second invitation: Resend issues a new link, sending is limited per address, Revoke ends it
    name2, email2 = f"Rae Revoke {STAMP}", f"rae.revoke.{STAMP}@example.com"
    page.get_by_role("button", name="Add Member").first.click()
    dialog = page.get_by_role("dialog", name="Add Member")
    dialog.get_by_label("Full name").fill(name2)
    dialog.get_by_label("E-mail").fill(email2)
    dialog.get_by_role("button", name="Create Invitation").click()
    created = page.get_by_role("dialog", name="Invitation created")
    links = [created.get_by_label("Invitation link").input_value(timeout=15000)]
    created.get_by_role("button", name="Done").click()
    created.wait_for(state="hidden", timeout=5000)

    def pending_row():
        return page.locator("tr, li").filter(has_text=email2).filter(has=page.locator('[data-slot="tag"]', has_text="Pending")).filter(visible=True).first

    resend = f"Resend the invitation for {name2}"
    for attempt in range(4):                 # the API allows five invitations per address and hour: this one and four more
        pending_row().get_by_role("button", name=resend).click()
        fresh = page.get_by_role("dialog", name="New invitation link")
        links.append(fresh.get_by_label("Invitation link").input_value(timeout=15000))
        if attempt == 0:
            check("Resend shows the new link and says the earlier one stopped working",
                  fresh.get_by_text("Earlier links for this person no longer work.").is_visible() and fresh.get_by_text("No e-mail was sent").is_visible())
        fresh.get_by_role("button", name="Done").click()
        fresh.wait_for(state="hidden", timeout=5000)
    check("every Resend issues a different link", len(set(links)) == 5 and all("/invite/accept?token=" in link for link in links), f"{len(set(links))} different links")
    with expecting("429", "/api/invitations/"):
        pending_row().get_by_role("button", name=resend).click()
        toast(page, "Too many invitations in the last hour. Wait a while before sending another one.")
    check("a sixth invitation to one address within the hour is refused with the API's own message (429)", True)

    old = new_context(browser)
    opage = old.new_page()
    with expecting("/api/invitations/validate", "404"):
        opage.goto(local(links[0]))
        opage.get_by_role("heading", name="This invitation cannot be used").wait_for(timeout=15000)
    check("the link that was replaced by Resend no longer works", True)

    pending_row().get_by_role("button", name=f"Revoke the invitation for {name2}").click()
    confirm = page.get_by_role("alertdialog")
    check("Revoke asks first", confirm.get_by_text("The link stops working at once.").is_visible())
    confirm.get_by_role("button", name="Revoke invitation").click()
    toast(page, "Invitation revoked")
    page.locator("tr, li").filter(has_text=email2).filter(visible=True).first.wait_for(timeout=15000)
    try:
        pending_row().wait_for(state="detached", timeout=15000)
        check("after Revoke no invitation for that person is pending", True)
    except Exception:
        check("after Revoke no invitation for that person is pending", False)
    with expecting("/api/invitations/validate", "404"):
        opage.goto(local(links[-1]))
        opage.get_by_role("heading", name="This invitation cannot be used").wait_for(timeout=15000)
    check("a revoked invitation link no longer works", True)
    old.close()
    ctx.close()


def run_search(page, area, candidates, titles, per_candidate, review, expect_skipped=None):
    """Fill the search form and start it. Returns the progress window locator."""
    page.goto(BASE + f"{area}/search")
    page.wait_for_load_state("networkidle")
    group = page.get_by_role("group", name=PICKER, exact=True)
    group.get_by_role("checkbox").first.wait_for(timeout=15000)
    for name in candidates:
        group.get_by_role("checkbox", name=re.compile(name)).check()
    field = page.get_by_label(re.compile("^Job titles"))
    for t in titles:
        field.fill(t)
        field.press("Enter")
    count = page.get_by_label("Results per candidate")
    count.fill(str(per_candidate))
    if review:
        page.get_by_role("radio", name=re.compile("Search first, review, then assign")).check()
    page.get_by_role("button", name="Start Search").click()
    window = page.get_by_role("dialog", name="AI Job Search Started")
    window.wait_for(timeout=15000)
    return window


def step_search(browser):
    print("== team lead: auto-assign search for a candidate, progress window, the responsible member sees the new jobs")
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com", expect_home="/tl/dashboard")
    samuel = candidate_named(page, "Samuel Okafor")                   # Noah Patel's candidate
    before = api(page, f"/api/assignments?candidate_id={samuel['id']}&page_size=1")["total"]

    page.goto(BASE + "/tl/search")
    page.wait_for_load_state("networkidle")
    group = page.get_by_role("group", name=PICKER, exact=True)
    group.get_by_role("checkbox").first.wait_for(timeout=15000)
    names = group.inner_text()
    check("team lead sees only their own team's candidates in the list",
          all(n in names for n in ("Jordan Rivera", "Elena Petrova", "Samuel Okafor", "Hannah Kim", "Diego Morales", "Amara Nwosu"))
          and not any(n in names for n in ("Aisha Rahman", "Tom Becker", "Grace Liu", "Lucas Meyer", "Victor Hale", "Elif Demir")))
    check("the list holds candidates, not members", group.get_by_role("checkbox").count() == 6 and group.get_by_role("checkbox", name=re.compile("^(Maya Chen|Noah Patel|Omar Haddad)")).count() == 0)
    check("each candidate row says who handles the candidate, or that nobody does",
          "Handled by Noah Patel" in group.get_by_role("listitem").filter(has_text="Samuel Okafor").inner_text()
          and "No responsible member" in group.get_by_role("listitem").filter(has_text="Hannah Kim").inner_text())
    check("each candidate row shows whether the resume is in", "No resume" in group.get_by_role("listitem").filter(has_text="Diego Morales").inner_text()
          and "No resume" not in group.get_by_role("listitem").filter(has_text="Samuel Okafor").inner_text())
    check("the old member wording is gone from the form", page.get_by_role("group", name="Members").count() == 0 and page.get_by_label("Results per member").count() == 0)
    check("the form says the count is per candidate", page.get_by_text(re.compile("The count is per candidate")).is_visible())
    check("the form says a blank override uses what is saved in each candidate's profile",
          page.get_by_text(re.compile("Leave a field empty to use what is saved in each candidate's profile")).is_visible())
    check("both modes are offered", page.get_by_role("radio", name=re.compile("Find & Assign Matching Jobs")).is_visible()
          and page.get_by_role("radio", name=re.compile("Search first, review, then assign")).is_visible())
    no_overflow(page, "search form")
    shot(page, "search_form")

    titles = ["AI Engineer"] if not MOBILE else ["Backend Engineer"]
    window = run_search(page, "/tl", ["Samuel Okafor"], titles, 6, review=False)
    check("progress window 'AI Job Search Started' opens at once", window.is_visible())
    check("progress window lists the selected candidate", window.get_by_text("Samuel Okafor").first.is_visible())
    for label in ("Requested", "Discovered", "Verified", "Duplicates removed", "Matched", "Assigned"):
        check(f"progress window shows: {label}", window.locator(f'[data-slot="run-counter"][data-label="{label}"]').is_visible())
    check("progress window has Minimize, View Search and Cancel Search",
          window.get_by_role("button", name="Minimize", exact=True).last.is_visible()
          and window.get_by_role("link", name=re.compile("View (Search|Results)")).is_visible()
          and (window.get_by_role("button", name="Cancel Search").count() == 1 or window.get_by_role("button", name="Close").count() == 1))
    shot(page, "search_running")

    # the window does not block the app: the page behind it still responds
    if not MOBILE:
        page.get_by_role("button", name="Theme").click()
        check("the app stays usable behind the progress window", page.get_by_role("menuitem", name="Dark").is_visible())
        page.keyboard.press("Escape")

    window.locator('[data-slot="tag"][data-status="completed"]').first.wait_for(timeout=90000)
    check("the search reaches Completed", True)
    toast(page, "Search completed")
    check("a toast announces the result", True)
    assigned = counter(window, "Assigned")
    matched = counter(window, "Matched")
    check("the search matched and assigned jobs", matched > 0 and assigned > 0, f"matched {matched}, assigned {assigned}")
    try:
        window.get_by_role("rowheader", name="Greenhouse").wait_for(timeout=10000)
        check("sources are listed with real figures", True)
    except Exception:
        check("sources are listed with real figures", False)
    shot(page, "search_completed")

    window.get_by_role("button", name="Minimize", exact=True).last.click()
    indicator = page.locator('[data-slot="search-indicator"]')
    indicator.wait_for(timeout=5000)
    check("minimized: the window becomes a small floating indicator", indicator.is_visible() and not window.is_visible())
    open_nav(page)
    nav_link(page, "Dashboard").click()
    page.wait_for_url(re.compile(r"/tl/dashboard$"), timeout=15000)
    check("the indicator survives navigation", indicator.is_visible())
    indicator.get_by_role("button", name=re.compile("Show search progress")).click()
    window.wait_for(timeout=5000)
    window.get_by_role("link", name="View Results").click()
    page.wait_for_url(re.compile(r"/tl/searches/[0-9a-f-]{36}$"), timeout=15000)
    page.wait_for_load_state("networkidle")
    page.get_by_role("heading", name="Results", exact=True).wait_for(timeout=20000)
    rows = page.get_by_role("checkbox", name=re.compile("^Select .* for Samuel Okafor"))
    rows.first.wait_for(timeout=15000)
    check("the results page lists the jobs found, each for the candidate", rows.count() > 0, f"{rows.count()} result rows")
    check("the run's settings say 'Results per candidate'", page.get_by_text("Results per candidate").first.is_visible() and page.get_by_text("Results per member").count() == 0)
    after = api(page, f"/api/assignments?candidate_id={samuel['id']}&page_size=1")["total"]
    check("the candidate has new assignments", after == before + assigned, f"{before} -> {after}")
    no_overflow(page, "search detail")
    shot(page, "search_detail")

    ctx.close()

    # the member sees the new jobs without anything else happening
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "noah@example.com", expect_home="/member/dashboard")
    page.goto(BASE + "/member/jobs")
    page.get_by_role("searchbox", name="Search jobs").fill(titles[0])
    page.wait_for_load_state("networkidle")
    cards = page.locator('[data-slot="job-card"]').filter(visible=True)
    cards.first.wait_for(timeout=15000)
    check("the responsible member sees the candidate's newly assigned jobs", cards.count() >= 1 and titles[0].split()[0] in cards.first.inner_text()
          and "Samuel Okafor" in cards.first.inner_text(), f"{cards.count()} cards for '{titles[0]}'")
    bell = page.get_by_role("button", name=re.compile("Notifications"))
    check("the member has a notification about it", "unread" in (bell.get_attribute("aria-label") or ""))
    bell.click()
    try:
        found = page.get_by_text(re.compile(rf"^A search found {assigned} new jobs? for Samuel Okafor\.$")).first
        found.wait_for(timeout=10000)
        check("the notification says how many new jobs a search found, and for which candidate", True)
    except Exception:
        check("the notification says how many new jobs a search found, and for which candidate", False)
    check("the old 'assigned you' wording is gone", page.get_by_text(re.compile("assigned you")).count() == 0)
    page.get_by_role("button", name="Mark all as read").click()
    try:
        expect(bell).to_have_attribute("aria-label", "Notifications", timeout=10000)
        check("Mark all as read clears the unread count", True)
    except Exception:
        check("Mark all as read clears the unread count", False, bell.get_attribute("aria-label") or "")
    ctx.close()


def step_tracking(browser):
    print("== tracking: later stages, notes, a manager recording in place of the member, private notes, withdrawing")
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "noah@example.com", expect_home="/member/dashboard")
    interviews_before = kpi(page, "Interviews")
    total_before = kpi(page, "Total Assigned")
    page.goto(BASE + "/member/jobs?applied=false")
    cards = page.locator('[data-slot="job-card"]').filter(visible=True)
    cards.first.wait_for(timeout=15000)
    title = cards.first.get_by_role("link").first.inner_text().strip()      # the card's first link is the job, the second the candidate
    cards.first.get_by_role("link").first.click()
    page.wait_for_url(re.compile(r"/member/jobs/[0-9a-f-]{36}$"), timeout=15000)
    assignment_id = page.url.rsplit("/", 1)[1]
    page.get_by_role("heading", level=1, name=title).wait_for(timeout=15000)

    # an interview recorded without "applied" first: the dialog says what the API will do about the applied date
    page.get_by_role("button", name="Change Status").click()
    page.get_by_role("menuitem", name=re.compile("^Interviewing")).click()
    dialog = page.get_by_role("dialog", name="Change status to Interviewing")
    check("a later stage without a recorded application explains what happens to the applied date",
          dialog.get_by_text(re.compile("No application has been recorded for this job yet")).is_visible())
    dialog.get_by_role("button", name="Change to Interviewing").click()
    toast(page, "Status changed to Interviewing")
    page.locator('[data-slot="status-badge"][data-status="INTERVIEWING"]').filter(visible=True).first.wait_for(timeout=10000)
    check("the member can move a job to Interviewing", True)
    check("the history says the applied date was set by that change",
          page.get_by_text("No earlier application was recorded, so the applied date was set to this moment.").filter(visible=True).first.is_visible())

    own_note = f"Recruiter is Dana {STAMP}"
    page.get_by_label("Add a note").fill(own_note)
    page.get_by_role("button", name="Add Note").click()
    toast(page, "Note added")
    page.get_by_text(own_note).filter(visible=True).first.wait_for(timeout=10000)
    check("the member can add a note", True)
    check("a member is not offered private notes", page.get_by_label(re.compile("^Private")).count() == 0)
    page.goto(BASE + "/member/dashboard")
    page.wait_for_load_state("networkidle")
    check("the Interviews KPI went up by one", kpi(page, "Interviews") == interviews_before + 1, f"{interviews_before} -> {kpi(page, 'Interviews')}")
    ctx.close()

    # the Team Lead opens the same assignment, records the offer for the member and keeps a private note
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com", expect_home="/tl/dashboard")
    page.goto(BASE + "/tl/assignments")
    page.get_by_role("combobox", name="Responsible member").wait_for(timeout=15000)
    check("the assignments list filters by candidate and by responsible member (no 'Member' filter)",
          page.get_by_role("combobox", name="Candidate").is_visible() and page.get_by_role("combobox", name="Member", exact=True).count() == 0)
    if not MOBILE:
        page.get_by_role("columnheader", name="Job").wait_for(timeout=15000)
        heads = [h.inner_text().strip() for h in page.get_by_role("columnheader").all()]
        check("the assignments table has the columns Candidate and Responsible member", "Candidate" in heads and "Responsible member" in heads and "Member" not in heads, str(heads))
    with page.expect_response(lambda r: "/api/assignments?" in r.url and "member_id=" in r.url) as info:
        pick_option(page, page, "Responsible member", "Noah Patel")
    check("the responsible-member filter is sent to the API as member_id", info.value.ok)
    page.get_by_role("combobox", name="Status").click()
    page.get_by_role("option", name="Interviewing").click()
    row = page.locator("table button, li button").filter(has_text=title).filter(visible=True).first
    row.wait_for(timeout=15000)
    row.click()
    drawer = page.get_by_role("dialog")
    drawer.get_by_role("tab", name="Tracking").wait_for(timeout=15000)
    check("the manager sees the member's note", drawer.get_by_text(own_note).first.is_visible())
    drawer.get_by_role("button", name="Change Status").click()
    page.get_by_role("menuitem", name=re.compile("^Offer")).click()
    dialog = page.get_by_role("dialog", name="Change status to Offer")
    check("a manager changing a status is told the history will show it was done in place of the responsible member",
          dialog.get_by_text(re.compile("You are recording this for Samuel Okafor, in place of the responsible member")).is_visible())
    dialog.get_by_role("button", name="Change to Offer").click()
    toast(page, "Status changed to Offer")
    drawer.locator('[data-slot="status-badge"][data-status="OFFER"]').filter(visible=True).first.wait_for(timeout=10000)
    check("the manager can record an offer for the candidate", True)
    check("the history shows who recorded it", drawer.get_by_text("Recorded by Lena Alpha in place of the responsible member.").first.is_visible())

    private_note = f"Salary band checked {STAMP}"
    drawer.get_by_label("Add a note").fill(private_note)
    drawer.get_by_label(re.compile("^Private")).check()
    drawer.get_by_role("button", name="Add Note").click()
    toast(page, "Note added")
    drawer.get_by_text(private_note).first.wait_for(timeout=10000)
    check("a manager can keep a note private", drawer.locator("li").filter(has_text=private_note).locator('[data-slot="tag"]').filter(has_text="Private").first.is_visible())
    page.keyboard.press("Escape")
    ctx.close()

    # the member sees the offer, their own note, and nothing of the private note
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "noah@example.com")
    page.goto(BASE + f"/member/jobs/{assignment_id}")
    page.locator('[data-slot="status-badge"][data-status="OFFER"]').filter(visible=True).first.wait_for(timeout=15000)
    check("the member sees the status the manager recorded", True)
    check("the member sees their own note but not the manager's private one",
          page.get_by_text(own_note).filter(visible=True).first.is_visible() and private_note not in page.content())
    ctx.close()

    # withdrawing: gone from the member's list and counts, kept in the history
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com")
    page.goto(BASE + "/tl/assignments?status=OFFER")
    with page.expect_response(lambda r: "/api/assignments?" in r.url and "candidate_id=" in r.url) as info:
        pick_option(page, page, "Candidate", "Samuel Okafor")
    check("the candidate filter is sent to the API as candidate_id", info.value.ok)
    row = page.locator("table button, li button").filter(has_text=title).filter(visible=True).first
    row.wait_for(timeout=15000)
    row.click()
    drawer = page.get_by_role("dialog")
    drawer.get_by_role("button", name="Withdraw").click()
    confirm = page.get_by_role("alertdialog", name="Withdraw this assignment?")
    check("withdrawing asks first and says the history is kept", confirm.get_by_text(re.compile("Its history is kept")).is_visible())
    confirm.get_by_role("button", name="Withdraw assignment").click()
    confirm.wait_for(state="hidden", timeout=10000)
    try:
        drawer.get_by_text(re.compile("This assignment was withdrawn from the candidate")).first.wait_for(timeout=10000)
        check("the drawer shows the assignment as withdrawn, with its history", drawer.get_by_text(re.compile("^Removed from the candidate's list by Lena Alpha")).first.is_visible())
    except Exception:
        check("the drawer shows the assignment as withdrawn, with its history", False)
    page.keyboard.press("Escape")
    try:
        page.locator("table button, li button").filter(has_text=title).filter(visible=True).first.wait_for(state="detached", timeout=10000)
        check("a withdrawn assignment leaves the list", True)
    except Exception:
        check("a withdrawn assignment leaves the list", False)
    page.get_by_role("combobox", name="Withdrawn assignments").click()
    page.get_by_role("option", name="Include withdrawn").click()
    try:
        page.locator("tr, li").filter(has_text=title).locator('[data-slot="tag"]').filter(has_text="Withdrawn from list").filter(visible=True).first.wait_for(timeout=10000)
        check("it can be shown again with 'Include withdrawn'", True)
    except Exception:
        check("it can be shown again with 'Include withdrawn'", False)
    no_overflow(page, "assignments with withdrawn")
    ctx.close()

    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "noah@example.com", expect_home="/member/dashboard")
    check("the withdrawn job no longer counts for the member", kpi(page, "Total Assigned") == total_before - 1, f"{total_before} -> {kpi(page, 'Total Assigned')}")
    with expecting("410", f"/api/assignments/{assignment_id}"):       # the API answers a member 410 for a job of theirs that a manager took back
        page.goto(BASE + f"/member/jobs/{assignment_id}")
        page.locator('[data-slot="error-state"]').filter(visible=True).first.wait_for(timeout=15000)
        page.wait_for_load_state("networkidle")
    check("the page of a withdrawn job shows the API's 410 sentence and where to go instead",
          page.get_by_text("This job is no longer assigned to your candidate.").is_visible()
          and page.get_by_role("link", name="Go to My Candidates").get_attribute("href") == "/member/candidates")
    no_overflow(page, "member job that was withdrawn")
    ctx.close()


def step_review(browser):
    print("== review-mode results, skipped candidates, bulk assign")
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com", expect_home="/tl/dashboard")
    titles = ["Account Executive"] if not MOBILE else ["Backend Engineer"]
    page.goto(BASE + "/tl/search")
    page.wait_for_load_state("networkidle")
    group = page.get_by_role("group", name=PICKER, exact=True)
    group.get_by_role("checkbox").first.wait_for(timeout=15000)
    group.get_by_role("checkbox", name=re.compile("Jordan Rivera")).check()
    group.get_by_role("checkbox", name=re.compile("Diego Morales")).check()
    check("a candidate without a resume is flagged before starting", page.get_by_text(re.compile("Diego Morales has no\\s+resume")).is_visible())
    check("a candidate without a responsible member is flagged too, and the search is still allowed",
          page.get_by_text(re.compile("Diego Morales has no\\s+responsible member")).is_visible() and page.get_by_role("button", name="Start Search").is_enabled())
    field = page.get_by_label(re.compile("^Job titles"))
    field.fill(titles[0])
    field.press("Enter")
    page.get_by_label("Results per candidate").fill("5")
    page.get_by_role("radio", name=re.compile("Search first, review, then assign")).check()
    page.get_by_role("button", name="Start Search").click()
    window = page.get_by_role("dialog", name="AI Job Search Started")
    window.wait_for(timeout=15000)
    check("skipped candidates are shown with the API's reason", window.get_by_text(re.compile("1 selected candidate was left out of this search")).is_visible()
          and window.get_by_text(re.compile("Diego Morales:")).is_visible())
    window.locator('[data-slot="tag"][data-status="completed"]').first.wait_for(timeout=90000)
    check("review-mode search completes", True)
    check("review mode assigns nothing by itself", counter(window, "Assigned") == 0)
    window.get_by_role("link", name="View Results").click()
    page.wait_for_url(re.compile(r"/tl/searches/[0-9a-f-]{36}$"), timeout=15000)
    page.wait_for_load_state("networkidle")
    page.get_by_role("heading", name="Results", exact=True).wait_for(timeout=20000)
    check("review mode opens on the results not assigned yet", page.get_by_role("radiogroup", name="Show").get_by_role("radio", name="Not assigned yet").get_attribute("aria-checked") == "true")
    boxes = page.get_by_role("checkbox", name=re.compile("^Select .* for Jordan Rivera"))
    boxes.first.wait_for(timeout=15000)
    enabled = [boxes.nth(i) for i in range(boxes.count()) if boxes.nth(i).is_enabled()]
    check("review results list jobs to choose from", len(enabled) > 0, f"{len(enabled)} selectable of {boxes.count()} shown")
    pick = min(2, len(enabled))
    for box in enabled[:pick]:
        box.check()
    page.get_by_role("button", name=re.compile(r"Assign Selected \(%d\)" % pick)).click()
    confirm = page.get_by_role("alertdialog")
    confirm.wait_for(timeout=5000)
    check("bulk assign asks for confirmation with a per-candidate summary", confirm.get_by_text("Jordan Rivera").is_visible() and confirm.get_by_text(re.compile(rf"{pick} jobs?")).first.is_visible())
    check("the confirmation says the responsible member is notified", confirm.get_by_text(re.compile("responsible member\\s+is notified")).is_visible())
    shot(page, "bulk_assign_confirm")
    confirm.get_by_role("button", name=re.compile("^Assign")).click()
    toast(page, "Jobs assigned")
    check("bulk assign succeeds", True)
    page.get_by_role("radiogroup", name="Show").get_by_role("radio", name="All results").click()
    page.locator('[data-slot="status-badge"][data-status="ASSIGNED"]').filter(visible=True).first.wait_for(timeout=15000)
    check("assigned results now show their assignment status", True)
    no_overflow(page, "review results")

    # other team lead pages
    for path, heading in (("/tl/candidates", "Candidates"), ("/tl/members", "Team Members"), ("/tl/assignments", "Assigned Jobs"), ("/tl/assignments?applied=true", "Applications"), ("/tl/jobs", "Pending Jobs"),
                          ("/tl/searches", "Search History"), ("/tl/analytics", "Analytics"), ("/tl/invitations", "Invitations"), ("/tl/settings", "Settings")):
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        check(f"{path} loads", page.get_by_role("heading", level=1, name=heading).is_visible())
        no_overflow(page, path)

    # date filters: the API takes days from 2000 to 2100 and answers 422 for anything else
    page.goto(BASE + "/tl/assignments")
    box = page.get_by_label("Assigned from")
    box.wait_for(timeout=15000)
    asked = []
    page.on("request", lambda r: asked.append(r.url) if "/api/assignments?" in r.url else None)
    box.fill("1999-12-31")
    box.press("Tab")
    try:
        page.get_by_role("alert").filter(has_text="Choose a date between 2000 and 2100.").first.wait_for(timeout=5000)
        page.wait_for_timeout(800)
        check("a day the API would refuse is not sent, and the filter says why", not any("date_from=1999" in url for url in asked), f"{len(asked)} list requests, none with 1999")
    except Exception:
        check("a day the API would refuse is not sent, and the filter says why", False)
    with page.expect_response(lambda r: "/api/assignments?" in r.url and "date_from=2026-01-01" in r.url) as info:
        box.fill("2026-01-01")
    check("a day inside the accepted range filters the list", info.value.ok)

    # an assignment opened from the list: full job details in a drawer, with what only managers get
    page.goto(BASE + "/tl/assignments?applied=true")
    page.wait_for_load_state("networkidle")
    first = page.locator("table button, li button").filter(visible=True).first
    first.click()
    drawer = page.get_by_role("dialog")
    drawer.get_by_role("tab", name="Tracking").wait_for(timeout=15000)
    check("managers see posting links and the other candidates with the job", drawer.get_by_text("Posting links").is_visible() and drawer.get_by_text("Other candidates with this job").is_visible())
    if MOBILE:
        box = drawer.bounding_box()
        check("job details are full screen on a small screen", box["width"] >= A.w - 1 and box["height"] >= A.h - 1, str(box))
    shot(page, "assignment_drawer")
    page.keyboard.press("Escape")

    # another team's member: the API refuses, the screen says so instead of crashing
    ctx_beta = new_context(browser)
    bpage = ctx_beta.new_page()
    login(bpage, "lead.beta@example.com")
    priya = next(u for u in api(bpage, "/api/users?role=MEMBER&page_size=50")["items"] if u["email"] == "priya@example.com")
    aisha = candidate_named(bpage, "Aisha Rahman")
    ctx_beta.close()
    with expecting("403", f"/api/users/{priya['id']}"):
        page.goto(BASE + f"/tl/members/{priya['id']}")
        page.locator('[data-slot="error-state"][data-denied]').filter(visible=True).first.wait_for(timeout=15000)
        page.wait_for_load_state("networkidle")
    check("a 403 from the API is shown as 'Access denied'", page.get_by_text("Access denied").first.is_visible())
    # another team's candidate: answered like one that does not exist (404), and the screen says it is not available
    with expecting("404", f"/api/candidates/{aisha['id']}", "/api/candidates/00000000-0000-4000-8000-000000000000"):
        with page.expect_response(lambda r: r.url.endswith(f"/api/candidates/{aisha['id']}")) as info:
            page.goto(BASE + f"/tl/candidates/{aisha['id']}")
        page.locator('[data-slot="error-state"]').filter(visible=True).first.wait_for(timeout=15000)
        page.wait_for_load_state("networkidle")
        check("another team's candidate is answered 404 for a Team Lead, like one that does not exist", info.value.status == 404
              and api(page, "/api/candidates/00000000-0000-4000-8000-000000000000").get("__status") == 404)
        page.wait_for_timeout(300)
    body = page.locator("main").inner_text()
    check("the screen says the candidate is not available and shows nothing of them",
          "This candidate is not available to you" in body and "Aisha" not in body and page.get_by_role("link", name="Go to Candidates").is_visible())
    ctx.close()


def step_admin(browser):
    print("== admin: dashboard, add job, delegate to a team lead queue")
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "admin@example.com", expect_home="/admin/dashboard")
    check("admin dashboard KPIs carry data", kpi(page, "Total Assigned") > 0 and kpi(page, "Postings in the portal") > 0,
          f"assigned {kpi(page, 'Total Assigned')}, postings {kpi(page, 'Postings in the portal')}")
    check("job postings and assignments are separate sections", page.get_by_role("heading", name="Job postings", exact=True).is_visible()
          and page.get_by_role("heading", name="Assignments", exact=True).is_visible())
    check("admin charts are drawn", page.locator("svg.recharts-surface").filter(visible=True).count() >= 1
          and page.locator("svg.recharts-surface path.recharts-line-curve, svg.recharts-surface .recharts-line path").count() >= 1)
    check("team performance table links to team pages", page.get_by_role("link", name="Team Alpha").first.get_attribute("href").startswith("/admin/teams/"))
    check("metric definitions are available", page.get_by_role("button", name="How these are counted").is_visible())
    page.get_by_role("button", name="How these are counted").click()
    check("definitions come up", page.get_by_text("Applied divided by assigned").is_visible())
    page.keyboard.press("Escape")
    no_overflow(page, "admin dashboard")
    shot(page, "admin_dashboard")

    # filters reach the API
    with page.expect_response(lambda r: "/api/analytics/admin" in r.url and "team_id=" in r.url) as info:
        page.get_by_role("combobox", name="Team").click()
        page.get_by_role("option", name="Team Beta").click()
    check("the team filter is sent to the API", info.value.ok)
    page.wait_for_load_state("networkidle")

    # Add Job
    job_title = f"E2E QA Engineer {STAMP}"
    page.goto(BASE + "/admin/jobs")
    page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="Add Job").click()
    dialog = page.get_by_role("dialog", name="Add Job")
    dialog.get_by_label("Job title").fill(job_title)
    dialog.get_by_label("Company", exact=True).fill("E2E Corp")
    dialog.get_by_label(re.compile("^City")).fill("Austin")
    dialog.get_by_label(re.compile("^State")).fill("texas")
    dialog.get_by_label(re.compile("^Posting address")).fill(f"https://careers.example.org/e2e/{STAMP}")
    dialog.get_by_label(re.compile("^Description")).fill(
        "About the Role\nTest posting.\n\nResponsibilities\n- Verify things\n- Report honestly\n\nHow to Apply\nSend your application through https://careers.example.org/apply-here today.")
    dialog.get_by_role("button", name="Save Job").click()
    toast(page, "Job added")
    page.get_by_role("link", name=job_title).first.wait_for(timeout=15000)
    check("Add Job creates the job and it appears in the list", True)
    no_overflow(page, "admin jobs")

    # delegate to a Team Lead queue
    page.get_by_role("checkbox", name=f"Select {job_title} at E2E Corp").check()
    page.get_by_role("region", name="Selected jobs").get_by_role("button", name="Assign to Team Lead").click()
    dialog = page.get_by_role("dialog", name="Assign to Team Lead")
    dialog.get_by_role("combobox", name="Team").click()
    page.get_by_role("option", name="Team Beta").click()
    dialog.get_by_label(re.compile("^Note for the Team Lead")).fill("From the E2E run")
    dialog.get_by_role("button", name="Send to Team Queue").click()
    toast(page, "sent to Team Beta's queue")
    check("delegating a job to a Team Lead queue succeeds", True)

    # job detail: manager info, description grouped under the headings the text itself has
    page.get_by_role("link", name=job_title).first.click()
    page.wait_for_url(re.compile(r"/admin/jobs/[0-9a-f-]{36}$"), timeout=15000)
    page.locator('[data-slot="job-description"]').wait_for(timeout=15000)
    page.get_by_text("Waiting in team queues").wait_for(timeout=15000)
    check("job page shows the queue it waits in", page.get_by_text("Waiting in team queues").is_visible() and page.locator('[data-slot="tag"]').filter(has_text="Team Beta").first.is_visible())
    desc = page.locator('[data-slot="job-description"]')
    check("description headings come from the text", desc.get_by_role("heading", name="About the Role").is_visible() and desc.get_by_role("heading", name="Responsibilities").is_visible()
          and desc.get_by_role("listitem").count() == 2)
    check("state name was normalised to its code", page.get_by_text("Austin, TX").first.is_visible())
    no_overflow(page, "admin job detail")
    shot(page, "admin_job_detail")

    # every other admin page renders
    for path, heading in (("/admin/analytics", "Analytics"), ("/admin/teams", "Teams"), ("/admin/team-leads", "Team Leads"), ("/admin/members", "Members"), ("/admin/candidates", "Candidates"),
                          ("/admin/search", "Find Jobs"), ("/admin/searches", "Search History"), ("/admin/assignments", "Assignments"),
                          ("/admin/invitations", "Invitations"), ("/admin/activity", "Activity Log"), ("/admin/settings", "Settings")):
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        check(f"{path} loads", page.get_by_role("heading", level=1, name=heading).is_visible())
        no_overflow(page, path)
    sign_out(page)
    check("admin signed out", page.url.endswith("/login"))
    ctx.close()

    # the Team Lead of that team finds the job in the queue and hands it to a candidate
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.beta@example.com", expect_home="/tl/dashboard")
    bell = page.get_by_role("button", name=re.compile("Notifications"))
    check("the Team Lead is notified", "unread" in (bell.get_attribute("aria-label") or ""))
    page.goto(BASE + "/tl/jobs")
    page.wait_for_load_state("networkidle")
    box = page.get_by_role("checkbox", name=f"Select {job_title} at E2E Corp")
    box.wait_for(timeout=15000)
    check("the delegated job is in the Team Lead's queue", True)
    check("the Admin's note travels with it", page.get_by_text("From the E2E run").first.is_visible())
    box.check()
    check("the queue button is 'Distribute to Candidates'", page.get_by_role("button", name=re.compile("Distribute to Members")).count() == 0)
    page.get_by_role("button", name=re.compile(r"Distribute to Candidates \(1\)")).click()
    dialog = page.get_by_role("dialog", name="Assign to Candidates")
    people = dialog.get_by_role("group", name="Candidates")
    people.get_by_role("checkbox", name=re.compile("Aisha Rahman")).wait_for(timeout=15000)
    offered = people.inner_text()
    check("distribution offers only this team's active candidates",
          all(n in offered for n in ("Aisha Rahman", "Tom Becker", "Grace Liu", "Lucas Meyer")) and "Jordan Rivera" not in offered and "Victor Hale" not in offered)
    people.get_by_role("checkbox", name=re.compile("Aisha Rahman")).check()
    dialog.get_by_role("button", name=re.compile("Review 1 Assignment")).click()
    check("the confirmation names the candidate and the responsible member", page.get_by_role("dialog").get_by_text("Aisha Rahman (Priya Nair)").is_visible())
    page.get_by_role("button", name=re.compile("^Assign 1 Job")).click()
    toast(page, "Jobs assigned")
    check("the Team Lead distributes the queued job to a candidate", True)
    page.get_by_text("No jobs are waiting").or_(page.get_by_role("checkbox", name=re.compile("^Select "))).first.wait_for(timeout=15000)
    check("the job leaves the waiting queue", page.get_by_role("checkbox", name=f"Select {job_title} at E2E Corp").count() == 0)
    no_overflow(page, "tl queue")
    ctx.close()

    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "priya@example.com", expect_home="/member/dashboard")
    page.goto(BASE + "/member/jobs")
    page.get_by_role("link", name=job_title).first.wait_for(timeout=15000)
    check("the responsible member received the job that started with the Admin", True)
    check("the job card says it is for the candidate", "Aisha Rahman" in page.locator('[data-slot="job-card"]').filter(has_text=job_title).first.inner_text())
    page.get_by_role("link", name=job_title).first.click()
    page.wait_for_url(re.compile(r"/member/jobs/[0-9a-f-]{36}$"), timeout=15000)
    desc = page.locator('[data-slot="job-description"]')
    desc.wait_for(timeout=15000)
    check("a web address in the description reaches the member as the API's placeholder, never as an address",
          "[link removed: use Open Job]" in desc.inner_text() and "careers.example.org" not in page.content(), desc.inner_text()[-80:].replace("\n", " "))
    check("the member's copy keeps the headings the text has", desc.get_by_role("heading", name="How to Apply").is_visible())
    no_overflow(page, "member job detail with a removed link")
    ctx.close()


def step_misc(browser):
    print("== the rest: forgot password, direct assignment by an Admin, notification link, CSV export, source switch, search prefilled for a candidate")
    # forgot password: the API's own sentence, the same whether or not the address has an account
    ctx = new_context(browser)
    page = ctx.new_page()
    page.goto(BASE + "/login")
    page.get_by_role("link", name=re.compile("Forgot")).click()
    page.wait_for_url(re.compile(r"/forgot-password$"), timeout=15000)
    page.get_by_role("button", name="Send reset link").click()
    check("an empty e-mail address is refused in the form", page.get_by_role("alert").filter(has_text=re.compile("e-mail", re.I)).first.is_visible())
    # the sign-in screen that was just left stays in the document, hidden (Next.js keeps visited routes): look at what is visible
    page.get_by_label("E-mail").filter(visible=True).fill(f"nobody.{STAMP}@example.com")
    page.get_by_role("button", name="Send reset link").click()
    page.get_by_role("button", name="Use another address").wait_for(timeout=15000)
    answer = page.get_by_role("status").filter(visible=True).first.inner_text().strip()
    check("forgot password shows the API's answer", len(answer) > 20, answer[:110])
    check("forgot password claims nothing of its own", page.get_by_role("heading", name="Check your inbox").count() == 0)
    no_overflow(page, "forgot password")
    page.get_by_role("link", name="Back to sign in").click()
    page.wait_for_url(re.compile(r"/login$"), timeout=15000)
    ctx.close()

    # an Admin adds a job and assigns it straight to a candidate of any team
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "admin@example.com", expect_home="/admin/dashboard")
    job_title = f"Direct Assign Analyst {STAMP}"
    page.goto(BASE + "/admin/jobs")
    page.get_by_role("button", name="Add Job").click()
    dialog = page.get_by_role("dialog", name="Add Job")
    dialog.get_by_label("Job title").fill(job_title)
    dialog.get_by_label("Company", exact=True).fill("Direct Corp")
    dialog.get_by_label(re.compile("^Posting address")).fill(f"https://careers.example.org/direct/{STAMP}")
    dialog.get_by_role("button", name="Save Job").click()
    toast(page, "Job added")
    page.get_by_role("link", name=job_title).first.wait_for(timeout=15000)
    if MOBILE:      # the cards on a small screen have no row menu: choose the job by its checkbox, then use the bar that appears
        card = page.locator("li").filter(has=page.get_by_role("link", name=job_title)).filter(visible=True).last
        check("390 px: a job card carries its own checkbox", card.get_by_role("checkbox", name=f"Select {job_title} at Direct Corp").is_visible()
              and page.get_by_role("button", name=f"Actions for {job_title} at Direct Corp").filter(visible=True).count() == 0)
        check("390 px: nothing is selected yet, so there is no selection bar", page.get_by_role("region", name="Selected jobs").count() == 0)
        card.get_by_role("checkbox", name=f"Select {job_title} at Direct Corp").check()
        bar = page.get_by_role("region", name="Selected jobs")
        bar.wait_for(timeout=5000)
        check("390 px: the selection bar counts the job and offers both ways of assigning",
              "1 job selected" in bar.inner_text() and bar.get_by_role("button", name="Assign to Team Lead").is_visible() and bar.get_by_role("button", name="Assign to Candidates").is_visible())
        bar.get_by_role("button", name="Assign to Candidates").click()
    else:
        page.get_by_role("button", name=f"Actions for {job_title} at Direct Corp").click()
        check("the row menu offers 'Assign to Candidates' (and no 'Assign to Members')", page.get_by_role("menuitem", name="Assign to Members").count() == 0)
        page.get_by_role("menuitem", name="Assign to Candidates").click()
    dialog = page.get_by_role("dialog", name="Assign to Candidates")
    people = dialog.get_by_role("group", name="Candidates")
    people.get_by_role("checkbox").first.wait_for(timeout=15000)
    check("the assignment dialog names the job", dialog.get_by_text(f"{job_title} at Direct Corp").first.is_visible())
    dialog.get_by_role("searchbox", name="Search candidates").fill("Grace")
    people.get_by_role("checkbox", name=re.compile("Grace Liu")).wait_for(timeout=15000)
    expect(people.get_by_role("checkbox")).to_have_count(1, timeout=10000)
    check("the picker is searched by the API and shows team and responsible member for an Admin",
          "Handled by Quinn Lopez, Team Beta" in people.inner_text(), people.inner_text().replace("\n", " | "))
    people.get_by_role("checkbox", name=re.compile("Grace Liu")).check()
    dialog.get_by_role("searchbox", name="Search candidates").fill("Jordan")
    people.get_by_role("checkbox", name=re.compile("Jordan Rivera")).wait_for(timeout=15000)
    check("an Admin can assign to candidates of every team", "Team Alpha" in people.inner_text())
    check("a chosen candidate stays chosen when the search text changes", dialog.get_by_text("1 selected of 50 allowed").is_visible())
    dialog.get_by_role("searchbox", name="Search candidates").fill("Victor")
    dialog.get_by_text("No candidates match this search").wait_for(timeout=15000)
    check("a deactivated candidate is not offered", people.get_by_role("checkbox").count() == 0)
    if MOBILE:
        box = dialog.bounding_box()
        check("390 px: the assignment dialog fits the screen", box["x"] >= 0 and box["x"] + box["width"] <= A.w + 0.5 and box["y"] >= 0 and box["y"] + box["height"] <= A.h + 0.5, str(box))
        inner = dialog.evaluate("e => e.scrollWidth - e.clientWidth")
        check("390 px: nothing in the assignment dialog is cut off sideways", inner <= 0, f"{inner}px")
        no_overflow(page, "admin jobs with the assignment dialog open")
        shot(page, "assign_dialog_mobile")
    dialog.get_by_role("button", name=re.compile("Review 1 Assignment")).click()
    review = page.get_by_role("dialog", name="Assign 1 job to 1 candidate?")
    review.wait_for(timeout=5000)
    check("the confirmation says to whom, with the responsible member", review.get_by_text("Grace Liu (Quinn Lopez)").is_visible()
          and review.get_by_text(re.compile("responsible member is notified")).is_visible())
    if MOBILE:
        box = review.bounding_box()
        check("390 px: the confirmation step fits the screen too", box["x"] >= 0 and box["x"] + box["width"] <= A.w + 0.5 and box["y"] + box["height"] <= A.h + 0.5, str(box))
    page.get_by_role("button", name=re.compile("^Assign 1 Job")).click()
    toast(page, "Jobs assigned")
    check("an Admin can assign a job directly to a candidate", True)
    if MOBILE:
        expect(page.get_by_role("region", name="Selected jobs")).to_have_count(0, timeout=10000)
        check("390 px: after assigning, the selection is cleared", True)

    # export: the file the API makes for the current filters
    page.goto(BASE + "/admin/assignments")
    link = page.get_by_role("link", name="Export CSV")
    link.wait_for(timeout=15000)
    with page.expect_download(timeout=20000) as download:
        link.click()
    path = download.value.path()
    with open(path, encoding="utf-8-sig") as handle:
        lines = handle.read().splitlines()
    check("Export CSV downloads the assignments as a file", download.value.suggested_filename == "assignments.csv" and len(lines) > 5 and "," in lines[0],
          f"{len(lines)} lines, header: {lines[0][:70]}")
    check("the CSV starts with candidate, responsible_member, team",
          lines[0] == "candidate,responsible_member,team,company,title,location,status,match_score,assigned_at,applied_at,link_opens,assigned_by,source", lines[0])
    direct = [line for line in lines if job_title in line]
    check("a row of the CSV names the candidate, the responsible member and the team", len(direct) == 1 and direct[0].startswith("Grace Liu,Quinn Lopez,Team Beta,Direct Corp,"),
          direct[0][:90] if direct else "no row for the job just assigned")
    check("no e-mail column is left in the export", "email" not in lines[0] and "@example.com" not in "\n".join(lines))

    # a job source can be switched off and on again; the API keeps the choice
    page.goto(BASE + "/admin/settings?tab=integrations")
    switch = page.get_by_role("switch", name="Use Jobicy in searches")
    switch.wait_for(timeout=15000)
    switch.click()
    toast(page, "Jobicy switched off for searches")
    check("a job source can be switched off", "jobicy" in api(page, "/api/settings")["disabled_sources"])
    expect(switch).to_be_enabled(timeout=10000)
    switch.click()
    toast(page, "Jobicy switched on for searches")
    check("and switched on again", "jobicy" not in api(page, "/api/settings")["disabled_sources"])
    ctx.close()

    # the member is notified, and the notification leads to the job list
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "quinn@example.com", expect_home="/member/dashboard")
    bell = page.get_by_role("button", name=re.compile("Notifications"))
    check("the member has an unread notification about the assignment", "unread" in (bell.get_attribute("aria-label") or ""))
    bell.click()
    told = page.get_by_text("Avery Admin assigned 1 new job to Grace Liu.").first
    told.wait_for(timeout=15000)
    check("the notification says who assigned how many new jobs to which candidate", True)
    told.click()
    page.wait_for_url(re.compile(r"/member/candidates/[0-9a-f-]{36}$"), timeout=15000)
    page.get_by_role("heading", level=1, name="Grace Liu").wait_for(timeout=15000)
    page.get_by_role("link", name=job_title).first.wait_for(timeout=15000)
    check("selecting the notification opens the candidate, where the new job is", True)
    ctx.close()

    # "Find Jobs for <candidate>" starts the search form with that candidate chosen
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com", expect_home="/tl/dashboard")
    page.goto(BASE + "/tl/candidates")
    page.get_by_role("searchbox", name="Search candidates").fill("Samuel")
    page.get_by_role("link", name="Samuel Okafor").first.click()
    page.wait_for_url(re.compile(r"/tl/candidates/[0-9a-f-]{36}"), timeout=15000)
    page.get_by_role("link", name="Find Jobs for Samuel").click()
    page.wait_for_url(re.compile(r"/tl/search\?candidate=[0-9a-f-]{36}$"), timeout=15000)
    group = page.get_by_role("group", name=PICKER, exact=True)
    group.get_by_role("checkbox").first.wait_for(timeout=15000)
    try:
        expect(group.get_by_role("checkbox", name=re.compile("Samuel Okafor"))).to_be_checked(timeout=10000)
        check("the search form opens with that candidate chosen", group.get_by_role("checkbox", name=re.compile("Jordan Rivera")).is_checked() is False
              and page.get_by_role("region", name="Selected candidates").get_by_text("Samuel Okafor").first.is_visible())
    except Exception:
        check("the search form opens with that candidate chosen", False)
    # the old address with ?member= selects nobody: members are not searched for
    noah = next(u for u in api(page, "/api/users?role=MEMBER&page_size=50")["items"] if u["email"] == "noah@example.com")
    page.goto(BASE + f"/tl/search?member={noah['id']}")
    group.get_by_role("checkbox").first.wait_for(timeout=15000)
    page.wait_for_load_state("networkidle")
    check("the old ?member= address selects nobody", group.get_by_role("checkbox", checked=True).count() == 0
          and page.get_by_role("region", name="Selected candidates").count() == 0)
    # a member's page offers the member's candidates, not a search for the member
    page.goto(BASE + f"/tl/members/{noah['id']}")
    page.get_by_role("heading", level=1, name="Noah Patel").wait_for(timeout=15000)
    check("a member's page has no 'Find Jobs for <member>' and no profile tab",
          page.get_by_role("link", name=re.compile("^Find Jobs for")).count() == 0 and page.get_by_role("tab", name=re.compile("Profile")).count() == 0)
    tabs = [t.inner_text().split("\n")[0].strip() for t in page.get_by_role("tab").all()]
    check("a member's page has the tabs Overview, Candidates, Jobs, Activity", [re.sub(r"\s*\d+$", "", t) for t in tabs] == ["Overview", "Candidates", "Jobs", "Activity"], str(tabs))
    page.get_by_role("link", name="Candidates of Noah").click()
    page.wait_for_url(re.compile(rf"/tl/candidates\?member={noah['id']}$"), timeout=15000)
    page.get_by_role("link", name="Samuel Okafor").first.wait_for(timeout=15000)
    check("'Candidates of <member>' opens the candidate list filtered by that member", page.get_by_role("link", name="Jordan Rivera").count() == 0)
    ctx.close()


def step_stubbed(browser):
    """Answers the demo stack cannot give by itself (it has no mail server, and its searches end at once).
    The request goes to the real API; only the named fields of its answer are changed on the way back."""
    print("== answers changed on the way back: links sent by e-mail, the limit on running searches")
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com", expect_home="/tl/dashboard")

    def emailed(field):
        def handler(route):
            response = route.fetch()
            data = response.json()
            data.update({"email_sent": True, field: ""})
            route.fulfill(response=response, json=data)
        return handler

    # an invitation the API sent by e-mail: the address is named, no link is shown
    page.route(re.compile(r"/api/invitations$"), emailed("invite_url"))
    page.goto(BASE + "/tl/members")
    page.get_by_role("button", name="Add Member").first.click()
    dialog = page.get_by_role("dialog", name="Add Member")
    email = f"mailed.{STAMP}@example.com"
    dialog.get_by_label("Full name").fill(f"Mia Mailed {STAMP}")
    dialog.get_by_label("E-mail").fill(email)
    dialog.get_by_role("button", name="Create Invitation").click()
    created = page.get_by_role("dialog", name="Invitation created")
    created.wait_for(timeout=15000)
    check("an invitation sent by e-mail names the address it went to", created.get_by_text(re.compile(r"The invitation was sent by e-mail to")).is_visible()
          and created.get_by_text(email, exact=True).first.is_visible())
    check("no link field is shown when the API returns an empty invite_url", created.get_by_label("Invitation link").count() == 0
          and created.get_by_role("button", name="Copy link").count() == 0 and created.get_by_role("textbox").count() == 0)
    shot(page, "invite_emailed_stubbed")
    created.get_by_role("button", name="Done").click()
    page.unroute(re.compile(r"/api/invitations$"))

    # a reset link the API sent by e-mail
    page.route(re.compile(r"/api/users/[0-9a-f-]{36}/reset-link$"), emailed("reset_url"))
    page.get_by_role("searchbox", name="Search members").fill("Noah")
    page.get_by_role("link", name="Noah Patel").first.wait_for(timeout=15000)
    page.get_by_role("button", name="Actions for Noah Patel").click()
    page.get_by_role("menuitem", name="Create Reset Link").click()
    dialog = page.get_by_role("dialog", name="Password reset link")
    dialog.get_by_role("button", name="Create Reset Link").click()
    try:
        dialog.get_by_text(re.compile(r"The reset link was sent by e-mail to")).wait_for(timeout=15000)
        check("a reset link sent by e-mail names the address and shows no link", dialog.get_by_text("noah@example.com").first.is_visible()
              and dialog.get_by_label("Reset link").count() == 0 and dialog.get_by_role("textbox").count() == 0)
    except Exception:
        check("a reset link sent by e-mail names the address and shows no link", False)
    dialog.get_by_role("button", name="Done").click()
    page.unroute(re.compile(r"/api/users/[0-9a-f-]{36}/reset-link$"))

    # the API refuses a fourth search while three of this person's are queued or running
    refusal = "You already have 3 searches queued or running. Wait for one to finish or cancel it."
    page.route(re.compile(r"/api/searches$"), lambda route: route.fulfill(status=429, json={"error": refusal, "detail": ""})
               if route.request.method == "POST" else route.continue_())
    page.goto(BASE + "/tl/search")
    group = page.get_by_role("group", name=PICKER, exact=True)
    group.get_by_role("checkbox", name=re.compile("Samuel Okafor")).wait_for(timeout=15000)
    group.get_by_role("checkbox", name=re.compile("Samuel Okafor")).check()
    with expecting("429", "/api/searches"):
        page.get_by_role("button", name="Start Search").click()
        page.get_by_role("alert").filter(has_text=refusal).first.wait_for(timeout=10000)
    check("the API's refusal of one search too many (429) is shown in its own words", True)
    check("no progress window opens for a search that was not started", page.get_by_role("dialog", name="AI Job Search Started").count() == 0)
    ctx.close()


def step_running(browser):
    """The demo API finishes a search in well under a second, so 'queued' and 'running' never stay on screen
    long enough to test. This step feeds the UI the API's own run object with its status rewritten to
    'running' for the first polls (everything else is real), to exercise polling, restore, minimize and cancel."""
    print("== running-state behaviour (API responses for one run rewritten to 'running' for the first polls)")
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com", expect_home="/tl/dashboard")
    real = api(page, "/api/searches?mine=true&page_size=1")["items"][0]
    run_id = real["id"]
    running = {**real, "status": "running", "stage": "Verifying application links", "completed_at": None, "matched_count": 0, "assigned_count": 0,
               "attempts": 1, "warnings": [],
               "targets": [{**t, "status": "running", "stage": "Verifying application links", "message": "", "matched": 0, "assigned": 0} for t in real["targets"]]}
    resumed_note = "The background worker restarted during this search. It resumed automatically; candidates already finished were kept."
    state = {"detail_calls": [], "active_calls": 0, "cancel": None, "finish": False, "phase": "running"}

    def current():
        """The run as the API reports it in each phase: running, back to queued after a worker restart, running again (attempt 2)."""
        if state["phase"] == "running":
            run = running
        elif state["phase"] == "requeued":
            run = {**running, "status": "queued", "stage": "Waiting for the background worker", "attempts": 1, "warnings": [resumed_note],
                   "targets": [{**t, "status": "queued", "stage": "", "message": ""} for t in running["targets"]]}
        else:
            run = {**running, "attempts": 2, "warnings": [resumed_note]}
        return {**run, "cancel_requested": state["cancel"] is not None}

    def detail(route):
        state["detail_calls"].append(time.time())
        if state["finish"]:
            route.continue_()
        else:
            route.fulfill(json=current())

    def active(route):
        state["active_calls"] += 1
        route.fulfill(json={"items": [] if state["finish"] else [current()], "total": 0 if state["finish"] else 1, "page": 1, "page_size": 10})

    def cancel(route):
        request = route.request
        state["cancel"] = {"method": request.method, "csrf": request.headers.get("x-csrf-token", "")}
        route.fulfill(json={**running, "cancel_requested": True})

    page.route(re.compile(rf"/api/searches/{run_id}$"), detail)
    page.route(re.compile(r"/api/searches\?(?=.*status=active)(?=.*mine=true)"), active)
    page.route(re.compile(rf"/api/searches/{run_id}/cancel$"), cancel)

    with expecting("rendering with server caches disabled"):   # dev-only notice caused by request interception in this step
        page.reload()
        indicator = page.locator('[data-slot="search-indicator"]')
        indicator.wait_for(timeout=15000)
        try:
            indicator.get_by_text("Search running").wait_for(timeout=10000)
            check("on load, a search that is still active is restored as the floating indicator", True)
        except Exception:
            check("on load, a search that is still active is restored as the floating indicator", False, indicator.inner_text().replace("\n", " | "))
        check("the indicator was restored from GET /api/searches?status=active&mine=true", state["active_calls"] >= 1)

        open_nav(page)
        nav_link(page, "Team Members").click()
        page.wait_for_url(re.compile(r"/tl/members$"), timeout=15000)
        check("the running indicator stays while navigating", indicator.is_visible())

        indicator.get_by_role("button", name=re.compile("Show search progress")).click()
        window = page.get_by_role("dialog", name="AI Job Search Started")
        window.wait_for(timeout=5000)
        check("expanding the indicator shows the run as Running with its stage", window.locator('[data-slot="tag"][data-status="running"]').first.is_visible()
              and window.get_by_text("Verifying application links").first.is_visible())
        check("while running: Minimize, View Search and Cancel Search are offered", window.get_by_role("button", name="Minimize", exact=True).last.is_visible()
              and window.get_by_role("link", name="View Search").is_visible() and window.get_by_role("button", name="Cancel Search").is_visible())
        shot(page, "search_running_stubbed")

        # the window is not modal: something behind it can be used while it is open
        if not MOBILE:
            page.get_by_role("searchbox", name="Search members").fill("noah")
            try:
                page.get_by_role("link", name="Maya Chen").first.wait_for(state="detached", timeout=10000)
                check("the page behind the progress window stays interactive", page.get_by_role("link", name="Noah Patel").first.is_visible())
            except Exception:
                check("the page behind the progress window stays interactive", False)

        page.wait_for_timeout(5000)
        gaps = [b - a for a, b in zip(state["detail_calls"], state["detail_calls"][1:])]
        steady = [g for g in gaps if g > 0.5]
        check("the run is polled about every 1.5 s while it is running", len(steady) >= 2 and all(1.2 <= g <= 2.2 for g in steady), ", ".join(f"{g:.2f}s" for g in gaps))

        # the worker restarts: the run goes back to "queued" and later continues. That is a normal course, not an end.
        toasts_before = page.locator("[data-sonner-toast]").count()
        state["phase"] = "requeued"
        try:
            window.locator('[data-slot="tag"][data-status="queued"]').first.wait_for(timeout=10000)
            check("a run that goes back to 'queued' after 'running' is shown as queued, with the API's stage", window.get_by_text("Waiting for the background worker").first.is_visible())
        except Exception:
            check("a run that goes back to 'queued' after 'running' is shown as queued, with the API's stage", False)
        check("the API's note about the restart is shown", window.get_by_text(resumed_note).first.is_visible())
        check("a queued run can still be minimized, viewed and cancelled", window.get_by_role("link", name="View Search").is_visible()
              and window.get_by_role("button", name="Cancel Search").is_enabled())
        calls = len(state["detail_calls"])
        page.wait_for_timeout(3500)
        check("the run is still polled while it waits", len(state["detail_calls"]) >= calls + 2, f"{len(state['detail_calls']) - calls} more requests in 3.5 s")
        check("going back to 'queued' is not announced as the end of the search", page.locator("[data-sonner-toast]").filter(has_text=re.compile("^Search (completed|failed|cancelled|finished)")).count() == 0
              and page.locator("[data-sonner-toast]").count() <= toasts_before)
        state["phase"] = "resumed"
        try:
            window.locator('[data-slot="tag"][data-status="running"]').first.wait_for(timeout=10000)
            check("when the worker picks the run up again it is shown as running, marked as attempt 2",
                  window.locator('[data-slot="tag"][data-attempts="2"]').filter(has_text="Resumed, attempt 2").first.is_visible())
        except Exception:
            check("when the worker picks the run up again it is shown as running, marked as attempt 2", False)

        window.get_by_role("button", name="Cancel Search").click()
        confirm = page.get_by_role("alertdialog", name="Cancel this search?")
        confirm.wait_for(timeout=5000)
        check("Cancel Search asks for confirmation", True)
        confirm.get_by_role("button", name="Cancel search").click()
        window.get_by_role("button", name="Cancelling").wait_for(timeout=10000)
        check("Cancel Search calls POST /api/searches/{id}/cancel with the CSRF header", state["cancel"] is not None and state["cancel"]["method"] == "POST" and len(state["cancel"]["csrf"]) > 10,
              str({k: (v if k == "method" else f"{len(v)} chars") for k, v in (state["cancel"] or {}).items()}))

        window.get_by_role("button", name="Minimize", exact=True).last.click()
        indicator.wait_for(timeout=5000)
        check("Escape or Minimize folds the window back into the indicator", indicator.is_visible())

        state["finish"] = True      # from here on the API's real (completed) answer is passed through
        page.locator("[data-sonner-toast]").filter(has_text=re.compile("Search (completed|finished with problems)")).first.wait_for(timeout=15000)
        check("when the run ends a toast announces it", True)
        check("the indicator switches to the finished state with a link to the results", indicator.get_by_role("link", name="Results").is_visible())
        calls = len(state["detail_calls"])
        page.wait_for_timeout(4000)
        check("polling stops once the run has ended", len(state["detail_calls"]) <= calls + 1, f"{calls} -> {len(state['detail_calls'])} calls")
    ctx.close()



RESUME_TEXT = """Erin Example
Machine Learning Engineer

Experience
- 5 years building machine learning models in Python, PyTorch and TensorFlow
- Deployed services with Docker and Kubernetes on AWS
- SQL, Airflow and Spark for data pipelines

Education
B.S. Computer Science
"""


def step_people(browser):
    print("== people: a member's page, reset link, change password, theme, deactivation and what it does to the member's candidate")
    if "email" not in SHARED:
        skip("people step", "needs the member invited in the 'invite' step")
        return
    name, email = SHARED["name"], SHARED["email"]
    candidate, candidate_id = SHARED.get("candidate"), SHARED.get("candidate_id")      # assigned to this member in the 'candidates' step

    # Team Lead: the member's page (an employee: candidates, jobs, activity; no profile, no resume), then a reset link
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com", expect_home="/tl/dashboard")
    page.goto(BASE + "/tl/members")
    page.get_by_role("searchbox", name="Search members").fill(name)
    row = page.locator("tr, li").filter(has=page.get_by_role("link", name=name)).filter(visible=True).last
    row.wait_for(timeout=15000)
    check("a member's row has no resume state any more", "No resume" not in row.inner_text() and "Resume uploaded" not in row.inner_text(), row.inner_text().replace("\n", " | ")[:140])
    if candidate:
        check("a member's row counts the candidates assigned to the member", row.get_by_role("link", name=f"1 candidate of {name}").count() == 1 or "1 candidate" in row.inner_text(),
              row.inner_text().replace("\n", " | ")[:140])
    page.get_by_role("link", name=name).first.click()
    page.wait_for_url(re.compile(r"/tl/members/[0-9a-f-]{36}"), timeout=15000)
    page.get_by_role("heading", level=1, name=name).wait_for(timeout=15000)
    check("a member's page has no resume upload and no profile form",
          page.get_by_role("tab", name=re.compile("Profile")).count() == 0 and page.locator('input[type="file"]').count() == 0 and page.get_by_role("button", name="Save Profile").count() == 0)
    for tab in ("Overview", "Candidates", "Jobs", "Activity"):
        page.get_by_role("tab", name=re.compile(f"^{tab}")).click()
        page.wait_for_load_state("networkidle")
        check(f"member detail tab renders: {tab}", page.locator('[data-slot="error-state"]').filter(visible=True).count() == 0)
        if tab == "Candidates" and candidate:
            page.get_by_role("link", name=candidate, exact=True).filter(visible=True).first.wait_for(timeout=15000)
            check("the Candidates tab of a member lists the candidates the member is responsible for", True)
    no_overflow(page, "member detail")
    shot(page, "member_detail")

    page.get_by_role("button", name="Manage Account").click()
    page.get_by_role("menuitem", name="Create Reset Link").click()
    dialog = page.get_by_role("dialog", name="Password reset link")
    dialog.get_by_role("button", name="Create Reset Link").click()
    reset_url = dialog.get_by_label("Reset link").input_value(timeout=15000)
    check("a manager gets a one-time reset link, with the truth about e-mail", "/reset-password?token=" in reset_url and dialog.get_by_text("No e-mail was sent").is_visible(), reset_url[:55])
    dialog.get_by_role("button", name="Done").click()
    ctx.close()

    # the member uses the reset link
    ctx = new_context(browser)
    page = ctx.new_page()
    page.goto(local(reset_url))
    page.get_by_role("heading", level=1, name="Choose a new password").wait_for(timeout=15000)
    second = f"Second-Chance-{STAMP}!"
    page.get_by_label("New password", exact=True).fill(second)
    page.get_by_label("Repeat the new password").fill(second)
    page.get_by_role("button", name="Save new password").click()
    page.get_by_role("heading", name="Password updated").wait_for(timeout=15000)
    check("the reset link sets a new password", page.get_by_text("Password updated. Sign in with your new password.").is_visible())
    page.get_by_role("link", name="Sign in", exact=True).click()
    page.wait_for_url(re.compile(r"/login$"), timeout=15000)
    page.get_by_label("E-mail").fill(email)
    page.get_by_label("Password", exact=True).fill(second)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(re.compile(r"/member/dashboard$"), timeout=20000)
    check("sign-in works with the password set through the reset link", True)
    if candidate:
        page.get_by_role("link", name=candidate, exact=True).filter(visible=True).first.wait_for(timeout=15000)
        check("the new member's dashboard lists the candidate assigned to them", page.get_by_text("Your Team Lead has not assigned a candidate to you yet").count() == 0)

    # the member's settings: change password, then sign in with it
    open_nav(page)
    nav_link(page, "Settings").click()
    page.wait_for_url(re.compile(r"/member/settings$"), timeout=15000)
    page.get_by_label("Current password").wait_for(timeout=15000)
    check("the member's settings are the account only: no resume, no job-search profile", page.locator('input[type="file"]').count() == 0
          and page.get_by_role("progressbar", name="Profile completion").count() == 0 and page.get_by_role("button", name="Save Profile").count() == 0)
    no_overflow(page, "member settings")
    shot(page, "member_settings")
    third = f"Third-Time-{STAMP}!"
    page.get_by_label("Current password").fill("not-the-current-one")
    page.get_by_label("New password", exact=True).fill(third)
    page.get_by_label("Repeat the new password").fill(third)
    with expecting("/api/auth/change-password", "403"):
        page.get_by_role("button", name="Change Password").click()
        page.get_by_role("alert").filter(has_text="Current password is incorrect.").first.wait_for(timeout=10000)
    check("a wrong current password shows the API's message on that field", True)
    page.get_by_label("Current password").fill(second)
    page.get_by_role("button", name="Change Password").click()
    toast(page, "Password changed.")
    check("the member can change their password", True)

    # theme: the choice is applied and survives a reload
    page.get_by_role("button", name="Theme").click()
    page.get_by_role("menuitem", name="Dark").click()
    check("the dark theme is applied", page.evaluate("document.documentElement.classList.contains('dark')"))
    page.reload()
    page.wait_for_load_state("networkidle")
    check("the theme choice survives a reload", page.evaluate("document.documentElement.classList.contains('dark')"))
    shot(page, "member_settings_dark")
    page.get_by_role("button", name="Theme").click()
    page.get_by_role("menuitem", name="Match device").click()

    sign_out(page)
    page.get_by_label("E-mail").fill(email)
    page.get_by_label("Password", exact=True).fill(third)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(re.compile(r"/member/dashboard$"), timeout=20000)
    check("sign-in works with the changed password", True)
    ctx.close()

    # an account an Admin deactivated cannot be reactivated by the Team Lead: the API answers 403 and the screen says why
    actx = new_context(browser)
    apage = actx.new_page()
    login(apage, "admin@example.com")
    apage.goto(BASE + "/admin/members")
    apage.get_by_role("searchbox", name="Search members").fill(name)
    apage.get_by_role("link", name=name).first.wait_for(timeout=15000)
    apage.get_by_role("button", name=f"Actions for {name}").click()
    apage.get_by_role("menuitem", name="Deactivate").click()
    apage.get_by_role("alertdialog").get_by_role("button", name="Deactivate account").click()
    toast(apage, f"{name} was deactivated")

    lctx = new_context(browser)
    lpage = lctx.new_page()
    login(lpage, "lead.alpha@example.com")
    if candidate:
        # the member's candidate stays in the team, unassigned, and the Team Lead (who did not cause it) is told
        record = api(lpage, f"/api/candidates/{candidate_id}")
        check("deactivating a member leaves their candidate in the team, without a responsible member",
              record["assigned_member"] is None and record["team"]["name"] == "Team Alpha" and record["is_active"] is True and record["has_resume"] is True)
        told = bell_text(lpage, f"1 candidate has no responsible member any more: {name} was deactivated. Choose who takes over.")
        check("notification 'candidates unassigned': how many, whose they were, and why", True)
        told.click()
        lpage.wait_for_url(re.compile(r"/tl/candidates\?member=unassigned$"), timeout=15000)
        lpage.get_by_role("link", name=candidate, exact=True).filter(visible=True).first.wait_for(timeout=15000)
        check("the notification opens the candidates under the 'Unassigned' filter, where the candidate is",
              lpage.get_by_role("combobox", name="Responsible member").inner_text().strip() == "Unassigned" and lpage.get_by_role("link", name="Jordan Rivera").count() == 0)
        no_overflow(lpage, "tl candidates, unassigned")
    lpage.goto(BASE + "/tl/members")
    lpage.get_by_role("searchbox", name="Search members").fill(name)
    lpage.get_by_role("link", name=name).first.wait_for(timeout=15000)
    lpage.get_by_role("button", name=f"Actions for {name}").click()
    lmenu = lpage.get_by_role("menu")
    lmenu.wait_for(timeout=15000)
    check("a Team Lead is not offered Reactivate for an account an Admin deactivated, and is told why",
          lmenu.get_by_role("menuitem", name="Reactivate").count() == 0 and lmenu.get_by_text("Deactivated by an administrator", exact=True).is_visible())
    lpage.keyboard.press("Escape")
    if candidate:
        # a deactivated member cannot be made responsible: not offered, and refused by the API
        lpage.goto(BASE + f"/tl/candidates/{candidate_id}")
        lpage.get_by_role("button", name="Assign to a Member").click()
        dialog = lpage.get_by_role("dialog", name="Assign to a Member")
        dialog.get_by_role("combobox", name=re.compile("Responsible member")).click()
        offered = [o.inner_text().strip() for o in lpage.get_by_role("option").all()]
        lpage.keyboard.press("Escape")
        dialog.get_by_role("button", name="Cancel").click()
        member = next(u for u in api(lpage, f"/api/users?role=MEMBER&q={email}&page_size=5")["items"] if u["email"] == email)
        with expecting("409", f"/api/candidates/{candidate_id}"):
            refused = send(lpage, "PATCH", f"/api/candidates/{candidate_id}", {"assigned_member_id": member["id"]})
            lpage.wait_for_timeout(300)
        check("a deactivated member is not offered as responsible member, and the API refuses them (409)", name not in offered and "Maya Chen" in offered and refused == 409, f"HTTP {refused}")
    lctx.close()

    apage.get_by_role("button", name=f"Actions for {name}").click()
    apage.get_by_role("menuitem", name="Reactivate").click()
    apage.get_by_role("alertdialog").get_by_role("button", name="Reactivate account").click()
    toast(apage, f"{name} can sign in again")
    check("the Admin can reactivate that account", True)
    if candidate:
        check("reactivating the member does not hand the candidate back by itself", api(apage, f"/api/candidates/{candidate_id}")["assigned_member"] is None)
    actx.close()


def step_admin_more(browser):
    print("== admin: CSV import, import from URL, verify, teams, transfer, deactivate, settings, activity log, presets")
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "admin@example.com", expect_home="/admin/dashboard")
    base_dir = WORK_DIR

    # CSV import: one good row, one row without a title
    csv_path = f"{base_dir}/jobs-{STAMP}.csv"
    with open(csv_path, "w") as handle:
        handle.write("company,title,city,state,work_mode,requirements,canonical_url\n")
        handle.write(f"CSV Corp {STAMP},CSV Imported Engineer {STAMP},Denver,CO,Remote,Python;SQL,https://careers.example.invalid/csv/{STAMP}\n")
        handle.write(f"CSV Corp {STAMP},,Denver,CO,Remote,,\n")
    page.goto(BASE + "/admin/jobs")
    page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="Import CSV").click()
    dialog = page.get_by_role("dialog", name="Import CSV")
    dialog.get_by_label("CSV file").set_input_files(csv_path)
    dialog.get_by_role("button", name="Import Jobs").click()
    dialog.get_by_text("Rows that were not imported").wait_for(timeout=20000)
    figures = dialog.locator("dl").inner_text().replace("\n", " ")
    check("CSV import reports what was added and what was skipped", "Added 1" in figures and "Skipped 1" in figures and dialog.get_by_text(re.compile("Row 3")).is_visible(), figures)
    dialog.get_by_role("button", name="Done").click()
    page.get_by_role("link", name=f"CSV Imported Engineer {STAMP}").first.wait_for(timeout=15000)
    check("the imported job is in the list", True)

    # Import from URL: a host that does not exist, so the backend makes no outside request
    page.get_by_role("button", name="Import from URL").click()
    dialog = page.get_by_role("dialog", name="Import from URL")
    dialog.get_by_label("Address of the posting").fill("not a url")
    dialog.get_by_role("button", name="Read Posting").click()
    check("an invalid address is refused in the form", dialog.get_by_role("alert").filter(has_text=re.compile("full address")).first.is_visible())
    url = f"https://careers.example.invalid/jobs/{STAMP}"
    dialog.get_by_label("Address of the posting").fill(url)
    dialog.get_by_role("button", name="Read Posting").click()
    dialog.get_by_text("The page could not be read as a job posting.").wait_for(timeout=30000)
    check("Import from URL shows a review step with the API's verdict", dialog.get_by_label(re.compile("^Posting address")).input_value() == url)
    imported = f"URL Imported Engineer {STAMP}"
    dialog.get_by_label("Job title").fill(imported)
    dialog.get_by_label("Company", exact=True).fill(f"URL Corp {STAMP}")
    dialog.get_by_role("button", name="Save Job").click()
    toast(page, "Job added")
    page.get_by_role("link", name=imported).first.wait_for(timeout=15000)
    check("the reviewed job is saved", True)

    # verify (the host does not resolve, so this is a failed check, recorded honestly)
    page.get_by_role("link", name=imported).first.click()
    page.wait_for_url(re.compile(r"/admin/jobs/[0-9a-f-]{36}$"), timeout=15000)
    page.get_by_role("button", name="Verify Now").wait_for(timeout=15000)
    check("a manual job starts as not verified", page.locator('[data-slot="tag"]').filter(has_text="Not verified").first.is_visible())
    page.get_by_role("button", name="Verify Now").click()
    toast(page, "Checked:", timeout=40000)
    page.get_by_text(re.compile("Last checked")).first.wait_for(timeout=15000)
    check("Verify Now checks the posting and records the outcome", page.locator('[data-slot="tag"]').filter(has_text=re.compile("Verification failed|Posting closed|Verified")).first.is_visible())
    page.get_by_role("button", name="Edit").click()
    dialog = page.get_by_role("dialog", name="Edit Job")
    dialog.get_by_label(re.compile("^Seniority")).fill("Senior")
    dialog.get_by_role("button", name="Save Changes").click()
    toast(page, "Job saved")
    check("Edit Job saves", page.get_by_text("Senior", exact=True).first.is_visible())

    # teams: create, create a Team Lead for it, rename, deactivate, reactivate
    team = f"E2E Team {STAMP}"
    page.goto(BASE + "/admin/teams")
    page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="Create Team", exact=True).first.click()
    dialog = page.get_by_role("dialog", name="Create Team", exact=True)
    dialog.get_by_label("Team name").fill("Team Alpha")
    with expecting("/api/teams", "409"):
        dialog.get_by_role("button", name="Create Team", exact=True).click()
        dialog.get_by_role("alert").filter(has_text="A team with this name already exists").first.wait_for(timeout=10000)
    check("a duplicate team name shows the API's message on the field", True)
    dialog.get_by_label("Team name").fill(team)
    dialog.get_by_role("button", name="Create Team", exact=True).click()
    toast(page, f"Team {team} created")
    page.get_by_role("link", name=team).first.wait_for(timeout=15000)
    check("Create Team works", True)

    page.get_by_role("button", name=f"Actions for {team}").click()
    page.get_by_role("menuitem", name="Create Team Lead").click()
    dialog = page.get_by_role("dialog", name=f"Create Team Lead for {team}")
    lead_email = f"lead.e2e.{STAMP}@example.com"
    dialog.get_by_label("Full name").fill(f"Lee Lead {STAMP}")
    dialog.get_by_label("E-mail").fill(lead_email)
    dialog.get_by_role("button", name="Create Invitation").click()
    created = page.get_by_role("dialog", name="Invitation created")
    created.wait_for(timeout=15000)
    lead_link = created.get_by_label("Invitation link").input_value()
    check("Create Team Lead issues an invitation for the team", "/invite/accept?token=" in lead_link
          and created.get_by_text(re.compile(f"Team Lead, {team}")).is_visible())
    created.get_by_role("button", name="Done").click()
    page.get_by_text(f"Lee Lead {STAMP}").first.wait_for(timeout=15000)
    check("the team shows its (invited) Team Lead", True)

    # the new Team Lead accepts, so that there is someone to sign in once the team is deactivated
    lead_password = f"Lead-Of-Team-{STAMP}!"
    gctx = new_context(browser)
    gpage = gctx.new_page()
    gpage.goto(local(lead_link))
    gpage.get_by_role("heading", level=1, name=f"Welcome, Lee Lead {STAMP}").wait_for(timeout=15000)
    gpage.get_by_label("Password", exact=True).fill(lead_password)
    gpage.get_by_label("Repeat the password").fill(lead_password)
    gpage.get_by_role("button", name="Create account").click()
    gpage.wait_for_url(re.compile(r"/login$"), timeout=15000)
    gctx.close()

    page.get_by_role("button", name=f"Actions for {team}").click()
    page.get_by_role("menuitem", name="Rename or Edit").click()
    dialog = page.get_by_role("dialog", name="Edit Team")
    renamed = f"E2E Squad {STAMP}"
    dialog.get_by_label("Team name").fill(renamed)
    dialog.get_by_role("button", name="Save Team").click()
    toast(page, "Team saved")
    page.get_by_role("link", name=renamed).first.wait_for(timeout=15000)
    check("renaming a team works", True)

    page.get_by_role("button", name=f"Actions for {renamed}").click()
    page.get_by_role("menuitem", name="Deactivate Team").click()
    confirm = page.get_by_role("alertdialog")
    check("deactivating a team asks for confirmation", confirm.get_by_text(re.compile("Members, candidates, assignments and history stay")).is_visible())
    confirm.get_by_role("button", name="Deactivate team").click()
    toast(page, f"{renamed} deactivated")
    try:
        page.locator("tr, li").filter(has_text=renamed).locator('[data-slot="tag"]').filter(has_text="Deactivated").filter(visible=True).first.wait_for(timeout=15000)
        check("the team is shown as deactivated", True)
    except Exception:
        check("the team is shown as deactivated", False)
    page.get_by_role("link", name=renamed).first.click()
    page.wait_for_url(re.compile(r"/admin/teams/[0-9a-f-]{36}"), timeout=15000)
    page.get_by_role("heading", level=1, name=renamed).wait_for(timeout=15000)
    for tab in ("Candidates", "Members", "Assignments", "Queue", "Searches", "Overview"):
        page.get_by_role("tab", name=re.compile(f"^{tab}")).click()
        page.wait_for_load_state("networkidle")
        check(f"team page tab renders: {tab}", page.locator('[data-slot="error-state"]').filter(visible=True).count() == 0)
    no_overflow(page, "team detail")

    # the Team Lead of the deactivated team can look, is told why nothing can be changed, and gets the API's 409 text
    tctx = new_context(browser)
    tpage = tctx.new_page()
    login(tpage, lead_email, password=lead_password, expect_home="/tl/dashboard")
    try:
        tpage.locator('[data-slot="team-notice"]').filter(has_text=f"{renamed} is deactivated").wait_for(timeout=15000)
        check("a Team Lead of a deactivated team is told so on every page", True)
    except Exception:
        check("a Team Lead of a deactivated team is told so on every page", False)
    check("their dashboard still renders", tpage.locator('[data-slot="error-state"]').filter(visible=True).count() == 0
          and tpage.locator('[data-slot="kpi"]').filter(visible=True).count() == 6)
    tpage.goto(BASE + "/tl/members")
    tpage.get_by_role("button", name="Add Member").first.click()
    tdialog = tpage.get_by_role("dialog", name="Add Member")
    tdialog.get_by_label("Full name").fill(f"Nobody Yet {STAMP}")
    tdialog.get_by_label("E-mail").fill(f"nobody.{STAMP}@example.com")
    with expecting("409", "/api/invitations"):
        tdialog.get_by_role("button", name="Create Invitation").click()
        tdialog.get_by_role("alert").filter(has_text="Your team is deactivated. An administrator has to reactivate it before you can make changes.").first.wait_for(timeout=10000)
    check("a change by the Team Lead of a deactivated team shows the API's own refusal (409)", True)
    no_overflow(tpage, "team lead of a deactivated team")
    shot(tpage, "tl_team_deactivated")
    tctx.close()

    # reactivate the team, then take its Team Lead away again
    page.goto(BASE + "/admin/teams")
    page.get_by_role("button", name=f"Actions for {renamed}").click()
    page.get_by_role("menuitem", name="Reactivate Team").click()
    page.get_by_role("alertdialog").get_by_role("button", name="Reactivate team").click()
    toast(page, f"{renamed} reactivated")
    check("reactivating a team works", True)
    page.get_by_role("button", name=f"Actions for {renamed}").click()
    page.get_by_role("menuitem", name="Remove Team Lead").click()
    confirm = page.get_by_role("alertdialog")
    confirm.get_by_role("button", name="Remove Team Lead").click()
    toast(page, "Team Lead removed")
    check("removing a Team Lead asks first, then works", True)

    # move the invited member to Team Beta, deactivate and reactivate them
    if "name" in SHARED:
        member = SHARED["name"]
        candidate, candidate_id = SHARED.get("candidate"), SHARED.get("candidate_id")
        if candidate:
            # the Admin makes the member responsible for the candidate again (it became unassigned in the 'people' step)
            page.goto(BASE + f"/admin/candidates/{candidate_id}")
            page.get_by_role("heading", level=1, name=candidate).wait_for(timeout=15000)
            check("an Admin sees a candidate of any team, with its team", page.get_by_text("Candidate of Team Alpha.").is_visible())
            page.get_by_role("button", name="Assign to a Member").click()
            dialog = page.get_by_role("dialog", name="Assign to a Member")
            dialog.get_by_role("combobox", name=re.compile("Responsible member")).click()
            offered = [o.inner_text().strip() for o in page.get_by_role("option").all()]
            check("for an Admin too, only members of the candidate's own team are offered", member in offered and "Maya Chen" in offered and "Priya Nair" not in offered, str(offered))
            page.get_by_role("option", name=member, exact=True).click()
            dialog.get_by_role("button", name="Assign Candidate").click()
            toast(page, f"{member} is now responsible for {candidate}")
            priya = next(u for u in api(page, "/api/users?role=MEMBER&q=priya&page_size=5")["items"] if u["email"] == "priya@example.com")
            with expecting("422", f"/api/candidates/{candidate_id}"):
                refused = send(page, "PATCH", f"/api/candidates/{candidate_id}", {"assigned_member_id": priya["id"]})
                page.wait_for_timeout(300)
            check("the API refuses a member of another team as responsible member, even for an Admin (422)", refused == 422
                  and api(page, f"/api/candidates/{candidate_id}")["assigned_member"]["name"] == member, f"HTTP {refused}")
        page.goto(BASE + "/admin/members")
        page.get_by_role("searchbox", name="Search members").fill(member)
        page.get_by_role("link", name=member).first.wait_for(timeout=15000)
        page.get_by_role("button", name=f"Actions for {member}").click()
        page.get_by_role("menuitem", name="Move to Another Team").click()
        dialog = page.get_by_role("dialog", name=f"Move {member} to another team")
        dialog.get_by_role("combobox", name="New team").click()
        page.get_by_role("option", name="Team Beta").click()
        dialog.get_by_role("button", name="Move Member").click()
        toast(page, f"{member} moved to Team Beta")
        check("an Admin can move a member to another team", True)
        if candidate:
            record = api(page, f"/api/candidates/{candidate_id}")
            check("the moved member's candidate stays in its team, with resume and history, and has no responsible member",
                  record["team"]["name"] == "Team Alpha" and record["assigned_member"] is None and record["has_resume"] is True)
            page.goto(BASE + "/admin/candidates?member=unassigned")
            page.get_by_role("link", name=candidate, exact=True).filter(visible=True).first.wait_for(timeout=15000)
            row = page.locator("tr, li").filter(has=page.get_by_role("link", name=candidate, exact=True)).filter(visible=True).last
            check("the Admin's candidate list shows it under 'Unassigned', with its team", "Team Alpha" in row.inner_text() and "Unassigned" in row.inner_text(),
                  row.inner_text().replace("\n", " | ")[:140])
            no_overflow(page, "admin candidates, unassigned")
            lctx = new_context(browser)
            lpage = lctx.new_page()
            login(lpage, "lead.alpha@example.com")
            bell_text(lpage, f"1 candidate has no responsible member any more: {member} was moved to another team. Choose who takes over.")
            check("the Team Lead is told that a candidate lost its member because the member was moved to another team", True)
            lctx.close()
            page.goto(BASE + "/admin/members")
            page.get_by_role("searchbox", name="Search members").fill(member)
            page.get_by_role("link", name=member).first.wait_for(timeout=15000)
        page.get_by_role("button", name=f"Actions for {member}").click()
        page.get_by_role("menuitem", name="Deactivate").click()
        page.get_by_role("alertdialog").get_by_role("button", name="Deactivate account").click()
        toast(page, f"{member} was deactivated")
        check("deactivating an account asks first, then works", True)
        page.get_by_role("button", name=f"Actions for {member}").click()
        page.get_by_role("menuitem", name="Reactivate").click()
        page.get_by_role("alertdialog").get_by_role("button", name="Reactivate account").click()
        toast(page, f"{member} can sign in again")
        check("reactivating an account works", True)
    else:
        skip("member transfer / deactivate", "needs the member invited in the 'invite' step")

    # settings: the watermark switch reaches members; integrations are reported as they are
    page.goto(BASE + "/admin/settings")
    switch = page.get_by_role("switch", name="Show a watermark on member job views")
    switch.wait_for(timeout=15000)
    was_on = switch.get_attribute("aria-checked") == "true"
    check("watermark setting is on in the demo data", was_on)
    try:
        switch.click()
        page.get_by_role("button", name="Save Settings").click()
        toast(page, "Settings saved")
        mctx = new_context(browser)
        mpage = mctx.new_page()
        login(mpage, "maya@example.com")
        mpage.goto(BASE + "/member/jobs")
        mpage.locator('[data-slot="job-card"]').filter(visible=True).first.wait_for(timeout=15000)
        check("with the setting off, member job views carry no watermark", mpage.locator('[data-slot="watermark"]').count() == 0)
        mctx.close()
    finally:
        page.goto(BASE + "/admin/settings")
        switch = page.get_by_role("switch", name="Show a watermark on member job views")
        switch.wait_for(timeout=15000)
        if switch.get_attribute("aria-checked") != "true":
            switch.click()
            page.get_by_role("button", name="Save Settings").click()
            toast(page, "Settings saved")
    restored = api(page, "/api/settings")["watermark_enabled"]
    check("the watermark setting was put back on", restored is True)
    page.get_by_role("tab", name="Integrations").click()
    page.get_by_role("heading", name="AI providers").wait_for(timeout=15000)
    check("integrations report the AI providers, worker, e-mail and sources as the API states them",
          page.get_by_text("No key configured").first.is_visible() and page.get_by_role("heading", name="Job sources").is_visible()
          and page.get_by_role("switch", name="Use Greenhouse in searches").is_visible())
    no_overflow(page, "admin settings integrations")
    shot(page, "admin_integrations")

    # activity log
    page.goto(BASE + "/admin/activity")
    page.get_by_role("combobox", name="Kind of activity").click()
    with page.expect_response(lambda r: "/api/audit-logs" in r.url and "action=team" in r.url) as info:
        page.get_by_role("option", name="Teams").click()
    check("the activity log filter is sent to the API", info.value.ok)
    page.get_by_text("Created a team").first.wait_for(timeout=15000)
    check("the activity log lists what was done in this run", True)

    # search presets
    page.goto(BASE + "/admin/search")
    page.get_by_role("group", name=PICKER, exact=True).wait_for(timeout=15000)
    page.get_by_label("Results per candidate").fill("7")
    page.get_by_role("button", name=re.compile("^Presets")).click()
    preset = f"E2E preset {STAMP}"
    page.get_by_label("Save the current settings as").fill(preset)
    page.get_by_role("button", name="Save", exact=True).click()
    toast(page, f'Preset "{preset}" saved')
    page.keyboard.press("Escape")
    page.get_by_label("Results per candidate").fill("3")
    page.get_by_role("button", name=re.compile("^Presets")).click()
    page.get_by_role("listitem").filter(has_text=preset).get_by_role("button", name="Apply").click()
    check("a saved preset restores the search settings", page.get_by_label("Results per candidate").input_value() == "7")
    page.get_by_role("button", name=re.compile("^Presets")).click()
    page.get_by_role("button", name=f"Delete preset {preset}").click()
    toast(page, "Preset deleted")
    check("a preset can be deleted", True)
    check("an Admin can pick a whole team in the search form", page.get_by_role("combobox", name="Whole team").is_visible())
    page.get_by_role("combobox", name="Whole team").click()
    check("a whole team is counted in active candidates", page.get_by_role("option", name=re.compile(r"^Team Beta \(5 active candidates\)$")).is_visible(),
          " | ".join(o.inner_text().strip() for o in page.get_by_role("option").all()))
    page.keyboard.press("Escape")
    people = page.get_by_role("group", name=PICKER, exact=True)
    people.get_by_role("checkbox").first.wait_for(timeout=15000)
    check("an Admin's candidate list spans the teams and names each candidate's team", "Team Alpha" in people.inner_text() and "Team Beta" in people.inner_text())
    ctx.close()


def step_admins(browser):
    print("== admin: Settings, tab Admins: accounts, invite an Admin, resend, revoke; a Team Lead sees none of it")
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "admin@example.com", expect_home="/admin/dashboard")
    page.goto(BASE + "/admin/settings?tab=admins")
    page.wait_for_load_state("networkidle")
    check("Settings has the tab Admins, and the address opens it", page.get_by_role("tab", name="Admins", exact=True).get_attribute("aria-selected") == "true")
    accounts = page.locator('[data-slot="data-table"]').filter(has=page.get_by_text("admin@example.com")).first
    accounts.wait_for(timeout=15000)
    check("the Admin accounts list shows the signed-in Admin, marked as You, with the account's status",
          "You" in accounts.inner_text() and "Active" in accounts.inner_text(), accounts.inner_text()[:120])
    check("the list has a last sign-in for the signed-in Admin", "Never signed in" not in accounts.inner_text())
    check("the form says plainly what an Admin can do, before anything is sent",
          page.locator('[data-slot="admin-access-warning"]').filter(visible=True).filter(has_text="can see and change everything in the portal").count() == 1)
    name, email = f"Ada Second {STAMP}", f"ada.second.{STAMP}@example.com"
    page.get_by_label("Full name").fill(name)
    page.get_by_label("E-mail").fill("not-an-address")
    page.get_by_role("button", name="Invite Admin").click()
    check("a wrong e-mail address is refused in the form", page.get_by_text("Enter a valid e-mail address.").first.is_visible())
    page.get_by_label("E-mail").fill(email)
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/api/invitations")) as sent:
        page.get_by_role("button", name="Invite Admin").click()
    body = sent.value.post_data_json or {}
    check("the invitation asks for the role ADMIN and no team", body.get("role") == "ADMIN" and not body.get("team_id") and not body.get("team_name"), str(body))
    page.get_by_label("Invitation link").wait_for(timeout=15000)
    first_link = page.get_by_label("Invitation link").input_value()
    check("without a mail server the link is shown once, and the page says no e-mail was sent",
          "/invite/accept?token=" in first_link and page.get_by_text("No e-mail was sent").first.is_visible())
    pending = page.locator('[data-slot="data-table"]').filter(has=page.get_by_text(email)).first
    pending.wait_for(timeout=15000)
    check("the invitation is listed as pending, with who sent it", "Pending" in pending.inner_text() and "by " in pending.inner_text(), pending.inner_text()[:160])
    check("an invited Admin is not listed as an account before choosing a password", email not in accounts.inner_text())
    no_overflow(page, "settings, Admins")
    shot(page, "settings_admins")

    # the invited person sees what the account will be
    guest = browser.new_context(viewport={"width": A.w, "height": A.h})
    gpage = guest.new_page()
    gpage.goto(local(first_link))
    gpage.get_by_role("heading", name=f"Welcome, {name}").wait_for(timeout=15000)
    check("the page that accepts the invitation names the role Admin, all teams, and what an Admin can do",
          gpage.get_by_text("Admin", exact=True).first.is_visible() and gpage.get_by_text("All teams").is_visible()
          and gpage.get_by_text("can see and change everything in the portal").is_visible())

    # resend: a new link, the old one is dead; revoke: the new one is dead as well
    pending.get_by_role("button", name=f"Resend the invitation for {name}").click()
    fresh = page.get_by_role("dialog", name="New invitation link")
    fresh.wait_for(timeout=15000)
    second_link = fresh.get_by_label("Invitation link").input_value()
    check("Resend gives a new link", "/invite/accept?token=" in second_link and second_link != first_link)
    fresh.get_by_role("button", name="Done").click()
    with expecting("/api/invitations/validate"):
        gpage.goto(local(first_link))
        gpage.get_by_role("heading", name="This invitation cannot be used").wait_for(timeout=15000)
        check("the link that was replaced no longer works", True)
        pending.get_by_role("button", name=f"Revoke the invitation for {name}").click()
        page.get_by_role("alertdialog").get_by_role("button", name="Revoke invitation").click()
        toast(page, "Invitation revoked")
        gpage.goto(local(second_link))
        gpage.get_by_role("heading", name="This invitation cannot be used").wait_for(timeout=15000)
        check("a revoked Admin invitation gives no account", True)
    guest.close()
    expect(pending).to_contain_text("Revoked", timeout=10000)
    again = pending.get_by_role("button", name=f"Resend the invitation for {name}")
    again.wait_for(timeout=10000)                       # the confirmation dialog hides the page from role queries while it closes
    check("the revoked invitation stays listed, as revoked, and can be sent again",
          again.is_visible() and pending.get_by_role("button", name=f"Revoke the invitation for {name}").count() == 0)

    # the Invitations screen offers the same, for an Admin only
    page.goto(BASE + "/admin/invitations")
    page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="Invite Admin").click()
    dialog = page.get_by_role("dialog", name="Invite Admin")
    dialog.wait_for(timeout=10000)
    check("Invite Admin on the Invitations screen shows the same warning and asks for no team",
          dialog.locator('[data-slot="admin-access-warning"]').is_visible() and dialog.get_by_role("combobox").count() == 0)
    dialog.get_by_role("button", name="Cancel").click()
    pick_option(page, page, "Role", "Admin")
    page.wait_for_url(re.compile(r"role=ADMIN"), timeout=10000)
    page.get_by_text(email).first.wait_for(timeout=15000)
    check("the role filter narrows the list to Admin invitations", "The whole portal" in page.locator('[data-slot="data-table"]').first.inner_text())
    ctx.close()

    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com", expect_home="/tl/dashboard")
    page.goto(BASE + "/admin/settings?tab=admins")
    page.wait_for_load_state("networkidle")
    check("a Team Lead opening Settings, Admins is sent to their own area", "/tl/" in page.url, page.url)
    page.goto(BASE + "/tl/settings?tab=admins")
    page.wait_for_load_state("networkidle")
    check("a Team Lead's settings have no Admins tab and no Admin list",
          page.get_by_role("tab", name="Admins").count() == 0 and "Admin accounts" not in page.locator("main").inner_text())
    page.goto(BASE + "/tl/invitations")
    page.wait_for_load_state("networkidle")
    check("a Team Lead's Invitations screen offers neither Invite Admin nor a role filter",
          page.get_by_role("button", name="Invite Admin").count() == 0 and page.get_by_role("combobox", name="Role").count() == 0)
    listed = api(page, "/api/invitations?role=ADMIN")
    people = api(page, "/api/users?role=ADMIN")
    check("the API gives a Team Lead no Admin invitation and no Admin account", listed.get("total") == 0 and people.get("items") == [], f"{listed} / {people}")
    with expecting("/api/invitations"):
        refused = send(page, "POST", "/api/invitations", {"name": "Eve Lead", "email": f"eve.{STAMP}@example.com", "role": "ADMIN"})
    check("the API refuses a Team Lead who asks for an Admin invitation", refused == 403, str(refused))
    ctx.close()


STEPS = {"auth": step_auth, "member": step_member, "my_candidates": step_my_candidates, "invite": step_invite, "search": step_search,
         "candidates": step_candidates, "search_form": step_search_form, "people": step_people,
         "tracking": step_tracking, "review": step_review, "admin": step_admin, "admin_more": step_admin_more, "misc": step_misc, "stubbed": step_stubbed, "running": step_running,
         # Last: these leave an invited Admin and a revoked invitation behind, which no earlier step should have to count.
         "admins": step_admins}

with sync_playwright() as pw:
    # Nothing in this run may reach a real external site: every host except this machine fails to resolve.
    # (Request interception is avoided on purpose: it turns the browser's HTTP cache off, which changes how pages load.)
    browser = pw.chromium.launch(args=["--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost"])
    print(f"### viewport {A.w}x{A.h}  stamp {STAMP}")
    for name, fn in STEPS.items():
        if A.only and name not in A.only.split(","):
            continue
        try:
            if name == "stubbed":      # request interception turns the HTTP cache off, which the dev server remarks on
                with expecting("rendering with server caches disabled"):
                    fn(browser)
            else:
                fn(browser)
        except Exception as exc:  # a step that cannot continue is one failed check, not the end of the run
            check(f"step '{name}' ran to the end", False, f"{type(exc).__name__}: {str(exc)[:400]}")
            traceback.print_exc(limit=3)
    browser.close()

failed = [r for r in results if not r[0]]
print(f"\n### {A.w}x{A.h}: {len(results) - len(failed)} passed, {len(failed)} failed, {len(skipped)} skipped, {len(problems)} unexpected console/network problems")
for name, reason in skipped:
    print("  SKIPPED:", name, "|", reason)
for _, name, detail in failed:
    print("  FAILED:", name, "|", detail)
for p in problems:
    print("  PROBLEM:", p)
sys.exit(1 if failed or problems else 0)
