"""Fifth browser check list: the 17 ATS platforms.

Admin: Settings, tab "ATS Platforms" (every platform with its state in plain words, the startup filter, counts and the
last successful read, the note, loading, empty and error states, cards on a phone, the search providers), in light and
dark.
Team Lead and Admin: in Find Jobs the "Where to search" panel (the ATS platform list, the "limited coverage" mark, the
"Startup focus" switch), what the form sends to POST /api/searches, and a preset that keeps the choice.
Searches run by the demo stack's real search engine against its synthetic sources, one limited to four platforms,
one without a filter and one on a platform that cannot be searched: the progress window while a search runs, then
"Where the jobs came from" and "Settings used" on the search page (also for a run with two candidates), the posting
link and the match explanation of a result, and what the responsible member sees.
Team Lead and Member: no ATS Platforms tab, and their pages never ask GET /api/integrations/ats.
Last: the test mode panel of the Admin settings ("Switch to Real Data"), which deletes the demo data.

    python tests/e2e/ui_checks_platforms.py --base http://127.0.0.1:3011 --w 1280 --h 900
    python tests/e2e/ui_checks_platforms.py --base http://127.0.0.1:3011 --w 390 --h 844

It needs the demo stack with freshly seeded data, the synthetic search engine and test mode switched on
(`python -m tests.e2e.server --reset --search-engine --test-mode`; tests/e2e/run.sh starts it that way). The last
step ends test mode, so the stack must be seeded again before anything else runs against it.

Every figure is compared with what the API reports for the same search. Nothing is answered in the browser, except
where a check says so: the loading, empty and error states of the ATS tab, a preset that names a platform the server
no longer has, and a search whose figures were never recorded. This test's own browser cannot resolve any host but
this machine.
"""
import argparse
import json
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
SEARCH_T = 120000                 # one search of the demo stack, with the slow synthetic source
STAMP = str(int(time.time()))[-6:]
PICKER = "Candidates in the portal"
HEALTH_TABLE = "Applicant tracking systems and their state"
SHARED = {}                       # what one step leaves for the next (the ids of the two searches)

# The five states of GET /api/integrations/ats and the three of a search provider, in the words the screen uses.
STATE_WORDS = {"verified_working": "Verified working", "configured_unverified": "Configured, not verified yet",
               "public_discovery_only": "Public pages only",
               "temporarily_unavailable": "Temporarily unavailable", "unsupported": "Not supported"}
PROVIDER_WORDS = {"configured": "Set up", "not_configured": "Not set up", "switched_off": "Switched off"}
REASON_WORDS = {"not_relevant": "title did not match", "non_us": "outside the United States", "work_mode": "work arrangement did not match",
                "eligibility": "work authorization did not match", "ai_audit": "set aside by the AI check"}
# The filter of the first search: two platforms with a board reader, one whose public pages the synthetic search
# engine knows a posting of, and one for which it knows nothing.
FILTER = ["greenhouse", "lever", "jobvite", "jazzhr"]

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
    if A.shots:   # a full-page capture resizes the viewport, which can close an open dialog: then only the viewport
        page.screenshot(path=f"{A.shots}/{A.w}_pf_{name}.png", full_page=page.get_by_role("dialog").count() + page.get_by_role("alertdialog").count() == 0)


def toast(page, text, timeout=T):
    item = page.locator("[data-sonner-toast]").filter(has_text=text).first
    item.wait_for(timeout=timeout)
    return item


def number(value):
    return f"{value:,}"


def words(text):
    return re.sub(r"\s+", " ", text or "").strip()


# What a person with normal sight can read: the contrast of an element's text against what is really painted behind
# it (translucent backgrounds are laid over each other, as the browser does), and whether its text is cut off.
READABLE = """(el) => {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 1;
  const g = canvas.getContext('2d', { willReadFrequently: true });
  const chain = [];
  for (let n = el; n; n = n.parentElement) chain.unshift(getComputedStyle(n).backgroundColor);
  g.fillStyle = '#ffffff';
  g.fillRect(0, 0, 1, 1);
  for (const colour of chain) { g.fillStyle = colour; g.fillRect(0, 0, 1, 1); }
  const back = [...g.getImageData(0, 0, 1, 1).data];
  g.fillStyle = getComputedStyle(el).color;
  g.fillRect(0, 0, 1, 1);
  const front = [...g.getImageData(0, 0, 1, 1).data];
  const luminance = (d) => {
    const [r, gr, b] = d.slice(0, 3).map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * r + 0.7152 * gr + 0.0722 * b;
  };
  const [light, dark] = [luminance(front), luminance(back)].sort((a, b) => b - a);
  const box = el.getBoundingClientRect();
  return { contrast: Math.round(((light + 0.05) / (dark + 0.05)) * 100) / 100, cut: el.scrollWidth > el.clientWidth + 1,
           left: box.left, right: box.right, size: parseFloat(getComputedStyle(el).fontSize), text: el.textContent.trim() };
}"""


def readable(page, locator, name, minimum=4.5):
    """Every element of the locator has text of at least `minimum` contrast, not cut off and inside the window."""
    bad = []
    items = locator.all()
    for item in items:
        if not item.is_visible():
            continue
        got = item.evaluate(READABLE)
        if got["contrast"] < minimum or got["cut"] or got["size"] < 11 or got["left"] < -1 or got["right"] > A.w + 1:
            bad.append(f"{got['text'][:40]!r} contrast {got['contrast']} cut {got['cut']} {got['size']}px x {got['left']:.0f}..{got['right']:.0f}")
    check(name, items and not bad, "; ".join(bad[:4]) if bad else f"{len(items)} checked")


def candidate_named(page, name):
    data = api(page, "/api/candidates?" + "q=" + name.split()[0] + "&page_size=50")
    return next(c for c in data["items"] if c["name"] == name)


# ---------------------------------------------------------------------------------------------------- ATS Platforms tab
def health_rows(page):
    """One locator per platform: a table row on a wide screen, a card on a phone."""
    if MOBILE:
        return page.get_by_role("list", name=HEALTH_TABLE, exact=True).locator("> li")
    return page.get_by_role("table", name=HEALTH_TABLE, exact=True).locator("tbody tr")


def open_ats_tab(page):
    page.goto(BASE + "/admin/settings?tab=ats")
    page.get_by_role("heading", name="Applicant tracking systems", exact=True).wait_for(timeout=T)
    health_rows(page).first.wait_for(timeout=T)


def ats_checks(page, mode):
    """What the tab shows, against GET /api/integrations/ats. `mode` names the theme in the check names."""
    health = api(page, "/api/integrations/ats")
    platforms = health["platforms"]
    rows = health_rows(page)
    check(f"ATS tab ({mode}): all 17 platforms are listed", rows.count() == len(platforms) == 17, f"{rows.count()} rows, {len(platforms)} from the API")
    wrong = []
    for row, platform in zip(rows.all(), platforms):
        text = words(row.inner_text())
        badge = row.locator('[data-slot="tag"][data-state]')
        if platform["label"] not in text or badge.count() != 1 or badge.get_attribute("data-state") != platform["state"] \
                or words(badge.inner_text()) != STATE_WORDS[platform["state"]] or words(platform["reason"]) not in text:
            wrong.append(platform["id"])
    check(f"ATS tab ({mode}): every platform has its state as a badge in plain words, in the API's order, with the reason beside it",
          not wrong, ", ".join(wrong) or str(sorted({p["state"] for p in platforms})))
    panel = words(page.get_by_role("tabpanel").inner_text())
    check(f"ATS tab ({mode}): no internal state name is shown", not any(state in panel for state in STATE_WORDS), panel[:120])
    readable(page, page.get_by_role("tabpanel").locator('[data-slot="tag"]'), f"ATS tab ({mode}): every badge is readable (contrast, not cut off, inside the window)")
    verified = sum(1 for p in platforms if p["state"] == "verified_working")
    check(f"ATS tab ({mode}): the introduction says how many platforms are verified and in which time window",
          f"{verified} of 17 are verified" in panel.replace("\n", " ") and f"in the last {health['window_days']} days" in panel, panel[:260])

    # counts and the last successful read
    wrong = []
    for row, platform in zip(rows.all(), platforms):
        text = words(row.inner_text())
        last = row.locator("[title]").filter(has_text=re.compile(r"ago|just now|yesterday|today", re.I))
        if platform["reader"]:
            ok = number(platform["boards_known"]) in text and number(platform["open_postings"]) in text
            ok = ok and (last.count() == 1 if platform["last_success_at"] else "Never" in text)
        else:
            ok = text.count("—") >= 3 and last.count() == 0
        if not ok:
            wrong.append(f"{platform['id']}: {text[:90]}")
    check(f"ATS tab ({mode}): boards, open postings and the last successful read are shown as the API reports them (a dash without a reader)",
          not wrong, "; ".join(wrong[:3]))
    read = [p for p in platforms if p["last_success_at"]]
    check(f"ATS tab ({mode}): the demo stack has platforms that were really read", {"greenhouse", "lever"} <= {p["id"] for p in read}
          and all(p["state"] == "verified_working" and p["boards_known"] > 0 and p["open_postings"] > 0 for p in read if p["id"] in ("greenhouse", "lever")),
          str([(p["id"], p["state"], p["boards_known"], p["open_postings"]) for p in read]))
    greenhouse = rows.nth([p["id"] for p in platforms].index("greenhouse"))
    when = greenhouse.locator("[title]").filter(has_text=re.compile(r"ago|just now", re.I)).first
    check(f"ATS tab ({mode}): the last successful read is a relative time with the exact time behind it",
          bool(re.search(r"\d{4}", when.get_attribute("title") or "")), f"{words(when.inner_text())} / {when.get_attribute('title')}")
    if MOBILE:
        labels = words(greenhouse.inner_text())
        check(f"ATS tab ({mode}): each card names its figures", all(s in labels for s in ("Boards", "Open postings", "Last successful read")), labels[:200])
    else:
        heads = [words(h.inner_text()) for h in page.get_by_role("table", name=HEALTH_TABLE, exact=True).locator("thead th").all()]
        check(f"ATS tab ({mode}): the table has a column for each figure", heads == ["Platform", "State", "Boards", "Open postings", "Last successful read", "Note"], str(heads))

    # the note
    wrong = []
    for row, platform in zip(rows.all(), platforms):
        wanted = words(platform["note"] or platform["access_note"])
        link = row.get_by_role("link", name=re.compile("Vendor documentation"))
        if wanted not in words(row.inner_text()) or (link.count() == 1) != bool(platform["docs"]) \
                or (platform["docs"] and (link.get_attribute("href") != platform["docs"] or link.get_attribute("target") != "_blank"
                                          or "noopener" not in (link.get_attribute("rel") or ""))):
            wrong.append(platform["id"])
    check(f"ATS tab ({mode}): every platform has its note in full, and a link to the vendor's documentation where there is one", not wrong, ", ".join(wrong))
    notes = rows.locator("span.text-muted-foreground").filter(has_text=re.compile(r"\w{4,}"))
    readable(page, notes, f"ATS tab ({mode}): notes and reasons are readable (contrast, not cut off, inside the window)")
    return health


def step_ats(browser):
    ctx = new_context(browser)
    page = ctx.new_page()
    login(page, "admin@example.com")

    # Loading: the answer is held back in the browser until the placeholder was seen. The answer itself is the API's.
    held = []
    page.route("**/api/integrations/ats", lambda route: held.append(route))
    page.goto(BASE + "/admin/settings?tab=ats")
    tab = page.get_by_role("tab", name="ATS Platforms", exact=True)
    tab.wait_for(timeout=T)
    loading = page.get_by_role("tabpanel").locator('[data-slot="loading"]')
    loading.wait_for(timeout=T)
    check("ATS tab: a placeholder is shown while the platforms load (the answer held back in the browser)",
          loading.is_visible() and tab.get_attribute("aria-selected") == "true" and health_rows(page).count() == 0)
    no_overflow(page, "ATS tab, loading")
    shot(page, "ats_loading")
    for route in held:
        route.continue_()
    page.unroute("**/api/integrations/ats")
    health_rows(page).first.wait_for(timeout=T)
    check("ATS tab: the platforms appear when the answer arrives", page.get_by_role("tabpanel").locator('[data-slot="loading"]').count() == 0)

    health = ats_checks(page, "light")
    platforms = health["platforms"]
    no_overflow(page, "ATS tab")
    if MOBILE:
        cards = health_rows(page)
        wide = [words(c.inner_text())[:30] for c in cards.all() if c.bounding_box()["x"] < 0 or c.bounding_box()["x"] + c.bounding_box()["width"] > A.w + 1]
        check("ATS tab at 390 px: platforms are cards, not a table, and every card fits the screen",
              page.get_by_role("table", name=HEALTH_TABLE, exact=True).count() == 0 and cards.count() == 17 and not wide, str(wide[:3]))
    else:
        scroller = page.get_by_role("group", name=f"{HEALTH_TABLE}, scrollable")
        inner = scroller.evaluate("(el) => el.scrollWidth - el.clientWidth")
        check("ATS tab at 1280 px: the table fits without scrolling sideways", inner <= 0, f"{inner}px")
    shot(page, "ats")

    # the startup filter
    startup = [p["label"] for p in platforms if p["startup"]]
    shown = page.get_by_role("group", name="Platforms shown").or_(page.get_by_role("radiogroup", name="Platforms shown")).first
    shown.get_by_text("Startup platforms", exact=True).click()
    page.wait_for_function("(n) => document.querySelectorAll('[data-slot=\"data-table\"] [data-state][data-slot=\"tag\"]').length === n", arg=len(startup), timeout=T)
    rows = health_rows(page)
    listed = [next((label for label in (p["label"] for p in platforms) if words(row.inner_text()).startswith(label)), "?") for row in rows.all()]
    check("ATS tab: the startup filter shows exactly the seven startup platforms",
          len(startup) == 7 and listed == startup == ["Greenhouse", "Lever", "Ashby", "Workable", "Pinpoint", "Recruitee", "Rippling"], str(listed))
    check("ATS tab: each of them is marked as a startup platform", all(row.locator('[data-slot="tag"]').filter(has_text="Startup").count() == 1 for row in rows.all()))
    no_overflow(page, "ATS tab, startup platforms")
    shot(page, "ats_startup")
    shown.get_by_text("All platforms", exact=True).click()
    page.wait_for_function("() => document.querySelectorAll('[data-slot=\"data-table\"] [data-state][data-slot=\"tag\"]').length === 17", timeout=T)
    marked = [words(row.inner_text())[:20] for row in health_rows(page).all() if row.locator('[data-slot="tag"]').filter(has_text="Startup").count()]
    check("ATS tab: back on all platforms, 17 are listed again and only the seven carry the startup mark", len(marked) == 7, str(marked))

    # search providers
    section = page.locator("section").filter(has=page.get_by_role("heading", name="Search providers", exact=True))
    wrong = []
    for provider in health["providers"]:
        item = section.get_by_role("listitem").filter(has=page.get_by_text(provider["label"], exact=True))
        badge = item.locator('[data-slot="tag"][data-state]')
        if item.count() != 1 or badge.get_attribute("data-state") != provider["state"] or words(badge.inner_text()) != PROVIDER_WORDS[provider["state"]] \
                or words(provider["reason"]) not in words(item.inner_text()):
            wrong.append(provider["id"])
    check("ATS tab: the search providers are listed with whether each is set up, in plain words",
          len(health["providers"]) >= 4 and not wrong, ", ".join(wrong) or ", ".join(f"{p['label']}: {p['state']}" for p in health["providers"]))
    check("ATS tab: the panel says that 'set up' is not a claim that a provider works",
          section.get_by_text("whether each one is set up, not whether its last answer was good").is_visible())
    section.get_by_role("button", name="Open Integrations").click()
    page.wait_for_url(re.compile(r"tab=integrations"), timeout=T)
    try:
        page.locator('[role="tab"][aria-selected="true"]').filter(has_text="Integrations").wait_for(timeout=T)
        check("ATS tab: 'Open Integrations' switches to the Integrations tab", page.get_by_role("heading", name="Search providers", exact=True).count() == 0)
    except Exception:
        check("ATS tab: 'Open Integrations' switches to the Integrations tab", False, page.url)

    # Empty: the API's answer with its platforms taken out in the browser (the real list is never empty).
    def no_platforms(route):
        response = route.fetch()
        route.fulfill(response=response, json={**response.json(), "platforms": []})
    page.route("**/api/integrations/ats", no_platforms)
    page.goto(BASE + "/admin/settings?tab=ats")
    empty = page.get_by_role("tabpanel").locator('[data-slot="empty-state"]')
    empty.wait_for(timeout=T)
    check("ATS tab: an answer without platforms (changed in the browser) is said in words, not left blank",
          "No platforms" in empty.inner_text() and "The server did not list any platform here." in empty.inner_text() and health_rows(page).count() == 0
          and page.get_by_role("heading", name="Search providers", exact=True).is_visible(), words(empty.inner_text()))
    no_overflow(page, "ATS tab, no platforms")
    page.unroute_all(behavior="wait")

    # Error: the request is answered with a 500 in the browser; then the real API answers again.
    with expecting("/api/integrations/ats", "500", "Failed to load resource"):
        page.route("**/api/integrations/ats", lambda route: route.fulfill(status=500, content_type="application/json", body=json.dumps({"error": "Internal error", "detail": ""})))
        page.goto(BASE + "/admin/settings?tab=ats")
        failed = page.get_by_role("tabpanel").locator('[data-slot="error-state"]')
        failed.wait_for(timeout=T)
        check("ATS tab: when the API fails (answered in the browser) an error is shown with 'Try again', and no platform list",
              failed.get_by_role("button", name="Try again").is_visible() and health_rows(page).count() == 0, words(failed.inner_text())[:120])
        no_overflow(page, "ATS tab, error")
        shot(page, "ats_error")
        page.unroute("**/api/integrations/ats")
        page.wait_for_timeout(300)
    page.get_by_role("tabpanel").get_by_role("button", name="Try again").click()
    health_rows(page).first.wait_for(timeout=T)
    check("ATS tab: 'Try again' loads the platforms", health_rows(page).count() == 17)
    ctx.close()


def step_ats_dark(browser):
    ctx, page = signed_in(browser, "admin@example.com", dark=True)
    check("dark: the app is in dark mode", page.evaluate("() => document.documentElement.classList.contains('dark')"))
    open_ats_tab(page)
    ats_checks(page, "dark")
    no_overflow(page, "ATS tab, dark")
    shot(page, "dark_ats")
    ctx.close()


# ---------------------------------------------------------------------------------------------------- roles
def step_roles(browser):
    for email, who, area in (("lead.alpha@example.com", "Team Lead", "/tl"), ("maya@example.com", "Member", "/member")):
        ctx, page = signed_in(browser, email)
        asked = []
        page.on("request", lambda r, asked=asked: asked.append(r.url) if "/api/integrations" in r.url else None)
        page.goto(BASE + "/admin/settings?tab=ats")
        page.wait_for_url(lambda u: "/admin/" not in u, timeout=T)
        page.wait_for_load_state("networkidle")
        check(f"{who}: /admin/settings?tab=ats redirects to their own area", area + "/" in page.url, page.url)
        page.goto(BASE + f"{area}/settings?tab=ats")
        page.get_by_role("heading", level=1, name="Settings").wait_for(timeout=T)
        page.wait_for_load_state("networkidle")
        body = page.locator("main").inner_text()
        check(f"{who}: their own settings have no ATS Platforms tab and no platform list",
              page.get_by_role("tab", name="ATS Platforms").count() == 0 and "Applicant tracking systems" not in body and "Verified working" not in body)
        check(f"{who}: the test mode panel is not theirs to see", "test mode" not in body.lower() and page.get_by_role("button", name="Switch to Real Data").count() == 0)
        for path in (f"{area}/dashboard", f"{area}/settings"):
            page.goto(BASE + path)
            page.wait_for_load_state("networkidle")
        check(f"{who}: no page asked GET /api/integrations/ats", asked == [], str(asked[:3]))
        with expecting("/api/integrations/ats", "/api/admin/go-live", "403", "Failed to load resource"):
            refused = api(page, "/api/integrations/ats")
            go_live = send(page, "POST", "/api/admin/go-live", {"confirm": "REAL DATA"})
            page.wait_for_timeout(200)
        check(f"{who}: asked directly (by this test), the API refuses the platform states and the switch to real data",
              refused == {"__status": 403} and go_live == 403, f"{refused} {go_live}")
        ctx.close()


# ---------------------------------------------------------------------------------------------------- Find Jobs
def open_search_form(page, area):
    page.goto(BASE + f"{area}/search?platforms=1")       # the platform choice is not shown on the plain form any more
    page.wait_for_load_state("networkidle")
    page.get_by_role("group", name=PICKER, exact=True).get_by_role("checkbox").first.wait_for(timeout=T)


def trigger(page):
    return page.get_by_role("button", name="ATS platforms", exact=True)


def open_platform_list(page):
    if trigger(page).get_attribute("aria-expanded") != "true":
        trigger(page).click()
    page.locator("li[data-platform]").first.wait_for(timeout=T)


def tick(page, platform_id, on=True):
    box = page.locator(f'li[data-platform="{platform_id}"]').get_by_role("checkbox")
    if box.is_checked() != on:
        box.click()


def this_search(page):
    return page.locator("section").filter(has=page.get_by_role("heading", name="This search", exact=True))


def posted_searches(page):
    """The bodies of POST /api/searches sent by this page from now on."""
    bodies = []
    page.on("request", lambda r: bodies.append(r.post_data_json) if r.method == "POST" and r.url.endswith("/api/searches") else None)
    return bodies


def start_search(page, candidate, per_candidate=None):
    """Tick the candidate, start the search and return the progress window."""
    page.get_by_role("group", name=PICKER, exact=True).get_by_role("checkbox", name=re.compile(candidate)).check()
    if per_candidate:
        page.get_by_label("Results per candidate").fill(str(per_candidate))
    page.get_by_role("button", name="Start Search").click()
    window = page.get_by_role("dialog", name="AI Job Search Started")
    window.wait_for(timeout=T)
    return window


def wait_finished(window):
    """Wait until the run itself has ended (its tag is the first one; a candidate's tag turns earlier). Returns its status."""
    tag = window.locator('[data-slot="tag"][data-status]').first
    expect(tag).to_have_attribute("data-status", re.compile("^(completed|partial|failed|cancelled)$"), timeout=SEARCH_T)
    return tag.get_attribute("data-status")


def step_filter(browser):
    ctx, page = signed_in(browser, "lead.alpha@example.com")
    options = api(page, "/api/searches/options")
    platforms = options["platforms"]
    labels = {p["id"]: p["label"] for p in platforms}
    open_search_form(page, "/tl")
    panel = page.locator("section").filter(has=page.get_by_role("heading", name="Where to search", exact=True))
    check("Find Jobs: the 'Where to search' panel says that everything is searched by default",
          panel.get_by_text("By default every applicant-tracking platform and job board the portal reads is searched.").is_visible())
    check("platform filter: the default is 'All platforms', with the list closed",
          words(trigger(page).inner_text()) == "All platforms" and trigger(page).get_attribute("aria-expanded") == "false"
          and page.locator("li[data-platform]").count() == 0)
    check("platform filter: the help text says what choosing platforms leaves out",
          panel.get_by_text("Leave it at all platforms to search everywhere. Choosing platforms leaves out job boards and every other site.").is_visible())
    aside = this_search(page)
    check("'This search' says all platforms", "ATS platforms All platforms" in words(aside.inner_text()), words(aside.inner_text())[:200])
    sources_all = int(re.search(r"Sources that will be read \((\d+)\)", aside.inner_text()).group(1))

    open_platform_list(page)
    items = page.locator("li[data-platform]")
    listed = [item.get_attribute("data-platform") for item in items.all()]
    check("platform filter: the list holds the 17 platforms of the API, in its order, nothing ticked",
          listed == [p["id"] for p in platforms] and len(listed) == 17 and not any(box.is_checked() for box in items.get_by_role("checkbox").all()), str(listed[:5]))
    footer = panel.get_by_text("Nothing chosen: all platforms")
    check("platform filter: with nothing ticked the list says all platforms are searched, and 'All platforms' has nothing to reset",
          footer.is_visible() and not panel.get_by_role("button", name="All platforms", exact=True).is_enabled())
    wrong = []
    for platform in platforms:
        item = page.locator(f'li[data-platform="{platform["id"]}"]')
        text = words(item.inner_text())
        mark = {"full": "", "limited": "Limited coverage", "none": "Cannot be searched"}[platform["coverage"]]
        marks = [words(t.inner_text()) for t in item.locator('[data-slot="tag"][data-tone="warning"]').all()]
        if marks != ([mark] if mark else []) or (mark and words(platform["hint"]) not in text) or (not mark and platform["hint"] and words(platform["hint"]) in text) \
                or (item.locator('[data-slot="tag"]').filter(has_text="Startup").count() == 1) != platform["startup"] or platform["label"] not in text:
            wrong.append(platform["id"])
    limited = [p["id"] for p in platforms if p["coverage"] == "limited"]
    unsearchable = [p["id"] for p in platforms if p["coverage"] == "none"]
    check("platform filter: platforms without a reader carry 'Limited coverage' or 'Cannot be searched' with the reason, the others no mark",
          not wrong and {"jobvite", "jazzhr"} <= set(limited) and unsearchable, f"wrong: {wrong}; limited: {limited}; cannot: {unsearchable}")
    readable(page, items.locator('[data-slot="tag"]'), "platform filter: the marks are readable")
    readable(page, items.locator("span.text-muted-foreground"), "platform filter: the reasons are readable")
    check("platform filter: there is nothing to type a platform into (only the listed ones can be ticked)",
          panel.locator("input:not([type=checkbox]), textarea").count() == 0 and panel.get_by_role("checkbox").count() == 17)
    no_overflow(page, "search form, platform list open")
    shot(page, "filter_open")

    # choosing
    tick(page, "lever")
    tick(page, "jazzhr")
    check("platform filter: two chosen platforms are named on the button and under 'This search'",
          words(trigger(page).inner_text()) == "Lever, JazzHR" and "ATS platforms Lever, JazzHR" in words(aside.inner_text())
          and panel.get_by_text("2 chosen", exact=True).is_visible(), words(trigger(page).inner_text()))
    warning = panel.get_by_text(re.compile(r"JazzHR has limited\s+or no coverage, so this search may find few jobs there or none"))
    check("platform filter: choosing a platform with limited coverage warns before the search starts", warning.is_visible())
    sources = [words(t.inner_text()) for t in aside.locator('[data-slot="tag"]').all()]
    check("'This search' lists only the sources that will still be read", "Lever" in sources and "Greenhouse" not in sources and len(sources) < sources_all,
          f"{sources} (all: {sources_all})")
    tick(page, "greenhouse")
    tick(page, "jobvite")
    check("platform filter: more than three chosen platforms are counted on the button, and all named under 'This search'",
          words(trigger(page).inner_text()) == "4 platforms" and "ATS platforms Greenhouse, Lever, Jobvite, JazzHR" in words(aside.inner_text()),
          words(trigger(page).inner_text()))
    check("platform filter: the warning names every chosen platform with limited coverage",
          panel.get_by_text(re.compile(r"Jobvite, JazzHR have limited\s+or no coverage")).is_visible())
    panel.get_by_role("button", name="All platforms", exact=True).click()
    check("platform filter: 'All platforms' clears the choice", words(trigger(page).inner_text()) == "All platforms"
          and not any(box.is_checked() for box in items.get_by_role("checkbox").all()) and warning.count() == 0)

    # startup focus
    switch = page.get_by_role("switch", name="Startup focus")
    startup = [p["label"] for p in platforms if p["startup"]]
    help_text = words(panel.locator("p").filter(has_text="Searches the platforms startups use most first").inner_text())
    check("startup focus: off by default", switch.get_attribute("aria-checked") == "false")
    check("startup focus: the help text names the seven startup platforms and says a match score never changes",
          f"({', '.join(startup)})" in help_text and len(startup) == 7 and "It never changes a match score." in help_text, help_text)
    switch.click()
    check("startup focus: the switch turns on", switch.get_attribute("aria-checked") == "true")

    # a preset keeps both
    for platform_id in FILTER:
        tick(page, platform_id)
    trigger(page).click()                                            # close the list: the button alone must say what is chosen
    check("platform filter: closed again, the button still says what is chosen", page.locator("li[data-platform]").count() == 0
          and words(trigger(page).inner_text()) == "4 platforms")
    name = f"Startup boards {STAMP}{A.w}"
    page.get_by_role("button", name=re.compile("^Presets")).click()
    page.get_by_label("Save the current settings as").fill(name)
    page.get_by_role("button", name="Save", exact=True).click()
    toast(page, f'Preset "{name}" saved')
    stored = next(p for p in api(page, "/api/search-presets") if p["name"] == name)
    check("preset: saved with the chosen platforms and the startup focus",
          stored["filters"]["filters"].get("ats_platforms") == FILTER and stored["filters"]["filters"].get("startup_focus") is True, str(stored["filters"]["filters"]))
    page.keyboard.press("Escape")
    open_search_form(page, "/tl")
    check("after a reload the form is back at all platforms, startup focus off",
          words(trigger(page).inner_text()) == "All platforms" and page.get_by_role("switch", name="Startup focus").get_attribute("aria-checked") == "false")
    page.get_by_role("button", name=re.compile("^Presets")).click()
    page.get_by_role("listitem").filter(has_text=name).get_by_role("button", name="Apply").click()
    open_platform_list(page)
    ticked = [item.get_attribute("data-platform") for item in page.locator("li[data-platform]").all() if item.get_by_role("checkbox").is_checked()]
    check("preset: applying it restores the platform filter and the startup focus",
          ticked == FILTER and words(trigger(page).inner_text()) == "4 platforms" and page.get_by_role("switch", name="Startup focus").get_attribute("aria-checked") == "true",
          str(ticked))
    shot(page, "filter_chosen")

    # A preset that names a platform the server no longer has (answered in the browser): the name is dropped, never sent.
    def stale(route):
        response = route.fetch()
        body = response.json()
        for preset in body:
            if preset["name"] == name:
                preset["filters"]["filters"]["ats_platforms"] = ["lever", "nosuchats", "greenhouse"]
        route.fulfill(response=response, json=body)
    page.route("**/api/search-presets", stale)
    bodies = posted_searches(page)
    open_search_form(page, "/tl")
    page.get_by_role("button", name=re.compile("^Presets")).click()
    page.get_by_role("listitem").filter(has_text=name).get_by_role("button", name="Apply").click()
    page.unroute("**/api/search-presets")
    check("a preset naming an unknown platform (answered in the browser) is applied without it",
          words(trigger(page).inner_text()) == "Greenhouse, Lever", words(trigger(page).inner_text()))
    window = start_search(page, "Hannah Kim", 4)
    status = wait_finished(window)
    sent = (bodies[0] or {}).get("filters", {}) if bodies else {}
    known = set(labels)
    check("the search started from it sends only platforms the API lists: an unknown one cannot be sent from the form",
          len(bodies) == 1 and sent.get("ats_platforms") == ["greenhouse", "lever"] and set(sent.get("ats_platforms") or []) <= known and sent.get("startup_focus") is True
          and status == "completed", f"{sent.get('ats_platforms')} {sent.get('startup_focus')} {status}")
    with expecting("/api/searches", "422", "Failed to load resource"):
        refused = send(page, "POST", "/api/searches", {"candidate_ids": [candidate_named(page, "Hannah Kim")["id"]], "filters": {"ats_platforms": ["nosuchats"]}})
        page.wait_for_timeout(200)
    check("sent directly (by this test), the API refuses an unknown platform", refused == 422, str(refused))
    ctx.close()

    # Admin: the same panel, and what the form sends without a filter and with one
    ctx, page = signed_in(browser, "admin@example.com")
    bodies = posted_searches(page)
    open_search_form(page, "/admin")
    check("Admin: the same 'Where to search' panel, at all platforms and startup focus off",
          words(trigger(page).inner_text()) == "All platforms" and page.get_by_role("switch", name="Startup focus").get_attribute("aria-checked") == "false")
    open_platform_list(page)
    check("Admin: the same 17 platforms", [item.get_attribute("data-platform") for item in page.locator("li[data-platform]").all()] == [p["id"] for p in platforms])
    tick(page, "lever")
    no_overflow(page, "Admin search form, platform list open")
    window = start_search(page, "Lucas Meyer", 3)
    status = wait_finished(window)
    sent = (bodies[0] or {}).get("filters", {}) if bodies else {}
    check("Admin: a search with one platform sends exactly that platform, startup focus off",
          len(bodies) == 1 and sent.get("ats_platforms") == ["lever"] and sent.get("startup_focus") is False and status == "completed", f"{sent} {status}")
    ctx.close()


# ---------------------------------------------------------------------------------------------------- searches
def follow_search(page, bodies):
    """Watch the progress window from start to end. Returns what it showed (in order, without repeats), and what the
    API reported for the run while this page asked (recorded as the answers pass through, unchanged)."""
    reported = []

    def passing(route):
        try:
            response = route.fetch()
            run = response.json()
            reported.append({"status": run["status"], "stage": run.get("stage") or "", "discovered": run["discovered_count"]})
            route.fulfill(response=response)
        except Exception:          # the watching ended while this answer was on its way: the request goes on by itself
            pass
    page.route(re.compile(r"/api/searches/[0-9a-f-]{36}$"), passing)
    shown, pictured = [], []
    read = """() => {
        const w = document.querySelector('[data-slot="search-progress"]');
        if (!w) return null;
        const tag = w.querySelector('[data-slot="tag"][data-status]');
        const live = w.querySelector('[aria-live="polite"]');
        const figure = (label) => { const e = w.querySelector(`[data-slot="run-counter"][data-label="${label}"] [data-slot="run-counter-value"]`); return e ? e.textContent.trim() : ''; };
        return { status: tag ? tag.dataset.status : '', stage: live ? live.textContent.trim() : '', discovered: figure('Discovered'), verified: figure('Verified') };
    }"""
    started = time.time()
    while time.time() - started < SEARCH_T / 1000:
        now = page.evaluate(read)
        if now and (not shown or shown[-1] != now):
            shown.append(now)
            if A.shots and not pictured and now["status"] == "running" and now["discovered"] not in ("", "0"):
                pictured.append(page.screenshot(path=f"{A.shots}/{A.w}_pf_search_running.png"))
        if now and now["status"] in ("completed", "partial", "failed", "cancelled"):
            break
        page.wait_for_timeout(120)
    page.unroute_all(behavior="wait")                                # an answer that is on its way is let through first
    return shown, reported


def origins(page):
    return page.locator("section").filter(has=page.get_by_role("heading", name="Where the jobs came from", exact=True))


def settings_used(page):
    return words(page.locator("section").filter(has=page.get_by_role("heading", name="Settings used", exact=True)).locator("dl").inner_text())


def seconds_text(value):
    """The page's wording of a number of seconds (the API reports tenths of a second)."""
    if value < 0.1:
        return "under 0.1 s"
    if value < 60:
        return f"{round(value, 1):g} s"
    return f"{int(value // 60)} min {round(value % 60)} s"


def origin_checks(page, run, who, target=0):
    """'Where the jobs came from' against the discovery statistics of one candidate of the run, as the API reports them."""
    data = run["targets"][target]["discovery"]
    section = origins(page)
    section.locator("[data-platform]").first.wait_for(timeout=T)
    # On a wide screen the figures are two tables; on a phone each row is a block with its four figures under their labels.
    form = "li" if MOBILE else "tr"
    wrong = []
    for platform_id, row in data["platforms"].items():
        line = section.locator(f'{form}[data-platform="{platform_id}"]')
        cells = [words(c.inner_text()) for c in line.locator('[data-slot="origin-figure"]').all()]
        want = [number(row["unique"]), number(row["verified"]), number(row["rejected"]), number(row["matched"])]
        reasons = ", ".join(f"{number(n)} {REASON_WORDS.get(reason, reason.replace('_', ' '))}" for reason, n in row["reasons"].items() if n > 0)
        head = words(line.locator('[data-slot="origin-name"]').inner_text())
        if cells != want or row["label"] not in head or (reasons and f"Rejected: {reasons}" not in head):
            wrong.append(f"{platform_id}: {head} {cells} (API {want} {reasons})")
    listing = section.get_by_role("list" if MOBILE else "table", name="Jobs found per platform")
    heads = [words(h.inner_text()) for h in (listing.locator("li").first.locator("dt") if MOBILE else listing.locator("thead th")).all()]
    check(f"{who}: per platform the unique, verified and rejected jobs and those in the results are the API's figures, with the reasons for rejecting",
          not wrong and section.locator("[data-platform]").count() == len(data["platforms"]) and heads[-4:] == ["Unique jobs", "Verified", "Rejected", "In results"],
          "; ".join(wrong) or ", ".join(f"{k} {v['unique']}/{v['verified']}/{v['rejected']}" for k, v in data["platforms"].items()))
    wrong = []
    for name, row in data["providers"].items():
        line = section.locator(f'{form}[data-provider="{name}"]')
        cells = [words(c.inner_text()) for c in line.locator('[data-slot="origin-figure"]').all()]
        want = [number(row["queries"]) if row["queries"] is not None else "—", number(row["leads"]), number(row["new_boards"]),
                number(row["verified"] + row["new_board_postings"])]
        kind = "AI provider" if row["kind"] == "ai" else "Search engine"
        if cells != want or kind not in words(line.locator('[data-slot="origin-name"]').inner_text()):
            wrong.append(f"{name}: {cells} (API {want})")
    check(f"{who}: the search engines and AI providers that contributed are listed with the API's figures",
          data["providers"] and not wrong and section.locator("[data-provider]").count() == len(data["providers"]),
          "; ".join(wrong) or ", ".join(f"{k}: {v['queries']} queries, {v['leads']} leads, {v['verified']} verified" for k, v in data["providers"].items()))
    figures = section.locator('[data-slot="origin-figure"]')
    outside = [words(f.inner_text()) for f in figures.all() if (box := f.bounding_box()) is None or box["x"] < 0 or box["x"] + box["width"] > A.w + 1]
    inner = section.locator("div.overflow-x-auto").evaluate_all("(els) => Math.max(0, ...els.map((el) => el.scrollWidth - el.clientWidth))")
    check(f"{who}: every figure is on the screen without scrolling sideways", figures.count() == 4 * (len(data["platforms"]) + len(data["providers"])) and not outside and inner <= 0,
          f"{len(outside)} outside, tables wider than their box by {inner}px")
    facts = words(section.locator("dl").first.inner_text())
    first, total = data["seconds_to_first_verified"], data["total_seconds"]
    check(f"{who}: the time to the first verified results and the total time are the API's",
          isinstance(first, (int, float)) and isinstance(total, (int, float)) and first <= total
          and f"Time to first verified results {seconds_text(first)}" in facts and f"Total time {seconds_text(total)}" in facts, f"{facts[:160]} (API {first} / {total})")
    check(f"{who}: the section says what the figures mean", section.get_by_text(re.compile("Verified means live, in the United States")).is_visible()
          and "Nothing here is estimated" in section.inner_text())
    readable(page, section.locator('[data-slot="tag"]'), f"{who}: the marks in 'Where the jobs came from' are readable")
    return data


def result_checks(page, run, candidate, allowed_hosts, who):
    """A result of the run: the explanation of its match, and the link to the employer's own posting."""
    listed = api(page, f"/api/searches/{run['id']}/results?page_size=100")
    check(f"{who}: the results are listed", listed["total"] == run["matched_count"] > 0, f"{listed['total']} results, matched {run['matched_count']}")
    hosts = sorted({re.sub(r"^https?://([^/]+)/.*$", r"\1", item["job"]["canonical_url"]) for item in listed["items"]})
    if allowed_hosts:
        check(f"{who}: every result is a posting on one of the chosen platforms", all(any(h == a or h.endswith("." + a) for a in allowed_hosts) for h in hosts), str(hosts))
    first = listed["items"][0]
    page.get_by_role("button", name=first["job"]["title"], exact=True).filter(visible=True).first.click()
    drawer = page.get_by_role("dialog").filter(has=page.get_by_role("tab", name=f"Match for {candidate}"))
    drawer.wait_for(timeout=T)
    drawer.get_by_text("Match score").first.wait_for(timeout=T)
    text = words(drawer.inner_text())
    match = first["match"]
    check(f"{who}: a result opens with the explanation of its match (score, breakdown, matching and missing skills)",
          str(first["match_score"]) in text and "Score breakdown" in text and "Matching skills" in text and "Missing skills" in text
          and all(skill in text for skill in match["matching_skills"][:3]) and (not match.get("reason") or words(match["reason"]) in text), text[:200])
    no_overflow(page, f"{who}: result drawer")
    page.wait_for_timeout(400)                                       # the drawer slides in
    shot(page, f"{who.split(':')[0].replace(' ', '_').lower()}_match")
    drawer.get_by_role("tab", name="Assignments").click()
    link = drawer.locator(f'a[href="{first["job"]["canonical_url"]}"]')
    link.first.wait_for(timeout=T)
    href = link.first.get_attribute("href") or ""
    check(f"{who}: the result carries a direct link to the employer's own posting, opened in a new tab",
          href == first["job"]["canonical_url"] and href.startswith("https://") and "/api/" not in href and link.first.get_attribute("target") == "_blank"
          and "noopener" in (link.first.get_attribute("rel") or "") and href in words(link.first.inner_text()), href)
    page.keyboard.press("Escape")
    drawer.wait_for(state="hidden", timeout=T)
    return listed


def step_search(browser):
    ctx, page = signed_in(browser, "lead.alpha@example.com")
    options = api(page, "/api/searches/options")
    labels = {p["id"]: p["label"] for p in options["platforms"]}
    elena = candidate_named(page, "Elena Petrova")                    # Maya Chen's candidate, no jobs yet
    before = api(page, f"/api/assignments?candidate_id={elena['id']}&page_size=1")["total"]

    # 1. with a platform filter and the startup focus
    bodies = posted_searches(page)
    open_search_form(page, "/tl")
    open_platform_list(page)
    for platform_id in FILTER:
        tick(page, platform_id)
    page.get_by_role("switch", name="Startup focus").click()
    no_overflow(page, "search form with a platform filter")
    window = start_search(page, "Elena Petrova", 50)                  # more than there is to find: the search goes on for several rounds
    shown, reported = follow_search(page, bodies)
    sent = (bodies[0] or {}).get("filters", {}) if bodies else {}
    check("filtered search: POST /api/searches carries the chosen platforms and the startup focus",
          len(bodies) == 1 and sent.get("ats_platforms") == FILTER and sent.get("startup_focus") is True and bodies[0].get("candidate_ids") == [elena["id"]],
          json.dumps(bodies[0])[:300] if bodies else "no request")
    running = [s for s in shown if s["status"] == "running"]
    api_stages = {r["stage"] for r in reported if r["status"] == "running" and r["stage"]}
    # Until the worker's first report a running search has no stage yet; the window then says that it is starting.
    first = [s["stage"] for s in running if s["stage"] not in api_stages]
    check("progress window: a search the worker has just taken is 'Starting the search', never 'Waiting for the background worker'",
          set(first) <= {"Starting the search"}, str(first))
    stages = [stage for stage in dict.fromkeys(s["stage"] for s in running) if stage in api_stages]
    check("progress window: the search is shown as running, stage by stage, while it runs: every stage shown is one the API reported",
          len(stages) >= 2 and all(stage in api_stages for stage in stages), f"shown: {stages}; API: {sorted(api_stages)}")
    check("progress window: the figures move while the search runs", any(s["discovered"] not in ("", "0") for s in running),
          str([(s["stage"], s["discovered"], s["verified"]) for s in running][:6]))
    check("progress window: it ends as completed", shown and shown[-1]["status"] == "completed" and shown[-1]["stage"] == "Search completed", str(shown[-1:]))
    no_overflow(page, "progress window")
    shot(page, "search_completed")
    window.get_by_role("link", name="View Results").click()
    page.wait_for_url(re.compile(r"/tl/searches/[0-9a-f-]{36}$"), timeout=T)
    run_id = page.url.rsplit("/", 1)[1]
    SHARED["filtered"] = run_id
    page.get_by_role("heading", name="Results", exact=True).wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    run = api(page, f"/api/searches/{run_id}")
    check("filtered search: completed, with the filter stored", run["status"] == "completed" and run["filters"].get("ats_platforms") == FILTER
          and run["filters"].get("startup_focus") is True, f"{run['status']} {run['filters']}")
    data = origin_checks(page, run, "filtered search")
    section = origins(page)
    rows = data["platforms"]
    check("filtered search: a row for every chosen platform, also one that found nothing, and for no other platform",
          set(rows) == set(FILTER) and rows["greenhouse"]["unique"] > 0 and rows["lever"]["unique"] > 0 and rows["jazzhr"]["unique"] > 0
          and rows["jobvite"]["unique"] == 0 and rows["greenhouse"]["rejected"] > 0 and rows["lever"]["rejected"] > 0,
          str({k: (v["unique"], v["verified"], v["rejected"]) for k, v in rows.items()}))
    marks = {pid: [words(t.inner_text()) for t in section.locator(f'[data-platform="{pid}"] [data-slot="tag"]').all()] for pid in FILTER}
    check("filtered search: the platforms without a reader are marked 'Limited coverage', the others are not",
          marks == {"greenhouse": [], "lever": [], "jobvite": ["Limited coverage"], "jazzhr": ["Limited coverage"]}, str(marks))
    warnings = [words(w.inner_text()) for w in section.locator('[data-slot="coverage-warnings"] li').all()]
    check("filtered search: a coverage warning for each chosen platform with limited coverage, in the API's words",
          warnings == data["coverage_warnings"] and len(warnings) == 2 and any(w.startswith("JazzHR has limited coverage:") for w in warnings)
          and any(w.startswith("Jobvite has limited coverage:") and w.endswith("Nothing was found for it in this search.") for w in warnings), str(warnings))
    searx = data["providers"].get("SearXNG") or {}
    check("filtered search: the search engine contributed the posting found on the platform without a reader",
          searx.get("queries", 0) > 0 and searx.get("leads", 0) >= 1 and searx.get("verified", 0) >= 1, str(searx))
    facts = words(section.locator("dl").first.inner_text())
    check("filtered search: the section says that the startup focus was on and what it did",
          "Startup focus On. Startup platforms were searched first; at an equal match score their jobs are listed first." in facts, facts[-200:])
    used = settings_used(page)
    names = ", ".join(labels[i] for i in FILTER)
    check("filtered search: 'Settings used' names the platforms and the startup focus", f"ATS platforms {names} Startup focus On" in used, used)
    sources = page.get_by_role("table", name="Job sources read by this search")
    wider = sources.evaluate("(table) => table.parentElement.scrollWidth - table.parentElement.clientWidth")
    read = [words(h.inner_text()) for h in sources.locator("tbody th").all()]
    check("filtered search: the Sources table lists only the chosen platforms that have a reader, and shows every column without scrolling sideways",
          sorted(read) == ["Greenhouse", "Lever"] and wider <= 0, f"{read}, wider than its box by {wider}px")
    no_overflow(page, "search page, filtered search")
    shot(page, "search_detail_filtered")
    result_checks(page, run, "Elena Petrova", ["greenhouse.io", "lever.co", "applytojob.com", "jobvite.com"], "filtered search")
    jazz = [r for r in api(page, f"/api/searches/{run_id}/results?page_size=100")["items"] if "applytojob.com" in r["job"]["canonical_url"]]
    check("filtered search: the posting on the platform without a reader is among the results, with its own address", len(jazz) == 1, str([r["job"]["canonical_url"] for r in jazz]))
    after = api(page, f"/api/assignments?candidate_id={elena['id']}&page_size=1")["total"]
    check("filtered search: the jobs at or above the minimum score were assigned to the candidate", run["assigned_count"] > 0 and after == before + run["assigned_count"],
          f"{before} -> {after}, assigned {run['assigned_count']}")

    # 2. without a filter
    bodies = posted_searches(page)
    open_search_form(page, "/tl")
    window = start_search(page, "Elena Petrova", 12)
    status = wait_finished(window)
    sent = (bodies[0] or {}).get("filters", {}) if bodies else {}
    check("search without a filter: POST /api/searches carries no platform and no startup focus",
          len(bodies) == 1 and sent.get("ats_platforms") == [] and sent.get("startup_focus") is False and status == "completed", f"{sent} {status}")
    window.get_by_role("link", name="View Results").click()
    page.wait_for_url(re.compile(r"/tl/searches/[0-9a-f-]{36}$"), timeout=T)
    plain_id = page.url.rsplit("/", 1)[1]
    SHARED["plain"] = plain_id
    page.get_by_role("heading", name="Results", exact=True).wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    plain = api(page, f"/api/searches/{plain_id}")
    data = origin_checks(page, plain, "search without a filter")
    section = origins(page)
    check("search without a filter: the platforms that were read have a row, no platform is marked and nothing is warned about",
          {"greenhouse", "lever"} <= set(data["platforms"]) and data["coverage_warnings"] == [] and section.locator('[data-slot="coverage-warnings"]').count() == 0
          and section.locator('[data-platform] [data-slot="tag"]').count() == 0, str(list(data["platforms"])))
    facts = words(section.locator("dl").first.inner_text())
    check("search without a filter: no line about a startup focus or about a platform choice", "Startup focus" not in facts and "Left out by the platform choice" not in facts, facts)
    used = settings_used(page)
    check("search without a filter: 'Settings used' says all platforms, startup focus off", "ATS platforms All platforms Startup focus Off" in used, used)
    no_overflow(page, "search page, search without a filter")
    shot(page, "search_detail_plain")
    result_checks(page, plain, "Elena Petrova", [], "search without a filter")
    ctx.close()

    # the member responsible for the candidate sees the assigned jobs
    ctx, page = signed_in(browser, "maya@example.com")
    page.goto(BASE + f"/member/jobs?candidate={elena['id']}")
    cards = page.locator('[data-slot="job-card"]').filter(visible=True)
    cards.first.wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    mine = api(page, f"/api/assignments?candidate_id={elena['id']}&page_size=100")
    titles = {item["job"]["title"] for item in mine["items"]}
    text = words(page.locator("main").inner_text())
    check("Member: the responsible member sees the candidate's jobs from the searches", mine["total"] >= run["assigned_count"] > 0 and cards.count() >= 1
          and "Elena Petrova" in words(cards.first.inner_text()) and any(title in text for title in titles), f"{mine['total']} jobs, {cards.count()} cards shown")
    html = page.content()
    check("Member: no posting address and nothing about platforms or sources on the member's list",
          not any(s in html for s in ("greenhouse.io", "lever.co", "applytojob.com")) and "Where the jobs came from" not in text and "ATS platforms" not in text)
    no_overflow(page, "member job list after the searches")
    shot(page, "member_jobs")
    ctx.close()


def step_states(browser):
    """The section for a run with two candidates, and what it says when figures are missing."""
    ctx, page = signed_in(browser, "lead.alpha@example.com")
    seeded = next((r for r in api(page, "/api/searches?page_size=50")["items"] if len(r["targets"]) == 2 and r["status"] == "completed"), None)
    if not seeded:
        check("the demo data holds a finished search with two candidates", False)
        ctx.close()
        return
    page.goto(BASE + f"/tl/searches/{seeded['id']}")
    page.get_by_role("heading", name="Results", exact=True).wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    run = api(page, f"/api/searches/{seeded['id']}")
    names = [t["candidate"]["name"] for t in run["targets"]]
    used = settings_used(page)
    check("a search stored without the two settings (from the demo data) reads 'All platforms' and startup focus 'Off'",
          not run["filters"].get("ats_platforms") and "ATS platforms All platforms Startup focus Off" in used, f"{run['filters']} / {used}")
    section = origins(page)
    select = section.get_by_role("combobox", name="Candidate")
    check("two candidates: the section shows one candidate's figures at a time, starting with the first",
          select.is_visible() and words(select.inner_text()) == names[0], words(select.inner_text()))
    origin_checks(page, run, f"two candidates, {names[0]}", target=0)
    select.click()
    page.get_by_role("option", name=names[1], exact=True).click()
    page.wait_for_timeout(300)
    check("two candidates: the other candidate can be chosen", words(select.inner_text()) == names[1], words(select.inner_text()))
    origin_checks(page, run, f"two candidates, {names[1]}", target=1)
    no_overflow(page, "search page, two candidates")
    shot(page, "search_detail_two_candidates")

    # A platform nothing can be read from: the form warns, the search runs and says that it could not be searched.
    bodies = posted_searches(page)
    open_search_form(page, "/tl")
    open_platform_list(page)
    tick(page, "icims")
    panel = page.locator("section").filter(has=page.get_by_role("heading", name="Where to search", exact=True))
    check("a platform that cannot be searched can be chosen, and the form warns about it",
          words(trigger(page).inner_text()) == "iCIMS" and panel.get_by_text(re.compile(r"iCIMS has limited\s+or no coverage")).is_visible()
          and "ATS platforms iCIMS" in words(this_search(page).inner_text()), words(this_search(page).inner_text())[-200:])
    window = start_search(page, "Hannah Kim", 5)
    status = wait_finished(window)
    window.get_by_role("link", name="View Results").click()
    page.wait_for_url(re.compile(r"/tl/searches/[0-9a-f-]{36}$"), timeout=T)
    page.get_by_role("heading", name="Results", exact=True).wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    nothing = api(page, f"/api/searches/{page.url.rsplit('/', 1)[1]}")
    data = nothing["targets"][0]["discovery"]
    row = section.locator('[data-platform="icims"]')
    row.wait_for(timeout=T)
    warnings = [words(w.inner_text()) for w in section.locator('[data-slot="coverage-warnings"] li').all()]
    check("a search on a platform that cannot be searched: finished with nothing found, the platform marked 'Not searched', the reason in the API's words",
          status == "completed" and (bodies[0] or {}).get("filters", {}).get("ats_platforms") == ["icims"] and nothing["matched_count"] == 0
          and [words(t.inner_text()) for t in row.locator('[data-slot="tag"]').all()] == ["Not searched"]
          and [words(f.inner_text()) for f in row.locator('[data-slot="origin-figure"]').all()] == ["0", "0", "0", "0"]
          and warnings == data["coverage_warnings"] and len(warnings) == 1 and warnings[0].startswith("iCIMS could not be searched:"),
          f"{status} {nothing['matched_count']} {warnings}")
    facts = words(section.locator("dl").first.inner_text())
    sources = words(page.locator("section").filter(has=page.get_by_role("heading", name="Sources", exact=True)).inner_text())
    check("a search that found nothing says so in the words of a finished search: no verified result, no source read, no provider, no matching jobs",
          page.get_by_text("This search found no matching jobs").is_visible() and data["providers"] == {}
          and section.get_by_text("No search engine or AI provider contributed to this search.", exact=True).is_visible()
          and "Time to first verified results No verified result Total time" in facts and "No job source was read in this search." in sources
          and "yet" not in facts and "yet" not in sources and "ATS platforms iCIMS Startup focus Off" in settings_used(page), f"{facts} / {sources}")
    readable(page, row.locator('[data-slot="tag"]'), "the 'Not searched' mark is readable")
    no_overflow(page, "search page, platform that cannot be searched")
    shot(page, "search_detail_not_searched")

    # The API's answer for a run with its figures taken out in the browser: a search from before they were recorded.
    def without_figures(route):
        response = route.fetch()
        body = response.json()
        for target in body["targets"]:
            target["discovery"] = {}
        route.fulfill(response=response, json=body)
    page.route(re.compile(rf"/api/searches/{seeded['id']}$"), without_figures)
    page.goto(BASE + f"/tl/searches/{seeded['id']}")
    page.get_by_role("heading", name="Results", exact=True).wait_for(timeout=T)
    said = section.get_by_text("This search has no figures per platform: it did not get that far, or it ran before they were recorded.")
    said.wait_for(timeout=T)
    check("a search without recorded figures (taken out in the browser) says so, with no empty table",
          section.locator("[data-platform], [data-provider], table").count() == 0 and "ATS platforms All platforms" in settings_used(page))
    no_overflow(page, "search page, no figures")
    shot(page, "search_detail_no_figures")
    page.unroute_all(behavior="wait")
    ctx.close()


def step_search_dark(browser):
    ctx, page = signed_in(browser, "lead.alpha@example.com", dark=True)
    open_search_form(page, "/tl")
    open_platform_list(page)
    for platform_id in ("lever", "jazzhr"):
        tick(page, platform_id)
    items = page.locator("li[data-platform]")
    readable(page, items.locator('[data-slot="tag"]'), "dark: the marks in the platform list are readable")
    readable(page, items.locator("span.text-muted-foreground"), "dark: the reasons in the platform list are readable")
    no_overflow(page, "search form, platform list open, dark")
    shot(page, "dark_filter_open")
    filtered = SHARED.get("filtered") or next((r["id"] for r in api(page, "/api/searches?mine=true&page_size=50")["items"]
                                               if r["filters"].get("ats_platforms") == FILTER and r["status"] == "completed"), None)
    if filtered:
        page.goto(BASE + f"/tl/searches/{filtered}")
        section = origins(page)
        section.locator("[data-platform]").first.wait_for(timeout=T)
        page.wait_for_load_state("networkidle")
        readable(page, section.locator('[data-slot="tag"]'), "dark: the marks in 'Where the jobs came from' are readable")
        readable(page, section.locator('[data-slot="origin-figure"]'), "dark: the figures in 'Where the jobs came from' are readable")
        readable(page, section.locator('[data-slot="coverage-warnings"] li'), "dark: the coverage warnings are readable")
        no_overflow(page, "search page, filtered search, dark")
        shot(page, "dark_search_detail_filtered")
    else:
        check("dark: the filtered search of the earlier step is there to look at", False, "no search id")
    ctx.close()


# ---------------------------------------------------------------------------------------------------- test mode
def step_test_mode(browser):
    """Last: this deletes the demo data."""
    dctx, dpage = signed_in(browser, "admin@example.com", dark=True)
    dpage.goto(BASE + "/admin/settings")
    dpage.get_by_role("heading", name="The portal is in test mode").wait_for(timeout=T)
    readable(dpage, dpage.get_by_role("heading", name="The portal is in test mode").locator("xpath=following-sibling::p"), "test mode panel, dark: the explanation is readable")
    no_overflow(dpage, "test mode panel, dark")
    shot(dpage, "dark_test_mode")
    dctx.close()

    lead_ctx, lead_page = signed_in(browser, "lead.alpha@example.com")       # signed in before, to see the session end
    ctx, page = signed_in(browser, "admin@example.com")
    counts = {"teams": len(api(page, "/api/teams")), "candidates": api(page, "/api/candidates?page_size=1")["total"],
              "searches": api(page, "/api/searches?page_size=1")["total"]}
    check("test mode: the demo database is in test mode and holds data", api(page, "/api/settings").get("test_mode") is True and all(counts.values()), str(counts))
    page.goto(BASE + "/admin/settings")
    heading = page.get_by_role("heading", name="The portal is in test mode")
    heading.wait_for(timeout=T)
    button = page.get_by_role("button", name="Switch to Real Data")
    explanation = words(heading.locator("xpath=following-sibling::p").inner_text())
    check("test mode: the panel says what is test data, what the switch deletes and what stays",
          button.is_visible() and "are test data" in explanation and "all of it is deleted and the portal starts empty" in explanation
          and "Your Admin account, these settings and the job board index stay." in explanation, explanation)
    readable(page, heading.locator("xpath=following-sibling::p"), "test mode panel: the explanation is readable")
    no_overflow(page, "test mode panel")
    shot(page, "test_mode")
    on_tabs = []
    for tab in ("integrations", "ats", "account"):
        page.goto(BASE + f"/admin/settings?tab={tab}")
        heading.wait_for(timeout=T)
        on_tabs.append(button.is_visible())
    check("test mode: the panel stays above every settings tab", all(on_tabs), str(on_tabs))

    calls = []
    page.on("request", lambda r: calls.append(r.post_data_json) if r.method == "POST" and "/api/admin/go-live" in r.url else None)
    page.goto(BASE + "/admin/settings")
    button.click()
    dialog = page.get_by_role("alertdialog", name="Delete all test data and switch to real data?")
    dialog.wait_for(timeout=T)
    text = words(dialog.inner_text())
    check("test mode: the confirmation names everything that is deleted, that it cannot be undone, and asks for the words REAL DATA",
          "Every team, Team Lead, member, candidate, resume, search, job, assignment, notification and audit entry is deleted." in text
          and "This cannot be undone." in text and dialog.get_by_label("Type REAL DATA to confirm").is_visible(), text[:260])
    no_overflow(page, "test mode confirmation")
    page.wait_for_timeout(400)                                       # the dialog fades in
    shot(page, "test_mode_confirm")
    confirm = dialog.get_by_role("button", name="Delete Test Data")
    confirm.click()
    toast(page, "Type REAL DATA to confirm.")
    check("test mode: without the typed words nothing is sent and the dialog stays", calls == [] and dialog.is_visible())
    dialog.get_by_label("Type REAL DATA to confirm").fill("delete")
    confirm.click()
    page.wait_for_timeout(600)
    check("test mode: with other words nothing is sent either", calls == [] and dialog.is_visible()
          and api(page, "/api/candidates?page_size=1")["total"] == counts["candidates"])
    dialog.get_by_role("button", name="Cancel").click()
    dialog.wait_for(state="hidden", timeout=T)
    button.click()
    dialog.wait_for(timeout=T)
    check("test mode: cancelling forgets what was typed", dialog.get_by_label("Type REAL DATA to confirm").input_value() == "")
    dialog.get_by_label("Type REAL DATA to confirm").fill("REAL DATA")
    with page.expect_response(lambda r: "/api/admin/go-live" in r.url and r.request.method == "POST", timeout=T) as answer:
        confirm.click()
    removed = answer.value.json().get("removed") if answer.value.status == 200 else {}
    check("test mode: with the words typed, POST /api/admin/go-live is sent once and removes the data",
          answer.value.status == 200 and calls == [{"confirm": "REAL DATA"}] and removed.get("teams") == counts["teams"]
          and removed.get("candidates") == counts["candidates"] and removed.get("searches") == counts["searches"], f"HTTP {answer.value.status} {removed}")
    try:
        toast(page, "Test data removed", timeout=10000)
        check("test mode: the Admin is told that the test data was removed", True)
    except Exception:
        check("test mode: the Admin is told that the test data was removed", False, "no toast")
    try:
        heading.wait_for(state="detached", timeout=T)
        dialog.wait_for(state="hidden", timeout=T)
        gone = True
    except Exception:
        gone = False
    check("test mode: the panel and the dialog disappear", gone and button.count() == 0 and page.get_by_role("alertdialog").count() == 0)
    page.wait_for_load_state("networkidle")
    check("test mode: the Admin stays on the settings, signed in", page.url.endswith("/admin/settings") and page.get_by_role("heading", level=1, name="Settings").is_visible()
          and api(page, "/api/auth/me").get("role") == "ADMIN", page.url)
    no_overflow(page, "settings after the switch to real data")
    shot(page, "after_go_live")
    page.reload()
    page.get_by_role("heading", level=1, name="Settings").wait_for(timeout=T)
    page.wait_for_load_state("networkidle")
    left = {"teams": len(api(page, "/api/teams")), "candidates": api(page, "/api/candidates?page_size=1")["total"],
            "searches": api(page, "/api/searches?page_size=1")["total"], "members": api(page, "/api/users?role=MEMBER&page_size=1")["total"]}
    check("test mode: after a reload the Admin is still signed in, the panel stays away and test mode is off",
          "/admin/settings" in page.url and heading.count() == 0 and api(page, "/api/settings").get("test_mode") is False)
    check("test mode: teams, members, candidates and searches are gone", not any(left.values()), str(left))
    health = api(page, "/api/integrations/ats")
    kept = {p["id"]: p["open_postings"] for p in health.get("platforms", []) if p["id"] in ("greenhouse", "lever")}
    check("test mode: the job board index is kept (the ATS tab still has its postings)", len(kept) == 2 and all(kept.values()), str(kept))
    for path, title in (("/admin/dashboard", None), ("/admin/teams", "Teams"), ("/admin/candidates", "Candidates"), ("/admin/searches", "Search History")):
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        if title:
            page.get_by_role("heading", level=1, name=title).wait_for(timeout=T)
        no_overflow(page, f"{path} with no data")
    check("test mode: the emptied portal's pages open without an error", page.locator('[data-slot="error-state"]').count() == 0)
    shot(page, "empty_searches")
    with expecting("/api/admin/go-live", "409", "Failed to load resource"):
        again = send(page, "POST", "/api/admin/go-live", {"confirm": "REAL DATA"})
        page.wait_for_timeout(200)
    check("test mode: a second switch (sent by this test) is refused", again == 409, str(again))
    with expecting("401", "Failed to load resource", "/api/"):
        lead_page.goto(BASE + "/tl/dashboard")
        try:
            lead_page.wait_for_url(re.compile(r"/login"), timeout=T)
            out = True
        except Exception:
            out = False
        lead_page.wait_for_timeout(300)
    check("test mode: a Team Lead who was signed in is signed out (the account is gone)", out, lead_page.url)
    lead_ctx.close()
    ctx.close()


STEPS = [
    ("ats", step_ats),
    ("ats_dark", step_ats_dark),
    ("roles", step_roles),
    ("filter", step_filter),
    ("search", step_search),
    ("states", step_states),
    ("search_dark", step_search_dark),
    ("test_mode", step_test_mode),       # last: it deletes the demo data
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
