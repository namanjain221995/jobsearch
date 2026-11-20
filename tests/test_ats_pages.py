"""Posting addresses on public career pages of platforms without a board reader (app/providers/ats_pages.py), their
hook in the search-engine result classifier, and the thirteen platform entries they belong to. No network."""
import pytest

from app.ats_platforms import ACCESS, PLATFORMS, platform_of_host
from app.providers import ats_pages
from app.providers.searxng import extract, site_hosts

# Addresses of the shape seen on live pages on 2026-11-21 (docs/ats-platforms.md).
POSTINGS = [
    ("jazzhr", "https://brennancenter.applytojob.com/apply/WpIfUaLo9Q/Associate-Editor",
     "https://brennancenter.applytojob.com/apply/WpIfUaLo9Q/Associate-Editor"),
    ("jazzhr", "https://easeinc.applytojob.com/apply/vMHpWDZa3l/Account-Executive?source=GS#top",
     "https://easeinc.applytojob.com/apply/vMHpWDZa3l/Account-Executive"),
    ("jazzhr", "https://acme.applytojob.com/apply/vMHpWDZa3l", "https://acme.applytojob.com/apply/vMHpWDZa3l"),
    ("jobvite", "https://jobs.jobvite.com/nutanix/job/o0lEAfwj", "https://jobs.jobvite.com/nutanix/job/o0lEAfwj"),
    ("jobvite", "https://jobs.jobvite.com/egnyte/job/o0n4AfwL?__jvst=Job%20Board", "https://jobs.jobvite.com/egnyte/job/o0n4AfwL"),
    ("avature", "https://bloomberg.avature.net/careers/JobDetail/Climate-Data-Utility-Tech-Team-Senior-Data-Engineer/46249",
     "https://bloomberg.avature.net/careers/JobDetail/Climate-Data-Utility-Tech-Team-Senior-Data-Engineer/46249"),
    ("avature", "https://acme.avature.net/en_US/careers/JobDetail/Data-Engineer/1234?jobId=1234",
     "https://acme.avature.net/en_US/careers/JobDetail/Data-Engineer/1234"),
    ("oracle", "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/job/338589",
     "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/job/338589"),
    ("oracle", "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210512345/?utm_medium=jobshare",
     "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210512345"),
    ("adp", "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=070b2b3d-d077-4d73-b340-6055055056a6"
            "&ccId=19000101_000001&lang=en_US&jobId=9201203789874_1&selectedMenuKey=CareerCenter",
     "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=070b2b3d-d077-4d73-b340-6055055056a6"
     "&ccId=19000101_000001&jobId=9201203789874_1&lang=en_US"),
    ("adp", "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?jobId=512345&cid=070B2B3D-D077-4D73-B340-6055055056A6",
     "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=070b2b3d-d077-4d73-b340-6055055056a6&jobId=512345"),
]

# Career home pages, listings, searches, application forms and vendor pages: never a posting.
NOT_POSTINGS = [
    "https://brennancenter.applytojob.com/", "https://brennancenter.applytojob.com/apply",
    "https://brennancenter.applytojob.com/apply/", "https://brennancenter.applytojob.com/apply/jobs",
    "https://brennancenter.applytojob.com/apply/jobs/details/WpIfUaLo9Q", "https://brennancenter.applytojob.com/apply/careers/x",
    "https://brennancenter.applytojob.com/cb/WpIfUaLo9Q", "https://www.applytojob.com/apply/WpIfUaLo9Q/Associate-Editor",
    "https://app.applytojob.com/apply/WpIfUaLo9Q/Associate-Editor", "https://applytojob.com/apply/WpIfUaLo9Q/Associate-Editor",
    "https://a.b.applytojob.com/apply/WpIfUaLo9Q/Associate-Editor", "https://brennancenter.applytojob.com/apply/WpIfUaLo9Q/a/b",
    "https://jobs.jobvite.com/nutanix", "https://jobs.jobvite.com/nutanix/jobs", "https://jobs.jobvite.com/nutanix/jobs/alljobs",
    "https://jobs.jobvite.com/nutanix/search?q=engineer", "https://jobs.jobvite.com/nutanix/job", "https://jobs.jobvite.com/nutanix/job/",
    "https://jobs.jobvite.com/nutanix/job/o0lEAfwj/apply/extra", "https://jobs.jobvite.com/careers/job/o0lEAfwj",
    "https://jobs.jobvite.com/nutanix/job/o0l", "https://www.jobvite.com/nutanix/job/o0lEAfwj", "https://careers.jobvite.com/nutanix/job/o0lEAfwj",
    "https://bloomberg.avature.net/", "https://bloomberg.avature.net/careers", "https://bloomberg.avature.net/careers/SearchJobs",
    "https://bloomberg.avature.net/careers/SearchJobs/engineer", "https://bloomberg.avature.net/careers/JobDetail",
    "https://bloomberg.avature.net/careers/JobDetail/Data-Engineer", "https://bloomberg.avature.net/careers/JobDetail/Data-Engineer/abc",
    "https://bloomberg.avature.net/careers/ApplicationMethods?jobId=46249", "https://bloomberg.avature.net/careers/Login",
    "https://www.avature.net/careers/JobDetail/Data-Engineer/46249", "https://avature.net/careers/JobDetail/Data-Engineer/46249",
    "https://eeho.fa.us2.oraclecloud.com/", "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001",
    "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/jobs",
    "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/requisitions?keyword=data",
    "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/job",
    "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/job/338589/apply/email",
    "https://eeho.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
    "https://eeho.fa.us2.oraclecloud.com/hcmUI/faces/FuseWelcome", "https://cloud.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1",
    "https://objectstorage.us-ashburn-1.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1",
    "https://workforcenow.adp.com/", "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html",
    "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=070b2b3d-d077-4d73-b340-6055055056a6",
    "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=070b2b3d-d077-4d73-b340-6055055056a6&ccId=19000101_000001&lang=en_US",
    "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=not-a-uuid&jobId=512345",
    "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?jobId=512345",
    "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=070b2b3d-d077-4d73-b340-6055055056a6&jobId=1&jobId=512345",
    "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=070b2b3d-d077-4d73-b340-6055055056a6&jobId=../x",
    "https://workforcenow.adp.com/theme/unified/login.html?cid=070b2b3d-d077-4d73-b340-6055055056a6&jobId=512345",
    "https://online.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=070b2b3d-d077-4d73-b340-6055055056a6&jobId=512345",
]

HOSTILE = [
    "http://brennancenter.applytojob.com/apply/WpIfUaLo9Q/Associate-Editor",                    # not https
    "https://user:pw@brennancenter.applytojob.com/apply/WpIfUaLo9Q/Associate-Editor",            # userinfo
    "https://user@jobs.jobvite.com/nutanix/job/o0lEAfwj", "https://@jobs.jobvite.com/nutanix/job/o0lEAfwj",
    "https://jobs.jobvite.com:8443/nutanix/job/o0lEAfwj", "https://jobs.jobvite.com:80/nutanix/job/o0lEAfwj",   # ports
    "https://jobs.jobvite.com:x/nutanix/job/o0lEAfwj",
    "https://jobs.jobvite.com.evil.example/nutanix/job/o0lEAfwj", "https://evil-jobs.jobvite.com/nutanix/job/o0lEAfwj",   # look-alikes
    "https://jobs.jobvite.com@evil.example/nutanix/job/o0lEAfwj", "https://evil.example/jobs.jobvite.com/nutanix/job/o0lEAfwj",
    "https://brennancenter.applytojob.com.evil.example/apply/WpIfUaLo9Q/Associate-Editor",
    "https://brennancenterapplytojob.com/apply/WpIfUaLo9Q/Associate-Editor", "https://evil.example/?u=https://x.applytojob.com/apply/WpIfUaLo9Q",
    "https://bloomberg.avature.net.evil.example/careers/JobDetail/Data-Engineer/46249",
    "https://bloomberg.notavature.net/careers/JobDetail/Data-Engineer/46249",
    "https://eeho.fa.us2.oraclecloud.com.evil.example/hcmUI/CandidateExperience/en/sites/CX_45001/job/338589",
    "https://eeho.fa.us2.evil-oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/job/338589",
    "https://workforcenow.adp.com.evil.example/mascsr/default/mdf/recruitment/recruitment.html?cid=070b2b3d-d077-4d73-b340-6055055056a6&jobId=512345",
    "https://careers-acme.icims.com.evil.example/jobs/1234/data-engineer/job",
    "https://jobs.jobvite.com/nutanix/job/../job/o0lEAfwj", "https://jobs.jobvite.com/nutanix/job/o0lEAfwj%2f..%2f",
    "https://jobs.jobvite.com/nutanix//job/o0lEAfwj", "https://jobs.jobvite.com\\@evil.example/nutanix/job/o0lEAfwj",
    "https://jobs.jobvite.com/nutanix/job/o0lEAfwj\nhttps://evil.example", "https://jobs.jobvite.com/nutanix/job/o0lEAfwj /x",
    "https://bloomberg.avature.net/careers/JobDetail/%2e%2e/46249", "https://[::1]/nutanix/job/o0lEAfwj", "https://127.0.0.1/nutanix/job/o0lEAfwj",
    "ftp://jobs.jobvite.com/nutanix/job/o0lEAfwj", "//jobs.jobvite.com/nutanix/job/o0lEAfwj", "jobs.jobvite.com/nutanix/job/o0lEAfwj",
    "https://jobs.jobvite.com/" + "a" * 700, "https://jobs.jobvite.com/nütanix/job/o0lEAfwj", "", None, 7,
]

# Platforms whose robots.txt or terms forbid automated reading: a posting there is never a page lead.
FORBIDDEN = [
    "https://careers-peraton.icims.com/jobs/174576/software-quality-assurance-engineer/job",
    "https://us-careers-rivian.icims.com/jobs/12345/data-engineer/job?in_iframe=1",
    "https://career4.successfactors.com/career?company=SAP&career_job_req_id=12345",
    "https://career5.successfactors.eu/career?company=acme&career_ns=job_listing&career_job_req_id=1",
    "https://acme.bamboohr.com/careers/42",
]

ALL_HOSTS = [site for sites in ats_pages.SITES.values() for site in sites] + ["icims.com", "successfactors.com",
                                                                               "successfactors.eu", "bamboohr.com"]


@pytest.mark.parametrize("platform,url,clean", POSTINGS)
def test_real_shaped_posting_addresses_are_recognised(platform, url, clean):
    assert ats_pages.posting(url) == (platform, clean)
    assert ats_pages.posting("  " + url) == (platform, clean)
    assert ats_pages.posting(clean) == (platform, clean)                       # the clean address is stable
    hit = extract(url, ALL_HOSTS)
    assert hit is not None and (hit.kind, hit.src, hit.board, hit.job_id, hit.url) == ("page", "", "", "", clean)
    assert platform_of_host(clean.split("/")[2]).id == platform


@pytest.mark.parametrize("url", NOT_POSTINGS)
def test_home_listing_and_search_pages_are_never_postings(url):
    assert ats_pages.posting(url) is None
    assert extract(url, ALL_HOSTS) is None


@pytest.mark.parametrize("url", HOSTILE)
def test_hostile_addresses_are_rejected(url):
    assert ats_pages.posting(url) is None
    assert extract(url, ALL_HOSTS) is None


@pytest.mark.parametrize("url", FORBIDDEN)
def test_platforms_that_forbid_automated_reading_give_no_page_lead(url):
    assert ats_pages.posting(url) is None
    assert extract(url, ALL_HOSTS) is None


def test_a_posting_counts_only_on_a_host_the_search_was_allowed_to_query():
    url = "https://jobs.jobvite.com/nutanix/job/o0lEAfwj"
    assert extract(url, ["jobs.lever.co"]) is None
    assert extract(url, ["jobs.jobvite.com"]).kind == "page"
    assert extract("https://acme.applytojob.com/apply/vMHpWDZa3l/Data-Engineer", ["applytojob.com"]).kind == "page"


def test_the_adp_company_id_survives_in_the_lead_address():
    """`cid` is a tracking parameter elsewhere (job_normalizer.canonical_url drops it); on ADP it names the employer."""
    hit = extract("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=070b2b3d-d077-4d73-b340-6055055056a6"
                  "&jobId=512345&utm_source=x", ALL_HOSTS)
    assert "cid=070b2b3d-d077-4d73-b340-6055055056a6" in hit.url and "jobId=512345" in hit.url and "utm_source" not in hit.url


def test_sites_are_valid_query_hosts_of_public_page_platforms():
    for platform_id, sites in ats_pages.SITES.items():
        platform = PLATFORMS[platform_id]
        assert platform.access == "public_pages"
        assert site_hosts(",".join(sites)) == list(sites)                       # usable in SEARXNG_SITES as they are
        assert {h.lstrip(".") for h in platform.hosts} == set(sites)            # the hosts a platform filter queries
        for site in sites:
            assert ats_pages.is_page_host(site) and ats_pages.is_page_host("acme." + site)
    assert not ats_pages.is_page_host("icims.com") and not ats_pages.is_page_host("evilapplytojob.com")
    assert not ats_pages.is_page_host("applytojob.com.evil.example") and not ats_pages.is_page_host("")


# ---------------------------------------------------------------- the thirteen platform entries
THIRTEEN = {
    "icims": ("restricted", ["careers-acme.icims.com", "us-careers-acme.icims.com"]),
    "smartrecruiters": ("restricted", ["jobs.smartrecruiters.com", "careers.smartrecruiters.com"]),
    "jobvite": ("public_pages", ["jobs.jobvite.com"]),
    "oracle": ("public_pages", ["eeho.fa.us2.oraclecloud.com", "jpmc.fa.oraclecloud.com"]),
    "successfactors": ("restricted", ["career4.successfactors.com", "career5.successfactors.eu"]),
    "bullhorn": ("credentials", []),
    "ceipal": ("credentials", []),
    "jobdiva": ("credentials", []),
    "bamboohr": ("restricted", ["acme.bamboohr.com"]),
    "jazzhr": ("public_pages", ["acme.applytojob.com"]),
    "avature": ("public_pages", ["acme.avature.net"]),
    "smartsearch": ("credentials", []),
    "adp": ("public_pages", ["workforcenow.adp.com"]),
}


@pytest.mark.parametrize("platform_id", sorted(THIRTEEN))
def test_platform_entry(platform_id):
    access, hosts = THIRTEEN[platform_id]
    platform = PLATFORMS[platform_id]
    assert platform.access == access and access in ACCESS
    assert len(platform.note) > 60 and platform.note.endswith(".")              # the Admin gets a reason, not a blank
    assert bool(platform.hosts) == bool(hosts)
    for host in hosts:
        assert platform_of_host(host) is platform and platform_of_host("www." + host) is platform
    for known in platform.hosts:
        bare = known.lstrip(".")
        assert platform_of_host(bare + ".evil.example") is None
        assert platform_of_host("evil" + bare) is None
    if access == "credentials":
        assert not platform.reader and not platform.hosts                       # nothing is read, nothing is queried


def test_look_alike_hosts_belong_to_no_platform():
    for host in ("icims.com.evil.example", "careers-acme.icims.com.evil.example", "notapplytojob.com", "oraclecloud.com.evil.example",
                 "avature.net.evil.example", "workforcenow.adp.com.evil.example", "xworkforcenow.adp.com", "adp.com", "jobvite.com",
                 "successfactors.com.evil.example", "bamboohr.com.evil.example"):
        assert platform_of_host(host) is None, host
