"""The ATS platform filter and the startup focus of one search, and the per-platform statistics.

Both options are read from the request and from app/ats_platforms.py at run time; nothing here knows which
board readers exist (that is the registry's business: the engine passes the names of its enabled sources).

- Platform filter (`ats_platforms`, empty = all): only the board readers of the chosen platforms are read,
  search engines are asked only for hosts of those platforms, and a lead or posting of any other platform, or of
  a source that is not an ATS platform at all, is dropped before verification. A chosen platform without an
  enabled reader whose postings are public pages is searched through `site:` queries on its hosts; what is found
  is verified page by page like every web lead.
- Startup focus (`startup_focus`): the startup platforms (ats_platforms.STARTUP_PLATFORMS) are read first and get
  the larger share of the search-engine queries and of the new-board budget; at an equal match score a job of a
  startup platform ranks first. A score is never changed. With a filter, the focus only orders within it.

Statistics (see `platform_stats`): counts only. No text of any search result is kept."""
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.parse import urlsplit

from . import ats_platforms
from .job_normalizer import canonical_url, parse_ats
from .job_verifier import host_blocked
from .schemas import Job

OTHER = "other"                       # statistics row of sources that are not an ATS platform
OTHER_LABEL = "Other sources"
STARTUP_QUERY_RUN = 3                 # startup focus: three queries on startup platforms, then one on another platform
OTHER_BOARD_SHARE = 4                 # startup focus: other platforms may use a quarter of the new-board budget
REASONS = ("not_relevant", "non_us", "work_mode", "eligibility", "ai_audit")


def reader_platform(src: str) -> str:
    """The platform id a registry source name (also `Job.ats`) belongs to, "" when it is not an ATS platform."""
    if not src:
        return ""
    for platform in ats_platforms.PLATFORMS.values():
        if platform.reader == src:
            return platform.id
    return src if src in ats_platforms.PLATFORMS else ""


def host_platform(host: str) -> str:
    """The platform serving career pages on `host`; a bare domain ("myworkdayjobs.com") counts like its subdomains."""
    host = (host or "").lower().removeprefix("www.")
    found = ats_platforms.platform_of_host(host) or ats_platforms.platform_of_host("x." + host)
    return found.id if found else ""


def platform_of_url(url: str) -> str:
    parsed = parse_ats(url or "")
    if parsed and reader_platform(parsed[0]):
        return reader_platform(parsed[0])
    try:
        return host_platform(urlsplit((url or "").strip()).hostname or "")
    except ValueError:
        return ""


def platform_of_job(job: Job) -> str:
    return reader_platform(job.ats) or platform_of_url(job.url)


def count_platforms(jobs: Iterable[Job]) -> Counter:
    return Counter(platform_of_job(job) or OTHER for job in jobs)


def bump(by: dict | None, job: Job, key: str) -> None:
    """Add one to `key` in the per-platform counts `by` ({platform id: {key: n}}); nothing when `by` is None."""
    if by is not None:
        row = by.setdefault(platform_of_job(job) or OTHER, {})
        row[key] = row.get(key, 0) + 1


def coverage(platform: ats_platforms.Platform, enabled: bool) -> tuple[str, str]:
    """(level, one sentence) for a platform: "full" = a board reader is switched on; "limited" = public pages only,
    found through web search; "none" = nothing can be read (credentials needed, forbidden, or the reader is off)."""
    why = (platform.note or ats_platforms.ACCESS.get(platform.access, "")).strip()
    why = why + "." if why and not why.endswith(".") else why
    if enabled:
        return "full", why
    if platform.access == "public_pages" and platform.hosts:
        return "limited", (why + " They are found only through web search results.").strip()
    if platform.reader and platform.access == "public_api":
        return "none", "Its board reader is switched off or not installed on this server."
    return "none", why


@dataclass(frozen=True)
class Scope:
    chosen: frozenset = frozenset()          # platform ids; empty = every platform and every other source
    focus: bool = False
    readers: frozenset = frozenset()         # names of the engine's enabled sources
    preferred: frozenset = field(default=frozenset())      # platforms the focus puts first (within the filter)

    @classmethod
    def of(cls, ats: list[str] | None, focus: bool, readers: Iterable[str]) -> "Scope":
        chosen = frozenset(i for i in ats or [] if i in ats_platforms.PLATFORMS)
        startup = frozenset(ats_platforms.STARTUP_PLATFORMS)
        preferred = (startup & chosen if chosen else startup) if focus else frozenset()
        if chosen and preferred == chosen:
            preferred = frozenset()               # everything chosen is a startup platform: nothing to put first
        return cls(chosen, bool(focus), frozenset(readers), preferred)

    @property
    def filtered(self) -> bool:
        return bool(self.chosen)

    def allows(self, platform_id: str) -> bool:
        return not self.chosen or platform_id in self.chosen

    def allows_source(self, src: str) -> bool:
        return not self.chosen or reader_platform(src) in self.chosen

    def has_reader(self, platform_id: str) -> bool:
        platform = ats_platforms.PLATFORMS.get(platform_id)
        return bool(platform and platform.reader and platform.reader in self.readers)

    @property
    def page_platforms(self) -> frozenset:
        """Chosen platforms without an enabled reader whose postings are public pages."""
        return frozenset(i for i in self.chosen if not self.has_reader(i)
                         and ats_platforms.PLATFORMS[i].access == "public_pages" and ats_platforms.PLATFORMS[i].hosts)

    # ---- search-engine hosts and queries
    def hosts(self, configured: list[str]) -> list[str]:
        """Hosts for `site:` queries: the configured ATS hosts, limited to the chosen platforms (plus the public
        career-page hosts of chosen platforms without a reader); startup platforms first under the focus."""
        hosts = list(configured)
        if self.chosen:
            hosts = [h for h in hosts if host_platform(h) in self.chosen]
            for platform_id in sorted(self.page_platforms):
                for host in ats_platforms.PLATFORMS[platform_id].hosts:
                    host = host.lstrip(".").lower()
                    if host and not host_blocked("https://" + host + "/") and host not in hosts:
                        hosts.append(host)
        if self.preferred:
            hosts.sort(key=lambda h: host_platform(h) not in self.preferred)      # stable
        return hosts

    def order_queries(self, queries: list[str]) -> list[str]:
        """Under the focus: startup-platform queries first and in the majority (three, then one of another platform),
        so they get the larger share of whatever part of the query budget is spent."""
        if not self.preferred:
            return queries
        first = [q for q in queries if host_platform(q.split(" ", 1)[0].removeprefix("site:")) in self.preferred]
        chosen = set(first)
        rest = [q for q in queries if q not in chosen]
        out: list[str] = []
        while first or rest:
            out.extend(first[:STARTUP_QUERY_RUN])
            first = first[STARTUP_QUERY_RUN:]
            out.extend(rest[:1])
            rest = rest[1:]
        return out

    def order_units(self, units: list) -> list:
        if not self.preferred:
            return units
        return sorted(units, key=lambda u: reader_platform(u.src) not in self.preferred)      # stable

    def other_boards(self, new_boards: int) -> int | None:
        """Under the focus: how many of the search's new boards may be boards of other platforms (None = no cap)."""
        return max(1, new_boards // OTHER_BOARD_SHARE) if self.preferred and new_boards > 0 else None

    def page_lead(self, url: str) -> str:
        """The address to verify as a posting page when `url` is a well-formed https address on a public career-page
        host of a chosen platform without a reader; "" otherwise. Only the address is used."""
        if not self.chosen or not isinstance(url, str) or len(url) > 600 or any(c in url for c in "\n\r\\"):
            return ""
        try:
            p = urlsplit(url.strip())
            port = p.port
        except ValueError:
            return ""
        if p.scheme != "https" or not p.hostname or p.username or p.password or port not in (None, 443) or host_blocked(url):
            return ""
        if not [s for s in p.path.split("/") if s]:
            return ""                              # the bare host is never a posting
        return canonical_url(url) if host_platform(p.hostname) in self.page_platforms else ""

    # ---- ranking
    def order_key(self, base: Callable[[Job], tuple]) -> Callable[[Job], tuple]:
        """`base` with one more key right after the score: at an equal score (and the same kind of scorer) a job of
        a preferred platform comes first. The score itself, and everything ranked above it, is untouched."""
        if not self.preferred:
            return base

        def key(job: Job) -> tuple:
            k = base(job)
            return (*k[:2], 0 if platform_of_job(job) in self.preferred else 1, *k[2:])
        return key

    # ---- coverage
    def coverage_warnings(self, found: dict | None = None) -> list[str]:
        """One sentence per chosen platform that has no enabled reader. `found` (statistics rows) adds whether
        anything was found for it in this search."""
        out = []
        for platform_id in sorted(self.chosen):
            platform = ats_platforms.PLATFORMS[platform_id]
            level, why = coverage(platform, self.has_reader(platform_id))
            if level == "full":
                continue
            nothing = found is not None and not (found.get(platform_id) or {}).get("considered")
            if level == "limited":
                text = f"{platform.label} has limited coverage: {why}"
                if nothing:
                    text += " Nothing was found for it in this search."
            else:
                text = f"{platform.label} could not be searched: {why}"
            out.append(text)
        return out


NO_SCOPE = Scope()


def platform_stats(scope: Scope, parts: list[dict], unique: Counter, duplicates: Counter, matched: Counter,
                   leads: Counter, audited: Counter | None = None) -> dict:
    """Per-platform counts of one search, keyed by platform id ("other" = sources that are not an ATS platform):

    - considered: postings of the platform with a matching title that were screened (from the index and from web leads);
    - verified:   postings that passed the screening (live, in the United States, eligible), copies included;
    - unique:     verified postings left after deduplication ("unique jobs found"); verified = unique + duplicates;
    - rejected / reasons: screened out, by reason (not_relevant, non_us, work_mode, eligibility, ai_audit);
    - duplicates: copies of a posting already kept;
    - matched:    postings in the final result list;
    - leads:      addresses on the platform named by a search engine or an AI provider.

    A row exists for every chosen platform and for every platform with a non-zero count."""
    rows: dict[str, dict] = {}

    def row(platform_id: str) -> dict:
        if platform_id not in rows:
            platform = ats_platforms.PLATFORMS.get(platform_id)
            rows[platform_id] = {
                "label": platform.label if platform else OTHER_LABEL, "startup": bool(platform and platform.startup),
                "coverage": coverage(platform, scope.has_reader(platform_id))[0] if platform else "",
                "considered": 0, "verified": 0, "unique": 0, "rejected": 0, "reasons": {}, "duplicates": 0, "matched": 0, "leads": 0}
        return rows[platform_id]

    for platform_id in [i for i in ats_platforms.PLATFORMS if i in scope.chosen]:
        row(platform_id)
    for part in [*parts, {k: {"ai_audit": v} for k, v in (audited or {}).items()}]:
        for platform_id, counts in part.items():
            r = row(platform_id)
            r["considered"] += counts.get("considered", 0)
            for reason in REASONS:
                if counts.get(reason):
                    r["reasons"][reason] = r["reasons"].get(reason, 0) + counts[reason]
    for name, counter in (("unique", unique), ("duplicates", duplicates), ("matched", matched), ("leads", leads)):
        for platform_id, n in counter.items():
            if n > 0:
                row(platform_id)[name] = n
    for r in rows.values():
        r["rejected"] = sum(r["reasons"].values())
        r["verified"] = r["unique"] + r["duplicates"]
    return rows
