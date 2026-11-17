"""Copy the legacy SQLite data into PostgreSQL.   python -m app.portal.migrate_sqlite [--sqlite PATH] [--replace]

  boards   -> index_boards        meta     -> index_meta
  units    -> index_units         postings -> index_postings   (same packed records, ids kept)
  searches -> legacy_records (kind "search")      jobs -> legacy_records (kind "job")

Safety:
- the SQLite file is opened read-only and is never modified or deleted;
- the whole copy is one PostgreSQL transaction: it either completes or leaves the target untouched;
- the target index tables must be empty, unless --replace is given (they are then emptied inside that transaction);
- afterwards row counts, a checksum sample of the packed records and a full-text query are compared between
  the two databases; any difference makes the command fail (non-zero exit) and the transaction is rolled back."""
import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
import time

import asyncpg

from ..config import get_settings

TABLES = {"boards": "index_boards", "meta": "index_meta", "units": "index_units", "postings": "index_postings"}


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


async def migrate(sqlite_path: str, dsn: str, replace: bool) -> int:
    started = time.time()
    try:
        src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        print(f"Cannot open {sqlite_path} read-only: {exc}", file=sys.stderr)
        return 2
    present = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    source_counts = {t: src.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("boards", "meta", "units", "postings", "searches", "jobs") if t in present}
    print("source rows:", source_counts)
    conn = await asyncpg.connect(dsn)
    try:
        existing = {target: await conn.fetchval(f"SELECT count(*) FROM {target}") for target in TABLES.values()}
        if any(existing.values()) and not replace:
            if all(existing[TABLES[t]] == source_counts.get(t, 0) for t in TABLES):
                print("Target already holds the same number of rows in every index table: nothing to do.")
                return 0
            print(f"Target index tables are not empty ({existing}). Re-run with --replace to overwrite them.", file=sys.stderr)
            return 2
        tx = conn.transaction()
        await tx.start()
        try:
            if replace:
                await conn.execute("TRUNCATE index_postings, index_units, index_boards, index_meta RESTART IDENTITY CASCADE")
            if "boards" in present:
                await conn.copy_records_to_table("index_boards", columns=("ats", "slug", "ok", "job_count", "checked_at", "origin"), records=[
                    (r[0], r[1], bool(r[2]), r[3] or 0, r[4], r[5] or "seed")
                    for r in src.execute("SELECT ats, slug, ok, job_count, checked_at, origin FROM boards")])
            if "meta" in present:
                await conn.copy_records_to_table("index_meta", columns=("key", "value"), records=list(src.execute("SELECT key, value FROM meta")))
            unit_ids: set[int] = set()
            if "units" in present:
                rows = [(r[0], r[1], r[2], r[3] or "", float(r[4] or 0), float(r[5] or 0), bool(r[6]), r[7] or 0, r[8] or "")
                        for r in src.execute("SELECT id, src, key, scope, fetched_at, expires_at, ok, count, error FROM units")]
                unit_ids = {r[0] for r in rows}
                await conn.copy_records_to_table("index_units", columns=("id", "src", "key", "scope", "fetched_at", "expires_at", "ok", "count", "error"), records=rows)
                await conn.execute("SELECT setval(pg_get_serial_sequence('index_units', 'id'), coalesce((SELECT max(id) FROM index_units), 1))")
            orphans = copied = 0
            if "postings" in present:
                sig = "sig" if "sig" in _columns(src, "postings") else "''"
                cursor = src.execute(f"SELECT id, unit_id, akey, title, tokens, {sig}, data FROM postings ORDER BY id")
                while True:
                    batch = cursor.fetchmany(2000)
                    if not batch:
                        break
                    keep = [(r[0], r[1], r[2], r[3], r[4], r[5] or "", bytes(r[6])) for r in batch if r[1] in unit_ids]
                    orphans += len(batch) - len(keep)
                    copied += len(keep)
                    await conn.copy_records_to_table("index_postings", columns=("id", "unit_id", "akey", "title", "tokens", "sig", "data"), records=keep)
                await conn.execute("SELECT setval(pg_get_serial_sequence('index_postings', 'id'), coalesce((SELECT max(id) FROM index_postings), 1))")
            legacy = 0
            if "searches" in present:
                for r in src.execute("SELECT id, created_at, status, params, stats FROM searches"):
                    data = {"created_at": r[1], "status": r[2], "params": json.loads(r[3] or "{}"), "stats": json.loads(r[4] or "{}")}
                    legacy += int((await conn.execute("INSERT INTO legacy_records (kind, legacy_id, data) VALUES ('search', $1, $2::jsonb) "
                                                      "ON CONFLICT (kind, legacy_id) DO NOTHING", str(r[0]), json.dumps(data))).split()[-1])
            if "jobs" in present:
                for r in src.execute("SELECT id, ats_key, url, company, title, verified_at, seen_at, data FROM jobs"):
                    data = {"ats_key": r[1], "url": r[2], "company": r[3], "title": r[4], "verified_at": r[5], "seen_at": r[6], "job": json.loads(r[7] or "{}")}
                    legacy += int((await conn.execute("INSERT INTO legacy_records (kind, legacy_id, data) VALUES ('job', $1, $2::jsonb) "
                                                      "ON CONFLICT (kind, legacy_id) DO NOTHING", str(r[0]), json.dumps(data))).split()[-1])

            # ---- verification, still inside the transaction
            problems = []
            target_counts = {t: await conn.fetchval(f"SELECT count(*) FROM {TABLES[t]}") for t in TABLES if t in present}
            for table, count in target_counts.items():
                expected = source_counts[table] - (orphans if table == "postings" else 0)
                if count != expected:
                    problems.append(f"{table}: {count} rows in PostgreSQL, {expected} expected")
            sample = src.execute("SELECT id, data FROM postings ORDER BY (id * 7919) % 104729 LIMIT 200").fetchall()
            stored = {r["id"]: bytes(r["data"]) for r in await conn.fetch("SELECT id, data FROM index_postings WHERE id = ANY($1::bigint[])", [s[0] for s in sample])}
            mismatched = sum(1 for pid, blob in sample if hashlib.md5(bytes(blob)).digest() != hashlib.md5(stored.get(pid, b"")).digest())
            if mismatched:
                problems.append(f"{mismatched} of {len(sample)} sampled records differ")
            fts_source = src.execute("SELECT count(*) FROM postings_fts WHERE postings_fts MATCH '\"ai\" OR \"ml\" OR \"llm\"'").fetchone()[0] if "postings_fts" in present else None
            fts_target = await conn.fetchval("SELECT count(*) FROM index_postings WHERE tokens_tsv @@ to_tsquery('simple', 'ai | ml | llm')")
            if fts_source is not None and fts_source != fts_target:
                problems.append(f"full-text query returns {fts_target} rows in PostgreSQL, {fts_source} in SQLite")
            legacy_total = await conn.fetchval("SELECT count(*) FROM legacy_records")
            expected_legacy = source_counts.get("searches", 0) + source_counts.get("jobs", 0)
            if legacy_total < expected_legacy:
                problems.append(f"legacy_records holds {legacy_total} rows, {expected_legacy} expected")
            if problems:
                await tx.rollback()
                print("MIGRATION FAILED verification, nothing was changed:", *problems, sep="\n  - ", file=sys.stderr)
                return 1
            await tx.commit()
        except BaseException:
            await tx.rollback()
            raise
        print("target rows:", target_counts, "| legacy_records:", legacy_total, f"(+{legacy} new)")
        print(f"verified: row counts equal, {len(sample)} sampled records byte-identical, full-text query {fts_target} = {fts_source}"
              + (f", {orphans} orphan postings without a unit skipped" if orphans else ""))
        print(f"done in {time.time() - started:.1f}s. The SQLite file was not modified.")
        return 0
    finally:
        await conn.close()
        src.close()


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Copy the legacy SQLite job index into PostgreSQL.")
    parser.add_argument("--sqlite", default=settings.sqlite_path, help="path of the SQLite file (default: LEGACY_SQLITE_PATH)")
    parser.add_argument("--replace", action="store_true", help="overwrite index data already in PostgreSQL")
    args = parser.parse_args()
    sys.exit(asyncio.run(migrate(args.sqlite, settings.postgres_dsn, args.replace)))


if __name__ == "__main__":
    main()
