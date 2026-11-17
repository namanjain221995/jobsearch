from app.deduplicator import deduplicate
from app.job_ranker import (apply_ai_ranking, best_title_relevance, evaluate_eligibility, heuristic_score,
                            static_related_titles, title_relevance)
from app.schemas import Job, Profile
from tests.conftest import make_request


def job(**kw):
    base = dict(title="AI Engineer", company="Acme AI", location="San Francisco, CA", url="https://x.example/1",
                source="Greenhouse", sources=["Greenhouse"], description="Python and PyTorch. 3+ years of experience.")
    base.update(kw)
    return Job(**base)


def test_scenario_d_same_vacancy_from_three_sources_is_one_result():
    greenhouse = job(url="https://job-boards.greenhouse.io/acme/jobs/1001", apply_url="https://job-boards.greenhouse.io/acme/jobs/1001",
                     ats="greenhouse", ats_id="1001", board="acme")
    via_openai = job(url="https://boards.greenhouse.io/acme/jobs/1001?gh_src=abc&utm_source=google", source="OpenAI web search",
                     sources=["OpenAI web search"], description="")
    via_claude = job(url="https://acme.ai/careers/ai-engineer?gh_jid=1001", source="Claude web search",
                     sources=["Claude web search"], company="Acme AI, Inc.", description="")
    unique, removed = deduplicate([via_openai, greenhouse, via_claude])
    assert len(unique) == 1 and removed == 2
    winner = unique[0]
    assert winner.source == "Greenhouse" and winner.apply_url.endswith("/acme/jobs/1001")
    assert set(winner.sources) == {"Greenhouse", "OpenAI web search", "Claude web search"}


def test_dedupe_by_canonical_url_and_company_title_location():
    a = job(url="https://careers.acme.ai/jobs/77?utm_campaign=x", source="OpenAI web search", sources=["OpenAI web search"])
    b = job(url="https://careers.acme.ai/jobs/77/", source="Claude web search", sources=["Claude web search"])
    c = job(url="https://other-board.example/acme-ai-engineer", title="AI  Engineer", company="ACME AI Inc", location="San Francisco, CA, United States")
    unique, removed = deduplicate([a, b, c])
    assert len(unique) == 1 and removed == 2


def test_fuzzy_duplicate_with_minor_title_difference():
    a = job(url="https://a.example/1", title="Sr. Machine Learning Engineer")
    b = job(url="https://b.example/2", title="Senior ML Engineer")
    assert len(deduplicate([a, b])[0]) == 1


def test_distinct_vacancies_at_same_employer_are_kept():
    a = job(url="https://job-boards.greenhouse.io/acme/jobs/1", ats="greenhouse", ats_id="1")
    b = job(url="https://job-boards.greenhouse.io/acme/jobs/2", ats="greenhouse", ats_id="2")      # same title, separate requisition
    c = job(url="https://x.example/3", title="AI Engineer II")
    d = job(url="https://x.example/4", title="AI Engineer III")
    e = job(url="https://x.example/5", location="Austin, TX")
    unique, removed = deduplicate([a, b, c, d, e])
    assert len(unique) == 5 and removed == 0


def test_title_relevance():
    assert title_relevance("AI Engineer", "AI Engineer") == 1.0
    assert title_relevance("Senior Software Engineer, AI Platform", "AI Engineer") >= 0.9
    assert title_relevance("Machine Learning Engineer", "AI Engineer") >= 0.6
    assert title_relevance("Software Engineer", "AI Engineer") < 0.5
    assert title_relevance("Account Executive, AI", "AI Engineer") < 0.5
    assert title_relevance("Engineering Manager, AI", "AI Engineer") < 0.5
    assert title_relevance("Backend Engineer (Python)", "Python Developer") >= 0.9


def test_related_titles_need_resume_evidence():
    assert "Machine Learning Engineer" in static_related_titles(["AI Engineer"], ["Python", "PyTorch"])
    assert static_related_titles(["AI Engineer"], ["Excel"]) == []
    assert best_title_relevance("Applied AI Engineer", ["Data Analyst"], []) < 0.5


def test_citizenship_filter_and_us_person():
    req = make_request(exclude_citizenship_required=True)
    citizen_only = job(citizenship_requirement="Required")
    assert evaluate_eligibility(citizen_only, req) == (True, "US citizenship required")
    us_person = job(citizenship_requirement="Not required")
    assert evaluate_eligibility(us_person, req)[0] is False and us_person.authorization_status == "Eligible"
    silent = job()
    assert evaluate_eligibility(silent, req)[0] is False
    assert silent.authorization_status == "Unknown" and silent.eligibility_note == "Not specified"
    kept = job(citizenship_requirement="Required")
    assert evaluate_eligibility(kept, make_request(exclude_citizenship_required=False))[0] is False
    assert kept.authorization_status == "Potential mismatch"


def test_sponsorship_filter():
    no_sponsor = job(sponsorship="Not available")
    assert evaluate_eligibility(no_sponsor, make_request(authorization="Require Visa Sponsorship", sponsorship_needed="Yes"))[0] is True
    assert evaluate_eligibility(job(sponsorship="Not available"), make_request())[0] is False


def test_heuristic_score_is_bounded_and_evidence_based():
    profile = Profile(skills=["Python", "PyTorch"], job_titles=["Machine Learning Engineer"])
    j = job(description="Python, PyTorch, Kubernetes and Rust. 3+ years of experience.", required_years=3.0)
    heuristic_score(j, profile, make_request(), [])
    assert 0 <= j.score <= 100 and sum(j.score_breakdown.values()) == j.score
    assert set(j.matching_skills) == {"Python", "PyTorch"} and "Rust" in j.missing_skills
    assert j.score_breakdown["experience"] == 20


def test_ai_ranking_is_clamped_and_cannot_invent_skills():
    profile = Profile(skills=["Python"])
    j = job()
    apply_ai_ranking(j, {"skills": 99, "title": 25, "experience": -4, "arrangement": 10, "additional": 3,
                         "matching_skills": ["Python", "Rust", "Kubernetes"], "missing_skills": ["Go"], "reason": "ok"},
                     profile, "Experienced with Python.")
    assert j.score_breakdown["skills"] == 35 and j.score_breakdown["experience"] == 0 and j.score <= 100
    assert j.matching_skills == ["Python"] and j.score_source == "ai"


def test_job_board_copy_merges_into_employer_record_but_distinct_listings_stay():
    ats = job(url="https://job-boards.greenhouse.io/acme/jobs/1", ats="greenhouse", ats_id="1", description="Original text. " * 30)
    copy = job(url="https://remoteok.com/remote-jobs/77", source="Remote OK", sources=["Remote OK"], ats="remoteok", ats_id="77",
               location="United States", description="Completely reformatted copy.", aggregator=True)
    repost = job(url="https://remoteok.com/remote-jobs/78", source="Remote OK", sources=["Remote OK"], ats="remoteok", ats_id="78",
                 location="Remote", aggregator=True)
    senior = job(url="https://jobicy.com/jobs/9", title="Senior AI Engineer", source="Jobicy", sources=["Jobicy"], ats="jobicy", ats_id="9", aggregator=True)
    for order in ([copy, ats, repost, senior], [repost, senior, copy, ats]):       # result must not depend on arrival order
        unique, removed = deduplicate([j.model_copy(deep=True) for j in order])
        assert removed == 2 and len(unique) == 2
        merged = next(j for j in unique if j.ats == "greenhouse")
        assert set(merged.sources) == {"Greenhouse", "Remote OK"} and merged.aggregator is False and merged.description.startswith("Original")
    # Without the employer's record, two listings on the same board are separate vacancies.
    assert len(deduplicate([copy.model_copy(deep=True), repost.model_copy(deep=True)])[0]) == 2
