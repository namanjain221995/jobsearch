"""Register the imported employer-board catalogue in the PostgreSQL job index.

    python -m app.portal.import_boards [--dry-run] [--only greenhouse,lever] [--validate N] [--simplify] [--dsn postgresql://...]
    python -m app.portal.import_boards --common-crawl [N] [--cc-state PATH] [--cc-max-pages M] [--only ...] [--dry-run]

Registration fetches nothing: boards are read later by the engine's paced catalogue trickle (app/board_catalog.py).
--validate N reads N random new boards per ATS now (paced, public feeds only) and reports the live ratio and postings per
board; without --dry-run those boards are recorded (live / empty / dead) and their postings stored like a normal refresh.
--common-crawl [N] (default 2) reads board ids from the Common Crawl URL index of the N most recent crawls
(app/portal/common_crawl.py) instead of the shipped CSV lists, and registers the new ones with origin 'commoncrawl'
through the same dedupe and register_catalog path; the trickle reads them like any other catalogue board.
Idempotent: a second run registers nothing new."""
import argparse
import asyncio
import json
import random
import sys
import time

import httpx

from .. import board_catalog, netguard
from ..database import pack
from ..job_normalizer import us_location_status
from ..providers import boards as boards_mod
from ..providers.base import BoardNotFound, Ctx, HostLimiter, RobotsCache, SourceError
from ..providers.registry import SPECS

VALIDATE_LIMITS = {"greenhouse": 4, "ashby": 2, "lever": 2, "workday": 1, "workable": 1, "smartrecruiters": 1, "bamboohr": 1}
VALIDATE_INTERVALS = {"greenhouse": 0.5, "ashby": 1.0, "lever": 1.0, "workday": 1.5, "workable": 2.0,
                      "smartrecruiters": 1.0, "bamboohr": 1.0}


def _spec(ats: str):
    return SPECS.get(ats) or boards_mod.extra_specs(include_disabled=True).get(ats)


async def validate(ats: str, keys: list[str], n: int, client: httpx.AsyncClient, index=None, ttl: float = 3600.0) -> dict:
    spec = _spec(ats)
    sample = random.sample(keys, min(n, len(keys)))
    ctx = Ctx(client, HostLimiter(default=2, limits=dict(VALIDATE_LIMITS), intervals=dict(VALIDATE_INTERVALS)), RobotsCache(), 45.0,
              options={"workday_pages": 1})
    out = {"sampled": len(sample), "live": 0, "empty": 0, "dead": 0, "error": 0, "postings": 0, "us_postings": 0, "bytes": 0,
           "errors": []}

    async def one(key: str):
        try:
            jobs = await spec.fetch(ctx, key, "")
        except BoardNotFound:
            out["dead"] += 1
            if index is not None:
                await index.record_board(ats, key, False, 0, "catalog")
            return
        except (SourceError, httpx.HTTPError, ValueError) as exc:
            out["error"] += 1
            out["errors"].append(f"{key}: {str(exc)[:80] or type(exc).__name__}")
            return
        out["live" if jobs else "empty"] += 1
        out["postings"] += len(jobs)
        out["us_postings"] += sum(1 for j in jobs if us_location_status(j.location, j.country_hint)[0])
        out["bytes"] += sum(len(pack(j)) for j in jobs)
        if index is not None:
            from ..search_engine import _rows          # same rows as a normal refresh (read-only use of the engine module)
            await index.replace_unit(ats, key, "", await asyncio.to_thread(_rows, jobs, {}), ttl)
            await index.record_board(ats, key, True, len(jobs), "catalog")

    await asyncio.gather(*(one(k) for k in sample))
    ok = out["live"] + out["empty"]
    out["live_ratio"] = round(out["live"] / out["sampled"], 2) if out["sampled"] else 0.0
    out["postings_per_live_board"] = round(out["postings"] / out["live"], 1) if out["live"] else 0.0
    out["us_per_live_board"] = round(out["us_postings"] / out["live"], 1) if out["live"] else 0.0
    out["bytes_per_posting"] = round(out["bytes"] / out["postings"]) if out["postings"] else 0
    out["answered"] = ok
    return out


async def fetch_simplify(client: httpx.AsyncClient) -> dict[str, list[str]]:
    await netguard.assert_public_url(board_catalog.SIMPLIFY_URL)
    r = await client.get(board_catalog.SIMPLIFY_URL, timeout=60.0)
    r.raise_for_status()
    return board_catalog.simplify_keys(r.json())


async def main(argv: list[str] | None = None, index=None, client: httpx.AsyncClient | None = None) -> dict:
    ap = argparse.ArgumentParser(prog="python -m app.portal.import_boards")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="", help="comma-separated ATS names")
    ap.add_argument("--validate", type=int, default=0, metavar="N")
    ap.add_argument("--simplify", action="store_true", help="also take board ids from SimplifyJobs New-Grad-Positions (off by default)")
    ap.add_argument("--dsn", default="", help="PostgreSQL DSN (default: the app's DATABASE_URL)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--common-crawl", type=int, nargs="?", const=2, default=0, metavar="N",
                    help="discover board ids from the Common Crawl index of the N most recent crawls (default 2)")
    ap.add_argument("--cc-state", default="", help="resume file for --common-crawl (default ~/.cache/jobsearch/common_crawl_state.json)")
    ap.add_argument("--cc-max-pages", type=int, default=0, metavar="M", help="read at most M index pages per crawl and pattern")
    args = ap.parse_args(argv)
    only = [a.strip() for a in args.only.split(",") if a.strip()] or None
    bad = [a for a in only or [] if a not in board_catalog.CATALOG_ATS]
    if bad:
        ap.error(f"unknown ATS: {', '.join(bad)} (known: {', '.join(board_catalog.CATALOG_ATS)})")
    own_index = index is None
    if own_index:
        from .index_pg import PgJobIndex
        dsn = args.dsn
        if not dsn:
            from ..config import get_settings
            dsn = get_settings().postgres_dsn
        index = await PgJobIndex(dsn, max_size=4).connect()
    own_client = client is None
    client = client or httpx.AsyncClient(follow_redirects=False)
    try:
        cc_stats = None
        if args.common_crawl > 0:
            from .common_crawl import DEFAULT_STATE, CommonCrawl
            from pathlib import Path
            cc = CommonCrawl(client, Path(args.cc_state) if args.cc_state else DEFAULT_STATE)
            cc_stats = await cc.run(args.common_crawl, only, args.cc_max_pages)
            extra = {ats: list(st.keys.values()) for ats, st in cc_stats.items()}
            plans = await board_catalog.plan_import(index, [a for a in only or board_catalog.CATALOG_ATS if a in extra],
                                                    extra, use_csv=False)
            inserted = {} if args.dry_run else await board_catalog.register(index, plans, origin="commoncrawl")
        else:
            extra = await fetch_simplify(client) if args.simplify else None
            plans = await board_catalog.plan_import(index, only, extra)
            inserted = {} if args.dry_run else await board_catalog.register(index, plans)
        report = {"dry_run": args.dry_run, "ats": {}}
        for p in plans:
            report["ats"][p.ats] = {"new": len(p.new), "existing": p.existing, "skipped": p.skipped,
                                    "registered": inserted.get(p.ats, 0)}
            if cc_stats and p.ats in cc_stats:
                st = cc_stats[p.ats]
                report["ats"][p.ats]["common_crawl"] = {"urls": st.urls, "distinct": len(st.keys), "pages": st.blocks,
                                                        "resumed": st.resumed, "failures": st.failures,
                                                        "seconds": round(st.seconds, 1)}
        if args.validate > 0:
            for p in plans:
                started = time.monotonic()
                v = await validate(p.ats, p.new or board_catalog.load_csv(p.ats).keys, args.validate, client,
                                   None if args.dry_run else index)
                v["seconds"] = round(time.monotonic() - started, 1)
                report["ats"][p.ats]["validation"] = v
        return report
    finally:
        if own_client:
            await client.aclose()
        if own_index:
            await index.close()


def _print(report: dict) -> None:
    print(("DRY RUN - nothing written\n" if report["dry_run"] else "") + f"{'ats':16} {'new':>7} {'existing':>9} {'skipped':>8} {'registered':>11}")
    for ats, r in report["ats"].items():
        print(f"{ats:16} {r['new']:>7} {r['existing']:>9} {r['skipped']:>8} {r['registered']:>11}")
        c = r.get("common_crawl")
        if c:
            print(f"  common crawl: urls {c['urls']} distinct {c['distinct']} new {r['new']} pages {c['pages']} "
                  f"(resumed {c['resumed']}) failures {c['failures']} ({c['seconds']} s)")
        v = r.get("validation")
        if v:
            print(f"  validate {v['sampled']}: live {v['live']} empty {v['empty']} dead {v['dead']} error {v['error']} "
                  f"ratio {v['live_ratio']} postings/live {v['postings_per_live_board']} US/live {v['us_per_live_board']} "
                  f"bytes/posting {v['bytes_per_posting']} ({v['seconds']} s)")
            for e in v["errors"][:5]:
                print("    " + e)


if __name__ == "__main__":
    rep = asyncio.run(main(sys.argv[1:]))
    print(json.dumps(rep, indent=1)) if "--json" in sys.argv else _print(rep)
