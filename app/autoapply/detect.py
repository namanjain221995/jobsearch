"""Which applicant tracking system hosts a posting, and the address of its application form.

Only employer-hosted forms on the four supported hosts are used. Everything else is recorded as unsupported with a
reason: Workday (an account per employer), LinkedIn / Indeed and other aggregators (not the employer's form),
custom career sites (an employer page that embeds a Greenhouse form in an iframe or its own form)."""
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit, urlunsplit

SUPPORTED = ("greenhouse", "lever", "ashby", "workable")
_AGGREGATORS = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "simplyhired.com", "dice.com", "monster.com",
                "careerbuilder.com", "wellfound.com", "builtin.com", "google.com")


@dataclass
class Target:
    ats: str                 # one of SUPPORTED, or "unsupported"
    url: str                 # the application form's address ("" when unsupported)
    reason: str = ""         # why unsupported
    code: str = ""           # blocker code when unsupported
    company_slug: str = ""
    posting_id: str = ""


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _ends(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def detect(url: str) -> Target:
    url = (url or "").strip()
    parts = urlsplit(url)
    host = _host(url)
    if parts.scheme not in ("http", "https") or not host:
        return Target("unsupported", "", "Not a web address", "unsupported_site")
    segs = [s for s in parts.path.split("/") if s]
    scheme = parts.scheme

    if host in ("job-boards.greenhouse.io", "boards.greenhouse.io", "job-boards.eu.greenhouse.io"):
        if segs[:1] == ["embed"]:
            query = parse_qs(parts.query)
            company, token = (query.get("for") or [""])[0], (query.get("token") or [""])[0]
            if company and token.isdigit():
                return Target("greenhouse", f"{scheme}://job-boards.greenhouse.io/{company}/jobs/{token}", company_slug=company, posting_id=token)
            return Target("unsupported", "", "Greenhouse embed without a job id", "unsupported_site")
        if len(segs) >= 3 and segs[1] == "jobs" and segs[2].isdigit():
            gh_host = "job-boards.eu.greenhouse.io" if host == "job-boards.eu.greenhouse.io" else "job-boards.greenhouse.io"
            return Target("greenhouse", f"{scheme}://{gh_host}/{segs[0]}/jobs/{segs[2]}", company_slug=segs[0], posting_id=segs[2])
        return Target("unsupported", "", "Greenhouse address that is not a single posting", "unsupported_site")
    if host == "jobs.lever.co" or host == "jobs.eu.lever.co":
        if len(segs) >= 2 and re.fullmatch(r"[0-9a-f-]{36}", segs[1]):
            return Target("lever", f"{scheme}://{host}/{segs[0]}/{segs[1]}/apply", company_slug=segs[0], posting_id=segs[1])
        return Target("unsupported", "", "Lever address that is not a single posting", "unsupported_site")
    if host == "jobs.ashbyhq.com":
        if len(segs) >= 2 and re.fullmatch(r"[0-9a-f-]{36}", segs[1]):
            return Target("ashby", f"{scheme}://{host}/{segs[0]}/{segs[1]}/application", company_slug=segs[0], posting_id=segs[1])
        return Target("unsupported", "", "Ashby address that is not a single posting", "unsupported_site")
    if host == "apply.workable.com":
        if "j" in segs:
            i = segs.index("j")
            if i + 1 < len(segs) and re.fullmatch(r"[0-9A-Za-z]{6,20}", segs[i + 1]):
                company = segs[i - 1] if i >= 1 else ""
                prefix = f"/{company}" if company else ""
                return Target("workable", f"{scheme}://{host}{prefix}/j/{segs[i + 1]}/apply/", company_slug=company, posting_id=segs[i + 1])
        return Target("unsupported", "", "Workable address that is not a single posting", "unsupported_site")
    if _ends(host, "myworkdayjobs.com") or _ends(host, "myworkdaysite.com") or _ends(host, "workday.com"):
        return Target("unsupported", "", "Workday requires an account per employer", "account_creation_required")
    if any(_ends(host, d) for d in _AGGREGATORS):
        return Target("unsupported", "", "Job board or aggregator, not the employer's application form", "unsupported_site")
    if "gh_jid" in parts.query or "lever-source" in parts.query:
        return Target("unsupported", "", "Employer career site that wraps the form; open the ATS posting instead", "custom_career_site")
    return Target("unsupported", "", "Not one of the supported application systems (Greenhouse, Lever, Ashby, Workable)", "unsupported_site")


def employer_key(ats: str, company_slug: str, company_name: str) -> str:
    """Groups submissions per employer for the daily cap: the ATS account if known, else the company name."""
    if ats in SUPPORTED and company_slug:
        return f"{ats}:{company_slug.lower()}"[:200]
    return "name:" + re.sub(r"[^a-z0-9]+", "", (company_name or "").lower())[:190]


def strip_query(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))
