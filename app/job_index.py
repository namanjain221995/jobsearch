"""The crawler's job index behind one async interface.

Two implementations exist:
- SqliteJobIndex (here): the original single-file index, used by the legacy single-user app and unit tests.
- PgJobIndex (app/portal/index_pg.py): the same index in PostgreSQL, used by the portal and its worker.
Both expose the same coroutine methods, so the search engine does not care which one it gets."""
import asyncio

from .database import Database

INDEX_METHODS = ("record_board", "known_boards", "dead_boards", "unit_states", "unit_rows", "replace_unit", "fail_unit",
                 "candidates", "load_postings", "update_postings", "delete_akeys", "live_akeys", "index_stats",
                 "meta_set", "meta_prefix", "prune")


class SqliteJobIndex:
    is_async = True

    def __init__(self, db: Database):
        self.db = db

    def __getattr__(self, name: str):
        if name in INDEX_METHODS:
            method = getattr(self.db, name)

            async def call(*args, **kwargs):
                return await asyncio.to_thread(method, *args, **kwargs)

            return call
        raise AttributeError(name)


def as_index(db):
    """Accept either index implementation or a plain SQLite Database."""
    return db if getattr(db, "is_async", False) else SqliteJobIndex(db)
