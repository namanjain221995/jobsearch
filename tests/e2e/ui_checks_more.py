"""Second browser check list: the screens that follow what the API states about itself.

Extra profile fields (Admin defines, Team Lead and Admin answer them on a candidate's profile, the responsible
member reads them), password rules from the API's policy, the forgot-password answer, a job a manager took back
from a candidate (410), Reactivate for an account an Admin deactivated, the "deep search by default" setting, and
the notice about outside AI services on a candidate's resume upload.

Members are recruiters with an account; candidates are records. Profiles and resumes belong to candidates and are
kept by Team Leads and Admins; the member responsible for a candidate reads them and works on the candidate's jobs.

    python tests/e2e/ui_checks_more.py --base http://127.0.0.1:3011 --w 1280 --h 900
    python tests/e2e/ui_checks_more.py --base http://127.0.0.1:3011 --w 390 --h 844

It needs the demo stack (see tests/e2e/run.sh, which runs it). Some checks alter an API answer on its way back
to the browser, to prove that a screen shows the API's numbers and words and not constants of its own.

Every step is a named check. Console errors/warnings, page errors and failed /api requests are
collected for the whole run; a step that provokes a failure on purpose declares it (expecting).
"""
import argparse
import json
import re
import sys
import traceback
import uuid
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
TAG = str(A.w)
MOBILE = A.w < 768
# A member of Team Alpha and a candidate that member is responsible for (another pair at the small width).
MEMBER_NAME, MEMBER_EMAIL, CANDIDATE_NAME = ("Maya Chen", "maya@example.com", "Jordan Rivera") if not MOBILE else ("Noah Patel", "noah@example.com", "Samuel Okafor")
T = 30000

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


dev_notices = []


def note(kind, text):
    if any(s in text for s in expected):
        return
    # The Next.js development server says this when the browser cache is off, which it is on every page this
    # script intercepts a request on (page.route). It is about React DevTools, not about the page.
    if kind == "console.warning" and "cache-bypass-in-dev" in text:
        dev_notices.append(text)
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


def login(page, email, password=PASSWORD):
    page.goto(BASE + "/login")
    page.get_by_label("E-mail").fill(email)
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(lambda u: "/login" not in u, timeout=T)
    page.wait_for_load_state("networkidle")


def api(page, path):
    return page.evaluate("async (p) => { const r = await fetch(p, {credentials: 'same-origin'}); return r.ok ? await r.json() : {__status: r.status} }", path)


def no_overflow(page, name):
    overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check(f"no horizontal page overflow: {name}", overflow <= 0, f"{overflow}px")


def shot(page, name):
    if A.shots:
        page.screenshot(path=f"{A.shots}/{A.w}_{name}.png", full_page=True)


def toast(page, text, timeout=T):
    page.locator("[data-sonner-toast]").filter(has_text=text).first.wait_for(timeout=timeout)


def vis(locator):
    return locator.filter(visible=True)


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


def member_id(page, email):
    data = api(page, f"/api/users?role=MEMBER&q={email}&page_size=5")
    return next(u["id"] for u in data["items"] if u["email"] == email)


def candidate_id(page, name):
    data = api(page, f"/api/candidates?q={name.replace(' ', '%20')}&page_size=5")
    return next(c["id"] for c in data["items"] if c["name"] == name)


def send(page, method, path, body=None):
    """A state-changing request as the signed-in person, with the CSRF header the app itself sends. Returns the status."""
    return page.evaluate(
        """async ([method, path, body]) => {
            const csrf = decodeURIComponent((document.cookie.split('; ').find((c) => c.startsWith('jf_csrf=')) || '').slice(8));
            const r = await fetch(path, { method, credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: body === null ? undefined : JSON.stringify(body) });
            return r.status;
        }""", [method, path, body])


def open_nav(page):
    if MOBILE:
        page.get_by_role("button", name="Toggle navigation").click()
        page.get_by_role("dialog").get_by_role("link", name="Dashboard", exact=True).wait_for(timeout=T)


def nav_link(page, name):
    scope = page.get_by_role("dialog") if MOBILE else page.get_by_role("navigation", name="Sections")
    return scope.get_by_role("link", name=name, exact=True)


# ------------------------------------------------------------------------------------------ 1. extra profile fields
L_SHIRT = f"Shirt size {TAG}"
L_RELOC = f"Open to relocation {TAG}"
L_YEARS = f"Years in the US {TAG}"
L_NOTES = f"Notes {TAG}"
L_REMARKS = f"Remarks {TAG}"
K_SHIRT, K_RELOC, K_YEARS, K_NOTES = (f"shirt_size_{TAG}", f"open_to_relocation_{TAG}", f"years_in_the_us_{TAG}", f"notes_{TAG}")


def settings_rows(page):
    return vis(page.locator('[data-slot="extra-field"]'))


def row_labels(page):
    return [row.get_by_label("Label", exact=True).input_value() for row in settings_rows(page).all()]


def save_settings(page):
    with page.expect_response(lambda r: r.url.endswith("/api/settings") and r.request.method == "PATCH", timeout=T) as info:
        page.get_by_role("button", name="Save Settings").click()
    response = info.value
    if response.status == 200:
        toast(page, "Settings saved")
    return response


def open_settings(page):
    page.goto(BASE + "/admin/settings")
    page.get_by_role("heading", name="Extra candidate profile fields").wait_for(timeout=T)
    page.wait_for_load_state("networkidle")


def step_extra_fields(browser):
    print("== 1. extra candidate-profile fields: Admin defines, Team Lead fills on a candidate, Admin edits, the member reads, some are cleared")
    actx = new_context(browser)
    apage = actx.new_page()
    login(apage, "admin@example.com")
    open_settings(apage)

    # start from no fields at all
    removed = 0
    while settings_rows(apage).count() > 0:
        settings_rows(apage).first.get_by_role("button", name=re.compile("^Remove ")).click()
        removed += 1
    if removed:
        response = save_settings(apage)
        check("Admin can remove every field (saved as an empty list)", response.status == 200 and response.json()["profile_extra_fields"] == [],
              f"removed {removed}")
    check("with no fields the settings section says so", apage.get_by_text("No extra fields yet.").is_visible())

    # the Team Lead keeps the candidate's profile; the member responsible for the candidate reads it
    lctx = new_context(browser)
    lpage = lctx.new_page()
    login(lpage, "lead.alpha@example.com")
    cid = candidate_id(lpage, CANDIDATE_NAME)
    profile_api = f"/api/candidates/{cid}/profile"
    # Housekeeping, not a check: an earlier run of this script may have left answers and a seniority. Clear them,
    # so the run can be repeated on the same data.
    left = api(lpage, profile_api)["extra"]
    cleared = send(lpage, "PATCH", profile_api, {"seniority": "", "extra": {k: None for k in left}})
    assert cleared == 200, cleared
    if left:
        print(f"  (cleared {len(left)} answers left by an earlier run: {sorted(left)})")
    lpage.goto(BASE + f"/tl/candidates/{cid}?tab=profile")
    lpage.get_by_role("button", name="Save Profile").wait_for(timeout=T)
    lpage.wait_for_load_state("networkidle")
    me = api(lpage, "/api/auth/me")
    check("no fields defined: the candidate's profile has no 'Additional information' section",
          me["settings"]["profile_extra_fields"] == [] and lpage.get_by_role("group", name="Additional information").count() == 0
          and lpage.get_by_text("Additional information").count() == 0)

    mctx = new_context(browser)
    mpage = mctx.new_page()
    login(mpage, MEMBER_EMAIL)

    def member_reads():
        """The member's read-only profile of the candidate: the 'Additional information' answers as {label: text} (None without the section)."""
        mpage.goto(BASE + f"/member/candidates/{cid}?tab=profile")
        panel = vis(mpage.get_by_role("tabpanel"))
        panel.get_by_text("Desired job titles").wait_for(timeout=T)
        mpage.wait_for_load_state("networkidle")
        heading = panel.get_by_role("heading", name="Additional information")
        if heading.count() == 0:
            return None
        block = panel.locator("div").filter(has=mpage.get_by_role("heading", name="Additional information")).last
        return dict(zip([t.strip() for t in block.locator("dt").all_inner_texts()], [t.strip() for t in block.locator("dd").all_inner_texts()]))

    check("no fields defined: the member's view of the candidate's profile has no 'Additional information' either", member_reads() is None)

    # ---- add, with the API's rules checked before anything is sent
    patches = []
    apage.on("request", lambda r: patches.append(r) if r.method == "PATCH" and r.url.endswith("/api/settings") else None)
    apage.get_by_role("button", name="Add Field").click()
    row = settings_rows(apage).last
    expect(row.get_by_label("Label", exact=True)).to_be_focused(timeout=T)
    check("a new field starts with its label focused", True)
    apage.get_by_role("button", name="Save Settings").click()
    check("a field without a label is refused in the form", row.get_by_role("alert").filter(has_text="Give the field a name of 1 to 60 characters.").is_visible())
    row.get_by_label("Label", exact=True).fill("x" * 61)
    apage.get_by_role("button", name="Save Settings").click()
    check("a label of 61 characters is refused in the form", row.get_by_role("alert").filter(has_text="Give the field a name of 1 to 60 characters.").is_visible())
    row.get_by_label("Label", exact=True).fill(L_SHIRT)
    check("a new field shows the key made from its label", row.get_by_text(K_SHIRT).is_visible() and row.get_by_text("set when you save").is_visible(),
          row.locator("code").inner_text())
    row.get_by_role("combobox", name="Type").click()
    apage.get_by_role("option", name="Choice from a list", exact=True).click()
    apage.get_by_role("button", name="Save Settings").click()
    check("a choice without options is refused in the form", row.get_by_role("alert").filter(has_text="A choice needs at least two options.").is_visible())
    row.get_by_label("Options", exact=True).fill("Small")
    apage.get_by_role("button", name="Save Settings").click()
    check("a choice with one option is refused in the form", row.get_by_role("alert").filter(has_text="A choice needs at least two options.").is_visible())
    row.get_by_label("Options", exact=True).fill("\n".join(f"Option {n}" for n in range(21)))
    apage.get_by_role("button", name="Save Settings").click()
    check("a choice with 21 options is refused in the form", row.get_by_role("alert").filter(has_text="A choice can have at most 20 options.").is_visible())
    row.get_by_label("Options", exact=True).fill("Small\n" + "y" * 61)
    apage.get_by_role("button", name="Save Settings").click()
    check("an option of 61 characters is refused in the form", row.get_by_role("alert").filter(has_text="Each option needs 1 to 60 characters.").is_visible())
    check("nothing was sent while the form had problems", len(patches) == 0, f"{len(patches)} requests")
    row.get_by_label("Options", exact=True).fill("Small\nMedium\n\n  Large  \nMedium")

    def add(label, kind):
        apage.get_by_role("button", name="Add Field").click()
        new_row = settings_rows(apage).last
        new_row.get_by_label("Label", exact=True).fill(label)
        if kind != "Text":
            new_row.get_by_role("combobox", name="Type").click()
            apage.get_by_role("option", name=kind, exact=True).click()
        return new_row

    add(L_RELOC, "Yes or no")
    add(L_YEARS, "Number")
    add(L_NOTES, "Text")
    add(f"Temp {TAG}", "Text")
    check("four kinds of field can be added", row_labels(apage) == [L_SHIRT, L_RELOC, L_YEARS, L_NOTES, f"Temp {TAG}"], str(row_labels(apage)))

    # ---- reorder
    apage.get_by_role("button", name=f"Move {L_NOTES} up").click()
    check("a field can be moved up", row_labels(apage) == [L_SHIRT, L_RELOC, L_NOTES, L_YEARS, f"Temp {TAG}"], str(row_labels(apage)))
    focused = apage.evaluate("document.activeElement && document.activeElement.getAttribute('aria-label')")
    check("the move button keeps the focus after moving up", focused == f"Move {L_NOTES} up", str(focused))
    apage.get_by_role("button", name=f"Move {L_SHIRT} down").click()
    check("a field can be moved down", row_labels(apage) == [L_RELOC, L_SHIRT, L_NOTES, L_YEARS, f"Temp {TAG}"], str(row_labels(apage)))
    focused = apage.evaluate("document.activeElement && document.activeElement.getAttribute('aria-label')")
    check("the move button keeps the focus after moving down", focused == f"Move {L_SHIRT} down", str(focused))
    first_up = settings_rows(apage).first.get_by_role("button", name=re.compile(" up$"))
    last_down = settings_rows(apage).last.get_by_role("button", name=re.compile(" down$"))
    check("the first field cannot move up and the last cannot move down", first_up.is_disabled() and last_down.is_disabled())
    no_overflow(apage, "admin settings with five fields being edited")
    shot(apage, "1_settings_editing")

    response = save_settings(apage)
    sent = response.request.post_data_json["profile_extra_fields"]
    want = [
        {"key": K_RELOC, "label": L_RELOC, "type": "yes_no", "options": []},
        {"key": K_SHIRT, "label": L_SHIRT, "type": "choice", "options": ["Small", "Medium", "Large"]},
        {"key": K_NOTES, "label": L_NOTES, "type": "text", "options": []},
        {"key": K_YEARS, "label": L_YEARS, "type": "number", "options": []},
        {"key": f"temp_{TAG}", "label": f"Temp {TAG}", "type": "text", "options": []},
    ]
    check("the fields are saved in order, with keys made from the labels (lower-case, underscores)", response.status == 200 and sent == want,
          json.dumps(sent)[:300])
    check("the API stored exactly what was sent", response.json()["profile_extra_fields"] == want)
    apage.wait_for_load_state("networkidle")
    # The form takes the saved values once the screen has reloaded what the settings feed (a moment after the answer).
    # A toast of the save before this one can still be on screen, so the toast alone does not tell.
    expect(apage.get_by_text("set when you save")).to_have_count(0, timeout=T)
    check("after saving, the key is no longer provisional", settings_rows(apage).nth(1).get_by_text(K_SHIRT).is_visible(),
          settings_rows(apage).nth(1).locator("p").first.inner_text().replace("\n", " "))
    expect(apage.get_by_role("button", name="Save Settings")).to_be_disabled(timeout=T)
    check("after saving there is nothing left to save", True)

    # ---- edit: a rename keeps the key; remove
    notes_row = settings_rows(apage).nth(2)
    notes_row.get_by_label("Label", exact=True).fill(L_REMARKS)
    check("renaming an existing field does not change the key shown", notes_row.get_by_text(K_NOTES, exact=True).is_visible(), notes_row.locator("code").inner_text())
    apage.get_by_role("button", name=f"Remove Temp {TAG}").click()
    expect(apage.get_by_role("button", name="Add Field")).to_be_focused(timeout=T)
    check("removing a field moves the focus to Add Field", True)
    response = save_settings(apage)
    sent = response.request.post_data_json["profile_extra_fields"]
    want = [
        {"key": K_RELOC, "label": L_RELOC, "type": "yes_no", "options": []},
        {"key": K_SHIRT, "label": L_SHIRT, "type": "choice", "options": ["Small", "Medium", "Large"]},
        {"key": K_NOTES, "label": L_REMARKS, "type": "text", "options": []},
        {"key": K_YEARS, "label": L_YEARS, "type": "number", "options": []},
    ]
    check("a renamed field keeps its key, and a removed field is gone", response.status == 200 and sent == want, json.dumps(sent)[:300])
    apage.reload()
    apage.get_by_role("heading", name="Extra candidate profile fields").wait_for(timeout=T)
    settings_rows(apage).first.wait_for(timeout=T)
    check("the saved fields come back after a reload, in order", row_labels(apage) == [L_RELOC, L_SHIRT, L_REMARKS, L_YEARS], str(row_labels(apage)))
    check("the choice shows its options one per line", settings_rows(apage).nth(1).get_by_label("Options", exact=True).input_value() == "Small\nMedium\nLarge")
    no_overflow(apage, "admin settings with four saved fields")
    shot(apage, "1_settings_saved")

    # ---- the Team Lead fills them in on the candidate's profile
    lpage.goto(BASE + f"/tl/candidates/{cid}?tab=profile")
    group = lpage.get_by_role("group", name="Additional information")
    group.wait_for(timeout=T)
    lpage.wait_for_load_state("networkidle")
    shirt = group.get_by_role("combobox", name=L_SHIRT)
    reloc = group.get_by_role("radiogroup", name=L_RELOC)
    years = group.get_by_label(L_YEARS, exact=True)
    remarks = group.get_by_label(L_REMARKS, exact=True)
    kinds_ok = (shirt.is_visible() and reloc.get_by_role("radio", name="Yes").is_visible() and reloc.get_by_role("radio", name="No").is_visible()
                and years.get_attribute("inputmode") == "decimal" and remarks.get_attribute("inputmode") is None)
    check("the candidate's profile has one control per field: select, yes/no, number input, text input", kinds_ok)
    order = group.evaluate("""(el, labels) => { const text = el.innerText; return labels.map((l) => text.indexOf(l)); }""", [L_RELOC, L_SHIRT, L_REMARKS, L_YEARS])
    check("the fields are in the Admin's order", all(i >= 0 for i in order) and order == sorted(order), str(order))
    check("nothing is answered yet", shirt.inner_text().strip() == "No answer" and reloc.get_by_role("radio", checked=True).count() == 0
          and years.input_value() == "" and remarks.input_value() == "")
    shirt.click()
    options = [o.inner_text().strip() for o in lpage.get_by_role("option").all()]
    check("the select offers the Admin's options and a way to give no answer", options == ["No answer", "Small", "Medium", "Large"], str(options))
    lpage.get_by_role("option", name="Medium", exact=True).click()
    reloc.get_by_role("radio", name="Yes").click()
    remarks.fill("  Prefers   mornings ")
    years.fill("abc")
    profile_patches = []
    lpage.on("request", lambda r: profile_patches.append(r) if r.method == "PATCH" and "/profile" in r.url else None)
    lpage.get_by_role("button", name="Save Profile").click()
    check("a number field refuses text before anything is sent",
          group.get_by_role("alert").filter(has_text="Enter a number, such as 3 or 2.5.").is_visible() and len(profile_patches) == 0)
    years.fill("7.5")
    lpage.get_by_label("Seniority").fill("Senior")
    with lpage.expect_response(lambda r: r.url.endswith(profile_api) and r.request.method == "PATCH", timeout=T) as info:
        lpage.get_by_role("button", name="Save Profile").click()
    body = info.value.request.post_data_json
    check("extra answers are saved together with the rest of the candidate's profile, changed keys only",
          info.value.status == 200 and body == {"seniority": "Senior", "extra": {K_SHIRT: "Medium", K_RELOC: True, K_NOTES: "Prefers mornings", K_YEARS: 7.5}},
          json.dumps(body))
    toast(lpage, "Profile saved")
    stored = info.value.json()["extra"]
    check("the API stored the four answers in their types, on the candidate", info.value.json()["candidate_id"] == cid
          and {k: stored.get(k) for k in (K_SHIRT, K_RELOC, K_NOTES, K_YEARS)} == {K_SHIRT: "Medium", K_RELOC: True, K_NOTES: "Prefers mornings", K_YEARS: 7.5}, json.dumps(stored))
    no_overflow(lpage, "candidate profile with answers")
    shot(lpage, "1_candidate_profile_filled")
    lpage.reload()
    group.wait_for(timeout=T)
    lpage.wait_for_load_state("networkidle")
    check("the answers are shown again after a reload",
          shirt.inner_text().strip() == "Medium" and reloc.get_by_role("radio", name="Yes").get_attribute("aria-checked") == "true"
          and years.input_value() == "7.5" and remarks.input_value() == "Prefers mornings")
    check("nothing to save right after loading", lpage.get_by_role("button", name="Save Profile").is_disabled())
    remarks.fill("Prefers evenings")
    with lpage.expect_response(lambda r: r.url.endswith(profile_api) and r.request.method == "PATCH", timeout=T) as info:
        lpage.get_by_role("button", name="Save Profile").click()
    check("changing one answer sends that key only", info.value.request.post_data_json == {"extra": {K_NOTES: "Prefers evenings"}},
          json.dumps(info.value.request.post_data_json))
    toast(lpage, "Profile saved")

    # ---- the responsible member reads them, with their labels, and can change nothing
    answers = member_reads()
    check("the member sees the same fields with the answers, in the Admin's order, as plain text",
          answers is not None and list(answers.items()) == [(L_RELOC, "Yes"), (L_SHIRT, "Medium"), (L_REMARKS, "Prefers evenings"), (L_YEARS, "7.5")], str(answers))
    panel = vis(mpage.get_by_role("tabpanel"))
    fields = sum(panel.get_by_role(role).count() for role in ("textbox", "combobox", "radio", "checkbox", "spinbutton", "switch"))
    check("the member's view has no controls: nothing to fill in, nothing to save",
          fields == 0 and mpage.get_by_role("button", name="Save Profile").count() == 0 and mpage.get_by_role("button", name=re.compile("^Clear the answer")).count() == 0, f"{fields} fields")
    with expecting("403", profile_api):
        refused = send(mpage, "PATCH", profile_api, {"extra": {K_NOTES: "changed by the member"}})
        mpage.wait_for_timeout(300)
    check("the API refuses a member who tries to change an answer (403), and the answer stays", refused == 403 and api(mpage, profile_api)["extra"].get(K_NOTES) == "Prefers evenings", f"HTTP {refused}")
    no_overflow(mpage, "member's view of a candidate profile with extra answers")
    shot(mpage, "1_member_reads_candidate_profile")

    # ---- the Admin sees them too, and can edit
    apage.goto(BASE + f"/admin/candidates/{cid}?tab=profile")
    agroup = apage.get_by_role("group", name="Additional information")
    agroup.wait_for(timeout=T)
    apage.wait_for_load_state("networkidle")
    check("the Admin sees the same fields with the answers the Team Lead gave",
          agroup.get_by_role("combobox", name=L_SHIRT).inner_text().strip() == "Medium"
          and agroup.get_by_role("radiogroup", name=L_RELOC).get_by_role("radio", name="Yes").get_attribute("aria-checked") == "true"
          and agroup.get_by_label(L_YEARS, exact=True).input_value() == "7.5"
          and agroup.get_by_label(L_REMARKS, exact=True).input_value() == "Prefers evenings")
    agroup.get_by_label(L_YEARS, exact=True).fill("8")
    agroup.get_by_label(L_REMARKS, exact=True).fill("Prefers evenings, not weekends")
    with apage.expect_response(lambda r: r.url.endswith(profile_api) and r.request.method == "PATCH", timeout=T) as info:
        apage.get_by_role("button", name="Save Profile").click()
    check("the Admin can change answers for the candidate (those keys only)", info.value.status == 200
          and info.value.request.post_data_json == {"extra": {K_NOTES: "Prefers evenings, not weekends", K_YEARS: 8}}, json.dumps(info.value.request.post_data_json))
    toast(apage, "Profile saved")
    no_overflow(apage, "Admin candidate detail, profile tab")
    answers = member_reads()
    check("the member reads what the Admin changed", answers is not None and answers.get(L_YEARS) == "8" and answers.get(L_REMARKS) == "Prefers evenings, not weekends", str(answers))

    # ---- the Team Lead clears answers
    lpage.reload()
    group.wait_for(timeout=T)
    lpage.wait_for_load_state("networkidle")
    check("the Team Lead sees what the Admin changed", years.input_value() == "8" and remarks.input_value() == "Prefers evenings, not weekends")
    group.get_by_role("button", name=f"Clear the answer to {L_RELOC}").click()
    remarks.fill("")
    shirt.click()
    lpage.get_by_role("option", name="No answer", exact=True).click()
    with lpage.expect_response(lambda r: r.url.endswith(profile_api) and r.request.method == "PATCH", timeout=T) as info:
        lpage.get_by_role("button", name="Save Profile").click()
    body = info.value.request.post_data_json
    check("cleared answers are sent as null, the untouched one is not sent", body == {"extra": {K_RELOC: None, K_SHIRT: None, K_NOTES: None}}, json.dumps(body))
    toast(lpage, "Profile saved")
    stored = api(lpage, profile_api)["extra"]
    check("the API no longer has the cleared answers and kept the other", K_RELOC not in stored and K_SHIRT not in stored and K_NOTES not in stored and stored.get(K_YEARS) == 8,
          json.dumps(stored))
    lpage.reload()
    group.wait_for(timeout=T)
    lpage.wait_for_load_state("networkidle")
    check("cleared answers are shown as not answered after a reload",
          shirt.inner_text().strip() == "No answer" and reloc.get_by_role("radio", checked=True).count() == 0 and remarks.input_value() == ""
          and years.input_value() == "8" and group.get_by_role("button", name=re.compile("^Clear")).count() == 0)
    reloc.get_by_role("radio", name="No").click()
    reloc.get_by_role("radio", name="No").click()
    check("pressing the chosen answer again takes it back", reloc.get_by_role("radio", checked=True).count() == 0
          and lpage.get_by_role("button", name="Save Profile").is_disabled())
    shot(lpage, "1_candidate_profile_cleared")
    answers = member_reads()
    check("the member reads the cleared answers as 'No answer'",
          answers == {L_RELOC: "No answer", L_SHIRT: "No answer", L_REMARKS: "No answer", L_YEARS: "8"}, str(answers))
    mctx.close()

    # ---- a wrong value: the API's 422 sentence is shown (the form is given an option the server does not have)
    def add_option(body):
        for field in body["settings"]["profile_extra_fields"]:
            if field["key"] == K_SHIRT:
                field["options"] = field["options"] + ["Extra large"]
    alter_json(lpage, "**/api/auth/me", add_option)
    lpage.reload()
    group.wait_for(timeout=T)
    lpage.wait_for_load_state("networkidle")
    shirt.click()
    lpage.get_by_role("option", name="Extra large", exact=True).click()
    with expecting("422", "/profile"):
        with lpage.expect_response(lambda r: r.url.endswith(profile_api) and r.request.method == "PATCH", timeout=T) as info:
            lpage.get_by_role("button", name="Save Profile").click()
        refusal = info.value.json().get("error", "")
        alert = vis(lpage.get_by_role("alert").filter(has_text=refusal)).first
        alert.wait_for(timeout=T)
    check("a value the API refuses (422) shows the API's own sentence", info.value.status == 422 and refusal == f"{L_SHIRT}: choose one of the listed options", refusal)
    # The page asks for /api/auth/me again after the refusal: let a handler that is still at work finish quietly.
    lpage.unroute_all(behavior="ignoreErrors")
    lctx.close()
    actx.close()


# ------------------------------------------------------------------------------------------ 2. password rules
def step_password(browser):
    print("== 2. password rules: reset screen from the public policy, change-password from the signed-in user's settings")
    ctx = new_context(browser)
    page = ctx.new_page()
    with page.expect_response(lambda r: "/api/auth/password-policy" in r.url, timeout=T) as info:
        page.goto(BASE + "/reset-password?token=" + "t" * 32)
    policy = info.value.json()
    page.get_by_role("heading", level=1, name="Choose a new password").wait_for(timeout=T)
    field = page.get_by_label("New password", exact=True)
    check("the public policy is asked for and answers with the three numbers", info.value.status == 200
          and set(policy) == {"min_length", "max_length", "character_kinds_required"}, json.dumps(policy))
    check("reset screen: the rule list shows the minimum length before anything is typed",
          field.input_value() == "" and page.get_by_text(f"At least {policy['min_length']} characters").is_visible()
          and page.get_by_text(f"At least {policy['character_kinds_required']} of these 4 kinds of character (0 so far)").is_visible())
    field.fill("Ab1!")
    page.get_by_label("Repeat the new password").fill("Ab1!")
    posts = []
    page.on("request", lambda r: posts.append(r) if r.method == "POST" and "reset-password" in r.url else None)
    page.get_by_role("button", name="Save new password").click()
    check("reset screen: a password shorter than the minimum is refused before sending",
          page.get_by_role("alert").filter(has_text=f"Use at least {policy['min_length']} characters.").is_visible() and len(posts) == 0)
    no_overflow(page, "reset password")
    shot(page, "2_reset_password")
    ctx.close()

    # the numbers on the screen are the policy's, not constants
    ctx = new_context(browser)
    page = ctx.new_page()
    alter_json(page, "**/api/auth/password-policy", lambda body: body.update(min_length=12, character_kinds_required=4))
    page.goto(BASE + "/reset-password?token=" + "t" * 32)
    page.get_by_role("heading", level=1, name="Choose a new password").wait_for(timeout=T)
    check("reset screen follows the policy's numbers (answer altered to 12 and 4 on the way back)",
          page.get_by_text("At least 12 characters").is_visible() and page.get_by_text("At least 4 of these 4 kinds of character").is_visible())
    page.get_by_label("New password", exact=True).fill("Abcdefghijk1")
    page.get_by_label("Repeat the new password").fill("Abcdefghijk1")
    page.get_by_role("button", name="Save new password").click()
    check("reset screen checks against the policy's numbers", page.get_by_role("alert").filter(has_text="Use at least four of:").is_visible())
    ctx.close()

    # the policy cannot be loaded: the form still works, without the length rule
    ctx = new_context(browser)
    page = ctx.new_page()
    page.route("**/api/auth/password-policy", lambda route: route.fulfill(status=503, content_type="application/json", body='{"error":"down","detail":""}'))
    with expecting("503", "password-policy"):
        page.goto(BASE + "/reset-password?token=" + "t" * 32)
        page.get_by_role("heading", level=1, name="Choose a new password").wait_for(timeout=T)
        page.wait_for_load_state("networkidle")
    check("reset screen without a policy still offers the form, with the character rule only",
          page.get_by_text("At least 3 of these 4 kinds of character").is_visible() and page.get_by_text(re.compile(r"At least \d+ characters")).count() == 0)
    ctx.close()

    # a real reset link, made by the Team Lead (no mail server on the demo stack), sets a password
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com")
    page.goto(BASE + "/tl/members")
    vis(page.get_by_role("button", name="Actions for Omar Haddad")).first.click()
    page.get_by_role("menuitem", name="Create Reset Link").click()
    dialog = page.get_by_role("dialog", name="Password reset link")
    dialog.get_by_role("button", name="Create Reset Link").click()
    reset_url = dialog.get_by_label("Reset link").input_value(timeout=T)
    ctx.close()
    ctx = new_context(browser)
    page = ctx.new_page()
    page.goto(re.sub(r"^https?://[^/]+", BASE, reset_url))
    page.get_by_role("heading", level=1, name="Choose a new password").wait_for(timeout=T)
    check("a real reset link opens the form with the minimum length listed", page.get_by_text("At least 10 characters").is_visible())
    page.get_by_label("New password", exact=True).fill(PASSWORD)
    expect(page.get_by_text("At least 10 characters (met)")).to_be_visible(timeout=T)
    page.get_by_label("Repeat the new password").fill(PASSWORD)
    page.get_by_role("button", name="Save new password").click()
    page.get_by_role("heading", name="Password updated").wait_for(timeout=T)
    check("the reset link sets the password", page.get_by_text("Password updated. Sign in with your new password.").is_visible())
    ctx.close()

    # change-password, signed in: from me.settings.password_min_length
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "omar@example.com")
    check("sign-in works with the password set through the reset link", "/member/dashboard" in page.url, page.url)
    page.goto(BASE + "/member/settings")
    page.get_by_label("Current password").wait_for(timeout=T)
    me = api(page, "/api/auth/me")
    check("change-password (member): the rule list shows the minimum length before anything is typed",
          page.get_by_label("New password", exact=True).input_value() == ""
          and page.get_by_text(f"At least {me['settings']['password_min_length']} characters").is_visible(), str(me["settings"]["password_min_length"]))
    posts = []
    page.on("request", lambda r: posts.append(r) if r.method == "POST" and "change-password" in r.url else None)
    page.get_by_label("Current password").fill(PASSWORD)
    page.get_by_label("New password", exact=True).fill("Ab1!")
    page.get_by_label("Repeat the new password").fill("Ab1!")
    page.get_by_role("button", name="Change Password").click()
    check("change-password: a password shorter than the minimum is refused before sending",
          page.get_by_role("alert").filter(has_text="Use at least 10 characters.").is_visible() and len(posts) == 0)
    shot(page, "2_change_password")
    ctx.close()

    ctx = new_context(browser)
    page = ctx.new_page()
    alter_json(page, "**/api/auth/me", lambda body: body["settings"].update(password_min_length=14))
    login(page, "omar@example.com")
    page.goto(BASE + "/member/settings")
    page.get_by_label("Current password").wait_for(timeout=T)
    check("change-password follows me.settings.password_min_length (answer altered to 14 on the way back)", page.get_by_text("At least 14 characters").is_visible())
    ctx.close()

    for email, path, who in (("lead.alpha@example.com", "/tl/settings", "Team Lead"), ("admin@example.com", "/admin/settings?tab=account", "Admin")):
        ctx = new_context(browser)
        page = ctx.new_page()
        login(page, email)
        page.goto(BASE + path)
        page.get_by_label("Current password").wait_for(timeout=T)
        check(f"change-password ({who}): the minimum length is listed", page.get_by_text("At least 10 characters").is_visible())
        no_overflow(page, f"{who} account settings")
        ctx.close()

    # the invitation screen was left as it is: it still gets the length from the invitation itself
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com")
    created = page.evaluate(
        """async (email) => {
            const csrf = decodeURIComponent((document.cookie.split('; ').find((c) => c.startsWith('jf_csrf=')) || '').slice(8));
            const r = await fetch('/api/invitations', { method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
                body: JSON.stringify({ name: 'Invited For A Check', email, role: 'MEMBER' }) });
            return { status: r.status, body: await r.json() };
        }""",
        f"invited.{uuid.uuid4().hex[:10]}@example.com",
    )
    ctx.close()
    assert created["status"] == 201, created
    ctx = new_context(browser)
    page = ctx.new_page()
    page.goto(re.sub(r"^https?://[^/]+", BASE, created["body"]["invite_url"]))
    page.get_by_role("heading", level=1, name="Welcome, Invited For A Check").wait_for(timeout=T)
    check("invitation screen (not changed): still lists the minimum length from the invitation", page.get_by_text("At least 10 characters").is_visible()
          and page.get_by_text("At least 3 of these 4 kinds of character").is_visible())
    ctx.close()


# ------------------------------------------------------------------------------------------ 3. forgot password
def step_forgot(browser):
    print("== 3. forgot password: the server's own message, nothing fixed")
    ctx = new_context(browser)
    page = ctx.new_page()
    page.goto(BASE + "/forgot-password")
    page.get_by_label("E-mail").fill("nobody@example.com")
    with page.expect_response(lambda r: r.url.endswith("/api/auth/forgot-password"), timeout=T) as info:
        page.get_by_role("button", name="Send reset link").click()
    message = info.value.json()["message"]
    status = vis(page.get_by_role("status")).first
    status.wait_for(timeout=T)
    check("the answer shown is the server's message, word for word", status.inner_text().strip() == message, message)
    main = page.locator("main").inner_text()
    check("no fixed sentence claims that a mail was sent",
          "Check your inbox" not in main and "No message after a few minutes" not in main and "spam" not in main and "we will send" not in main, main.replace("\n", " | ")[:200])
    no_overflow(page, "forgot password, answered")
    shot(page, "3_forgot_password")
    page.get_by_role("button", name="Use another address").click()
    check("the form before sending promises no e-mail either", "we will send" not in page.locator("main").inner_text()
          and page.get_by_role("button", name="Send reset link").is_visible())
    ctx.close()

    ctx = new_context(browser)
    page = ctx.new_page()
    other = "If an account exists for this address, a password reset link is being e-mailed to it."
    alter_json(page, "**/api/auth/forgot-password", lambda body: body.update(message=other))
    page.goto(BASE + "/forgot-password")
    page.get_by_label("E-mail").fill("maya@example.com")
    page.get_by_role("button", name="Send reset link").click()
    page.get_by_role("button", name="Use another address").wait_for(timeout=T)      # the spinner in the button is a "status" too
    status = vis(page.get_by_role("status")).first
    status.wait_for(timeout=T)
    check("another server message (answer altered on the way back) is shown just the same", status.inner_text().strip() == other, status.inner_text().strip())
    ctx.close()


# ------------------------------------------------------------------------------------------ 4. withdrawn job
def withdraw_as_lead(browser, mid, job):
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com")
    page.goto(BASE + f"/tl/assignments?member={mid}&q={job['company'].replace(' ', '+')}")
    row = vis(page.locator("table button, li button")).filter(has_text=re.compile(r"^" + re.escape(job["title"]) + r"$")).first
    row.wait_for(timeout=T)
    row.click()
    drawer = page.get_by_role("dialog")
    drawer.get_by_role("button", name="Withdraw").click()
    confirm = page.get_by_role("alertdialog", name="Withdraw this assignment?")
    confirm.get_by_role("button", name="Withdraw assignment").click()
    toast(page, "Assignment withdrawn. Its history is kept.")
    confirm.wait_for(state="hidden", timeout=T)
    ctx.close()


def restore_withdrawn(browser, mid):
    """Housekeeping, not a check: assign the withdrawn jobs again (the API restores them), so the run can be repeated."""
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com")
    data = api(page, f"/api/assignments?member_id={mid}&include_inactive=true&page_size=200")
    withdrawn = [a for a in data["items"] if not a["is_active"]]
    for a in withdrawn:
        status = send(page, "POST", "/api/assignments", {"job_id": a["job"]["id"], "candidate_id": a["candidate"]["id"]})
        assert status == 201, status
    ctx.close()
    return len(withdrawn)


# The withdrawn-job checks need five open jobs of one candidate: Jordan Rivera (Maya Chen's) has them in the demo data
# at both widths; Samuel Okafor has four jobs in all.
W_EMAIL, W_CANDIDATE = "maya@example.com", "Jordan Rivera"


def step_withdrawn(browser):
    print("== 4. a job withdrawn from the member's candidate: the API's 410 text with a way back; 403 stays as it was")
    mctx = new_context(browser)
    page = mctx.new_page()
    login(page, W_EMAIL)
    me = api(page, "/api/auth/me")
    restored = restore_withdrawn(browser, me["id"])
    if restored:
        print(f"  (restored {restored} assignments withdrawn by an earlier run)")
    cid = candidate_id(page, W_CANDIDATE)
    items = api(page, f"/api/assignments?candidate_id={cid}&page_size=100")["items"]
    open_ones = [a for a in items if a["status"] in ("ASSIGNED", "VIEWED") and a["job"].get("has_apply_link", True)]
    check("the member's candidate has five open jobs to try this with", len(open_ones) >= 5, f"{len(open_ones)} of {len(items)}")
    check("each of them names the candidate and the responsible member", all(a["candidate"]["name"] == W_CANDIDATE and a["member"]["id"] == me["id"] for a in open_ones))
    jobs = [{"id": a["id"], "title": a["job"]["title"], "company": a["job"]["company"]} for a in open_ones[:5]]
    gone = "This job is no longer assigned to your candidate."
    back = "Go to My Candidates"

    def gone_state(p):
        state = vis(p.locator('[data-slot="error-state"]')).first
        state.wait_for(timeout=T)
        p.wait_for_load_state("networkidle")
        return state

    # (a) opened after it was withdrawn
    withdraw_as_lead(browser, me["id"], jobs[0])
    with expecting("410", f"/api/assignments/{jobs[0]['id']}"):
        with page.expect_response(lambda r: r.url.endswith(f"/api/assignments/{jobs[0]['id']}"), timeout=T) as info:
            page.goto(BASE + f"/member/jobs/{jobs[0]['id']}")
        state = gone_state(page)
    check("the API answers 410 with its sentence", info.value.status == 410 and info.value.json().get("error") == gone, str(info.value.status))
    check("the page shows the API's 410 text", state.get_by_text(gone, exact=True).is_visible(), state.inner_text().replace("\n", " | "))
    check("with a link back to the member's candidates", state.get_by_role("link", name=back).get_attribute("href") == "/member/candidates")
    check("and neither the 403 words nor a retry button", state.get_by_text("This job is not on your candidates' lists").count() == 0
          and state.get_by_role("button", name="Try again").count() == 0 and state.get_attribute("data-gone") == "true")
    check("the old wording ('assigned to you', 'My Jobs') is gone", state.get_by_text("This job is no longer assigned to you.", exact=True).count() == 0
          and page.get_by_role("link", name="Go to My Jobs").count() == 0)
    no_overflow(page, "member job that was withdrawn (410)")
    shot(page, "4_withdrawn_410")
    state.get_by_role("link", name=back).click()
    page.wait_for_url(re.compile(r"/member/candidates$"), timeout=T)
    vis(page.locator('[data-slot="candidate-card"]')).first.wait_for(timeout=T)
    check("the link leads to My Candidates", vis(page.locator('[data-slot="candidate-card"]').filter(has_text=W_CANDIDATE)).count() == 1)
    page.goto(BASE + f"/member/candidates/{cid}?page_size=100")
    vis(page.locator('[data-slot="job-card"]')).first.wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    check("the withdrawn job is no longer among the candidate's jobs", vis(page.locator(f'a[href^="/member/jobs/{jobs[0]["id"]}"]')).count() == 0
          and vis(page.locator(f'a[href^="/member/jobs/{jobs[1]["id"]}"]')).count() == 1)

    # (a2) opened through a link from the candidate's page: the way back to that candidate is offered as well
    withdraw_as_lead(browser, me["id"], jobs[4])
    with expecting("410", f"/api/assignments/{jobs[4]['id']}"):
        page.goto(BASE + f"/member/jobs/{jobs[4]['id']}?candidate={cid}")
        state = gone_state(page)
    check("a withdrawn job opened from the candidate's page: the API's text, with the way back to that candidate and to My Candidates",
          state.get_by_text(gone, exact=True).is_visible() and state.get_by_role("link", name="Back to the candidate").get_attribute("href") == f"/member/candidates/{cid}"
          and state.get_by_role("link", name=back).is_visible())

    # (b) withdrawn while the member has the job open: a status change is answered 410
    page.goto(BASE + f"/member/jobs/{jobs[1]['id']}")
    page.get_by_role("heading", level=1, name=jobs[1]["title"]).wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    withdraw_as_lead(browser, me["id"], jobs[1])
    vis(page.get_by_role("button", name="Change Status")).first.click()
    page.get_by_role("menuitem", name=re.compile("^Applying")).click()
    dialog = page.get_by_role("dialog", name="Change status to Applying")
    with expecting("410", f"/api/assignments/{jobs[1]['id']}"):
        with page.expect_response(lambda r: r.url.endswith(f"/api/assignments/{jobs[1]['id']}/status"), timeout=T) as info:
            dialog.get_by_role("button", name="Change to Applying").click()
        state = gone_state(page)
    check("a status change on a job withdrawn meanwhile: 410, and the page turns into the API's text with the link",
          info.value.status == 410 and state.get_by_text(gone, exact=True).is_visible() and state.get_by_role("link", name=back).is_visible()
          and page.get_by_role("dialog").count() == 0)

    # (c) the same for a note
    page.goto(BASE + f"/member/jobs/{jobs[2]['id']}")
    page.get_by_role("heading", level=1, name=jobs[2]["title"]).wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    withdraw_as_lead(browser, me["id"], jobs[2])
    vis(page.get_by_label("Add a note")).first.fill("Too late")
    with expecting("410", f"/api/assignments/{jobs[2]['id']}"):
        with page.expect_response(lambda r: r.url.endswith(f"/api/assignments/{jobs[2]['id']}/notes"), timeout=T) as info:
            vis(page.get_by_role("button", name="Add Note")).first.click()
        state = gone_state(page)
    check("a note on a job withdrawn meanwhile: 410, and the page turns into the API's text with the link",
          info.value.status == 410 and state.get_by_text(gone, exact=True).is_visible() and state.get_by_role("link", name=back).is_visible())

    # (d) the same for Open Job: the new tab gets the API's own page, this one follows
    page.goto(BASE + f"/member/jobs/{jobs[3]['id']}")
    page.get_by_role("heading", level=1, name=jobs[3]["title"]).wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    withdraw_as_lead(browser, me["id"], jobs[3])
    with expecting("410", f"/api/assignments/{jobs[3]['id']}"):
        with page.expect_popup(timeout=T) as popup_info:
            vis(page.get_by_role("button", name=re.compile("^Open Job"))).first.click()
        popup = popup_info.value
        popup.wait_for_load_state("load")
        popup_text = popup.locator("body").inner_text()
        state = gone_state(page)
        popup.close()
    check("Open Job on a job withdrawn meanwhile: the new tab says so, and the page turns into the API's text with the link",
          gone in popup_text and state.get_by_text(gone, exact=True).is_visible() and state.get_by_role("link", name=back).is_visible(),
          popup_text.replace("\n", " | ")[:120])

    # (e) a job that is not theirs: 403, handled as before
    stranger = str(uuid.uuid4())
    with expecting("403", f"/api/assignments/{stranger}"):
        with page.expect_response(lambda r: r.url.endswith(f"/api/assignments/{stranger}"), timeout=T) as info:
            page.goto(BASE + f"/member/jobs/{stranger}")
        state = gone_state(page)
    check("a job that is not one of the member's candidates' is still answered 403 and shown as 'not on your candidates' lists' with the link",
          info.value.status == 403 and state.get_by_text("This job is not on your candidates' lists").is_visible()
          and state.get_by_role("link", name=back).is_visible() and state.get_by_text(gone).count() == 0
          and state.get_attribute("data-denied") == "true")
    no_overflow(page, "member job that is not theirs (403)")
    shot(page, "4_not_theirs_403")
    mctx.close()
    print(f"  (restored {restore_withdrawn(browser, me['id'])} withdrawn assignments afterwards)")


# ------------------------------------------------------------------------------------------ 5. reactivate
def step_reactivate(browser):
    print("== 5. reactivate: not offered to a Team Lead for an account an Admin deactivated")
    name = "Omar Haddad"
    actx = new_context(browser)
    apage = actx.new_page()
    login(apage, "admin@example.com")
    lctx = new_context(browser)
    lpage = lctx.new_page()
    login(lpage, "lead.alpha@example.com")
    oid = member_id(lpage, "omar@example.com")

    def menu(page, path):
        page.goto(BASE + path)
        button = vis(page.get_by_role("button", name=f"Actions for {name}")).first
        button.wait_for(timeout=T)
        page.wait_for_load_state("networkidle")
        button.click()
        page.get_by_role("menu").wait_for(timeout=T)
        return page.get_by_role("menu")

    def deactivate(page, path):
        menu(page, path).get_by_role("menuitem", name="Deactivate").click()
        page.get_by_role("alertdialog").get_by_role("button", name="Deactivate account").click()
        toast(page, f"{name} was deactivated")

    def reactivate(page, path):
        menu(page, path).get_by_role("menuitem", name="Reactivate").click()
        page.get_by_role("alertdialog").get_by_role("button", name="Reactivate account").click()
        toast(page, f"{name} can sign in again")

    user = api(lpage, f"/api/users/{oid}")
    if not user["is_active"]:                       # left over from an interrupted run
        reactivate(apage, "/admin/members")

    # the Admin deactivates
    deactivate(apage, "/admin/members")
    user = api(lpage, f"/api/users/{oid}")
    check("the API marks the account as deactivated by an Admin", user["is_active"] is False and user["deactivated_by_admin"] is True, json.dumps({k: user[k] for k in ("is_active", "deactivated_by_admin")}))
    m = menu(lpage, "/tl/members")
    check("Team Lead, member list: Reactivate is not offered", m.get_by_role("menuitem", name="Reactivate").count() == 0)
    check("Team Lead, member list: 'Deactivated by an administrator' is shown instead", m.get_by_text("Deactivated by an administrator", exact=True).is_visible(),
          m.inner_text().replace("\n", " | "))
    check("Team Lead: the other actions are still there", m.get_by_role("menuitem", name="Rename").is_visible())
    no_overflow(lpage, "Team Lead member list with the account menu open")
    shot(lpage, "5_tl_menu_deactivated_by_admin")
    lpage.keyboard.press("Escape")
    lpage.goto(BASE + f"/tl/members/{oid}")
    lpage.get_by_role("button", name="Manage Account").click()
    m = lpage.get_by_role("menu")
    check("Team Lead, member page: the same (no Reactivate, the sentence instead)",
          m.get_by_role("menuitem", name="Reactivate").count() == 0 and m.get_by_text("Deactivated by an administrator", exact=True).is_visible())
    lpage.keyboard.press("Escape")
    m = menu(apage, "/admin/members")
    check("Admin: Reactivate is offered, and not the sentence", m.get_by_role("menuitem", name="Reactivate").is_visible()
          and m.get_by_text("Deactivated by an administrator").count() == 0)
    apage.keyboard.press("Escape")
    reactivate(apage, "/admin/members")
    check("the Admin can reactivate the account", api(lpage, f"/api/users/{oid}")["is_active"] is True)

    # the Team Lead deactivates
    deactivate(lpage, "/tl/members")
    user = api(lpage, f"/api/users/{oid}")
    check("deactivated by the Team Lead: the API does not mark it as an Admin's decision", user["is_active"] is False and user["deactivated_by_admin"] is False)
    m = menu(lpage, "/tl/members")
    check("Team Lead: Reactivate is offered for an account the Team Lead deactivated",
          m.get_by_role("menuitem", name="Reactivate").is_visible() and m.get_by_text("Deactivated by an administrator").count() == 0)
    lpage.keyboard.press("Escape")
    reactivate(lpage, "/tl/members")
    check("the Team Lead can reactivate that account", api(lpage, f"/api/users/{oid}")["is_active"] is True)
    lctx.close()
    actx.close()


# ------------------------------------------------------------------------------------------ 6. deep search default
def step_deep_search(browser):
    print("== 6. deep search default: the switch in both search forms starts at the setting")
    actx = new_context(browser)
    apage = actx.new_page()
    login(apage, "admin@example.com")
    lctx = new_context(browser)
    lpage = lctx.new_page()
    login(lpage, "lead.alpha@example.com")

    def set_default(on):
        open_settings(apage)
        switch = apage.get_by_role("switch", name="Deep search by default")
        if (switch.get_attribute("aria-checked") == "true") != on:
            switch.click()
            response = save_settings(apage)
            assert response.status == 200, response.status

    def search_switch(page, path):
        page.goto(BASE + path)
        switch = vis(page.get_by_role("switch", name="Deep search")).first
        switch.wait_for(timeout=T)
        page.wait_for_load_state("networkidle")
        return switch.get_attribute("aria-checked") == "true"

    def through_menu(name, url):
        """Client-side navigation: Next.js keeps the screen that is left alive, with its form."""
        open_nav(apage)
        nav_link(apage, name).click()
        apage.wait_for_url(re.compile(url), timeout=T)

    def flip_default_in_place(expect_on):
        switch = vis(apage.get_by_role("switch", name="Deep search by default")).first
        switch.wait_for(timeout=T)
        switch.click()
        response = save_settings(apage)
        assert response.status == 200 and response.json()["deep_search_default"] is expect_on, response.text()[:200]

    set_default(True)
    check("setting on: me.settings.deep_search_default is true for the Team Lead", api(lpage, "/api/auth/me")["settings"]["deep_search_default"] is True)
    check("setting on: the Admin's search form starts with deep search on", search_switch(apage, "/admin/search") is True)
    check("setting on: the Team Lead's search form starts with deep search on", search_switch(lpage, "/tl/search") is True)

    # The Admin has the search form in the background, changes the setting, and comes back through the menu:
    # the untouched switch follows the new default.
    through_menu("Settings", r"/admin/settings")
    flip_default_in_place(False)
    check("the Admin can turn the default off", True)
    through_menu("Find Jobs", r"/admin/search")
    expect(vis(apage.get_by_role("switch", name="Deep search")).first).to_have_attribute("aria-checked", "false", timeout=T)
    check("setting off: the search form the Admin had open follows the new default", True)

    check("setting off: me.settings.deep_search_default is false for the Team Lead", api(lpage, "/api/auth/me")["settings"]["deep_search_default"] is False)
    check("setting off: the Admin's search form starts with deep search off", search_switch(apage, "/admin/search") is False)
    check("setting off: the Team Lead's search form starts with deep search off", search_switch(lpage, "/tl/search") is False)
    no_overflow(lpage, "Team Lead search form")
    shot(lpage, "6_tl_search_deep_off")

    # A switch set by hand is left alone when the default changes afterwards (default is off here).
    hand = vis(apage.get_by_role("switch", name="Deep search")).first
    hand.click()
    expect(hand).to_have_attribute("aria-checked", "true", timeout=T)
    through_menu("Settings", r"/admin/settings")
    flip_default_in_place(True)
    through_menu("Find Jobs", r"/admin/search")
    apage.wait_for_load_state("networkidle")
    through_menu("Settings", r"/admin/settings")
    flip_default_in_place(False)
    through_menu("Find Jobs", r"/admin/search")
    apage.wait_for_load_state("networkidle")
    apage.wait_for_timeout(500)
    check("a switch set by hand stays as set when the default changes (on by hand, default now off)",
          vis(apage.get_by_role("switch", name="Deep search")).first.get_attribute("aria-checked") == "true")

    # A preset carries its own choice: it is not overridden by coming back to the screen or by a new default.
    preset = f"Deep on {TAG}"
    vis(apage.get_by_role("button", name=re.compile("^Presets"))).first.click()
    apage.get_by_label("Save the current settings as").fill(preset)
    apage.get_by_role("button", name="Save", exact=True).click()
    toast(apage, f'Preset "{preset}" saved')
    apage.keyboard.press("Escape")
    check("fresh form again: deep search off (the default)", search_switch(apage, "/admin/search") is False)
    vis(apage.get_by_role("button", name=re.compile("^Presets"))).first.click()
    apage.locator("li").filter(has_text=preset).get_by_role("button", name="Apply").click()
    expect(vis(apage.get_by_role("switch", name="Deep search")).first).to_have_attribute("aria-checked", "true", timeout=T)
    through_menu("Settings", r"/admin/settings")
    through_menu("Find Jobs", r"/admin/search")
    apage.wait_for_load_state("networkidle")
    apage.wait_for_timeout(500)
    check("a preset's deep-search choice survives leaving the screen and coming back",
          vis(apage.get_by_role("switch", name="Deep search")).first.get_attribute("aria-checked") == "true")
    vis(apage.get_by_role("button", name=re.compile("^Presets"))).first.click()
    apage.get_by_role("button", name=f"Delete preset {preset}").click()
    toast(apage, "Preset deleted")
    apage.keyboard.press("Escape")

    set_default(True)
    check("the default is back on at the end", api(lpage, "/api/auth/me")["settings"]["deep_search_default"] is True)
    lctx.close()
    actx.close()


# ------------------------------------------------------------------------------------------ 7. AI notice on resume upload
NONE_TEXT = "No outside AI service is configured on this server. The resume text and the uploaded file stay on this server."


def sent_text(names):
    return (f"When a job search runs for this candidate, text from this resume is sent to {names} to match and rank the jobs found. "
            "The uploaded file is kept on this server, where the candidate's responsible member can download it.")


def step_ai_notice(browser):
    print("== 7. a candidate's resume upload: where the resume text goes")
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "lead.alpha@example.com")
    cid = candidate_id(page, CANDIDATE_NAME)
    page.goto(BASE + f"/tl/candidates/{cid}?tab=profile")
    page.get_by_text(f"Drop a new file here to replace {CANDIDATE_NAME}'s resume, or choose a file").wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    me = api(page, "/api/auth/me")
    notice = vis(page.get_by_role("status").filter(has_text="Resume text").or_(page.get_by_role("status").filter(has_text="resume text")))
    check("the demo API has no AI provider configured", me["settings"]["ai_providers"] == [], json.dumps(me["settings"]["ai_providers"]))
    check("Team Lead, candidate's resume panel: the notice says nothing leaves the server", notice.count() == 1 and notice.first.inner_text().strip() == NONE_TEXT,
          notice.first.inner_text().strip() if notice.count() else "missing")
    check("the old fixed sentences are gone", page.get_by_text("sent to the configured AI providers").count() == 0
          and page.get_by_text("The uploaded file itself is not kept").count() == 0 and page.get_by_text("You are uploading on behalf of").count() == 0)
    no_overflow(page, "candidate profile tab with the resume notice")
    shot(page, "7_candidate_resume_notice_none")
    ctx.close()

    # the responsible member uploads nothing: the read-only resume has no upload area and no notice, whatever is configured
    ctx = new_context(browser)
    page = ctx.new_page()
    alter_json(page, "**/api/auth/me", lambda body: body["settings"].update(ai_providers=["OpenAI", "Anthropic"]))
    login(page, MEMBER_EMAIL)
    page.goto(BASE + f"/member/candidates/{cid}?tab=resume")
    vis(page.get_by_role("link", name="Download File")).first.wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    check("member, candidate's resume: no upload control and no notice about AI services",
          page.locator('input[type="file"]').count() == 0 and page.get_by_text(re.compile("outside AI service|text from this resume")).count() == 0)
    page.goto(BASE + "/member/settings")
    page.get_by_label("Current password").wait_for(timeout=T)
    check("member, own settings: no resume upload and no notice either", page.locator('input[type="file"]').count() == 0
          and page.get_by_text(re.compile("outside AI service|text from this resume")).count() == 0)
    ctx.close()

    # providers configured: checked by altering the /api/auth/me answer on the way back
    for names, joined in ((["OpenAI", "Anthropic"], "OpenAI and Anthropic"), (["OpenAI"], "OpenAI")):
        ctx = new_context(browser)
        page = ctx.new_page()
        alter_json(page, "**/api/auth/me", lambda body, names=names: body["settings"].update(ai_providers=names))
        login(page, "lead.alpha@example.com")
        page.goto(BASE + f"/tl/candidates/{cid}?tab=profile")
        page.get_by_text(f"Drop a new file here to replace {CANDIDATE_NAME}'s resume, or choose a file").wait_for(timeout=T)
        notice = vis(page.get_by_role("status").filter(has_text="text from this resume"))
        notice.first.wait_for(timeout=T)
        check(f"Team Lead, providers {names} (altered answer): the notice names them, 'for this candidate'", notice.first.inner_text().strip() == sent_text(joined),
              notice.first.inner_text().strip())
        check("and the 'no outside AI service' text is not shown", page.get_by_text("No outside AI service").count() == 0)
        if len(names) == 2:
            no_overflow(page, "candidate profile tab with the notice naming two providers")
            shot(page, "7_tl_resume_notice_providers")
        ctx.close()

    ctx = new_context(browser)
    page = ctx.new_page()
    alter_json(page, "**/api/auth/me", lambda body: body["settings"].update(ai_providers=["OpenAI", "Anthropic"]))
    login(page, "admin@example.com")
    page.goto(BASE + f"/admin/candidates/{cid}?tab=profile")
    page.get_by_text(f"Drop a new file here to replace {CANDIDATE_NAME}'s resume, or choose a file").wait_for(timeout=T)
    notice = vis(page.get_by_role("status").filter(has_text="text from this resume"))
    notice.first.wait_for(timeout=T)
    check("Admin on the candidate's resume panel (altered answer): the same notice",
          notice.first.inner_text().strip() == sent_text("OpenAI and Anthropic"), notice.first.inner_text().strip())
    no_overflow(page, "Admin candidate detail with the resume notice")

    # the Add Candidate dialog says the same once a file has been chosen (nothing is created here: the dialog is cancelled)
    page.goto(BASE + "/admin/candidates")
    vis(page.get_by_role("button", name="Add Candidate")).first.click()
    dialog = page.get_by_role("dialog", name="Add Candidate")
    dialog.wait_for(timeout=T)
    check("Add Candidate, no file chosen yet: no notice", dialog.get_by_role("status").filter(has_text="text from this resume").count() == 0)
    dialog.locator('input[type="file"]').set_input_files({"name": "notice-check.txt", "mimeType": "text/plain", "buffer": b"A resume that is never uploaded."})
    notice = dialog.get_by_role("status").filter(has_text="text from this resume")
    notice.wait_for(timeout=T)
    check("Add Candidate with a file chosen (altered answer): the notice names the providers", notice.inner_text().strip() == sent_text("OpenAI and Anthropic"), notice.inner_text().strip())
    check("an Admin adding a candidate has to choose the team", dialog.get_by_role("combobox", name="Team").is_visible())
    if MOBILE:
        box = dialog.bounding_box()
        check("390 px: the Add Candidate dialog with a file and the notice fits the screen", box["x"] >= 0 and box["x"] + box["width"] <= A.w + 0.5 and box["y"] + box["height"] <= A.h + 0.5, str(box))
    dialog.get_by_role("button", name="Cancel").click()
    dialog.wait_for(state="hidden", timeout=T)
    ctx.close()


STEPS = [
    ("fields", step_extra_fields),
    ("password", step_password),
    ("forgot", step_forgot),
    ("withdrawn", step_withdrawn),
    ("reactivate", step_reactivate),
    ("deep", step_deep_search),
    ("ai", step_ai_notice),
]

with sync_playwright() as p:
    browser = p.chromium.launch()
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
print(f"\n{A.w}x{A.h}: {passed} passed, {len(failed)} failed, {len(problems)} unexpected console/network problems"
      f" ({len(dev_notices)} Next.js dev-server notices about a disabled browser cache on intercepted pages, not counted)")
for name, detail in failed:
    print(f"  FAILED {name}  [{detail}]")
for problem in problems:
    print(f"  PROBLEM {problem}")
sys.exit(1 if failed or problems else 0)
