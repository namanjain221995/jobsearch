"""The adapters in a real Chromium against the local fake ATS (tests/autoapply/fake_ats.py).

Proves: dry runs send zero submissions (counted on the server) even when the submit button is pressed; the real
submission path sends exactly one and detects the confirmation; blockers are recognised. Nothing leaves this machine:
the guard proxy's resolver only knows the fake hosts."""
import asyncio
from types import SimpleNamespace

import pytest

from app.autoapply.adapters import adapter_for
from app.autoapply.answers import Engine, Facts
from app.autoapply.browser import ApplyBrowser, screenshot
from app.autoapply.detect import detect
from tests.autoapply.fake_ats import ASHBY_ID, LEVER_ID, FakeATS
from tests.conftest import RESUME

pytest.importorskip("playwright.async_api")

RECORDED = {"authorized_us": "yes", "sponsorship_now_or_future": "no", "previously_employed_by": [], "previous_employers_complete": True,
            "linkedin_url": "https://www.linkedin.com/in/jordan-example", "country": "United States", "expected_salary": "$120,000",
            "notice_period": "2 weeks", "current_company": "Example Corp"}
GOOD = {"answers": [{"key": "question_2", "cannot_answer": False,
                     "text": "I build and deploy models with Python and PyTorch, which fits the Machine Learning Engineer role at Acme.",
                     "claims": [{"claim": "Python", "evidence": "Skills: Python, PyTorch"}]}]}


class Writer:
    name = "FakeWriter"

    async def json_call(self, system, user, schema, name):
        return GOOD


def facts(**kw):
    cand = SimpleNamespace(name="Jordan Rivera", phone="+1 555 0100", email="jordan.rivera@example.com", salesforce_id=None)
    prof = SimpleNamespace(work_authorization="Green Card / US Permanent Resident", sponsorship_needed="No", salary_min=None,
                           skills=["Python", "PyTorch"], desired_titles=["AI Engineer"], years_experience=3)
    return Facts(candidate_name=cand.name, email=cand.email, phone=cand.phone, answers_raw=dict(RECORDED), profile=prof, candidate=cand,
                 resume_filename="resume.txt", resume_text=RESUME, company="Acme", job_title="Machine Learning Engineer", **kw)


@pytest.fixture(scope="module")
def fake():
    with FakeATS() as server:
        yield server


@pytest.fixture
def browser(fake):
    b = ApplyBrowser(resolver=fake.resolver, host_check=fake.host_check, ignore_https_errors=True)

    async def probe():
        try:
            async with b.session(dry_run=True):
                return True
        except Exception as exc:
            return exc
    ok = asyncio.run(probe())
    if ok is not True:
        pytest.skip(f"Chromium cannot start here: {type(ok).__name__}")
    return b


URLS = {
    "greenhouse": (0, "/acme/jobs/1001"),
    "lever": (1, f"/acme/{LEVER_ID}/apply"),
    "ashby": (2, f"/acme/{ASHBY_ID}/application"),
    "workable": (3, "/acme/j/ABC123/apply/"),
}
FILES = ("resume.txt", "text/plain", RESUME.encode())


async def prepare(browser, url, *, dry_run=True, press_submit=False, facts_=None):
    target = detect(url)
    adapter = adapter_for(target.ats)
    async with browser.session(dry_run=dry_run) as (page, guard, proxy):
        await adapter.open(page, target.url)
        guard.armed = True
        schema = await adapter.inspect(page, target.url)
        prepared = await Engine(writers=[Writer()]).prepare(schema.fields, facts_ or facts(may_accept_data_processing=True))
        answers = [a.as_dict() for a in prepared.answers]
        report = await adapter.fill(page, schema, answers, {a["field_key"]: FILES for a in answers if a["source"] == "resume_file"})
        blockers = await adapter.detect_blockers(page)
        shot = await screenshot(page, 600_000)
        result = evidence = None
        if press_submit:
            result = await adapter.submit(page, guard)
            evidence = await adapter.confirm(page)
        return SimpleNamespace(schema=schema, prepared=prepared, report=report, blockers=blockers, guard=guard.report(proxy), shot=shot,
                               result=result, evidence=evidence)


@pytest.mark.parametrize("ats", sorted(URLS))
def test_dry_run_fills_every_answer_and_sends_nothing(ats, fake, browser):
    index, path = URLS[ats]
    before = len(fake.submissions())
    out = asyncio.run(prepare(browser, fake.url(index, path)))
    assert out.schema.blockers == [] and out.blockers == []
    assert out.report and all(e["filled"] for e in out.report), out.report
    assert out.prepared.needs_input == [] and out.prepared.blockers == [], out.prepared.needs_input
    assert out.guard["allowed_submissions"] == 0
    assert len(fake.submissions()) == before                                   # the server saw no submission
    assert out.shot and out.shot[:2] == b"\xff\xd8" and len(out.shot) <= 600_000
    sources = {a.field_key: a.source for a in out.prepared.answers if a.value}
    assert "resume_file" in sources.values()


@pytest.mark.parametrize("ats", sorted(URLS))
def test_dry_run_blocks_the_submit_button_too(ats, fake, browser):
    index, path = URLS[ats]
    before = len(fake.submissions())
    out = asyncio.run(prepare(browser, fake.url(index, path), press_submit=True))
    assert len(fake.submissions()) == before, fake.submissions()[before:]
    assert out.guard["allowed_submissions"] == 0 and out.guard["blocked_requests"] >= 1
    assert out.evidence["detected"] is False


@pytest.mark.parametrize("ats", sorted(URLS))
def test_submission_sends_once_and_detects_the_confirmation(ats, fake, browser):
    index, path = URLS[ats]
    before = len(fake.submissions())
    out = asyncio.run(prepare(browser, fake.url(index, path), dry_run=False, press_submit=True))
    sent = fake.submissions()[before:]
    assert len(sent) == 1, sent
    assert out.result["clicked"] and out.result["requests"] >= 1
    assert out.evidence["detected"] and len(out.evidence["text_sha256"]) == 64
    if ats == "greenhouse":
        assert out.evidence["reference"] == "GH-4711"


@pytest.mark.parametrize("index,path,code", [
    (0, "/captcha/jobs/2001", "captcha_visible"),
    (0, "/signin/jobs/2002", "sign_in_required"),
    (0, "/nojs/jobs/2003", "posting_closed"),
    (2, f"/puzzle/{ASHBY_ID}/application", "anti_bot_puzzle"),
])
def test_blockers_are_detected(index, path, code, fake, browser):
    async def go():
        target = detect(fake.url(index, path))
        adapter = adapter_for(target.ats)
        async with browser.session(dry_run=True) as (page, guard, proxy):
            await adapter.open(page, target.url)
            return await adapter.inspect(page, target.url)
    schema = asyncio.run(go())
    assert code in [b.code for b in schema.blockers]


def test_invisible_recaptcha_is_not_a_blocker(fake, browser):
    out = asyncio.run(prepare(browser, fake.url(0, "/acme/jobs/1001")))
    assert not [b for b in out.schema.blockers + out.blockers if b.code == "captcha_visible"]


@pytest.mark.parametrize("index,path,code", [
    (2, f"/certify/{ASHBY_ID}/application", "personal_completion_certification"),
    (1, f"/arb/{LEVER_ID}/apply", "arbitration_agreement"),
])
def test_certification_and_arbitration_checkboxes_block(index, path, code, fake, browser):
    out = asyncio.run(prepare(browser, fake.url(index, path)))
    assert code in [b.code for b in out.prepared.blockers]


def test_unknown_required_field_needs_input(fake, browser):
    out = asyncio.run(prepare(browser, fake.url(3, "/unknown/j/UNK999/apply/")))
    assert [(n["label"], n["reason"]) for n in out.prepared.needs_input] == [("T-shirt size", "required_unknown_field")]
