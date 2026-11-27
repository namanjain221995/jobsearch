"""Cleaning posting titles into job titles, and matching them against typed text (app/job_titles.py)."""
import pytest

from app.job_titles import TitleVocabulary, clean_title, resume_titles, title_key
from app.resume_parser import heuristic_profile
from tests.conftest import RESUME


def test_titles_read_from_a_resume_never_carry_the_persons_name_or_run_across_lines():
    # The reading stored at upload runs across line breaks: its first "title" starts with the surname.
    assert heuristic_profile(RESUME).job_titles[0] == "Rivera\nMachine Learning Engineer".replace("\n", " ")
    assert resume_titles(RESUME, "Jordan Rivera") == ["Machine Learning Engineer", "Software Engineer"]
    assert resume_titles(RESUME) == ["Machine Learning Engineer", "Software Engineer"]          # the line break alone is enough
    one_line = ("Jordan Rivera Machine Learning Engineer\nSenior Data Engineer at Example Corp. Built REST APIs. Software Engineer II, Sample LLC\n"
                "Worked as QA Analyst (contract)\nSkills: Python, SQL")
    assert resume_titles(one_line, "Jordan Rivera") == ["Machine Learning Engineer", "Senior Data Engineer", "Software Engineer", "QA Analyst"]
    assert resume_titles(one_line, "Jordan Rivera", cap=2) == ["Machine Learning Engineer", "Senior Data Engineer"]
    assert resume_titles("", "Jordan Rivera") == [] and resume_titles("no titles here, only text", "") == []


@pytest.mark.parametrize("raw, expected", [
    ("Senior Software Engineer - Platform (Remote) R-10234", "Senior Software Engineer"),
    ("software engineer (remote) R-10234", "Software Engineer"),
    ("SENIOR SOFTWARE ENGINEER - REMOTE", "Senior Software Engineer"),
    ("Sr. Data Engineer, Austin, TX", "Senior Data Engineer"),
    ("AI Engineer REQ-5521", "AI Engineer"),
    ("Machine Learning Engineer @ Example Corp", "Machine Learning Engineer"),
    ("Software Engineer, Compute Infrastructure", "Software Engineer"),
    ("Technical Program Manager, Research Compute Services", "Technical Program Manager"),
    ("Director, Product Management", "Director, Product Management"),          # the part after a rank is the job
    ("Executive Assistant, Office of the CEO", "Executive Assistant"),
    ("Customer Success Manager (Remote, US)", "Customer Success Manager"),
    ("Account Executive, Mid-Market, Remote", "Account Executive"),
    ("DevOps Engineer - Remote - US", "DevOps Engineer"),
    ("IOS DEVELOPER", "iOS Developer"),
    ("QA ENGINEER", "QA Engineer"),
    ("Registered Nurse (RN) - ICU - Nights", "Registered Nurse"),
    ("Medical Lab Scientist I", "Medical Lab Scientist"),
    ("Field Support Logistician IV", "Field Support Logistician"),
    ("Quantitative Research Intern, Summer 2027", "Quantitative Research Intern"),
    ("Accounting Manager | U.S. GAAP", "Accounting Manager"),
    ("R&amp;D Manager – Control Systems", "R&D Manager"),
    ("Data Engineer [Job ID: 44717]", "Data Engineer"),
    ("Full-Stack Engineer (Frontend Leaning) - Studio", "Full-Stack Engineer"),
])
def test_a_posting_title_becomes_the_name_of_the_job(raw, expected):
    assert clean_title(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "Manager", "Remote", "Hiring now!!! Drivers $25/hr", "Nurse Practitioner or Physician Assistant - Newark, New Jersey",
    "Senior / Staff Machine Learning Engineer", "https://example.com/jobs/1", "General Application", "Join our talent community",
    "x" * 400, None, 42, "12345678", "A Very Long Title With Far Too Many Words To Be A Job Name At All",
])
def test_what_is_not_a_job_title_is_dropped(raw):
    assert clean_title(raw) is None


def test_spellings_of_one_title_are_counted_together_and_shown_the_way_most_postings_write_them():
    vocabulary = TitleVocabulary([("Senior Data Engineer", 5), ("Sr. Data Engineer - Remote", 2), ("SENIOR DATA ENGINEER (R-1001)", 1),
                                  ("Data Engineer", 9), ("Data Engineer, Platform", 3), ("data engineer", 1), ("Account Executive", 20),
                                  ("Database Administrator", 4), ("Hiring!!! Data people", 50)])
    assert [(title, count) for title, _, count in vocabulary.entries] == [
        ("Account Executive", 20), ("Data Engineer", 13), ("Senior Data Engineer", 8), ("Database Administrator", 4)]
    assert title_key("Sr. Data Engineer") == title_key("SENIOR DATA ENGINEER") == "senior data engineer"
    # Titles that begin with the typed text come first, then by how many postings carry them.
    assert vocabulary.suggest("data") == ["Data Engineer", "Database Administrator", "Senior Data Engineer"]
    assert vocabulary.suggest("eng dat") == ["Data Engineer", "Senior Data Engineer"]       # any order, each word a prefix
    assert vocabulary.suggest("sr data") == ["Senior Data Engineer"]
    assert vocabulary.suggest("data", limit=1) == ["Data Engineer"]
    assert vocabulary.suggest("data", exclude=["data engineer"]) == ["Database Administrator", "Senior Data Engineer"]
    assert vocabulary.suggest("d") == [] and vocabulary.suggest("") == [] and vocabulary.suggest("zzz") == []
    assert vocabulary.suggest("'; DROP TABLE x; --") == [] and vocabulary.suggest("%") == []


def test_the_vocabulary_keeps_the_most_frequent_titles_only_and_answers_fast():
    import time
    letters = "abcdefghijklmnopqrstuvwxyz"
    word = lambda n: "Wi" + letters[n // 676 % 26] + letters[n // 26 % 26] + letters[n % 26] + letters[n // 17576 % 26]
    counted = [(f"{word(n)} Engineer", 1 + n % 7) for n in range(30000)] + [("Data Engineer", 500)]
    vocabulary = TitleVocabulary(counted, limit=20000)
    assert len(vocabulary) == 20000 and vocabulary.entries[0][0] == "Data Engineer"
    started = time.perf_counter()
    for _ in range(50):
        assert vocabulary.suggest("eng")[0] == "Data Engineer" and len(vocabulary.suggest("wi eng")) == 8
    assert (time.perf_counter() - started) / 100 < 0.05                 # generous: a lookup is a few milliseconds
