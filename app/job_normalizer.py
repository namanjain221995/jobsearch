"""Deterministic normalization: URLs, ATS identifiers, US location, work mode,
authorization language, experience and salary extraction. No AI, no network."""
import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ---------------------------------------------------------------- text
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def html_to_text(value: str) -> str:
    if not value:
        return ""
    text = html.unescape(value)
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<\s*(br|/p|/li|/div|/h[1-6])\s*/?>", ". ", text)
    text = _TAG.sub(" ", text)
    return _WS.sub(" ", html.unescape(text)).strip()


_TAG_START = re.compile(r"<(?=[A-Za-z/!?])")
_INLINE_TAG = re.compile(r"(?i)</?\s*(a|abbr|b|code|em|font|i|mark|s|small|span|strike|strong|sub|sup|u)(\s[^>]*)?/?>")


def html_to_structured_text(value: str) -> str:
    """HTML to plain text that keeps the posting's shape: paragraphs, headings and bullet points become
    line breaks and "• " markers. The result contains no markup, so it is safe to render as text."""
    if not value:
        return ""
    text = html.unescape(value)
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<\s*h[1-6][^>]*>", "\n\n", text)
    text = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", text)
    text = re.sub(r"(?i)<\s*(br|hr)\s*/?>|</\s*(li|tr)\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|section|article|table|ul|ol|h[1-6]|blockquote)\s*>", "\n\n", text)
    text = _INLINE_TAG.sub("", text)                    # <b>word</b>. must not become "word ."
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    # The source may have spelled a tag with entities, or left one unclosed. After decoding, nothing that can
    # start a tag is kept: complete tags go, and a "<" directly before a letter, "/", "!" or "?" gets a space after it.
    text = _TAG_START.sub("< ", _TAG.sub(" ", text))
    out: list[str] = []
    for line in (re.sub(r"[ \t\xa0\u200b]+", " ", raw).strip() for raw in text.split("\n")):
        if line.startswith("•"):
            line = ("• " + line[1:].strip()).strip()
        if line == "•" or (not line and (not out or not out[-1])):
            continue                                    # empty bullet, or a second blank line in a row
        if line.startswith("• ") and len(out) >= 2 and not out[-1] and out[-2].startswith("• "):
            out.pop()                                   # a list stays one block: no blank line between its bullets
        out.append(line)
    return "\n".join(out).strip()


def norm_text(value: str) -> str:
    return _WS.sub(" ", re.sub(r"[^a-z0-9+#]+", " ", (value or "").lower())).strip()


_COMPANY_SUFFIX = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|technology|labs|holdings|pbc|usa|us)\b")


def norm_company(value: str) -> str:
    return _WS.sub(" ", _COMPANY_SUFFIX.sub(" ", norm_text(value))).strip() or norm_text(value)


# ---------------------------------------------------------------- urls
_TRACKING = re.compile(
    r"^(utm_.*|gh_src|lever-source.*|lever-origin|lever-via|source|src|ref|referrer|referer|gclid|fbclid|"
    r"msclkid|trk.*|mc_cid|mc_eid|_hsenc|_hsmi|hsa_.*|campaign|cid|sid|jobboard|jobsite|origin|"
    r"ashby_src|__jvst|__jvsd|iis|iisn|rx_.*|sponsored|from)$",
    re.I,
)
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
GREENHOUSE_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"}


def canonical_url(url: str) -> str:
    """Lowercase host, drop fragment / tracking params / trailing apply segment."""
    try:
        p = urlsplit((url or "").strip())
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+", "/", p.path)
    path = re.sub(r"/(apply|application)/?$", "", path, flags=re.I).rstrip("/")
    query = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not _TRACKING.match(k))
    return urlunsplit(("https", host, path, urlencode(query), ""))


def parse_ats(url: str) -> tuple[str, str, str] | None:
    """Return (ats, board, job_id) for Greenhouse / Lever / Ashby URLs. board or job_id may be ''."""
    try:
        p = urlsplit((url or "").strip())
    except ValueError:
        return None
    host = p.netloc.lower().removeprefix("www.")
    parts = [s for s in p.path.split("/") if s]
    query = dict(parse_qsl(p.query))
    if host in GREENHOUSE_HOSTS:
        if parts[:2] == ["embed", "job_app"] or parts[:2] == ["embed", "job_board"]:
            return ("greenhouse", query.get("for", "").lower(), query.get("token", "") if query.get("token", "").isdigit() else "")
        if len(parts) >= 3 and parts[1] == "jobs" and parts[2].isdigit():
            return ("greenhouse", parts[0].lower(), parts[2])
        if parts:
            return ("greenhouse", parts[0].lower(), "")
        return None
    if query.get("gh_jid", "").isdigit():
        return ("greenhouse", "", query["gh_jid"])
    if host == "jobs.lever.co":
        if len(parts) >= 2 and re.fullmatch(_UUID, parts[1].lower()):
            return ("lever", parts[0].lower(), parts[1].lower())
        if parts:
            return ("lever", parts[0].lower(), "")
        return None
    if host == "jobs.ashbyhq.com":
        if len(parts) >= 2 and re.fullmatch(_UUID, parts[1].lower()):
            return ("ashby", parts[0].lower(), parts[1].lower())
        if parts:
            return ("ashby", parts[0].lower(), "")
        return None
    if re.fullmatch(_UUID, query.get("ashby_jid", "").lower()):
        return ("ashby", "", query["ashby_jid"].lower())
    m = _WORKDAY_HOST.fullmatch(host)
    if m:
        # https://{tenant}.wd5.myworkdayjobs.com/[en-US/]{site}/job/{location}/{title}_{REQ-ID}[-n]
        tenant, wd = m.group(1), m.group(2)
        segs = [s for s in parts if not re.fullmatch(r"[a-z]{2}-[A-Za-z]{2}", s)]
        if not segs:
            return None
        board = f"{tenant}|{wd}|{segs[0]}"
        if len(segs) >= 3 and segs[1] == "job":
            req = re.search(r"_([A-Za-z0-9-]+?)(?:-\d{1,2})?$", segs[-1])
            return ("workday", board, f"{tenant}:{req.group(1).upper()}" if req else "")
        return ("workday", board, "")
    if host == "apply.workable.com":
        # https://apply.workable.com/{account}/j/{SHORTCODE}  or  https://apply.workable.com/j/{SHORTCODE}
        if "j" in parts and parts.index("j") + 1 < len(parts):
            i = parts.index("j")
            return ("workable", parts[0].lower() if i >= 1 else "", parts[i + 1].upper())
        if parts and parts[0] not in ("api", "j"):
            return ("workable", parts[0].lower(), "")
    return None


_WORKDAY_HOST = re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com")


def ats_key(ats: str, job_id: str) -> str:
    """Greenhouse numeric ids and Lever/Ashby UUIDs are unique per ATS, so the board is not needed."""
    return f"{ats}:{job_id}" if ats and job_id else ""


# ---------------------------------------------------------------- location
US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
    "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
    "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york", "NC": "north carolina",
    "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode island", "SC": "south carolina", "SD": "south dakota", "TN": "tennessee", "TX": "texas",
    "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
}
_US_CITIES = (
    "san francisco|new york|nyc|seattle|austin|boston|chicago|los angeles|san jose|palo alto|mountain view|"
    "sunnyvale|menlo park|redwood city|san mateo|santa clara|cupertino|oakland|berkeley|san diego|denver|boulder|"
    "atlanta|miami|dallas|houston|phoenix|portland|philadelphia|pittsburgh|washington dc|washington d c|"
    "arlington|reston|mclean|raleigh|durham|charlotte|nashville|minneapolis|detroit|salt lake city|bellevue|"
    "redmond|kirkland|irvine|santa monica|culver city|brooklyn|jersey city|cambridge ma|bay area|silicon valley|"
    "south san francisco|foster city|costa mesa|el segundo|huntsville|colorado springs|st louis|kansas city|"
    "columbus|indianapolis|cincinnati|cleveland|tampa|orlando|las vegas|sacramento|honolulu|baltimore"
)
_US_TOKEN = re.compile(
    r"\b(united states( of america)?|u\.s\.a?\.?|usa|us[- ]based|us[- ]remote|remote[- ]us|us only|"
    r"anywhere in the us|north america \(us\))\b|\bUS\b|\(US\)|\bU\.S\b",
)
_US_TOKEN_CI = re.compile(r"\b(united states( of america)?|u\.s\.a?\.?|usa|us[- ]based|us[- ]remote|remote[- ]us)\b", re.I)
_FOREIGN = re.compile(
    r"\b(canada|united kingdom|uk|england|scotland|wales|ireland|germany|deutschland|france|spain|portugal|italy|"
    r"netherlands|belgium|switzerland|austria|sweden|norway|denmark|finland|poland|czech|czechia|romania|"
    r"bulgaria|hungary|greece|ukraine|serbia|croatia|estonia|latvia|lithuania|luxembourg|turkey|israel|"
    r"india|china|japan|korea|singapore|malaysia|indonesia|thailand|vietnam|philippines|taiwan|hong kong|"
    r"australia|new zealand|brazil|mexico|argentina|chile|colombia|peru|costa rica|uruguay|"
    r"south africa|nigeria|kenya|egypt|uae|united arab emirates|saudi arabia|qatar|pakistan|bangladesh|sri lanka|"
    r"emea|apac|latam|europe|european union|eu|asia|africa|middle east|"
    r"london|dublin|berlin|munich|hamburg|paris|amsterdam|madrid|barcelona|lisbon|zurich|geneva|stockholm|"
    r"copenhagen|oslo|helsinki|warsaw|krakow|prague|vienna|bucharest|budapest|tel aviv|bangalore|bengaluru|"
    r"hyderabad|mumbai|pune|gurgaon|gurugram|noida|chennai|new delhi|delhi|beijing|shanghai|shenzhen|tokyo|"
    r"osaka|seoul|sydney|melbourne|brisbane|auckland|toronto|vancouver|montreal|ottawa|calgary|waterloo|"
    r"ontario|quebec|british columbia|alberta|sao paulo|são paulo|mexico city|guadalajara|bogota|bogotá|"
    r"buenos aires|santiago|dubai|abu dhabi|riyadh|doha|cairo|lagos|nairobi|cape town|johannesburg|"
    r"manila|jakarta|bangkok|kuala lumpur|ho chi minh|hanoi|taipei|milan|rome|brussels|edinburgh|manchester|"
    r"cork|belfast|luxembourg city|tallinn|riga|vilnius|kyiv|belgrade|zagreb|sofia|athens|istanbul)\b",
    re.I,
)
_STATE_ABBR = re.compile(r"(?:,\s*|-\s+|\(\s*|^\s*)(" + "|".join(US_STATES) + r")\b(?!\.)")
_STATE_NAME = re.compile(r"\b(" + "|".join(n for n in US_STATES.values()) + r")\b", re.I)
_US_CITY = re.compile(r"\b(" + _US_CITIES + r")\b", re.I)
_SPLIT = re.compile(r"\s*(?:;|\||\n|/|•|·)\s*|\s+(?:or|and)\s+")
_US_COUNTRY_CODES = {"us", "usa", "united states", "united states of america", "u.s.", "u.s.a."}


def classify_location(segment: str) -> str:
    """'us', 'foreign' or 'unknown' for one location segment."""
    s = (segment or "").strip()
    if not s:
        return "unknown"
    if _US_TOKEN.search(s) or _US_TOKEN_CI.search(s) or re.search(r"\bnew mexico\b", s, re.I):
        return "us"
    foreign = _FOREIGN.search(s)
    if foreign:
        # "Paris, TX" style: an unambiguous US state abbreviation after a foreign-looking city.
        m = _STATE_ABBR.search(s)
        if m and m.group(1) in {"TX", "NY", "NJ", "OH", "FL", "WA", "VA", "NC", "TN", "KY", "MN", "WI", "MI", "OK", "KS", "UT", "NV", "AZ", "NM", "OR", "IA", "MO", "CT", "RI", "NH", "VT", "WV", "WY", "ND", "SD", "HI", "AK", "MS", "GA", "IL", "MA", "MD", "PA", "CO", "SC", "LA", "AL", "AR", "ID", "MT", "NE", "ME"} \
                and not re.search(r"\b(canada|uk|india|germany|france)\b", s, re.I):
            return "us"
        return "foreign"
    if _STATE_ABBR.search(s) or _STATE_NAME.search(s) or _US_CITY.search(s):
        return "us"
    return "unknown"


def us_location_status(location: str, country_hint: str = "", description: str = "") -> tuple[bool, str]:
    """Decide whether a job is confirmed to be in / eligible from the United States.

    Returns (is_us, reason). Unknown or worldwide-remote locations are NOT accepted unless the
    posting itself confirms US eligibility.
    """
    hint = (country_hint or "").strip().lower()
    segments = [s for s in _SPLIT.split(location or "") if s.strip()]
    kinds = [classify_location(s) for s in segments]
    if "us" in kinds:
        return True, "US location listed"
    if hint in _US_COUNTRY_CODES and "foreign" not in kinds:
        return True, "Source lists country as United States"
    if "foreign" in kinds:
        return False, "Located outside the United States"
    if hint and hint not in _US_COUNTRY_CODES:
        return False, "Source lists a non-US country"
    # Unknown location (e.g. plain "Remote"): require explicit US eligibility in the posting text.
    head = (description or "")[:4000]
    if re.search(r"\b(remote|located|based|reside|residents?|work(ing)? from)\b[^.]{0,60}\b(united states|u\.s\.|usa|\bUS\b)", head, re.I) \
            or re.search(r"\b(united states|u\.s\.|usa)\b[^.]{0,40}\b(remote|only|based)\b", head, re.I):
        return True, "Posting text confirms US eligibility"
    return False, "US eligibility not confirmed"


def detect_work_mode(*values: str) -> str:
    text = " ".join(v for v in values if v).lower()
    if not text:
        return "Not listed"
    if re.search(r"\bhybrid\b", text):
        return "Hybrid"
    if re.search(r"\b(remote|work from home|wfh|distributed)\b", text):
        return "Remote"
    if re.search(r"\b(on[- ]?site|onsite|in[- ]office|in[- ]person)\b", text):
        return "On-site"
    return "Not listed"


# ---------------------------------------------------------------- authorization language
_OR_RESIDENT = re.compile(
    r"\b(or|/|and)\s+(a\s+|an\s+)?(u\.?s\.?\s+)?(lawful\s+|legal\s+)?(permanent\s+resident|green[- ]card|national|"
    r"u\.?s\.?\s+person|asylee|refugee)", re.I)
_US = r"(?:u\.?\s?s\.?(?:a\.?)?|united\s+states)"
_CITIZEN_REQUIRED = [
    re.compile(rf"\b(must|required\s+to|need(?:s)?\s+to|shall|has\s+to|have\s+to)\s+(?:be|hold|have|possess)\s+(?:a\s+|an\s+)?{_US}\s+citizen", re.I),
    re.compile(rf"\b{_US}\s+citizenship\s+(?:is\s+)?(?:strictly\s+)?(required|mandatory|needed|a\s+requirement|a\s+must)", re.I),
    re.compile(rf"\b(requires?|requiring|required:?|requirement:?)\s+(?:proof\s+of\s+)?{_US}\s+citizenship", re.I),
    re.compile(rf"\b{_US}\s+citizens?\s+only\b", re.I),
    re.compile(rf"\bonly\s+{_US}\s+citizens", re.I),
    re.compile(rf"\b(limited|restricted|open)\s+(?:only\s+)?to\s+{_US}\s+citizens", re.I),
    re.compile(rf"\bcitizenship(?:\s+requirement)?\s*[:\-]\s*{_US}(?:\s+citizen(?:ship)?)?(?:\s+required)?", re.I),
    re.compile(rf"\bmust\s+be\s+(?:a\s+)?citizen\s+of\s+the\s+{_US}", re.I),
]
_CITIZEN_NOT_REQUIRED = [
    re.compile(rf"\b{_US}\s+person\b", re.I),
    re.compile(rf"\b{_US}\s+citizens?\s+(?:or|/|and)\s+(?:a\s+)?(?:u\.?s\.?\s+)?(?:lawful\s+|legal\s+)?(permanent\s+resident|green[- ]card|national)", re.I),
    re.compile(r"\bcitizenship\s+is\s+not\s+required", re.I),
]
_CLEARANCE = [
    re.compile(r"\b(active|current|existing|must\s+(?:have|hold|possess|obtain|be\s+able\s+to\s+obtain)|ability\s+to\s+obtain|"
               r"able\s+to\s+obtain|eligib\w+\s+(?:for|to\s+obtain)|requires?|required)\b[^.]{0,70}\b"
               r"(security\s+clearance|secret\s+clearance|top\s+secret|ts/sci|dod\s+clearance|q\s+clearance)", re.I),
    re.compile(r"\b(ts/sci|top\s+secret)\b[^.]{0,40}\b(required|clearance)", re.I),
    re.compile(r"\b(security\s+clearance)\s+(?:is\s+)?(required|needed|mandatory)", re.I),
]
_SPONSOR_NO = [
    re.compile(r"\b(unable|not\s+able|cannot|can\s?not|can't|won't|will\s+not|do(?:es)?\s+not|don't|doesn't|not)\b[^.]{0,60}\bsponsor", re.I),
    re.compile(r"\bno\s+(visa\s+|immigration\s+|employment\s+|work\s+)?sponsorship", re.I),
    re.compile(r"\bsponsorship\s+(?:is\s+|will\s+)?(?:not|unavailable)", re.I),
    re.compile(r"\bwithout\s+(?:the\s+)?(?:need\s+for\s+|requiring\s+|requirement\s+for\s+)?(?:current\s+or\s+future\s+)?[^.]{0,30}sponsorship", re.I),
    re.compile(r"\bnot\s+eligible\s+for\s+[^.]{0,30}sponsorship", re.I),
]
_SPONSOR_YES = [
    re.compile(r"\b(visa\s+)?sponsorship\s+(?:is\s+)?(available|offered|provided|possible)", re.I),
    re.compile(r"\b(we|will|do|does|can|able\s+to|happy\s+to)\s+(?:do\s+)?(?:offer\s+|provide\s+)?sponsor(?:ship)?\b", re.I),
    re.compile(r"\bsponsor\s+(?:work\s+|h-?1b\s+|employment\s+)?visas?\b", re.I),
    re.compile(r"\b(offers?|provides?|support(?:s)?)\s+(?:visa\s+|immigration\s+|h-?1b\s+)sponsorship", re.I),
]
_AUTH_SENTENCE = re.compile(
    r"[^.!?\n]{0,220}\b(citizen|citizenship|clearance|sponsor\w*|visa|work authorization|authorized to work|"
    r"u\.?s\.? person|permanent resident|green card|itar|export control)\b[^.!?\n]{0,220}", re.I)


def authorization_snippets(description: str, limit: int = 4) -> list[str]:
    out, seen = [], set()
    for m in _AUTH_SENTENCE.finditer(description or ""):
        s = _WS.sub(" ", m.group(0)).strip()
        if s.lower() not in seen:
            seen.add(s.lower())
            out.append(s[:400])
        if len(out) >= limit:
            break
    return out


def analyze_authorization(description: str) -> dict:
    """Extract only what the posting explicitly states. Unknown stays 'Not stated'."""
    text = description or ""
    citizenship = "Not stated"
    for rx in _CITIZEN_REQUIRED:
        hit = None
        for m in rx.finditer(text):
            tail = text[m.end(): m.end() + 90]
            if _OR_RESIDENT.search(tail):
                citizenship = "Not required"      # "US citizen or permanent resident" is not citizen-only
                continue
            hit = m
            break
        if hit:
            citizenship = "Required"
            break
    if citizenship == "Not stated" and any(rx.search(text) for rx in _CITIZEN_NOT_REQUIRED):
        citizenship = "Not required"
    clearance = any(rx.search(text) for rx in _CLEARANCE)
    sponsorship = "Not stated"
    if any(rx.search(text) for rx in _SPONSOR_NO):
        sponsorship = "Not available"
    elif any(rx.search(text) for rx in _SPONSOR_YES):
        sponsorship = "Available"
    return {"citizenship_requirement": citizenship, "clearance_required": clearance, "sponsorship": sponsorship}


# ---------------------------------------------------------------- experience / salary
_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*)?(?:years?|yrs?)\b", re.I)


def extract_experience(description: str) -> tuple[float | None, str]:
    text = description or ""
    for m in _YEARS.finditer(text):
        low = int(m.group(1))
        if low > 25:
            continue
        window = text[max(0, m.start() - 80): m.end() + 100].lower()
        if "experience" not in window and "exp." not in window:
            continue
        if re.search(r"\b(founded|for over|company|history|since)\b", window[:80]) and "experience" not in window[80:]:
            continue
        high = m.group(2)
        label = f"{low}-{high} years" if high else f"{low}+ years"
        return float(low), label
    return None, "Not listed"


_SALARY = re.compile(
    r"\$\s?\d{2,3}(?:,\d{3})+(?:\.\d{2})?\s*(?:-|–|—|to)\s*\$?\s?\d{2,3}(?:,\d{3})+(?:\.\d{2})?"
    r"|\$\s?\d{2,3}(?:\.\d)?\s?[kK]\s*(?:-|–|—|to)\s*\$?\s?\d{2,3}(?:\.\d)?\s?[kK]")


def extract_salary(description: str) -> str:
    m = _SALARY.search(description or "")
    return _WS.sub(" ", m.group(0)).strip() if m else "Not listed"


SENIORITY_YEARS = [
    (r"\b(intern|internship|new grad|graduate|entry[- ]level|apprentice)\b", 0),
    (r"\b(junior|jr\.?|associate)\b", 1),
    (r"\b(principal|distinguished|fellow)\b", 10),
    (r"\b(staff)\b", 8),
    (r"\b(director|head of|vp|vice president|chief)\b", 10),
    (r"\b(senior|sr\.?|lead)\b", 5),
]


def seniority_years(title: str) -> float | None:
    t = (title or "").lower()
    for rx, years in SENIORITY_YEARS:
        if re.search(rx, t):
            return float(years)
    return None


def pretty_slug(slug: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[-_]+", slug or "") if w) or slug


ENRICH_VERSION = 4      # raise when enrich_job extracts something new, so stored postings are re-analysed


def enrich_job(job) -> None:
    """Lazy per-posting analysis. Run only for postings that survive the cheap title filter."""
    if job.raw_html:
        job.description = html_to_text(job.raw_html)
        job.description_rich = html_to_structured_text(job.raw_html)[:40000]     # for display: keeps paragraphs and bullets
        job.raw_html = ""
    if job.enriched >= ENRICH_VERSION:
        return
    job.enriched = ENRICH_VERSION
    job.description = job.description[:20000]
    for field, value in analyze_authorization(job.description).items():
        setattr(job, field, value)
    if job.required_years is None:
        job.required_years, job.experience_required = extract_experience(job.description)
    if job.salary == "Not listed":
        job.salary = extract_salary(job.description)
    from .resume_parser import find_skills      # local import: resume_parser is the heavier module
    job.skills = find_skills(f"{job.title}. {job.description}")
