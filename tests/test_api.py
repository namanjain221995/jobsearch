import io
import time

import docx
import httpx
import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.main import app
from app.search_engine import SearchEngine
from tests.conftest import RESUME, FakeAI, FakeBrowser, handler


@pytest.fixture
def client():
    with TestClient(app) as c:
        mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        c.app.state.engine = SearchEngine(c.app.state.settings, Database(":memory:"), client=mock,
                                          seeds=[("greenhouse", "acme"), ("lever", "beta"), ("ashby", "gamma")],
                                          ai_factory=lambda: FakeAI(available=False), sources=["greenhouse", "lever", "ashby"],
                                          browser=FakeBrowser(available=False))
        yield c


def upload(client, name, data):
    return client.post("/api/resume/parse", files={"file": (name, data, "application/octet-stream")})


def test_frontend_and_health(client):
    page = client.get("/")
    assert page.status_code == 200 and "AI Job Finder — United States" in page.text
    assert "Find Matching USA Jobs" in page.text
    health = client.get("/api/health").json()
    assert health["status"] == "ok" and health["providers"]["openai"]["configured"] is False
    assert {x["name"] for x in health["sources"]} == {"Greenhouse", "Lever", "Ashby"} and health["browser"]["available"] is False
    assert "sk-" not in client.get("/static/app.js").text


@pytest.mark.parametrize("name,data,status", [
    ("resume.exe", b"MZ" + b"0" * 100, 400),
    ("resume.pdf", b"this is not a pdf at all, just text pretending to be one", 400),
    ("resume.docx", b"PK\x03\x04 broken zip", 400),
    ("resume.txt", b"", 400),
    ("resume.txt", b"short", 400),
    ("resume.txt", b"a" * (10 * 1024 * 1024 + 1), 413),
])
def test_scenario_i_invalid_or_oversized_resume(client, name, data, status):
    r = upload(client, name, data)
    assert r.status_code == status
    assert r.json()["error"] and "Traceback" not in r.text


def test_docx_resume_is_parsed(client):
    document = docx.Document()
    for line in RESUME.splitlines():
        document.add_paragraph(line)
    buf = io.BytesIO()
    document.save(buf)
    r = upload(client, "resume.docx", buf.getvalue())
    assert r.status_code == 200 and "PyTorch" in r.json()["detected_skills"]


def test_search_validation(client):
    rid = upload(client, "resume.txt", RESUME.encode()).json()["resume_id"]
    bad = client.post("/api/jobs/search", json={"resume_id": rid, "job_titles": "AI Engineer", "years_experience": 99})
    assert bad.status_code == 422 and bad.json()["error"] == "Invalid input"
    too_many = client.post("/api/jobs/search", json={"resume_id": rid, "job_titles": "AI Engineer", "years_experience": 3, "result_count": 501})
    assert too_many.status_code == 422
    missing = client.post("/api/jobs/search", json={"resume_id": "does-not-exist-123", "job_titles": "AI Engineer", "years_experience": 3})
    assert missing.status_code == 400
    assert client.get("/api/jobs/search/nope/status").status_code == 404


def test_end_to_end_search_results_events_and_exports(client):
    rid = upload(client, "resume.txt", RESUME.encode()).json()["resume_id"]
    started = client.post("/api/jobs/search", json={
        "resume_id": rid, "job_titles": "AI Engineer, Machine Learning Engineer", "years_experience": 3,
        "authorization": "Green Card / US Permanent Resident", "sponsorship_needed": "No",
        "exclude_citizenship_required": True, "result_count": 10, "work_mode": "All"})
    assert started.status_code == 202
    sid = started.json()["search_id"]
    for _ in range(100):
        status = client.get(f"/api/jobs/search/{sid}/status").json()
        if status["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.05)
    assert status["status"] == "completed"
    results = client.get(f"/api/jobs/search/{sid}/results").json()
    assert len(results["jobs"]) == 10 and results["stats"]["final_matches"] == 10
    first = results["jobs"][0]
    assert {"title", "company", "location", "score", "apply_url", "url", "verified_at", "authorization_status",
            "citizenship_requirement"} <= set(first)
    assert "description" not in first and all(j["citizenship_requirement"] != "Required" for j in results["jobs"])
    events = client.get(f"/api/jobs/search/{sid}/events").text
    assert "event: stages" in events and "Analyzing resume" in events and "event: done" in events
    csv_text = client.get(f"/api/jobs/search/{sid}/export.csv").text
    assert csv_text.splitlines()[0].startswith("score,title,company") and len(csv_text.splitlines()) == 11
    assert len(client.get(f"/api/jobs/search/{sid}/export.json").json()["jobs"]) == 10
    assert client.post(f"/api/jobs/search/{sid}/cancel").json()["status"] == "completed"
