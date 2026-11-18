"""Fourth browser check list: AI auto-apply (Admin only) and the application answers a Team Lead records.

Admin: the candidate row menu "Auto Apply", the start dialog (eligibility per job from the API: application system,
supported or why not, the current run), the consent block and recording a consent, starting a run that the real
worker code prepares against the local fake ATS, the run sheet (screenshot, answer sources, the AI-drafted answer,
needs-input links, blocked reasons, outcome unknown, the submitted confirmation), editing an answer (a new dry run),
the approve dialog and the submission, the queue page with its batch rule (only previewed runs), the plain-language
refusal texts, notifications, audit labels and the integrations panel; in light and dark.
Team Lead: no auto-apply anywhere; the Application answers form on the Profile tab works and saves.
Member: no auto-apply anywhere; the `application_submitted` notification. `/admin/auto-apply` redirects non-Admins.

    python tests/e2e/ui_checks_auto_apply.py --base http://127.0.0.1:3011 --w 1280 --h 900
    python tests/e2e/ui_checks_auto_apply.py --base http://127.0.0.1:3011 --w 390 --h 844

It needs the demo stack with freshly seeded data (tests/e2e/run.sh runs it): the run starts, edits and approves runs.
The demo API's auto-apply browser reaches only the local fake ATS (tests/autoapply/fake_ats.py); this test's own
browser cannot resolve any host but this machine. The two refusal texts (a single approve answered 409, a batch with a
refused run) are checked by answering the portal's own /api request in the browser; those checks say so.
"""
import argparse
import json
import re
import sys
import traceback
from contextlib import contextmanager

from playwright.sync_api import sync_playwright

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
RUN_T = 90000                     # a dry run or a submission in the demo API's browser

results = []
problems = []
expected = []


def check(name, ok, detail=""):
    results.append((bool(ok), name, detail))
    print(("  PASS " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)
    return bool(ok)


@contextmanager
def expecting(*substrings):
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


def new_context(browser, dark=False):
    ctx = browser.new_context(viewport={"width": A.w, "height": A.h}, color_scheme="dark" if dark else "light")
    ctx.on("page", watch)
    return ctx


def login(page, email):
    page.goto(BASE + "/login")
    page.get_by_label("E-mail").fill(email)
    page.get_by_label("Password", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(lambda u: "/login" not in u, timeout=T)
    page.wait_for_load_state("networkidle")


def signed_in(browser, email, dark=False):
    ctx = new_context(browser, dark)
    page = ctx.new_page()
    login(page, email)
    return ctx, page


def api(page, path):
    return page.evaluate("async (p) => { const r = await fetch(p, {credentials: 'same-origin'}); return r.ok ? await r.json() : {__status: r.status} }", path)


def no_overflow(page, name):
    overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check(f"no horizontal page overflow: {name}", overflow <= 0, f"{overflow}px")


def shot(page, name):
    if A.shots:   # a full-page capture resizes the viewport, which can close an open dialog: then only the viewport
        page.screenshot(path=f"{A.shots}/{A.w}_aa_{name}.png", full_page=page.get_by_role("dialog").count() == 0)


def toast(page, text, timeout=T):
    item = page.locator("[data-sonner-toast]").filter(has_text=text).first
    item.wait_for(timeout=timeout)
    return item


def candidate_id(page, name):
    found = api(page, "/api/candidates?" + "q=" + name.split()[0] + "&page_size=50")
    return next(c["id"] for c in found["items"] if c["name"] == name)


def runs_by_company(page, cid):
    return {r["job"]["company"]: r for r in api(page, f"/api/auto-apply/runs?candidate_id={cid}&page_size=50")["items"]}


def open_row_menu(page, name):
    page.get_by_role("button", name=f"Actions for {name}").first.click()


def open_start_dialog(page, name):
    page.goto(BASE + "/admin/candidates")
    page.wait_for_load_state("networkidle")
    open_row_menu(page, name)
    item = page.get_by_role("menuitem", name="Auto Apply")
    check(f"Admin: the row menu of {name} offers Auto Apply", item.is_visible())
    item.click()
    dialog = page.get_by_role("dialog", name=f"Auto Apply for {name}")
    dialog.wait_for(timeout=T)
    dialog.get_by_role("list", name="Jobs that can be applied to").or_(dialog.get_by_text("No open jobs")).first.wait_for(timeout=T)
    return dialog


def job_row(scope, company):
    return scope.locator("li").filter(has_text=company).first


def open_run(page, company):
    """On a list of runs (candidate tab or queue): opens the run sheet of the run at `company`."""
    row = page.locator("li").filter(has=page.locator("[data-run-status]")).filter(has_text=company).first
    row.wait_for(timeout=T)
    row.get_by_role("button").first.click()
    sheet = page.get_by_role("dialog").filter(has=page.get_by_role("heading", name="Answers", exact=False)).first
    sheet.wait_for(timeout=T)
    return sheet


def close_sheet(page):
    page.keyboard.press("Escape")
    page.get_by_role("dialog").first.wait_for(state="detached", timeout=T)


def wait_run_status(page, company, status, timeout=RUN_T):
    page.locator("li").filter(has_text=company).locator(f'[data-run-status="{status}"]').first.wait_for(timeout=timeout)


# ---------------------------------------------------------------------------------------------------- Admin
def step_dialog(browser):
    ctx, page = signed_in(browser, "admin@example.com")
    dialog = open_start_dialog(page, "Jordan Rivera")
    check("start dialog: consent in force for Jordan", dialog.get_by_text("Consent in force").is_visible())
    robotics = job_row(dialog, "Northpeak Robotics")
    check("start dialog: the Greenhouse job without a run is supported and can be picked",
          robotics.get_by_text("Greenhouse", exact=True).is_visible() and robotics.get_by_text("Supported", exact=True).is_visible()
          and robotics.get_by_role("checkbox").is_enabled())
    lever = job_row(dialog, "Lumen Labs")
    check("start dialog: a job with an open run shows its run and cannot be picked",
          lever.locator('[data-run-status="ready_for_review"]').is_visible() and lever.get_by_text("already being prepared").is_visible()
          and not lever.get_by_role("checkbox").is_enabled())
    ashby = job_row(dialog, "Keystone AI")
    check("start dialog: a posting whose submission may have gone through cannot be applied to again",
          ashby.get_by_text("already submitted").is_visible() and not ashby.get_by_role("checkbox").is_enabled())
    check("start dialog: the submitted job is no longer listed", job_row(dialog, "Orbit Cloud").count() == 0)
    check("start dialog: Prepare is disabled while nothing is picked", not dialog.get_by_role("button", name=re.compile("^Prepare")).is_enabled())
    no_overflow(page, "start dialog")
    shot(page, "dialog")
    dialog.get_by_role("button", name="Close").first.click()

    # Hannah: no consent. Recording it unlocks the dialog.
    dialog = open_start_dialog(page, "Hannah Kim")
    check("start dialog without consent: the consent block says so", dialog.get_by_text("No consent recorded").is_visible())
    boxes = dialog.get_by_role("checkbox")
    check("start dialog without consent: no job can be picked", boxes.count() > 0 and all(not b.is_enabled() for b in boxes.all()), str(boxes.count()))
    shot(page, "dialog_no_consent")
    dialog.get_by_role("button", name="Record consent").click()
    record = page.get_by_role("dialog", name="Record consent")
    record.wait_for(timeout=T)
    record.get_by_role("combobox", name="How consent was given").click()
    page.get_by_role("option", name="By e-mail").click()
    record.get_by_label("Document reference").fill("E-mail of 18 November, demo")
    no_overflow(page, "record consent dialog")
    record.get_by_role("button", name="Record consent").click()
    toast(page, "Consent recorded for Hannah Kim")
    record.wait_for(state="detached", timeout=T)
    dialog.get_by_text("Consent in force").wait_for(timeout=T)
    enabled = [b for b in dialog.get_by_role("checkbox").all() if b.is_enabled()]
    check("after recording the consent, the dialog shows it in force and jobs can be picked", len(enabled) > 0, str(len(enabled)))
    dialog.get_by_role("button", name="Close").first.click()
    ctx.close()


def step_run(browser):
    ctx, page = signed_in(browser, "admin@example.com")
    jordan = candidate_id(page, "Jordan Rivera")
    dialog = open_start_dialog(page, "Jordan Rivera")
    job_row(dialog, "Northpeak Robotics").get_by_role("checkbox").click()
    dialog.get_by_role("button", name="Prepare 1 application").click()
    toast(page, "1 application is being prepared for your review")
    check("start run: the dialog closes after starting", page.get_by_role("dialog", name="Auto Apply for Jordan Rivera").count() == 0)

    page.goto(BASE + f"/admin/candidates/{jordan}?tab=auto-apply")
    page.get_by_role("heading", level=1, name="Jordan Rivera").wait_for(timeout=T)
    wait_run_status(page, "Northpeak Robotics", "ready_for_review")
    check("the new run was prepared by the worker and is ready for review", True)
    no_overflow(page, "candidate Auto Apply tab")
    shot(page, "tab")

    sheet = open_run(page, "Northpeak Robotics")
    img = sheet.get_by_role("img", name=re.compile("Screenshot 1 of the filled application form"))
    img.wait_for(timeout=T)
    img.scroll_into_view_if_needed()
    page.wait_for_function("(el) => el.complete && el.naturalWidth > 100", arg=img.element_handle(), timeout=T)
    check("run sheet: the dry-run screenshot is shown", True)
    check("run sheet: the dry run blocked every submission", sheet.get_by_text(re.compile(r"0 allowed")).is_visible())
    sources = {s: sheet.get_by_text(s, exact=True).count() for s in ("Candidate details", "Application answers", "Resume file", "AI draft", "Declined (EEO)")}
    check("run sheet: every answer shows where it came from", all(sources.values()), str(sources))
    ai = sheet.locator("#run-answer-question_2")
    check("run sheet: the AI-drafted answer is highlighted and flagged",
          "bg-warning" in (ai.get_attribute("class") or "") and ai.get_by_text("Written by AI. Read it before approving.").is_visible()
          and sheet.get_by_text("1 answer was drafted by AI and is highlighted.").is_visible())
    check("run sheet: the resume file cannot be edited as text", sheet.get_by_role("button", name="Edit Resume/CV").count() == 0)
    ai.get_by_role("button", name="Where this comes from").click()
    check("run sheet: the evidence of an answer opens", ai.get_by_text("Skills: Python, PyTorch").is_visible())
    no_overflow(page, "run sheet")
    shot(page, "sheet")

    # Edit the AI answer: recorded as an Admin edit, a new dry run is prepared.
    ai.get_by_role("button", name=re.compile("^Edit Why do you want")).click()
    box = ai.get_by_role("textbox")
    box.fill("I have built production models with Python and PyTorch and want to do that at Northpeak Robotics.")
    ai.get_by_role("button", name="Save").click()
    toast(page, "Answer saved")
    sheet.locator('[data-run-status="ready_for_review"]').wait_for(timeout=RUN_T)
    edited = sheet.locator("#run-answer-question_2")
    check("answer edit: the run is prepared again and the answer is marked as edited by the Admin",
          edited.get_by_text("Edited by Admin", exact=True).is_visible() and edited.get_by_text("want to do that at Northpeak Robotics").is_visible())

    # The approve dialog says exactly what happens.
    sheet.get_by_role("button", name="Approve and submit").click()
    confirm = page.get_by_role("alertdialog").or_(page.get_by_role("dialog", name="Submit this application?")).first
    confirm.wait_for(timeout=T)
    text = confirm.inner_text()
    check("approve dialog: names job, employer, candidate, the form address, the answers and that it cannot be undone",
          all(s in text for s in ("Submit this application?", "Robotics Perception Engineer", "Northpeak Robotics", "Jordan Rivera",
                                  "job-boards.greenhouse.io/acme/jobs/1001", "15 answers", "exactly as in the preview",
                                  "cannot be undone")), text[:300])
    shot(page, "approve")

    # A refusal (409 with a stable code) is shown in plain words. Answered in the browser: the real API would approve.
    def refuse(route):
        route.fulfill(status=409, content_type="application/json",
                      body=json.dumps({"error": "answers_changed: The answers changed since this preview; reload the run and review it again", "detail": ""}))
    page.route("**/api/auto-apply/runs/*/approve", refuse)
    with expecting("/approve", "409"):
        confirm.get_by_role("button", name="Submit application").click()
        toast(page, "The answers changed since you opened the preview")
        page.wait_for_timeout(300)
    check("approve refused (answered in the browser): the 409 code is explained in plain words", True)
    page.unroute("**/api/auto-apply/runs/*/approve")

    sheet.get_by_role("button", name="Approve and submit").click()
    confirm.wait_for(timeout=T)
    confirm.get_by_role("button", name="Submit application").click()
    toast(page, "Application approved and being submitted")
    sheet.locator('[data-run-status="submitted"]').wait_for(timeout=RUN_T)
    sheet.get_by_role("heading", name="Submitted", exact=True).wait_for(timeout=T)
    check("approve: the run is submitted and the confirmation page is reported",
          sheet.get_by_text("The employer's confirmation page was detected.").is_visible())
    check("submitted: the job is marked Applied", sheet.get_by_text("The job is marked").is_visible())
    shot(page, "submitted")
    close_sheet(page)
    ctx.close()


def step_states(browser):
    ctx, page = signed_in(browser, "admin@example.com")
    jordan = candidate_id(page, "Jordan Rivera")
    page.goto(BASE + f"/admin/candidates/{jordan}?tab=auto-apply")
    page.get_by_role("heading", level=1, name="Jordan Rivera").wait_for(timeout=T)

    sheet = open_run(page, "Unknown Co")
    needs = sheet.locator("section").filter(has=page.get_by_role("heading", name="Needs input", exact=True))
    link = needs.get_by_role("link", name="What is your notice period?")
    check("needs input: a missing recorded answer links to its field on the Profile tab",
          (link.get_attribute("href") or "").endswith(f"/admin/candidates/{jordan}?tab=profile#answer-notice_period")
          and needs.get_by_text("not recorded").is_visible(), link.get_attribute("href") or "")
    run_link = needs.get_by_role("link", name=re.compile("T-shirt size"))
    check("needs input: a question only this run asks links to its answer in the run",
          (run_link.get_attribute("href") or "").startswith("#run-answer-"), run_link.get_attribute("href") or "")
    no_overflow(page, "run sheet, needs input")
    shot(page, "needs_input")
    link.click()
    page.wait_for_url(re.compile(r"tab=profile"), timeout=T)
    page.locator('input[name="notice_period"]').wait_for(timeout=T)
    page.wait_for_function("() => document.activeElement && document.activeElement.getAttribute('name') === 'notice_period'", timeout=T)
    check("the needs-input link opens the Profile tab with the field focused", True)

    page.goto(BASE + f"/admin/candidates/{jordan}?tab=auto-apply")
    sheet = open_run(page, "Captcha Co")
    check("blocked: the sheet says a person has to finish it, with the reason in plain words",
          sheet.get_by_text("A person has to finish this application").is_visible()
          and sheet.get_by_text("The form shows a visible CAPTCHA. The portal never solves one: a person has to apply.").is_visible())
    shot(page, "blocked")
    close_sheet(page)

    sheet = open_run(page, "Keystone AI")
    check("failed with outcome unknown: the Admin is told to check by hand and record the outcome",
          sheet.get_by_text("It is not known whether this application went through").is_visible()
          and sheet.get_by_text(re.compile("record the outcome on the job assignment by hand")).is_visible()
          and sheet.get_by_role("button", name="Approve and submit").count() == 0 and sheet.get_by_role("button", name="Cancel run").count() == 0)
    shot(page, "unknown")
    close_sheet(page)

    sheet = open_run(page, "Orbit Cloud")
    shots = sheet.get_by_role("img", name="The employer's page after submitting")
    check("submitted (seeded): the confirmation and its screenshot are shown",
          sheet.get_by_text("The employer's confirmation page was detected.").is_visible() and shots.count() == 1)
    close_sheet(page)

    # Consent block on the tab.
    consent = page.get_by_text("Signed in writing")
    check("consent block: the consent in force with its method and permissions",
          consent.is_visible() and page.get_by_text("May accept privacy notices").is_visible()
          and page.get_by_role("button", name="Revoke").is_visible() and page.get_by_role("button", name="Record new consent").is_visible())
    ctx.close()


def step_queue(browser):
    ctx, page = signed_in(browser, "admin@example.com")
    page.goto(BASE + "/admin/auto-apply")
    page.get_by_role("heading", level=1, name="Auto Apply").wait_for(timeout=T)
    lever = page.locator("li").filter(has=page.locator("[data-run-status]")).filter(has_text="Lumen Labs").first
    lever.wait_for(timeout=T)
    statuses = {s: page.locator(f'[data-run-status="{s}"]').count() for s in ("ready_for_review", "needs_input", "blocked", "failed", "submitted")}
    check("queue: lists what needs attention (ready, needs input, blocked, failed) and not what is done",
          all(statuses[s] for s in ("ready_for_review", "needs_input", "blocked", "failed")) and statuses["submitted"] == 0, str(statuses))
    box = lever.get_by_role("checkbox")
    check("queue batch rule: a ready run cannot be picked before its preview was opened",
          not box.is_enabled() and lever.get_by_text("Open to preview before batch approval").is_visible())
    approve = page.get_by_role("button", name=re.compile("^Approve and submit"))
    check("queue: batch approval is disabled while nothing is picked", not approve.is_enabled())
    no_overflow(page, "queue")
    shot(page, "queue")
    sheet = open_run(page, "Lumen Labs")
    sheet.get_by_role("button", name="Approve and submit").wait_for(timeout=T)
    close_sheet(page)
    check("queue batch rule: after opening the preview the run can be picked", box.is_enabled())
    box.click()

    # A refused run is explained in plain words. Answered in the browser: the real API would approve it.
    run_id = runs_by_company(page, candidate_id(page, "Jordan Rivera"))["Lumen Labs"]["id"]

    def refuse(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"approved": [], "refused": [{"run_id": run_id, "reason": "employer_daily_cap", "detail": "5 applications ..."}]}))
    page.route("**/api/auto-apply/runs/approve-batch", refuse)
    approve.click()
    confirm = page.get_by_role("alertdialog").or_(page.get_by_role("dialog", name="Submit 1 application?")).first
    confirm.wait_for(timeout=T)
    text = confirm.inner_text()
    check("batch dialog: lists the job and the candidate, and that it cannot be undone",
          "Lumen Platform Engineer at Lumen Labs" in text and "Jordan Rivera" in text and "cannot be undone" in text, text[:200])
    confirm.get_by_role("button", name="Submit applications").click()
    toast(page, "Today's limit of applications to this employer is reached")
    check("batch refused (answered in the browser): the refusal code is explained in plain words", True)
    page.unroute("**/api/auto-apply/runs/approve-batch")

    box.click()
    approve.click()
    confirm.wait_for(timeout=T)
    confirm.get_by_role("button", name="Submit applications").click()
    toast(page, "1 application approved and being submitted")
    lever.wait_for(state="detached", timeout=RUN_T)
    for _ in range(RUN_T // 1000):              # the submission runs in the demo API's browser
        if api(page, f"/api/auto-apply/runs/{run_id}").get("status") not in ("approved", "submitting"):
            break
        page.wait_for_timeout(1000)
    got = api(page, f"/api/auto-apply/runs/{run_id}")
    check("batch approval: the run leaves the queue and is submitted", got.get("status") == "submitted", f"{got.get('status')} {got.get('error')}")
    ctx.close()


def step_signals(browser):
    ctx, page = signed_in(browser, "admin@example.com")
    page.goto(BASE + "/admin/dashboard")
    page.get_by_role("button", name=re.compile("^Notifications")).click()
    panel = page.get_by_role("dialog").last
    panel.get_by_text(re.compile("is ready for your review")).first.wait_for(timeout=T)
    text = panel.inner_text()
    check("notifications: ready, needs input, blocked and submitted runs in plain sentences",
          all(s in text for s in ("at Lumen Labs for Jordan Rivera", "is ready for your review", "needs answers before it can be reviewed",
                                  "is blocked and needs a person", "was submitted")), text[:400])
    no_overflow(page, "notifications")
    shot(page, "notifications")
    page.keyboard.press("Escape")

    page.goto(BASE + "/admin/activity?action=auto_apply")
    page.get_by_text("Started preparing automatic applications").first.wait_for(timeout=T)
    text = page.locator("main").inner_text()
    check("audit log, Auto Apply: the contract's actions have plain labels",
          all(s in text for s in ("Started preparing automatic applications", "Approved an automatic application", "Automatic application submitted"))
, text[:300])
    page.goto(BASE + "/admin/activity?action=consent")
    page.get_by_text("Recorded a candidate's consent to auto-apply").first.wait_for(timeout=T)
    page.goto(BASE + "/admin/activity?action=application_answers")
    page.get_by_text("Updated a candidate's application answers").first.wait_for(timeout=T)
    check("audit log: consent and application answers have labels", True)
    no_overflow(page, "audit log")

    page.goto(BASE + "/admin/settings?tab=integrations")
    panel = page.locator("section, div").filter(has=page.get_by_text("Set on the server (AUTO_APPLY_ENABLED")).filter(has_text="Screenshots kept").last
    panel.wait_for(timeout=T)
    text = panel.inner_text()
    check("integrations: the Auto Apply panel shows state, worker, sites and limits",
          all(s in text for s in ("Enabled", "Online", "Greenhouse, Lever, Ashby, Workable", "Per employer per day", "Screenshots kept")), text[:300])
    no_overflow(page, "integrations")
    shot(page, "integrations")
    ctx.close()


def step_dark(browser):
    ctx, page = signed_in(browser, "admin@example.com", dark=True)
    check("dark: the app is in dark mode", page.evaluate("() => document.documentElement.classList.contains('dark')"))
    dialog = open_start_dialog(page, "Jordan Rivera")
    no_overflow(page, "start dialog, dark")
    shot(page, "dark_dialog")
    dialog.get_by_role("button", name="Close").first.click()
    jordan = candidate_id(page, "Jordan Rivera")
    page.goto(BASE + f"/admin/candidates/{jordan}?tab=auto-apply")
    sheet = open_run(page, "Unknown Co")
    bg = page.evaluate("(el) => getComputedStyle(el).backgroundColor", sheet.element_handle())
    check("dark: the run sheet is dark", bg not in ("rgb(255, 255, 255)", "rgba(0, 0, 0, 0)"), bg)
    no_overflow(page, "run sheet, dark")
    shot(page, "dark_sheet")
    close_sheet(page)
    page.goto(BASE + "/admin/auto-apply")
    page.get_by_role("heading", level=1, name="Auto Apply").wait_for(timeout=T)
    page.locator("[data-run-status]").first.wait_for(timeout=T)
    no_overflow(page, "queue, dark")
    shot(page, "dark_queue")
    ctx.close()


# ---------------------------------------------------------------------------------------------------- others
def no_auto_apply(page, who, asked):
    nav = page.get_by_role("link", name="Auto Apply")
    check(f"{who}: no Auto Apply in the navigation", nav.count() == 0)
    page.goto(BASE + "/admin/auto-apply")
    page.wait_for_url(lambda u: "/admin/" not in u, timeout=T)
    check(f"{who}: /admin/auto-apply redirects to their own area", "/admin/" not in page.url, page.url)
    check(f"{who}: no page asked the API about auto-apply runs or consents", asked == [], str(asked[:5]))


def track(page):
    asked = []
    page.on("request", lambda r: asked.append(r.url) if "/api/auto-apply" in r.url or "/consents" in r.url else None)
    return asked


def step_lead(browser):
    ctx, page = signed_in(browser, "lead.alpha@example.com")
    asked = track(page)
    page.goto(BASE + "/tl/candidates")
    page.wait_for_load_state("networkidle")
    open_row_menu(page, "Jordan Rivera")
    check("Team Lead: the row menu has no Auto Apply", page.get_by_role("menuitem").count() > 0 and page.get_by_role("menuitem", name="Auto Apply").count() == 0)
    page.keyboard.press("Escape")
    jordan = candidate_id(page, "Jordan Rivera")
    page.goto(BASE + f"/tl/candidates/{jordan}?tab=auto-apply")
    page.get_by_role("heading", level=1, name="Jordan Rivera").wait_for(timeout=T)
    check("Team Lead: the candidate page has no Auto Apply tab", page.get_by_role("tab", name="Auto Apply").count() == 0)
    page.goto(BASE + f"/tl/candidates/{jordan}?tab=profile")
    form = page.locator("form#application-answers")
    form.wait_for(timeout=T)
    relocate = form.get_by_role("combobox", name="Willing to relocate")
    relocate.scroll_into_view_if_needed()
    check("Team Lead: the Application answers form shows the recorded answers", "No" in relocate.inner_text(), relocate.inner_text())
    relocate.click()
    page.get_by_role("option", name="Yes", exact=True).click()
    form.get_by_label("Portfolio").fill("https://portfolio.example.com/jordan")
    no_overflow(page, "Team Lead, application answers")
    shot(page, "lead_answers")
    form.get_by_role("button", name="Save answers").click()
    toast(page, "Application answers saved")
    page.reload()
    form = page.locator("form#application-answers")
    form.wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    saved = api(page, f"/api/candidates/{jordan}/application-answers")
    check("Team Lead: the answers were saved and are shown after a reload",
          saved.get("willing_to_relocate") == "yes" and saved.get("portfolio_url") == "https://portfolio.example.com/jordan"
          and form.get_by_label("Portfolio").input_value() == "https://portfolio.example.com/jordan"
          and "Yes" in form.get_by_role("combobox", name="Willing to relocate").inner_text(), str({k: saved.get(k) for k in ("willing_to_relocate", "portfolio_url")}))
    page.get_by_role("button", name=re.compile("^Notifications")).click()
    page.wait_for_timeout(500)
    check("Team Lead: no auto-apply notification", "auto" not in page.get_by_role("dialog").last.inner_text().lower())
    page.keyboard.press("Escape")
    no_auto_apply(page, "Team Lead", asked)
    ctx.close()


def step_member(browser):
    ctx, page = signed_in(browser, "maya@example.com")
    asked = track(page)
    page.get_by_role("button", name=re.compile("^Notifications")).click()
    panel = page.get_by_role("dialog").last
    item = panel.get_by_text(re.compile("An administrator submitted the application to Orbit Inference Engineer at Orbit Cloud for Jordan Rivera"))
    item.wait_for(timeout=T)
    text = panel.inner_text()
    check("Member: the application_submitted notification names job, employer and candidate, and nothing about automation",
          "automat" not in text.lower() and "auto apply" not in text.lower() and "auto-apply" not in text.lower(), text[:300])
    no_overflow(page, "member notifications")
    shot(page, "member_notifications")
    page.keyboard.press("Escape")
    jordan = candidate_id(page, "Jordan Rivera")
    page.goto(BASE + f"/member/candidates/{jordan}?tab=auto-apply")
    page.get_by_role("heading", level=1, name="Jordan Rivera").wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    check("Member: the candidate page has no Auto Apply tab and no application answers form",
          page.get_by_role("tab", name="Auto Apply").count() == 0 and page.locator("form#application-answers").count() == 0)
    for path in ("/member/dashboard", "/member/jobs", "/member/applications", f"/member/candidates/{jordan}?tab=activity"):
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        body = page.locator("main").inner_text().lower()
        check(f"Member: {path} says nothing about auto-apply", "auto apply" not in body and "auto-apply" not in body and "automatic application" not in body)
    no_auto_apply(page, "Member", asked)
    ctx.close()


STEPS = [
    ("dialog", step_dialog),
    ("states", step_states),
    ("run", step_run),
    ("queue", step_queue),
    ("signals", step_signals),
    ("dark", step_dark),
    ("lead", step_lead),
    ("member", step_member),
]

with sync_playwright() as p:
    # Nothing in this run may reach a real external site: every host except this machine fails to resolve.
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
