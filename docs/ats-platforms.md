# ATS platforms: what can be read without the employer's credentials

Status: 2026-11-21. Covers thirteen of the 21 platforms in `app/ats_platforms.py` (iCIMS, SmartRecruiters, Jobvite,
Oracle Recruiting Cloud, SAP SuccessFactors, Bullhorn, CEIPAL, JobDiva, BambooHR, JazzHR, Avature, SmartSearch,
ADP Workforce Now). The other eight have board readers and are described in `docs/board-sources.md` and
`app/providers/ats_startup.py`.

Every statement below comes from a page fetched or a command run on 2026-11-21. "Not verified" means exactly that.
robots.txt was fetched with plain `curl`; terms pages were read through a page fetcher and the quoted words are the
ones it returned.

## Summary

| Platform | Access class | What the portal does | Reader |
|---|---|---|---|
| iCIMS | restricted | Nothing. Posting addresses are not followed. | none |
| SmartRecruiters | restricted | API reader stays off. Single posting pages on `jobs.smartrecruiters.com` found by web search are verified page by page (unchanged). | `smartrecruiters`, off |
| Jobvite | public_pages | Posting pages found by web search become page leads. | none |
| Oracle Recruiting Cloud | public_pages | Posting pages found by web search become page leads. Board reader built and verified live, off by default. | `oracle`, off (`BOARD_SOURCES_ORACLE=1`) |
| SAP SuccessFactors | restricted | Nothing on `career{N}.successfactors.com/.eu`. Employer-branded sites are ordinary employer pages. | none |
| Bullhorn | credentials | Nothing. | none |
| CEIPAL | credentials | Nothing. | none |
| JobDiva | credentials | Nothing. | none |
| BambooHR | restricted | Reader stays off. | `bamboohr`, off |
| JazzHR | public_pages | Posting pages found by web search become page leads; verified from the page's JobPosting data. | none |
| Avature | public_pages | Posting pages found by web search become page leads. | none |
| SmartSearch | credentials | Nothing. | none |
| ADP Workforce Now | public_pages | Posting addresses found by web search become page leads; need the browser renderer. | none |

"Page lead" = the address is handed to the existing `PageVerifier` (`app/job_verifier.py`), which checks the SSRF
guard and that host's robots.txt, opens the employer's live page and accepts it only when the page proves an open
posting (schema.org `JobPosting`, or the browser / AI page check when those are switched on).

## Evidence per platform

| Platform | Official docs | Public URL pattern | Anonymous API | robots.txt (fetched 2026-11-21) | Terms | Portal behaviour | Owner must supply to go further |
|---|---|---|---|---|---|---|---|
| **iCIMS** | Terms: https://www.icims.com/legal/terms-of-use/ (no public API documentation was read) | `https://careers-{company}.icims.com/jobs/{id}/{slug}/job` (seen live on `careers-peraton.icims.com`; some tenants redirect to `us-careers-{company}.icims.com`). The page is a frame; the JobPosting data is only in the `?in_iframe=1` variant. | No. None found. | Differs per tenant. `us-careers-rivian.icims.com` and `careers-amd.icims.com`: `User-agent: *` / `Disallow: /`. `careers-peraton.icims.com`: `Disallow: /jobs/*referral`, `/jobs/*login`, `/jobs/*candidate`, `/connect...` (posting pages allowed). `careers.icims.com` (iCIMS' own): `Allow: /`, `crawl-delay: 5`. | Terms of use (last modified April 1, 2024), prohibited uses: "Use any robot, spider or other automatic device, process, or means to access the Website for any purpose"; the Website is defined to include "its related domains and subdomains". Whether a customer's subscription agreement overrides this for its career site is not verified. | Nothing is read; iCIMS addresses are never page leads (`tests/test_ats_pages.py: FORBIDDEN`). | Written consent from iCIMS, or the employer's own iCIMS API credentials. |
| **SmartRecruiters** | https://developers.smartrecruiters.com/docs/posting-api (could not be re-read today: the fetcher was refused) | `https://jobs.smartrecruiters.com/{Company}/{postingId}-{slug}` | Yes technically: `https://api.smartrecruiters.com/v1/companies/{company}/postings` (reader exists in `app/providers/ats_extra.py`). | `api.smartrecruiters.com/robots.txt`, complete: `User-agent: LinkedInBot` / `Allow: /v1/companies/` / `User-agent: *` / `Disallow: /`. Unchanged from 2026-11-18. `jobs.smartrecruiters.com/robots.txt`: HTTP 404 (no rules). `careers.smartrecruiters.com/robots.txt`: 302 to `jobs.smartrecruiters.com/`. | Not re-read today. | Reader off (`BOARD_SOURCES_SMARTRECRUITERS` unset) and it refuses anyway while that robots.txt stands. Posting pages on `jobs.smartrecruiters.com` stay page leads as before. | SmartRecruiters' permission for the API (an allow rule for this client), or an employer's API key. |
| **Jobvite** | Terms: https://www.jobvite.com/terms-of-use/ ; API: Jobvite help centre article "Jobvite API" (seen in search results only) | `https://jobs.jobvite.com/{company}/job/{id}` (seen live: `/nutanix/job/o0lEAfwj`); board list at `/{company}/jobs`. | No. The API needs an API key and secret issued by Jobvite to the customer (search-result summary of Jobvite's help article; the article itself was not opened). | `jobs.jobvite.com/robots.txt`: HTTP 404, so no rules. | Terms of use (last updated February 23, 2021) cover "this website located at www.jobvite.com (\"Site\")": "You may not copy, distribute, scrape, disassemble, reverse engineer or otherwise appropriate or create any derivative works from any of these materials." Nothing in them names the customer career sites on `jobs.jobvite.com`. | Posting addresses are page leads. The live page had no JobPosting data (1 page checked), so the plain check answers "No structured job data on page"; a posting is accepted only by the browser / AI page check. | The employer's Jobvite API key and secret for whole boards. |
| **Oracle Recruiting Cloud** | https://docs.oracle.com/en/cloud/saas/human-resources/farws/api-recruiting-ce-job-requisitions.html | `https://{pod}.fa.[{region}.]oraclecloud.com/hcmUI/CandidateExperience/{lang}/sites/{siteNumber}/job/{id}` (seen live on `eeho.fa.us2.oraclecloud.com`, site `CX_45001`). | Yes, anonymous, and it is what the career site itself calls: `GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site},limit=25,offset=0` and `.../recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;Id="{id}",siteNumber={site}`. Both answered HTTP 200 without a login. | `eeho.fa.us2.oraclecloud.com/robots.txt`: HTTP 404 (no rules). `jpmc.fa.oraclecloud.com/robots.txt`: HTTP 403 "W4S-402: Blocked by WAF4SaaS" (that tenant blocks this client; not worked around). | Oracle's REST documentation of the resource: "The recruitingCEJobRequisitions resource includes the list of the available requisitions. Note that the service endpoints in this resource category are only for Oracle internal use." No site terms were read. | Posting addresses are page leads (the raw page has `og:title` and `og:site_name` but no JobPosting data, so it needs the browser / AI page check). Reader `app/providers/ats_oracle.py` is built and verified live but OFF because of the quoted sentence. | A decision to accept Oracle's "internal use" note (`BOARD_SOURCES_ORACLE=1`), plus board keys `{host}\|{siteNumber}` taken from real career-site addresses. |
| **SAP SuccessFactors** | Not read (the SAP Help Portal returned no text to the fetcher) | `https://career{N}.successfactors.com/career?company={id}&career_job_req_id={id}` (the pattern was not opened because robots.txt forbids it), and employer-branded sites on the employer's own domain (example checked: `jobs.sap.com`). | None found. Not verified against SAP's API documentation. | `career4.successfactors.com/robots.txt` and `career5.successfactors.eu/robots.txt`, complete: `User-agent: *` / `Disallow: /` / `Disallow: /*company` / `Allow: /login`. `jobs.sap.com/robots.txt`: `Allow: /` with `Disallow` only for `?search=`, `?keyword=`, `?query=`, `?q=`. | Not read. | Nothing on `successfactors.com` / `.eu`. A branded site on an employer's domain is handled like any employer page when a web search names it; it cannot be recognised as SuccessFactors by its address. | The employer's own SuccessFactors API access. |
| **Bullhorn** | https://bullhorn.github.io/Public-API/ ; https://bullhorn.github.io/rest-api-docs/ ; https://github.com/bullhorn/career-portal | None of Bullhorn's own. Each staffing firm hosts the open-source Career Portal on its own site. | Per firm, yes: `https://public-rest{cls}.bullhornstaffing.com/rest-services/{corpToken}/search/JobOrder?query=(isOpen:1) AND (isDeleted:0)&fields=...&count=..&start=..&sort=-dateLastPublished`. One live GET with the sample cluster and token shipped in the Career Portal's `src/app.json` answered HTTP 200, `"total":117`. The cluster number and corp token are issued by Bullhorn Support "After the customer has authorized the integration". | `public-rest33.bullhornstaffing.com/robots.txt`: HTTP 404 (no rules). | The Public API page links an "API Fair Use Policy"; it was not read. | Nothing. No reader was built: the answer carries no public posting address and no employer name, and there is no way to learn a firm's cluster and token except from that firm. | Per staffing firm: cluster number, corp token and the address of its career portal (then a reader is a small job). For the full REST API: an OAuth 2.0 login with the firm's API user. |
| **CEIPAL** | https://developer.ceipal.com/ | Not verified (no public career-page pattern was confirmed). | No. The v1 and v2 references list an authentication endpoint and a refresh-token endpoint before "Job Postings"; the body of the authentication page could not be read. Third-party guides name login e-mail, password and an API key from the CEIPAL admin portal (not verified against CEIPAL's own page). | `jobsapi.ceipal.com/robots.txt`, complete: `User-agent: *` / `Disallow: /`. `api.ceipal.com/robots.txt`: HTTP 404. | Not read. | Nothing. | A CEIPAL customer's API credentials (generated through that customer's CEIPAL account representative). |
| **JobDiva** | Not reached (`api.jobdiva.com` answers HTTP 401; no public documentation page was found) | A public candidate portal exists on `www1.jobdiva.com` (its robots.txt names `https://www1.jobdiva.com/candidates/myjobs/sitemapindex.jsp?env=1`). The posting-address shape was not verified: the one sitemap opened answered "Cannot request more than 200 rows at a time." | No. Third-party guides describe `GET /api/authenticate` with `clientid`, `username`, `password`; not verified against JobDiva. | `www1.jobdiva.com/robots.txt`, complete: one `Sitemap:` line, no rules. `api.jobdiva.com/robots.txt`: HTTP 401. | Not read. | Nothing. | A staffing firm's JobDiva API client id (issued by JobDiva support) and an API user's name and password. |
| **BambooHR** | https://www.bamboohr.com/legal/terms-of-service | `https://{company}.bamboohr.com/careers/{id}` | Yes technically: `https://{company}.bamboohr.com/careers/list` (reader exists in `app/providers/ats_extra.py`). | Tenant robots.txt was not re-fetched today (2026-11-18 finding: allows everything). | Terms of service (last updated September 18, 2026), section 4.2 forbids use "with any robot, spider, other automated device, or manual process to monitor or copy any content from the Service." Unchanged in substance. | Reader off (`BOARD_SOURCES_BAMBOOHR` unset); BambooHR addresses are never page leads. | BambooHR's consent, or an employer's own BambooHR API key. |
| **JazzHR** | https://www.resumatorapi.com/v1/ ; https://www.jazzhr.com/terms-of-service | `https://{company}.applytojob.com/apply/{id}/{Title-Slug}` (seen live in JazzHR's own sitemap feed `app.jazz.co/feeds/google/xml/0` and on `brennancenter.applytojob.com`). | No. The API needs the customer's key ("Your API key can be found in the Integrations section of your JazzHR account."). | `brennancenter.applytojob.com/robots.txt`: `User-agent: SemrushBot` / `User-agent: dotbot` / `Disallow: /`, then `User-agent: *` / `Disallow: /cb`, then five `Sitemap: http://app.jazz.co/feeds/google/xml/{0..4}` lines. Posting pages are allowed. | Terms of service (effective May 22, 2023, last updated November 27, 2024) are the customer agreement; a search of the text for robot, spider, scrape, crawl and automated found no clause on reading public career pages. | Posting addresses are page leads. The live page carries schema.org JobPosting data and was verified by the page verifier (see smoke test). | The employer's JazzHR API key for whole boards. |
| **Avature** | https://www.avature.net/terms-of-use/ (no public API documentation was read) | `https://{company}.avature.net/{portal}/JobDetail/{Title-Slug}/{number}` (seen live in `bloomberg.avature.net/careers/sitemap.xml`). A locale segment before the portal (`/en_US/careers/...`) is accepted too; that variant was not seen live. | No. None found. | Per tenant. `bloomberg.avature.net/robots.txt`: `User-agent: *` / `Allow: /$` / `Allow: /careers` / `Disallow: /careers/*qtvc=` / `Sitemap: https://bloomberg.avature.net/careers/sitemap_index.xml` (and the same pair for its other portals). | Terms of use (no date on the page) have no clause on robots or automated access; they say "This site and all content in this site may not be copied, reproduced, republished, uploaded, posted" and "You may not mirror any of the content from this site". | Posting addresses are page leads. The live page had no JobPosting data (1 page checked), so a posting is accepted only by the browser / AI page check. | The employer's Avature API access for whole boards (not verified what Avature offers). |
| **SmartSearch** | None found (`www.smartsearchinc.com/terms-of-use` is HTTP 404; a third-party catalogue points to provider API documentation that was not read) | Not verified. | Not verified; none found. | `www.smartsearchinc.com/robots.txt` exists (marketing site); no career-page host was identified to check. | Not read. | Nothing. | API access arranged with SmartSearch for a customer account. |
| **ADP Workforce Now** | https://www.adp.com/legal.aspx (ADP's developer documentation returned no text to the fetcher) | `https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={uuid}&ccId={id}&lang=en_US` for a career centre, with `&jobId={id}` for one posting. The career-centre address answered HTTP 200 live; the `jobId` form comes from third-party documentation and was not opened. | Not documented. The page is a script shell (19 kB, `<title>Recruitment</title>`, no job data); the endpoint its script calls was not examined. | `workforcenow.adp.com/robots.txt` answers 302 to the `online.adp.com` login and ends in an HTML page ("Federation Redirector"), so no rules can be read. The app treats an unparseable answer as "no rules". | Terms, section 3: "you will not access the Site in a manner that sends more requests to ADP servers than a human can reasonably produce" and no collecting or harvesting of personally identifiable information. No clause names robots or crawlers. | Posting addresses (with `cid` and `jobId`) are page leads; `cid` is kept in the lead address although it is a tracking parameter elsewhere. The page proves nothing without script, so a posting is accepted only when the headless browser renders it. Not verified live with a real posting. | API access arranged with ADP and the employer for whole boards. |

## What was implemented

- `app/providers/ats_pages.py`: strict posting-address patterns for JazzHR, Jobvite, Avature, Oracle and ADP;
  `posting(url)` returns the platform and a clean address, `SITES` the hosts for `site:` queries.
- `app/providers/searxng.py`, page branch of `extract()`: an address on one of those hosts is a `page` hit when it is
  a posting and is ignored otherwise.
- Hosts that get queried: when a search is limited to platforms, `Scope.hosts()` (`app/search_scope.py`) already adds
  the hosts of chosen `public_pages` platforms from `app/ats_platforms.py`. For searches without a platform filter the
  hosts are only queried when they are in `SEARXNG_SITES`; the default in `app/config.py` was not changed (every extra
  host takes a share of the per-search query budget). To add them:
  `applytojob.com,jobs.jobvite.com,avature.net,oraclecloud.com,workforcenow.adp.com`.
- `app/providers/ats_oracle.py`: Oracle reader (board key `{host}|{siteNumber}`, searched per title like Workday,
  25 postings per page, at most 4 pages, one request per second for all Oracle tenants, netguard and robots.txt on every
  request, HTTP 404 = `BoardNotFound`, description read per posting). Registered in `app/providers/registry.py` only
  when `BOARD_SOURCES_ORACLE=1`.

Not wired by this work: recognising an Oracle career-site address as a *board* (`parse_ats`, `valid_board`,
`BOARD_SOURCES`). Until then the Oracle reader only reads boards that are put into the index by hand;
`ats_oracle.valid_key()` is ready for that hook.

## Live smoke test (2026-11-21)

Oracle reader, board `eeho.fa.us2.oraclecloud.com|CX_45001`, title "data engineer", 2 pages: 8 requests in total
(1 robots.txt, 4 list pages, 2 site home pages, 1 detail; 3 of them belong to the unknown-site check below), 4.4 s for
the search itself. 50 postings returned, 39 of them in the United States, employer name "Oracle" read from the site's
`og:site_name`, description of one posting 20,079 characters. `TotalJobsCount` for the whole site was 2,851.

Limit found: a site number that does not exist (`CX_99999999`) still answers HTTP 200 with the tenant's postings, so
a wrong site number is not detected and would produce wrong posting addresses. Board keys must come from a real
career-site address.

Page leads through `extract()` and `PageVerifier` (plain fetch, no browser, no AI), 8 requests (robots.txt + page each):

| Address | Result |
|---|---|
| `brennancenter.applytojob.com/apply/WpIfUaLo9Q/Associate-Editor` (JazzHR) | Verified: "Associate Editor", Brennan Center for Justice, New York, NY, posted 2026-10-08 |
| `jobs.jobvite.com/nutanix/job/o0lEAfwj` (Jobvite) | Not verified: "No structured job data on page" (goes on to the browser / AI check when enabled) |
| `bloomberg.avature.net/careers/JobDetail/.../46249` (Avature) | Not verified: "No structured job data on page" |
| `eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/job/350789` (Oracle) | Not verified: "No structured job data on page" |

The browser and AI page checks were not run in this test, so whether Jobvite, Avature, Oracle and ADP pages pass them is
not verified.

## Tests

`.venv/bin/python -m pytest tests/test_ats_pages.py tests/test_ats_oracle.py -q`
