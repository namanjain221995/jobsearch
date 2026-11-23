"""Background worker (arq). Start with:   arq app.portal.worker.WorkerSettings

One worker process owns the search engine: the job index refresh, the headless browser pool and the
per-source rate limits all live here, so they are shared by every search it runs at the same time."""
import asyncio
import json
import logging
import time
import uuid

import httpx
from arq.connections import RedisSettings
from arq.cron import cron
from arq.worker import func
from sqlalchemy import select

from ..autoapply import runner as autoapply_runner
from ..autoapply.browser import ApplyBrowser
from ..config import get_settings
from ..netguard import assert_public_url
from ..providers.base import RobotsCache
from ..providers import registry
from ..search_engine import SearchEngine
from . import cache as cache_module
from . import logs
from .cache import AnalyticsCache
from .db import make_engine, make_sessionmaker
from .index_pg import PgJobIndex
from .models import AutoApplyRun, SearchRun
from .queue import WORKER_STATUS_KEY, auto_apply_job_id, job_id
from .search_runner import RunnerDeps, execute_search, recover_interrupted
from .services import settings as settings_service

log = logging.getLogger("jobfinder.worker")


async def _heartbeat(ctx: dict) -> None:
    engine: SearchEngine = ctx["engine"]
    started = time.time()
    ticks = 0
    while True:
        try:
            index = await engine.index.index_stats()
            status = {"online": True, "started_at": started, "last_seen": time.time(), "active_searches": len(ctx["active"]),
                      "browser": {"enabled": engine.settings.browser_enabled, "available": engine.browser.available,
                                  "running": getattr(engine.browser, "_browser", None) is not None,
                                  "concurrency": engine.browser.concurrency, "note": engine.browser.unavailable_reason},
                      "index": index, "sources": sorted(engine.specs)}
            await ctx["redis"].set(WORKER_STATUS_KEY, json.dumps(status), ex=45)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("heartbeat failed: %s", type(exc).__name__)
        ticks += 1
        if ticks % 3 == 0:                                  # every 30 s
            try:
                await _hand_over_runs(ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("run hand-over failed: %s", type(exc).__name__)
        await asyncio.sleep(10)


async def _hand_over_runs(ctx: dict) -> None:
    """Resume or close runs whose worker died, then give every waiting run to the queue. A queue entry is named
    after the run and its attempt, so handing the same run over again changes nothing."""
    resumed, closed = await recover_interrupted(ctx["sessions"], exclude=frozenset(ctx["active"]))
    if resumed or closed:
        log.warning("interrupted search runs: %s resumed, %s closed", len(resumed), closed)
    async with ctx["sessions"]() as db:
        waiting = (await db.execute(select(SearchRun.id, SearchRun.attempts).where(SearchRun.status == "queued").order_by(SearchRun.created_at))).all()      # oldest first: first come, first served
    for run_id, attempts in waiting:
        if str(run_id) not in ctx["active"]:
            await ctx["redis"].enqueue_job("run_search", str(run_id), _job_id=job_id(run_id, attempts or 0))
    # Auto-apply runs the API could not queue (Redis was down) are handed over the same way.
    async with ctx["sessions"]() as db:
        pending = (await db.execute(select(AutoApplyRun.id, AutoApplyRun.status, AutoApplyRun.attempts)
                                    .where(AutoApplyRun.status.in_(("queued", "approved"))))).all()
    for run_id, status, attempts in pending:
        kind = "prepare" if status == "queued" else "submit"
        await ctx["redis"].enqueue_job(f"auto_apply_{kind}", str(run_id), _job_id=auto_apply_job_id(kind, run_id, attempts or 0))


async def startup(ctx: dict) -> None:
    settings = get_settings()
    logs.configure(settings.log_format, settings.log_level)
    ctx["db_engine"] = make_engine(settings.database_url, pool_size=5)
    sessions = make_sessionmaker(ctx["db_engine"])
    # Commits made by search runs retire cached dashboard answers, exactly like commits made by the API.
    cache_module.install(AnalyticsCache.for_database(ctx["redis"], settings.database_url, settings.analytics_cache_seconds))
    index = await PgJobIndex(settings.postgres_dsn).connect()
    engine = SearchEngine(settings, index)
    ctx.update(settings=settings, sessions=sessions, index=index, engine=engine, active=set(),
               deps=RunnerDeps(sessions=sessions, engine=engine, settings=settings), all_specs=dict(engine.specs))
    await index.prune()
    ctx["auto_apply"] = autoapply_deps(settings, sessions)
    await autoapply_runner.recover(ctx["auto_apply"])
    await _hand_over_runs(ctx)                              # also picks up runs queued while no worker was up
    if settings.browser_enabled:
        ctx["browser_task"] = asyncio.create_task(engine.browser.start())
    if settings.prewarm_on_start:
        engine.prewarm()
    ctx["heartbeat"] = asyncio.create_task(_heartbeat(ctx))
    log.warning("worker ready: %s sources, %s seed employers", len(engine.specs), len(engine.seeds))


async def shutdown(ctx: dict) -> None:
    for name in ("heartbeat", "browser_task"):
        task = ctx.get(name)
        if task:
            task.cancel()
    try:
        await ctx["redis"].delete(WORKER_STATUS_KEY)
    except Exception:
        pass
    await ctx["engine"].aclose()
    await ctx["index"].close()
    await ctx["db_engine"].dispose()


async def run_search(ctx: dict, run_id: str) -> None:
    engine: SearchEngine = ctx["engine"]
    async with ctx["sessions"]() as db:                      # source switches made by the Admin apply to the next run
        portal = await settings_service.load(db, ctx["settings"])
    off = set(portal.disabled_sources)
    engine.specs = {name: spec for name, spec in ctx["all_specs"].items() if name not in off}
    ctx["active"].add(run_id)
    try:
        await execute_search(uuid.UUID(run_id), ctx["deps"])
    finally:
        ctx["active"].discard(run_id)


def autoapply_deps(settings, sessions) -> autoapply_runner.RunnerDeps:
    browser = ApplyBrowser(concurrency=settings.auto_apply_browser_concurrency)
    robots = RobotsCache()
    client = httpx.AsyncClient(follow_redirects=False, timeout=12.0)

    async def robots_allowed(url: str) -> bool:
        try:
            await assert_public_url(url)
            return await robots.allowed(client, url)
        except Exception:
            return False
    return autoapply_runner.RunnerDeps(sessions=sessions, settings=settings, browser=browser,
                                       engine_factory=lambda: autoapply_runner.build_engine(settings), robots_allowed=robots_allowed)


async def auto_apply_prepare(ctx: dict, run_id: str) -> None:
    if not ctx["settings"].auto_apply_enabled:
        return
    await autoapply_runner.prepare(uuid.UUID(run_id), ctx["auto_apply"])


async def auto_apply_submit(ctx: dict, run_id: str) -> None:
    if not ctx["settings"].auto_apply_enabled:
        return
    await autoapply_runner.submit(uuid.UUID(run_id), ctx["auto_apply"])


async def auto_apply_cleanup(ctx: dict) -> None:
    removed = await autoapply_runner.cleanup(ctx["auto_apply"])
    if removed:
        log.warning("auto-apply: %s screenshots past retention deleted", removed)


_settings = get_settings()


class WorkerSettings:
    functions = [func(run_search, name="run_search", max_tries=1, timeout=max(3600, _settings.worker_job_timeout_seconds), keep_result=60),
                 # never retried by arq: a submission must not be repeated (the runner decides about every outcome)
                 func(auto_apply_prepare, name="auto_apply_prepare", max_tries=1, timeout=int(_settings.auto_apply_run_timeout_seconds) + 120, keep_result=60),
                 func(auto_apply_submit, name="auto_apply_submit", max_tries=1, timeout=int(_settings.auto_apply_run_timeout_seconds) + 120, keep_result=60)]
    cron_jobs = [cron(auto_apply_cleanup, hour={3}, minute={17}, run_at_startup=False)]
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    max_jobs = max(1, _settings.worker_max_searches)
    job_timeout = max(3600, _settings.worker_job_timeout_seconds)
    on_startup = startup
    on_shutdown = shutdown
    health_check_interval = 30
    allow_abort_jobs = False
    retry_jobs = False          # a stopped run is put back by the application itself (search_runner), with its own attempt limit
