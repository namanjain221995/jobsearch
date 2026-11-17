"""The crawler's job index in PostgreSQL (same contract as the SQLite index in app/database.py).

index_units    one row per fetch unit (an employer board, one search query against a source, or a feed)
index_postings postings of a unit, replaced atomically on every refresh; title tokens are full-text indexed
index_boards   employer boards the crawler knows (seed list + boards learned from AI web search)
index_meta     small key/value facts (for example each Workday employer's "United States" filter)
Times are epoch seconds, exactly as in the SQLite index, so both behave the same."""
import asyncio
import re
import time

import asyncpg

from ..database import pack, unpack
from ..schemas import Job

INDEX_DDL = """
CREATE TABLE IF NOT EXISTS index_boards (
    ats text NOT NULL, slug text NOT NULL, ok boolean NOT NULL DEFAULT true, job_count integer NOT NULL DEFAULT 0,
    checked_at double precision, origin text NOT NULL DEFAULT 'seed', PRIMARY KEY (ats, slug));
CREATE TABLE IF NOT EXISTS index_units (
    id bigserial PRIMARY KEY, src text NOT NULL, key text NOT NULL, scope text NOT NULL DEFAULT '',
    fetched_at double precision NOT NULL DEFAULT 0, expires_at double precision NOT NULL DEFAULT 0,
    ok boolean NOT NULL DEFAULT false, count integer NOT NULL DEFAULT 0, error text NOT NULL DEFAULT '',
    CONSTRAINT uq_index_units UNIQUE (src, key, scope));
CREATE TABLE IF NOT EXISTS index_postings (
    id bigserial PRIMARY KEY, unit_id bigint NOT NULL REFERENCES index_units (id) ON DELETE CASCADE,
    akey text NOT NULL, title text NOT NULL, tokens text NOT NULL,
    tokens_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', tokens)) STORED,
    sig text NOT NULL DEFAULT '', data bytea NOT NULL);
CREATE INDEX IF NOT EXISTS ix_index_postings_unit ON index_postings (unit_id);
CREATE INDEX IF NOT EXISTS ix_index_postings_akey ON index_postings (akey);
CREATE INDEX IF NOT EXISTS ix_index_postings_tsv ON index_postings USING gin (tokens_tsv);
CREATE TABLE IF NOT EXISTS index_meta (key text PRIMARY KEY, value text NOT NULL);
"""
INDEX_TABLES = ("index_postings", "index_units", "index_boards", "index_meta")
_SAFE_TOKEN = re.compile(r"[^a-z0-9]")


CATALOG_ORIGINS = ("catalog", "commoncrawl")      # unread imported boards, read by the paced trickle


class PgJobIndex:
    is_async = True

    def __init__(self, dsn: str, max_size: int = 8):
        self._dsn = dsn
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> "PgJobIndex":
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=self._max_size)
        return self

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PgJobIndex.connect() was not awaited")
        return self._pool

    # ------------------------------------------------------------ known employer boards
    async def record_board(self, ats: str, slug: str, ok: bool, job_count: int = 0, origin: str = "seed") -> None:
        await self.pool.execute(
            "INSERT INTO index_boards (ats, slug, ok, job_count, checked_at, origin) VALUES ($1,$2,$3,$4,$5,$6) "
            "ON CONFLICT (ats, slug) DO UPDATE SET ok=excluded.ok, job_count=excluded.job_count, checked_at=excluded.checked_at",
            ats, slug, ok, job_count, time.time(), origin)

    async def known_boards(self) -> list[tuple[str, str]]:
        # Imported catalogue boards (origin 'catalog', see app/board_catalog.py) join only after a read found postings,
        # so registering thousands of them never makes every search plan thousands of unread boards.
        rows = await self.pool.fetch("SELECT ats, slug FROM index_boards WHERE ok AND origin <> 'seed' "
                                     "AND (origin NOT IN ('catalog', 'commoncrawl') OR job_count > 0) ORDER BY job_count DESC")
        return [(r["ats"], r["slug"]) for r in rows]

    async def register_catalog(self, pairs: list[tuple[str, str]], origin: str = "catalog") -> int:
        """Register boards from the imported catalogue as not yet read (checked_at NULL). Existing rows are left alone.
        `origin` is 'catalog' (CSV lists) or 'commoncrawl' (Common Crawl index); both are read by the trickle.
        Returns how many rows were new."""
        if origin not in CATALOG_ORIGINS:
            raise ValueError(origin)
        if not pairs:
            return 0
        result = await self.pool.execute(
            "INSERT INTO index_boards (ats, slug, ok, job_count, checked_at, origin) "
            "SELECT a, s, true, 0, NULL, $3 FROM unnest($1::text[], $2::text[]) AS t(a, s) ON CONFLICT (ats, slug) DO NOTHING",
            [a for a, _ in pairs], [s for _, s in pairs], origin)
        return int(result.split()[-1])

    async def board_slugs(self, ats: str) -> set[str]:
        rows = await self.pool.fetch("SELECT slug FROM index_boards WHERE ats=$1", ats)
        return {r["slug"] for r in rows}

    async def catalog_due(self, per_ats: dict[str, int], recheck_seconds: float = 7 * 86400) -> list[tuple[str, str]]:
        """Catalogue boards to read next: never read first, then empty ones not checked for `recheck_seconds`.
        At most per_ats[ats] boards per ATS."""
        names = [a for a, n in per_ats.items() if n > 0]
        if not names:
            return []
        rows = await self.pool.fetch(
            "SELECT ats, slug FROM (SELECT ats, slug, row_number() OVER (PARTITION BY ats ORDER BY checked_at NULLS FIRST, slug) AS rn "
            "FROM index_boards WHERE origin IN ('catalog', 'commoncrawl') AND ok AND ats = ANY($1::text[]) "
            "AND (checked_at IS NULL OR (job_count = 0 AND checked_at < $2))) t "
            "WHERE rn <= (SELECT n FROM unnest($1::text[], $3::int[]) AS q(a, n) WHERE q.a = t.ats)",
            names, time.time() - recheck_seconds, [per_ats[a] for a in names])
        return [(r["ats"], r["slug"]) for r in rows]

    async def catalog_touch(self, pairs: list[tuple[str, str]]) -> None:
        """Mark catalogue boards as attempted now, so a board whose read fails is not picked again every minute."""
        if pairs:
            await self.pool.execute(
                "UPDATE index_boards b SET checked_at=$3 FROM unnest($1::text[], $2::text[]) AS t(a, s) "
                "WHERE b.ats=t.a AND b.slug=t.s AND b.origin IN ('catalog', 'commoncrawl')", [x for x, _ in pairs], [y for _, y in pairs], time.time())

    async def catalog_stats(self) -> dict[str, dict[str, int]]:
        rows = await self.pool.fetch(
            "SELECT ats, count(*) AS total, count(checked_at) AS read, count(*) FILTER (WHERE ok AND job_count > 0) AS live, "
            "count(*) FILTER (WHERE NOT ok) AS dead FROM index_boards WHERE origin IN ('catalog', 'commoncrawl') GROUP BY ats")
        return {r["ats"]: {k: int(r[k]) for k in ("total", "read", "live", "dead")} for r in rows}

    async def dead_boards(self, max_age_seconds: int = 86400) -> set[tuple[str, str]]:
        rows = await self.pool.fetch("SELECT ats, slug FROM index_boards WHERE NOT ok AND checked_at > $1", time.time() - max_age_seconds)
        return {(r["ats"], r["slug"]) for r in rows}

    # ------------------------------------------------------------ units
    async def unit_states(self) -> dict[tuple[str, str, str], tuple[float, float, int]]:
        rows = await self.pool.fetch("SELECT src, key, scope, fetched_at, expires_at, ok FROM index_units")
        return {(r["src"], r["key"], r["scope"]): (r["fetched_at"], r["expires_at"], int(r["ok"])) for r in rows}

    async def unit_rows(self, src: str, key: str, scope: str) -> dict[str, tuple[str, bytes]]:
        rows = await self.pool.fetch(
            "SELECT p.akey, p.sig, p.data FROM index_postings p JOIN index_units u ON u.id = p.unit_id "
            "WHERE u.src=$1 AND u.key=$2 AND u.scope=$3", src, key, scope)
        return {r["akey"]: (r["sig"], bytes(r["data"])) for r in rows}

    async def replace_unit(self, src: str, key: str, scope: str, rows: list[tuple[str, str, str, str, bytes]], ttl: float) -> int:
        now = time.time()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                unit_id = await conn.fetchval(
                    "INSERT INTO index_units (src, key, scope, fetched_at, expires_at, ok, count, error) "
                    "VALUES ($1,$2,$3,$4,$5,true,$6,'') ON CONFLICT (src, key, scope) DO UPDATE SET "
                    "fetched_at=excluded.fetched_at, expires_at=excluded.expires_at, ok=true, count=excluded.count, error='' "
                    "RETURNING id", src, key, scope, now, now + ttl, len(rows))
                await conn.execute("DELETE FROM index_postings WHERE unit_id=$1", unit_id)
                if rows:
                    await conn.copy_records_to_table(
                        "index_postings", columns=("unit_id", "akey", "title", "tokens", "sig", "data"),
                        records=[(unit_id, a, t, k, g, d) for a, t, k, g, d in rows])
        return unit_id

    async def fail_unit(self, src: str, key: str, scope: str, error: str, retry_after: float = 600.0) -> None:
        now = time.time()
        await self.pool.execute(
            "INSERT INTO index_units (src, key, scope, fetched_at, expires_at, ok, count, error) VALUES ($1,$2,$3,$4,$5,false,0,$6) "
            "ON CONFLICT (src, key, scope) DO UPDATE SET fetched_at=excluded.fetched_at, expires_at=excluded.expires_at, "
            "ok=false, error=excluded.error", src, key, scope, now, now + retry_after, error[:200])

    # ------------------------------------------------------------ postings
    async def candidates(self, tokens: list[str], akeys: list[str], max_age: float | None = None,
                         limit: int = 60000) -> list[tuple[int, str, str, str, float]]:
        now = time.time()
        fresh = "u.ok AND u.expires_at > $1" if max_age is None else "u.ok AND u.fetched_at > $1"
        bound = now if max_age is None else now - max_age
        out: dict[int, tuple[int, str, str, str, float]] = {}
        clean = [t for t in (_SAFE_TOKEN.sub("", tok.lower()) for tok in tokens) if t]
        base = ("SELECT p.id, p.akey, p.title, u.src, u.fetched_at FROM index_postings p "
                "JOIN index_units u ON u.id = p.unit_id WHERE ")
        async with self.pool.acquire() as conn:
            if clean:
                for r in await conn.fetch(base + f"p.tokens_tsv @@ to_tsquery('simple', $2) AND {fresh} LIMIT $3",
                                          bound, " | ".join(clean), limit):
                    out[r["id"]] = (r["id"], r["akey"], r["title"], r["src"], r["fetched_at"])
            if akeys:
                for r in await conn.fetch(base + f"p.akey = ANY($2::text[]) AND {fresh}", bound, list(akeys)):
                    out[r["id"]] = (r["id"], r["akey"], r["title"], r["src"], r["fetched_at"])
        return list(out.values())

    async def load_postings(self, row_ids: list[int]) -> list[Job]:
        if not row_ids:
            return []
        rows = await self.pool.fetch("SELECT id, data FROM index_postings WHERE id = ANY($1::bigint[])", list(row_ids))
        return await asyncio.to_thread(lambda: [unpack(bytes(r["data"]), r["id"]) for r in rows])

    async def update_postings(self, jobs: list[Job]) -> None:
        rows = await asyncio.to_thread(lambda: [(pack(j), j.row_id) for j in jobs if j.row_id])
        if rows:
            await self.pool.executemany("UPDATE index_postings SET data=$1 WHERE id=$2", rows)

    async def delete_akeys(self, akeys: list[str]) -> None:
        if akeys:
            await self.pool.execute("DELETE FROM index_postings WHERE akey = ANY($1::text[])", list(akeys))

    async def live_akeys(self, akeys: list[str]) -> set[str]:
        if not akeys:
            return set()
        rows = await self.pool.fetch(
            "SELECT DISTINCT p.akey FROM index_postings p JOIN index_units u ON u.id = p.unit_id "
            "WHERE p.akey = ANY($1::text[]) AND u.ok AND u.expires_at > $2", list(akeys), time.time())
        return {r["akey"] for r in rows}

    async def index_stats(self, max_age: float | None = None) -> dict:
        now = time.time()
        fresh = "ok AND expires_at > $1" if max_age is None else "ok AND fetched_at > $1"
        bound = now if max_age is None else now - max_age
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT src, count(*) AS units, coalesce(sum(count), 0) AS postings FROM index_units WHERE {fresh} GROUP BY src", bound)
            total = await conn.fetchval("SELECT count(*) FROM index_postings")
        return {"fresh_postings": int(sum(r["postings"] for r in rows)), "fresh_units": int(sum(r["units"] for r in rows)),
                "by_source": {r["src"]: {"units": int(r["units"]), "postings": int(r["postings"])} for r in rows},
                "stored_postings": int(total)}

    async def meta_set(self, key: str, value: str) -> None:
        await self.pool.execute("INSERT INTO index_meta (key, value) VALUES ($1,$2) ON CONFLICT (key) DO UPDATE SET value=excluded.value", key, value)

    async def meta_prefix(self, prefix: str) -> dict[str, str]:
        rows = await self.pool.fetch("SELECT key, value FROM index_meta WHERE key LIKE $1", prefix.replace("%", r"\%").replace("_", r"\_") + "%")
        return {r["key"][len(prefix):]: r["value"] for r in rows}

    async def prune(self, older_than_seconds: float = 3 * 86400) -> int:
        result = await self.pool.execute("DELETE FROM index_units WHERE fetched_at < $1", time.time() - older_than_seconds)
        return int(result.split()[-1])
