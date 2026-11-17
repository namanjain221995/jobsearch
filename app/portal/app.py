"""Portal API (FastAPI). Run with:   uvicorn app.portal.app:app --host 127.0.0.1 --port 8010

The browser never talks to this server directly: the Next.js app proxies /api/* to it, so the session
cookie is same-origin. Searches are only enqueued here; the worker process (app.portal.worker) runs them."""
import logging
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis_asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..config import Settings, get_settings
from ..job_verifier import PageVerifier
from . import cache as cache_module
from . import logs
from .cache import AnalyticsCache
from .db import make_engine, make_sessionmaker
from .queue import SearchQueue
from .routers import admin, assignments, auth, invitations, jobs, searches, teams, users
from .security import RateLimiter
from .services.emailer import Emailer

log = logging.getLogger("jobfinder.api")
access = logging.getLogger("jobfinder.request")


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
        app.state.queue = queue if queue is not None else SearchQueue(settings)
        if queue is None:
            await app.state.queue.connect()
        app.state.http = httpx.AsyncClient(follow_redirects=False, limits=httpx.Limits(max_connections=20))
        app.state.verifier = PageVerifier(app.state.http, browser=None)      # used for URL import / re-verification
        yield
        cache_module.install(None)
        await app.state.http.aclose()
        if queue is None:
            await app.state.queue.close()
        if client is not None:
            await client.aclose()
        await app.state.db_engine.dispose()

    app = FastAPI(title="AI Job Finder Portal API", version="2.0", lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
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

    for module in (auth, invitations, teams, users, jobs, searches, assignments, admin):
        app.include_router(module.router)
    return app


app = create_app()
