"""Board discovery from the Common Crawl URL index (free, open data).

Used by `python -m app.portal.import_boards --common-crawl [N]`. For each of the N most recent crawls it reads the
crawl's CDX index for the ATS host patterns below and keeps only board ids (no page content is fetched or stored).

Why the index files and not the CDX API: https://index.commoncrawl.org/robots.txt disallows `/CC-MAIN-*-index` for
every user agent (only collinfo.json and a few pages are allowed), while https://data.commoncrawl.org/robots.txt says
its disallow is for crawlers and "you are welcome to download our data via https". So the crawl list comes from
index.commoncrawl.org/collinfo.json and the URL index from the same shards the API serves, read from
data.commoncrawl.org: `cluster.idx` (sorted SURT keys, one line per block of about 3,000 index lines) is binary
searched with HTTP Range requests, then each matching block (one gzip member of a cdx-NNNNN.gz shard) is fetched by
byte range. A block is the same unit as one page of the CDX API (`showNumPages`/`page=K`).

Politeness: one request at a time, a gap between requests, a User-Agent naming the tool, exponential backoff with
jitter (up to 5 minutes) on 429/5xx/timeouts, and per-(crawl, pattern, block) resume from a JSON state file."""
import asyncio
import gzip
import json
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx

from .. import board_catalog

COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"
DATA_BASE = "https://data.commoncrawl.org/cc-index/collections"
USER_AGENT = "jobsearch-board-discovery/1.0 (Common Crawl index reader; one request at a time)"
DEFAULT_STATE = Path.home() / ".cache" / "jobsearch" / "common_crawl_state.json"

# (pattern as the CDX API would take it, ATS, SURT prefix in cluster.idx / cdx lines)
PATTERNS = [
    ("job-boards.greenhouse.io/*", "greenhouse", "io,greenhouse,job-boards)/"),
    ("boards.greenhouse.io/*", "greenhouse", "io,greenhouse,boards)/"),
    ("jobs.lever.co/*", "lever", "co,lever,jobs)/"),
    ("jobs.ashbyhq.com/*", "ashby", "com,ashbyhq,jobs)/"),
    ("apply.workable.com/*", "workable", "com,workable,apply)/"),
    ("*.myworkdayjobs.com/*", "workday", "com,myworkdayjobs,"),
]
# First path segments that are pages of the ATS itself, not employer boards.
_RESERVED = {"embed", "api", "j", "static", "assets", "robots.txt", "favicon.ico", "sitemap.xml", "sitemap", "oauth",
             "login", "signin", "privacy", "terms", "careers", "jobs", "search", "_next", "v1", "v2", "cdn-cgi", "wday"}
_HOSTS = {"greenhouse": {"job-boards.greenhouse.io", "boards.greenhouse.io"}, "lever": {"jobs.lever.co"},
          "ashby": {"jobs.ashbyhq.com"}, "workable": {"apply.workable.com"}}


def board_key(ats: str, url: str) -> str | None:
    """Engine key of the employer board a crawled URL belongs to, or None."""
    try:
        p = urlsplit((url or "").strip())
    except ValueError:
        return None
    if ats == "workday":
        return board_catalog.normalize("workday", "", "https://" + (p.hostname or "") + (p.path or ""))
    if (p.hostname or "").lower() not in _HOSTS.get(ats, ()):
        return None
    segs = [s for s in (p.path or "").split("/") if s]
    if not segs:
        return None
    first = unquote(segs[0]).lower()
    if ats == "greenhouse" and first == "embed":                 # /embed/job_board?for=<board>
        first = dict(parse_qsl(p.query)).get("for", "").lower()
    if not first or first in _RESERVED or first.endswith((".txt", ".xml", ".ico", ".js", ".css", ".png", ".html")):
        return None
    return board_catalog.normalize(ats, first)


def parse_cdx_line(line: str) -> dict | None:
    """`<surt> <timestamp> {json}` -> the JSON fields (url, status, ...)."""
    parts = line.split(" ", 2)
    if len(parts) < 3:
        return None
    try:
        data = json.loads(parts[2])
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("url") else None


def keys_from_lines(ats: str, lines, prefix: str) -> tuple[int, dict[str, str]]:
    """(URLs read, {folded key: key}) for index lines inside `prefix`. 404/410 captures are not counted as boards."""
    n, out = 0, {}
    for line in lines:
        if not line.startswith(prefix):
            continue
        row = parse_cdx_line(line)
        if row is None:
            continue
        n += 1
        if str(row.get("status", "")) in ("404", "410"):
            continue
        key = board_key(ats, str(row["url"]))
        if key:
            out.setdefault(board_catalog._fold(ats, key), key)
    return n, out


@dataclass
class Block:
    shard: str
    offset: int
    length: int
    seq: int


def blocks_for(cluster_lines: list[str], prefix: str) -> list[Block]:
    """Blocks of a (sorted) cluster.idx slice that may hold keys starting with `prefix`: the last block whose first
    key sorts before the prefix, then every block whose first key starts with it."""
    out, before = [], None
    for line in cluster_lines:
        cols = line.split("\t")
        if len(cols) < 5:
            continue
        key = cols[0].split(" ")[0]
        blk = Block(cols[1], int(cols[2]), int(cols[3]), int(cols[4]))
        if key < prefix:
            before = blk
        elif key.startswith(prefix):
            out.append(blk)
        else:
            break
    return ([before] if before else []) + out


@dataclass
class AtsStats:
    urls: int = 0
    keys: dict[str, str] = field(default_factory=dict)
    blocks: int = 0
    resumed: int = 0
    failures: int = 0
    seconds: float = 0.0


class CommonCrawl:
    def __init__(self, client: httpx.AsyncClient, state_path: Path | None = None, gap: float = 1.0,
                 max_backoff: float = 300.0, attempts: int = 8, sleep=asyncio.sleep, out=sys.stderr):
        self.client, self.gap, self.max_backoff, self.attempts = client, gap, max_backoff, attempts
        self.state_path = state_path
        self._sleep, self.out = sleep, out
        self._last = 0.0
        self.requests = 0
        self.state = {"done": {}}
        if state_path and state_path.exists():
            try:
                self.state = json.loads(state_path.read_text())
                self.state.setdefault("done", {})
            except ValueError:
                pass

    def _save(self) -> None:
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state))
            tmp.replace(self.state_path)

    def _log(self, msg: str) -> None:
        if self.out:
            print(msg, file=self.out, flush=True)

    async def get(self, url: str, rng: tuple[int, int] | None = None) -> bytes:
        """One polite GET with retries. Raises the last error after `attempts` tries."""
        headers = {"User-Agent": USER_AGENT}
        if rng:
            headers["Range"] = f"bytes={rng[0]}-{rng[1]}"
        last: Exception | None = None
        for attempt in range(self.attempts):
            wait = self._last + self.gap - time.monotonic()
            if wait > 0:
                await self._sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            try:
                r = await self.client.get(url, headers=headers, timeout=60.0, follow_redirects=False)
                if r.status_code in (200, 206):
                    return r.content
                last = httpx.HTTPStatusError(str(r.status_code), request=r.request, response=r)
                if r.status_code not in (429, 500, 502, 503, 504):
                    raise last
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
            delay = min(self.max_backoff, 20.0 * 2 ** attempt) * random.uniform(0.75, 1.25)
            self._log(f"  retry {attempt + 1}/{self.attempts} in {delay:.0f} s ({str(last) or type(last).__name__})")
            await self._sleep(delay)
        raise last or RuntimeError("no answer")

    async def crawls(self, n: int) -> list[str]:
        data = json.loads(await self.get(COLLINFO_URL))
        return [str(c["id"]) for c in data if isinstance(c, dict) and str(c.get("id", "")).startswith("CC-MAIN-")][:n]

    async def _cluster_size(self, crawl: str) -> int:
        r = await self.client.head(f"{DATA_BASE}/{crawl}/indexes/cluster.idx", headers={"User-Agent": USER_AGENT},
                                   timeout=60.0)
        r.raise_for_status()
        return int(r.headers["content-length"])

    async def _first_key_at(self, url: str, pos: int) -> tuple[str, int]:
        """First complete cluster.idx line at or after byte `pos`: (key, its byte offset)."""
        chunk = await self.get(url, (pos, pos + 4095))
        start = 0 if pos == 0 else chunk.find(b"\n") + 1
        end = chunk.find(b"\n", start)
        line = chunk[start:end if end > 0 else len(chunk)].decode("utf-8", "replace")
        return line.split(" ")[0], pos + start

    async def cluster_slice(self, crawl: str, prefix: str) -> list[str]:
        """cluster.idx lines from just before `prefix` to just after it, by binary search over byte ranges."""
        url = f"{DATA_BASE}/{crawl}/indexes/cluster.idx"
        lo, hi = 0, await self._cluster_size(crawl)
        while hi - lo > 65536:
            mid = (lo + hi) // 2
            key, _ = await self._first_key_at(url, mid)
            if key < prefix:
                lo = mid
            else:
                hi = mid
        lines, pos, buf = [], lo, b""
        while True:
            chunk = await self.get(url, (pos, pos + 262143))
            if not chunk:
                break
            buf += chunk
            pos += len(chunk)
            *full, buf = buf.split(b"\n")
            if pos - len(chunk) == lo and lo > 0:
                full = full[1:]                     # first line may be cut
            for raw in full:
                line = raw.decode("utf-8", "replace")
                lines.append(line)
            if lines and lines[-1].split(" ")[0] > prefix and not lines[-1].startswith(prefix):
                break
            if len(chunk) < 262144:
                break
        return lines

    async def run(self, n_crawls: int = 2, only: list[str] | None = None, max_blocks: int = 0) -> dict[str, AtsStats]:
        stats: dict[str, AtsStats] = {}
        for crawl in await self.crawls(n_crawls):
            for pattern, ats, prefix in PATTERNS:
                if only and ats not in only:
                    continue
                st = stats.setdefault(ats, AtsStats())
                started = time.monotonic()
                try:
                    blocks = blocks_for(await self.cluster_slice(crawl, prefix), prefix)
                except Exception as exc:
                    st.failures += 1
                    self._log(f"{crawl} {pattern}: block list failed ({str(exc) or type(exc).__name__})")
                    st.seconds += time.monotonic() - started
                    continue
                if max_blocks and len(blocks) > max_blocks:
                    self._log(f"{crawl} {pattern}: {len(blocks)} blocks, reading the first {max_blocks}")
                    blocks = blocks[:max_blocks]
                for i, blk in enumerate(blocks, 1):
                    tag = f"{crawl}|{pattern}|{blk.seq}"
                    done = self.state["done"].get(tag)
                    if done is None:
                        try:
                            raw = await self.get(f"{DATA_BASE}/{crawl}/indexes/{blk.shard}",
                                                 (blk.offset, blk.offset + blk.length - 1))
                            text = gzip.decompress(raw).decode("utf-8", "replace").splitlines()
                        except Exception as exc:
                            st.failures += 1
                            self._log(f"{crawl} {pattern} page {i}/{len(blocks)}: failed ({str(exc) or type(exc).__name__})")
                            continue
                        n, keys = keys_from_lines(ats, text, prefix)
                        done = {"urls": n, "keys": list(keys.values())}
                        self.state["done"][tag] = done
                        self._save()
                    else:
                        st.resumed += 1
                    st.blocks += 1
                    st.urls += done["urls"]
                    for k in done["keys"]:
                        st.keys.setdefault(board_catalog._fold(ats, k), k)
                    self._log(f"{crawl} {pattern} page {i}/{len(blocks)}: {done['urls']} urls, "
                              f"{len(done['keys'])} ids, {len(st.keys)} distinct {ats} so far")
                st.seconds += time.monotonic() - started
        return stats
