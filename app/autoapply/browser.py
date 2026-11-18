"""The application browser: the portal's honest Chromium (browser_pool.py policy) with a submission guard.

Same policy as the crawler: it identifies itself in the User-Agent, uses no stealth, never solves or works around a
CAPTCHA or bot check, sends every connection through netguard.GuardProxy (no localhost, no internal network), has
downloads and service workers off and gets a fresh cookie-less profile per run. Differences: it loads stylesheets
and images (the Admin reviews a screenshot) and it runs one Chromium per run (its own proxy, so the dry-run mode of
one run can never leak into another), at most AUTO_APPLY_BROWSER_CONCURRENCY at a time.

Dry run (`dry_run=True`), two independent layers:
1. the browser's request routing aborts, before anything is sent, every request that could submit or store data:
   any method other than GET/HEAD/OPTIONS (a GraphQL *query* is the only POST let through, because Ashby loads its
   form that way; a GraphQL mutation, a multipart or url-encoded body is always blocked), and every main-frame
   navigation after the form has loaded (a form with method=GET);
2. the guard proxy refuses state-changing methods of plain-http requests (`GuardProxy.block_submissions`).
Every blocked request is counted; `allowed_submissions` counts submission-like requests that went out, which is
zero in a dry run by construction and is asserted by the runner and the tests."""
import asyncio
import glob
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..netguard import GuardProxy, assert_public_url, resolve_public

log = logging.getLogger("jobfinder.autoapply")
BLOCKED_RESOURCES = {"media"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
UA_SUFFIX = " usa-ai-job-finder/1.0"


def is_graphql_query(post_data: str | None, content_type: str) -> bool:
    """A JSON GraphQL request whose every operation is a query (read-only by GraphQL's own rules)."""
    if not post_data or "json" not in (content_type or "").lower():
        return False
    try:
        data = json.loads(post_data)
    except ValueError:
        return False
    items = data if isinstance(data, list) else [data]
    if not items:
        return False
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("query"), str):
            return False
        text = re.sub(r"#[^\n]*", "", item["query"]).strip()
        if not (text.startswith("query") or text.startswith("{")) or re.search(r"\bmutation\b|\bsubscription\b", text):
            return False
    return True


def submission_like(method: str, post_data: str | None, content_type: str) -> bool:
    return method.upper() not in SAFE_METHODS and not is_graphql_query(post_data, content_type)


@dataclass
class SubmissionGuard:
    dry_run: bool
    armed: bool = False                    # set once the form has loaded: later main-frame navigations count as submissions
    blocked_requests: int = 0
    allowed_submissions: int = 0
    blocked: list[dict] = field(default_factory=list)
    submitted: list[dict] = field(default_factory=list)
    form_host: str = ""

    def report(self, proxy: GuardProxy | None = None) -> dict:
        return {"dry_run": self.dry_run, "blocked_requests": self.blocked_requests + (proxy.blocked_submissions if proxy else 0),
                "allowed_submissions": self.allowed_submissions, "blocked": self.blocked[:30]}


def _short(url: str) -> str:
    p = urlsplit(url)
    return f"{p.hostname or ''}{p.path[:120]}"


class ApplyBrowser:
    def __init__(self, *, resolver=resolve_public, host_check=assert_public_url, headless: bool = True,
                 nav_timeout: float = 30.0, concurrency: int = 1,
                 ignore_https_errors: bool = False):
        self.resolver = resolver
        self.host_check = host_check
        self.headless = headless
        self.ignore_https_errors = ignore_https_errors      # tests only (local fake server with a self-signed certificate)
        self.nav_timeout = nav_timeout
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self.unavailable_reason = ""

    @staticmethod
    def _cached_chromium() -> str:
        hits = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))
        return hits[-1] if hits else ""

    @asynccontextmanager
    async def session(self, *, dry_run: bool):
        """Yields (page, guard, proxy). One Chromium and one guard proxy, closed afterwards."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError("Playwright is not installed") from None
        async with self._sem:
            proxy = GuardProxy(self.resolver, block_submissions=dry_run)
            port = await proxy.start()
            playwright = browser = context = None
            guard = SubmissionGuard(dry_run=dry_run)
            host_ok: dict[str, bool] = {}
            try:
                playwright = await async_playwright().start()
                options = dict(headless=self.headless, proxy={"server": f"http://127.0.0.1:{port}", "bypass": "<-loopback>"})
                try:
                    browser = await playwright.chromium.launch(**options)
                except Exception:
                    fallback = self._cached_chromium()
                    if not fallback:
                        raise
                    browser = await playwright.chromium.launch(executable_path=fallback, **options)
                probe = await browser.new_page()
                user_agent = (await probe.evaluate("navigator.userAgent")) + UA_SUFFIX
                await probe.close()
                context = await browser.new_context(user_agent=user_agent, service_workers="block", accept_downloads=False,
                                                    locale="en-US", viewport={"width": 1280, "height": 900},
                                                    ignore_https_errors=self.ignore_https_errors)

                async def allowed_host(url: str) -> bool:
                    parts = urlsplit(url)
                    if parts.scheme not in ("http", "https") or not parts.hostname:
                        return parts.scheme in ("data", "blob", "about")
                    key = f"{parts.scheme}://{parts.netloc}"
                    if key not in host_ok:
                        try:
                            await self.host_check(url)
                            host_ok[key] = True
                        except Exception:
                            host_ok[key] = False
                    return host_ok[key]

                page_holder: dict = {}

                async def route(route_):
                    request = route_.request
                    try:
                        if request.resource_type in BLOCKED_RESOURCES or not await allowed_host(request.url):
                            await route_.abort()
                            return
                        content_type = (await request.all_headers()).get("content-type", "")
                        post = request.post_data if request.method.upper() not in SAFE_METHODS else None
                        is_sub = submission_like(request.method, post, content_type)
                        page = page_holder.get("page")
                        main_nav = (guard.armed and request.is_navigation_request() and page is not None
                                    and request.frame == page.main_frame)
                        if guard.dry_run and (is_sub or main_nav):
                            guard.blocked_requests += 1
                            guard.blocked.append({"method": request.method, "target": _short(request.url),
                                                  "kind": "navigation" if main_nav and not is_sub else request.resource_type})
                            await route_.abort("blockedbyclient")
                            return
                        if is_sub:
                            guard.allowed_submissions += 1
                            guard.submitted.append({"method": request.method, "target": _short(request.url)})
                        await route_.continue_()
                    except Exception:
                        pass                       # page closed while the request was pending

                await context.route("**/*", route)
                if hasattr(context, "route_web_socket"):
                    async def _close_socket(ws):
                        await ws.close()
                    await context.route_web_socket("**/*", _close_socket)
                page = await context.new_page()
                page_holder["page"] = page
                page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.dismiss()))
                page.set_default_timeout(15000)
                yield page, guard, proxy
            finally:
                for closer in (getattr(context, "close", None), getattr(browser, "close", None), getattr(playwright, "stop", None)):
                    if closer:
                        try:
                            await closer()
                        except Exception:
                            pass
                await proxy.close()


async def screenshot(page, max_bytes: int, max_height: int = 7000) -> bytes | None:
    """A JPEG of the page, at most `max_bytes`: quality is lowered first, then the captured height is reduced."""
    try:
        height = await page.evaluate("Math.max(document.body ? document.body.scrollHeight : 0, document.documentElement.scrollHeight)")
    except Exception:
        height = 900
    height = int(min(max(height or 900, 300), max_height))
    while height >= 300:
        for quality in (60, 45, 30):
            try:
                data = await page.screenshot(type="jpeg", quality=quality, full_page=True,
                                             clip={"x": 0, "y": 0, "width": 1280, "height": height})
            except Exception as exc:
                log.warning("screenshot failed: %s", type(exc).__name__)
                return None
            if len(data) <= max_bytes:
                return data
        height //= 2
    return None
