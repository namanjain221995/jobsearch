"""Headless-browser crawler pool (Playwright + Chromium).

Used only for pages that need JavaScript to show their content. Several pages render at the same
time (bounded). It behaves as a plain, honest browser:
- identifies itself in the User-Agent, never uses stealth plugins, never solves or bypasses
  CAPTCHAs / bot challenges (a challenge page is reported as "blocked" and dropped);
- all of its traffic goes through a local guard proxy (netguard.GuardProxy), so no page, redirect, iframe or
  script can make it contact localhost or an internal network; the host check that guards every request also
  refuses the hosts this app never fetches, so a rendered page cannot make the crawler talk to them either;
- it only reads: it never types and never presses anything, with one exception that is asked for explicitly
  (render_list, used by the career-site crawler on listing pages): a job list's own "Next" / "Load more" button, a
  button outside any form whose whole label is a paging label. A page cannot make it submit a form (a navigation with
  a method other than GET is refused, also when a pressed button sets it off) and a script's write request leaves only
  to the page's own host or as JSON (see _reads_only);
- images, media and fonts are not downloaded (speed), downloads and service workers are off. Stylesheets are
  loaded: the text a page shows (RenderResult.text) is only right when the page's own rules for hiding apply;
- each page gets a fresh, cookie-less context that is destroyed afterwards.

How long a page is given (measured on live pages on 2026-11-21, tests/fixtures/pages/eval_set.json): the page is
looked at every 0.4 s after DOMContentLoaded. It is taken as soon as it carries JobPosting data, or once it shows a
page full of text that has stopped changing for `settle` seconds, or once neither its text nor its network has moved
for `quiet` seconds; a page that still shows next to nothing while it is loading is given up to `max_wait` seconds
(an ADP career centre needed 5 to 11 s after DOMContentLoaded and once more than 12 s; an Oracle career site added
its JobPosting data 2 s after its text).

Playwright is optional: without it the pool reports itself unavailable and the app keeps working."""
import asyncio
import glob
import os
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from .netguard import GuardProxy, resolve_public

BLOCKED_RESOURCES = {"image", "media", "font"}
MAX_HTML_CHARS = 3_000_000
MAX_TEXT_CHARS = 400_000
FULL_PAGE_CHARS = 1500              # this much visible text is a page that has rendered, not a loading shell
DNS_TTL_SECONDS, DNS_RETRY_SECONDS = 300.0, 15.0
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
_CHALLENGE = re.compile(
    r"(just a moment|attention required|verify(ing)? you are (a )?human|are you a robot|captcha|"
    r"access denied|unusual traffic|checking your browser|pardon our interruption)", re.I)
# One look at the page: how much text a reader sees, and whether JobPosting structured data has arrived.
_LOOK = """() => {
  const body = document.body;
  let posting = false;
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    if ((s.textContent || '').includes('JobPosting')) { posting = true; break; }
  }
  if (!posting && document.querySelector('[itemtype$="schema.org/JobPosting"]')) posting = true;
  return [body ? (body.innerText || '').length : 0, posting];
}"""
_TEXT = "(limit) => (document.body ? (document.body.innerText || '') : '').slice(0, limit)"
# Paging through a list that is built by script (render_list). The words of a control that shows the next part of a
# list, as its whole label: "Next", "Next jobs", "Load more jobs", "Show more results". Read on live career sites on
# 2026-11-21: <button aria-label="Next jobs"> (a list of 2,408 jobs shown 20 at a time) and
# <button aria-label="Load more jobs"> (83 jobs shown 5 at a time). "Next similar jobs" and "Next step" do not match.
PAGING_LABEL = (r"^\W*(next( page| jobs| results| openings| positions| roles)?|(load|show|view|see) more( jobs| results| openings| positions| roles)?|"
                r"more (jobs|results|openings|positions|roles))\W*$")
MAX_PAGING_STEPS = 40               # never more controls pressed on one page, whatever the caller asks for
MAX_LIST_LINKS = 5000               # link addresses kept from the further parts of one list
STEP_WAIT, STEP_STILL = 8.0, 0.9    # seconds a pressed control is given to change the list / the list must then stand still
# The one control this crawler may press: a button (or a link without an address) whose own label is a paging label,
# enabled, visible, and not part of any form. It is marked, never pressed, here.
_PAGING_CONTROL = """(pattern) => {
  const rx = new RegExp(pattern, 'i');
  for (const old of document.querySelectorAll('[data-list-next]')) old.removeAttribute('data-list-next');
  for (const el of document.querySelectorAll('button, [role="button"], a')) {
    if (el.tagName === 'A') {
      const href = (el.getAttribute('href') || '').trim();
      if (href && href !== '#' && !href.toLowerCase().startsWith('javascript:')) continue;   // a link with an address is read as a link
    }
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    if (el.closest('form') || el.form) continue;
    if ((el.getAttribute('type') || '').toLowerCase() === 'submit') continue;
    const text = (el.innerText || '').trim(), aria = (el.getAttribute('aria-label') || '').trim();
    const label = text || aria || (el.getAttribute('title') || '').trim();
    if (!label || label.length > 40 || !(rx.test(label) || (aria && rx.test(aria)))) continue;
    const box = el.getBoundingClientRect(), style = getComputedStyle(el);
    if (box.width < 1 || box.height < 1 || style.visibility === 'hidden' || style.display === 'none') continue;
    el.setAttribute('data-list-next', '1');
    return label;
  }
  return '';
}"""
_PRESS = "() => { const el = document.querySelector('[data-list-next=\"1\"]'); if (!el) return false; el.removeAttribute('data-list-next'); el.click(); return true; }"
# What the list shows now, as one short value: the amount of text, the number of links and a hash of their addresses.
_LIST_STATE = """() => {
  let hash = 0, count = 0;
  for (const a of document.querySelectorAll('a[href]')) {
    const h = a.href; count += 1;
    for (let i = 0; i < h.length; i += 1) hash = (hash * 31 + h.charCodeAt(i)) | 0;
  }
  return (document.body ? (document.body.innerText || '').length : 0) + ':' + count + ':' + hash;
}"""
_LIST_LINKS = """(limit) => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const h = a.href;
    if (h && !seen.has(h)) { seen.add(h); out.push(h); if (out.length >= limit) break; }
  }
  return out;
}"""


@dataclass
class RenderResult:
    status: int = 0
    url: str = ""
    html: str = ""
    error: str = ""          # "" on success; otherwise a short reason ("blocked", "timeout", ...)
    text: str = ""           # what a reader of the rendered page sees (hidden elements left out); "" when unknown
    links: tuple = ()        # render_list only: link addresses of the further parts of the list, in the order they appeared
    steps: int = 0           # render_list only: how often the list's own "next" / "load more" control was pressed


def _related(a: str, b: str) -> bool:
    """The two hosts are the same site as far as can be told without a public-suffix list: equal, or one is a
    subdomain of the other. Sibling subdomains do not count."""
    a, b = (a or "").lower(), (b or "").lower()
    return bool(a) and bool(b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def _reads_only(method: str, resource_type: str, target_host: str, from_host: str, content_type: str) -> bool:
    """Whether the crawler lets this request out. GET, HEAD and OPTIONS always. Anything else only when a script
    sends it (never a navigation: that is a form being submitted) and it either goes to the page's own host (the
    site's own API, which career sites use to load their postings) or is JSON (which another site only receives
    after it agreed to in a CORS preflight). A form-encoded, multipart or text request to another site is what
    a page would use to make this crawler act somewhere else, and is refused."""
    if method.upper() in SAFE_METHODS:
        return True
    if resource_type not in ("xhr", "fetch"):
        return False
    return _related(target_host, from_host) or "json" in (content_type or "").lower()


class BrowserPool:
    def __init__(self, concurrency: int = 6, nav_timeout: float = 20.0, settle: float = 2.0, enabled: bool = True,
                 guard: Callable[[str], Awaitable[None]] | None = None, max_wait: float = 20.0, quiet: float = 3.0,
                 resolver=resolve_public):
        self.concurrency = max(1, concurrency)
        self.nav_timeout = nav_timeout
        self.settle = settle
        self.max_wait = max(settle, max_wait)
        self.quiet = quiet
        self.enabled = enabled
        self.guard = guard
        self.resolver = resolver
        self.pages_rendered = 0
        self.paging_steps = 0               # "next" / "load more" controls pressed by render_list
        self.blocked_writes = 0             # requests refused by _reads_only (counted for the tests and the health view)
        self.unavailable_reason = "" if enabled else "disabled by configuration"
        self._sem = asyncio.Semaphore(self.concurrency)
        self._start_lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._proxy: GuardProxy | None = None
        self._user_agent = ""
        self._host_ok: dict[str, tuple[bool, float]] = {}      # origin -> (allowed, until when a refusal is remembered)
        self._dns: dict[tuple[str, int], tuple[float, object]] = {}

    @property
    def available(self) -> bool:
        return self.enabled and not self.unavailable_reason

    async def _resolve(self, host: str, port: int):
        """The guard proxy's resolver with a short memory. A page opens dozens of connections to a handful of
        hosts; each one used to wait for its own lookup, and one slow lookup stalled the page past its time. The
        remembered value is the address that was checked to be public, so nothing is trusted for longer than it
        was verified for, and a refusal is remembered only briefly (it may have been the resolver's fault)."""
        key = ((host or "").lower(), port)
        hit = self._dns.get(key)
        if hit and hit[0] > time.monotonic():
            if isinstance(hit[1], Exception):
                raise hit[1]
            return hit[1]
        try:
            answer = await asyncio.wait_for(self.resolver(host, port), 8.0)
        except Exception as exc:
            self._dns[key] = (time.monotonic() + DNS_RETRY_SECONDS, exc)
            raise
        if len(self._dns) > 2000:
            self._dns.clear()
        self._dns[key] = (time.monotonic() + DNS_TTL_SECONDS, answer)
        return answer

    async def start(self) -> bool:
        """Launch Chromium on first use. Returns False (with a reason) when the browser cannot be used."""
        if self._browser is not None:
            return True
        if not self.available:
            return False
        async with self._start_lock:
            if self._browser is not None:
                return True
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                self.unavailable_reason = "Playwright is not installed (pip install playwright && playwright install chromium)"
                return False
            try:
                self._proxy = GuardProxy(self._resolve)
                port = await self._proxy.start()
                # "<-loopback>" removes Chromium's built-in rule that sends localhost traffic around the proxy.
                options = dict(headless=True, proxy={"server": f"http://127.0.0.1:{port}", "bypass": "<-loopback>"})
                self._playwright = await async_playwright().start()
                try:
                    self._browser = await self._playwright.chromium.launch(**options)
                except Exception:
                    fallback = self._cached_chromium()
                    if not fallback:
                        raise
                    self._browser = await self._playwright.chromium.launch(executable_path=fallback, **options)
                page = await self._browser.new_page()
                self._user_agent = (await page.evaluate("navigator.userAgent")) + " usa-ai-job-finder/1.0"
                await page.close()
            except Exception as exc:
                self.unavailable_reason = f"Chromium could not start ({type(exc).__name__}); run: playwright install chromium"
                await self.aclose()
                return False
        return True

    @staticmethod
    def _cached_chromium() -> str:
        hits = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))
        return hits[-1] if hits else ""

    async def aclose(self) -> None:
        browser, playwright, proxy = self._browser, self._playwright, self._proxy
        self._browser = self._playwright = self._proxy = None
        for closer in (getattr(browser, "close", None), getattr(playwright, "stop", None), getattr(proxy, "close", None)):
            if closer:
                try:
                    await closer()
                except Exception:
                    pass

    async def _host_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False
        key = f"{parts.scheme}://{parts.netloc}"
        known = self._host_ok.get(key)
        if known is None or (not known[0] and known[1] < time.monotonic()):
            try:
                if self.guard:
                    await self.guard(url)
                known = (True, 0.0)
            except Exception:
                # A refusal is kept for a minute, not for the life of the process: the guard also fails when the
                # name could not be resolved just now, and that must not shut a host out until the next restart.
                known = (False, time.monotonic() + 60.0)
            self._host_ok[key] = known
        return known[0]

    async def _route(self, route) -> None:
        request = route.request
        try:
            if request.resource_type in BLOCKED_RESOURCES or not await self._host_allowed(request.url):
                await route.abort()
                return
            if request.method.upper() not in SAFE_METHODS:
                headers = request.headers
                sender = urlsplit(headers.get("origin") or headers.get("referer") or "").hostname or ""
                if not _reads_only(request.method, request.resource_type, urlsplit(request.url).hostname or "", sender,
                                   headers.get("content-type", "")):
                    self.blocked_writes += 1
                    await route.abort("blockedbyclient")
                    return
            await route.continue_()
        except Exception:
            pass      # the page was closed while the request was pending

    async def render(self, url: str) -> RenderResult:
        """Load a page with JavaScript enabled and return the rendered HTML and the text it shows."""
        if not await self.start():
            return RenderResult(error=self.unavailable_reason or "browser unavailable")
        if not await self._host_allowed(url):
            return RenderResult(error="URL rejected by the network safety check")
        async with self._sem:
            try:
                return await asyncio.wait_for(self._render(url), timeout=self.nav_timeout + self.max_wait + 8)
            except asyncio.TimeoutError:
                return RenderResult(error="timeout")
            except Exception as exc:
                return RenderResult(error=type(exc).__name__)

    async def render_list(self, url: str, steps: int = 0, pause: float = 1.0) -> RenderResult:
        """render() for a page that lists jobs, followed through its own paging control: after the first view the
        list's "Next" / "Load more" button is pressed up to `steps` times and the link addresses of every further
        part are returned in RenderResult.links (the first view is RenderResult.html, as always).

        This is the only thing the crawler ever presses, and it stays a reader: the control is a button whose whole
        label is a paging label (PAGING_LABEL), outside any form and not a submit button; nothing is typed; the
        request rules of render() apply to whatever the press sets off (a form submission and a write to another site
        are refused, hosts that are never fetched are not contacted); `pause` seconds (never less than one) pass
        before every press, so the site gets one request series at a time at the crawler's pace; paging stops when
        there is no such control, when a press changes nothing, or when the page leaves its site."""
        steps = max(0, min(int(steps), MAX_PAGING_STEPS))
        if steps == 0:
            return await self.render(url)
        if not await self.start():
            return RenderResult(error=self.unavailable_reason or "browser unavailable")
        if not await self._host_allowed(url):
            return RenderResult(error="URL rejected by the network safety check")
        pause = max(1.0, pause)
        async with self._sem:
            try:
                limit = self.nav_timeout + self.max_wait + 8 + steps * (pause + STEP_WAIT + STEP_STILL + 2)
                return await asyncio.wait_for(self._render(url, steps, pause), timeout=limit)
            except asyncio.TimeoutError:
                return RenderResult(error="timeout")
            except Exception as exc:
                return RenderResult(error=type(exc).__name__)

    async def _list_changed(self, page, before: str) -> bool:
        """Whether the list shows something else than `before` and has come to rest (STEP_STILL without a change)."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        last, since, moved = before, started, False
        while loop.time() - started < STEP_WAIT + STEP_STILL:
            await asyncio.sleep(0.3)
            try:
                state = await page.evaluate(_LIST_STATE)
            except Exception:
                state = last                             # the document is being replaced: look again
            now = loop.time()
            if state != last:
                last, since, moved = state, now, True
            elif moved and now - since >= STEP_STILL:
                return True
            if not moved and now - started >= STEP_WAIT:
                return False
        return moved

    async def _page_on(self, page, steps: int, pause: float) -> tuple[list[str], int]:
        """Press the list's own paging control up to `steps` times. Returns (link addresses seen after the presses, presses)."""
        home = urlsplit(page.url).hostname or ""
        links: dict[str, None] = {}
        pressed = 0
        for _ in range(steps):
            try:
                before = await page.evaluate(_LIST_STATE)
                label = await page.evaluate(_PAGING_CONTROL, PAGING_LABEL)
            except Exception:
                break
            if not label:
                break
            await asyncio.sleep(pause)                   # the crawler's pace: a pause before every further request to the site
            try:
                if not await page.evaluate(_PRESS):
                    break
            except Exception:
                break
            pressed += 1
            self.paging_steps += 1
            if not await self._list_changed(page, before):
                break
            if not _related(urlsplit(page.url).hostname or "", home) or not await self._host_allowed(page.url):
                break                                    # the control led away from the site: nothing there is read
            try:
                found = await page.evaluate(_LIST_LINKS, MAX_LIST_LINKS)
            except Exception:
                break
            for href in found if isinstance(found, list) else []:
                if isinstance(href, str) and len(href) <= 2000:
                    links.setdefault(href)
            if len(links) >= MAX_LIST_LINKS:
                break
        return list(links)[:MAX_LIST_LINKS], pressed

    @staticmethod
    def _watch_network(page) -> dict:
        """Counts the page's requests in flight and notes when the last one started or ended."""
        loop = asyncio.get_running_loop()
        net = {"at": loop.time(), "open": 0}

        def begin(_):
            net["open"] += 1
            net["at"] = loop.time()

        def end(_):
            net["open"] = max(0, net["open"] - 1)
            net["at"] = loop.time()
        page.on("request", begin)
        page.on("requestfinished", end)
        page.on("requestfailed", end)
        return net

    async def _settled(self, page, net: dict) -> None:
        """Wait until the page has shown what it is going to show (see the module text for the rules)."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        last, changed = -1, started
        while True:
            try:
                length, posting = await page.evaluate(_LOOK)
            except Exception:
                length, posting = last, False          # the document is being replaced (a client-side redirect): look again
            now = loop.time()
            if length != last:
                last, changed = length, now
            still = now - changed
            if posting and still >= 0.4:
                return
            if length >= FULL_PAGE_CHARS and still >= self.settle:
                return
            if net["open"] == 0 and still >= self.quiet and now - net["at"] >= self.quiet:
                return                                 # nothing is loading and nothing has changed: this is all there is
            if now - started >= self.max_wait:
                return
            await asyncio.sleep(0.4)

    async def _render(self, url: str, steps: int = 0, pause: float = 1.0) -> RenderResult:
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        context = await self._browser.new_context(
            user_agent=self._user_agent, service_workers="block", accept_downloads=False, locale="en-US",
            viewport={"width": 1280, "height": 900})
        try:
            await context.route("**/*", self._route)
            if hasattr(context, "route_web_socket"):
                async def _close_socket(ws):
                    await ws.close()
                await context.route_web_socket("**/*", _close_socket)
            page = await context.new_page()
            page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.dismiss()))
            net = self._watch_network(page)
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout * 1000)
            except PlaywrightTimeout:
                return RenderResult(error="timeout")
            status = response.status if response else 0
            await self._settled(page, net)
            title = await page.title()
            html = (await page.content())[:MAX_HTML_CHARS]
            try:
                text = await page.evaluate(_TEXT, MAX_TEXT_CHARS)
            except Exception:
                text = ""
            final_url = page.url
            self.pages_rendered += 1
            if status in (401, 403, 429, 503) or _CHALLENGE.search(title or ""):
                return RenderResult(status=status, url=final_url, error="blocked")   # never worked around
            if not await self._host_allowed(final_url):
                return RenderResult(status=status, url=final_url, error="redirected to a rejected address")
            result = RenderResult(status=status, url=final_url, html=html, text=text if isinstance(text, str) else "")
            if steps > 0:
                links, pressed = await self._page_on(page, steps, pause)
                result.links, result.steps = tuple(links), pressed
            return result
        finally:
            await context.close()
