"""Catalogue rows in the PostgreSQL index: registration is idempotent, unread catalogue boards stay out of
known_boards, the trickle sees due boards per ATS, and a board joins searches once a read found postings."""
from app.portal.index_pg import PgJobIndex
from tests.portal.conftest import PLAIN_URL


async def test_catalog_rows_in_postgres(database):
    index = await PgJobIndex(PLAIN_URL).connect()
    try:
        await index.pool.execute("TRUNCATE index_postings, index_units, index_boards, index_meta RESTART IDENTITY CASCADE")
        await index.record_board("greenhouse", "found", True, 5, "searxng")
        pairs = [("greenhouse", "a"), ("greenhouse", "b"), ("greenhouse", "found"), ("lever", "c")]
        assert await index.register_catalog(pairs) == 3
        assert await index.register_catalog(pairs) == 0                          # idempotent
        assert await index.known_boards() == [("greenhouse", "found")]           # unread catalogue rows are not planned
        due = await index.catalog_due({"greenhouse": 1, "lever": 5})
        assert sorted(due) == [("greenhouse", "a"), ("lever", "c")]
        await index.catalog_touch(due)
        assert await index.catalog_due({"greenhouse": 5}) == [("greenhouse", "b")]
        await index.record_board("greenhouse", "a", True, 12, "catalog")
        await index.record_board("lever", "c", False, 0, "catalog")
        assert await index.known_boards() == [("greenhouse", "a"), ("greenhouse", "found")]
        assert ("lever", "c") in await index.dead_boards()
        assert await index.board_slugs("greenhouse") == {"a", "b", "found"}
        stats = await index.catalog_stats()
        assert stats["greenhouse"] == {"total": 2, "read": 1, "live": 1, "dead": 0} and stats["lever"]["dead"] == 1
    finally:
        await index.close()
