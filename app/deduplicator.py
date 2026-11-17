"""Cross-source duplicate removal.

Priority 1: ATS + posting id.   Priority 2: canonical URL.
Priority 3: company + title + location.   Priority 4: conservative fuzzy match on posting details.
Two postings with different ids on the same ATS are never merged: they are distinct requisitions.
"""
import hashlib
import re
from difflib import SequenceMatcher

from .job_normalizer import ats_key, canonical_url, norm_company, norm_text, parse_ats
from .schemas import Job

_SOURCE_RANK = {"Greenhouse": 3, "Lever": 3, "Ashby": 3, "Workday": 3, "Workable": 3, "Amazon Jobs": 3}
_TITLE_ALIASES = [(r"\bsr\b", "senior"), (r"\bjr\b", "junior"), (r"\bml\b", "machine learning"),
                  (r"\bai\b", "artificial intelligence"), (r"\bswe\b", "software engineer"),
                  (r"\bii\b", "2"), (r"\biii\b", "3"), (r"\biv\b", "4")]


def norm_title(title: str) -> str:
    t = norm_text(title)
    for rx, rep in _TITLE_ALIASES:
        t = re.sub(rx, rep, t)
    return re.sub(r"\s+", " ", t).strip()


def norm_location(location: str) -> str:
    t = norm_text(location)
    t = re.sub(r"\b(united states of america|united states|usa|us|u s|remote|hybrid|onsite|on site|hq|office)\b", " ", t)
    return " ".join(sorted(set(t.split())))


def job_keys(job: Job) -> tuple[str, str, str]:
    """(ats key, canonical url, company|title|location) for one job."""
    a_key = ats_key(job.ats, job.ats_id)
    if not a_key:
        parsed = parse_ats(job.url) or parse_ats(job.apply_url)
        if parsed and parsed[2]:
            a_key = ats_key(parsed[0], parsed[2])
    c_url = canonical_url(job.url)
    ctl = f"{norm_company(job.company)}|{norm_title(job.title)}|{norm_location(job.location)}"
    return a_key, c_url, ctl


def stable_id(job: Job) -> str:
    a_key, c_url, ctl = job_keys(job)
    return hashlib.sha1((a_key or c_url or ctl).encode()).hexdigest()[:16]


class _Keys:
    """Comparison keys for one job, computed once (deduplication compares many pairs)."""
    __slots__ = ("akey", "ats", "url", "ctl", "company", "title", "digits", "loc")

    def __init__(self, job: Job):
        self.akey, self.url, self.ctl = job_keys(job)
        self.ats = self.akey.split(":")[0] if self.akey else ""
        self.company = norm_company(job.company)
        self.title = norm_title(job.title)
        self.digits = re.findall(r"\d+", self.title)
        self.loc = set(norm_location(job.location).split())


def _same_ats_different_id(ka: _Keys, kb: _Keys) -> bool:
    return bool(ka.akey and kb.akey and ka.akey != kb.akey and ka.ats == kb.ats)


def _locations_compatible(ka: _Keys, kb: _Keys) -> bool:
    if not ka.loc or not kb.loc or ka.loc == {"not", "listed"} or kb.loc == {"not", "listed"}:
        return True
    return bool(ka.loc & kb.loc)


def _fuzzy_same(a: Job, b: Job, ka: _Keys, kb: _Keys) -> bool:
    if ka.company != kb.company or _same_ats_different_id(ka, kb):
        return False
    # Digits distinguish levels ("Engineer 2" vs "Engineer 3"): never merge across them.
    if ka.digits != kb.digits:
        return False
    if a.aggregator or b.aggregator:
        # A job-board copy words the location its own way and often reformats the description, so neither
        # can be compared. Same employer + same title is treated as the same vacancy; the employer's own
        # record wins in merge().
        return ka.title == kb.title
    if ka.title != kb.title and SequenceMatcher(None, ka.title, kb.title).ratio() < 0.93:
        return False
    if not _locations_compatible(ka, kb):
        return False
    da, db = a.description[:1500], b.description[:1500]
    if da and db:
        return da == db or SequenceMatcher(None, da, db).ratio() >= 0.9
    return ka.title == kb.title   # one side has no description: require an exact normalized title


def _rank(job: Job) -> tuple:
    return (not job.aggregator, _SOURCE_RANK.get(job.source, 1), bool(job.apply_url), len(job.description), bool(job.posted_at))


def merge(a: Job, b: Job) -> Job:
    """Keep the strongest verified record; retain provenance from both."""
    best, other = (a, b) if _rank(a) >= _rank(b) else (b, a)
    best = best.model_copy(deep=True)
    for s in [*a.sources, *b.sources]:
        if s not in best.sources:
            best.sources.append(s)
    for field in ("apply_url", "posted_at", "ats", "ats_id", "board", "description", "verified_at"):
        if not getattr(best, field) and getattr(other, field):
            setattr(best, field, getattr(other, field))
    for field in ("salary", "experience_required", "work_mode", "location"):
        if getattr(best, field) == "Not listed" and getattr(other, field) != "Not listed":
            setattr(best, field, getattr(other, field))
    if other.score > best.score and other.score_source == "ai" and best.score_source != "ai":
        for field in ("score", "score_breakdown", "score_source", "matching_skills", "missing_skills", "reason", "experience_comparison"):
            setattr(best, field, getattr(other, field))
    return best


def deduplicate(jobs: list[Job]) -> tuple[list[Job], int]:
    """Return (unique jobs, number of duplicates removed).
    Employer records are processed before job-board copies, so a copy always merges into the employer's
    own posting when both were found, whatever order the sources answered in."""
    jobs = sorted(jobs, key=lambda j: j.aggregator)      # stable: employer records first
    unique: list[Job] = []
    keys: list[_Keys] = []
    by_ats: dict[str, int] = {}
    by_url: dict[str, int] = {}
    by_ctl: dict[str, int] = {}
    by_company: dict[str, list[int]] = {}
    removed = 0
    for job in jobs:
        k = _Keys(job)
        idx = None
        if k.akey and k.akey in by_ats:
            idx = by_ats[k.akey]
        elif k.url and k.url in by_url:
            idx = by_url[k.url]
        elif k.ctl in by_ctl and not _same_ats_different_id(k, keys[by_ctl[k.ctl]]):
            idx = by_ctl[k.ctl]
        else:
            for i in by_company.get(k.company, []):
                if _fuzzy_same(job, unique[i], k, keys[i]):
                    idx = i
                    break
        if idx is None:
            idx = len(unique)
            unique.append(job)
            keys.append(k)
            by_company.setdefault(k.company, []).append(idx)
        else:
            unique[idx] = merge(unique[idx], job)
            keys[idx] = _Keys(unique[idx])
            removed += 1
        merged = keys[idx]
        for key, table in ((k.akey, by_ats), (merged.akey, by_ats), (k.url, by_url), (merged.url, by_url)):
            if key:
                table.setdefault(key, idx)
        by_ctl.setdefault(k.ctl, idx)
        by_ctl.setdefault(merged.ctl, idx)
    for job, k in zip(unique, keys):
        job.id = hashlib.sha1((k.akey or k.url or k.ctl).encode()).hexdigest()[:16]
    return unique, removed
