"""Headless-browser crawler pool (Playwright + Chromium).

Used only for pages that need JavaScript to show their content. Several pages render at the same
time (bounded). It behaves as a plain, honest browser:
- identifies itself in the User-Agent, never uses stealth plugins, never solves or bypasses
  CAPTCHAs / bot challenges (a challenge page is reported as "blocked" and dropped);
- all of its traffic goes through a local guard proxy (netguard.GuardProxy), so no page, redirect, iframe or
  script can make it contact localhost or an internal network;
- images, media, fonts and stylesheets are not downloaded (speed), downloads and service workers are off;
- each page gets a fresh, cookie-less context that is destroyed afterwards.
Playwright is optional: without it the pool reports itself unavailable and the app keeps working."""
import asyncio
import glob
import os
import re
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from .netguard import GuardProxy

BLOCKED_RESOURCES = {"image", "media", "font", "stylesheet"}
MAX_HTML_CHARS = 3_000_000
_CHALLENGE = re.compile(
    r"(just a moment|attention required|verify(ing)? you are (a )?human|are you a robot|captcha|"
    r"access denied|unusual traffic|checking your browser|pardon our interruption)", re.I)


@dataclass
class RenderResult:
    status: int = 0
    url: str = ""
    html: str = ""
    error: str = ""          # "" on success; otherwise a short reason ("blocked", "timeout", ...)


class BrowserPool:
    def __init__(self, concurrency: int = 6, nav_timeout: float = 20.0, settle: float = 3.0, enabled: bool = True,
                 guard: Callable[[str], Awaitable[None]] | None = None):
        self.concurrency = max(1, concurrency)
        self.nav_timeout = nav_timeout
        self.settle = settle
        self.enabled = enabled
        self.guard = guard
        self.pages_rendered = 0
        self.unavailable_reason = "" if enabled else "disabled by configuration"
        self._sem = asyncio.Semaphore(self.concurrency)
        self._start_lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._proxy: GuardProxy | None = None
        self._user_agent = ""
        self._host_ok: dict[str, bool] = {}

    @property
    def available(self) -> bool:
        return self.enabled and not self.unavailable_reason

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
                self._proxy = GuardProxy()
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
        if key not in self._host_ok:
            try:
                if self.guard:
                    await self.guard(url)
                self._host_ok[key] = True
            except Exception:
                self._host_ok[key] = False
        return self._host_ok[key]

    async def _route(self, route) -> None:
        request = route.request
        try:
            if request.resource_type in BLOCKED_RESOURCES or not await self._host_allowed(request.url):
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            pass      # the page was closed while the request was pending

    async def render(self, url: str) -> RenderResult:
        """Load a page with JavaScript enabled and return the rendered HTML."""
        if not await self.start():
            return RenderResult(error=self.unavailable_reason or "browser unavailable")
        if not await self._host_allowed(url):
            return RenderResult(error="URL rejected by the network safety check")
        async with self._sem:
            try:
                return await asyncio.wait_for(self._render(url), timeout=self.nav_timeout + self.settle + 8)
            except asyncio.TimeoutError:
                return RenderResult(error="timeout")
            except Exception as exc:
                return RenderResult(error=type(exc).__name__)

    async def _render(self, url: str) -> RenderResult:
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
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout * 1000)
            except PlaywrightTimeout:
                return RenderResult(error="timeout")
            status = response.status if response else 0
            try:
                await page.wait_for_load_state("networkidle", timeout=self.settle * 1000)
            except PlaywrightTimeout:
                pass
            title = await page.title()
            html = (await page.content())[:MAX_HTML_CHARS]
            final_url = page.url
            self.pages_rendered += 1
            if status in (401, 403, 429, 503) or _CHALLENGE.search(title or ""):
                return RenderResult(status=status, url=final_url, error="blocked")   # never worked around
            if not await self._host_allowed(final_url):
                return RenderResult(status=status, url=final_url, error="redirected to a rejected address")
            return RenderResult(status=status, url=final_url, html=html)
        finally:
            await context.close()
