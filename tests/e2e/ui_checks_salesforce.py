"""Third browser check list: finding candidates in Salesforce and bringing them into the portal.

Team Leads and Admins: the Find Jobs candidate list with its two groups ("Candidates in the portal" and "From
Salesforce"), the "Active candidates only" switch, bringing a person in from the row (with and without a resume),
the team an Admin brings a candidate into, "Add from Salesforce" as the only way to add a candidate on the Candidates pages and
on an Admin's team page (the dialog for typing one in, "Add Candidate", is offered only where Salesforce is not configured), the source marker and the source filter, the Salesforce block on a candidate's page ("Open in
Salesforce", "Refresh from Salesforce"), where a resume came from, the Team Lead scope in the Admin settings (`all`
or `own`) and the integrations page. Members have none of it: their pages never ask about Salesforce, and the API
refuses them.

    python tests/e2e/ui_checks_salesforce.py --base http://127.0.0.1:3011 --w 1280 --h 900
    python tests/e2e/ui_checks_salesforce.py --base http://127.0.0.1:3011 --w 390 --h 844

It needs the demo stack with freshly seeded data (see tests/e2e/run.sh, which runs it): the run brings candidates in.
Salesforce there is an in-process fake (tests/e2e/salesforce_fake.py); no request ever leaves this machine, and the
browser cannot resolve any other host. What the fake cannot be told to do from here (fail, answer too fast, be
switched off) is checked by answering the portal's own /api request in the browser instead; those checks say so.

Every step is a named check. Console errors/warnings, page errors and failed /api requests are
collected for the whole run; a step that provokes a failure on purpose declares it (expecting).
"""
import argparse
import re
import sys
import time
import traceback
from contextlib import contextmanager

from playwright.sync_api import expect, sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--w", type=int, default=1280)
ap.add_argument("--h", type=int, default=900)
ap.add_argument("--base", default="http://127.0.0.1:3011")
ap.add_argument("--shots", default="", help="folder for screenshots; none are taken without it")
ap.add_argument("--only", default="")
A = ap.parse_args()

BASE = A.base
PASSWORD = "Demo-Password-42!"
MOBILE = A.w < 768
T = 30000
PICKER = "Candidates in the portal"          # the Find Jobs list of the portal's own candidates, beside "From Salesforce"
# The fake's ids (tests/e2e/salesforce_fake.py): 001E2E000000<number>AAA.
SF_BRUNO, SF_DMITRI = "001E2E000000002AAA", "001E2E000000004AAA"
SEARCH_URL = "**/api/salesforce/candidates**"
STATUS_URL = "**/api/salesforce/status"
IMPORT_URL = "**/api/candidates/import-salesforce"
STATUS_OFF = {"configured": False, "connected": None, "message": "", "checked_at": None, "api_version": "v68.0", "team_lead_scope": "all"}
ON_ROW = "(name) => document.activeElement?.closest('[data-slot=\"salesforce-row\"]')?.textContent?.includes(name) ?? false"

results = []
problems = []
expected = []


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
    problems.append(f"{kind}: {text[:400]}")
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


def login(page, email):
    page.goto(BASE + "/login")
    page.get_by_label("E-mail").fill(email)
    page.get_by_label("Password", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(lambda u: "/login" not in u, timeout=T)
    page.wait_for_load_state("networkidle")


def signed_in(browser, email):
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, email)
    return ctx, page


def api(page, path):
    return page.evaluate("async (p) => { const r = await fetch(p, {credentials: 'same-origin'}); return r.ok ? await r.json() : {__status: r.status} }", path)


def send(page, method, path, body=None):
    """A state-changing request as the signed-in person, with the CSRF header the app itself sends. Returns the status."""
    return page.evaluate(
        """async ([method, path, body]) => {
            const csrf = decodeURIComponent((document.cookie.split('; ').find((c) => c.startsWith('jf_csrf=')) || '').slice(8));
            const r = await fetch(path, { method, credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: body === null ? undefined : JSON.stringify(body) });
            return r.status;
        }""", [method, path, body])


def no_overflow(page, name):
    overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check(f"no horizontal page overflow: {name}", overflow <= 0, f"{overflow}px")


def shot(page, name):
    if A.shots:
        page.screenshot(path=f"{A.shots}/{A.w}_sf_{name}.png", full_page=True)


def toast(page, text, timeout=T):
    item = page.locator("[data-sonner-toast]").filter(has_text=text).first
    item.wait_for(timeout=timeout)
    return item


def vis(locator):
    return locator.filter(visible=True)


def shown(locator, timeout=10000):
    """Whether it becomes visible in time (a check that waits instead of looking once)."""
    try:
        locator.first.wait_for(timeout=timeout)
        return True
    except Exception:
        return False


def alter_json(page, pattern, change):
    """Let the request through and change the JSON answer on the way back."""
    def handler(route):
        response = route.fetch()
        if response.status != 200:
            route.fulfill(response=response)
            return
        body = response.json()
        change(body)
        route.fulfill(response=response, json=body)
    page.route(pattern, handler)


def candidate_named(page, name):
    data = api(page, f"/api/candidates?q={name.replace(' ', '%20')}&page_size=50")
    return next((c for c in data.get("items", []) if c["name"] == name), None)


def names_listed(page, known, want, timeout=10000):
    """The candidates of `known` that the list shows; waits until it is `want` (the list reloads after a filter change)."""
    deadline = time.time() + timeout / 1000
    while True:
        listed = {n for n in known if vis(page.get_by_role("link", name=n, exact=True)).count() > 0}
        if listed == set(want) or time.time() > deadline:
            return listed
        page.wait_for_timeout(200)


def sf(scope):
    """The "From Salesforce" part of a candidate list or of the Add from Salesforce dialog."""
    return scope.locator('[data-slot="salesforce-import"]')


def row(scope, name):
    return scope.locator('[data-slot="salesforce-row"]').filter(has_text=name)


def type_query(scope, text, label="Search candidates"):
    box = scope.get_by_role("searchbox", name=label)
    box.fill(text)
    return box


def open_search_form(page, area="/tl"):
    page.goto(BASE + f"{area}/search")
    page.wait_for_load_state("networkidle")
    group = sf(page)
    group.wait_for(timeout=T)
    return group


def salesforce_panel(page):
    return page.locator("section").filter(has=page.get_by_role("heading", name="Salesforce", exact=True))


# ------------------------------------------------------------------------------------------ 1. Team Lead, Find Jobs
def step_picker(browser):
    print("== 1. Team Lead, Find Jobs: two groups, the switch, bringing a person in from the row, and what a row says")
    ctx, page = signed_in(browser, "lead.alpha@example.com")
    searches, imports = [], []
    page.on("request", lambda r: searches.append(r.url) if "/api/salesforce/candidates" in r.url else None)
    page.on("response", lambda r: imports.append(r) if r.url.endswith("/api/candidates/import-salesforce") else None)
    group = open_search_form(page)
    portal = page.get_by_role("group", name=PICKER, exact=True)
    check("the list has two groups with their titles, 'In the portal' and 'From Salesforce'",
          page.get_by_role("heading", name="In the portal", exact=True).is_visible() and group.get_by_role("heading", name="From Salesforce", exact=True).is_visible())
    portal.get_by_role("checkbox").first.wait_for(timeout=T)
    check("the portal's own candidates are the group 'Candidates in the portal'; the Salesforce results are the group 'Candidates in Salesforce'",
          portal.count() == 1 and page.get_by_role("group", name="Candidates", exact=True).count() == 0
          and group.get_by_role("group", name="Candidates in Salesforce", exact=True).count() == 1)
    check("the portal group lists the team's candidates, the one seeded from Salesforce among them",
          portal.get_by_role("checkbox").count() == 6 and portal.get_by_role("checkbox", name=re.compile("Amara Nwosu")).count() == 1, str(portal.get_by_role("checkbox").count()))
    check("before two characters are typed the Salesforce group says what to type",
          group.get_by_text("Type at least 2 characters of a name, e-mail address or phone number in the search box to look in Salesforce.").is_visible())
    check("a Team Lead is not asked for a team", group.get_by_text("Team the candidate is brought into").count() == 0 and group.get_by_role("combobox").count() == 0)
    toggle = group.get_by_role("switch", name="Active candidates only")
    check("'Active candidates only' is on by default", toggle.get_attribute("aria-checked") == "true")
    check("candidates come from Salesforce only: the list offers nothing to type a candidate in with", page.get_by_role("button", name=re.compile("^Add (Candidate|Manually)")).count() == 0)
    no_overflow(page, "Team Lead Find Jobs, nothing typed")

    type_query(page, "c")
    page.wait_for_timeout(900)
    check("one character does not ask Salesforce", searches == [] and group.get_by_text("Type at least 2 characters").is_visible(), str(searches))

    # active only, then everyone
    box = type_query(page, "ch")
    row(group, "Chloe Bennett").wait_for(timeout=T)
    check("the search is sent with the text, status=active and the page size", len(searches) == 1 and all(p in searches[0] for p in ("q=ch", "status=active", "limit=20")), str(searches))
    check("search 'ch': Chloe Bennett (Active) is found, Chloe Benson (Placed) is not", row(group, "Chloe Benson").count() == 0 and group.locator('[data-slot="salesforce-row"]').count() == 1)
    text = row(group, "Chloe Bennett").inner_text()
    for part in ("Product Manager", "Work authorization: USC", "Job type: All of the above", "Locations: Texas, NJ, St. Louis (Open to relocate)", "Active",
                 "Marketing team lead: Lena Alpha", "Recruiter: Rita Recruiter", "E-mail: chloe.bennett@example.com", "Resume in Salesforce", "Bring in"):
        check(f"the row shows {part!r}", part in text, text.replace("\n", " | "))
    check("the row of a person who is not in the portal carries no 'In the portal' mark", "In the portal" not in text)
    check("the number of results is announced (role=status)", group.locator('p[role="status"]').filter(has_text="1 candidate found in Salesforce").count() == 1,
          " | ".join(group.locator('p[role="status"]').all_inner_texts()))
    check("the same text narrows the portal group too", shown(portal.get_by_text("Nobody in the portal matches this search"))
          and portal.get_by_text("Look under From Salesforce below to bring the candidate in.").is_visible())
    shot(page, "tl_search_ch")
    toggle.click()
    row(group, "Chloe Benson").wait_for(timeout=T)
    check("switch off: Chloe Benson appears with her status 'Placed', asked with status=all",
          "Placed" in row(group, "Chloe Benson").inner_text() and "status=all" in searches[-1] and group.locator('[data-slot="salesforce-row"]').count() == 2, searches[-1])
    no_overflow(page, "Team Lead Find Jobs, Salesforce results")
    toggle.click()
    expect(row(group, "Chloe Benson")).to_have_count(0, timeout=T)
    check("switch on again: only active candidates", toggle.get_attribute("aria-checked") == "true" and row(group, "Chloe Bennett").count() == 1)

    # keyboard: down from the box, through the results, Enter brings the person in
    box.focus()
    page.keyboard.press("ArrowDown")
    for _ in range(40):
        if page.evaluate(ON_ROW, "Chloe Bennett"):
            break
        page.keyboard.press("ArrowDown")
    check("keyboard: the arrow keys lead from the search box to the Salesforce row", page.evaluate(ON_ROW, "Chloe Bennett"))

    def slow(route):                      # hold the answer back so the state in between can be seen
        time.sleep(1.2)
        route.continue_()
    page.route(IMPORT_URL, slow)
    page.keyboard.press("Enter")
    check("while the person is brought in the row says 'Bringing in details and resume' and is marked busy",
          shown(row(group, "Chloe Bennett").get_by_text("Bringing in details and resume"), 5000)
          and row(group, "Chloe Bennett").get_by_role("button").get_attribute("aria-busy") == "true")
    selected = page.get_by_role("region", name="Selected candidates")
    item = selected.get_by_role("listitem").filter(has_text="Chloe Bennett")
    item.wait_for(timeout=T)
    page.unroute(IMPORT_URL)
    sent = imports[-1].request.post_data_json if imports else {}
    answer = imports[-1].json() if imports else {}
    check("one request brought her in: the Salesforce id, no team from the browser for a Team Lead, nobody responsible; the API created her with the resume",
          len(imports) == 1 and imports[0].status == 201 and sent == {"salesforce_id": "001E2E000000003AAA", "team_id": None, "assigned_member_id": None}
          and answer["sync"]["created"] is True and answer["sync"]["resume"] == "imported", f"{len(imports)} requests, sent {sent}")
    done = toast(page, "Chloe Bennett brought in from Salesforce")
    check("the confirmation says details, profile and resume are in the portal", "Details, profile and resume are in the portal." in done.inner_text(), done.inner_text())
    check("the person is chosen for the search at once, with the resume", vis(item.locator('[data-slot="tag"]').filter(has_text="Resume uploaded")).count() == 1, item.inner_text())
    check("a chosen candidate from Salesforce has 'Replace Resume' and the responsible-member select like any other",
          item.get_by_role("button", name="Replace Resume for Chloe Bennett").is_visible()
          and item.get_by_role("combobox", name="Responsible member, who handles the jobs found").inner_text().strip() == "Nobody yet (unassigned)")
    check("the row now reads 'In the portal' and 'Selected'", shown(row(group, "Chloe Bennett").get_by_text("Selected", exact=True))
          and "In the portal" in row(group, "Chloe Bennett").inner_text() and "Bring in" not in row(group, "Chloe Bennett").inner_text())
    check("the focus stayed on the row", page.evaluate(ON_ROW, "Chloe Bennett"))
    check("Enter on a row did not submit the search form", "/tl/search" in page.url and page.get_by_role("dialog").count() == 0)
    check("she is listed in the portal group now, ticked", shown(portal.get_by_role("checkbox", name=re.compile("Chloe Bennett")))
          and portal.get_by_role("checkbox", name=re.compile("Chloe Bennett")).is_checked())
    record = candidate_named(page, "Chloe Bennett")
    profile = api(page, f"/api/candidates/{record['id']}/profile")
    check("the candidate is in the Team Lead's team, marked as coming from Salesforce, with contact details",
          record["source"] == "salesforce" and record["team"]["name"] == "Team Alpha" and record["assigned_member"] is None and record["has_resume"] is True
          and record["email"] == "chloe.bennett@example.com" and record["salesforce_url"] == "https://salesforce.example.com/001E2E000000003AAA", str(record.get("source")))
    check("the job-search profile holds what Salesforce had (titles, work arrangement, authorization, states)",
          profile.get("desired_titles") == ["Product Manager"] and profile.get("work_mode") == "All" and profile.get("work_authorization") == "US Citizen"
          and sorted(profile.get("preferred_states") or []) == ["NJ", "TX"],
          str({k: profile.get(k) for k in ("desired_titles", "work_mode", "work_authorization", "preferred_states")}))
    check("the original text of the locations is kept in the notes", "Preferred job locations (from Salesforce): Texas, NJ, St. Louis (Open to relocate)" in (record.get("notes") or ""), record.get("notes") or "")

    # a file Salesforce holds that is not a resume: brought in without one, and the form asks for it
    type_query(page, "gideon")
    row(group, "Gideon Park").wait_for(timeout=T)
    row(group, "Gideon Park").get_by_role("button").click()
    item = selected.get_by_role("listitem").filter(has_text="Gideon Park")
    item.wait_for(timeout=T)
    check("a person whose Salesforce file is not a resume is brought in without one: 'No resume' and 'Upload Resume'",
          vis(item.locator('[data-slot="tag"]').filter(has_text="No resume")).count() == 1 and item.get_by_role("button", name="Upload Resume for Gideon Park").is_visible(), item.inner_text())
    check("the API said why (refused_type), and the import was not blocked", imports[-1].status == 201 and imports[-1].json()["sync"]["resume"] == "refused_type")
    check("the form's warning about a missing resume names the candidate", page.get_by_text(re.compile(r"Gideon Park has no\s+resume")).count() >= 1)
    done = toast(page, "Gideon Park brought in from Salesforce")
    check("the confirmation says why there is no resume and where to upload one",
          "The resume in Salesforce is not a PDF or DOCX file, so it was not brought in. Upload one under Selected candidates." in done.inner_text(), done.inner_text())
    shot(page, "tl_selected")
    no_overflow(page, "Team Lead Find Jobs, two candidates brought in")

    # already in the portal: chosen, not brought in again
    before = len(imports)
    type_query(page, "amara")
    row(group, "Amara Nwosu").wait_for(timeout=T)
    text = row(group, "Amara Nwosu").inner_text()
    check("a person who is already in the own team is marked 'In the portal' and offers 'Select'", "In the portal" in text and "Select" in text and "Bring in" not in text, text.replace("\n", " | "))
    row(group, "Amara Nwosu").get_by_role("button").click()
    selected.get_by_role("listitem").filter(has_text="Amara Nwosu").wait_for(timeout=T)
    check("choosing her selects the portal candidate without bringing her in again", len(imports) == before, f"{len(imports) - before} import requests")
    check("the portal group lists her too, ticked", portal.get_by_role("checkbox", name=re.compile("Amara Nwosu")).is_checked()
          and shown(row(group, "Amara Nwosu").get_by_text("Selected", exact=True)))

    # in the portal with another team: nothing more is said, nothing can be done
    type_query(page, "elif")
    row(group, "Elif Demir").wait_for(timeout=T)
    other = row(group, "Elif Demir")
    check("another team's candidate: 'Not available', no button, and neither the team nor a link is given away",
          "Not available: this candidate is already in the portal with another team." in other.inner_text() and other.get_by_role("button").count() == 0
          and other.get_by_role("link").count() == 0 and "Team Beta" not in other.inner_text() and "In the portal" not in other.inner_text().replace("already in the portal", ""),
          other.inner_text().replace("\n", " | "))
    check("the portal group does not list her", shown(portal.get_by_text("Nobody in the portal matches this search")))

    # nobody
    type_query(page, "zzzz")
    check("no match: the Salesforce group says so, with the text that was typed", shown(group.get_by_text('Nobody in Salesforce matches "zzzz"')))
    check("no match: the hint names the 'Active candidates only' switch",
          group.get_by_text('Only candidates whose status in Salesforce is Active are searched: switch off "Active candidates only" to include the others.').is_visible())
    check("scope 'all': the hint does not speak of the Team Lead's own candidates", group.get_by_text("whose marketing team lead in Salesforce is you").count() == 0)
    check("no match: the portal group says nobody in the portal matches", portal.get_by_text("Nobody in the portal matches this search").is_visible())
    check("no match is announced (role=status)", group.locator('p[role="status"]').filter(has_text="No candidate found in Salesforce").count() == 1)

    # Salesforce cannot be reached, too many searches, and the way back (the portal's answer is replaced in the browser)
    state = {"mode": 503}

    def broken(route):
        if state["mode"] == 503:
            route.fulfill(status=503, content_type="application/json", body='{"error":"Salesforce cannot be reached right now.","detail":""}')
        elif state["mode"] == 429:
            route.fulfill(status=429, content_type="application/json", headers={"Retry-After": "20"},
                          body='{"error":"Too many Salesforce searches. Wait a moment and try again.","detail":""}')
        else:
            route.continue_()
    with expecting("/api/salesforce/candidates", "status of 503", "status of 429"):
        page.route(SEARCH_URL, broken)
        type_query(page, "bruno")
        alert = group.get_by_role("alert").filter(has_text="Salesforce cannot be reached right now")
        check("503 from the API (stubbed): 'Salesforce cannot be reached right now', with the API's sentence and 'Try again'",
              shown(alert) and "Candidates already in the portal are not affected." in alert.inner_text() and group.get_by_role("button", name="Try again").is_visible())
        check("503: the portal group still works", portal.is_visible() and portal.get_by_role("alert").count() == 0)
        check("503: no stale rows and no 'limit' hint are shown", group.locator('[data-slot="salesforce-row"]').count() == 0 and group.get_by_text("Showing the first").count() == 0)
        shot(page, "tl_unreachable")
        state["mode"] = 429
        group.get_by_role("button", name="Try again").click()
        alert = group.get_by_role("alert").filter(has_text="Too many searches in a short time")
        check("429 from the API (stubbed): 'Too many searches in a short time', with the API's sentence and 'Search again'",
              shown(alert) and "Too many Salesforce searches. Wait a moment and try again." in alert.inner_text() and group.get_by_role("button", name="Search again").is_visible())
        state["mode"] = 200
        group.get_by_role("button", name="Search again").click()
        check("'Search again' recovers: the result is shown", shown(row(group, "Bruno Castell")) and group.get_by_role("alert").count() == 0)
        page.unroute(SEARCH_URL)

    # loading
    def slow_search(route):
        time.sleep(1.0)
        route.continue_()
    page.route(SEARCH_URL, slow_search)
    type_query(page, "dmitri")
    check("while Salesforce is asked the group shows a loading state and is marked busy",
          shown(group.locator('[data-slot="loading"]'), 5000) and group.get_by_role("group", name="Candidates in Salesforce").get_attribute("aria-busy") == "true")
    row(group, "Dmitri Volkov").wait_for(timeout=T)
    page.unroute(SEARCH_URL)
    check("a person without a resume in Salesforce is marked 'No resume in Salesforce'", "No resume in Salesforce" in row(group, "Dmitri Volkov").inner_text()
          and "Resume in Salesforce" not in row(group, "Dmitri Volkov").inner_text().replace("No resume in Salesforce", ""))

    # the API refuses the import (stubbed 409): the sentence stays in the row
    with expecting("/api/candidates/import-salesforce", "status of 409"):
        page.route(IMPORT_URL, lambda route: route.fulfill(status=409, content_type="application/json", body='{"error":"This candidate is already in the portal.","detail":""}'))
        row(group, "Dmitri Volkov").get_by_role("button").click()
        expect(row(group, "Dmitri Volkov").get_by_role("alert")).to_have_text("This candidate is already in the portal.", timeout=T)
        check("409 from the API (stubbed): its sentence is shown in the row, and nobody is added to the selection",
              selected.get_by_role("listitem").filter(has_text="Dmitri Volkov").count() == 0 and "Bring in" in row(group, "Dmitri Volkov").inner_text())
        page.unroute(IMPORT_URL)
    check("none of this started a search", page.get_by_role("dialog", name="AI Job Search Started").count() == 0 and "/tl/search" in page.url)
    ctx.close()


# ------------------------------------------------------------------------------------------ 2. states of the connection
def step_states(browser):
    print("== 2. the connection: cannot be checked, not reachable, not configured; more matches than are shown")
    # the status request fails (stubbed 500)
    ctx, page = signed_in(browser, "lead.alpha@example.com")
    failing = {"on": True}
    page.route(STATUS_URL, lambda route: route.fulfill(status=500, content_type="application/json", body='{"error":"Something went wrong on the server.","detail":""}')
               if failing["on"] else route.continue_())
    with expecting("/api/salesforce/status", "status of 500"):
        page.goto(BASE + "/tl/search")
        group = sf(page)
        alert = group.get_by_role("alert").filter(has_text="The connection to Salesforce could not be checked")
        check("the status request fails (stubbed 500): 'The connection to Salesforce could not be checked', with the API's sentence and 'Try again'",
              shown(alert, T) and "Something went wrong on the server." in alert.inner_text() and group.get_by_role("button", name="Try again").is_visible(),
              group.inner_text().replace("\n", " | ")[:200] if group.count() else "no Salesforce group")
        portal = page.get_by_role("group", name=PICKER, exact=True)
        check("status failed: the list offers nothing to type a candidate in with (the portal may well be connected)", page.get_by_role("button", name=re.compile("^Add (Candidate|Manually)")).count() == 0)
        check("status failed: the portal's own candidates are still listed and can be chosen",
              shown(portal.get_by_role("checkbox", name=re.compile("Jordan Rivera"))) and portal.get_by_role("checkbox").count() >= 6)
        portal.get_by_role("checkbox", name=re.compile("Jordan Rivera")).check()
        check("status failed: a portal candidate is selected as always", shown(page.get_by_role("region", name="Selected candidates").get_by_text("Jordan Rivera", exact=True)))
        type_query(page, "bruno")
        page.wait_for_timeout(900)
        check("status failed: typing does not replace the notice by a search", alert.is_visible() and group.locator('[data-slot="salesforce-row"]').count() == 0)
        no_overflow(page, "Find Jobs, connection could not be checked")
        shot(page, "tl_status_failed")
        failing["on"] = False
        group.get_by_role("button", name="Try again").click()
        check("'Try again' asks once more: with the connection back the search runs", shown(row(group, "Bruno Castell"), T) and group.get_by_role("alert").count() == 0)
        failing["on"] = True
        page.goto(BASE + "/tl/candidates")
        vis(page.get_by_role("link", name="Jordan Rivera", exact=True)).first.wait_for(timeout=T)
        check("status failed: the Candidates page lists the candidates and offers a way to add one",
              shown(vis(page.get_by_role("button", name=re.compile("^Add (Candidate|from Salesforce)$"))), T))
    ctx.close()

    # configured, but the last check did not get through (the status answer is altered on its way back)
    ctx, page = signed_in(browser, "lead.alpha@example.com")
    words = "Salesforce did not answer the last check (altered answer)."
    alter_json(page, STATUS_URL, lambda body: body.update(connected=False, message=words))
    group = open_search_form(page)
    alert = group.get_by_role("alert").filter(has_text="Salesforce cannot be reached right now")
    check("connected: false (altered answer): the notice says Salesforce cannot be reached, in the API's words, with 'Check again'",
          shown(alert) and words in alert.inner_text() and group.get_by_role("button", name="Check again").is_visible(), group.inner_text().replace("\n", " | ")[:200])
    check("connected: false: the two groups stay, and the portal's candidates are listed",
          page.get_by_role("heading", name="In the portal", exact=True).is_visible() and shown(page.get_by_role("group", name=PICKER, exact=True).get_by_role("checkbox").first))
    check("connected: false: the 'type two characters' hint is replaced by the notice", group.get_by_text("Type at least 2 characters").count() == 0)
    no_overflow(page, "Find Jobs, Salesforce not reachable")
    page.goto(BASE + "/tl/candidates")
    check("connected: false: the Candidates page still offers 'Add from Salesforce' (configured is what counts)", shown(vis(page.get_by_role("button", name="Add from Salesforce")), T))
    page.wait_for_load_state("networkidle")          # no answer is on its way through the handler when it is taken away
    page.unroute(STATUS_URL)
    group = open_search_form(page)
    check("with the real answer again the hint is back and no notice is shown", shown(group.get_by_text("Type at least 2 characters")) and group.get_by_role("alert").count() == 0)
    ctx.close()

    # not configured (stubbed status): everything is as it was before Salesforce
    ctx, page = signed_in(browser, "lead.alpha@example.com")
    asked = []
    page.on("request", lambda r: asked.append(r.url) if "/api/salesforce/candidates" in r.url else None)
    page.route(STATUS_URL, lambda route: route.fulfill(status=200, json=STATUS_OFF))
    page.goto(BASE + "/tl/search")
    page.wait_for_load_state("networkidle")
    plain = page.get_by_role("group", name="Candidates", exact=True)
    plain.get_by_role("checkbox").first.wait_for(timeout=T)
    check("not configured (stubbed): one group 'Candidates', no Salesforce group and no 'In the portal' title",
          sf(page).count() == 0 and page.get_by_role("heading", name="In the portal").count() == 0 and page.get_by_role("group", name=PICKER).count() == 0)
    check("not configured: the button in the list is 'Add Candidate' as before", vis(page.get_by_role("button", name="Add Candidate", exact=True)).count() >= 1
          and page.get_by_role("button", name="Add Manually").count() == 0)
    vis(page.get_by_role("button", name="Add Candidate", exact=True)).first.click()
    check("not configured: it opens the dialog for typing a candidate in, from inside the search form", shown(page.get_by_role("dialog", name="Add Candidate").get_by_label("Full name")))
    page.get_by_role("dialog", name="Add Candidate").get_by_role("button", name="Cancel").click()
    page.get_by_role("dialog", name="Add Candidate").wait_for(state="hidden", timeout=T)
    type_query(page, "zzzz")
    check("not configured: the no-match text is the old one, with its advice", shown(plain.get_by_text("No candidates match this search"))
          and plain.get_by_text("Check the spelling or clear the search.").is_visible())
    check("not configured: Salesforce is never searched", asked == [], str(asked))
    page.goto(BASE + "/tl/candidates")
    vis(page.get_by_role("link", name="Jordan Rivera", exact=True)).first.wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    check("not configured: the Candidates page offers only 'Add Candidate'", vis(page.get_by_role("button", name="Add Candidate", exact=True)).count() >= 1
          and page.get_by_role("button", name="Add from Salesforce").count() == 0 and page.get_by_role("button", name="Add Manually").count() == 0)
    check("not configured: there is no Source filter", page.get_by_role("combobox", name="Source").count() == 0)
    check("not configured: a candidate that came from Salesforce earlier keeps its marker", vis(page.locator('[data-slot="source-tag"]')).count() >= 1)
    check("not configured: the page describes adding a candidate by hand", page.get_by_text("Add a candidate, upload the resume, choose the member who handles their jobs").count() >= 1)
    vis(page.get_by_role("button", name="Add Candidate", exact=True)).first.click()
    check("not configured: 'Add Candidate' opens the dialog for typing a candidate in", shown(page.get_by_role("dialog", name="Add Candidate").get_by_label("Full name")))
    page.get_by_role("dialog", name="Add Candidate").get_by_role("button", name="Cancel").click()
    ctx.close()

    # the same for an Admin: Candidates page and a team's Candidates tab
    ctx, page = signed_in(browser, "admin@example.com")
    team = next(t for t in api(page, "/api/teams") if t["name"] == "Team Alpha")
    page.route(STATUS_URL, lambda route: route.fulfill(status=200, json=STATUS_OFF))
    for where, path in (("Admin Candidates", "/admin/candidates"), ("Admin team page, Candidates tab", f"/admin/teams/{team['id']}?tab=candidates")):
        page.goto(BASE + path)
        vis(page.get_by_role("link", name="Jordan Rivera", exact=True)).first.wait_for(timeout=T)
        page.wait_for_load_state("networkidle")
        check(f"not configured, {where}: only 'Add Candidate', and it opens the dialog for typing a candidate in",
              vis(page.get_by_role("button", name="Add Candidate", exact=True)).count() == 1 and page.get_by_role("button", name="Add from Salesforce").count() == 0)
        vis(page.get_by_role("button", name="Add Candidate", exact=True)).first.click()
        manual = page.get_by_role("dialog", name="Add Candidate")
        team_box = manual.get_by_role("combobox", name="Team")
        check(f"not configured, {where}: the dialog has the name field and the team" + (", to be chosen" if "tab" not in path else ", with this team chosen"),
              shown(manual.get_by_label("Full name")) and team_box.count() == 1
              # the team's name appears in the select once the list of teams has arrived
              and (shown(team_box.filter(has_text="Team Alpha"), T) if "tab" in path else "Team Alpha" not in team_box.inner_text()), team_box.inner_text())
        manual.get_by_role("button", name="Cancel").click()
        manual.wait_for(state="hidden", timeout=T)
    ctx.close()

    # more matches than are shown at once: the fake holds 21 closed "Zimmer" records, the web app asks for 20
    ctx, page = signed_in(browser, "lead.alpha@example.com")
    answers = []
    page.on("response", lambda r: answers.append(r) if "/api/salesforce/candidates" in r.url else None)
    group = open_search_form(page)
    type_query(page, "zimmer")
    check("active only: none of the closed records is found", shown(group.get_by_text('Nobody in Salesforce matches "zimmer"')))
    group.get_by_role("switch", name="Active candidates only").click()
    expect(group.locator('[data-slot="salesforce-row"]')).to_have_count(20, timeout=T)
    body = answers[-1].json()
    check("more matches than the page: the API says limit_reached, and the list shows the 20 it was given, by name",
          body["limit_reached"] is True and len(body["items"]) == 20 and row(group, "Ada Zimmer").count() == 1 and row(group, "Uwe Zimmer").count() == 0
          and [r.split("\n")[0].strip() for r in group.locator('[data-slot="salesforce-row"]').all_inner_texts()] == [p["name"] for p in body["items"]])
    check("limit reached: the hint under the list asks for more of the name",
          group.get_by_text("Showing the first 20 matches, by name. There may be more: type more of the name to narrow the list.").is_visible())
    check("limit reached: the status of each record is shown", "Closed" in row(group, "Ada Zimmer").inner_text())
    no_overflow(page, "Find Jobs, a full page of Salesforce results")
    shot(page, "tl_limit")
    type_query(page, "uwe zimmer")
    row(group, "Uwe Zimmer").wait_for(timeout=T)
    expect(group.locator('[data-slot="salesforce-row"]')).to_have_count(1, timeout=T)
    check("a narrower search: one match, limit_reached false, and the hint is gone",
          answers[-1].json()["limit_reached"] is False and group.get_by_text("Showing the first").count() == 0)
    ctx.close()


# ------------------------------------------------------------------------------------------ 3. Team Lead, Candidates
def step_candidates(browser):
    print("== 3. Team Lead, Candidates: Add from Salesforce and no other way, source marker and filter, the candidate's page, refresh")
    ctx, page = signed_in(browser, "lead.alpha@example.com")
    imports = []
    page.on("response", lambda r: imports.append(r) if r.url.endswith("/api/candidates/import-salesforce") else None)
    maya = next(u for u in api(page, "/api/users?role=MEMBER&page_size=50")["items"] if u["email"] == "maya@example.com")
    page.goto(BASE + "/tl/candidates")
    page.wait_for_load_state("networkidle")
    add = vis(page.get_by_role("button", name="Add from Salesforce", exact=True))
    add.first.wait_for(timeout=T)
    check("the Candidates page offers 'Add from Salesforce' (once, as the primary button) and nothing to type a candidate in with",
          add.count() == 1 and add.first.get_attribute("data-variant") in (None, "default") and page.get_by_role("button", name=re.compile("^Add (Candidate|Manually)")).count() == 0, f"{add.count()} buttons, variant {add.first.get_attribute('data-variant')}")
    check("the page says candidates are brought in from Salesforce", page.get_by_text("the candidates brought in from Salesforce and worked on here").count() >= 1)
    everyone = api(page, "/api/candidates?page_size=200")["items"]
    known = [c["name"] for c in everyone]
    from_sf = {c["name"] for c in everyone if c["source"] == "salesforce"}
    by_hand = {c["name"] for c in everyone if c["source"] == "manual"}
    check("the demo data holds candidates of both kinds for this team", "Amara Nwosu" in from_sf and {"Jordan Rivera", "Diego Morales"} <= by_hand, f"{sorted(from_sf)} / {sorted(by_hand)}")
    names_listed(page, known, known)
    marked = vis(page.locator("tr, li").filter(has=page.locator('[data-slot="source-tag"]')))
    check("the list marks exactly the candidates that came from Salesforce",
          marked.count() == len(from_sf) and all(marked.filter(has=page.get_by_role("link", name=n, exact=True)).count() == 1 for n in from_sf), f"{marked.count()} marked, {len(from_sf)} expected")
    check("the marker reads 'Salesforce' and says 'Source' to a screen reader", vis(page.locator('[data-slot="source-tag"]')).first.inner_text().replace("\n", " ").strip() in ("Source: Salesforce", "Salesforce")
          and vis(page.locator('[data-slot="source-tag"]')).first.locator(".sr-only").inner_text().strip() == "Source:")
    no_overflow(page, "Team Lead Candidates")
    shot(page, "tl_candidates")

    # the source filter is done by the API
    def filtered(option, param, want):
        with page.expect_response(lambda r: "/api/candidates?" in r.url and param in r.url) as info:
            page.get_by_role("combobox", name="Source").click()
            page.get_by_role("option", name=option, exact=True).click()
        return info.value.ok, names_listed(page, known, want)

    page.get_by_role("combobox", name="Source").click()
    offered = [o.inner_text().strip() for o in page.get_by_role("option").all()]
    page.keyboard.press("Escape")
    check("the Source filter offers any source, From Salesforce and Added manually", offered == ["Any source", "From Salesforce", "Added manually"], str(offered))
    ok, listed = filtered("Added manually", "source=manual", by_hand)
    check("filter 'Added manually': sent as source=manual, lists those and no marker", ok and listed == by_hand and "source=manual" in page.url
          and vis(page.locator('[data-slot="source-tag"]')).count() == 0, str(sorted(listed)))
    ok, listed = filtered("From Salesforce", "source=salesforce", from_sf)
    check("filter 'From Salesforce': sent as source=salesforce, lists only those", ok and listed == from_sf and "source=salesforce" in page.url, str(sorted(listed)))
    page.get_by_role("searchbox", name="Search candidates").fill("zzzz")
    check("a filter without a match says so in the list's words", shown(page.get_by_text("No candidates match these filters")))
    page.get_by_role("button", name="Clear filters").click()
    check("'Clear filters' brings everyone back", names_listed(page, known, known) == set(known) and "source=" not in page.url)

    # Add from Salesforce
    add.first.click()
    dialog = page.get_by_role("dialog", name="Add from Salesforce")
    dialog.wait_for(timeout=T)
    check("the dialog says what is brought in and that nothing is changed in Salesforce",
          dialog.get_by_text("Their details, job-search profile and newest resume are brought into the portal. Nothing is changed in Salesforce.").is_visible()
          and dialog.get_by_text("Read from Salesforce. Nothing is changed there.").is_visible())
    check("the dialog asks a Team Lead for the responsible member (optional) and not for a team",
          dialog.get_by_text("Responsible member").is_visible() and dialog.get_by_text("(optional)").is_visible() and dialog.get_by_text("Team the candidate is brought into").count() == 0)
    check("the dialog has the 'Active candidates only' switch, on", dialog.get_by_role("switch", name="Active candidates only").get_attribute("aria-checked") == "true")
    check("the dialog offers no way to type the candidate in instead: only Close",
          dialog.get_by_role("button", name=re.compile("Manually")).count() == 0 and dialog.get_by_role("button", name="Close").count() >= 1
          and page.get_by_role("dialog", name="Add Candidate").count() == 0)
    type_query(dialog, "amara", "Search Salesforce")
    row(dialog, "Amara Nwosu").wait_for(timeout=T)
    check("in the dialog a person who is already in the portal offers 'Open'", "In the portal" in row(dialog, "Amara Nwosu").inner_text() and "Open" in row(dialog, "Amara Nwosu").inner_text())
    dialog.get_by_role("combobox").click()
    offered = [o.inner_text().strip() for o in page.get_by_role("option").all()]
    check("the member select offers the active members of the own team, or nobody",
          offered[0] == "Nobody yet (unassigned)" and {"Maya Chen", "Noah Patel", "Omar Haddad"} <= set(offered) and not {"Priya Nair", "Quinn Lopez"} & set(offered), str(offered))
    page.get_by_role("option", name="Maya Chen", exact=True).click()
    type_query(dialog, "bruno", "Search Salesforce")
    row(dialog, "Bruno Castell").wait_for(timeout=T)
    if MOBILE:
        box = dialog.bounding_box()
        check("390 px: the Add from Salesforce dialog with a result fits the screen", box["x"] >= 0 and box["x"] + box["width"] <= A.w + 0.5 and box["y"] + box["height"] <= A.h + 0.5, str(box))
    no_overflow(page, "Add from Salesforce dialog")
    shot(page, "tl_dialog")
    dialog.get_by_role("searchbox").focus()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(r"/tl/candidates/[0-9a-f-]{36}\?tab=profile$"), timeout=T)
    bruno_id = page.url.rsplit("/", 1)[1].split("?")[0]
    page.get_by_role("heading", level=1, name="Bruno Castell").wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    sent = imports[-1].request.post_data_json if imports else {}
    check("brought in from the dialog by keyboard: the request names the person and the chosen member, and the new candidate opens on the profile",
          len(imports) == 1 and imports[0].status == 201 and sent == {"salesforce_id": SF_BRUNO, "team_id": None, "assigned_member_id": maya["id"]}, str(sent))
    done = toast(page, "Bruno Castell brought in from Salesforce")
    check("the confirmation says the resume came along", "The resume was brought in from Salesforce." in done.inner_text(), done.inner_text())
    check("the candidate's page names the responsible member chosen in the dialog", vis(page.get_by_role("link", name="Maya Chen")).first.get_attribute("href") == f"/tl/members/{maya['id']}")
    check("the header carries the Salesforce marker", vis(page.locator('[data-slot="source-tag"]')).count() >= 1)
    source = vis(page.locator('[data-slot="resume-source"]'))
    source.first.wait_for(timeout=T)
    check("the resume panel says the file came from Salesforce", source.first.inner_text().strip() == "From Salesforce" and source.first.get_attribute("data-source") == "salesforce"
          and page.get_by_text("brought in from Salesforce").count() >= 1)
    resume = api(page, f"/api/candidates/{bruno_id}/resume")
    check("the DOCX file from Salesforce was read like an uploaded one", resume.get("source") == "salesforce" and resume.get("filename", "").lower().endswith(".docx")
          and "Bruno Castell" in resume.get("text", "") and resume.get("version") == 1, str({k: resume.get(k) for k in ("source", "filename", "version")}))
    check("download, delete and upload are there as for any resume", vis(page.get_by_role("link", name="Download File")).count() >= 1
          and vis(page.get_by_role("button", name="Delete")).count() >= 1 and page.locator('input[type="file"]').count() >= 1)
    check("the upload hint says what a refresh from Salesforce does to an uploaded file", page.get_by_text("a refresh from Salesforce keeps it until Salesforce has a newer file").count() >= 1)
    no_overflow(page, "Team Lead candidate from Salesforce, profile tab")
    shot(page, "tl_candidate_profile")

    # a resume uploaded here replaces it, and says so
    page.locator('input[type="file"]').first.set_input_files({"name": "bruno-new.txt", "mimeType": "text/plain",
                                                                "buffer": b"Bruno Castell\nJava developer with seven years of experience in Spring, Kafka and AWS.\n" * 5})
    expect(vis(page.locator('[data-slot="resume-source"]')).first).to_have_text("Uploaded here", timeout=T)
    check("after an upload in the portal the resume reads 'Uploaded here' (version 2)",
          api(page, f"/api/candidates/{bruno_id}/resume").get("source") == "upload" and api(page, f"/api/candidates/{bruno_id}/resume").get("version") == 2)

    # the Salesforce block on the overview
    page.get_by_role("tab", name="Overview").click()
    panel = salesforce_panel(page)
    panel.wait_for(timeout=T)
    link = panel.get_by_role("link", name=re.compile("Open in Salesforce"))
    check("the Salesforce block says when the record was last read",
          panel.get_by_text("Last read from Salesforce").is_visible() and panel.locator("time").count() == 1 and panel.get_by_text("This candidate was brought in from Salesforce.").is_visible())
    check("'Open in Salesforce' leads to the record in a new tab, without handing the portal's address on",
          link.get_attribute("href") == f"https://salesforce.example.com/{SF_BRUNO}" and link.get_attribute("target") == "_blank"
          and link.get_attribute("rel") == "noopener noreferrer", str(link.get_attribute("href")))
    check("the block says the portal only reads Salesforce", panel.get_by_text("The portal only reads Salesforce. Changes made here are not written back there.").is_visible())
    refreshes = []
    page.on("request", lambda r: refreshes.append(r) if r.url.endswith("/salesforce-refresh") else None)
    panel.get_by_role("button", name="Refresh from Salesforce").click()
    confirm = page.get_by_role("alertdialog", name="Refresh Bruno Castell from Salesforce?")
    confirm.wait_for(timeout=T)
    body = confirm.inner_text()
    check("the confirmation states what is updated and what is kept",
          all(part in body for part in ("What is updated", "What is kept", "Every field that was changed in the portal", "A resume uploaded in the portal after the Salesforce file was last changed",
                                        "Nothing is written to Salesforce.")), body.replace("\n", " | ")[:300])
    if MOBILE:
        box = confirm.bounding_box()
        check("390 px: the refresh confirmation fits the screen", box["x"] >= 0 and box["x"] + box["width"] <= A.w + 0.5 and box["y"] + box["height"] <= A.h + 0.5, str(box))
    no_overflow(page, "refresh confirmation")
    shot(page, "tl_refresh_confirm")
    confirm.get_by_role("button", name="Cancel").click()
    confirm.wait_for(state="hidden", timeout=T)
    check("Cancel refreshes nothing", refreshes == [])
    # a detail changed in the portal survives the refresh, and the answer says so
    check("the phone number is changed in the portal (PATCH 200)", send(page, "PATCH", f"/api/candidates/{bruno_id}", {"phone": "+1 555 0142"}) == 200)
    panel.get_by_role("button", name="Refresh from Salesforce").click()
    confirm.wait_for(timeout=T)
    with page.expect_response(lambda r: r.url.endswith(f"/api/candidates/{bruno_id}/salesforce-refresh") and r.request.method == "POST") as info:
        confirm.get_by_role("button", name="Refresh from Salesforce").click()
    sync = info.value.json()["sync"]
    done = toast(page, "Bruno Castell was read again from Salesforce")
    words = done.inner_text()
    check("the refresh kept the phone number changed here and the resume uploaded here, and the API says so",
          info.value.status == 200 and sync["created"] is False and "phone" in sync["kept_fields"] and sync["resume"] in ("kept_local", "unchanged"), str(sync))
    check("the summary after the refresh says what was kept", "Kept as changed in the portal: phone." in words and "was kept" in words, words.replace("\n", " | "))
    after = api(page, f"/api/candidates/{bruno_id}")
    check("after the refresh the candidate still has the local phone number and the uploaded resume",
          after["phone"] == "+1 555 0142" and api(page, f"/api/candidates/{bruno_id}/resume").get("filename") == "bruno-new.txt" and after["assigned_member"]["id"] == maya["id"])
    confirm.wait_for(state="hidden", timeout=T)
    no_overflow(page, "Team Lead candidate from Salesforce, overview")
    shot(page, "tl_candidate_overview")

    # someone who is in the portal already is opened, not brought in twice
    page.goto(BASE + "/tl/candidates")
    add.first.wait_for(timeout=T)
    add.first.click()
    dialog.wait_for(timeout=T)
    type_query(dialog, "amara", "Search Salesforce")
    row(dialog, "Amara Nwosu").wait_for(timeout=T)
    before = len(imports)
    row(dialog, "Amara Nwosu").get_by_role("button").click()
    page.wait_for_url(re.compile(r"/tl/candidates/[0-9a-f-]{36}$"), timeout=T)
    page.get_by_role("heading", level=1, name="Amara Nwosu").wait_for(timeout=T)
    check("'Open' in the dialog leads to the candidate's page without bringing her in again", len(imports) == before and candidate_named(page, "Amara Nwosu")["id"] in page.url)
    check("a candidate seeded from Salesforce has the Salesforce block and a resume 'From Salesforce'", shown(salesforce_panel(page).get_by_role("button", name="Refresh from Salesforce")))

    # a candidate added by hand has none of it
    jordan = candidate_named(page, "Jordan Rivera")
    page.goto(BASE + f"/tl/candidates/{jordan['id']}")
    page.get_by_role("heading", level=1, name="Jordan Rivera").wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    check("a candidate added by hand has no Salesforce marker, block, link or refresh",
          page.locator('[data-slot="source-tag"]').count() == 0 and page.get_by_role("heading", name="Salesforce", exact=True).count() == 0
          and page.get_by_text("Open in Salesforce").count() == 0 and page.get_by_role("button", name="Refresh from Salesforce").count() == 0)
    with expecting("/salesforce-refresh", "status of 409"):
        check("the API refuses to refresh a candidate that did not come from Salesforce (409)", send(page, "POST", f"/api/candidates/{jordan['id']}/salesforce-refresh") == 409)
    page.goto(BASE + f"/tl/candidates/{jordan['id']}?tab=profile")
    source = vis(page.locator('[data-slot="resume-source"]'))
    check("the resume of a candidate added by hand reads 'Uploaded here'", shown(source, T) and source.first.inner_text().strip() == "Uploaded here"
          and source.first.get_attribute("data-source") == "upload")
    ctx.close()


# ------------------------------------------------------------------------------------------ 4. Admin
def step_admin(browser):
    print("== 4. Admin: the team a candidate is brought into, the team page, integrations, the activity log")
    ctx, page = signed_in(browser, "admin@example.com")
    imports = []
    page.on("response", lambda r: imports.append(r) if r.url.endswith("/api/candidates/import-salesforce") else None)
    teams = {t["name"]: t for t in api(page, "/api/teams")}
    quinn = next(u for u in api(page, "/api/users?role=MEMBER&page_size=50")["items"] if u["email"] == "quinn@example.com")
    group = open_search_form(page, "/admin")
    check("an Admin is asked for the team the candidate is brought into", group.get_by_text("Team the candidate is brought into").is_visible()
          and group.get_by_role("combobox").inner_text().strip() == "Choose a team")
    type_query(page, "keiko")
    row(group, "Keiko Tanabe").wait_for(timeout=T)
    row(group, "Keiko Tanabe").get_by_role("button").click()
    check("without a team nothing is brought in: the Admin is asked for it in place, and the focus goes to the team select",
          shown(group.get_by_role("alert").filter(has_text="Choose the team first, then choose the candidate again."), 5000) and imports == []
          and page.evaluate("() => document.activeElement?.getAttribute('role')") == "combobox" and group.get_by_role("combobox").get_attribute("aria-invalid") == "true")
    shot(page, "admin_team_needed")
    page.keyboard.press("Enter")
    page.get_by_role("option", name="Team Beta", exact=True).click()
    check("choosing a team takes the request away", group.get_by_text("Choose the team first").count() == 0)
    row(group, "Keiko Tanabe").get_by_role("button").click()
    selected = page.get_by_role("region", name="Selected candidates")
    selected.get_by_role("listitem").filter(has_text="Keiko Tanabe").wait_for(timeout=T)
    sent = imports[-1].request.post_data_json if imports else {}
    keiko = candidate_named(page, "Keiko Tanabe")
    check("the Admin's import names the chosen team, and the candidate is in that team and selected",
          len(imports) == 1 and imports[0].status == 201 and sent.get("team_id") == teams["Team Beta"]["id"] and keiko["team"]["name"] == "Team Beta" and keiko["source"] == "salesforce", str(sent))
    type_query(page, "elif")
    row(group, "Elif Demir").wait_for(timeout=T)
    text = row(group, "Elif Demir").inner_text()
    check("an Admin sees a candidate of any team as 'In the portal', with 'Select'", "In the portal" in text and "Select" in text and "Not available" not in text, text.replace("\n", " | "))
    no_overflow(page, "Admin Find Jobs with a Salesforce result")
    shot(page, "admin_search")

    # the whole team chosen in the form is the team a person is brought into
    group = open_search_form(page, "/admin")
    page.get_by_role("combobox", name="Whole team").click()
    page.get_by_role("option", name=re.compile("^Team Alpha")).click()
    check("the team chosen as 'Whole team' is preselected for a person from Salesforce", shown(group.get_by_role("combobox").filter(has_text="Team Alpha"), 5000))

    # Candidates page
    page.goto(BASE + "/admin/candidates")
    page.wait_for_load_state("networkidle")
    add = vis(page.get_by_role("button", name="Add from Salesforce", exact=True))
    add.first.wait_for(timeout=T)
    check("Admin Candidates: 'Add from Salesforce', and nothing to type a candidate in with", add.count() == 1 and page.get_by_role("button", name=re.compile("^Add (Candidate|Manually)")).count() == 0)
    everyone = api(page, "/api/candidates?page_size=200")["items"]
    from_sf = {c["name"] for c in everyone if c["source"] == "salesforce"}
    with page.expect_response(lambda r: "/api/candidates?" in r.url and "source=salesforce" in r.url):
        page.get_by_role("combobox", name="Source").click()
        page.get_by_role("option", name="From Salesforce", exact=True).click()
    listed = names_listed(page, [c["name"] for c in everyone], from_sf)
    check("Admin Candidates, filter 'From Salesforce': the candidates of every team that came from Salesforce", listed == from_sf and {"Amara Nwosu", "Elif Demir", "Keiko Tanabe"} <= from_sf,
          str(sorted(listed)))
    no_overflow(page, "Admin Candidates, From Salesforce")
    page.get_by_role("button", name="Clear filters").click()
    add.first.click()
    dialog = page.get_by_role("dialog", name="Add from Salesforce")
    dialog.wait_for(timeout=T)
    check("the Admin's dialog asks for the team and the responsible member", dialog.get_by_text("Team the candidate is brought into").is_visible() and dialog.get_by_text("Responsible member").is_visible()
          and dialog.get_by_role("combobox").count() == 2)
    type_query(dialog, "hugo", "Search Salesforce")
    row(dialog, "Hugo Lindqvist").wait_for(timeout=T)
    dialog.get_by_role("combobox").first.click()
    page.get_by_role("option", name="Team Beta", exact=True).click()
    dialog.get_by_role("combobox").nth(1).click()
    page.get_by_role("option", name="Quinn Lopez", exact=True).wait_for(timeout=T)
    offered = [o.inner_text().strip() for o in page.get_by_role("option").all()]
    check("the member select offers the members of the chosen team only", {"Priya Nair", "Quinn Lopez"} <= set(offered) and not {"Maya Chen", "Noah Patel", "Omar Haddad"} & set(offered), str(offered))
    page.get_by_role("option", name="Quinn Lopez", exact=True).click()
    if MOBILE:
        box = dialog.bounding_box()
        check("390 px: the Admin's Add from Salesforce dialog fits the screen", box["x"] >= 0 and box["x"] + box["width"] <= A.w + 0.5 and box["y"] + box["height"] <= A.h + 0.5, str(box))
    no_overflow(page, "Admin Add from Salesforce dialog")
    shot(page, "admin_dialog")
    row(dialog, "Hugo Lindqvist").get_by_role("button").click()
    page.wait_for_url(re.compile(r"/admin/candidates/[0-9a-f-]{36}\?tab=profile$"), timeout=T)
    page.get_by_role("heading", level=1, name="Hugo Lindqvist").wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    sent = imports[-1].request.post_data_json
    hugo = candidate_named(page, "Hugo Lindqvist")
    check("brought in from the Admin's dialog: team and member as chosen, and the candidate's page opens",
          imports[-1].status == 201 and sent == {"salesforce_id": "001E2E000000008AAA", "team_id": teams["Team Beta"]["id"], "assigned_member_id": quinn["id"]}
          and hugo["team"]["name"] == "Team Beta" and hugo["assigned_member"]["name"] == "Quinn Lopez"
          and vis(page.get_by_role("link", name="Quinn Lopez")).count() >= 1 and vis(page.get_by_text("Team Beta")).count() >= 1, str(sent))
    page.get_by_role("tab", name="Overview").click()
    panel = salesforce_panel(page)
    check("an Admin has the Salesforce block with link and refresh", shown(panel.get_by_role("button", name="Refresh from Salesforce"), T)
          and panel.get_by_role("link", name=re.compile("Open in Salesforce")).get_attribute("href") == "https://salesforce.example.com/001E2E000000008AAA")
    no_overflow(page, "Admin candidate from Salesforce, overview")

    # an Admin's team page, Candidates tab
    page.goto(BASE + f"/admin/teams/{teams['Team Alpha']['id']}?tab=candidates")
    add = vis(page.get_by_role("button", name="Add from Salesforce", exact=True))
    add.first.wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    check("team page, Candidates tab: 'Add from Salesforce', usable, and nothing to type a candidate in with", add.count() == 1 and add.first.is_enabled() and page.get_by_role("button", name=re.compile("^Add (Candidate|Manually)")).count() == 0)
    check("team page, Candidates tab: the Source filter and the marker are there",
          vis(page.get_by_role("combobox", name="Source")).count() == 1 and vis(page.locator("tr, li").filter(has=page.locator('[data-slot="source-tag"]')).filter(
              has=page.get_by_role("link", name="Amara Nwosu", exact=True))).count() == 1)
    add.first.click()
    dialog.wait_for(timeout=T)
    check("team page: 'Add from Salesforce' starts with this team chosen", shown(dialog.get_by_role("combobox").first.filter(has_text="Team Alpha"), 5000))
    check("team page: the Add from Salesforce dialog offers no manual way either", dialog.get_by_role("button", name=re.compile("Manually")).count() == 0)
    dialog.get_by_role("button", name="Close").first.click()
    dialog.wait_for(state="hidden", timeout=T)
    no_overflow(page, "Admin team page, Candidates tab")
    shot(page, "admin_team_candidates")

    # integrations
    page.goto(BASE + "/admin/settings?tab=integrations")
    panel = salesforce_panel(page)
    panel.wait_for(timeout=T)
    text = panel.inner_text()
    check("Integrations: Salesforce is configured, reachable and read-only, with the API's sentence, version and the Team Lead scope",
          all(part in text for part in ("Configured", "Reachable", "Read-only", "Connected (demo data, not the real Salesforce)", "v68.0", "Team Leads can find", "Every candidate",
                                        "Nothing is written back.")) and "Not reachable" not in text, text.replace("\n", " | "))
    no_overflow(page, "Admin integrations")
    shot(page, "admin_integrations")
    words = "Salesforce refused the sign-in (altered answer)."
    alter_json(page, "**/api/integrations", lambda body: body["salesforce"].update(connected=False, message=words))
    page.reload()
    panel.wait_for(timeout=T)
    check("Integrations, connected: false (altered answer): 'Not reachable', in the API's words", shown(panel.get_by_text("Not reachable")) and words in panel.inner_text()
          and "Configured" in panel.inner_text(), panel.inner_text().replace("\n", " | "))
    page.unroute("**/api/integrations")
    page.route("**/api/integrations", lambda route: route.fulfill(response=(r := route.fetch()), json={**r.json(), "salesforce": STATUS_OFF}))
    page.reload()
    panel.wait_for(timeout=T)
    check("Integrations, not configured (altered answer): said so, and that candidates are added manually",
          shown(panel.get_by_text("Not configured on the server")) and "candidates are added manually" in panel.inner_text() and "Read-only" not in panel.inner_text(),
          panel.inner_text().replace("\n", " | "))
    page.unroute("**/api/integrations")

    # the activity log names what happened
    page.goto(BASE + "/admin/activity?action=candidate")
    check("Activity log: 'Brought a candidate in from Salesforce'", shown(vis(page.get_by_text("Brought a candidate in from Salesforce")), T))
    page.goto(BASE + "/admin/activity?action=resume")
    check("Activity log: 'Brought a candidate's resume in from Salesforce'", shown(vis(page.get_by_text("Brought a candidate's resume in from Salesforce")), T))
    no_overflow(page, "Admin activity log")
    ctx.close()


# ------------------------------------------------------------------------------------------ 5. the Team Lead scope
def set_scope(page, label):
    page.goto(BASE + "/admin/settings")
    scope = page.get_by_role("radiogroup", name="Candidates a Team Lead can find in Salesforce")
    scope.wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    scope.get_by_role("radio", name=re.compile(label)).click()
    with page.expect_response(lambda r: r.url.endswith("/api/settings") and r.request.method == "PATCH", timeout=T) as info:
        page.get_by_role("button", name="Save Settings").click()
    if info.value.status == 200:
        toast(page, "Settings saved")
    return info.value


def step_scope(browser):
    print("== 5. Admin settings: what a Team Lead can find in Salesforce (all or own)")
    ctx, page = signed_in(browser, "admin@example.com")
    page.goto(BASE + "/admin/settings")
    scope = page.get_by_role("radiogroup", name="Candidates a Team Lead can find in Salesforce")
    scope.wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    check("Settings: two explained choices, 'Every candidate' chosen (the default)",
          scope.get_by_role("radio").count() == 2 and scope.get_by_role("radio", name=re.compile("Every candidate")).get_attribute("aria-checked") == "true"
          and scope.get_by_role("radio", name=re.compile("Only their own candidates")).get_attribute("aria-checked") == "false"
          and api(page, "/api/settings")["salesforce_team_lead_scope"] == "all")
    check("Settings: each choice says what it means, and that an Admin always sees all",
          page.get_by_text("A Team Lead can find and bring in every candidate in Salesforce.").is_visible()
          and page.get_by_text("only the candidates whose marketing team lead in Salesforce has the Team Lead's sign-in e-mail address").is_visible()
          and page.get_by_text("An Admin always sees all of them.").is_visible())
    no_overflow(page, "Admin settings with the Salesforce choice")
    shot(page, "admin_settings")
    try:
        saved = set_scope(page, "Only their own candidates")
        check("Settings: 'Only their own candidates' is saved as salesforce_team_lead_scope = own",
              saved.status == 200 and saved.request.post_data_json.get("salesforce_team_lead_scope") == "own" and api(page, "/api/settings")["salesforce_team_lead_scope"] == "own")
        page.goto(BASE + "/admin/settings?tab=integrations")
        panel = salesforce_panel(page)
        check("Integrations names the scope now in force", shown(panel.get_by_text("Only their own candidates"), T))

        # a Team Lead of Team Beta: only the candidates whose marketing team lead is Ben Beta
        tl_ctx, tl = signed_in(browser, "lead.beta@example.com")
        check("the status the Team Lead receives carries the scope", api(tl, "/api/salesforce/status").get("team_lead_scope") == "own")
        group = open_search_form(tl)
        type_query(tl, "bruno")
        check("scope 'own': another Team Lead's candidate is not found", shown(group.get_by_text('Nobody in Salesforce matches "bruno"'), T))
        check("scope 'own': the reason is given", group.get_by_text("You can find the candidates whose marketing team lead in Salesforce is you.").is_visible())
        type_query(tl, "keiko")
        check("scope 'own': a candidate without a marketing team lead is not found either", shown(group.get_by_text('Nobody in Salesforce matches "keiko"'), T))
        type_query(tl, "ines")
        check("scope 'own': the Team Lead's own candidate is found and can be brought in", shown(row(group, "Ines Carvalho"), T) and "Bring in" in row(group, "Ines Carvalho").inner_text()
              and "Marketing team lead: Ben Beta" in row(group, "Ines Carvalho").inner_text())
        type_query(tl, "elif")
        check("scope 'own': an own candidate who is in the portal is offered as before", shown(row(group, "Elif Demir"), T) and "In the portal" in row(group, "Elif Demir").inner_text())
        no_overflow(tl, "Team Lead Find Jobs under scope 'own'")
        with expecting("/api/candidates/import-salesforce", "status of 404"):
            refused = send(tl, "POST", "/api/candidates/import-salesforce", {"salesforce_id": SF_DMITRI, "team_id": None, "assigned_member_id": None})
            check("scope 'own': the API refuses to bring in another Team Lead's candidate by its id (404)", refused == 404 and candidate_named(page, "Dmitri Volkov") is None, f"HTTP {refused}")
        tl.goto(BASE + "/tl/candidates")
        vis(tl.get_by_role("button", name="Add from Salesforce", exact=True)).first.click()
        dialog = tl.get_by_role("dialog", name="Add from Salesforce")
        type_query(dialog, "chloe", "Search Salesforce")
        check("scope 'own': the Add from Salesforce dialog gives the same answer and reason",
              shown(dialog.get_by_text('Nobody in Salesforce matches "chloe"'), T) and dialog.get_by_text("whose marketing team lead in Salesforce is you").is_visible())
        tl_ctx.close()

        # the Admin is not narrowed
        group = open_search_form(page, "/admin")
        type_query(page, "dmitri")
        check("scope 'own': an Admin still finds every candidate, and is given no such reason", shown(row(group, "Dmitri Volkov"), T))
        type_query(page, "zzzz")
        check("scope 'own': an Admin's no-match hint does not speak of own candidates",
              shown(group.get_by_text('Nobody in Salesforce matches "zzzz"'), T) and group.get_by_text("whose marketing team lead in Salesforce is you").count() == 0)
    finally:
        saved = set_scope(page, "Every candidate")
        check("Settings: back to 'Every candidate' (all)", saved.status == 200 and api(page, "/api/settings")["salesforce_team_lead_scope"] == "all")
    tl_ctx, tl = signed_in(browser, "lead.beta@example.com")
    group = open_search_form(tl)
    type_query(tl, "bruno")
    check("scope 'all' again: the Team Lead of Team Beta finds the other lead's candidate, as 'not available' because Team Alpha has him",
          shown(row(group, "Bruno Castell"), T) and group.get_by_text("whose marketing team lead in Salesforce is you").count() == 0,
          group.inner_text().replace("\n", " | ")[:200])
    tl_ctx.close()
    ctx.close()


# ------------------------------------------------------------------------------------------ 6. Member
def step_member(browser):
    print("== 6. Member: the marker only; no Salesforce link, action or request, and the API refuses")
    ctx = new_context(browser)
    page = ctx.new_page()
    asked = []
    page.on("request", lambda r: asked.append(r.url) if "/api/salesforce" in r.url else None)
    login(page, "maya@example.com")                 # Bruno Castell was brought in with Maya as the responsible member (step 3)
    mine = [c for c in api(page, "/api/candidates?page_size=50")["items"] if c["source"] == "salesforce"]
    if not check("the member is responsible for a candidate that came from Salesforce", len(mine) >= 1, "run the step 'candidates' first (it brings Bruno Castell in with Maya)"):
        ctx.close()
        return
    person = mine[0]
    check("the API gives a member neither the Salesforce id nor the link", all(c["salesforce_id"] is None and c["salesforce_url"] is None for c in mine))
    page.goto(BASE + "/member/candidates")
    card = page.locator('[data-slot="candidate-card"]').filter(has_text=person["name"])
    card.wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    check("My Candidates: the card of that candidate carries the source marker, the others do not",
          vis(card.locator('[data-slot="source-tag"]')).count() == 1
          and vis(page.locator('[data-slot="candidate-card"]').filter(has=page.locator('[data-slot="source-tag"]'))).count() == len(mine))
    check("My Candidates: nothing to add a candidate with, from Salesforce or by hand",
          page.get_by_role("button", name=re.compile("^Add (from Salesforce|Manually|Candidate)$")).count() == 0 and page.get_by_role("combobox", name="Source").count() == 0)
    no_overflow(page, "member, My Candidates with a Salesforce candidate")
    for tab in ("", "?tab=details", "?tab=profile", "?tab=resume"):
        page.goto(BASE + f"/member/candidates/{person['id']}{tab}")
        page.get_by_role("heading", level=1, name=person["name"]).wait_for(timeout=T)
        page.wait_for_load_state("networkidle")
        check(f"member, candidate page{' ' + tab if tab else ''}: the marker, and no Salesforce block, link or refresh",
              vis(page.locator('[data-slot="source-tag"]')).count() >= 1 and page.get_by_text("Open in Salesforce").count() == 0
              and page.get_by_role("button", name="Refresh from Salesforce").count() == 0 and page.get_by_role("heading", name="Salesforce", exact=True).count() == 0
              and page.locator('a[href*="salesforce.example.com"]').count() == 0)
    no_overflow(page, "member, candidate from Salesforce")
    shot(page, "member_candidate")
    for path in ("/member/dashboard", "/member/jobs", "/member/applications", "/member/settings"):
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
    check("no page of a member asks the API about Salesforce", asked == [], str(asked))
    for path in ("/tl/search", "/tl/candidates", "/admin/settings?tab=integrations"):
        page.goto(BASE + path)
        page.wait_for_url(re.compile(r"/member/dashboard$"), timeout=T)
    check("the screens that offer Salesforce do not open for a member", page.url.endswith("/member/dashboard") and asked == [], page.url)
    with expecting("/api/salesforce", "/api/candidates/import-salesforce", "/salesforce-refresh", "status of 403"):
        codes = {"status": api(page, "/api/salesforce/status").get("__status"), "search": api(page, "/api/salesforce/candidates?q=bruno").get("__status"),
                 "import": send(page, "POST", "/api/candidates/import-salesforce", {"salesforce_id": SF_DMITRI, "team_id": None, "assigned_member_id": None}),
                 "refresh": send(page, "POST", f"/api/candidates/{person['id']}/salesforce-refresh")}
        check("a member who calls the Salesforce endpoints directly is refused with 403 (status, search, import, refresh)", set(codes.values()) == {403}, str(codes))
        page.wait_for_timeout(300)
    ctx.close()


STEPS = [
    ("picker", step_picker),
    ("states", step_states),
    ("candidates", step_candidates),
    ("admin", step_admin),
    ("scope", step_scope),
    ("member", step_member),
]

with sync_playwright() as p:
    # Nothing in this run may reach a real external site (Salesforce least of all): every host except this machine fails to resolve.
    browser = p.chromium.launch(args=["--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost"])
    for key, step in STEPS:
        if A.only and key not in A.only.split(","):
            continue
        try:
            step(browser)
        except Exception as exc:  # a step that cannot go on is a failed check, the others still run
            check(f"step '{key}' ran to its end", False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}")
            traceback.print_exc()
    browser.close()

passed = sum(1 for ok, _, _ in results if ok)
failed = [(name, detail) for ok, name, detail in results if not ok]
print(f"\n{A.w}x{A.h}: {passed} passed, {len(failed)} failed, {len(problems)} unexpected console/network problems")
for name, detail in failed:
    print(f"  FAILED {name}  [{detail}]")
for problem in problems:
    print(f"  PROBLEM {problem}")
sys.exit(1 if failed or problems else 0)
