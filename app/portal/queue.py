"""Hand-off of search runs to the background worker (arq on Redis).

The API only enqueues a run id; the worker process does the work. `InlineQueue` runs the same
coroutine inside the current process and exists for tests and single-process development only."""
import asyncio
import uuid

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from ..config import Settings

WORKER_STATUS_KEY = "jobfinder:worker:status"


def job_id(run_id, attempts: int) -> str:
    """One queue entry per run and attempt: a run handed over twice is started once; a resumed run gets a new entry."""
    return f"search:{run_id}:r{attempts}"


class SearchQueue:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: ArqRedis | None = None

    async def connect(self) -> bool:
        try:
            self.pool = await create_pool(RedisSettings.from_dsn(self.settings.redis_url))
            return True
        except Exception:
            self.pool = None
            return False

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.aclose()
            self.pool = None

    @property
    def available(self) -> bool:
        return self.pool is not None

    async def ping(self) -> bool:
        try:
            return bool(self.pool and await self.pool.ping())
        except Exception:
            return False

    async def enqueue(self, run_id: uuid.UUID) -> bool:
        """True when the worker queue accepted the run (or already holds it)."""
        if self.pool is None and not await self.connect():
            return False
        try:
            await self.pool.enqueue_job("run_search", str(run_id), _job_id=job_id(run_id, 0))
            return True
        except Exception:
            return False

    async def worker_status(self) -> dict | None:
        try:
            raw = await self.pool.get(WORKER_STATUS_KEY) if self.pool else None
        except Exception:
            return None
        if not raw:
            return None
        import json
        try:
            return json.loads(raw)
        except ValueError:
            return None


class InlineQueue:
    """Runs searches as tasks of the current event loop."""
    available = True

    def __init__(self, runner):
        self.runner = runner                 # async callable(run_id)
        self.tasks: set[asyncio.Task] = set()
        self.pool = None

    async def connect(self) -> bool:
        return True

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def ping(self) -> bool:
        return True

    async def enqueue(self, run_id: uuid.UUID) -> bool:
        task = asyncio.create_task(self.runner(run_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    async def worker_status(self) -> dict | None:
        return {"online": True, "inline": True}

    async def drain(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
