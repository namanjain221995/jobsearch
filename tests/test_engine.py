import asyncio

from app.deduplicator import job_keys
from app.schemas import Candidate
from tests.conftest import make_request, run_search


def run(coro):
    return asyncio.run(coro)


def test_scenario_a_e_f_us_only_no_citizen_only_no_duplicates(make_engine):
    state = run(run_search(make_engine(), make_request(result_count=50)))
    assert state.status == "completed"
    titles = [(j.title, j.location) for j in state.results]
    assert state.results and len(state.results) <= 50
    assert not any("Toronto" in loc or "London" in loc or "Berlin" in loc for _, loc in titles)      # Scenario E
    assert not any(loc == "Remote" for _, loc in titles)                    # unconfirmed worldwide remote excluded
    assert all(j.citizenship_requirement != "Required" for j in state.results)                        # Scenario F
    assert any(j.title == "Senior AI Engineer" for j in state.results)     # "US person" is not citizen-only
    assert not any(j.title == "Account Executive" for j in state.results)
    keys = [job_keys(j)[0] or job_keys(j)[1] for j in state.results]
    assert len(keys) == len(set(keys))
    assert all(j.url.startswith("https://") and j.verified_at and j.source for j in state.results)
    assert state.stats["rejected_non_us"] >= 4 and state.stats["rejected_eligibility"] >= 1
    assert state.stats["final_matches"] == len(state.results)
    assert "Found" in state.message and "of 50 requested" in state.message      # shortfall is explained


def test_citizen_only_job_kept_when_filter_off(make_engine):
    state = run(run_search(make_engine(), make_request(exclude_citizenship_required=False)))
    flagged = [j for j in state.results if j.citizenship_requirement == "Required"]
    assert flagged and flagged[0].authorization_status == "Potential mismatch"


def test_scenario_b_limit_of_ten(make_engine):
    state = run(run_search(make_engine(), make_request(result_count=10)))
    assert len(state.results) == 10 and len({j.id for j in state.results}) == 10


def test_scenario_c_zero_means_all_within_cap(make_engine):
    state = run(run_search(make_engine(), make_request(result_count=0)))
    assert state.stats["requested"] == 0 and 10 < len(state.results) <= 500


def test_scenario_g_failed_source_is_reported_not_fabricated(make_engine):
    state = run(run_search(make_engine(), make_request()))
    assert state.status == "completed" and state.results
    assert state.sources["Ashby"]["boards_failed"] == 1 and state.stats["boards_failed"] == 1
    assert any("Ashby" in w and "incomplete" in w for w in state.warnings)
    assert all(j.source in ("Greenhouse", "Lever") for j in state.results)


def test_all_sources_down_returns_zero_results(make_engine):
    state = run(run_search(make_engine(seeds=[("ashby", "gamma")]), make_request()))
    assert state.status == "completed" and state.results == [] and "Found 0 of 50" in state.message


def test_scenario_d_ai_leads_for_same_vacancy_merge_into_one(make_engine):
    leads = [
        Candidate(url="https://boards.greenhouse.io/acme/jobs/1001?gh_src=openai", title="AI Engineer", company="Acme", provider="OpenAI", cited=True),
        Candidate(url="https://job-boards.greenhouse.io/acme/jobs/1001", title="AI Engineer", company="Acme AI", provider="Claude", cited=True),
    ]
    state = run(run_search(make_engine(leads, ai_available=True), make_request()))
    hits = [j for j in state.results if j.ats_id == "1001"]
    assert len(hits) == 1
    assert set(hits[0].sources) == {"Greenhouse", "OpenAI web search", "Claude web search"}


def test_scenario_h_closed_or_broken_leads_are_rejected(make_engine):
    leads = [
        Candidate(url="https://job-boards.greenhouse.io/acme/jobs/999999", title="AI Engineer", company="Acme", provider="OpenAI", cited=True),
        Candidate(url="https://careers.example.com/jobs/gone", title="AI Engineer", company="Example", provider="Claude", cited=True),
        Candidate(url="https://careers.example.com/jobs/closed", title="AI Engineer", company="Example", provider="Claude", cited=True),
        Candidate(url="https://www.linkedin.com/jobs/view/123", title="AI Engineer", company="Example", provider="OpenAI", cited=True),
        Candidate(url="https://careers.example.com/jobs/ml-1", title="Machine Learning Engineer", company="Example", provider="Claude", cited=True),
    ]
    state = run(run_search(make_engine(leads, ai_available=True), make_request()))
    urls = {j.url for j in state.results}
    assert state.stats["rejected_unverified"] == 4
    assert not any("999999" in u or "gone" in u or "closed" in u or "linkedin" in u for u in urls)
    verified_page = [j for j in state.results if j.url == "https://careers.example.com/jobs/ml-1"]
    assert len(verified_page) == 1 and verified_page[0].company == "Example Careers Inc"
    assert "JobPosting" in verified_page[0].verification and verified_page[0].apply_url == ""


def test_no_ai_configured_is_stated(make_engine):
    state = run(run_search(make_engine(), make_request()))
    assert any("No AI provider configured" in w for w in state.warnings)
    assert all(j.score_source == "heuristic" for j in state.results)


def test_ai_scored_jobs_rank_ahead_of_keyword_only_jobs(make_engine):
    engine = make_engine(ai_available=True)
    engine.settings.max_ai_ranked_jobs = 3
    ai = engine.ai_factory()

    async def rank_batch(profile, req, jobs):
        return {j.id: {"skills": 10, "title": 10, "experience": 5, "arrangement": 5, "additional": 0,
                       "matching_skills": ["Python"], "missing_skills": [], "reason": "checked", "experience_comparison": ""} for j in jobs}

    ai.rank_batch = rank_batch
    engine.ai_factory = lambda: ai
    state = run(run_search(engine, make_request(result_count=10)))
    kinds = [j.score_source for j in state.results]
    assert kinds[:3] == ["ai", "ai", "ai"] and "ai" not in kinds[3:]
    assert state.results[0].score == 30 and state.results[3].score > 30      # lower AI score still outranks keyword score


# ---------------------------------------------------------------- warnings a person can act on (2026-11-28)
def test_boards_still_loading_are_a_warning_only_when_jobs_are_missing(make_engine):
    """Production, 2026-11-27: a search that delivered the 100 jobs asked for warned that 3,116 source requests were
    still loading. The note is kept for the search that came up short; the other one found what was asked for."""
    from app.search_engine import SearchState

    engine = make_engine()
    note = "12 source request(s) (Greenhouse) were still loading at the time limit."

    def searched(result_count):
        seen = {}
        original = SearchState.publish

        def publish(self, final, partial=False):
            seen["state"] = self
            return original(self, final, partial=partial)

        SearchState.publish = publish
        try:
            report = engine._source_report

            async def with_pending(state, failed, pending):
                await report(state, failed, pending)
                state.pending_note = note
            engine._source_report = with_pending
            return run(run_search(engine, make_request(result_count=result_count)))
        finally:
            SearchState.publish = original

    full = searched(5)
    assert len(full.results) == 5 and not any("still loading" in w for w in full.warnings)
    short = searched(400)
    assert len(short.results) < 400 and any("still loading" in w for w in short.warnings)


def test_a_few_failed_boards_out_of_many_are_not_a_warning_and_a_refusing_source_is_said_once(make_engine):
    from app.providers.registry import SPECS
    from app.search_engine import SearchState, Unit

    engine = make_engine()
    state = SearchState("s1", 25)

    async def stats():
        return {"by_source": {"greenhouse": {"units": 2000, "postings": 9000}, "ashby": {"units": 4, "postings": 30}},
                "fresh_units": 2004, "fresh_postings": 9030}

    engine.index.index_stats = stats
    failed = [(Unit("greenhouse", f"g{i}", ""), "HTTP 503 from boards-api.greenhouse.io") for i in range(3)]
    failed += [(Unit("ashby", "acme", ""), "HTTP 500 from api.ashbyhq.com")]
    failed += [(Unit("workday", f"t{i}|wd1|x", ""), "t.wd1.myworkdayjobs.com answered with a bot check (HTTP 429); not read") for i in range(40)]
    run(engine._source_report(state, failed, []))
    notes = state.source_notes
    assert not any(n.startswith(SPECS["greenhouse"].label) for n in notes)                    # 3 of 2,003
    assert any(n.startswith("Ashby: 1 of 5 sources could not be read") for n in notes)        # 1 of 5
    workday = [n for n in notes if n.startswith("Workday")]
    assert len(workday) == 1 and "bot check" in workday[0] and "never works around" in workday[0]
    assert state.sources["Workday"]["boards_failed"] == 0 and state.sources["Workday"]["refuses"] is True
    assert state.warnings == []                                                                # said at the end of the search, once
