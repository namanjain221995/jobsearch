"""SQLite storage.

- postings / postings_fts / units: the local job index. A "unit" is one fetch from one source
  (an employer board, one search query against a source, or a feed). Postings are replaced per unit,
  so a posting that disappears from its source disappears from the index on the next refresh.
- boards: employer board keys the app knows about (seed list + boards found by AI web search).
- searches / jobs: search history (never resume content) and the last verified results."""
import json
import sqlite3
import threading
import time
import zlib

from .schemas import Job


def pack(job: Job) -> bytes:
    return zlib.compress(job.model_dump_json(exclude={"row_id"}).encode(), 1)


def unpack(blob: bytes, row_id: int = 0) -> Job:
    job = Job.model_validate_json(zlib.decompress(blob))
    job.row_id = row_id
    return job


class Database:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS searches (
                id TEXT PRIMARY KEY, created_at REAL, status TEXT, params TEXT, stats TEXT);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, ats_key TEXT, url TEXT, company TEXT, title TEXT,
                verified_at TEXT, seen_at REAL, data TEXT);
            CREATE TABLE IF NOT EXISTS boards (
                ats TEXT, slug TEXT, ok INTEGER DEFAULT 1, job_count INTEGER DEFAULT 0, checked_at REAL,
                origin TEXT, PRIMARY KEY (ats, slug));
            CREATE TABLE IF NOT EXISTS units (
                id INTEGER PRIMARY KEY, src TEXT NOT NULL, key TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '',
                fetched_at REAL DEFAULT 0, expires_at REAL DEFAULT 0, ok INTEGER DEFAULT 0,
                count INTEGER DEFAULT 0, error TEXT DEFAULT '', UNIQUE (src, key, scope));
            CREATE TABLE IF NOT EXISTS postings (
                id INTEGER PRIMARY KEY, unit_id INTEGER NOT NULL, akey TEXT NOT NULL, title TEXT NOT NULL,
                tokens TEXT NOT NULL, data BLOB NOT NULL);
            CREATE INDEX IF NOT EXISTS postings_unit ON postings (unit_id);
            CREATE INDEX IF NOT EXISTS postings_akey ON postings (akey);
            CREATE VIRTUAL TABLE IF NOT EXISTS postings_fts USING fts5 (tokens);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        if "sig" not in {r[1] for r in self._db.execute("PRAGMA table_info(postings)")}:
            self._db.execute("ALTER TABLE postings ADD COLUMN sig TEXT NOT NULL DEFAULT ''")     # older index files
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------ history
    def save_search(self, search_id: str, status: str, params: dict, stats: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO searches (id, created_at, status, params, stats) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, stats=excluded.stats",
                (search_id, time.time(), status, json.dumps(params), json.dumps(stats)))
            self._db.commit()

    def save_jobs(self, jobs: list[Job]) -> None:
        rows = [(j.id, f"{j.ats}:{j.ats_id}" if j.ats_id else "", j.url, j.company, j.title, j.verified_at,
                 time.time(), json.dumps(j.public())) for j in jobs]
        with self._lock:
            self._db.executemany(
                "INSERT INTO jobs (id, ats_key, url, company, title, verified_at, seen_at, data) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET verified_at=excluded.verified_at, seen_at=excluded.seen_at, data=excluded.data",
                rows)
            self._db.commit()

    # ------------------------------------------------------------ known employer boards
    def record_board(self, ats: str, slug: str, ok: bool, job_count: int = 0, origin: str = "seed") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO boards (ats, slug, ok, job_count, checked_at, origin) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(ats, slug) DO UPDATE SET ok=excluded.ok, job_count=excluded.job_count, checked_at=excluded.checked_at",
                (ats, slug, int(ok), job_count, time.time(), origin))
            self._db.commit()

    def known_boards(self) -> list[tuple[str, str]]:
        """Boards discovered in earlier searches that answered successfully."""
        with self._lock:
            rows = self._db.execute("SELECT ats, slug FROM boards WHERE ok=1 AND origin!='seed' ORDER BY job_count DESC").fetchall()
        return [(r[0], r[1]) for r in rows]

    def dead_boards(self, max_age_seconds: int = 86400) -> set[tuple[str, str]]:
        with self._lock:
            rows = self._db.execute("SELECT ats, slug FROM boards WHERE ok=0 AND checked_at > ?", (time.time() - max_age_seconds,)).fetchall()
        return {(r[0], r[1]) for r in rows}

    # ------------------------------------------------------------ index: units
    def unit_states(self) -> dict[tuple[str, str, str], tuple[float, float, int]]:
        """(src, key, scope) -> (fetched_at, expires_at, ok) for every unit ever fetched."""
        with self._lock:
            rows = self._db.execute("SELECT src, key, scope, fetched_at, expires_at, ok FROM units").fetchall()
        return {(r[0], r[1], r[2]): (r[3], r[4], r[5]) for r in rows}

    def unit_rows(self, src: str, key: str, scope: str) -> dict[str, tuple[str, bytes]]:
        """akey -> (content signature, packed job) currently stored for one unit."""
        with self._lock:
            rows = self._db.execute(
                "SELECT p.akey, p.sig, p.data FROM postings p JOIN units u ON u.id=p.unit_id "
                "WHERE u.src=? AND u.key=? AND u.scope=?", (src, key, scope)).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    def replace_unit(self, src: str, key: str, scope: str, rows: list[tuple[str, str, str, str, bytes]], ttl: float) -> int:
        """Atomically replace every posting of one unit. rows = (akey, title, tokens, signature, packed job)."""
        now = time.time()
        with self._lock:
            cur = self._db.cursor()
            cur.execute(
                "INSERT INTO units (src, key, scope, fetched_at, expires_at, ok, count, error) VALUES (?,?,?,?,?,1,?,'') "
                "ON CONFLICT(src, key, scope) DO UPDATE SET fetched_at=excluded.fetched_at, expires_at=excluded.expires_at, "
                "ok=1, count=excluded.count, error=''", (src, key, scope, now, now + ttl, len(rows)))
            unit_id = cur.execute("SELECT id FROM units WHERE src=? AND key=? AND scope=?", (src, key, scope)).fetchone()[0]
            cur.execute("DELETE FROM postings_fts WHERE rowid IN (SELECT id FROM postings WHERE unit_id=?)", (unit_id,))
            cur.execute("DELETE FROM postings WHERE unit_id=?", (unit_id,))
            cur.executemany("INSERT INTO postings (unit_id, akey, title, tokens, sig, data) VALUES (?,?,?,?,?,?)",
                            [(unit_id, a, t, k, g, d) for a, t, k, g, d in rows])
            cur.execute("INSERT INTO postings_fts (rowid, tokens) SELECT id, tokens FROM postings WHERE unit_id=?", (unit_id,))
            self._db.commit()
        return unit_id

    def fail_unit(self, src: str, key: str, scope: str, error: str, retry_after: float = 600.0) -> None:
        """Record a failed fetch. Existing postings of the unit are kept but no longer count as fresh."""
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO units (src, key, scope, fetched_at, expires_at, ok, count, error) VALUES (?,?,?,?,?,0,0,?) "
                "ON CONFLICT(src, key, scope) DO UPDATE SET fetched_at=excluded.fetched_at, expires_at=excluded.expires_at, "
                "ok=0, error=excluded.error", (src, key, scope, now, now + retry_after, error[:200]))
            self._db.commit()

    # ------------------------------------------------------------ index: postings
    def candidates(self, tokens: list[str], akeys: list[str], max_age: float | None = None,
                   limit: int = 60000) -> list[tuple[int, str, str, str, float]]:
        """Fresh postings whose title shares a token with the query, plus postings named by akey.
        Returns (row id, akey, title, source, fetched_at). `max_age` widens freshness for preliminary results."""
        now = time.time()
        fresh = "u.ok=1 AND u.expires_at > ?" if max_age is None else "u.ok=1 AND u.fetched_at > ?"
        bound = now if max_age is None else now - max_age
        out: dict[int, tuple[int, str, str, str, float]] = {}
        with self._lock:
            if tokens:
                match = " OR ".join('"' + t.replace('"', "") + '"' for t in tokens)
                for r in self._db.execute(
                        f"SELECT p.id, p.akey, p.title, u.src, u.fetched_at FROM postings_fts f JOIN postings p ON p.id=f.rowid "
                        f"JOIN units u ON u.id=p.unit_id WHERE postings_fts MATCH ? AND {fresh} LIMIT ?", (match, bound, limit)):
                    out[r[0]] = r
            for i in range(0, len(akeys), 400):
                chunk = akeys[i:i + 400]
                marks = ",".join("?" * len(chunk))
                for r in self._db.execute(
                        f"SELECT p.id, p.akey, p.title, u.src, u.fetched_at FROM postings p JOIN units u ON u.id=p.unit_id "
                        f"WHERE p.akey IN ({marks}) AND {fresh}", (*chunk, bound)):
                    out[r[0]] = r
        return list(out.values())

    def load_postings(self, row_ids: list[int]) -> list[Job]:
        jobs: list[Job] = []
        with self._lock:
            for i in range(0, len(row_ids), 500):
                chunk = row_ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                rows = self._db.execute(f"SELECT id, data FROM postings WHERE id IN ({marks})", chunk).fetchall()
                jobs.extend(unpack(data, row_id) for row_id, data in rows)
        return jobs

    def update_postings(self, jobs: list[Job]) -> None:
        """Persist per-posting work (fetched description, extracted fields) so later searches skip it."""
        rows = [(pack(j), j.row_id) for j in jobs if j.row_id]
        if not rows:
            return
        with self._lock:
            self._db.executemany("UPDATE postings SET data=? WHERE id=?", rows)
            self._db.commit()

    def delete_akeys(self, akeys: list[str]) -> None:
        """Remove a posting everywhere it was indexed (the source reported it closed)."""
        if not akeys:
            return
        with self._lock:
            for i in range(0, len(akeys), 400):
                chunk = akeys[i:i + 400]
                marks = ",".join("?" * len(chunk))
                self._db.execute(f"DELETE FROM postings_fts WHERE rowid IN (SELECT id FROM postings WHERE akey IN ({marks}))", chunk)
                self._db.execute(f"DELETE FROM postings WHERE akey IN ({marks})", chunk)
            self._db.commit()

    def live_akeys(self, akeys: list[str]) -> set[str]:
        found: set[str] = set()
        now = time.time()
        with self._lock:
            for i in range(0, len(akeys), 400):
                chunk = akeys[i:i + 400]
                marks = ",".join("?" * len(chunk))
                found.update(r[0] for r in self._db.execute(
                    f"SELECT p.akey FROM postings p JOIN units u ON u.id=p.unit_id WHERE p.akey IN ({marks}) "
                    f"AND u.ok=1 AND u.expires_at > ?", (*chunk, now)))
        return found

    def index_stats(self, max_age: float | None = None) -> dict:
        now = time.time()
        fresh = "ok=1 AND expires_at > ?" if max_age is None else "ok=1 AND fetched_at > ?"
        bound = now if max_age is None else now - max_age
        with self._lock:
            rows = self._db.execute(f"SELECT src, COUNT(*), COALESCE(SUM(count),0) FROM units WHERE {fresh} GROUP BY src", (bound,)).fetchall()
            total = self._db.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
        return {"fresh_postings": sum(r[2] for r in rows), "fresh_units": sum(r[1] for r in rows),
                "by_source": {r[0]: {"units": r[1], "postings": r[2]} for r in rows}, "stored_postings": total}

    def meta_set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self._db.commit()

    def meta_prefix(self, prefix: str) -> dict[str, str]:
        with self._lock:
            rows = self._db.execute("SELECT key, value FROM meta WHERE key LIKE ?", (prefix + "%",)).fetchall()
        return {r[0][len(prefix):]: r[1] for r in rows}

    def prune(self, older_than_seconds: float = 3 * 86400) -> int:
        """Drop postings of units that have not been refreshed for days (old search queries, dead boards)."""
        cutoff = time.time() - older_than_seconds
        with self._lock:
            ids = [r[0] for r in self._db.execute("SELECT id FROM units WHERE fetched_at < ?", (cutoff,))]
            for unit_id in ids:
                self._db.execute("DELETE FROM postings_fts WHERE rowid IN (SELECT id FROM postings WHERE unit_id=?)", (unit_id,))
                self._db.execute("DELETE FROM postings WHERE unit_id=?", (unit_id,))
                self._db.execute("DELETE FROM units WHERE id=?", (unit_id,))
            self._db.commit()
        return len(ids)

    def counts(self) -> dict:
        with self._lock:
            return {t: self._db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("searches", "jobs", "boards", "units", "postings")}
