"""Visit every route for each role at the given width; report console problems, failed requests and overflow."""
import argparse, json, sys, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:3011"
PASSWORD = "Demo-Password-42!"
ap = argparse.ArgumentParser()
ap.add_argument("--w", type=int, default=1280); ap.add_argument("--h", type=int, default=900)
ap.add_argument("--roles", default="admin,tl,member"); ap.add_argument("--shots", default="")
ap.add_argument("--dark", action="store_true"); ap.add_argument("--base", default=BASE)
a = ap.parse_args()

CANDIDATE_TABS = ["?tab=jobs", "?tab=profile", "?tab=activity", "?tab=searches"]
ROUTES = {
  "admin": ("admin@example.com", ["/admin/dashboard", "/admin/analytics", "/admin/teams", "/admin/teams/{team}", "/admin/teams/{team}?tab=candidates", "/admin/teams/{team}?tab=members",
                                   "/admin/teams/{team}?tab=assignments", "/admin/teams/{team}?tab=queue", "/admin/teams/{team}?tab=searches", "/admin/team-leads",
                                   "/admin/members", "/admin/members/{member}", "/admin/members/{member}?tab=candidates", "/admin/members/{member}?tab=jobs", "/admin/members/{member}?tab=activity",
                                   "/admin/candidates", "/admin/candidates?member=unassigned&resume=no", "/admin/candidates/{candidate}", *["/admin/candidates/{candidate}" + t for t in CANDIDATE_TABS],
                                   "/admin/candidates/{bare}?tab=profile",
                                   "/admin/jobs", "/admin/jobs/{job}", "/admin/search", "/admin/search?candidate={candidate}", "/admin/searches", "/admin/searches/{search}", "/admin/assignments",
                                   "/admin/invitations", "/admin/activity", "/admin/settings", "/admin/settings?tab=integrations", "/admin/settings?tab=account"]),
  "tl": ("lead.alpha@example.com", ["/tl/dashboard", "/tl/candidates", "/tl/candidates?member=unassigned&resume=no", "/tl/candidates/{candidate}", *["/tl/candidates/{candidate}" + t for t in CANDIDATE_TABS],
                                    "/tl/candidates/{bare}?tab=profile", "/tl/members", "/tl/members/{member}", "/tl/members/{member}?tab=candidates", "/tl/members/{member}?tab=jobs",
                                    "/tl/members/{member}?tab=activity", "/tl/search", "/tl/search?candidate={candidate}", "/tl/searches", "/tl/searches/{search}",
                                    "/tl/jobs", "/tl/jobs?tab=jobs", "/tl/jobs/{job}", "/tl/assignments", "/tl/assignments?applied=true", "/tl/analytics", "/tl/invitations", "/tl/settings"]),
  # /member/profile no longer exists as a page of its own: it must land on the account settings.
  "member": ("maya@example.com", ["/member/dashboard", "/member/candidates", "/member/candidates?sort=name", "/member/candidates/{candidate}", "/member/candidates/{candidate}?tab=details",
                                  "/member/candidates/{candidate}?tab=profile", "/member/candidates/{candidate}?tab=resume", "/member/candidates/{candidate}?tab=activity",
                                  "/member/jobs", "/member/jobs?candidate={candidate}", "/member/jobs/{assignment}", "/member/applications", "/member/activity", "/member/settings",
                                  "/member/profile"]),
}
# Where a route must end up when it is only a redirect.
LANDS_ON = {"/member/profile": "/member/settings"}

# Elements that stick out of the box they sit in (a card, a panel, a table cell with a border or a background).
# Scrolling or clipping containers, absolutely positioned elements and 1px screen-reader elements are left out.
STICKS_OUT = """() => {
  const root = document.querySelector('main') || document.body;
  const out = [];
  const isBox = (el) => {
    const cs = getComputedStyle(el);
    const border = parseFloat(cs.borderLeftWidth) > 0 || parseFloat(cs.borderRightWidth) > 0;
    const bg = cs.backgroundColor !== 'rgba(0, 0, 0, 0)' && cs.backgroundColor !== 'transparent';
    return border || bg;
  };
  for (const el of root.querySelectorAll('input, textarea, button, a, [role=combobox], [data-slot=tag], h1, h2, h3, h4, p, dt, dd, label, td, th, svg.recharts-surface')) {
    const r = el.getBoundingClientRect();
    if (r.width <= 2 || r.height <= 2) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.position === 'absolute' || cs.position === 'fixed') continue;
    if (!el.checkVisibility || !el.checkVisibility({ checkOpacity: false, checkVisibilityCSS: true })) continue;
    let a = el.parentElement, clipped = false, skip = false;
    while (a && a !== document.body) {
      const acs = getComputedStyle(a);
      if (acs.position === 'absolute' || acs.position === 'fixed') { skip = true; break; }
      if (acs.overflowX !== 'visible') { clipped = true; break; }
      if (isBox(a)) break;
      a = a.parentElement;
    }
    if (skip || clipped || !a || a === document.body) continue;
    const b = a.getBoundingClientRect();
    if (r.right > b.right + 1.5 || r.left < b.left - 1.5) {
      out.push(`${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''} "${(el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 40)}" [${Math.round(r.left)}..${Math.round(r.right)}] outside ${a.tagName.toLowerCase()}.${(a.className || '').toString().split(' ').slice(0, 3).join('.')} [${Math.round(b.left)}..${Math.round(b.right)}]`);
    }
  }
  return out.slice(0, 8);
}"""


def api(page, path):
    return page.evaluate("async (p) => { const r = await fetch(p, {credentials: 'same-origin'}); return r.ok ? await r.json() : null }", path)

problems = 0
with sync_playwright() as pw:
    b = pw.chromium.launch(args=["--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost"])
    for role in a.roles.split(","):
        email, routes = ROUTES[role]
        ctx = b.new_context(viewport={"width": a.w, "height": a.h}, color_scheme="dark" if a.dark else "light")
        page = ctx.new_page()
        logs = []
        page.on("console", lambda m: logs.append(f"console.{m.type}: {m.text[:400]}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: logs.append(f"pageerror: {str(e)[:400]}"))
        page.on("response", lambda r: logs.append(f"http {r.status} {r.request.method} {r.url}") if r.status >= 400 else None)
        page.on("requestfailed", lambda r: logs.append(f"requestfailed {r.method} {r.url} {r.failure}") if "/api/" in r.url and "ERR_ABORTED" not in str(r.failure) else None)
        page.goto(a.base + "/login")
        page.get_by_label("E-mail").fill(email)
        page.get_by_label("Password", exact=True).fill(PASSWORD)
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
        page.wait_for_load_state("networkidle")
        ids = {}
        if role != "member":
            teams = api(page, "/api/teams") or []
            ids["team"] = teams[0]["id"] if teams else ""
            users = api(page, "/api/users?role=MEMBER&sort=name&page_size=50") or {"items": []}
            holders = [u for u in users["items"] if u.get("candidate_count")]             # a member who holds candidates: every tab has content
            ids["member"] = (holders or users["items"] or [{"id": ""}])[0]["id"]
            jobs = api(page, "/api/jobs?page_size=1") or {"items": []}
            ids["job"] = jobs["items"][0]["id"] if jobs["items"] else ""
            searches = api(page, "/api/searches?page_size=5") or {"items": []}
            review = [s for s in searches["items"] if s["mode"] == "review"]
            ids["search"] = (review or searches["items"] or [{"id": ""}])[0]["id"]
        else:
            asg = api(page, "/api/assignments?page_size=1&status=APPLIED") or {"items": []}
            ids["assignment"] = asg["items"][0]["id"] if asg["items"] else ""
        # The candidate with the most jobs (charts, lists and activity are filled), and one without a resume (empty states).
        people = (api(page, "/api/candidates?page_size=200") or {"items": []})["items"]
        people.sort(key=lambda c: -c["stats"]["assigned"])
        ids["candidate"] = people[0]["id"] if people else ""
        ids["bare"] = next((c["id"] for c in people if not c["has_resume"]), ids["candidate"])
        missing = [k for k, v in ids.items() if not v]
        if missing:
            problems += 1
            print(f"BAD [{role} {a.w}px] no demo record to visit for: {', '.join(missing)}")
        for route in routes:
            path = route.format(**ids)
            logs.clear()
            t = time.time()
            page.goto(a.base + path)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception as e:
                logs.append(f"networkidle timeout")
            page.wait_for_timeout(700)
            overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            h1 = page.locator("h1").filter(visible=True).first
            title = h1.inner_text() if h1.count() else "(no h1)"
            errs = page.locator("[data-slot=error-state]").filter(visible=True).count()
            if route in LANDS_ON and not page.url.split("?")[0].endswith(LANDS_ON[route]):
                logs.append(f"expected to land on {LANDS_ON[route]}, is on {page.url}")
            if title == "(no h1)":
                logs.append("the page has no visible h1")
            for line in page.evaluate(STICKS_OUT):
                logs.append("sticks out: " + line)
            state = "OK " if not logs and overflow <= 0 and not errs else "BAD"
            if state == "BAD": problems += 1
            print(f"{state} [{role} {a.w}px] {route:48s} h1={title[:40]!r} overflow={overflow} error_states={errs} {time.time()-t:.1f}s")
            for l in logs: print("      ", l)
            if a.shots:
                name = route.strip("/").replace("/", "_").replace("{", "").replace("}", "").replace("?", "_").replace("=", "-")
                page.screenshot(path=f"{a.shots}/{role}_{a.w}_{name}.png", full_page=True)
        ctx.close()
    b.close()
print("problems:", problems)
sys.exit(1 if problems else 0)
