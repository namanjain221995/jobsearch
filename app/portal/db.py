"""Async SQLAlchemy engine and session factory (asyncpg driver, pooled connections).

Sessions also keep cached analytics honest: a commit that changed a table the dashboards read raises the
cache's data version before `commit()` returns (see cache.py)."""
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from . import cache
from .cache import TRACKED_TABLES

_DIRTY = "analytics_dirty"


@event.listens_for(Session, "after_flush")
def _flag_flushed_objects(session: Session, _context) -> None:
    if not session.info.get(_DIRTY):
        for obj in (*session.new, *session.dirty, *session.deleted):
            if getattr(obj, "__tablename__", None) in TRACKED_TABLES:
                session.info[_DIRTY] = True
                return


@event.listens_for(Session, "do_orm_execute")
def _flag_statements(state) -> None:
    if state.is_insert or state.is_update or state.is_delete:                # INSERT / UPDATE / DELETE run through execute()
        if getattr(getattr(state.statement, "table", None), "name", None) in TRACKED_TABLES:
            state.session.info[_DIRTY] = True


class PortalSession(AsyncSession):
    async def commit(self) -> None:
        await super().commit()
        if self.info.pop(_DIRTY, False):
            current = cache.active()
            if current is not None:
                await current.bump()

    async def rollback(self) -> None:
        await super().rollback()
        self.info.pop(_DIRTY, None)


def keep_analytics_cache(session: AsyncSession) -> None:
    """Call before commit when the pending change cannot alter any dashboard number (a heartbeat, progress text)."""
    session.info.pop(_DIRTY, None)


def make_engine(url: str, pool_size: int = 10) -> AsyncEngine:
    return create_async_engine(url, pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True, pool_recycle=1800)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=PortalSession, expire_on_commit=False, autoflush=False)
