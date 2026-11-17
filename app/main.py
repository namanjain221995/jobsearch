"""FastAPI application: API + static single-page frontend."""
import asyncio
import csv
import io
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, get_settings
from .database import Database
from .resume_parser import ResumeError, extract_text, heuristic_profile
from .schemas import SearchRequest
from .search_engine import STAGES, SearchEngine, SearchState

log = logging.getLogger("jobfinder")
STATIC_DIR = BASE_DIR / "static"
SEARCH_TTL_SECONDS = 2 * 3600


class Registry:
    """In-memory state: uploaded resume text (TTL, never written to disk) and running searches."""

    def __init__(self):
        self.resumes: dict[str, tuple[float, str, str]] = {}
        self.searches: dict[str, SearchState] = {}
        self.hits: dict[str, deque] = defaultdict(deque)

    def purge(self, resume_ttl: int) -> None:
        now = time.time()
        for rid in [r for r, (ts, _, _) in self.resumes.items() if now - ts > resume_ttl]:
            del self.resumes[rid]
        for sid in [s for s, st in self.searches.items() if now - st.created > SEARCH_TTL_SECONDS and st.status != "running"]:
            del self.searches[sid]

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.time()
        q = self.hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.registry = Registry()
    app.state.db = Database(settings.sqlite_path)
    app.state.db.prune()
    app.state.engine = engine = SearchEngine(settings, app.state.db)
    startup: list[asyncio.Task] = []
    if settings.browser_enabled:
        startup.append(asyncio.create_task(engine.browser.start()))      # launch Chromium once, reuse for every search
    if settings.prewarm_on_start:
        engine.prewarm()                                                   # fill the local job index in the background
    yield
    for task in startup:
        task.cancel()
    for st in app.state.registry.searches.values():
        if st.task and not st.task.done():
            st.task.cancel()
    await app.state.engine.aclose()
    app.state.db.close()


app = FastAPI(title="AI Job Finder - United States", lifespan=lifespan, docs_url=None, redoc_url=None)


def error(status: int, message: str, detail: str = "") -> JSONResponse:
    return JSONResponse({"error": message, "detail": detail}, status_code=status)


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return error(exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    parts = [f"{'.'.join(str(p) for p in e.get('loc', [])[1:]) or 'request'}: {e.get('msg', 'invalid')}" for e in exc.errors()[:6]]
    return error(422, "Invalid input", "; ".join(parts))


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    log.error("Unhandled error: %s", type(exc).__name__)     # type only: never log request or resume content
    return error(500, "Internal server error")


@app.middleware("http")
async def guard(request: Request, call_next):
    settings, registry = request.app.state.settings, request.app.state.registry
    ip = request.client.host if request.client else "unknown"
    if request.url.path.startswith("/api/") and not registry.allow(f"req:{ip}", settings.requests_per_minute, 60):
        return error(429, "Too many requests", "Slow down and try again in a minute.")
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def get_search(request: Request, search_id: str) -> SearchState:
    state = request.app.state.registry.searches.get(search_id)
    if not state:
        raise HTTPException(404, "Search not found or expired")
    return state


# ---------------------------------------------------------------- endpoints
@app.get("/api/health")
async def health(request: Request):
    s = request.app.state.settings
    engine = request.app.state.engine
    return {
        "status": "ok",
        "providers": {
            "openai": {"configured": bool(s.openai_api_key), "model": s.openai_model},
            "claude": {"configured": bool(s.anthropic_api_key), "model": s.anthropic_model},
        },
        "sources": [{"name": spec.label, "kind": spec.kind, "note": spec.note} for spec in engine.specs.values()],
        "seed_boards": len(engine.seeds),
        "max_results": s.search_max_results,
        "index": await asyncio.to_thread(request.app.state.db.index_stats),
        "browser": {"enabled": s.browser_enabled, "available": engine.browser.available,
                    "running": engine.browser._browser is not None, "concurrency": engine.browser.concurrency,
                    "note": engine.browser.unavailable_reason},
        "database": request.app.state.db.counts(),
    }


@app.post("/api/resume/parse")
async def parse_resume(request: Request, file: UploadFile = File(...)):
    settings, registry = request.app.state.settings, request.app.state.registry
    registry.purge(settings.resume_ttl_seconds)
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > settings.max_upload_bytes + 64 * 1024:
        return error(413, "File is too large", f"Maximum size is {settings.max_upload_bytes // (1024 * 1024)} MB.")
    data = await file.read(settings.max_upload_bytes + 1)
    filename = (file.filename or "resume")[:120]
    try:
        text, truncated = await asyncio.to_thread(extract_text, filename, data, settings.max_upload_bytes)
    except ResumeError as exc:
        status = 413 if "too large" in str(exc).lower() else 400
        return error(status, str(exc))
    finally:
        del data
        await file.close()
    resume_id = secrets.token_urlsafe(18)
    registry.resumes[resume_id] = (time.time(), text, filename)
    preview = heuristic_profile(text)
    return {
        "resume_id": resume_id, "filename": filename, "characters": len(text), "truncated": truncated,
        "detected_skills": preview.skills[:40], "detected_titles": preview.job_titles[:6],
        "expires_in_seconds": settings.resume_ttl_seconds,
        "notice": "Resume text is kept in memory only and may be sent to the configured AI providers during a search.",
    }


@app.post("/api/jobs/search", status_code=202)
async def start_search(request: Request, body: SearchRequest):
    settings, registry, engine = request.app.state.settings, request.app.state.registry, request.app.state.engine
    registry.purge(settings.resume_ttl_seconds)
    entry = registry.resumes.get(body.resume_id)
    if not entry:
        return error(400, "Resume not found", "Upload your resume again; uploads expire after an hour.")
    ip = request.client.host if request.client else "unknown"
    if sum(1 for s in registry.searches.values() if s.status in ("queued", "running")) >= settings.max_concurrent_searches:
        return error(429, "Too many searches are running", "Wait for a running search to finish or cancel it.")
    if not registry.allow(f"search:{ip}", settings.searches_per_10_minutes, 600):
        return error(429, "Search limit reached", "Try again in a few minutes.")
    resume_text = entry[1]
    state = SearchState(secrets.token_urlsafe(12), body.result_count or settings.search_max_results)
    registry.searches[state.id] = state

    async def runner():
        try:
            await engine.run(state, body, resume_text)
            db = request.app.state.db
            await asyncio.to_thread(db.save_jobs, state.results)
            await asyncio.to_thread(db.save_search, state.id, state.status, body.model_dump(exclude={"resume_id"}),
                                    {**state.stats, "timings": state.timings})
        except asyncio.CancelledError:
            state.status = "cancelled"
            state.message = "Search cancelled."
            state.emit("done", state.message)
        except Exception as exc:
            log.error("Search failed: %s", type(exc).__name__)
            state.status = "failed"
            state.message = "The search failed unexpectedly. No results were fabricated."
            state.emit("done", state.message)

    state.task = asyncio.create_task(runner())
    return {"search_id": state.id, "status": state.status}


@app.get("/api/jobs/search/{search_id}/status")
async def search_status(request: Request, search_id: str):
    return get_search(request, search_id).summary()


@app.get("/api/jobs/search/{search_id}/results")
async def search_results(request: Request, search_id: str):
    state = get_search(request, search_id)
    return {**state.summary(), "jobs": [j.public() for j in state.results]}


@app.get("/api/jobs/search/{search_id}/events")
async def search_events(request: Request, search_id: str):
    state = get_search(request, search_id)

    async def stream():
        sent = 0
        yield f"event: stages\ndata: {json.dumps([{'id': s, 'label': l} for s, l in STAGES])}\n\n"
        while True:
            while sent < len(state.events):
                event = state.events[sent]
                sent += 1
                payload = {**event, "status": state.status if event["stage"] == "done" else event["status"],
                           "warnings": state.warnings, "sources": state.sources}
                yield f"event: {'done' if event['stage'] == 'done' else 'progress'}\ndata: {json.dumps(payload)}\n\n"
                if event["stage"] == "done":
                    return
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.3)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/jobs/search/{search_id}/cancel")
async def cancel_search(request: Request, search_id: str):
    state = get_search(request, search_id)
    if state.task and not state.task.done():
        state.task.cancel()
    return {"search_id": state.id, "status": "cancelling" if state.status in ("queued", "running") else state.status}


CSV_FIELDS = ["score", "title", "company", "location", "work_mode", "salary", "experience_required",
              "authorization_status", "citizenship_requirement", "sponsorship", "eligibility_note", "posted_at",
              "source", "sources", "verified_at", "verification", "url", "apply_url", "matching_skills",
              "missing_skills", "reason", "experience_comparison", "score_source"]


def _csv_cell(value) -> str:
    text = ", ".join(value) if isinstance(value, list) else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text     # CSV formula-injection guard


@app.get("/api/jobs/search/{search_id}/export.csv")
async def export_csv(request: Request, search_id: str):
    state = get_search(request, search_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_FIELDS)
    for job in state.results:
        row = job.public()
        writer.writerow([_csv_cell(row.get(f, "")) for f in CSV_FIELDS])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="usa-jobs.csv"'})


@app.get("/api/jobs/search/{search_id}/export.json")
async def export_json(request: Request, search_id: str):
    state = get_search(request, search_id)
    body = json.dumps({**state.summary(), "jobs": [j.public() for j in state.results]}, indent=2)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="usa-jobs.json"'})


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
