"""Posting age (app/freshness.py): the rule with its boundaries and time zones, where each source's date comes from,
jobs without a date, the engine applying the rule before any ranking, and discovery asking for recent results.
Every date here is relative to the day the tests run."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import freshness
from app import job_verifier as jv
from app.ai_orchestrator import EXTRACT_SCHEMA, AIOrchestrator, fresh_only
from app.config import Settings
from app.freshness import FRESH, TOO_OLD, UNDATED, AgeWitness, age_class, stated_posting, within_days
from app.providers import amazon, ashby, ats_extra, ats_oracle, ats_startup, greenhouse, lever, workable, workday
from app.providers.paid import serp_item
from app.providers.searxng import SearxClient
from app.schemas import Candidate, Job, Profile, SearchRequest
from app.search_engine import SITE_REFUSED, SearchEngine
from app.search_plan import plan_for
from app.search_scope import Scope
from tests import conftest
from tests.conftest import CALLS, FakeAI, make_request, run_search
from tests.test_ai_discovery import HOSTS, READERS, Recorder, SiteLister, searches_of

NOON = datetime(2026, 11, 27, 22, 0, tzinfo=timezone.utc)        # 12:00 in Hawaii, 17:00 in New York, the 27th everywhere in the US


def run(coro):
    return asyncio.run(coro)


def days_ago(n: int, now: datetime | None = None) -> str:
    return (freshness.today(now) - timedelta(days=n)).isoformat()


def posting(posted_at="", posted_before="", **kw) -> Job:
    base = dict(title="AI Engineer", company="Acme AI", location="Austin, TX", url="https://careers.acme.example/jobs/1", source="Greenhouse",
                posted_at=posted_at, posted_before=posted_before)
    base.update(kw)
    return Job(**base)


# ---------------------------------------------------------------- 1. the rule: boundaries, time zones, impossible dates
def test_a_posting_is_too_old_only_after_the_last_day_of_the_limit():
    for limit in (1, 3, 7, 14, 30):
        assert age_class(posting(days_ago(limit, NOON)), limit, NOON) == FRESH             # exactly the limit: still delivered
        assert age_class(posting(days_ago(limit + 1, NOON)), limit, NOON) == TOO_OLD       # one day more: never delivered
        assert age_class(posting(days_ago(0, NOON)), limit, NOON) == FRESH
    assert age_class(posting(days_ago(1, NOON)), 1, NOON) == FRESH                         # "last 24 hours": posted today or yesterday
    assert age_class(posting(days_ago(400, NOON)), 0, NOON) == FRESH                       # 0 = any time: nothing is too old
    assert age_class(posting(""), 7, NOON) == UNDATED and age_class(posting(""), 0, NOON) == UNDATED
    assert freshness.age_days(posting(days_ago(5, NOON)), NOON) == 5 and freshness.age_days(posting(""), NOON) is None
    assert freshness.POSTED_WITHIN_CHOICES == (0, 1, 3, 7, 14, 30) and freshness.DEFAULT_POSTED_WITHIN_DAYS == 7


def test_the_day_is_the_one_in_the_last_us_time_zone_not_the_servers():
    job = posting("2026-11-20")
    # 05:00 UTC on the 28th is still the 27th from New York to Hawaii: the posting of the 20th is seven days old.
    assert freshness.today(datetime(2026, 11, 28, 5, 0, tzinfo=timezone.utc)).isoformat() == "2026-11-27"
    assert age_class(job, 7, datetime(2026, 11, 28, 5, 0, tzinfo=timezone.utc)) == FRESH
    assert age_class(job, 7, datetime(2026, 11, 28, 9, 59, tzinfo=timezone.utc)) == FRESH        # 23:59 in Hawaii
    assert age_class(job, 7, datetime(2026, 11, 28, 10, 0, tzinfo=timezone.utc)) == TOO_OLD      # midnight there: the 28th everywhere in the US
    # A moment given without a zone is UTC; a posting dated "tomorrow" by UTC (published east of it) is today's.
    assert freshness.today(datetime(2026, 11, 28, 5, 0)) == freshness.today(datetime(2026, 11, 28, 5, 0, tzinfo=timezone.utc))
    assert age_class(posting("2026-11-28"), 7, NOON) == FRESH and freshness.age_days(posting("2026-11-28"), NOON) == 0


def test_a_date_in_the_future_or_absurdly_old_is_treated_as_unknown():
    for value in ("2026-11-30", "2031-01-01", "1970-01-01", "0001-01-01", "2010-05-05", "not a date", "2026-13-45", "", None):
        assert freshness.clean_date(value, NOON) == "" and age_class(posting(str(value or "")), 7, NOON) == UNDATED, value
    assert freshness.clean_date("2026-11-20T10:00:00-05:00", NOON) == "2026-11-20"          # a timestamp counts by its day
    assert freshness.clean_date("2017-01-15", NOON) == "2017-01-15" and age_class(posting("2017-01-15"), 30, NOON) == TOO_OLD   # old, but a real date
    assert freshness.undated(posting("1970-01-01"), NOON) and not freshness.undated(posting("2026-11-20"), NOON)


def test_a_least_age_can_prove_a_posting_too_old_but_never_new():
    # "Posted 30+ Days Ago" / a "last updated" date: the publish day is unknown, the posting is at least that old.
    assert age_class(posting(posted_before=days_ago(31, NOON)), 30, NOON) == TOO_OLD
    assert age_class(posting(posted_before=days_ago(31, NOON)), 7, NOON) == TOO_OLD
    assert age_class(posting(posted_before=days_ago(30, NOON)), 30, NOON) == UNDATED       # "30+": may be exactly 30 days, which is allowed
    assert age_class(posting(posted_before=days_ago(2, NOON)), 7, NOON) == UNDATED         # updated two days ago says nothing about new
    assert age_class(posting(posted_before=days_ago(400, NOON)), 0, NOON) == UNDATED       # any time: delivered as undated
    assert age_class(posting(days_ago(2, NOON), posted_before=days_ago(90, NOON)), 7, NOON) == FRESH    # a publish date wins
    assert freshness.least_age_days(posting(posted_before=days_ago(31, NOON)), NOON) == 31


def test_a_stored_value_from_before_the_limit_existed_means_seven_days():
    assert [within_days(v) for v in (None, "", "x", 5, -1, True, 7.5, [7])] == [7] * 8        # nothing stored, or nothing valid: the default
    assert [within_days(v) for v in (0, 1, 3, 7, 14, 30, "14", " 30 ")] == [0, 1, 3, 7, 14, 30, 14, 30]
    assert SearchRequest(resume_id="resume-test-0001", job_titles="AI Engineer", years_experience=3).posted_within_days == 7
    assert SearchRequest(resume_id="resume-test-0001", job_titles="AI Engineer", years_experience=3).include_undated is True
    for bad in (2, 5, 60, -1):
        with pytest.raises(ValueError):
            make_request(posted_within_days=bad)
    assert "posted_before" not in posting("2026-11-20", "2026-11-01").public() and posting("2026-11-20").public()["posted_at"] == "2026-11-20"


# ---------------------------------------------------------------- 2. dates a source states in words
def test_words_about_the_posting_date_are_read_and_never_guessed():
    d = lambda n: days_ago(n, NOON)
    exact = {"Posted Today": d(0), "Posted Yesterday": d(1), "Posted 3 Days Ago": d(3), "posted an hour ago": d(0), "Just posted": d(0),
             "Date posted: November 20, 2026": "2026-11-20", "Posted on 2026-11-20": "2026-11-20", "Posted: 11/20/2026": "2026-11-20",
             "Posting Date\n11/18/2026, 07:39 PM": "2026-11-18", "Posting Date 20 Nov 2026": "2026-11-20", "Job posted on Nov 20th, 2026": "2026-11-20",
             "Published: 20.11.2026": "2026-11-20", "Posted Nov. 3, 2026": "2026-11-03"}
    for text, day in exact.items():
        assert stated_posting(text, NOON) == (day, ""), text
    least = {"Posted 30+ Days Ago": d(30), "posted 2 weeks ago": d(14), "Posted a month ago": d(30), "Posted more than 30 days ago": d(30),
             "Posted over 5 days ago": d(5)}
    for text, day in least.items():
        assert stated_posting(text, NOON) == ("", day), text                    # only a least age: never a publish date
    for text in ("3 days ago", "Updated 2 days ago", "Apply by November 30, 2026", "Closing date: 2026-12-01", "Start date: January 5, 2027",
                 "Posted: 05/06/2026",                                          # the fifth of June or the sixth of May: not known
                 "Posted 3 days ago. More jobs: Data Analyst, posted 9 days ago",     # two different dates: which one is this job's would be a guess
                 "Posted on December 25, 2027", "Date Posted 1970-01-01", "Posted recently", "", None):
        assert stated_posting(text, NOON) == ("", ""), text
    assert stated_posting("Posted 3 days ago ... Posted 3 Days Ago", NOON) == (d(3), "")        # said twice, the same day
    # Only the start of a page is read: its own content comes first, lists of other jobs and footers after it.
    page = "Data Engineer. Posted 2 days ago. " + "x " * 4000 + " Similar jobs: posted 20 days ago"
    assert freshness.stated_on_page(page, NOON) == (d(2), "")


# ---------------------------------------------------------------- 3. the date each reader maps
def test_each_board_reader_takes_the_publish_date_and_never_an_update_date():
    stamp = "2026-11-27T00:00:00Z"
    raw = {"id": 1, "title": "AI Engineer", "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1", "location": {"name": "Austin, TX"},
           "first_published": "2026-08-01T10:00:00-04:00", "updated_at": "2026-11-26T09:00:00-05:00"}
    job = greenhouse.normalize("acme", raw, stamp)
    assert (job.posted_at, job.posted_before) == ("2026-08-01", "")              # updated yesterday, published in August: an August posting
    job = greenhouse.normalize("acme", {**raw, "first_published": None}, stamp)
    assert (job.posted_at, job.posted_before) == ("", "2026-11-26")             # no publish date: the update date is only a least age
    assert age_class(job, 7, NOON) == UNDATED
    old = greenhouse.normalize("acme", {**raw, "first_published": None, "updated_at": "2026-09-01T09:00:00-05:00"}, stamp)
    assert age_class(old, 7, NOON) == TOO_OLD                                   # not updated since September: older than a week for certain
    job = lever.normalize("beta", {"id": "11111111-1111-4111-8111-111111111111", "text": "AI Engineer", "createdAt": int(datetime(2026, 11, 25, 12, tzinfo=timezone.utc).timestamp() * 1000),
                                   "hostedUrl": "https://jobs.lever.co/beta/11111111-1111-4111-8111-111111111111"}, stamp)
    assert job.posted_at == "2026-11-25"                                        # createdAt, epoch milliseconds (the only date Lever publishes)
    job = ashby.normalize("gamma", {"id": "22222222-2222-4222-8222-222222222222", "title": "AI Engineer", "publishedAt": "2026-11-24T15:00:00.000+00:00",
                                    "updatedAt": "2026-11-27T00:00:00.000+00:00", "jobUrl": "https://jobs.ashbyhq.com/gamma/22222222-2222-4222-8222-222222222222"}, stamp)
    assert job.posted_at == "2026-11-24"
    job = workable.account_job("acme", "Acme", {"title": "AI Engineer", "shortcode": "ABC123DEF4", "url": "https://apply.workable.com/acme/j/ABC123DEF4/",
                                                "published_on": "2026-11-22", "created_at": "2026-10-01", "country": "United States"}, stamp)
    assert job.posted_at == "2026-11-22"                                        # published_on, not created_at
    job = ats_startup.recruitee_job("acme", {"title": "AI Engineer", "slug": "ai-engineer", "careers_url": "https://acme.recruitee.com/o/ai-engineer",
                                             "published_at": "2026-11-21 08:29:18 UTC", "updated_at": "2026-11-27 08:29:18 UTC", "status": "published"}, stamp)
    assert job.posted_at == "2026-11-21"
    job = ats_extra.sr_job("AcmeCo", {"id": "744000012345678", "name": "AI Engineer", "releasedDate": "2026-11-23T11:22:33.000Z",
                                      "location": {"city": "Austin", "region": "TX", "country": "us"}}, stamp)
    assert job.posted_at == "2026-11-23" and job.needs_detail
    job = ats_oracle._job("eeho.fa.us2.oraclecloud.com|CX_1", "Oracle", {"Id": "350789", "Title": "AI Engineer", "PostedDate": "2026-11-20",
                                                                          "PrimaryLocation": "Austin, TX, United States", "PrimaryLocationCountry": "US"}, stamp)
    assert job.posted_at == "2026-11-20"
    job = amazon._job({"title": "AI Engineer", "job_path": "/en/jobs/900/ai-engineer", "id_icims": "900", "posted_date": "November 19, 2026",
                       "updated_time": "1 day", "country_code": "USA"}, stamp)
    assert job.posted_at == "2026-11-19"


def test_workday_reads_posted_n_days_ago_from_the_list_and_the_start_date_from_the_detail():
    key = "wdco|wd5|Careers"
    row = lambda words: {"title": "AI Engineer", "externalPath": "/job/US-TX-Austin/AI-Engineer_R100", "locationsText": "US, TX, Austin",
                         "bulletFields": ["R100"], **({"postedOn": words} if words else {})}
    listed = lambda words: workday._list_job(key, row(words), True, "2026-11-27T00:00:00Z")
    assert (listed("Posted Today").posted_at, listed("Posted Yesterday").posted_at) == (days_ago(0), days_ago(1))
    assert (listed("Posted 6 Days Ago").posted_at, listed("Posted 6 Days Ago").posted_before) == (days_ago(6), "")
    old = listed("Posted 30+ Days Ago")
    assert (old.posted_at, old.posted_before) == ("", days_ago(30)) and age_class(old, 14) == TOO_OLD and age_class(old, 30) == UNDATED
    assert (listed("").posted_at, listed("").posted_before) == ("", "") and (listed("Posted whenever").posted_at, listed("Posted whenever").posted_before) == ("", "")
    # The detail's start date is the exact day and replaces the least age of the list.
    detail = workday._apply_detail(listed("Posted 30+ Days Ago"), {"title": "AI Engineer", "location": "US, TX, Austin", "startDate": "2026-09-14",
                                                                   "jobDescription": "<p>x</p>", "canApply": True, "posted": True})
    assert (detail.posted_at, detail.posted_before) == ("2026-09-14", "")
    # A detail without a start date: its own words, else what the list said.
    worded = workday._apply_detail(listed(""), {"title": "AI Engineer", "postedOn": "Posted 2 Days Ago", "jobDescription": "<p>x</p>"})
    kept = workday._apply_detail(listed("Posted 4 Days Ago"), {"title": "AI Engineer", "jobDescription": "<p>x</p>"})
    assert worded.posted_at == days_ago(2) and kept.posted_at == days_ago(4)


def test_google_jobs_listings_carry_the_age_google_states():
    item = lambda words: {"title": "AI Engineer", "company_name": "Initech", "location": "Austin, TX", "via": "Dice",
                          "apply_options": [{"link": "https://www.dice.com/job-detail/1"}], "description": "Python.",
                          "detected_extensions": {"posted_at": words} if words else {}}
    hosts = ("dice.com",)
    assert serp_item(item("3 days ago"), hosts, True)[1].posted_at == days_ago(3)
    old = serp_item(item("30+ days ago"), hosts, True)[1]
    assert (old.posted_at, old.posted_before) == ("", days_ago(30))
    assert serp_item(item(""), hosts, True)[1].posted_at == "" and serp_item(item("soon"), hosts, True)[1].posted_at == ""


# ---------------------------------------------------------------- 4. the page verifier
def page_of(body: str, url: str = "https://careers.acme.example/jobs/4411-ai-engineer") -> jv.Page:
    return jv.Page(url, 200, body, jv.page_text(body))


def jsonld(**fields) -> str:
    data = {"@type": "JobPosting", "title": "AI Engineer", "hiringOrganization": {"name": "Acme AI"}, "description": "Python and PyTorch.",
            "jobLocation": {"address": {"addressLocality": "Austin", "addressRegion": "TX", "addressCountry": "US"}}, **fields}
    return '<script type="application/ld+json">' + json.dumps(data) + "</script>"


def test_a_page_gives_its_structured_date_or_the_date_it_states_and_nothing_else(monkeypatch):
    monkeypatch.setattr(jv, "_now", lambda: NOON)
    lead = Candidate(url="https://careers.acme.example/jobs/4411-ai-engineer", provider="Claude", cited=True)
    html = lambda head, body="<h1>AI Engineer</h1><p>Apply now</p>": page_of(f"<html><head><title>AI Engineer</title>{head}</head><body>{body}</body></html>")
    job, _ = jv._evaluate(html(jsonld(datePosted="2026-11-24T08:00:00Z")), lead)
    assert (job.posted_at, job.posted_before) == ("2026-11-24", "")                     # schema.org datePosted
    job, _ = jv._evaluate(html(jsonld(datePosted="2027-03-01")), lead)
    assert job.posted_at == ""                                                          # a date in the future is not a posting date
    job, _ = jv._evaluate(html(jsonld(), "<h1>AI Engineer</h1><p>Posted 4 days ago</p><p>Apply now</p>"), lead)
    assert job.posted_at == days_ago(4, NOON)                                           # no datePosted: the words on the page
    job, _ = jv._evaluate(html(jsonld(), "<h1>AI Engineer</h1><p>Posted 30+ days ago</p><p>Apply now</p>"), lead)
    assert (job.posted_at, job.posted_before) == ("", days_ago(30, NOON))
    job, _ = jv._evaluate(html(jsonld(dateModified="2026-11-26", validThrough="2027-01-31")), lead)
    assert (job.posted_at, job.posted_before) == ("", "")                               # an update date or a closing date is no posting date
    micro = ('<div itemscope itemtype="https://schema.org/JobPosting"><h1 itemprop="title">AI Engineer</h1>'
             '<div itemprop="hiringOrganization" itemscope itemtype="https://schema.org/Organization"><meta itemprop="name" content="Acme AI"></div>'
             '<meta itemprop="datePosted" content="2026-11-22T10:00:00.000Z"><div itemprop="description">Python and PyTorch.</div></div>')
    job, _ = jv._evaluate(html("", micro + "<p>Apply now</p>"), lead)
    assert job is not None and "microdata" in job.verification and job.posted_at == "2026-11-22"


def test_the_ai_page_check_may_only_point_at_words_that_are_on_the_page(monkeypatch):
    monkeypatch.setattr(jv, "_now", lambda: NOON)
    assert "posted_quote" in EXTRACT_SCHEMA["properties"]["pages"]["items"]["properties"]
    lead = Candidate(url="https://careers.acme.example/jobs/4411-ai-engineer", provider="Claude", cited=True)
    body = ("<html><head><title>AI Engineer</title></head><body><h1>AI Engineer</h1><p>Acme AI</p><p>Austin, TX</p>" + "<p>About the team.</p>" * 400
            + "<p>Req 4411. Posted on November 21, 2026</p><p>Apply now</p></body></html>")
    page = page_of(body)
    answer = {"is_open_job_posting": True, "page_kind": "one_open_job_posting", "closed_quote": "", "title": "AI Engineer", "company": "Acme AI",
              "location": "Austin, TX"}
    assert freshness.stated_on_page(page.text, NOON) == ("", "")                        # too far down for the plain reading
    job, _ = jv.job_from_extraction(page, lead, {**answer, "posted_quote": "Posted on November 21, 2026"})
    assert job.posted_at == "2026-11-21"                                                # the model pointed at the page's own words
    job, _ = jv.job_from_extraction(page, lead, {**answer, "posted_quote": "Posted on November 26, 2026"})
    assert job is not None and job.posted_at == ""                                      # words that are not on the page: no date
    job, _ = jv.job_from_extraction(page, lead, {**answer, "posted_quote": "November 21, 2026"})
    assert job.posted_at == ""                                                          # a bare date says nothing about posting
    job, _ = jv.job_from_extraction(page, lead, answer)
    assert job.posted_at == "" and job.posted_before == ""                              # nothing stated, nothing invented


# ---------------------------------------------------------------- 5. the engine
def gh(job_id: int, days: int | None, title: str | None = None, updated: int | None = None, location: str = "Austin, TX") -> dict:
    """A Greenhouse posting published `days` ago (None: the board gives no publish date; `updated`: its update date)."""
    stamp = lambda n: f"{days_ago(n)}T10:00:00-04:00"
    return {"id": job_id, "title": title or f"AI Engineer, Team {job_id}", "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{job_id}",
            "location": {"name": location}, "content": conftest.DESC, "company_name": "Acme AI", "offices": [], "metadata": [],
            "first_published": stamp(days) if days is not None else None, "updated_at": stamp(updated) if updated is not None else None}


@pytest.fixture
def board(monkeypatch):
    def put(*jobs):
        monkeypatch.setattr(conftest, "GREENHOUSE_ACME", {"jobs": list(jobs)})
    return put


def gh_engine(make_engine, **kw):
    return make_engine(seeds=[("greenhouse", "acme")], sources=("greenhouse",), **kw)


def ids(state) -> set[int]:
    return {int(job.ats_id) for job in state.results}


MIXED = (gh(1, 3), gh(2, 7), gh(3, 8), gh(4, 40), gh(5, None), gh(6, None, updated=2), gh(7, None, updated=60))


def test_a_search_never_delivers_a_posting_older_than_its_limit(make_engine, board):
    board(*MIXED)
    state = run(run_search(gh_engine(make_engine), make_request(posted_within_days=7)))
    assert ids(state) == {1, 2, 5, 6}                                           # 7 days is in, 8 is out; no date: delivered, marked below
    stats = state.stats
    assert (stats["posted_within_days"], stats["include_undated"], stats["too_old"], stats["undated_dropped"]) == (7, True, 3, 0)
    assert (stats["dated_delivered"], stats["undated_delivered"], stats["final_matches"]) == (2, 2, 4)
    assert stats["age_by_source"] == {"Greenhouse": {"fresh": 2, "too_old": 3, "undated": 2}}
    row = stats["platforms"]["greenhouse"]
    assert row["reasons"]["too_old"] == 3 and (row["considered"], row["unique"], row["matched"]) == (7, 4, 4)
    assert {int(j.ats_id) for j in state.results if not j.posted_at} == {5, 6}         # the mark of an undated job: no posting date
    assert "3 were posted longer ago than the limit of this search (7 days)" in state.message
    # 30 days: the posting of 8 days ago is in; 40 days, and "not updated for 60 days", stay out.
    assert ids(run(run_search(gh_engine(make_engine), make_request(posted_within_days=30)))) == {1, 2, 3, 5, 6}
    day = run(run_search(gh_engine(make_engine), make_request(posted_within_days=1)))
    assert ids(day) == {5} and day.stats["too_old"] == 6 and "(24 hours)" in day.message     # "last updated two days ago" is older than a day for certain
    # Any time: nothing is left out for its age.
    anytime = run(run_search(gh_engine(make_engine), make_request(posted_within_days=0)))
    assert ids(anytime) == {1, 2, 3, 4, 5, 6, 7} and anytime.stats["too_old"] == 0 and anytime.stats["undated_delivered"] == 3


def test_jobs_without_a_date_are_left_out_only_when_the_search_says_so(make_engine, board):
    board(*MIXED)
    state = run(run_search(gh_engine(make_engine), make_request(posted_within_days=7, include_undated=False)))
    assert ids(state) == {1, 2} and all(job.posted_at for job in state.results)
    assert (state.stats["undated_dropped"], state.stats["undated_delivered"], state.stats["too_old"]) == (2, 0, 3)
    assert state.stats["platforms"]["greenhouse"]["reasons"] == {"too_old": 3, "undated": 2}
    assert "2 state no posting date (this search leaves those out)" in state.message
    anytime = run(run_search(gh_engine(make_engine), make_request(posted_within_days=0, include_undated=False)))
    assert ids(anytime) == {1, 2, 3, 4} and anytime.stats["undated_dropped"] == 3      # "any time" with dated jobs only


def test_an_undated_job_ranks_after_dated_jobs_of_the_same_score_and_keeps_its_score(make_engine, board):
    same = dict(score=80, score_source="ai")
    dated, undated, better = posting(days_ago(2), company="Zeta", **same), posting("", company="Alpha", **same), posting("", company="Mid", score=81, score_source="ai")
    assert sorted([undated, dated, better], key=SearchEngine._order) == [better, dated, undated]      # a tie-break only: a higher score still wins
    assert sorted([posting("1970-01-01", company="Alpha", **same), dated], key=SearchEngine._order)[0] is dated      # an impossible date is no date
    focus = Scope.of([], True, {"greenhouse", "ashby"}).order_key(SearchEngine._order)
    assert sorted([undated, dated], key=focus) == [dated, undated]              # also under the startup focus
    # A date that cannot be one (the future, a placeholder) is not handed on as a posting date: the job is an undated job.
    impossible = gh(3, 2, "AI Engineer, Team Future")
    impossible["first_published"] = "2031-01-01T00:00:00Z"
    board(impossible, gh(4, 1, "AI Engineer, Team Now"))
    state = run(run_search(gh_engine(make_engine), make_request(posted_within_days=7)))
    assert [(int(j.ats_id), bool(j.posted_at)) for j in state.results] == [(4, True), (3, False)] and state.stats["undated_delivered"] == 1
    # In a search: two postings with the same text get the same score whatever their date, and the dated one comes first.
    board(gh(1, None, "AI Engineer, Team Alpha"), gh(2, 2, "AI Engineer, Team Zeta"))
    state = run(run_search(gh_engine(make_engine), make_request(posted_within_days=7)))
    assert [int(j.ats_id) for j in state.results] == [2, 1] and state.results[0].score == state.results[1].score


class CountingAI(FakeAI):
    """Records which postings reach the models that rank, judge and audit."""
    def __init__(self):
        super().__init__(leads=[], available=True)
        self.judged: list[str] = []
        self.audited: list[str] = []

    async def rank_batch(self, profile, req, jobs):
        self.judged += [job.ats_id for job in jobs]
        return {}

    async def audit(self, req, jobs):
        self.audited += [job.ats_id for job in jobs]
        return {}


def test_old_postings_never_reach_the_ranking_judging_or_audit(make_engine, board):
    board(gh(1, 2), gh(2, 5), *(gh(100 + i, 20 + i) for i in range(30)))
    engine = gh_engine(make_engine)
    ai = CountingAI()
    engine.ai_factory = lambda: ai
    state = run(run_search(engine, make_request(posted_within_days=7, result_count=10)))
    assert ids(state) == {1, 2} and state.stats["too_old"] == 30
    assert sorted(ai.judged) == ["1", "2"] and set(ai.audited) <= {"1", "2"}    # the 30 old ones cost no model call
    assert state.stats["verified_active"] == state.stats["unique_candidates"] == 2      # "verified" counts deliverable jobs only


def test_the_requested_count_is_reached_only_by_jobs_that_can_be_delivered(make_engine, board):
    board(gh(1, 1), gh(2, 2), *(gh(100 + i, 30 + i) for i in range(12)))
    state = run(run_search(gh_engine(make_engine), make_request(posted_within_days=7, result_count=3)))
    # 14 verified postings match, 12 of them too old: the search has 2, not "3 reached".
    assert state.stats["rounds"][0]["matches"] == 2 and state.stats["rounds"][0]["verified"] == 2
    assert "requested number" not in state.stats["stop_reason"] and len(state.results) == 2
    assert state.message.startswith("Found 2 of 3 requested jobs") and "12 were posted longer ago than the limit" in state.message
    full = run(run_search(gh_engine(make_engine), make_request(posted_within_days=0, result_count=3)))
    assert full.stats["rounds"][0]["matches"] == 14 and full.stats["stop_reason"].startswith("the requested number") and len(full.results) == 3


COPY = "https://careers.acme.example/jobs/4411-ai-engineer-team-9"


def copy_page(date: str = "") -> tuple[int, str]:
    """The same vacancy on the employer's own career site, with or without a posting date."""
    return 200, ("<html><head><title>AI Engineer, Team 9</title>" + jsonld(title="AI Engineer, Team 9", **({"datePosted": date} if date else {}))
                 + "</head><body><h1>AI Engineer, Team 9</h1><p>Apply now</p></body></html>")


def test_a_copy_without_a_date_is_not_delivered_in_place_of_its_old_original(make_engine, board, monkeypatch):
    lead = [Candidate(url=COPY, provider="Claude", cited=True)]
    monkeypatch.setitem(conftest.PAGES, COPY, copy_page())
    # The board says the posting is 50 days old; the career-site copy of it states no date.
    board(gh(9, 50, "AI Engineer, Team 9"), gh(1, 2))
    state = run(run_search(gh_engine(make_engine, leads=lead, ai_available=True), make_request(posted_within_days=7)))
    assert ids(state) == {1} and COPY not in {job.url for job in state.results}
    assert state.stats["too_old"] == 2 and state.stats["undated_delivered"] == 0       # the original and its copy
    # The same copy beside a fresh original: one job, with the board's date.
    board(gh(9, 3, "AI Engineer, Team 9"), gh(1, 2))
    state = run(run_search(gh_engine(make_engine, leads=lead, ai_available=True), make_request(posted_within_days=7)))
    assert len(state.results) == 2 and all(job.posted_at for job in state.results) and state.stats["too_old"] == 0
    # A copy with no original in the search is what it is: an undated job.
    board(gh(1, 2))
    state = run(run_search(gh_engine(make_engine, leads=lead, ai_available=True), make_request(posted_within_days=7)))
    assert COPY in {job.url for job in state.results} and state.stats["undated_delivered"] == 1


def test_what_copies_of_one_vacancy_know_about_its_age():
    old = posting(days_ago(40), ats="greenhouse", ats_id="9", url="https://job-boards.greenhouse.io/acme/jobs/9")
    witness = AgeWitness()
    witness.add(old)
    copy = posting("", url="https://careers.acme.example/jobs/9")                      # same employer, title and place; no date
    assert witness.says_old(copy) and len(witness) == 1
    assert witness.says_old(posting(days_ago(1), source="Jobicy", aggregator=True, url="https://jobicy.com/jobs/9"))      # a job board's date does not outvote the employer
    assert not witness.says_old(posting(days_ago(1), url="https://careers.acme.example/jobs/9"))       # an employer's record with its own date speaks for itself
    assert not witness.says_old(posting("", title="Data Analyst", url="https://careers.acme.example/jobs/10"))
    assert not witness.says_old(posting("", ats="greenhouse", ats_id="10", url="https://job-boards.greenhouse.io/acme/jobs/10"))   # another requisition of the same ATS
    assert witness.says_old(posting("", url="https://example.com/x", apply_url="https://job-boards.greenhouse.io/acme/jobs/9"))    # its apply link is the old posting
    # Only a job board said "old": that settles an undated copy, not an employer's dated record or another dated copy.
    boards = AgeWitness()
    boards.add(posting(days_ago(40), source="Jobicy", aggregator=True, url="https://jobicy.com/jobs/9"))
    assert boards.says_old(copy) and not boards.says_old(posting(days_ago(1), source="Himalayas", aggregator=True, url="https://himalayas.app/9"))
    merged = AgeWitness()
    merged.update(witness)
    assert merged.says_old(copy) and not AgeWitness().says_old(copy)


def test_a_workday_posting_the_list_calls_old_is_dropped_before_its_detail_is_fetched(make_engine, monkeypatch):
    rows = [{**row, "postedOn": words} for row, words in zip(conftest.WD_POSTINGS, ("Posted 30+ Days Ago", "Posted 2 Days Ago", "Posted Today", "Posted 9 Days Ago"))]
    monkeypatch.setattr(conftest, "WD_POSTINGS", rows)
    engine = make_engine(seeds=[("workday", "wdco|wd5|Careers")], sources=("workday",))
    state = run(run_search(engine, make_request(job_titles="AI Engineer, Machine Learning Engineer", posted_within_days=7)))
    detail = lambda path: CALLS["https://wdco.wd5.myworkdayjobs.com/wday/cxs/wdco/Careers" + path]
    assert detail("/job/US-TX-Austin/AI-Engineer_R100") == 0 and detail("/job/US-VA/Sr-AI-Engineer_R103") == 0      # "30+ days", "9 days": no request
    assert detail("/job/2-Locations/ML-Engineer_R101") == 1 and state.stats["detail_fetches"] == 1                  # R102 answered 404 (closed)
    assert [(j.title, j.posted_at) for j in state.results] == [("Machine Learning Engineer", conftest.POSTED_ISO)]  # the detail's start date
    assert state.stats["too_old"] == 2 and state.stats["platforms"]["workday"]["reasons"]["too_old"] == 2


# ---------------------------------------------------------------- 6. the lead check: every lead, and no site asked after it said no
def test_a_site_that_refuses_is_not_asked_again_however_late_its_answers_arrive(make_engine, monkeypatch):
    from app.search_engine import PROBES_PER_SITE
    refusing = [f"https://careers.walled.example/jobs/{1000 + i}-ai-engineer" for i in range(12)]
    open_page = "https://careers.acme.example/jobs/5511-ai-engineer"
    monkeypatch.setitem(conftest.PAGES, open_page, (200, "<html><head><title>AI Engineer</title>" + jsonld(datePosted=days_ago(1))
                                                    + "</head><body><h1>AI Engineer</h1><p>Apply now</p></body></html>"))
    for url in refusing:
        monkeypatch.setitem(conftest.PAGES, url, (403, "forbidden"))
    monkeypatch.setattr("app.search_engine.PROBE_SECONDS", 0.0)                 # the first answers are not in when the other checks are queued
    leads = [Candidate(url=u, provider="Claude", cited=True) for u in [*refusing, open_page]]
    engine = make_engine(leads=leads, ai_available=True, seeds=[], sources=())
    engine.verifier.per_host, engine.verifier.host_gap = 1, 0.0
    state = run(run_search(engine, make_request(posted_within_days=7)))
    asked = sum(CALLS[u] for u in refusing)
    assert asked == PROBES_PER_SITE == 2 and state.stats["pages_site_refused"] == 10       # two refusals, then no further request
    assert [job.url for job in state.results] == [open_page]
    assert (state.stats["page_leads"], state.stats["page_leads_checked"], state.stats["page_leads_unchecked"]) == (13, 3, 0)
    assert SITE_REFUSED.startswith("Not requested")


# ---------------------------------------------------------------- 7. discovery: more AI web search, every call something else
def default_orchestrator(target=25, **settings):
    s = Settings(_env_file=None, **settings)
    ai = AIOrchestrator(s, SiteLister("OpenAI"), Recorder("Claude"))
    ai.configure(plan_for(s, target))
    scope = Scope.of([], False, READERS)
    hosts = scope.hosts(HOSTS)
    ai.set_scope(scope, hosts, [h for h in hosts if h == "myworkdayjobs.com"])
    return ai


VARIANTS = ["ML Engineer", "Machine Learning Engineer", "LLM Engineer", "MLOps Engineer", "NLP Engineer", "Applied Scientist", "Data Scientist",
            "Deep Learning Engineer", "Generative AI Engineer", "Computer Vision Engineer", "AI Research Engineer", "AI Platform Engineer"]


def test_a_round_makes_more_calls_than_before_and_no_two_of_them_ask_the_same_thing():
    s = Settings(_env_file=None)
    assert (s.openai_search_calls, s.claude_search_calls, s.openai_open_web_calls, s.claude_open_web_calls, s.ai_open_web_followup_calls) == (10, 10, 8, 10, 6)
    assert (s.ai_web_searches_per_call, s.ai_open_web_searches_per_call, s.max_page_verifications, s.lead_verification_seconds) == (5, 10, 1200, 240.0)
    assert (s.openai_search_concurrency, s.claude_search_concurrency, s.ai_discovery_breaker_failures) == (24, 24, 2)
    assert (s.max_page_verifications_max, s.ai_page_extraction_calls, s.max_browser_pages_per_search) == (4800, 16, 40)
    ai = default_orchestrator()
    run(ai.discover(Profile(skills=["Python", "PyTorch"]), make_request(result_count=25), VARIANTS, []))
    own = lambda call: "OWN career sites" in call["prompt"]
    on_hosts = {name: [c for c in provider.calls if not own(c)] for name, provider in (("OpenAI", ai.openai), ("Claude", ai.claude))}
    own_sites = {name: [c for c in provider.calls if own(c)] for name, provider in (("OpenAI", ai.openai), ("Claude", ai.claude))}
    assert {name: len(calls) for name, calls in on_hosts.items()} == {"OpenAI": 10, "Claude": 10}        # before 2026-11-27: 6 and 6
    assert len(ai.openai.plans) == 8                                             # OpenAI names the career sites of each of its 8 open-web calls first
    assert len(own_sites["Claude"]) == 10 and len(ai.openai.calls) == 10 + 8
    # Calls on the ATS hosts: 100 searches, every one a different (title, host, place), in either provider.
    asked = [query for calls in on_hosts.values() for call in calls for query in searches_of(call["prompt"])]
    assert len(asked) == len(set(asked)) == 100 and all(call["searches"] == 5 for calls in on_hosts.values() for call in calls)
    titles = {query.split('"')[1] for query in asked}
    assert "AI Engineer" in titles and len(titles & set(VARIANTS)) >= 10         # the extra calls search further titles, not the same ones again
    assert len({query.split(" ", 1)[0] for query in asked}) >= 5                 # ... on every host of the search
    # Calls on employers' own sites: each about another kind of employer (or the same kind one step further down).
    kinds = [line for call in own_sites["Claude"] for line in call["prompt"].splitlines() if line.startswith("Employers for this call:")]
    planned = [plan["user"].splitlines()[0] for plan in ai.openai.plans]
    assert len(set(kinds)) == 10 and len(set(planned)) == 8 and len({k.split(":", 1)[1].strip() for k in kinds} | {p.split(":", 1)[1].strip() for p in planned}) == 18
    sites = [query.split(" ", 1)[0] for call in own_sites["OpenAI"] for query in searches_of(call["prompt"])]
    assert len(sites) == len(set(sites)) == 80                                   # 8 calls x 10 career sites, none twice
    assert ai.calls == ai.discovery_calls + 8 == 38 + 8                          # 38 web-search calls and the 8 site lists


def test_the_first_round_already_searches_the_title_variants_of_the_search(make_engine):
    class Expanding(FakeAI):
        related: list = []

        async def expand_titles(self, titles, profile):
            return ["Machine Learning Engineer", "LLM Engineer", "Applied Scientist"], "TechSara"

        async def discover(self, profile, req, related, known):
            Expanding.related = list(related)
            return []

    engine = make_engine(seeds=[("greenhouse", "acme")], sources=("greenhouse",))
    engine.ai_factory = lambda: Expanding(available=True)
    run(run_search(engine, make_request()))
    assert {"Machine Learning Engineer", "LLM Engineer", "Applied Scientist"} <= set(Expanding.related)      # known to round 1, not only to round 2


def test_web_search_calls_are_told_the_limit_and_search_engines_are_asked_for_the_span():
    assert fresh_only(0) == "" and "the last 7 days" in fresh_only(7) and "the last 24 hours" in fresh_only(1)
    assert "keep a result that shows no date" in fresh_only(7)                   # recency never hides a posting without a date
    ai = default_orchestrator(openai_search_calls=1, claude_search_calls=1, openai_open_web_calls=0, claude_open_web_calls=1, ai_open_web_followup_calls=0)
    run(ai.discover(Profile(), make_request(posted_within_days=7), ["ML Engineer"], []))
    assert all("Only postings published in the last 7 days are wanted" in c["prompt"] for c in [*ai.openai.calls, *ai.claude.calls])
    assert all(" posted " not in q for c in ai.claude.calls if c["restrict"] for q in searches_of(c["prompt"]))       # `site:` searches stay plain
    anytime = default_orchestrator(openai_search_calls=1, claude_search_calls=1, openai_open_web_calls=0, claude_open_web_calls=0)
    run(anytime.discover(Profile(), make_request(posted_within_days=0), ["ML Engineer"], []))
    assert not any("Only postings published" in c["prompt"] for c in [*anytime.openai.calls, *anytime.claude.calls])
    # The month words of the open-web searches cover the limit: early in a month, the month before as well.
    early = datetime(2026, 12, 3, 22, 0, tzinfo=timezone.utc)
    assert freshness.month_words(7, early) == ["posted December 2026", "posted November 2026"]
    assert freshness.month_words(1, early) == freshness.month_words(0, early) == ["posted December 2026"]
    assert freshness.month_words(30, NOON) == ["posted November 2026", "posted October 2026"]
    assert [freshness.time_range(n) for n in (0, 1, 3, 7, 14, 30)] == ["", "day", "week", "week", "month", "month"]


def test_a_time_range_never_hides_results_the_plain_query_has():
    sent: list[dict] = []
    answers = {"week": [], "": ["https://jobs.lever.co/acme/11111111-1111-4111-8111-111111111111"]}

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        sent.append(params)
        return httpx.Response(200, json={"results": [{"url": u} for u in answers[params.get("time_range", "")]]})

    async def go():
        async def sleep(_):
            return None
        client = SearxClient("http://searx.test", httpx.AsyncClient(transport=httpx.MockTransport(handler)), None, sleep=sleep)
        useful = lambda url: "lever.co" in url
        ranged = await client.search('site:jobs.lever.co "AI Engineer"', 1, useful=useful, time_range="week")
        # The restricted query brought nothing usable: asked once more without the restriction, and that answer is used.
        assert [p.get("time_range") for p in sent] == ["week", None] and ranged.urls == answers[""] and ranged.ranged and ranged.widened
        answers["week"] = ["https://jobs.lever.co/acme/22222222-2222-4222-8222-222222222222"]
        fresh = await client.search('site:jobs.lever.co "ML Engineer"', 1, useful=useful, time_range="week")
        assert fresh.urls == answers["week"] and fresh.ranged and not fresh.widened and len(sent) == 3
        plain = await client.search('site:jobs.lever.co "LLM Engineer"', 1, useful=useful)
        assert "time_range" not in sent[-1] and not plain.ranged and len(sent) == 4
        bogus = await client.search('site:jobs.lever.co "NLP Engineer"', 1, useful=useful, time_range="decade")
        assert "time_range" not in sent[-1] and not bogus.ranged                 # not one of SearXNG's spans: no restriction
        # The restricted and the plain answer of one query are remembered apart.
        again = await client.search('site:jobs.lever.co "ML Engineer"', 1, useful=useful, time_range="week")
        unrestricted = await client.search('site:jobs.lever.co "ML Engineer"', 1, useful=useful)
        assert again.cached and again.urls == answers["week"] and not unrestricted.cached and unrestricted.urls == answers[""]
    run(go())


def test_rate_limited_calls_are_counted_apart_and_named_in_the_warning(make_engine):
    from app.providers.openai_provider import ProviderError
    ai = default_orchestrator(openai_search_calls=1, claude_search_calls=0, openai_open_web_calls=0, claude_open_web_calls=0, ai_open_web_followup_calls=0)
    ai._fail("OpenAI", ProviderError("HTTP 429: rate limited or quota exceeded"))
    ai._fail("OpenAI", ProviderError("HTTP 500: request failed"))
    ai._fail("Claude", ProviderError("web search rate limited"))
    assert (ai.status["OpenAI"]["failed_calls"], ai.status["OpenAI"]["rate_limited"]) == (2, 1) and ai.status["Claude"]["rate_limited"] == 1

    class Limited(FakeAI):
        def __init__(self):
            super().__init__(leads=[], available=True)
            self.status["OpenAI"].update(failed_calls=3, rate_limited=2, last_error="HTTP 429: rate limited or quota exceeded")

    engine = make_engine(seeds=[("greenhouse", "acme")], sources=("greenhouse",))
    engine.ai_factory = Limited
    state = run(run_search(engine, make_request()))
    warning = next(w for w in state.warnings if w.startswith("OpenAI degraded"))
    assert "3 call(s) failed" in warning and "2 of them were refused as rate-limited (HTTP 429)" in warning and "OPENAI_SEARCH_CONCURRENCY" in warning
