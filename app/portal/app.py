"""Portal API (FastAPI). Run with:   uvicorn app.portal.app:app --host 127.0.0.1 --port 8010

The browser never talks to this server directly: the Next.js app proxies /api/* to it, so the session
cookie is same-origin. Searches are only enqueued here; the worker process (app.portal.worker) runs them."""
import asyncio
import html
import logging
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis_asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse

from ..config import Settings, get_settings
from ..job_verifier import PageVerifier
from . import cache as cache_module
from . import logs
from .cache import AnalyticsCache
from .db import make_engine, make_sessionmaker
from .limits import RequestGuardMiddleware
from .queue import SearchQueue
from ..providers import aws_web_search
from ..providers.aws_web_search import AwsWebSearch
from .routers import admin, assignments, auth, auto_apply, candidates, invitations, jobs, salesforce, searches, teams, users, web_search
from .security import RateLimiter
from .services.emailer import Emailer
from .services.salesforce import SalesforceClient

log = logging.getLogger("jobfinder.api")
access = logging.getLogger("jobfinder.request")


def problem_page(status: int, message: str) -> str:
    """A small page for the one route people open directly in a browser tab (Open Job). No scripts, no outside files."""
    title = {401: "Sign in again", 403: "This job cannot be opened from here", 404: "Job not found",
             410: "This job is no longer assigned to you"}.get(status, "This job cannot be opened")
    hint = ('<p><a href="/login">Go to the sign-in page</a></p>' if status == 401
            else "<p>Close this tab and go back to the portal.</p>")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>body{{font:16px/1.5 system-ui,sans-serif;margin:0;display:grid;min-height:100vh;"
            f"place-items:center;background:#f6f7f9;color:#1c2330}}main{{max-width:28rem;padding:2rem;margin:1rem;background:#fff;"
            f"border:1px solid #dfe3ea;border-radius:12px}}h1{{font-size:1.25rem;margin:0 0 .5rem}}p{{margin:.5rem 0;color:#465063}}"
            f"a{{color:#1d4ed8}}@media (prefers-color-scheme:dark){{body{{background:#0f1319;color:#e7eaf0}}main{{background:#171c25;"
            f"border-color:#2a3140}}p{{color:#aab2c0}}a{{color:#8ab4ff}}}}</style></head><body><main><h1>{html.escape(title)}</h1>"
            f"<p>{html.escape(message)}</p>{hint}</main></body></html>")


def create_app(settings: Settings | None = None, *, queue=None, use_redis: bool = True) -> FastAPI:
    settings = settings or get_settings()
    logs.configure(settings.log_format, settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db_engine = make_engine(settings.database_url)
        app.state.sessions = make_sessionmaker(app.state.db_engine)
        client = None
        if use_redis:
            try:
                client = redis_asyncio.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
                await client.ping()
            except Exception:
                log.warning("Redis is not reachable: login throttling falls back to this process only; searches cannot be queued")
                client = None
        app.state.redis = client
        app.state.cache = AnalyticsCache.for_database(client, settings.database_url, settings.analytics_cache_seconds)
        cache_module.install(app.state.cache)
        app.state.limiter = RateLimiter(client)
        app.state.emailer = Emailer(settings)
        app.state.salesforce = SalesforceClient(settings)                    # read-only; signs in on first use
        app.state.salesforce_cache = {}                                      # identical candidate searches, for a few seconds
        app.state.queue = queue if queue is not None else SearchQueue(settings)
        if queue is None:
            await app.state.queue.connect()
        app.state.http = httpx.AsyncClient(follow_redirects=False, limits=httpx.Limits(max_connections=20))
        app.state.verifier = PageVerifier(app.state.http, browser=None)      # used for URL import / re-verification
        # AWS web search for the managers' endpoint; its monthly counter is the same PostgreSQL row the workers use.
        app.state.aws_search = AwsWebSearch(settings, aws_web_search.quota_for(app.state.db_engine, settings))
        background: set[asyncio.Task] = set()

        async def quietly(work) -> None:
            try:
                await work
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("background task failed: %s", type(exc).__name__)

        def spawn(work) -> None:
            """Run a coroutine after the response went out (mail delivery). Cancelled when the server stops."""
            task = asyncio.create_task(quietly(work))
            background.add(task)
            task.add_done_callback(background.discard)

        app.state.spawn = spawn
        yield
        for task in list(background):
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        cache_module.install(None)
        await app.state.salesforce.aclose()
        await app.state.http.aclose()
        if queue is None:
            await app.state.queue.close()
        if client is not None:
            await client.aclose()
        await app.state.db_engine.dispose()

    # The interactive docs load scripts from a CDN and publish the whole route map: off in production unless asked for.
    docs = settings.api_docs if settings.api_docs is not None else not settings.is_production
    app = FastAPI(title="AI Job Finder Portal API", version="2.0", lifespan=lifespan, redoc_url=None,
                  docs_url="/api/docs" if docs else None, openapi_url="/api/openapi.json" if docs else None)
    app.add_middleware(RequestGuardMiddleware, settings=settings)       # added first: it runs inside the request log below

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.method == "GET" and request.url.path.endswith("/open-job") and "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(problem_page(exc.status_code, str(exc.detail)), status_code=exc.status_code,
                                headers={"Cache-Control": "no-store"})          # Open Job runs in a new browser tab
        return JSONResponse({"error": str(exc.detail), "detail": ""}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        fields, parts = {}, []
        for e in exc.errors()[:12]:
            name = ".".join(str(p) for p in e.get("loc", [])[1:]) or "request"
            message = str(e.get("msg", "invalid")).removeprefix("Value error, ")
            fields[name] = message
            parts.append(f"{name}: {message}")
        return JSONResponse({"error": "Invalid input", "detail": "; ".join(parts), "fields": fields}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.error("Unhandled error: %s", type(exc).__name__, exc_info=not settings.is_production,
                  extra={"request_id": getattr(request.state, "request_id", "")})
        return JSONResponse({"error": "Internal server error", "detail": ""}, status_code=500)

    @app.middleware("http")
    async def headers_and_request_log(request: Request, call_next):
        started = time.perf_counter()
        request.state.request_id = uuid.uuid4().hex[:16]
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
        finally:
            route = getattr(request.scope.get("route"), "path", "") or "unmatched"       # template: no ids, no tokens
            user_id, role = getattr(request.state, "actor", (None, None))
            access.log(logging.DEBUG if route == "/api/health" else logging.INFO, "request", extra={
                "request_id": request.state.request_id, "method": request.method, "route": route, "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1), "user_id": user_id, "role": role})
        response.headers.setdefault("X-Request-ID", request.state.request_id)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    for module in (auth, invitations, teams, users, candidates, jobs, searches, assignments, salesforce, admin, web_search, auto_apply):
        app.include_router(module.router)
    return app


app = create_app()
