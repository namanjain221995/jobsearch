"""Read-through Redis cache for the heavy dashboard queries (organisation and team analytics).

How stale numbers are prevented
- Every entry is stored under the current data version, a single Redis counter.
- A database session that commits a change to a table the dashboards read raises that counter before
  `commit()` returns (PortalSession in db.py). Entries of an older version are never read again; they expire.
- The counter is raised only after the commit, so an entry can never hold data older than its version.
- The time-to-live bounds staleness for changes this code cannot see: rows edited directly in PostgreSQL,
  the crawler's index tables (the "postings indexed" figures), and a Redis failure at the moment of a commit.

Authorization always runs before a lookup, and the key holds every parameter that shapes the answer.
Without Redis, or while Redis is failing, every request is computed from PostgreSQL."""
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, TypeVar

from pydantic import BaseModel

log = logging.getLogger("jobfinder.cache")
T = TypeVar("T", bound=BaseModel)

# Tables whose rows show up in dashboard numbers, names or activity lists.
TRACKED_TABLES = frozenset({
    "job_assignments", "application_events", "job_postings", "users", "teams", "team_lead_assignments",
    "member_profiles", "resumes", "search_runs", "search_targets", "search_results", "team_job_queue",
})


class AnalyticsCache:
    def __init__(self, redis, ttl_seconds: int = 60, namespace: str = "default"):
        self.redis = redis
        self.ttl = max(0, int(ttl_seconds))
        self.prefix = f"jobfinder:analytics:{namespace}"
        self.hits = self.misses = self.bumps = self.errors = 0
        self._distrust_until = 0.0
        self._owed = False                               # an invalidation that could not be sent yet

    @classmethod
    def for_database(cls, redis, database_url: str, ttl_seconds: int) -> "AnalyticsCache":
        """One namespace per database, so two installations sharing a Redis server never read each other's entries."""
        return cls(redis, ttl_seconds, hashlib.sha1(database_url.encode()).hexdigest()[:10])

    @property
    def enabled(self) -> bool:
        return self.redis is not None and self.ttl > 0

    @property
    def version_key(self) -> str:
        return f"{self.prefix}:version"

    def _entry_key(self, version: int, kind: str, model: type[BaseModel], params: dict) -> str:
        shaped = {"params": params, "day": datetime.now(timezone.utc).date(), "model": model.__name__}   # default ranges end "today"
        digest = hashlib.sha1(json.dumps(shaped, sort_keys=True, default=str).encode()).hexdigest()[:24]
        return f"{self.prefix}:entry:{version}:{kind}:{digest}"

    async def cached(self, kind: str, params: dict, model: type[T], compute: Callable[[], Awaitable[T]]) -> T:
        """`compute()` on a miss; the stored answer on a hit. The caller has already authorized the request."""
        if not self.enabled or time.monotonic() < self._distrust_until:
            return await compute()
        if self._owed:
            await self.bump()
            if self._owed:
                return await compute()
        key = None
        try:
            key = self._entry_key(int(await self.redis.get(self.version_key) or 0), kind, model, params)
            raw = await self.redis.get(key)
        except Exception as exc:
            self._failed("read", exc)
            return await compute()
        if raw is not None:
            try:
                value = model.model_validate_json(raw)
                self.hits += 1
                return value
            except ValueError:                           # written by an older release: recompute and overwrite
                pass
        value = await compute()
        self.misses += 1
        try:
            await self.redis.set(key, value.model_dump_json(), ex=self.ttl)
        except Exception as exc:
            self._failed("write", exc)
        return value

    async def bump(self) -> None:
        """Data changed and is committed: retire every stored entry."""
        if self.redis is None:
            return
        if time.monotonic() < self._distrust_until:
            self._owed = True                            # do not make every commit wait for a Redis that is down
            return
        try:
            await self.redis.incr(self.version_key)
            self.bumps += 1
            self._owed = False
        except Exception as exc:
            self._owed = True
            self._failed("invalidate", exc)

    def _failed(self, action: str, exc: Exception) -> None:
        """Redis trouble: this process stops using Redis for one time-to-live. By then every entry that could
        have missed an invalidation has expired; an invalidation skipped meanwhile is sent before the next read."""
        self.errors += 1
        self._distrust_until = time.monotonic() + self.ttl
        log.warning("analytics cache %s failed (%s); computing live for %s s", action, type(exc).__name__, self.ttl)


_active: AnalyticsCache | None = None


def install(cache: AnalyticsCache | None) -> None:
    """The cache this process invalidates on commit (one per process: the API, or the worker)."""
    global _active
    _active = cache


def active() -> AnalyticsCache | None:
    return _active
