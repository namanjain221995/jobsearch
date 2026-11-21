"""The 21 applicant-tracking systems (ATS) this portal knows about, in one place.

An ATS vendor is not a job board: jobs live on each employer's own board or tenant. What the portal can do with a
platform therefore depends on what the vendor offers without the employer's credentials:

- `reader`: the name of the source in providers/registry.py that reads one employer board through an official,
  anonymous API or feed. Empty = there is no such reader.
- `access`: how postings of this platform can be reached at all (see ACCESS).
- `hosts`: host names (or suffixes) on which the platform serves public career pages. Used to recognise result
  addresses of search engines; a posting page on such a host is verified by the page verifier like any employer page.

Nothing here claims that a platform works. The operational state shown to an Admin is computed from evidence
(readers switched on, boards in the index, last successful read) by services/ats_health.py."""
from dataclasses import dataclass

# How postings of a platform can be reached without the employer's credentials.
ACCESS = {
    "public_api": "Official anonymous API or feed per employer board",
    "public_pages": "Public career pages only (no anonymous API); postings are verified page by page",
    "credentials": "API needs the employer's credentials; nothing is read without them",
    "restricted": "The vendor's robots.txt or terms forbid automated reading; switched off",
}


@dataclass(frozen=True)
class Platform:
    id: str                         # stable identifier, also the registry source name when `reader` is set
    label: str
    access: str                     # a key of ACCESS
    hosts: tuple[str, ...] = ()     # public career-page hosts; a leading "." means "any subdomain of"
    reader: str = ""                # registry source name of the board reader, "" when there is none
    startup: bool = False           # one of the seven startup-focused platforms
    docs: str = ""                  # official documentation the integration is based on
    note: str = ""                  # one sentence for the Admin: what is and is not possible, and why


PLATFORMS: dict[str, Platform] = {p.id: p for p in [
    Platform("greenhouse", "Greenhouse", "public_api", ("boards.greenhouse.io", "job-boards.greenhouse.io"), "greenhouse", True,
             "https://developers.greenhouse.io/job-board.html", "Job Board API, one request per employer board."),
    Platform("lever", "Lever", "public_api", ("jobs.lever.co",), "lever", True,
             "https://github.com/lever/postings-api", "Postings API, one request per employer board."),
    Platform("ashby", "Ashby", "public_api", ("jobs.ashbyhq.com",), "ashby", True,
             "https://developers.ashbyhq.com/docs/public-job-posting-api", "Public job posting API per employer board."),
    Platform("workday", "Workday", "public_api", (".myworkdayjobs.com",), "workday", False,
             "", "The search endpoint each tenant's own career site uses; one tenant and site per board."),
    Platform("icims", "iCIMS", "restricted", (".icims.com",), "", False, "https://www.icims.com/legal/terms-of-use/",
             "Not read: iCIMS' terms of use forbid any robot or other automatic means on icims.com and its subdomains, and "
             "several employer career sites (careers-{company}.icims.com) answer robots.txt with Disallow: /. There is no "
             "anonymous API; the owner would need written consent from iCIMS or the employer's own API credentials."),
    Platform("smartrecruiters", "SmartRecruiters", "restricted", ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"),
             "smartrecruiters", False, "https://developers.smartrecruiters.com/docs/posting-api",
             "A reader for the public Posting API exists but stays off: api.smartrecruiters.com/robots.txt allows only "
             "LinkedInBot and disallows everything for all other clients (checked again on 2026-11-21). Single posting "
             "pages on jobs.smartrecruiters.com found by web search are still verified page by page."),
    Platform("workable", "Workable", "public_api", ("apply.workable.com",), "workable", True,
             "https://workable.readme.io/reference/jobs-1",
             "Documented public jobs endpoint, one request per employer account with full descriptions; robots.txt "
             "allows it. Workable's cross-company job search (undocumented, WORKABLE_SEARCH_ENABLED) is read as well."),
    Platform("jobvite", "Jobvite", "public_pages", ("jobs.jobvite.com",), "", False, "https://www.jobvite.com/terms-of-use/",
             "No anonymous API. Posting pages (jobs.jobvite.com/{company}/job/{id}) found by web search are verified one "
             "by one from the page itself, without a browser: by structured job data where the page carries it (5 of 7 "
             "open postings on 2026-11-21), otherwise only when the AI page check confirms it. Reading whole boards would "
             "need the employer's Jobvite API key and secret."),
    Platform("oracle", "Oracle Recruiting Cloud", "public_pages", (".oraclecloud.com",), "oracle", False,
             "https://docs.oracle.com/en/cloud/saas/human-resources/farws/api-recruiting-ce-job-requisitions.html",
             "Posting pages on an employer's Oracle career site found by web search are verified one by one; each needs a "
             "rendered page from the browser budget (5 to 8 s on 2026-11-21), and without the browser none is verified. A "
             "board reader for the anonymous endpoint those career sites call is built and was verified live, but it is "
             "off (BOARD_SOURCES_ORACLE=1 switches it on) because Oracle documents that endpoint as only for Oracle "
             "internal use."),
    Platform("successfactors", "SAP SuccessFactors", "restricted", (".successfactors.com", ".successfactors.eu"), "", False,
             "",
             "Not read: career{N}.successfactors.com and .eu answer robots.txt with Disallow: /, and no anonymous job "
             "API was found. Employer-branded career sites on the employer's own domain are treated like any other "
             "employer page; they cannot be recognised as SuccessFactors by their address. Going further needs the "
             "employer's own SuccessFactors API access."),
    Platform("bullhorn", "Bullhorn", "credentials", (), "", False, "https://bullhorn.github.io/Public-API/",
             "Nothing is read. Bullhorn's public job endpoint answers without a login, but only for a staffing firm whose "
             "cluster number and corp token are known (issued by Bullhorn Support to that firm), and it gives no public "
             "posting address; the owner must supply cluster, corp token and career-portal address per firm. The full "
             "REST API needs an OAuth 2.0 login with the firm's own API user."),
    Platform("ceipal", "CEIPAL", "credentials", (), "", False, "https://developer.ceipal.com/",
             "Nothing is read. The CEIPAL ATS API issues a token only for a customer's own API credentials (generated "
             "through the customer's CEIPAL account representative), and jobsapi.ceipal.com/robots.txt disallows everything."),
    Platform("jobdiva", "JobDiva", "credentials", (), "", False, "",
             "Nothing is read. api.jobdiva.com answers HTTP 401 without a login; the owner would need a staffing firm's "
             "JobDiva API client id and an API user (user name and password) issued by JobDiva."),
    Platform("bamboohr", "BambooHR", "restricted", (".bamboohr.com",), "bamboohr", False,
             "https://www.bamboohr.com/legal/terms-of-service",
             "A reader for the public careers list exists but stays off: BambooHR's terms of service (section 4.2, last "
             "updated 2026-09-18) forbid using any robot, spider or other automated device to monitor or copy content. "
             "The owner would need BambooHR's consent or an employer's own API key."),
    Platform("jazzhr", "JazzHR", "public_pages", (".applytojob.com",), "", False, "https://www.jazzhr.com/terms-of-service",
             "No anonymous API. Posting pages ({company}.applytojob.com/apply/{id}/...) found by web search are verified "
             "one by one from the page itself, without a browser: by structured job data where the page carries it (4 of 6 "
             "open postings on 2026-11-21), otherwise only when the AI page check confirms it; robots.txt there disallows "
             "only /cb. Reading whole boards would need the employer's JazzHR API key."),
    Platform("avature", "Avature", "public_pages", (".avature.net",), "", False, "https://www.avature.net/terms-of-use/",
             "No anonymous API. Posting pages ({company}.avature.net/careers/JobDetail/...) found by web search are "
             "verified one by one from the page itself, without a browser; they carry no structured job data, so a page "
             "counts only when the AI page check confirms it (no posting date that way). Employers that answer with a "
             "robot check (2 of 4 tried on 2026-11-21) cannot be read. Each employer sets its own robots.txt, which is "
             "read before a page is opened."),
    Platform("pinpoint", "Pinpoint", "public_api", (".pinpointhq.com",), "pinpoint", True,
             "https://developers.pinpointhq.com/docs/jobs-json-endpoint",
             "Documented postings.json feed per employer, but employer tenants answer robots.txt with Disallow: /, so the "
             "reader is off by default and refuses such tenants even when switched on."),
    Platform("recruitee", "Recruitee", "public_api", (".recruitee.com",), "recruitee", True,
             "https://docs.recruitee.com/reference/offers",
             "Careers Site API, one request per employer board with full descriptions; robots.txt allows it."),
    Platform("rippling", "Rippling", "public_api", ("ats.rippling.com",), "rippling", True, "",
             "No Rippling board is read: the reader is switched off until Rippling's terms are reviewed. It uses the job "
             "list Rippling's hosted career pages call, which is not in Rippling's public documentation, and the terms "
             "pages could not be read (robots.txt allows the endpoint). RIPPLING_ENABLED=1 switches it on after that review."),
    Platform("smartsearch", "SmartSearch", "credentials", (), "", False, "",
             "Nothing is read. No public documentation of an anonymous job feed or of public career-page addresses was "
             "found; the owner would need API access arranged with SmartSearch for a customer account."),
    Platform("adp", "ADP Workforce Now", "public_pages", ("workforcenow.adp.com",), "", False, "https://www.adp.com/legal.aspx",
             "No anonymous API is documented. Career-centre posting addresses (recruitment.html?cid=...&jobId=...) found "
             "by web search are verified one by one; each needs a rendered page from the browser budget (10 to 13 s on "
             "2026-11-21) and the AI page check. The page has no employer or location field (the employer counts only when "
             "the posting text names it), so few postings can be shown. Reading whole boards would need API access "
             "arranged with ADP and the employer."),
]}

STARTUP_PLATFORMS: tuple[str, ...] = tuple(p.id for p in PLATFORMS.values() if p.startup)


def platform_of_host(host: str) -> Platform | None:
    """The platform that serves public career pages on `host`, or None."""
    host = (host or "").lower().removeprefix("www.")
    for platform in PLATFORMS.values():
        for known in platform.hosts:
            if (known.startswith(".") and host.endswith(known)) or host == known:
                return platform
    return None
