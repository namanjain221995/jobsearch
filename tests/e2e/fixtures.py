"""Synthetic employer boards for the E2E / demo stack. Nothing here is a real posting."""
import asyncio

import httpx

from tests.conftest import JSONLD
from tests.conftest import handler as base_handler

_ROLES = [
    ("Machine Learning Engineer", "ML"), ("AI Engineer", "AI"), ("Senior AI Engineer", "AI"), ("Applied AI Engineer", "AI"),
    ("Machine Learning Engineer, Platform", "ML"), ("Data Scientist", "DS"), ("Backend Engineer (Python)", "BE"),
    ("LLM Engineer", "AI"), ("Software Engineer, Machine Learning", "ML"), ("Account Executive", "SALES"),
]
_PLACES = [("New York, NY", "Hybrid"), ("Seattle, WA", "On-Site"), ("Remote - US", "Remote"), ("Austin, TX", "Hybrid"),
           ("Boston, MA", "On-Site"), ("San Francisco, CA", "Hybrid"), ("Denver, CO", "Remote"), ("Toronto, Canada", "Hybrid")]
_COMPANIES = {"globex": "Globex Robotics", "initech": "Initech Health", "hooli": "Hooli Cloud", "umbrella": "Umbrella Analytics"}


def _content(company: str, title: str, years: int, extra: str = "") -> str:
    return (f"<h2>About {company}</h2><p>{company} builds products used by millions of people.</p>"
            f"<h2>Responsibilities</h2><ul><li>Design, train and ship machine learning models in Python and PyTorch</li>"
            f"<li>Build reliable APIs with FastAPI on AWS and Kubernetes</li><li>Work with product teams on LLM and RAG features</li></ul>"
            f"<h2>Required Qualifications</h2><ul><li>{years}+ years of experience in a similar {title} role</li>"
            f"<li>Strong Python, SQL and Docker skills</li></ul>"
            f"<h2>Preferred Qualifications</h2><ul><li>Experience with TensorFlow, Spark or Airflow</li></ul>"
            f"<h2>Compensation</h2><p>The base salary range is ${110 + years * 15},000 - ${150 + years * 20},000 per year.</p>"
            f"<h2>Benefits</h2><p>Health insurance, 401(k) match and flexible time off.</p>{extra}")


def _board(slug: str) -> dict:
    company = _COMPANIES[slug]
    jobs = []
    for i, (title, _) in enumerate(_ROLES):
        location, kind = _PLACES[(i + len(slug)) % len(_PLACES)]
        years = 2 + (i + len(slug)) % 5
        extra = ""
        if i == 3:
            extra = "<h2>Work Authorization</h2><p>We are unable to sponsor visas for this role.</p>"
        if i == 7 and slug == "globex":
            extra = "<h2>Work Authorization</h2><p>Due to contract terms you must be a U.S. citizen.</p>"
        jobs.append({"id": 9000 + len(jobs) + 100 * list(_COMPANIES).index(slug), "title": title,
                     "absolute_url": f"https://job-boards.greenhouse.io/{slug}/jobs/{9000 + i + 100 * list(_COMPANIES).index(slug)}",
                     "location": {"name": location}, "content": _content(company, title, years, extra), "company_name": company,
                     "first_published": f"2026-11-{1 + (i * 2) % 15:02d}T10:00:00-05:00", "offices": [],
                     "metadata": [{"name": "Location Type", "value": kind}]})
    return {"jobs": jobs}


BOARDS = {f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs": _board(slug) for slug in _COMPANIES}
SEEDS = [("greenhouse", "acme"), ("lever", "beta"), *[("greenhouse", slug) for slug in _COMPANIES]]


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url).split("?")[0]
    if url in BOARDS:
        return httpx.Response(200, json=BOARDS[url])
    return base_handler(request)


# ---------------------------------------------------------------- a search engine, and a platform without a reader
# Only in a stack started with `--search-engine` (tests/e2e/server.py). The synthetic search engine knows two things:
# an employer board the index already has (so it adds nothing), and one posting on a JazzHR career page, a platform
# the portal has no board reader for. That posting is found only by a search limited to platforms that include JazzHR,
# because applytojob.com is left out of the hosts the demo stack asks the search engine about by default.
# The two answers about JazzHR are slow on purpose: the real stack finishes a search in well under a second, and a
# search that is still running has to stay on screen long enough to be looked at.
SEARCH_ENGINE = "http://search-engine.e2e.invalid"
JAZZHR_HOST = "applytojob.com"
JAZZHR_POSTING = "https://juniper.applytojob.com/apply/Qm7Xw2Lp9Z/Machine-Learning-Engineer"
SLOW_SECONDS = 2.5
KNOWN_BOARD = "https://boards.greenhouse.io/acme"
# Hosts of the synthetic postings. The demo stack treats them as public without asking DNS (see server.demo_resolver).
POSTING_HOSTS = ("job-boards.greenhouse.io", "boards.greenhouse.io", "jobs.lever.co", "juniper.applytojob.com")
_JAZZHR_PAGE = ("<html><head><title>Machine Learning Engineer</title>" + JSONLD % ("Machine Learning Engineer", "Juniper Analytics")
                + "</head><body><h1>Machine Learning Engineer</h1><p>Apply now</p></body></html>")


async def search_engine_handler(request: httpx.Request) -> httpx.Response:
    """`handler` plus the synthetic search engine and the JazzHR posting page."""
    url = str(request.url).split("?")[0]
    if url == SEARCH_ENGINE + "/search":
        site = request.url.params.get("q", "").split(" ", 1)[0].removeprefix("site:")
        found = []
        if request.url.params.get("pageno", "1") == "1":
            if site == JAZZHR_HOST:
                await asyncio.sleep(SLOW_SECONDS)
                found = [JAZZHR_POSTING]
            elif site == "boards.greenhouse.io":
                found = [KNOWN_BOARD]
        return httpx.Response(200, json={"results": [{"url": u, "title": "", "content": "", "engines": ["demo"]} for u in found],
                                         "unresponsive_engines": []})
    if url == JAZZHR_POSTING:
        await asyncio.sleep(SLOW_SECONDS)
        return httpx.Response(200, text=_JAZZHR_PAGE, headers={"content-type": "text/html"})
    return handler(request)
