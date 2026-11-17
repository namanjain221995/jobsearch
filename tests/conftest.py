"""Shared fixtures. All job data here is synthetic and used only by tests."""
import collections
import json
import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["OPENAI_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["PREWARM_ON_START"] = "false"
os.environ["BROWSER_ENABLED"] = "false"

import httpx
import pytest

from app.browser_pool import RenderResult
from app.config import Settings
from app.database import Database
from app.schemas import SearchRequest
from app.search_engine import SearchEngine, SearchState

RESUME = """Jordan Rivera
Machine Learning Engineer
Summary: Python / ML engineer with 3 years of experience building and deploying models.
Skills: Python, PyTorch, TensorFlow, scikit-learn, SQL, Docker, Kubernetes, AWS, FastAPI, LLM, RAG, NLP
Experience
Machine Learning Engineer, Example Corp (2023-2026): trained and deployed deep learning models, built REST APIs.
Software Engineer, Sample LLC (2022-2023): backend services in Python and PostgreSQL.
Education: B.S. Computer Science
"""

DESC = "<p>Build ML systems with Python and PyTorch on AWS. 3+ years of experience required. Kubernetes a plus.</p>"
CALLS: collections.Counter = collections.Counter()      # request counts per URL (without query string)


def gh_job(job_id, title, location, content=DESC, board="acme"):
    return {"id": job_id, "title": title, "absolute_url": f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}",
            "location": {"name": location}, "content": content, "company_name": "Acme AI",
            "first_published": "2026-11-01T10:00:00-04:00", "offices": [], "metadata": []}


GREENHOUSE_ACME = {"jobs": [
    gh_job(1001, "AI Engineer", "San Francisco, CA"),
    gh_job(1002, "AI Engineer", "Toronto, Canada"),
    gh_job(1003, "Machine Learning Engineer", "Remote - US",
           DESC + "<p>Due to federal contract requirements, you must be a U.S. citizen.</p>"),
    gh_job(1004, "Senior AI Engineer", "New York, NY",
           DESC + "<p>This role requires access to export-controlled data; you must be a U.S. person.</p>"),
    gh_job(1005, "Account Executive", "New York, NY"),
    gh_job(1006, "AI Engineer", "London, UK"),
    gh_job(1007, "AI Engineer", "Remote"),
    *[gh_job(2000 + i, f"AI Engineer, Platform Team {i}", "Austin, TX") for i in range(1, 16)],
]}
LEVER_BETA = [
    {"id": "11111111-1111-4111-8111-111111111111", "text": "Machine Learning Engineer",
     "hostedUrl": "https://jobs.lever.co/beta/11111111-1111-4111-8111-111111111111",
     "applyUrl": "https://jobs.lever.co/beta/11111111-1111-4111-8111-111111111111/apply",
     "categories": {"location": "Seattle, WA", "allLocations": ["Seattle, WA"]}, "country": "US",
     "workplaceType": "hybrid", "createdAt": 1793000000000,
     "descriptionPlain": "Train models with Python and TensorFlow. 2+ years of experience. We are unable to sponsor visas."},
    {"id": "22222222-2222-4222-8222-222222222222", "text": "AI Engineer",
     "hostedUrl": "https://jobs.lever.co/beta/22222222-2222-4222-8222-222222222222",
     "applyUrl": "https://jobs.lever.co/beta/22222222-2222-4222-8222-222222222222/apply",
     "categories": {"location": "Berlin", "allLocations": ["Berlin"]}, "country": "DE",
     "workplaceType": "on-site", "createdAt": 1793000000000, "descriptionPlain": "Python and PyTorch."},
]

# ---------------------------------------------------------------- Workday (tenant "wdco")
WD_US_FACET = {"facetParameter": "locationMainGroup", "values": [
    {"facetParameter": "locationHierarchy1", "descriptor": "Locations", "values": [
        {"descriptor": "India", "id": "in-1", "count": 1}, {"descriptor": "United States", "id": "us-1", "count": 4}]}]}
WD_POSTINGS = [
    {"title": "AI Engineer", "externalPath": "/job/US-TX-Austin/AI-Engineer_R100", "locationsText": "US, TX, Austin", "bulletFields": ["R100"]},
    {"title": "Machine Learning Engineer", "externalPath": "/job/2-Locations/ML-Engineer_R101", "locationsText": "2 Locations", "bulletFields": ["R101"]},
    {"title": "AI Engineer", "externalPath": "/job/US-CA/AI-Engineer-Closed_R102", "locationsText": "US, CA, San Jose", "bulletFields": ["R102"]},
    {"title": "Senior AI Engineer", "externalPath": "/job/US-VA/Sr-AI-Engineer_R103", "locationsText": "US, VA, Reston", "bulletFields": ["R103"]},
]
WD_INDIA = {"title": "AI Engineer", "externalPath": "/job/India/AI-Engineer_R200", "locationsText": "India, Bangalore", "bulletFields": ["R200"]}


def wd_detail(title, location, description, extra=None):
    info = {"title": title, "location": location, "jobDescription": description, "startDate": "2026-11-03",
            "country": {"descriptor": "United States of America", "id": "x"}, "canApply": True, "posted": True,
            "externalUrl": "https://wdco.wd5.myworkdayjobs.com/Careers" + (extra or {}).pop("path", "/job/x")}
    info.update(extra or {})
    return {"jobPostingInfo": info}


WD_DETAILS = {
    "/job/US-TX-Austin/AI-Engineer_R100": wd_detail("AI Engineer", "US, TX, Austin", DESC, {"path": "/job/US-TX-Austin/AI-Engineer_R100"}),
    "/job/2-Locations/ML-Engineer_R101": wd_detail("Machine Learning Engineer", "US, WA, Seattle", DESC,
                                                    {"path": "/job/2-Locations/ML-Engineer_R101", "additionalLocations": ["US, NY, New York"]}),
    "/job/US-VA/Sr-AI-Engineer_R103": wd_detail("Senior AI Engineer", "US, VA, Reston",
                                                 DESC + "<p>An active TS/SCI clearance is required.</p>", {"path": "/job/US-VA/Sr-AI-Engineer_R103"}),
}

# ---------------------------------------------------------------- other API sources
WORKABLE_PAGE1 = {"totalSize": 3, "nextPageToken": "tok2", "jobs": [
    {"id": "wk-1", "title": "AI Engineer", "state": "published", "description": DESC, "requirementsSection": "", "benefitsSection": "",
     "url": "https://jobs.workable.com/view/abc/ai-engineer-at-gizmo", "locations": ["Denver, Colorado, United States"],
     "location": {"city": "Denver", "subregion": "Colorado", "countryName": "United States"}, "created": "2026-11-10T00:00:00Z",
     "company": {"title": "Gizmo Labs"}, "workplace": "hybrid"},
    {"id": "wk-2", "title": "AI Engineer", "state": "published", "description": DESC, "url": "https://jobs.workable.com/view/def/ai-engineer-at-euro",
     "locations": ["Paris, France"], "location": {"city": "Paris", "countryName": "France"}, "created": "2026-11-10T00:00:00Z",
     "company": {"title": "Euro Co"}, "workplace": "on_site"}]}
WORKABLE_PAGE2 = {"totalSize": 3, "jobs": [
    {"id": "wk-3", "title": "Machine Learning Engineer", "state": "published", "description": DESC,
     "url": "https://jobs.workable.com/view/ghi/ml-engineer-at-widget", "locations": ["Remote, United States"],
     "location": {"countryName": "United States"}, "created": "2026-11-09T00:00:00Z", "company": {"title": "Widget Works"}, "workplace": "remote"}]}
AMAZON = {"hits": 1, "jobs": [
    {"title": "Machine Learning Engineer", "job_path": "/en/jobs/900/machine-learning-engineer", "id_icims": "900", "description": "Build ML with Python.",
     "basic_qualifications": "3+ years of experience with PyTorch", "preferred_qualifications": "AWS", "normalized_location": "Seattle, Washington, USA",
     "country_code": "USA", "posted_date": "November  6, 2026", "url_next_step": "https://account.amazon.jobs/jobs/900/apply", "company_name": "Amazon.com Services LLC"}]}
REMOTEOK = [
    {"legal": "API Terms of Service: link back and mention Remote OK."},
    {"id": "77", "position": "AI Engineer", "company": "Acme AI", "location": "United States", "description": "Reformatted copy. " + DESC,
     "url": "https://remoteok.com/remote-jobs/77", "apply_url": "https://remoteok.com/remote-jobs/77", "date": "2026-11-02T00:00:00+00:00",
     "salary_min": 150000, "salary_max": 190000},
    {"id": "78", "position": "Machine Learning Engineer", "company": "Globex", "location": "Worldwide", "description": DESC,
     "url": "https://remoteok.com/remote-jobs/78", "date": "2026-11-02T00:00:00+00:00", "salary_min": 0, "salary_max": 0},
    {"id": "79", "position": "ML Engineer", "company": "Initech", "location": "USA only", "description": DESC,
     "url": "https://remoteok.com/remote-jobs/79", "date": "2026-11-04T00:00:00+00:00"},
]
JOBICY = {"jobs": [
    {"id": 501, "url": "https://jobicy.com/jobs/501-ai-engineer", "jobTitle": "AI Engineer", "companyName": "Hooli", "jobGeo": "USA",
     "jobDescription": DESC, "pubDate": "2026-11-12T10:00:00+00:00", "salaryMin": 140000, "salaryMax": 180000, "salaryCurrency": "USD", "salaryPeriod": "yearly"}]}
HIMALAYAS = {"jobs": [
    {"title": "AI Engineer", "companyName": "Pied Piper", "description": DESC, "locationRestrictions": ["United States", "Canada"],
     "pubDate": 1794441600, "expiryDate": 1894441600, "applicationLink": "https://himalayas.app/companies/pied-piper/jobs/ai-engineer?utm_source=api",
     "guid": "https://himalayas.app/companies/pied-piper/jobs/ai-engineer", "minSalary": 0, "maxSalary": 0},
    {"title": "AI Engineer", "companyName": "Old Co", "description": DESC, "locationRestrictions": ["United States"], "pubDate": 1700000000,
     "expiryDate": 1700000100, "applicationLink": "https://himalayas.app/x", "guid": "https://himalayas.app/companies/old-co/jobs/ai-engineer"}]}
ADZUNA = {"results": [
    {"id": "az1", "title": "<strong>AI</strong> <strong>Engineer</strong>", "description": "Snippet about Python and PyTorch...", "redirect_url": "https://www.adzuna.com/land/ad/az1",
     "company": {"display_name": "Umbrella Corp"}, "location": {"display_name": "Chicago, Illinois", "area": ["US", "Illinois", "Chicago"]},
     "created": "2026-11-11T08:00:00Z", "salary_min": 120000, "salary_max": 120000, "salary_is_predicted": "1"}]}

# ---------------------------------------------------------------- web pages
JSONLD = ('<script type="application/ld+json">{"@type":"JobPosting","title":"%s","hiringOrganization":{"name":"%s"},'
          '"datePosted":"2026-11-05","jobLocation":{"address":{"addressLocality":"Denver","addressRegion":"CO","addressCountry":"US"}},'
          '"description":"Python and PyTorch. 3+ years of experience."}</script>')
PAGES = {
    "https://careers.example.com/jobs/ml-1": (200, "<html><head><title>ML Engineer</title>" + JSONLD % ("Machine Learning Engineer", "Example Careers Inc") + "</head><body>Apply now</body></html>"),
    "https://careers.example.com/jobs/closed": (200, "<html><title>AI Engineer</title><body>This position has been filled.</body></html>"),
    "https://careers.example.com/jobs/gone": (404, "not found"),
    "https://careers.example.com/jobs/js-1": (200, "<html><head><title>Careers</title></head><body><div id='app'></div></body></html>"),
    "https://careers.example.com/jobs/blocked-1": (200, "<html><head><title>Careers</title></head><body><div id='app'></div></body></html>"),
    "https://careers.example.com/jobs/ai-9": (200, "<html><head><title>AI Engineer</title>" + JSONLD % ("AI Engineer - Platform", "Example Careers Inc") + "</head><body>Apply</body></html>"),
    "https://careers.example.com/openings": (200, "<html><head><title>Open roles</title><meta property='og:site_name' content='Example Careers Inc'></head><body>"
                                                   "<a href='/jobs/ml-1'>Machine Learning Engineer <span>Denver</span></a> <a href='/jobs/ai-9'>AI Engineer - Platform</a> "
                                                   "<a href='/about'>About us</a> <a href='/jobs/sales-1'>Account Executive</a></body></html>"),
    "https://careers.example.com/jobs/plain-1": (200, "<html><head><title>Join us</title></head><body><h1>Open role</h1><p>AI Engineer</p><p>Plain Example Co</p>"
                                                       "<p>Boston, MA</p><p>Python and PyTorch. 3+ years of experience.</p><button>Apply now</button></body></html>"),
    "https://forbidden.example.com/jobs/1": (200, "<html><title>AI Engineer</title>" + JSONLD % ("AI Engineer", "Forbidden Inc") + "</html>"),
}
RENDERED = {
    "https://careers.example.com/jobs/js-1": RenderResult(200, "https://careers.example.com/jobs/js-1",
                                                           "<html><head><title>AI Engineer</title>" + JSONLD % ("AI Engineer", "JS Example Inc") + "</head><body>Apply</body></html>"),
    "https://careers.example.com/jobs/blocked-1": RenderResult(403, "https://careers.example.com/jobs/blocked-1", "", "blocked"),
}
ROBOTS = {"https://forbidden.example.com": "User-agent: *\nDisallow: /jobs/\n",
          "https://blockedco.wd1.myworkdayjobs.com": "User-agent: *\nDisallow: /wday/\n"}


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url).split("?")[0]
    origin = f"{request.url.scheme}://{request.url.host}"
    CALLS[url] += 1
    if url.endswith("/robots.txt"):
        return httpx.Response(200, text=ROBOTS[origin]) if origin in ROBOTS else httpx.Response(404)
    if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs":
        return httpx.Response(200, json=GREENHOUSE_ACME)
    if url == "https://api.lever.co/v0/postings/beta":
        return httpx.Response(200, json=LEVER_BETA)
    if url == "https://api.ashbyhq.com/posting-api/job-board/gamma":
        return httpx.Response(500, text="upstream error")
    if url == "https://wdco.wd5.myworkdayjobs.com/wday/cxs/wdco/Careers/jobs":
        body = json.loads(request.content)
        if body.get("appliedFacets") == {"locationHierarchy1": ["us-1"]}:
            page = WD_POSTINGS[body["offset"]: body["offset"] + body["limit"]]
            return httpx.Response(200, json={"total": len(WD_POSTINGS), "jobPostings": page, "facets": [WD_US_FACET]})
        return httpx.Response(200, json={"total": 5, "jobPostings": [WD_INDIA, *WD_POSTINGS], "facets": [WD_US_FACET]})
    if url.startswith("https://wdco.wd5.myworkdayjobs.com/wday/cxs/wdco/Careers/job/"):
        path = url.split("/wday/cxs/wdco/Careers")[1]
        return httpx.Response(200, json=WD_DETAILS[path]) if path in WD_DETAILS else httpx.Response(404, json={})
    if url == "https://jobs.workable.com/api/v1/jobs":
        return httpx.Response(200, json=WORKABLE_PAGE2 if request.url.params.get("pageToken") == "tok2" else WORKABLE_PAGE1)
    if url == "https://www.amazon.jobs/en/search.json":
        return httpx.Response(200, json=AMAZON)
    if url == "https://remoteok.com/api":
        return httpx.Response(200, json=REMOTEOK)
    if url == "https://jobicy.com/api/v2/remote-jobs":
        return httpx.Response(200, json=JOBICY)
    if url == "https://himalayas.app/jobs/api/search":
        return httpx.Response(200, json=HIMALAYAS)
    if url.startswith("https://api.adzuna.com/v1/api/jobs/us/search/"):
        return httpx.Response(200, json=ADZUNA)
    if url in PAGES:
        status, body = PAGES[url]
        return httpx.Response(status, text=body, headers={"content-type": "text/html"})
    return httpx.Response(404, json={})


class FakeBrowser:
    """Stand-in for the Playwright pool: returns canned rendered pages."""
    def __init__(self, pages=None, available=True):
        self.pages = RENDERED if pages is None else pages
        self.available = available
        self.unavailable_reason = "" if available else "disabled in tests"
        self.concurrency = 2
        self.rendered: list[str] = []
        self._browser = None

    async def start(self):
        return self.available

    async def render(self, url):
        self.rendered.append(url)
        return self.pages.get(url) or RenderResult(200, url, PAGES.get(url, (200, ""))[1])

    async def aclose(self):
        return None


class FakeAI:
    """Stand-in orchestrator: no network. Returns configured leads; ranking and audit return nothing."""
    def __init__(self, leads=None, available=True, extractions=None):
        self.leads = leads or []
        self.available = available
        self.extractions = extractions or {}
        self.discover_calls = 0
        self.status = {"OpenAI": {"configured": available, "ok_calls": 0, "failed_calls": 0, "web_searches": 0, "last_error": "", "model": "fake"},
                       "Claude": {"configured": available, "ok_calls": 0, "failed_calls": 0, "web_searches": 0, "last_error": "", "model": "fake"}}

    async def extract_profile(self, resume_text, req, base):
        return base

    async def discover(self, profile, req, related, known):
        self.discover_calls += 1
        return list(self.leads)

    async def rank_batch(self, profile, req, jobs):
        return {}

    async def audit(self, req, jobs):
        return {}

    async def extract_pages(self, pages):
        return {pid: data for pid, text in pages for marker, data in self.extractions.items() if marker in text}


@pytest.fixture
def settings():
    return Settings(_env_file=None, openai_api_key="", anthropic_api_key="", database_url="sqlite:///:memory:",
                    prewarm_on_start=False, browser_enabled=False, fetch_deadline_seconds=10)


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    async def ok(url):
        return None
    monkeypatch.setattr("app.job_verifier.assert_public_url", ok)
    CALLS.clear()
    from app.providers import workday
    workday._us_facet.clear()


DEFAULT_SEEDS = [("greenhouse", "acme"), ("lever", "beta"), ("ashby", "gamma")]


@pytest.fixture
def make_engine(settings):
    def factory(leads=None, ai_available=False, seeds=None, sources=("greenhouse", "lever", "ashby"), browser=None,
                extractions=None, db=None):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        fake = FakeAI(leads, ai_available, extractions)
        engine = SearchEngine(settings, db or Database(":memory:"), client=client, seeds=DEFAULT_SEEDS if seeds is None else seeds,
                              ai_factory=lambda: fake, sources=list(sources), browser=browser or FakeBrowser(available=False))
        engine.fake_ai = fake
        engine.limiter.intervals = {}          # no request pacing against the mock transport
        return engine
    return factory


def make_request(**overrides) -> SearchRequest:
    base = dict(resume_id="resume-test-0001", job_titles="AI Engineer", years_experience=3,
                authorization="Green Card / US Permanent Resident", sponsorship_needed="No",
                exclude_citizenship_required=True, result_count=50, work_mode="All")
    base.update(overrides)
    return SearchRequest(**base)


async def run_search(engine, req, resume=RESUME, close=True) -> SearchState:
    state = SearchState("test-search", req.result_count)
    await engine.run(state, req, resume)
    if close:
        await engine.aclose()
    return state
