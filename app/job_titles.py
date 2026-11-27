"""Job titles as people type them into a search, made from the titles employers write on postings.

A posting title carries more than the job's name: a requisition number, a place, "(Remote)", a team, a level.
`clean_title` keeps the name ("Senior Software Engineer - Platform (Remote) R-10234" -> "Senior Software Engineer")
or returns None when what is left is not a usable title. `TitleVocabulary` holds the most frequent cleaned titles of
the job index and answers "which titles match what was typed so far" from memory, without a database query.

Nothing here invents a title: every suggestion is a title that postings in the index really carry, cleaned."""
import html
import re
from collections import Counter, defaultdict

MAX_TITLE_CHARS = 60
MAX_TITLE_WORDS = 6
MIN_QUERY_CHARS = 2

_BRACKETS = re.compile(r"\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\}")
_OPEN_BRACKET = re.compile(r"[(\[{].*$")                              # an unclosed "(Remote" at the end
_SEPARATOR = re.compile(r"\s+[-–—|@•·:]+\s+|\s*[|•·]\s*|\s+[-–—]+$|:\s+")
# R-10234, REQ 5521, JR100234, Job ID: 4471, #2291, 2026-0412, and any bare run of four or more digits.
_REQUISITION = re.compile(r"(?i)(?:\b(?:req(?:uisition)?|job\s*id|job|jr|ref|id|r)\s*[#:._-]?\s*\d{3,}[\w-]*|#\s*\d+[\w-]*|\b\d{4,}[\w-]*)")
_SPACES = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9+#]+(?:[.'&/-][a-z0-9+#]+)*")
_NOT_A_TITLE = re.compile(r"(?i)(https?:|www\.|[$€£!?*=<>@]|\b(?:hiring|urgent(?:ly)?|bonus|per\s+hour|/\s*hr|apply|vacanc(?:y|ies)|opening|opportunit(?:y|ies)|"
                          r"talent\s+(?:pool|community|network)|general\s+application|join\s+(?:us|our))\b|\s/\s|\bor\b|\band/or\b)")

# Words that say where, how or for how long, never what the job is. Dropped from the ends of a title.
_EDGE_NOISE = {
    "remote", "hybrid", "onsite", "on-site", "wfh", "telecommute", "us", "usa", "u.s.", "u.s", "united", "states", "america",
    "contract", "contractor", "contract-to-hire", "c2h", "c2c", "w2", "1099", "temp", "temporary", "seasonal", "part-time",
    "full-time", "parttime", "fulltime", "ft", "pt", "nights", "days", "evenings", "weekends", "prn", "new", "grad", "summer",
    "fall", "winter", "spring", "i", "ii", "iii", "iv", "v", "1", "2", "3", "4", "5", "based", "only", "in", "at", "for", "the", "of", "and",
}
_COUNTRIES = {"united states", "usa", "us", "u.s.", "canada", "uk", "united kingdom", "india", "mexico", "germany", "france", "ireland",
              "australia", "japan", "singapore", "brazil", "spain", "italy", "netherlands", "poland", "israel", "china", "emea", "apac",
              "latam", "amer", "americas", "europe", "north america", "nationwide", "worldwide", "global", "anywhere"}
_US_STATES = {"al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma",
              "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn",
              "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc"}
_STATE_NAMES = {"alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida", "georgia",
                "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts",
                "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
                "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
                "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
                "wisconsin", "wyoming"}
# A first part made only of these is a rank, and the part after the comma says what of: "Director, Product Management".
_RANK = {"senior", "sr", "junior", "jr", "lead", "principal", "staff", "associate", "assistant", "director", "manager", "head", "vp",
         "svp", "evp", "avp", "vice", "president", "chief", "executive", "managing", "group", "global", "regional", "deputy", "general",
         "supervisor", "coordinator", "specialist", "consultant", "officer", "partner"}
_HEADS = {"director", "manager", "head", "vp", "svp", "evp", "avp", "president", "lead", "chief", "supervisor", "officer", "partner"}
_ACRONYMS = {"ai", "ml", "qa", "ui", "ux", "it", "hr", "vp", "svp", "evp", "avp", "ceo", "cto", "cfo", "coo", "cio", "ciso", "rn", "lpn",
             "cna", "sql", "aws", "gcp", "sap", "erp", "crm", "sre", "php", "api", "bi", "etl", "nlp", "llm", "sdet", "pmo", "seo", "sem",
             "cad", "plc", "hvac", "cdl", "emt", "np", "pa", "md", "dba", "sdr", "bdr", "gtm", "saas", "b2b", "b2c", "ios", "qc", "ehs",
             "fp&a", "r&d", "m&a", "devops", "mlops", "ii", "iii", "iv", "kyc", "aml", "gis", "bim", "vfx", "cnc", "csm"}
_SPELLED = {"devops": "DevOps", "mlops": "MLOps", "ios": "iOS", "saas": "SaaS", "fp&a": "FP&A", "r&d": "R&D", "m&a": "M&A"}
_SMALL = {"of", "and", "for", "to", "in", "the", "a", "an", "on", "at", "with", "or"}
_LONG_FORM = {"sr": "Senior", "sr.": "Senior", "jr": "Junior", "jr.": "Junior"}


def _is_place(part: str) -> bool:
    words = part.lower().strip(" .").split()
    text = " ".join(words)
    if not words:
        return True
    if text in _COUNTRIES or text in _STATE_NAMES or text in _US_STATES or all(w in _EDGE_NOISE for w in words):
        return True
    return len(words) >= 2 and words[-1] in _US_STATES                 # "Austin TX"


def _strip_edges(words: list[str]) -> list[str]:
    while words and words[-1].lower().strip(".,;") in _EDGE_NOISE:
        words = words[:-1]
    while words and words[0].lower().strip(".,;") in {"remote", "hybrid", "onsite", "on-site", "contract", "temporary", "seasonal", "the", "a"}:
        words = words[1:]
    return words


def _cased(title: str) -> str:
    """Titles written all in capitals or all in small letters get ordinary title casing; anything else is kept as
    the employers wrote it. "Sr." and "Jr." are written out."""
    words = title.split()
    shout = title.upper() == title or title.lower() == title
    out = []
    for position, word in enumerate(words):
        bare = word.lower().strip(".,")
        if word.lower() in _LONG_FORM:
            out.append(_LONG_FORM[word.lower()])
        elif not shout:
            out.append(word)
        elif bare in _SPELLED:
            out.append(_SPELLED[bare])
        elif bare in _ACRONYMS:
            out.append(word.upper())
        elif bare in _SMALL and position:
            out.append(word.lower())
        else:
            out.append("-".join(piece[:1].upper() + piece[1:].lower() for piece in word.split("-")))
    return " ".join(out)


def clean_title(raw) -> str | None:
    """The job's name out of a posting title, or None when no usable name is left."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 300:
        return None
    text = html.unescape(raw).replace(" ", " ")
    for _ in range(3):                                                # nested brackets
        text = _BRACKETS.sub(" ", text)
    text = _OPEN_BRACKET.sub(" ", text)
    text = _REQUISITION.sub(" ", text)
    text = _SPACES.sub(" ", text).strip(" -–—|,;:./")
    head = next((part for part in _SEPARATOR.split(text) if part and part.strip(" -–—|,;:./")), "")
    parts = [part.strip() for part in head.split(",") if part.strip()]
    while len(parts) > 1 and _is_place(parts[-1]):                     # "..., Austin, TX" / "..., Remote"
        state = parts[-1].lower().strip(" .") in _US_STATES
        parts = parts[:-1]
        if state and len(parts) > 1:                                   # the city in front of the state
            parts = parts[:-1]
    if not parts:
        return None
    first = [w.lower().strip(".") for w in parts[0].split()]
    if len(parts) > 1 and all(w in _RANK for w in first) and first[-1] in _HEADS:
        title = f"{parts[0]}, {parts[1]}"                              # "Director, Product Management"
    else:
        title = parts[0]                                               # "Software Engineer, Compute Infrastructure" -> the job
    words = _strip_edges(title.split())
    title = " ".join(words).strip(" -–—|,;:./")
    if not (3 <= len(title) <= MAX_TITLE_CHARS) or len(words) > MAX_TITLE_WORDS:
        return None
    if _NOT_A_TITLE.search(title) or not re.search(r"[A-Za-z]{2}", title) or re.search(r"\d{3,}", title):
        return None
    bare = [w.lower().strip(".,") for w in words]
    if all(w in _EDGE_NOISE for w in bare) or (len(bare) == 1 and bare[0] in _RANK):     # "Manager" alone is a rank, not a job
        return None
    return _cased(title)


def title_key(title: str) -> str:
    """What makes two spellings the same title: the words in small letters, "Sr." read as "senior"."""
    words = [{"sr": "senior", "jr": "junior"}.get(w, w) for w in _WORD.findall(title.lower().replace("&", " and "))]
    return " ".join(words)


def _query_words(text: str) -> list[str]:
    return [{"sr": "senior", "jr": "junior"}.get(w, w) for w in _WORD.findall((text or "").lower().replace("&", " and "))][:8]


def matches(key_words: list[str], query: list[str]) -> bool:
    """Every typed word starts a word of the title, each a different one, in any order."""
    used: set[int] = set()
    for typed in query:
        hit = next((i for i, word in enumerate(key_words) if i not in used and word.startswith(typed)), None)
        if hit is None:
            return False
        used.add(hit)
    return True


class TitleVocabulary:
    """The most frequent cleaned titles of the job index, ready to be matched against typed text."""

    def __init__(self, counted=(), limit: int = 20000):
        """`counted`: (posting title, how many postings carry it) pairs, in any order."""
        totals: Counter[str] = Counter()
        spellings: dict[str, Counter[str]] = defaultdict(Counter)
        self.read = 0
        for raw, count in counted:
            self.read += 1
            title = clean_title(raw)
            if title is None:
                continue
            key = title_key(title)
            if not key:
                continue
            totals[key] += int(count)
            spellings[key][title] += int(count)
        ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))[:max(0, limit)]
        # (title as most postings spell it, its words, number of postings); the position is the rank by frequency.
        self.entries: list[tuple[str, list[str], int]] = [
            (max(spellings[key].items(), key=lambda item: (item[1], item[0]))[0], key.split(), count) for key, count in ranked]
        self._by_prefix: dict[str, list[int]] = defaultdict(list)
        for position, (_, words, _) in enumerate(self.entries):
            for prefix in {word[:MIN_QUERY_CHARS] for word in words}:
                self._by_prefix[prefix].append(position)

    def __len__(self) -> int:
        return len(self.entries)

    def suggest(self, text: str, limit: int = 8, exclude=()) -> list[str]:
        """Up to `limit` titles matching the typed text: those that begin with it first, then by how many postings
        carry them. `exclude`: titles (any spelling) that are already offered or chosen."""
        query = _query_words(text)
        if not query or len("".join(query)) < MIN_QUERY_CHARS or limit <= 0:
            return []
        longest = max(query, key=len)
        if len(longest) < MIN_QUERY_CHARS:
            return []
        skip = {title_key(t) for t in exclude}
        typed = " ".join(query)
        found: list[tuple[int, int, str]] = []
        for position in self._by_prefix.get(longest[:MIN_QUERY_CHARS], ()):
            title, words, _ = self.entries[position]
            if not matches(words, query):
                continue
            key = " ".join(words)
            if key in skip:
                continue
            found.append((0 if key.startswith(typed) else 1, position, title))
        found.sort()
        return [title for _, _, title in found[:limit]]


# ---------------------------------------------------------------- titles a resume names
_RESUME_FRAGMENT = re.compile(r"[,;:|•·()\[\]\t]|\s[-–—]+\s|\.\s|\s(?:at|@|with|for)\s")
_RESUME_TITLE = re.compile(
    r"\b((?:[A-Z][A-Za-z/&+#-]*[ ]){0,3}(?:Engineer|Developer|Scientist|Analyst|Architect|Researcher|Manager|Consultant|Administrator|"
    r"Designer|Specialist|Technician|Programmer|Coordinator|Recruiter|Accountant|Nurse|Intern))\b")
_RESUME_HEADINGS = {"experience", "summary", "skills", "education", "objective", "profile", "employment", "work", "professional", "career",
                    "current", "previous", "former", "title", "position", "role"}


def resume_titles(resume_text: str, person_name: str = "", cap: int = 6) -> list[str]:
    """Job titles a resume names, read line by line. A match never runs across a line break, a sentence end or a
    comma, and words of the person's own name are never part of a title: a resume opens with the name, and the
    line after it is very often a job title."""
    name = {word.lower() for word in re.findall(r"[A-Za-z]+", person_name or "")}
    out: dict[str, str] = {}
    for line in (resume_text or "")[:200_000].splitlines():
        for fragment in _RESUME_FRAGMENT.split(line):
            for match in _RESUME_TITLE.finditer(fragment):
                words = match.group(1).split()
                while len(words) > 1 and (words[0].lower() in name or words[0].lower() in _RESUME_HEADINGS):
                    words = words[1:]
                title = clean_title(" ".join(words))
                if title and len(title.split()) >= 2 and not (name & {w.lower() for w in title.split()}):
                    out.setdefault(title_key(title), title)
                if len(out) >= cap:
                    return list(out.values())
    return list(out.values())
