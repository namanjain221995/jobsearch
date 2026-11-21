"""A faster ranking stage that ranks exactly as before: the memory of model judgements (rank_cache.py), the reranker
asked side by side under a time limit, the early reranking pass, and explanations only for jobs that can be
delivered. No network: every transport is a mock and every posting is synthetic."""
import asyncio
import json
import re
import time

import httpx

from app.ai_orchestrator import AIOrchestrator
from app.config import Settings
from app.portal.search_runner import discovery_of
from app.providers.techsara_provider import TechsaraProvider
from app.rank_cache import RankCache, item_key, scope_key
from app.schemas import Job, Profile
from tests.conftest import RESUME, make_request
from tests.test_wider_search import SEARX_ANSWERS, WideAI, _reset, ai_scores, run, search, wide_engine, wide_settings  # noqa: F401


class MetaIndex:
    """The key/value store of the job index, as far as the memory uses it."""

    def __init__(self):
        self.rows: dict[str, str] = {}
        self.fail = False

    async def meta_set(self, key, value):
        if self.fail:
            raise RuntimeError("index down")
        self.rows[key] = value

    async def meta_prefix(self, prefix):
        if self.fail:
            raise RuntimeError("index down")
        return {k[len(prefix):]: v for k, v in self.rows.items() if k.startswith(prefix)}


def posting(i: int, text: str = "") -> Job:
    return Job(id=str(i), title="AI Engineer", company="Acme", url=f"https://x.example/{i}", source="Greenhouse",
               description=text or f"Posting number {i} asks for Python.")


# ---------------------------------------------------------------- the memory itself
def test_memory_is_keyed_by_candidate_side_and_posting_text_survives_a_restart_and_expires():
    now = [1000.0]
    index = MetaIndex()
    cache = RankCache(index, ttl_seconds=100, clock=lambda: now[0])
    side = scope_key("rerank", "model-1", "query of candidate A")
    one, two = item_key("posting text 1"), item_key("posting text 2")
    assert run(cache.get_many("rerank", side, {"job1": one})) == {}
    run(cache.put_many("rerank", side, {one: 0.75}))
    assert run(cache.get_many("rerank", side, {"job1": one, "job2": two})) == {"job1": 0.75}
    # Another candidate, another model, another kind of judgement, a changed posting: never answered from memory.
    assert run(cache.get_many("rerank", scope_key("rerank", "model-1", "query of candidate B"), {"job1": one})) == {}
    assert run(cache.get_many("rerank", scope_key("rerank", "model-2", "query of candidate A"), {"job1": one})) == {}
    assert run(cache.get_many("judge", side, {"job1": one})) == {}
    assert run(cache.get_many("rerank", side, {"job1": item_key("posting text 1, edited")})) == {}
    # Only hashes and the model's output are written; a new process finds them.
    assert list(index.rows) == [f"rankcache:rerank:{side}"]
    assert "posting text" not in index.rows[f"rankcache:rerank:{side}"] and "candidate A" not in index.rows[f"rankcache:rerank:{side}"]
    restarted = RankCache(index, ttl_seconds=100, clock=lambda: now[0])
    assert run(restarted.get_many("rerank", side, {"x": one})) == {"x": 0.75}
    now[0] += 101                                                   # a model behind an unchanged name may have been updated
    assert run(restarted.get_many("rerank", side, {"x": one})) == {}
    assert run(RankCache(index, ttl_seconds=100, clock=lambda: now[0]).get_many("rerank", side, {"x": one})) == {}


def test_memory_is_bounded_ignores_a_failing_index_and_keeps_profiles_in_the_process_only():
    now = [1000.0]
    index = MetaIndex()
    cache = RankCache(index, ttl_seconds=1000, max_items=3, clock=lambda: now[0])
    for i in range(5):
        now[0] += 1
        run(cache.put_many("judge", "side", {f"k{i}": {"n": i}}))
    assert run(cache.get_many("judge", "side", {f"k{i}": f"k{i}" for i in range(5)})) == {"k2": {"n": 2}, "k3": {"n": 3}, "k4": {"n": 4}}
    cache.put_local("profile-key", {"skills": ["Python"]})
    assert cache.get_local("profile-key") == {"skills": ["Python"]} and "Python" not in json.dumps(index.rows)       # never written to the index
    assert RankCache(index, ttl_seconds=1000, clock=lambda: now[0]).get_local("profile-key") is None                 # not there after a restart
    index.fail = True                                               # the index is down: nothing raises, the process still remembers
    run(cache.put_many("judge", "side", {"k9": {"n": 9}}))
    assert run(cache.get_many("judge", "side", {"a": "k9"})) == {"a": {"n": 9}}
    assert run(RankCache(index, ttl_seconds=1000).get_many("judge", "side", {"a": "k9"})) == {}
    off = RankCache(MetaIndex(), ttl_seconds=0)                     # RANK_CACHE_DAYS=0: nothing is kept anywhere
    run(off.put_many("judge", "side", {"k": 1}))
    off.put_local("p", 1)
    assert not off.enabled and run(off.get_many("judge", "side", {"a": "k"})) == {} and off.get_local("p") is None and not off.index.rows


# ---------------------------------------------------------------- the reranker
def reranker(delay: float = 0.0):
    seen: list[list[str]] = []
    live = {"now": 0, "most": 0}

    async def respond(request: httpx.Request) -> httpx.Response:
        documents = json.loads(request.content)["documents"]
        seen.append(documents)
        live["now"] += 1
        live["most"] = max(live["most"], live["now"])
        try:
            if delay:
                await asyncio.sleep(delay)
            return httpx.Response(200, json={"results": [{"index": i, "relevance_score": (len(d) % 90) / 100} for i, d in enumerate(documents)]})
        finally:
            live["now"] -= 1

    provider = TechsaraProvider("tsk_test_key", "https://model.test/v1", "techsara-35b", "techsara-rerank", retries=0, concurrency=16,
                                client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    return provider, seen, live


def test_reranker_gets_one_batch_at_a_time_and_never_the_same_posting_twice():
    s = Settings(_env_file=None, techsara_rerank_batch=2, techsara_concurrency=3)
    provider, seen, live = reranker(delay=0.03)
    cache = RankCache(None, 3600)
    jobs = [posting(i) for i in range(14)]
    req, profile = make_request(), Profile(skills=["Python"])

    async def go():
        first = AIOrchestrator(s, None, None, provider)
        first.remember(cache, RESUME, req)
        scores = await first.rerank(profile, req, jobs)
        assert set(scores) == {j.id for j in jobs} and len(seen) == 7
        assert live["most"] == 1                                    # the service reads serially (measured): requests side by side gain nothing
        assert len(seen[0]) == 2 and seen[0] == [first._rerank_document(j) for j in jobs[:2]]
        # The ranking stage of the same search asks again after the early pass: nothing is sent, and that is not "reuse".
        assert await first.rerank(profile, req, jobs) == scores and len(seen) == 7 and first.reused["rerank"] == 0
        # The next search for the same candidate: everything from memory.
        second = AIOrchestrator(s, None, None, provider)
        second.remember(cache, RESUME, req)
        assert await second.rerank(profile, req, jobs) == scores and len(seen) == 7 and second.reused["rerank"] == 14
        # One posting changed its text and one is new: exactly those two are sent.
        changed = [*jobs[:13], posting(13, "Posting number 13 now asks for Rust."), posting(99)]
        third = AIOrchestrator(s, None, None, provider)
        third.remember(cache, RESUME, req)
        again = await third.rerank(profile, req, changed)
        assert len(seen) == 8 and len(seen[-1]) == 2 and set(again) == {j.id for j in changed} and third.reused["rerank"] == 13
        # Another profile is another question to the reranker: all of it is sent.
        other = AIOrchestrator(s, None, None, provider)
        other.remember(cache, RESUME, req)
        await other.rerank(Profile(skills=["Rust"]), req, jobs)
        assert len(seen) == 15 and other.reused["rerank"] == 0
        # Without the memory (RANK_CACHE_DAYS=0) every search sends everything.
        plain = AIOrchestrator(s, None, None, provider)
        plain.remember(RankCache(None, 0), RESUME, req)
        assert plain.cache is None and await plain.rerank(profile, req, jobs) == scores and len(seen) == 22

    run(go())


def test_reranker_time_limit_keeps_what_arrived_and_remembers_it():
    s = Settings(_env_file=None, techsara_rerank_batch=1, techsara_concurrency=1, techsara_rerank_seconds=1.0)
    provider, seen, _ = reranker(delay=0.45)
    cache = RankCache(None, 3600)
    jobs = [posting(i) for i in range(8)]
    req = make_request()

    async def go():
        ai = AIOrchestrator(s, None, None, provider)
        ai.remember(cache, RESUME, req)
        started = time.perf_counter()
        scores = await ai.rerank(Profile(), req, jobs)
        assert time.perf_counter() - started < 2.0 and 1 <= len(scores) < len(jobs)
        assert "time limit" in ai.status["TechSara"]["last_error"]
        later = AIOrchestrator(s, None, None, provider)             # the next call only sends what is still missing
        later.remember(cache, RESUME, req)
        sent_before = sum(len(d) for d in seen)
        await later.rerank(Profile(), req, jobs[:len(scores)])
        assert sum(len(d) for d in seen) == sent_before and later.reused["rerank"] == len(scores)

    run(go())


def test_early_pass_reads_best_first_and_the_ranking_stage_waits_only_a_bounded_time():
    s = Settings(_env_file=None, techsara_rerank_batch=2, techsara_rerank_seconds=30.0)
    provider, seen, live = reranker(delay=0.2)
    jobs = [posting(i) for i in range(40)]                          # the engine hands them over best keyword match first
    req, profile = make_request(), Profile(skills=["Python"])

    async def go():
        ai = AIOrchestrator(s, None, None, provider)
        ai.remember(RankCache(None, 3600), RESUME, req)
        ai.rerank_start(profile, req, jobs)                         # returns at once; the worker is on its way
        assert seen == []
        await asyncio.sleep(0.5)                                    # sources are read, leads are verified ...
        final = [posting(100), posting(101), *jobs]                 # two postings the lead checks added come first in the final order
        started = time.perf_counter()
        scores = await ai.rerank_collect(profile, req, final, seconds=1.0)
        waited = time.perf_counter() - started
        assert waited < 2.0 and live["most"] == 1
        assert {"100", "101"} <= set(scores) and 6 <= len(scores) < len(final)            # the new ones were put in front of the queue
        scored = sorted(int(i) for i in scores if int(i) < 100)
        assert scored == list(range(len(scored)))                   # of the others exactly the best ones, in order: no gaps
        sent = sum(len(batch) for batch in seen)
        await asyncio.sleep(0.6)
        assert sum(len(batch) for batch in seen) - sent <= 2        # the worker stops after the batch that was on its way
        # Asking for everything later (rerank) finishes the rest and sends nothing twice.
        everything = await ai.rerank(profile, req, final)
        assert set(everything) == {j.id for j in final}
        texts = [text for batch in seen for text in batch]
        assert len(texts) == len(set(texts)) == len(final)
        await ai.aclose()

    run(go())


# ---------------------------------------------------------------- judgements, audits, explanations, the profile
class Judge:
    name, model = "OpenAI", "judge-model"

    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []

    async def json_call(self, system, user, schema, name):
        ids = re.findall(r'<job id="([^"]+)">', user)
        self.calls.append((name, ids))
        if name == "job_rankings":
            return {"rankings": [{"id": i, "skills": 30, "title": 20, "experience": 15, "arrangement": 10, "additional": 5,
                                  "matching_skills": ["Python"], "missing_skills": [], "reason": f"judged {i}", "experience_comparison": ""} for i in ids]}
        if name == "shortlist_audit":
            return {"audits": [{"id": i, "relevant": True, "citizenship_requirement": "", "sponsorship": "", "clearance_required": False, "evidence": ""}
                               for i in ids]}
        return {"skills": ["Python"], "tools": [], "job_titles": ["AI Engineer"], "education": [], "industries": [], "summary": "Builds models.",
                "related_titles": []}


def test_judgements_and_audits_of_the_same_candidate_and_posting_are_used_again():
    s = Settings(_env_file=None)
    index = MetaIndex()
    cache = RankCache(index, 3600)
    judge = Judge()
    jobs = [posting(i) for i in range(5)]
    req, profile = make_request(), Profile(skills=["Python"], source="OpenAI")

    def orchestrator(resume=RESUME, memory=cache):
        ai = AIOrchestrator(s, judge, None, None)
        if memory is not None:
            ai.remember(memory, resume, req)
        return ai

    async def go():
        first = orchestrator()
        answered = await first.rank_batch(profile, req, jobs[:3])
        assert set(answered) == {"0", "1", "2"} and judge.calls == [("job_rankings", ["0", "1", "2"])]
        assert await first.recall_rankings(profile, req, jobs) == {i: {**answered[i], "id": i} for i in ("0", "1", "2")}
        # The next search (and a restarted worker, through the index) knows them; no call is made for a recall.
        second = orchestrator(memory=RankCache(index, 3600))
        known = await second.recall_rankings(profile, req, jobs)
        assert set(known) == {"0", "1", "2"} and known["1"]["reason"] == "judged 1" and second.reused["judge"] == 3 and len(judge.calls) == 1
        # Not for another resume, a changed posting, or a profile another provider extracted.
        assert await orchestrator(resume=RESUME + " Also Rust.").recall_rankings(profile, req, jobs) == {}
        assert await orchestrator().recall_rankings(profile, req, [posting(0, "Posting number 0 asks for Go.")]) == {}
        assert await orchestrator().recall_rankings(Profile(skills=["Python"], source="Claude"), req, jobs) == {}
        # Without the memory nothing is recalled or kept.
        bare = orchestrator(memory=None)
        assert await bare.recall_rankings(profile, req, jobs) == {} and bare.cache is None
        # The audit: one call, then memory; a new posting is the only one sent.
        audits = await orchestrator().audit(req, jobs)
        assert set(audits) == {j.id for j in jobs} and judge.calls[-1] == ("shortlist_audit", ["0", "1", "2", "3", "4"])
        calls = len(judge.calls)
        again = orchestrator()
        assert await again.audit(req, jobs) == audits and len(judge.calls) == calls and again.reused["audit"] == 5
        more = orchestrator()
        assert set(await more.audit(req, [*jobs, posting(7)])) == {"0", "1", "2", "3", "4", "7"} and judge.calls[-1] == ("shortlist_audit", ["7"])
        # What is stored: model output under hashes. No resume text, no posting text.
        stored = json.dumps(index.rows)
        assert "judged 1" in stored and "Posting number" not in stored and RESUME[:40] not in stored

    run(go())


def test_profile_is_extracted_once_per_resume_in_a_process_and_is_never_written_to_the_index():
    s = Settings(_env_file=None)
    index = MetaIndex()
    cache = RankCache(index, 3600)
    judge = Judge()
    req = make_request()

    async def go():
        first = AIOrchestrator(s, judge, None, None)
        first.remember(cache, RESUME, req)
        profile = await first.extract_profile(RESUME, req, Profile())
        assert profile.source == "OpenAI" and len(judge.calls) == 1
        second = AIOrchestrator(s, judge, None, None)
        second.remember(cache, RESUME, req)
        assert await second.extract_profile(RESUME, req, Profile()) == profile and len(judge.calls) == 1 and second.reused["profile"] == 1
        other = AIOrchestrator(s, judge, None, None)
        other.remember(cache, RESUME + " Also Rust.", req)
        await other.extract_profile(RESUME + " Also Rust.", req, Profile())
        assert len(judge.calls) == 2 and not index.rows

    run(go())


# ---------------------------------------------------------------- the engine
class CountingAI(WideAI):
    def __init__(self, *args, slow_judges: float = 0.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.explained: list[str] = []
        self.slow_judges = slow_judges

    async def explain_batch(self, profile, req, jobs):
        self.explained.extend(j.id for j in jobs)
        return await super().explain_batch(profile, req, jobs)

    async def rank_batch(self, profile, req, jobs):
        if self.slow_judges:
            await asyncio.sleep(self.slow_judges)
        return await super().rank_batch(profile, req, jobs)


def raw_scores(jobs):
    return {j.id: 0.5 + (i % 40) / 100 for i, j in enumerate(jobs)}


def test_explanations_are_written_only_for_jobs_that_can_be_delivered():
    # 25 jobs asked: all 25 delivered jobs are judged by OpenAI / Claude, so the company model explains nothing
    # (it explained the best 400 before, for up to three minutes, and none of that text was delivered).
    SEARX_ANSWERS["*"] = ["https://boards.greenhouse.io/bigco"]
    ai = CountingAI(available=True, raw=raw_scores, rank=ai_scores(40))
    state = run(search(wide_engine(ai), result_count=25))
    assert len(state.results) == 25 and all(j.score_source == "ai" for j in state.results)
    assert ai.explained == [] and state.stats["company_model_explained"] == 0 and state.stats["company_model_scored"] > 100
    assert state.stats["rank_seconds"] is not None and state.stats["rerank_sent"] == state.stats["unique_candidates"]
    assert {"rank_seconds", "rerank_sent"} <= set(discovery_of(state))
    # 200 asked, 40 judged: the 160 delivered jobs of the company model are explained, plus a margin, not the best 400.
    SEARX_ANSWERS["*"] = ["https://boards.greenhouse.io/bigco"]
    ai = CountingAI(available=True, raw=raw_scores, rank=ai_scores(40))
    state = run(search(wide_engine(ai, wide_settings(max_ai_ranked_jobs=40, max_ai_ranked_jobs_max=40)), result_count=200))
    company = [j for j in state.results if j.score_source == "company_model"]
    assert len(state.results) == 200 and len(company) == 160
    assert all(j.reason.startswith("Uses Python daily.") for j in company) and {j.id for j in company} <= set(ai.explained)
    assert 160 <= len(ai.explained) <= 200 and len(set(ai.explained)) == len(ai.explained)


def test_judging_stops_at_its_time_limit_and_the_search_still_completes():
    SEARX_ANSWERS["*"] = ["https://boards.greenhouse.io/bigco"]
    ai = CountingAI(available=True, raw=raw_scores, rank=ai_scores(40), slow_judges=30.0)
    started = time.perf_counter()
    state = run(search(wide_engine(ai, wide_settings(ai_rank_seconds=1.0, techsara_explain_max_jobs=0)), result_count=25))
    assert time.perf_counter() - started < 20
    assert state.status == "completed" and len(state.results) == 25 and not any(j.score_source == "ai" for j in state.results)
    assert any("AI ranking did not finish within its time limit" in w for w in state.warnings)


def test_early_reranking_hands_its_scores_to_the_ranking_stage_and_the_next_search_asks_nothing():
    SEARX_ANSWERS["*"] = ["https://boards.greenhouse.io/bigco"]
    provider, seen, _ = reranker()
    settings = wide_settings(searxng_url="", techsara_rerank_batch=64, techsara_explain_max_jobs=0)
    engine = wide_engine(AIOrchestrator(settings, None, None, provider), settings, seeds=[("greenhouse", "bigco"), ("greenhouse", "acme")],
                         sources=("greenhouse",))
    engine.ai_factory = lambda: AIOrchestrator(settings, None, None, provider)       # one orchestrator per search, as in production

    async def go():
        first = await search(engine, close=False, result_count=50)
        sent = [d for request in seen for d in request]
        assert first.results and all(j.score_source == "company_model" for j in first.results)
        assert len(sent) == len(set(sent)) >= first.stats["unique_candidates"]       # the early pass and the ranking stage: each posting once
        assert first.stats["rank_reused"] == {}
        again = await search(engine, close=True, result_count=50)
        assert sum(len(r) for r in seen) == len(sent)                                # the same candidate again: the reranker is not asked
        assert again.stats["rank_reused"]["rerank"] == again.stats["rerank_sent"] > 0
        assert [(j.id, j.score) for j in again.results] == [(j.id, j.score) for j in first.results]

    run(go())
