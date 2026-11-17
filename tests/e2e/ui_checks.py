"""Browser checks of the whole web app against the demo stack (see tests/e2e/run.sh, which starts it).

    python tests/e2e/ui_checks.py --base http://127.0.0.1:3011 --w 1280 --h 900
    python tests/e2e/ui_checks.py --base http://127.0.0.1:3011 --w 390 --h 844

The run changes data (it invites people, assigns jobs, marks applications), so start from freshly seeded demo data.

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
SHARED = {}           # what one step leaves for the next (the member invited during the run)
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


# --------------------------------------------------------------------------------------------- steps
def step_auth(browser):
    print("== auth and role redirects")
    ctx = new_context(browser)
    page = ctx.new_page()
    page.goto(BASE + "/")
    check("signed out: / goes to /login", page.url.rstrip("/").endswith("/login"), page.url)
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
    for title in ("Pipeline", "Applications over time", "Status distribution", "Weekly activity", "Pending vs applied"):
        check(f"dashboard panel present: {title}", page.get_by_role("heading", name=title, exact=True).is_visible())
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

    title = cards.first.get_by_role("link").inner_text().strip()
    cards.first.get_by_role("link").click()
    page.wait_for_url(re.compile(r"/member/jobs/[0-9a-f-]{36}$"), timeout=15000)
    assignment_id = page.url.rsplit("/", 1)[1]
    page.get_by_role("heading", level=1, name=title).wait_for(timeout=15000)
    page.wait_for_load_state("networkidle")
    check("job detail page opens", True, title)
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

    for path, heading in (("/member/applications", "Applications"), ("/member/activity", "Activity"), ("/member/profile", "Profile")):
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        check(f"{path} loads", page.get_by_role("heading", level=1, name=heading).is_visible())
        no_overflow(page, path)
    page.goto(BASE + "/member/applications")
    try:
        page.get_by_role("link", name=title).first.wait_for(timeout=15000)
        check("applications list includes the job just applied to", True)
    except Exception:
        check("applications list includes the job just applied to", False, title)

    # navigation drawer on small screens / sidebar on wide ones
    open_nav(page)
    nav_link(page, "My Jobs").click()
    page.wait_for_url(re.compile(r"/member/jobs$"), timeout=15000)
    check("navigation works from the " + ("drawer" if MOBILE else "sidebar"), True)

    sign_out(page)
    check("member signed out", page.url.endswith("/login"))
    page.goto(BASE + "/member/dashboard")
    check("after sign-out the dashboard needs a sign-in again", "/login?next=%2Fmember%2Fdashboard" in page.url, page.url)
    ctx.close()


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


def run_search(page, area, members, titles, per_member, review, expect_skipped=None):
    """Fill the search form and start it. Returns the progress window locator."""
    page.goto(BASE + f"{area}/search")
    page.wait_for_load_state("networkidle")
    group = page.get_by_role("group", name="Members")
    group.wait_for(timeout=15000)
    for member in members:
        group.get_by_role("checkbox", name=re.compile(member)).check()
    field = page.get_by_label(re.compile("^Job titles"))
    for t in titles:
        field.fill(t)
        field.press("Enter")
    count = page.get_by_label("Results per member")
    count.fill(str(per_member))
    if review:
        page.get_by_role("radio", name=re.compile("Search first, review, then assign")).check()
    page.get_by_role("button", name="Start Search").click()
    window = page.get_by_role("dialog", name="AI Job Search Started")
    window.wait_for(timeout=15000)
    return window


def step_search(browser):
    print("== team lead: auto-assign search, progress window, member sees new jobs")
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com", expect_home="/tl/dashboard")
    noah = next(u for u in api(page, "/api/users?role=MEMBER&page_size=50")["items"] if u["email"] == "noah@example.com")
    before = api(page, f"/api/assignments?member_id={noah['id']}&page_size=1")["total"]

    page.goto(BASE + "/tl/search")
    page.wait_for_load_state("networkidle")
    group = page.get_by_role("group", name="Members")
    group.wait_for(timeout=15000)
    names = group.inner_text()
    check("team lead sees only their own team in the member list", "Noah Patel" in names and "Maya Chen" in names and "Priya Nair" not in names and "Quinn Lopez" not in names)
    check("the form says the count is per member", page.get_by_text(re.compile("The count is per member")).is_visible())
    check("the form says a blank override uses each member's saved value", page.get_by_text(re.compile("Leave a field empty to use each member's own")).is_visible())
    check("both modes are offered", page.get_by_role("radio", name=re.compile("Find & Assign Matching Jobs")).is_visible()
          and page.get_by_role("radio", name=re.compile("Search first, review, then assign")).is_visible())
    no_overflow(page, "search form")
    shot(page, "search_form")

    titles = ["Data Scientist"] if not MOBILE else ["Backend Engineer"]
    window = run_search(page, "/tl", ["Noah Patel"], titles, 6, review=False)
    check("progress window 'AI Job Search Started' opens at once", window.is_visible())
    check("progress window lists the selected member", window.get_by_text("Noah Patel").first.is_visible())
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
    rows = page.get_by_role("checkbox", name=re.compile("^Select .* for Noah Patel"))
    rows.first.wait_for(timeout=15000)
    check("the results page lists the jobs found", rows.count() > 0, f"{rows.count()} result rows")
    after = api(page, f"/api/assignments?member_id={noah['id']}&page_size=1")["total"]
    check("the member has new assignments", after == before + assigned, f"{before} -> {after}")
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
    check("the member sees the newly assigned jobs", cards.count() >= 1 and titles[0].split()[0] in cards.first.inner_text(), f"{cards.count()} cards for '{titles[0]}'")
    bell = page.get_by_role("button", name=re.compile("Notifications"))
    check("the member has a notification about it", "unread" in (bell.get_attribute("aria-label") or ""))
    bell.click()
    try:
        page.get_by_text(re.compile("Lena Alpha assigned you")).first.wait_for(timeout=10000)
        check("the notification names who assigned the jobs", True)
    except Exception:
        check("the notification names who assigned the jobs", False)
    page.get_by_role("button", name="Mark all as read").click()
    try:
        expect(bell).to_have_attribute("aria-label", "Notifications", timeout=10000)
        check("Mark all as read clears the unread count", True)
    except Exception:
        check("Mark all as read clears the unread count", False, bell.get_attribute("aria-label") or "")
    ctx.close()


def step_tracking(browser):
    print("== tracking: later stages, notes, a manager recording for a member, private notes, withdrawing")
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "noah@example.com", expect_home="/member/dashboard")
    interviews_before = kpi(page, "Interviews")
    total_before = kpi(page, "Total Assigned")
    page.goto(BASE + "/member/jobs?applied=false")
    cards = page.locator('[data-slot="job-card"]').filter(visible=True)
    cards.first.wait_for(timeout=15000)
    title = cards.first.get_by_role("link").inner_text().strip()
    cards.first.get_by_role("link").click()
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
    page.get_by_role("combobox", name="Member").click()
    page.get_by_role("option", name="Noah Patel").click()
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
    check("a manager changing a member's status is told the history will show it", dialog.get_by_text(re.compile("You are recording this for Noah Patel")).is_visible())
    dialog.get_by_role("button", name="Change to Offer").click()
    toast(page, "Status changed to Offer")
    drawer.locator('[data-slot="status-badge"][data-status="OFFER"]').filter(visible=True).first.wait_for(timeout=10000)
    check("the manager can record an offer for the member", True)
    check("the history shows who recorded it", drawer.get_by_text("Recorded by Lena Alpha on the member's behalf.").first.is_visible())

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
    page.get_by_role("combobox", name="Member").click()
    page.get_by_role("option", name="Noah Patel").click()
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
        drawer.get_by_text(re.compile("This assignment was withdrawn from the member")).first.wait_for(timeout=10000)
        check("the drawer shows the assignment as withdrawn, with its history", drawer.get_by_text(re.compile("^Removed from the member's list by Lena Alpha")).first.is_visible())
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
          page.get_by_text("This job is no longer assigned to you.").is_visible() and page.get_by_role("link", name="Go to My Jobs").is_visible())
    no_overflow(page, "member job that was withdrawn")
    ctx.close()


def step_review(browser):
    print("== review-mode results, skipped members, bulk assign")
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com", expect_home="/tl/dashboard")
    titles = ["Account Executive"] if not MOBILE else ["Backend Engineer"]
    page.goto(BASE + "/tl/search")
    page.wait_for_load_state("networkidle")
    group = page.get_by_role("group", name="Members")
    group.wait_for(timeout=15000)
    group.get_by_role("checkbox", name=re.compile("Maya Chen")).check()
    group.get_by_role("checkbox", name=re.compile("Omar Haddad")).check()
    check("a member without a resume is flagged before starting", page.get_by_text(re.compile("Omar Haddad has no resume")).is_visible())
    field = page.get_by_label(re.compile("^Job titles"))
    field.fill(titles[0])
    field.press("Enter")
    page.get_by_label("Results per member").fill("5")
    page.get_by_role("radio", name=re.compile("Search first, review, then assign")).check()
    page.get_by_role("button", name="Start Search").click()
    window = page.get_by_role("dialog", name="AI Job Search Started")
    window.wait_for(timeout=15000)
    check("skipped members are shown with the API's reason", window.get_by_text(re.compile("was left out of this search")).is_visible()
          and window.get_by_text(re.compile("Omar Haddad:")).is_visible())
    window.locator('[data-slot="tag"][data-status="completed"]').first.wait_for(timeout=90000)
    check("review-mode search completes", True)
    check("review mode assigns nothing by itself", counter(window, "Assigned") == 0)
    window.get_by_role("link", name="View Results").click()
    page.wait_for_url(re.compile(r"/tl/searches/[0-9a-f-]{36}$"), timeout=15000)
    page.wait_for_load_state("networkidle")
    page.get_by_role("heading", name="Results", exact=True).wait_for(timeout=20000)
    check("review mode opens on the results not assigned yet", page.get_by_role("radiogroup", name="Show").get_by_role("radio", name="Not assigned yet").get_attribute("aria-checked") == "true")
    boxes = page.get_by_role("checkbox", name=re.compile("^Select .* for Maya Chen"))
    boxes.first.wait_for(timeout=15000)
    enabled = [boxes.nth(i) for i in range(boxes.count()) if boxes.nth(i).is_enabled()]
    check("review results list jobs to choose from", len(enabled) > 0, f"{len(enabled)} selectable of {boxes.count()} shown")
    pick = min(2, len(enabled))
    for box in enabled[:pick]:
        box.check()
    page.get_by_role("button", name=re.compile(r"Assign Selected \(%d\)" % pick)).click()
    confirm = page.get_by_role("alertdialog")
    confirm.wait_for(timeout=5000)
    check("bulk assign asks for confirmation with a per-member summary", confirm.get_by_text("Maya Chen").is_visible() and confirm.get_by_text(re.compile(rf"{pick} jobs?")).first.is_visible())
    shot(page, "bulk_assign_confirm")
    confirm.get_by_role("button", name=re.compile("^Assign")).click()
    toast(page, "Jobs assigned")
    check("bulk assign succeeds", True)
    page.get_by_role("radiogroup", name="Show").get_by_role("radio", name="All results").click()
    page.locator('[data-slot="status-badge"][data-status="ASSIGNED"]').filter(visible=True).first.wait_for(timeout=15000)
    check("assigned results now show their assignment status", True)
    no_overflow(page, "review results")

    # other team lead pages
    for path, heading in (("/tl/assignments", "Assigned Jobs"), ("/tl/assignments?applied=true", "Applications"), ("/tl/jobs", "Pending Jobs"),
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
    check("managers see posting links and other assignees", drawer.get_by_text("Posting links").is_visible() and drawer.get_by_text("Also assigned to").is_visible())
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
    ctx_beta.close()
    with expecting("403", f"/api/users/{priya['id']}"):
        page.goto(BASE + f"/tl/members/{priya['id']}")
        page.locator('[data-slot="error-state"][data-denied]').filter(visible=True).first.wait_for(timeout=15000)
        page.wait_for_load_state("networkidle")
    check("a 403 from the API is shown as 'Access denied'", page.get_by_text("Access denied").first.is_visible())
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
    for path, heading in (("/admin/analytics", "Analytics"), ("/admin/teams", "Teams"), ("/admin/team-leads", "Team Leads"), ("/admin/members", "Members"),
                          ("/admin/search", "Find Jobs"), ("/admin/searches", "Search History"), ("/admin/assignments", "Assignments"),
                          ("/admin/invitations", "Invitations"), ("/admin/activity", "Activity Log"), ("/admin/settings", "Settings")):
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        check(f"{path} loads", page.get_by_role("heading", level=1, name=heading).is_visible())
        no_overflow(page, path)
    sign_out(page)
    check("admin signed out", page.url.endswith("/login"))
    ctx.close()

    # the Team Lead of that team finds the job in the queue and hands it to a member
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
    page.get_by_role("button", name=re.compile(r"Distribute to Members \(1\)")).click()
    dialog = page.get_by_role("dialog", name="Assign to Members")
    members = dialog.get_by_role("group", name="Members")
    members.wait_for(timeout=15000)
    members.get_by_role("checkbox", name=re.compile("Priya Nair")).wait_for(timeout=15000)
    check("distribution offers only this team's members", "Priya Nair" in members.inner_text() and "Maya Chen" not in members.inner_text())
    members.get_by_role("checkbox", name=re.compile("Priya Nair")).check()
    dialog.get_by_role("button", name=re.compile("Review 1 Assignment")).click()
    page.get_by_role("button", name=re.compile("^Assign 1 Job")).click()
    toast(page, "Jobs assigned")
    check("the Team Lead distributes the queued job to a member", True)
    page.get_by_text("No jobs are waiting").or_(page.get_by_role("checkbox", name=re.compile("^Select "))).first.wait_for(timeout=15000)
    check("the job leaves the waiting queue", page.get_by_role("checkbox", name=f"Select {job_title} at E2E Corp").count() == 0)
    no_overflow(page, "tl queue")
    ctx.close()

    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "priya@example.com", expect_home="/member/dashboard")
    page.goto(BASE + "/member/jobs")
    page.get_by_role("link", name=job_title).first.wait_for(timeout=15000)
    check("the member received the job that started with the Admin", True)
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
    print("== the rest: forgot password, direct assignment by an Admin, notification link, CSV export, source switch, search prefilled for a member")
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

    # an Admin adds a job and assigns it straight to a member of any team
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
    if MOBILE:      # the cards on a small screen have no row menu: choose the job, then use the bar that appears
        page.get_by_role("checkbox", name=f"Select {job_title} at Direct Corp").check()
        page.get_by_role("region", name="Selected jobs").get_by_role("button", name="Assign to Members").click()
    else:
        page.get_by_role("button", name=f"Actions for {job_title} at Direct Corp").click()
        page.get_by_role("menuitem", name="Assign to Members").click()
    dialog = page.get_by_role("dialog", name="Assign to Members")
    members = dialog.get_by_role("group", name="Members")
    members.get_by_role("checkbox", name=re.compile("Quinn Lopez")).wait_for(timeout=15000)
    check("an Admin can assign to members of every team", "Quinn Lopez" in members.inner_text() and "Maya Chen" in members.inner_text())
    members.get_by_role("checkbox", name=re.compile("Quinn Lopez")).check()
    dialog.get_by_role("button", name=re.compile("Review 1 Assignment")).click()
    page.get_by_role("button", name=re.compile("^Assign 1 Job")).click()
    toast(page, "Jobs assigned")
    check("an Admin can assign a job directly to a member", True)

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
    page.get_by_text(re.compile("Avery Admin assigned you")).first.click()
    page.wait_for_url(re.compile(r"/member/jobs"), timeout=15000)
    page.get_by_role("link", name=job_title).first.wait_for(timeout=15000)
    check("selecting the notification opens the job list, where the new job is", True)
    ctx.close()

    # "Find Jobs for <member>" starts the search form with that member chosen
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com", expect_home="/tl/dashboard")
    page.goto(BASE + "/tl/members")
    page.get_by_role("searchbox", name="Search members").fill("Noah")
    page.get_by_role("link", name="Noah Patel").first.click()
    page.wait_for_url(re.compile(r"/tl/members/[0-9a-f-]{36}"), timeout=15000)
    page.get_by_role("link", name="Find Jobs for Noah").click()
    page.wait_for_url(re.compile(r"/tl/search\?member="), timeout=15000)
    group = page.get_by_role("group", name="Members")
    group.wait_for(timeout=15000)
    try:
        expect(group.get_by_role("checkbox", name=re.compile("Noah Patel"))).to_be_checked(timeout=10000)
        check("the search form opens with that member chosen", group.get_by_role("checkbox", name=re.compile("Maya Chen")).is_checked() is False)
    except Exception:
        check("the search form opens with that member chosen", False)
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
    group = page.get_by_role("group", name="Members")
    group.wait_for(timeout=15000)
    group.get_by_role("checkbox", name=re.compile("Noah Patel")).check()
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
    resumed_note = "The background worker restarted during this search. It resumed automatically; members already finished were kept."
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
    print("== people: reset link, resume on a member's behalf, profile, change password, theme")
    if "email" not in SHARED:
        skip("people step", "needs the member invited in the 'invite' step")
        return
    name, email = SHARED["name"], SHARED["email"]
    resume_path = f"{WORK_DIR}/resume-{STAMP}.txt"
    with open(resume_path, "w") as handle:
        handle.write(RESUME_TEXT)

    # Team Lead: upload a resume on the member's behalf, then create a reset link
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com", expect_home="/tl/dashboard")
    page.goto(BASE + "/tl/members")
    page.get_by_role("searchbox", name="Search members").fill(name)
    page.get_by_role("link", name=name).first.click()
    page.wait_for_url(re.compile(r"/tl/members/[0-9a-f-]{36}"), timeout=15000)
    page.get_by_role("tab", name="Profile and Resume").click()
    page.get_by_text(f"You are uploading on behalf of {name}").wait_for(timeout=15000)
    check("the Team Lead can upload a resume on the member's behalf", True)
    page.locator('input[type="file"]').first.set_input_files(resume_path)
    toast(page, "uploaded (version 1)")
    check("resume upload (multipart, through the /api proxy) succeeds", True)
    page.get_by_text("Skills found in the resume").wait_for(timeout=15000)
    check("extracted skills are shown for review", page.locator('[data-slot="tag"]').filter(has_text="Python").first.is_visible())
    check("the notice about outside AI services is shown (none configured on the demo stack)",
          page.get_by_text("No outside AI service is configured on this server. Resume text stays on this server.").first.is_visible())
    try:
        page.get_by_role("button", name="Remove Python").first.wait_for(timeout=10000)
        check("skills the API took from the resume appear in the open profile form", True)
    except Exception:
        check("skills the API took from the resume appear in the open profile form", False)
    no_overflow(page, "member detail, profile tab")
    shot(page, "member_detail_profile")

    # wrong file type is refused before upload
    bad = resume_path.replace(".txt", ".png")
    with open(bad, "wb") as handle:
        handle.write(b"not a resume")
    page.locator('input[type="file"]').first.set_input_files(bad)
    check("a file that is not PDF, DOCX or TXT is refused with a clear message", page.get_by_role("alert").filter(has_text="Choose a PDF, DOCX or TXT file.").first.is_visible())

    for tab in ("Overview", "Assignments", "Activity", "Searches"):
        page.get_by_role("tab", name=tab).click()
        page.wait_for_load_state("networkidle")
        check(f"member detail tab renders: {tab}", page.locator('[data-slot="error-state"]').filter(visible=True).count() == 0)

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

    # profile: completion meter, missing hints, save
    page.goto(BASE + "/member/profile")
    page.get_by_role("progressbar", name="Profile completion").wait_for(timeout=15000)
    before = int(page.get_by_role("progressbar", name="Profile completion").get_attribute("aria-valuenow"))
    check("the profile shows what is still missing", page.get_by_text("Add at least one desired job title").first.is_visible(), f"{before}% complete")
    titles = page.get_by_label(re.compile("^Desired job titles"))
    titles.fill("QA Engineer, Test Engineer")
    titles.press("Enter")
    page.get_by_label("Years of experience").fill("5")
    states = page.get_by_label(re.compile("^Preferred states"))
    states.fill("new york")
    states.press("Enter")
    page.get_by_role("button", name="Save Profile").click()
    toast(page, "Profile saved")
    expect(page.get_by_role("progressbar", name="Profile completion")).to_have_attribute("aria-valuenow", "100", timeout=15000)
    check("saving the profile raises the completion meter", True, f"{before}% -> 100%")
    check("a state name is stored as its code", page.get_by_role("button", name="Remove NY").is_visible())

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
    no_overflow(page, "member profile")
    shot(page, "member_profile")

    # change password, then sign in with it
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
    shot(page, "member_profile_dark")
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
    lpage.goto(BASE + "/tl/members")
    lpage.get_by_role("searchbox", name="Search members").fill(name)
    lpage.get_by_role("link", name=name).first.wait_for(timeout=15000)
    lpage.get_by_role("button", name=f"Actions for {name}").click()
    lmenu = lpage.get_by_role("menu")
    lmenu.wait_for(timeout=15000)
    check("a Team Lead is not offered Reactivate for an account an Admin deactivated, and is told why",
          lmenu.get_by_role("menuitem", name="Reactivate").count() == 0 and lmenu.get_by_text("Deactivated by an administrator", exact=True).is_visible())
    lpage.keyboard.press("Escape")
    lctx.close()

    apage.get_by_role("button", name=f"Actions for {name}").click()
    apage.get_by_role("menuitem", name="Reactivate").click()
    apage.get_by_role("alertdialog").get_by_role("button", name="Reactivate account").click()
    toast(apage, f"{name} can sign in again")
    check("the Admin can reactivate that account", True)
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
    check("deactivating a team asks for confirmation", confirm.get_by_text(re.compile("Members, assignments and history stay")).is_visible())
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
    for tab in ("Members", "Assignments", "Queue", "Searches", "Overview"):
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
    page.get_by_role("group", name="Members").wait_for(timeout=15000)
    page.get_by_label("Results per member").fill("7")
    page.get_by_role("button", name=re.compile("^Presets")).click()
    preset = f"E2E preset {STAMP}"
    page.get_by_label("Save the current settings as").fill(preset)
    page.get_by_role("button", name="Save", exact=True).click()
    toast(page, f'Preset "{preset}" saved')
    page.keyboard.press("Escape")
    page.get_by_label("Results per member").fill("3")
    page.get_by_role("button", name=re.compile("^Presets")).click()
    page.get_by_role("listitem").filter(has_text=preset).get_by_role("button", name="Apply").click()
    check("a saved preset restores the search settings", page.get_by_label("Results per member").input_value() == "7")
    page.get_by_role("button", name=re.compile("^Presets")).click()
    page.get_by_role("button", name=f"Delete preset {preset}").click()
    toast(page, "Preset deleted")
    check("a preset can be deleted", True)
    check("an Admin can pick a whole team in the search form", page.get_by_role("combobox", name="Whole team").is_visible())
    ctx.close()


STEPS = {"auth": step_auth, "member": step_member, "invite": step_invite, "search": step_search, "people": step_people,
         "tracking": step_tracking, "review": step_review, "admin": step_admin, "admin_more": step_admin_more, "misc": step_misc, "stubbed": step_stubbed, "running": step_running}

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
