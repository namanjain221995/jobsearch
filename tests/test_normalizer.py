import pytest

from app.job_normalizer import (analyze_authorization, canonical_url, extract_experience, extract_salary,
                                html_to_text, parse_ats, us_location_status)


def test_canonical_url_strips_tracking_and_apply():
    a = canonical_url("https://www.Jobs.Lever.co/beta/11111111-1111-4111-8111-111111111111/apply?utm_source=x&lever-source=LinkedIn#top")
    b = canonical_url("https://jobs.lever.co/beta/11111111-1111-4111-8111-111111111111")
    assert a == b
    assert canonical_url("javascript:alert(1)") == ""
    assert "gh_jid=123" in canonical_url("https://acme.com/careers?gh_jid=123&gh_src=abc")


@pytest.mark.parametrize("url,expected", [
    ("https://boards.greenhouse.io/acme/jobs/1001?gh_src=x", ("greenhouse", "acme", "1001")),
    ("https://job-boards.greenhouse.io/acme/jobs/1001", ("greenhouse", "acme", "1001")),
    ("https://acme.com/careers/job?gh_jid=1001", ("greenhouse", "", "1001")),
    ("https://jobs.lever.co/beta/11111111-1111-4111-8111-111111111111/apply", ("lever", "beta", "11111111-1111-4111-8111-111111111111")),
    ("https://jobs.ashbyhq.com/gamma/33333333-3333-4333-8333-333333333333/application", ("ashby", "gamma", "33333333-3333-4333-8333-333333333333")),
    ("https://jobs.ashbyhq.com/gamma", ("ashby", "gamma", "")),
])
def test_parse_ats(url, expected):
    assert parse_ats(url) == expected


def test_parse_ats_non_ats():
    assert parse_ats("https://example.com/jobs/1") is None


@pytest.mark.parametrize("location,hint,expected", [
    ("San Francisco, CA", "", True), ("New York, NY; London, UK", "", True), ("Remote - US", "", True),
    ("United States", "", True), ("Seattle, WA", "US", True), ("Eugene, OR", "", True), ("Albuquerque, New Mexico", "", True),
    ("Toronto, Canada", "", False), ("London, UK", "", False), ("Berlin", "DE", False), ("Bengaluru, IN", "", False),
    ("Remote - Europe", "", False), ("Remote", "", False), ("", "", False), ("Remote - Canada", "United States", False),
    ("Mexico City", "", False),
])
def test_us_location(location, hint, expected):
    assert us_location_status(location, hint)[0] is expected


def test_remote_needs_confirmed_us_eligibility():
    assert us_location_status("Remote", "", "This role is remote within the United States.")[0] is True
    assert us_location_status("Remote", "", "Work from anywhere in the world.")[0] is False


@pytest.mark.parametrize("text,expected", [
    ("Applicants must be a U.S. citizen.", "Required"),
    ("US citizenship is required for this position.", "Required"),
    ("Open to US citizens only.", "Required"),
    ("Requires U.S. citizenship due to contract terms.", "Required"),
    ("You must be a U.S. person (citizen or lawful permanent resident).", "Not required"),
    ("Must be a US citizen or permanent resident.", "Not required"),
    ("Must be a U.S. citizen or green card holder.", "Not required"),
    ("We value people from all backgrounds.", "Not stated"),
    ("Experience with citizen science data.", "Not stated"),
])
def test_citizenship_detection(text, expected):
    assert analyze_authorization(text)["citizenship_requirement"] == expected


@pytest.mark.parametrize("text,expected", [
    ("We are unable to sponsor visas for this role.", "Not available"),
    ("No visa sponsorship is available.", "Not available"),
    ("Must be authorized to work in the US without sponsorship.", "Not available"),
    ("Visa sponsorship is available for this position.", "Available"),
    ("We will sponsor H-1B visas.", "Available"),
    ("Great benefits.", "Not stated"),
])
def test_sponsorship_detection(text, expected):
    assert analyze_authorization(text)["sponsorship"] == expected


def test_clearance_detection():
    assert analyze_authorization("Active TS/SCI clearance required.")["clearance_required"] is True
    assert analyze_authorization("Clear communication skills.")["clearance_required"] is False


def test_experience_and_salary_are_never_invented():
    assert extract_experience("5+ years of experience with Python")[0] == 5
    assert extract_experience("3-5 years of relevant experience")[1] == "3-5 years"
    assert extract_experience("Founded 12 years ago, we build tools.") == (None, "Not listed")
    assert extract_salary("Base pay range: $150,000 - $210,000 per year") == "$150,000 - $210,000"
    assert extract_salary("Competitive salary") == "Not listed"


def test_html_to_text_removes_scripts():
    assert html_to_text("&lt;p&gt;Hello &lt;script&gt;alert(1)&lt;/script&gt;world&lt;/p&gt;") == "Hello world."
