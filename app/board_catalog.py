"""Imported employer-board catalogue: loads the board lists in app/data/boards/ (MIT, see SOURCE.txt there),
normalizes ids per ATS, dedupes them against the seed list and the index, and reads registered boards progressively.

How imported boards reach searches (details in docs/board-sources.md):
1. `python -m app.portal.import_boards` registers them in index_boards with origin 'catalog' and checked_at NULL.
   Nothing is fetched at that moment, and `known_boards()` ignores catalogue rows that never showed postings, so
   searches and prewarm keep planning exactly the boards they planned before.
2. `start_catalog_trickle(engine)` (called by the engine once, next to prewarm) reads a bounded number of due
   catalogue boards per minute per ATS through the engine's normal unit refresh (same limiter, robots.txt, 429 pause,
   INDEX_TTL_SECONDS). A board that answers with postings gets job_count > 0 and from then on is an ordinary known
   board, ranked by job_count like boards found by web search; a 404 board is marked dead.
3. The order inside one ATS's allowance (PgJobIndex.catalog_due; the per-minute allowance itself is the only thing
   that decides how many requests are made):
   - boards whose last read brought US postings are read again once BOARD_CATALOG_REFRESH_HOURS old, oldest first
     (Workday and Oracle tenants, which searches query per title anyway, once BOARD_CATALOG_RECHECK_DAYS old);
   - then boards never read (while any are waiting, refreshes take at most half of the allowance);
   - then boards whose last read failed (first retry after BOARD_CATALOG_RETRY_HOURS) or brought no US posting (first
     re-check after BOARD_CATALOG_RECHECK_DAYS); each further miss in a row doubles the wait;
   - last, dead boards (404): tried again after BOARD_CATALOG_DEAD_DAYS, doubling likewise.
   No wait grows beyond BOARD_CATALOG_MAX_BACKOFF_DAYS. `note_read` (called by the engine after every board read,
   whoever asked for it) records the outcome: US postings found, or a miss.
Workable: its registry entry is query-based, but the same entry reads one employer account when it is given an
account id (providers/workable.py), so catalogue accounts are read here like the boards of the keyed sources."""
import asyncio
import csv
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .job_normalizer import us_location_status
from .providers import ats_extra, ats_startup
from .providers.boards import SEED_BOARDS, WORKDAY_SITES

log = logging.getLogger(__name__)
DATA_DIR = Path(__file__).parent / "data" / "boards"
CATALOG_ATS = ("greenhouse", "lever", "ashby", "workable", "workday", "smartrecruiters", "bamboohr",
               "recruitee", "pinpoint", "rippling")
SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Settings (to be moved into app/config.py). Boards read per minute per ATS by the trickle; 0 switches an ATS off.
# Recruitee, 15 a minute: every Recruitee board is its own employer host ({company}.recruitee.com) and one read is two
# requests to that host (its robots.txt, then the one Careers Site API call that returns the whole board), after which
# the host is left alone for at least BOARD_CATALOG_REFRESH_HOURS. So the rate does not decide how hard any employer is
# asked, only how much of the shared Recruitee pace the trickle takes: the limiter allows one request a second for the
# whole platform (search_engine.HOST_INTERVALS), 15 boards are 30 requests, half of each minute; the other half stays
# free for searches. 2,956 catalogue boards / 15 = 3.3 hours for the first pass (it was 8.2 hours at 6).
# Workable, 4 a minute (unchanged, but only now in effect): all accounts live on one host, apply.workable.com.
DEFAULT_PER_MINUTE = {"greenhouse": 20, "lever": 8, "ashby": 8, "workable": 4, "workday": 2, "smartrecruiters": 4, "bamboohr": 6,
                      "recruitee": 15, "pinpoint": 6, "rippling": 4,
                      # remembered postings on employers' own sites (registry.CAREER_PAGE): one page request each, to that
                      # employer's own host, at most once a day per page
                      "career_page": 12}
PER_MINUTE = {ats: _env_int(f"BOARD_CATALOG_PER_MINUTE_{ats.upper()}", n) for ats, n in DEFAULT_PER_MINUTE.items()}
TRICKLE_ENABLED = os.environ.get("BOARD_CATALOG_TRICKLE", "1").strip().lower() not in ("0", "false", "no", "off")
RECHECK_DAYS = _env_int("BOARD_CATALOG_RECHECK_DAYS", 7)          # first re-check of a board that had no US posting
REFRESH_HOURS = _env_int("BOARD_CATALOG_REFRESH_HOURS", 24)       # a board with US postings is read again when this old
RETRY_HOURS = _env_int("BOARD_CATALOG_RETRY_HOURS", 6)            # first retry of a board whose read failed
DEAD_DAYS = _env_int("BOARD_CATALOG_DEAD_DAYS", 30)               # first retry of a board that answered "not found"
MAX_BACKOFF_DAYS = _env_int("BOARD_CATALOG_MAX_BACKOFF_DAYS", 90) # no wait doubles beyond this
# Sources whose registry entry is query-based (not `keyed`) but reads one employer account when given its id.
ACCOUNT_SOURCES = ("workable",)
MAX_INFLIGHT = _env_int("BOARD_CATALOG_MAX_INFLIGHT", 150)       # skip a tick while the engine is this busy

_SIMPLE = re.compile(r"[a-z0-9](?:[a-z0-9_.-]{0,98}[a-z0-9])?")
_WD_HOST = re.compile(r"([a-z0-9][a-z0-9-]{0,62})\.(wd\d{1,3})\.myworkdayjobs\.com")
_WD_SITE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,99}")
_LOCALE = re.compile(r"[a-z]{2}-[A-Za-z]{2}")


def normalize(ats: str, slug: str, url: str = "") -> str | None:
    """Engine key for one catalogue row, or None when it is not a well-formed id (never a path, never a host)."""
    slug = unquote((slug or "").strip())
    if ats == "workday":
        try:
            p = urlsplit((url or "").strip())
        except ValueError:
            return None
        m = _WD_HOST.fullmatch((p.hostname or "").lower())
        segs = [s for s in (p.path or "").split("/") if s and not _LOCALE.fullmatch(s)]
        if not m or not segs or p.scheme != "https":
            return None
        site = unquote(segs[0])
        return f"{m.group(1)}|{m.group(2)}|{site}" if _WD_SITE.fullmatch(site) else None
    if ats == "smartrecruiters":
        return slug if ats_extra.SR_SLUG.fullmatch(slug) and ".." not in slug else None
    if ats == "bamboohr":
        slug = slug.lower()
        return slug if ats_extra.BAMBOO_SLUG.fullmatch(slug) else None
    if ats in ("greenhouse", "lever", "ashby", "workable"):
        slug = slug.lower()
        return slug if _SIMPLE.fullmatch(slug) and ".." not in slug else None
    if ats in ats_startup.SLUGS:
        # Recruitee / Pinpoint: the career-site subdomain; Rippling: the board slug. When the row has an address it must
        # be that board's own address on the vendor's host (rows on an employer's own domain cannot be read by slug).
        slug = slug.lower()
        if not ats_startup.valid_key(ats, slug):
            return None
        if (url or "").strip():
            from .job_normalizer import parse_ats
            parsed = parse_ats(url)
            if not parsed or parsed[:2] != (ats, slug) or not url.strip().startswith("https://"):
                return None
        return slug
    return None


def _fold(ats: str, key: str) -> str:
    return key if ats == "smartrecruiters" else key.lower()       # SmartRecruiters ids are case-sensitive


def seed_keys(ats: str) -> set[str]:
    if ats == "workday":
        return {_fold(ats, k) for k in WORKDAY_SITES}
    return {_fold(ats, k) for k in SEED_BOARDS.get(ats, [])}


@dataclass
class Loaded:
    keys: list[str] = field(default_factory=list)      # normalized, unique within the file, in file order
    rows: int = 0
    malformed: int = 0
    duplicates: int = 0


def load_csv(ats: str, path: Path | None = None) -> Loaded:
    path = path or DATA_DIR / f"{ats}.csv"
    out, seen = Loaded(), set()
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            out.rows += 1
            key = normalize(ats, row.get("slug") or "", row.get("url") or "")
            if key is None:
                out.malformed += 1
                continue
            folded = _fold(ats, key)
            if folded in seen:
                out.duplicates += 1
                continue
            seen.add(folded)
            out.keys.append(key)
    return out


def simplify_keys(listings) -> dict[str, list[str]]:
    """Board identifiers from SimplifyJobs listings.json content (only the ids are kept; listing content is discarded)."""
    from .job_normalizer import parse_ats
    out: dict[str, dict[str, str]] = {}
    for item in listings if isinstance(listings, list) else []:
        if not isinstance(item, dict) or item.get("active") is False:
            continue
        url = str(item.get("url") or "")
        parsed = parse_ats(url)
        key = None
        if parsed and parsed[0] in ("greenhouse", "lever", "ashby", "workable", *ats_startup.SLUGS) and parsed[1]:
            ats, key = parsed[0], normalize(parsed[0], parsed[1])
        elif parsed and parsed[0] == "workday":
            ats, key = "workday", normalize("workday", "", url)
        else:
            m = re.match(r"https://(?:jobs|careers)\.smartrecruiters\.com/([^/?#]+)", url)
            if m:
                ats, key = "smartrecruiters", normalize("smartrecruiters", m.group(1))
        if key:
            out.setdefault(ats, {}).setdefault(_fold(ats, key), key)
    return {ats: list(d.values()) for ats, d in out.items()}


@dataclass
class Plan:
    ats: str
    new: list[str]
    existing: int
    skipped: int          # malformed rows + duplicates inside the list


async def plan_import(index, only: list[str] | None = None, extra: dict[str, list[str]] | None = None,
                      use_csv: bool = True) -> list[Plan]:
    """What an import would register, per ATS. `index` needs `board_slugs(ats)` (PgJobIndex) or may be None."""
    plans = []
    for ats in only or CATALOG_ATS:
        loaded = load_csv(ats) if use_csv else Loaded()
        keys, skipped = list(loaded.keys), loaded.malformed + loaded.duplicates
        folded = {_fold(ats, k) for k in keys}
        for k in (extra or {}).get(ats, []):
            if _fold(ats, k) not in folded:
                folded.add(_fold(ats, k))
                keys.append(k)
        known = seed_keys(ats)
        if index is not None and hasattr(index, "board_slugs"):
            known |= {_fold(ats, s) for s in await index.board_slugs(ats)}
        new = [k for k in keys if _fold(ats, k) not in known]
        plans.append(Plan(ats, new, len(keys) - len(new), skipped))
    return plans


async def register(index, plans: list[Plan], chunk: int = 2000, origin: str = "catalog") -> dict[str, int]:
    inserted = {}
    for plan in plans:
        n = 0
        for i in range(0, len(plan.new), chunk):
            pairs = [(plan.ats, k) for k in plan.new[i: i + chunk]]
            n += await (index.register_catalog(pairs, origin) if origin != "catalog" else index.register_catalog(pairs))
        inserted[plan.ats] = n
    return inserted


# ---------------------------------------------------------------- progressive reading (engine hook)
async def catalog_tick(engine, per_minute: dict[str, int] | None = None) -> int:
    """Start reading the next due catalogue boards (at most per_minute[ats] each). Returns how many were started."""
    from .providers.registry import Unit
    index = engine.index
    if not hasattr(index, "catalog_due") or len(getattr(engine, "_inflight", {})) > MAX_INFLIGHT:
        return 0
    rates = per_minute or PER_MINUTE
    wanted = {ats: n for ats, n in rates.items()
              if n > 0 and ats in engine.specs and (engine.specs[ats].keyed or ats in ACCOUNT_SOURCES)}
    # Sources that are searched per title (Workday, Oracle) are kept fresh by the searches themselves; here a read only
    # proves that the tenant is alive, so a live one is looked at again after the re-check time, not the refresh time.
    queried = {ats: n for ats, n in wanted.items() if engine.specs[ats].kind == "search" and engine.specs[ats].keyed}
    boards = {ats: n for ats, n in wanted.items() if ats not in queried}
    waits = (RETRY_HOURS * 3600.0, DEAD_DAYS * 86400.0, MAX_BACKOFF_DAYS * 86400.0)
    due = await index.catalog_due(boards, RECHECK_DAYS * 86400.0, REFRESH_HOURS * 3600.0, *waits) if boards else []
    if queried:
        due += await index.catalog_due(queried, RECHECK_DAYS * 86400.0, RECHECK_DAYS * 86400.0, *waits)
    await index.catalog_touch(due)
    for ats, key in due:
        engine._spawn(Unit(ats, key, ""), "catalog")
    return len(due)


def us_postings(jobs) -> int:
    """How many of a board's postings are in, or open to, the United States (the rule searches screen with)."""
    return sum(1 for j in jobs if us_location_status(j.location, j.country_hint)[0])


_note_failed = False


async def note_read(index, unit, jobs) -> None:
    """Engine hook, after every read of one employer board (catalogue trickle or search): `jobs` are the postings read,
    None when the read failed. Stores what catalog_due orders by. Never raises: a read must not fail over this note
    (for example while the migration that adds the columns has not run yet)."""
    global _note_failed
    from .providers.registry import SPECS
    spec = SPECS.get(unit.src)
    if unit.scope or unit.key == "*" or not hasattr(index, "catalog_result") or not (
            (spec and spec.keyed) or unit.src in ACCOUNT_SOURCES):
        return
    try:
        count = None if jobs is None else us_postings(jobs) if len(jobs) < 200 else await asyncio.to_thread(us_postings, jobs)
        await index.catalog_result(unit.src, unit.key, count)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if not _note_failed:
            _note_failed = True
            log.warning("catalogue read history is not being recorded: %s", type(exc).__name__)


def start_catalog_trickle(engine, interval: float = 60.0, per_minute: dict[str, int] | None = None) -> asyncio.Task | None:
    """Engine hook: call once after the engine is built (for example in SearchEngine.prewarm). Reads the imported
    catalogue at the bounded rate above until every board has been read once, and keeps the boards with US postings
    fresh; boards without them are re-checked at growing intervals (module docstring, point 3)."""
    if not TRICKLE_ENABLED:
        return None
    scale = interval / 60.0

    async def loop():
        while True:
            try:
                rates = {a: max(1, round(n * scale)) if n > 0 else 0 for a, n in (per_minute or PER_MINUTE).items()}
                await catalog_tick(engine, rates)
            except asyncio.CancelledError:
                raise
            except Exception as exc:          # never let the trickle die on one bad tick
                log.warning("catalog trickle: %s", type(exc).__name__)
            await asyncio.sleep(interval)

    task = asyncio.create_task(loop())
    background = getattr(engine, "_background", None)
    if isinstance(background, set):
        background.add(task)
        task.add_done_callback(background.discard)
    return task
