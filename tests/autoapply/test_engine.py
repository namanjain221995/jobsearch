"""Answer engine, classifier, free-text verification, ATS detection and the dry-run guard predicates (no browser, no DB)."""
import asyncio
import random
from types import SimpleNamespace

import pytest

from app.autoapply import records
from app.autoapply.answers import Engine, Facts, decline_option, match_option
from app.autoapply.browser import is_graphql_query, submission_like
from app.autoapply.classify import CLASSES, classify_rules, classify_with_models
from app.autoapply.detect import detect, employer_key
from app.autoapply.schema import FormField
from app.autoapply.verify import check, sources_text, unsupported_terms
from app.netguard import GuardProxy
from tests.conftest import RESUME


def profile(**kw):
    base = dict(work_authorization="Other / Prefer Not to Say", sponsorship_needed="Unknown", salary_min=None, skills=["Python", "PyTorch"],
                desired_titles=["AI Engineer"], years_experience=3)
    base.update(kw)
    return SimpleNamespace(**base)


def facts(answers=None, resume=RESUME, prof=None, filename="resume.pdf", **kw):
    cand = SimpleNamespace(name="Jordan Rivera", phone="+1 555 0100", email="jordan@example.com", salesforce_id=None)
    return Facts(candidate_name=cand.name, email=cand.email, phone=cand.phone, answers_raw=answers or {}, profile=prof or profile(),
                 candidate=cand, resume_filename=filename, resume_text=resume, company="Acme", job_title="Machine Learning Engineer", **kw)


def F(key, label, type_="text", required=True, options=None):
    return FormField(key=key, label=label, type=type_, required=required, options=options or [])


class Writer:
    name = "FakeWriter"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def json_call(self, system, user, schema, name):
        self.calls += 1
        reply = self.replies.pop(0) if self.replies else self.last
        self.last = reply
        return reply(user) if callable(reply) else reply


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- classification
@pytest.mark.parametrize("label,type_,expected", [
    ("Will you now or in the future require sponsorship for employment visa status?", "select", "legal:sponsorship"),
    ("Are you legally authorized to work in the United States?", "select", "legal:authorized_us"),
    ("I have personally completed this application without the use of automation", "checkbox", "certification:personal_completion"),
    ("I agree to the Mutual Arbitration Agreement", "checkbox", "certification:arbitration"),
    ("I certify that my answers are true and correct to the best of my knowledge", "checkbox", "certification:attest"),
    ("I have read and accept the candidate privacy notice", "checkbox", "certification:data_processing"),
    ("Gender", "select", "eeo:gender"), ("Are you Hispanic/Latino?", "select", "eeo:hispanic_latino"),
    ("Veteran Status", "select", "eeo:veteran"), ("Disability Status", "radio", "eeo:disability"),
    ("Why do you want to work at Acme?", "textarea", "free_text"), ("Resume/CV", "file", "standard:resume"),
    ("Cover Letter", "file", "standard:cover_letter_file"), ("What are your salary expectations?", "text", "legal:salary"),
    ("What is your notice period?", "text", "legal:notice_period"), ("Have you previously worked for Acme?", "select", "legal:previously_employed"),
    ("Are you subject to a non-compete agreement?", "radio", "legal:restrictive_agreements"),
    ("First Name", "text", "standard:first_name"), ("LinkedIn Profile", "text", "standard:linkedin"), ("T-shirt size", "select", "unknown"),
])
def test_rules_classify_the_questions_seen_on_real_forms(label, type_, expected):
    assert classify_rules(F("k", label, type_)) == expected


def test_model_classification_is_limited_to_the_fixed_list_and_falls_back():
    class Broken:
        name = "broken"

        async def json_call(self, *a):
            raise RuntimeError("down")

    class Good:
        name = "good"

        async def json_call(self, system, user, schema, name):
            return {"items": [{"key": "a", "class": "legal:salary"}, {"key": "b", "class": "made:up"}, {"key": "zzz", "class": "free_text"}]}

    usage = {}
    got = run(classify_with_models([F("a", "Comp?"), F("b", "Odd")], [Broken(), Good()], usage))
    assert got == {"a": "legal:salary"} and usage["classify_model"] == "good"
    assert "made:up" not in CLASSES


# ---------------------------------------------------------------- legal and EEO only from records
def test_legal_questions_need_input_when_not_recorded_and_nothing_is_guessed():
    fields = [F("auth", "Are you legally authorized to work in the United States?", "select", options=["Yes", "No"]),
              F("spon", "Will you require sponsorship?", "select", options=["Yes", "No"]),
              F("reloc", "Are you willing to relocate?", "radio", options=["Yes", "No"]),
              F("prev", "Have you previously worked for Acme?", "select", options=["Yes", "No"]),
              F("sal", "Desired salary", "text"), F("city", "City", "text")]
    p = run(Engine().prepare(fields, facts()))
    assert {n["field_key"]: n["fill_in"] for n in p.needs_input} == {
        "auth": "application_answers.authorized_us", "spon": "application_answers.sponsorship_now_or_future",
        "reloc": "application_answers.willing_to_relocate", "prev": "application_answers.previously_employed_by",
        "sal": "application_answers.expected_salary", "city": "application_answers.city"}
    assert all(a.value == "" for a in p.answers if a.field_key in {"auth", "spon", "reloc", "prev", "sal", "city"})


def test_legal_answers_come_from_records_and_profile_with_their_source():
    fields = [F("auth", "Are you legally authorized to work in the United States?", "select", options=["Yes", "No"]),
              F("spon", "Will you now or in the future require visa sponsorship?", "radio", options=["Yes, I will", "No"]),
              F("prev", "Have you ever worked for Acme before?", "select", options=["Yes", "No"]),
              F("sal", "What are your salary expectations?", "text"), F("start", "Earliest start date", "date")]
    fx = facts({"willing_to_relocate": "no", "previously_employed_by": ["Globex"], "previous_employers_complete": True, "earliest_start": "2026-12-15"},
               prof=profile(work_authorization="Green Card / US Permanent Resident", sponsorship_needed="No", salary_min=120000))
    p = run(Engine().prepare(fields, fx))
    got = {a.field_key: (a.value, a.source) for a in p.answers}
    assert got == {"auth": ("Yes", "profile"), "spon": ("No", "profile"), "prev": ("No", "application_answers"),
                   "sal": ("$120,000", "profile"), "start": ("2026-12-15", "application_answers")}
    assert p.needs_input == []


def test_sponsorship_unknown_in_profile_blocks_and_worked_here_before_is_detected():
    fields = [F("spon", "Do you require sponsorship?", "select", options=["Yes", "No"]), F("prev", "Have you previously worked for Acme?", "select", options=["Yes", "No"])]
    p = run(Engine().prepare(fields, facts({"previously_employed_by": ["ACME Inc"]})))
    assert [n["field_key"] for n in p.needs_input] == ["spon"]
    assert next(a for a in p.answers if a.field_key == "prev").value == "Yes"


def test_eeo_defaults_to_the_decline_option_and_never_reads_the_name():
    options = ["Male", "Female", "Decline To Self Identify"]
    fields = [F("g", "Gender", "select", False, options), F("v", "Veteran Status", "select", True,
              ["I am not a protected veteran", "I don't wish to answer"]), F("d", "Disability status", "radio", True, ["Yes", "No"])]
    # A name that "sounds" like a gender must change nothing.
    for name in ("Maria Garcia", "John Smith", "Jordan Rivera"):
        fx = facts()
        fx.candidate.name = fx.candidate_name = name
        p = run(Engine().prepare(fields, fx))
        got = {a.field_key: (a.value, a.source) for a in p.answers}
        assert got["g"] == ("Decline To Self Identify", "eeo_decline") and got["v"] == ("I don't wish to answer", "eeo_decline")
        assert got["d"][0] == "" and [n["field_key"] for n in p.needs_input] == ["d"]       # no decline option: a human decides
    p = run(Engine().prepare(fields, facts({"eeo_gender": "Female", "eeo_veteran": "I am not a protected veteran"})))
    got = {a.field_key: (a.value, a.source) for a in p.answers}
    assert got["g"] == ("Female", "application_answers") and got["v"] == ("I am not a protected veteran", "application_answers")


def test_certifications_and_arbitration_need_a_human_unless_consent_covers_them():
    fields = [F("pc", "I have personally completed this application", "checkbox"),
              F("arb", "I agree to the arbitration agreement", "checkbox"),
              F("att", "I certify my answers are true and correct", "checkbox"),
              F("priv", "I accept the candidate privacy notice", "checkbox"),
              F("news", "Sign up to receive job alerts and marketing updates", "checkbox", False)]
    p = run(Engine().prepare(fields, facts()))
    assert sorted(b.code for b in p.blockers) == ["arbitration_agreement", "attestation_not_permitted", "data_processing_consent_not_permitted",
                                                 "personal_completion_certification"]
    assert all(a.value != "checked" for a in p.answers)
    p = run(Engine().prepare(fields, facts(may_attest=True, may_accept_data_processing=True)))
    got = {a.field_key: a.value for a in p.answers}
    assert got["att"] == got["priv"] == "checked" and got["news"] == "unchecked"
    assert sorted(b.code for b in p.blockers) == ["arbitration_agreement", "personal_completion_certification"]


def test_no_resume_file_blocks():
    p = run(Engine().prepare([F("cv", "Resume", "file")], facts(filename=None)))
    assert [b.code for b in p.blockers] == ["no_resume_file"]


def test_admin_edit_wins_but_cannot_tick_certifications():
    fields = [F("sal", "Salary expectation", "text"), F("pc", "I have personally completed this application", "checkbox")]
    p = run(Engine().prepare(fields, facts(), {"sal": "$130k", "pc": "checked"}))
    got = {a.field_key: (a.value, a.source) for a in p.answers}
    assert got["sal"] == ("$130k", "admin_edit") and got["pc"][0] == "unchecked"


def test_match_option_and_decline():
    assert match_option("yes", ["Yes", "No"]) == "Yes" and match_option("no", ["Yes, I will", "No, I won't"]) == "No, I won't"
    assert match_option("Female", ["Male", "Female"]) == "Female" and match_option("Other", ["Male", "Female"]) is None
    assert decline_option(["A", "I prefer not to say"]) == "I prefer not to say" and decline_option(["A", "B"]) is None


def test_records_effective_and_validation():
    eff = records.effective({}, profile=profile(work_authorization="US Citizen"), candidate=SimpleNamespace(name="Ann Lee Ray", phone=""))
    assert eff["authorized_us"].value == "yes" and eff["first_name"].value == "Ann Lee" and eff["last_name"].value == "Ray"
    assert eff["eeo_gender"].value == "decline" and eff["phone"].source == "unknown"
    with pytest.raises(ValueError):
        records.ApplicationAnswers(linkedin_url="linkedin.com/in/x")
    assert records.stored({"authorized_us": "maybe", "city": "Austin"}).model_dump()["city"] == "Austin"


# ---------------------------------------------------------------- free text: never invents
GOOD = {"answers": [{"key": "why", "cannot_answer": False, "text": "I build and deploy models with Python and PyTorch, which fits the Machine Learning Engineer role at Acme.",
                     "claims": [{"claim": "Python", "evidence": "Skills: Python, PyTorch"}]}]}


def test_free_text_draft_passes_with_evidence():
    w = Writer([GOOD])
    p = run(Engine(writers=[w]).prepare([F("why", "Why do you want to work at Acme?", "textarea")], facts()))
    a = p.answers[0]
    assert a.source == "ai_draft" and a.value.startswith("I build") and a.evidence == ["Skills: Python, PyTorch"] and w.calls == 1


def bad(text, evidence="Skills: Python, PyTorch"):
    return {"answers": [{"key": "why", "cannot_answer": False, "text": text, "claims": [{"claim": "x", "evidence": evidence}]}]}


@pytest.mark.parametrize("text,problem", [
    ("I have 7 years of Python experience.", "number not in the sources: 7"),
    ("I worked at Google on ranking.", "name not in the sources: Google"),
    ("I hold a PhD and build models in Python.", "term not in the sources: phd"),
    ("I am an AWS certified engineer using Snowflake daily.", "term not in the sources: snowflake"),
])
def test_free_text_with_invented_facts_is_regenerated_then_left_for_a_human(text, problem):
    w = Writer([bad(text), bad(text)])
    p = run(Engine(writers=[w]).prepare([F("why", "Why do you want to work at Acme?", "textarea")], facts()))
    assert w.calls == 2 and p.answers[0].value == "" and p.needs_input[0]["reason"] == "free_text_unverified"
    assert problem in p.answers[0].note


def test_regeneration_fixes_a_draft():
    w = Writer([bad("I have 9 years of experience."), GOOD])
    p = run(Engine(writers=[w]).prepare([F("why", "Why do you want to work at Acme?", "textarea")], facts()))
    assert w.calls == 2 and p.answers[0].source == "ai_draft"


def test_fake_evidence_is_rejected():
    problems = check("I use Python.", ["Senior Staff Engineer at Initech for 10 years"], facts(), sources_text(facts()))
    assert any(p.startswith("evidence not found") for p in problems)


SKILLS = ["Kubernetes", "Snowflake", "Terraform", "Scala", "Kafka", "Airflow", "Rust", "GraphQL", "Spark", "Azure", "Tableau"]


def test_property_question_about_a_skill_the_resume_lacks_is_never_drafted():
    rng = random.Random(7)
    base_lines = RESUME.splitlines()
    for _ in range(40):
        skill = rng.choice(SKILLS)
        resume = "\n".join(line for line in base_lines if skill.lower() not in line.lower())   # resume without that skill
        w = Writer([lambda user: {"answers": [{"key": "q", "cannot_answer": False, "text": f"I have used {skill} extensively.",
                                               "claims": [{"claim": skill, "evidence": "Python"}]}]}])
        fx = facts(resume=resume, prof=profile(skills=["Python"]))
        label = rng.choice(["Describe your experience with {s}.", "How many years have you used {s}?", "Tell us about a {s} project."]).format(s=skill)
        p = run(Engine(writers=[w]).prepare([F("q", label, "textarea")], fx))
        assert p.answers[0].value == "" and w.calls == 0, (skill, label)
        assert skill.lower() in p.answers[0].note.lower()


def test_property_random_invented_numbers_and_skills_never_pass():
    rng = random.Random(11)
    fx = facts(resume="Jordan Rivera\nPython developer, Example Corp (2023-2026): 3 years of backend work.", prof=profile(skills=["Python"]))
    src = sources_text(fx)
    for _ in range(60):
        n = rng.choice([4, 5, 6, 8, 10, 12, 15, 20])
        skill = rng.choice(SKILLS)
        text = rng.choice([f"I bring {n} years of experience.", f"I led {skill} migrations.", f"I improved latency by {n}0% with {skill}."])
        assert check(text, ["Skills: Python"], fx, src), text


def test_unsupported_terms():
    assert unsupported_terms("Describe your Kubernetes experience", "Skills: Python") == ["kubernetes"]
    assert unsupported_terms("Describe your Python experience", "Skills: Python") == []


def test_no_writer_means_needs_input():
    p = run(Engine().prepare([F("why", "Why do you want to work here?", "textarea")], facts()))
    assert p.needs_input[0]["reason"] == "free_text_unverified"


# ---------------------------------------------------------------- detection
@pytest.mark.parametrize("url,ats,form", [
    ("https://job-boards.greenhouse.io/acme/jobs/123?gh_src=x", "greenhouse", "https://job-boards.greenhouse.io/acme/jobs/123"),
    ("https://boards.greenhouse.io/embed/job_app?for=acme&token=55", "greenhouse", "https://job-boards.greenhouse.io/acme/jobs/55"),
    ("https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555", "lever", "https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555/apply"),
    ("https://jobs.ashbyhq.com/acme/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "ashby", "https://jobs.ashbyhq.com/acme/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/application"),
    ("https://apply.workable.com/acme/j/ABC123DEF/", "workable", "https://apply.workable.com/acme/j/ABC123DEF/apply/"),
])
def test_detect_supported(url, ats, form):
    t = detect(url)
    assert (t.ats, t.url) == (ats, form)


@pytest.mark.parametrize("url,code", [
    ("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/x", "account_creation_required"),
    ("https://www.linkedin.com/jobs/view/123", "unsupported_site"),
    ("https://careers.acme.com/jobs?gh_jid=123", "custom_career_site"),
    ("https://acme.com/careers", "unsupported_site"),
])
def test_detect_unsupported(url, code):
    t = detect(url)
    assert t.ats == "unsupported" and t.code == code and t.reason


def test_employer_key():
    assert employer_key("lever", "Acme", "Acme Inc") == "lever:acme" and employer_key("unsupported", "", "Acme, Inc.") == "name:acmeinc"


# ---------------------------------------------------------------- guard predicates and the proxy's dry-run mode
def test_submission_predicate():
    assert submission_like("POST", "a=b", "application/x-www-form-urlencoded")
    assert submission_like("PUT", None, "")
    assert not submission_like("GET", None, "")
    q = '{"operationName":"ApiJobPosting","query":"query ApiJobPosting { x }"}'
    m = '{"operationName":"Submit","query":"mutation Submit { x }"}'
    assert is_graphql_query(q, "application/json") and not submission_like("POST", q, "application/json")
    assert submission_like("POST", m, "application/json") and submission_like("POST", q, "multipart/form-data")
    assert submission_like("POST", '[{"query":"query A {x}"},{"query":"mutation B {y}"}]', "application/json")
    assert submission_like("POST", '{"query":"query A { x } mutation B { y }"}', "application/json")


async def test_guard_proxy_refuses_plain_http_submissions_in_dry_run():
    async def resolver(host, port):
        return ("127.0.0.1", 9)                        # never reached for a refused request
    proxy = GuardProxy(resolver, block_submissions=True)
    port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"POST http://jobs.lever.co/acme/x/apply HTTP/1.1\r\nHost: jobs.lever.co\r\nContent-Length: 3\r\n\r\nabc")
        await writer.drain()
        head = await reader.read(100)
        writer.close()
        assert head.startswith(b"HTTP/1.1 403") and proxy.blocked_submissions == 1
    finally:
        await proxy.close()
