"""Job-title suggestions for the Find Jobs form.

Two kinds, both help only (the form takes any typed title, with or without them):

While typing     titles that postings in the job index really carry (app/job_titles.py cleans them: no requisition
                 number, place or "(Remote)"), the most frequent first, plus the selected candidates' own titles.
                 The titles are read from the index once every few hours with one aggregate query and matched in
                 memory; typing never queries the index.
From the resume  for the selected candidates: the titles saved in the profile, the titles found in the resume when it
                 was uploaded, and, when a model is configured and answers in time, other names employers use for the
                 same work. A model answer is remembered per candidate, resume version and profile, so the same
                 candidate is not asked about twice. Without a model, or when it does not answer, the answer says so
                 and holds the saved and resume titles only.

The caller's scope is applied to every candidate id before anything is read: a Team Lead gets titles for candidates
of their own team only. No resume text leaves the server, only titles (short, cleaned strings)."""
import asyncio
import hashlib
import json
import logging
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from ...job_titles import TitleVocabulary, matches, resume_titles, title_key, _query_words
from ...rank_cache import PREFIX as RANK_CACHE_PREFIX
from ...schemas import Profile
from ...search_plan import clean_variants
from ..deps import Principal, candidate_scope
from ..models import Candidate, CandidateProfile, Resume

log = logging.getLogger("jobfinder.titles")

SUGGESTION_LIMIT = 8
SUGGESTIONS_PER_MINUTE = 120            # typed suggestions per person; the form waits between keystrokes
RESUME_REQUESTS_PER_MINUTE = 20         # "from the resume" requests per person
MAX_CANDIDATES = 50                     # as many as one search can take
PER_CANDIDATE = 8                       # titles offered per candidate

VOCABULARY_TITLES = 20000               # distinct cleaned titles kept in memory
VOCABULARY_ROWS = 80000                 # distinct posting titles read from the index, the most frequent first
VOCABULARY_SECONDS = 6 * 3600.0         # read again after this long
VOCABULARY_RETRY_SECONDS = 300.0        # ... or after this long when a read failed
VOCABULARY_FIRST_WAIT = 1.5             # the first request waits this long for the first read, then answers without it
VOCABULARY_SQL_TIMEOUT_MS = 20000

MODEL_CANDIDATES = 8                    # candidates per request a model is asked about; the rest get their own titles
MODEL_WAIT_SECONDS = 4.0                # how long a request waits for model answers before it answers without them
MODEL_DEADLINE_SECONDS = 30.0           # a model call still running after this long is given up
MODEL_MEMORY_SECONDS = 7 * 86400.0
# Under the ranking memory's prefix on purpose: these rows are model output about candidates too, so they are pruned
# by the same rule (rank_cache.RankCache.prune) and deleted with the test data when test mode ends (settings.go_live).
MEMORY_PREFIX = RANK_CACHE_PREFIX + "titles:"


# ---------------------------------------------------------------- the title vocabulary
class VocabularyCache:
    """The titles of the job index, held in this process. `current()` never waits for the database except on the
    very first request, and then only briefly."""

    def __init__(self, sessions, clock=time.monotonic):
        self._sessions = sessions
        self._clock = clock
        self.vocabulary = TitleVocabulary()
        self.loaded = False                              # a read has succeeded at least once
        self._next = 0.0
        self._task: asyncio.Task | None = None

    async def _read(self) -> None:
        try:
            async with self._sessions() as db:
                await db.execute(text(f"SET LOCAL statement_timeout = {VOCABULARY_SQL_TIMEOUT_MS}"))
                rows = (await db.execute(text(
                    "SELECT title, count(*) AS n FROM index_postings GROUP BY title ORDER BY n DESC, title LIMIT :rows"),
                    {"rows": VOCABULARY_ROWS})).all()
            self.vocabulary = await asyncio.to_thread(TitleVocabulary, [(row[0], row[1]) for row in rows], VOCABULARY_TITLES)
            self.loaded = True
            self._next = self._clock() + VOCABULARY_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception as exc:                         # the suggestions are help: without them the form still works
            log.warning("job-title vocabulary was not read (%s)", type(exc).__name__)
            self._next = self._clock() + VOCABULARY_RETRY_SECONDS

    def refresh(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._next = self._clock() + VOCABULARY_RETRY_SECONDS      # one read at a time
            self._task = asyncio.create_task(self._read())
        return self._task

    async def current(self) -> TitleVocabulary:
        if self._clock() >= self._next:
            task = self.refresh()
            if not self.loaded:
                await asyncio.wait({task}, timeout=VOCABULARY_FIRST_WAIT)
        return self.vocabulary

    async def aclose(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


def vocabulary_of(app) -> VocabularyCache:
    cache = getattr(app.state, "title_vocabulary", None)
    if cache is None:
        cache = app.state.title_vocabulary = VocabularyCache(app.state.sessions)
    return cache


# ---------------------------------------------------------------- limits and scope
async def throttle(request, key: str, per_minute: int) -> None:
    limiter = request.app.state.limiter
    if await limiter.count(key, 60) >= per_minute:
        raise HTTPException(429, "Too many requests for title suggestions. Wait a moment; you can keep typing your own titles.")
    await limiter.hit(key, 60)


def parse_ids(raw: str | None) -> list[uuid.UUID]:
    """Candidate ids from a comma-separated query value. Anything that is not an id is refused, not skipped."""
    out: list[uuid.UUID] = []
    for part in (raw or "").split(","):
        if not part.strip():
            continue
        try:
            out.append(uuid.UUID(part.strip()))
        except ValueError:
            raise HTTPException(422, "candidate_ids must be candidate ids separated by commas") from None
    if len(out) > MAX_CANDIDATES:
        raise HTTPException(422, f"At most {MAX_CANDIDATES} candidates at a time")
    return list(dict.fromkeys(out))


async def candidates_in_scope(db: AsyncSession, principal: Principal, ids: list[uuid.UUID]) -> list[Candidate]:
    """The candidates with these ids, in the order asked for. One id outside the caller's scope refuses the whole
    request with the answer an unknown id gets, so it cannot be used to find out which ids exist elsewhere."""
    if not ids:
        return []
    rows = {c.id: c for c in (await db.execute(select(Candidate).where(Candidate.id.in_(ids), candidate_scope(principal)))).scalars()}
    if len(rows) != len(set(ids)):
        raise HTTPException(404, "Candidate not found")
    return [rows[i] for i in dict.fromkeys(ids)]


# ---------------------------------------------------------------- what the portal knows about a candidate
def _titles(raw, cap: int = PER_CANDIDATE) -> list[str]:
    """Cleaned, distinct titles from a stored list. A saved title is kept as it was typed when cleaning would drop it."""
    out: dict[str, str] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        title = " ".join(item.split())[:80]
        if title and title_key(title) and title_key(title) not in out:
            out[title_key(title)] = title
    return list(out.values())[:cap]


async def known_titles(db: AsyncSession, candidates: list[Candidate]) -> dict[uuid.UUID, dict]:
    """Per candidate: the saved titles, the titles found in the current resume, the skills, and what identifies this
    state of the candidate (resume version and profile), for the model memory."""
    ids = [c.id for c in candidates]
    names = {c.id: c.name for c in candidates}
    out: dict[uuid.UUID, dict] = {i: {"saved": [], "resume": [], "skills": [], "has_resume": False, "state": ""} for i in ids}
    if not ids:
        return out
    for profile in (await db.execute(select(CandidateProfile).where(CandidateProfile.candidate_id.in_(ids)))).scalars():
        out[profile.candidate_id]["saved"] = _titles(profile.desired_titles)
        out[profile.candidate_id]["skills"] = [str(s)[:60] for s in (profile.skills or []) if isinstance(s, str)][:20]
    rows = (await db.execute(select(Resume.candidate_id, Resume.id, Resume.version, Resume.sha256, Resume.parsed_profile, Resume.text)
                             .where(Resume.candidate_id.in_(ids), Resume.is_current))).all()
    for candidate_id, resume_id, version, sha, parsed, resume_text in rows:
        entry = out[candidate_id]
        parsed = parsed if isinstance(parsed, dict) else {}
        # Read from the text, line by line (job_titles.resume_titles). The titles stored at upload
        # (parsed_profile["job_titles"], resume_parser.heuristic_profile) are not used here: that reading runs across
        # line breaks, so a stored "title" can begin with the person's surname or the end of the sentence before it.
        saved = {title_key(t) for t in entry["saved"]}
        found = resume_titles(resume_text or "", names.get(candidate_id, ""), PER_CANDIDATE)
        entry["resume"] = [t for t in found if title_key(t) not in saved][:PER_CANDIDATE]
        entry["has_resume"] = True
        if not entry["skills"]:
            entry["skills"] = [str(s)[:60] for s in (parsed.get("skills") or []) if isinstance(s, str)][:20]
        entry["state"] = f"{resume_id}:{version}:{sha}"
    for candidate_id, entry in out.items():
        shaped = json.dumps([str(candidate_id), entry["state"], entry["saved"], entry["resume"], entry["skills"]], sort_keys=True)
        entry["key"] = MEMORY_PREFIX + hashlib.sha1(shaped.encode()).hexdigest()[:32]
    return out


# ---------------------------------------------------------------- the model's related titles, remembered
def ai_factory_of(app, settings: Settings):
    """A callable that makes the model client, or None when no model is configured (or title variants are off)."""
    override = getattr(app.state, "title_ai_factory", None)
    if override is not None:
        return override
    if not settings.title_expansion or not (settings.openai_api_key or settings.anthropic_api_key or settings.techsara_configured):
        return None
    from ...ai_orchestrator import AIOrchestrator        # imported here: the API starts without loading the model clients
    return lambda: AIOrchestrator.from_settings(settings)


async def _remembered(db: AsyncSession, keys: list[str]) -> dict[str, list[str]]:
    if not keys:
        return {}
    rows = (await db.execute(text("SELECT key, value FROM index_meta WHERE key = ANY(:keys)"), {"keys": keys})).all()
    out: dict[str, list[str]] = {}
    for key, value in rows:
        try:
            data = json.loads(value)
        except ValueError:
            continue
        if isinstance(data, dict) and time.time() - float(data.get("t") or 0) < MODEL_MEMORY_SECONDS and isinstance(data.get("v"), list):
            out[key] = [str(v) for v in data["v"]][:PER_CANDIDATE]
    return out


async def _ask(app, factory, key: str, titles: list[str], skills: list[str]) -> list[str] | None:
    """One model call for one candidate. The answer (also an empty one) is remembered; no answer is not."""
    ai = factory()
    try:
        raw, provider = await asyncio.wait_for(ai.expand_titles(titles, Profile(skills=skills)), MODEL_DEADLINE_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("title suggestions: the model call failed (%s)", type(exc).__name__)
        return None
    finally:
        close = getattr(ai, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass
    if not provider:
        return None                                       # no model answered
    variants = clean_variants(raw, titles, PER_CANDIDATE)
    try:
        async with app.state.sessions() as db:
            await db.execute(text("INSERT INTO index_meta (key, value) VALUES (:key, :value) "
                                  "ON CONFLICT (key) DO UPDATE SET value = excluded.value"),
                             {"key": key, "value": json.dumps({"t": time.time(), "v": variants, "by": provider})})
            await db.commit()
    except Exception as exc:                              # not remembered: the next request asks again
        log.warning("title suggestions: the answer was not stored (%s)", type(exc).__name__)
    return variants


async def related_titles(app, db: AsyncSession, settings: Settings, known: dict[uuid.UUID, dict]) -> tuple[dict[uuid.UUID, list[str]], str]:
    """Model titles per candidate and how the model stands: "answered" (every candidate asked about has an answer,
    now or from memory), "pending" (calls are still running: ask again in a few seconds), "none" (no model is
    configured, or it did not answer). Waits at most MODEL_WAIT_SECONDS."""
    factory = ai_factory_of(app, settings)
    askable = {cid: e for cid, e in known.items() if e["saved"] or e["resume"] or e["skills"]}
    if factory is None or not askable:
        return {}, "none"
    remembered = await _remembered(db, [e["key"] for e in askable.values()])
    out = {cid: remembered[e["key"]] for cid, e in askable.items() if e["key"] in remembered}
    running: dict = getattr(app.state, "title_tasks", None)
    if running is None:
        running = app.state.title_tasks = {}
    waiting: dict[uuid.UUID, asyncio.Task] = {}
    for cid, entry in askable.items():
        if cid in out:
            continue
        task = running.get(entry["key"])
        if task is None:
            if len(waiting) >= MODEL_CANDIDATES:
                continue
            task = running[entry["key"]] = asyncio.create_task(
                _ask(app, factory, entry["key"], [*entry["saved"], *entry["resume"]][:8], entry["skills"]))
            task.add_done_callback(lambda _done, key=entry["key"]: running.pop(key, None))
        waiting[cid] = task
    if not waiting:
        return out, "answered" if out else "none"
    await asyncio.wait(set(waiting.values()), timeout=MODEL_WAIT_SECONDS)
    pending = failed = False
    for cid, task in waiting.items():
        if not task.done():
            pending = True
        elif task.cancelled() or task.exception() is not None or task.result() is None:
            failed = True
        else:
            out[cid] = task.result()
    if pending:
        return out, "pending"
    return out, "none" if failed and not out else "answered"


# ---------------------------------------------------------------- while typing
def typed_suggestions(vocabulary: TitleVocabulary, typed: str, own: list[str], limit: int = SUGGESTION_LIMIT) -> list[tuple[str, str]]:
    """(title, source) pairs for the typed text: the selected candidates' own titles that match first ("candidate"),
    then titles from postings ("jobs")."""
    query = _query_words(typed)
    if not query:
        return []
    mine = [t for t in dict.fromkeys(own) if matches(title_key(t).split(), query)][:limit]
    rest = vocabulary.suggest(typed, limit - len(mine), exclude=mine)
    return [(t, "candidate") for t in mine] + [(t, "jobs") for t in rest]
