"""How old a posting is, and whether a search may deliver it.

A search has a posting-age limit (`SearchRequest.posted_within_days`: 0 = any time, else 1, 3, 7, 14 or 30 days;
default 7) and says whether postings without a known date are delivered (`include_undated`). The rule:

- a posting whose known posting date is older than the limit is never delivered (`TOO_OLD`);
- a posting with no known date (`UNDATED`) is delivered only with `include_undated`, after dated postings of the
  same score, and is shown as "Posting date not stated";
- nothing here ever invents a date. A date comes from the source that published the posting: a board API's publish
  date, a page's schema.org `datePosted`, or words the page itself states ("Posted 3 days ago", "Date posted:
  November 20, 2026"). A date in the future or absurdly long ago is treated as unknown.

Two fields of `Job` carry what is known:
- `posted_at`: the day the posting was published (ISO date), "" when unknown;
- `posted_before`: only where the source gives no publish date but does say that the posting is at least this old
  (Workday's "Posted 30+ Days Ago", "Posted 2 weeks ago", or a "last updated" date, which is never earlier than
  the publish date). It can prove a posting too old; it never makes a posting look new.

Days are calendar days. "Today" is the day in the last US time zone (UTC-10): a posting dated the 20th is within a
7-day limit through the whole of the 27th everywhere in the United States, whatever the server's clock says."""
import re
from datetime import date, datetime, timedelta, timezone

from .deduplicator import job_keys

POSTED_WITHIN_CHOICES = (0, 1, 3, 7, 14, 30)      # 0 = any time
DEFAULT_POSTED_WITHIN_DAYS = 7
MAX_PLAUSIBLE_AGE_DAYS = 3650                     # a date further back is a placeholder (1970-01-01), not a posting date
FUTURE_GRACE_DAYS = 1                             # a date may be one day ahead of UTC (time zones east of it)
LAST_US_ZONE = timezone(timedelta(hours=-10))     # Hawaii: the last place in the United States where a day ends
FRESH, TOO_OLD, UNDATED = "fresh", "too_old", "undated"


def within_days(value, default: int = DEFAULT_POSTED_WITHIN_DAYS) -> int:
    """A stored or submitted limit as one of POSTED_WITHIN_CHOICES. A search or preset stored before the limit
    existed has no value (None) and gets the default; so does anything that is not one of the choices."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = int(str(value).strip())
    except ValueError:
        return default
    return number if number in POSTED_WITHIN_CHOICES else default


def _now(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def today(now: datetime | None = None) -> date:
    """The reference day of every age: the calendar day in the last US time zone."""
    return _now(now).astimezone(LAST_US_ZONE).date()


def known_date(value, now: datetime | None = None) -> date | None:
    """An ISO date as a day, or None when it is not a date, lies in the future or is absurdly long ago."""
    text = str(value or "").strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return None
    moment = _now(now)
    if day > moment.astimezone(timezone.utc).date() + timedelta(days=FUTURE_GRACE_DAYS):
        return None
    if (today(moment) - day).days > MAX_PLAUSIBLE_AGE_DAYS:
        return None
    return day


def clean_date(value, now: datetime | None = None) -> str:
    """`value` as an ISO date when it is a plausible posting date, else ""."""
    day = known_date(value, now)
    return day.isoformat() if day else ""


def age_days(job, now: datetime | None = None) -> int | None:
    """Days since the posting was published (never negative), None when no publish date is known."""
    day = known_date(getattr(job, "posted_at", ""), now)
    return None if day is None else max(0, (today(now) - day).days)


def least_age_days(job, now: datetime | None = None) -> int | None:
    """The least the posting can be old, from `posted_before`; None when the source says nothing of the kind."""
    day = known_date(getattr(job, "posted_before", ""), now)
    return None if day is None else max(0, (today(now) - day).days)


def age_class(job, limit: int, now: datetime | None = None) -> str:
    """FRESH, TOO_OLD or UNDATED for a limit in days (0 = any time: nothing is too old)."""
    age = age_days(job, now)
    if age is not None:
        return TOO_OLD if limit and age > limit else FRESH
    least = least_age_days(job, now)
    if limit and least is not None and least > limit:
        return TOO_OLD                            # no exact date, but the source says it is older than the limit
    return UNDATED


def undated(job, now: datetime | None = None) -> bool:
    """Whether the posting has no known publish date (the tie-break of the ranking and the label of the job card)."""
    return known_date(getattr(job, "posted_at", ""), now) is None


# ---------------------------------------------------------------- dates a source states in words
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                 "nine": 9, "ten": 10}
_COUNT = r"(\d{1,3}|an?|one|two|three|four|five|six|seven|eight|nine|ten)"
# "Posted 3 Days Ago", "Posted Today", "Posted 30+ Days Ago", "Posted 2 weeks ago". The word "posted" (or "published",
# "listed") is required: "3 days ago" alone may be about anything on the page.
_RELATIVE = re.compile(
    r"(?i)\b(?:posted|published|listed|date posted|posting date|posted on|posted date)\s*:?\s*(?:on\s+)?(?:about\s+)?(?P<over>over\s+|more than\s+|at least\s+)?(?:"
    r"(?P<today>today|just now|moments? ago|(?:less than\s+)?" + _COUNT.replace("(", "(?:") + r"\s+(?:hours?|hrs?|minutes?|mins?)\s+ago)"
    r"|(?P<yesterday>yesterday)"
    r"|(?P<count>" + _COUNT[1:-1] + r")\s*(?P<plus>\+)?\s*(?P<unit>days?|weeks?|months?|years?)\s+ago)")
_JUST_POSTED = re.compile(r"(?i)\bjust posted\b")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
_MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
_LABEL = (r"(?:date\s+posted|posted\s+date|posting\s+date|date\s+of\s+posting|job\s+posted(?:\s+on)?|posted\s+on|posted|date\s+published|"
          r"published\s+on|published|first\s+posted|opening\s+date|date\s+opened|post\s+date)")
_ABSOLUTE = re.compile(
    r"(?i)\b" + _LABEL + r"\s*[:\-–—|]?\s*(?:on\s+)?(?:"
    r"(?P<iso>\d{4}-\d{2}-\d{2})"
    r"|(?P<mdy>" + _MONTH + r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})"
    r"|(?P<dmy>\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r",?\s+\d{4})"
    r"|(?P<num>\d{1,2}[/.]\d{1,2}[/.]\d{4}))")
STATED_TEXT_CHARS = 6000                          # how much of a page's readable text is searched for its posting date


def _month_number(word: str) -> int:
    key = word.lower().rstrip(".")[:3]
    return next((i for i, name in enumerate(_MONTHS, 1) if name.startswith(key)), 0)


def _named_date(text: str) -> date | None:
    parts = re.findall(r"[A-Za-z]+|\d+", text)
    month = next((_month_number(p) for p in parts if p.isalpha() and _month_number(p)), 0)
    numbers = [int(p) for p in parts if p.isdigit()]
    year = next((n for n in numbers if n >= 1000), 0)
    day = next((n for n in numbers if n < 1000), 0)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _numeric_date(text: str) -> date | None:
    """11/20/2026 or 20.11.2026: read only when it can be read one way (a part above 12 is the day). 05/06/2026 is
    the fifth of June for some and the sixth of May for others, so it is not a known date."""
    a, b, year = (int(n) for n in re.split(r"[/.]", text))
    if a > 12 >= b:
        day, month = a, b
    elif b > 12 >= a:
        day, month = b, a
    elif a == b:
        day, month = a, b
    else:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _count(word: str) -> int:
    return int(word) if word.isdigit() else _NUMBER_WORDS.get(word.lower(), 0)


def stated_posting(text, now: datetime | None = None) -> tuple[str, str]:
    """(posted_at, posted_before) from words a source states about ONE posting, both ISO dates or "".

    "Posted Today" / "Posted 3 Days Ago" / "Date posted: November 20, 2026" give the publish date. "Posted 30+ Days
    Ago", "Posted 2 weeks ago" or "Posted 3 months ago" do not say the day, only that the posting is at least that
    old: they give `posted_before`. When the text states different dates (the page lists other jobs with theirs),
    nothing is taken: which of them belongs to this posting would be a guess."""
    text = str(text or "")
    ref = today(now)
    exact: set[date] = set()
    least: set[date] = set()
    if _JUST_POSTED.search(text):
        exact.add(ref)
    for m in _RELATIVE.finditer(text):
        if m.group("today"):
            exact.add(ref)
        elif m.group("yesterday"):
            exact.add(ref - timedelta(days=1))
        else:
            n, unit = _count(m.group("count")), m.group("unit").lower()
            if n <= 0:
                continue
            if unit.startswith("day") and not m.group("plus") and not m.group("over"):
                exact.add(ref - timedelta(days=n))
            else:
                days = n * (1 if unit.startswith("day") else 7 if unit.startswith("week") else 30 if unit.startswith("month") else 365)
                least.add(ref - timedelta(days=days))
    for m in _ABSOLUTE.finditer(text):
        if m.group("iso"):
            try:
                day = date.fromisoformat(m.group("iso"))
            except ValueError:
                day = None
        elif m.group("num"):
            day = _numeric_date(m.group("num"))
        else:
            day = _named_date(m.group("mdy") or m.group("dmy"))
        if day is not None:
            exact.add(day)
    if len(exact) == 1 and not least:
        return clean_date(next(iter(exact)).isoformat(), now), ""
    if not exact and len(least) == 1:
        return "", clean_date(next(iter(least)).isoformat(), now)
    return "", ""


def stated_on_page(page_text, now: datetime | None = None) -> tuple[str, str]:
    """`stated_posting` for the start of a page's readable text (its own content comes first, navigation and footers
    last: see job_verifier.page_text)."""
    return stated_posting(str(page_text or "")[:STATED_TEXT_CHARS], now)


# ---------------------------------------------------------------- what copies of one posting know about its age
class AgeWitness:
    """Postings a search screened out as too old, remembered by what identifies them (no posting text is kept).

    The same vacancy often reaches a search twice: from the employer's board API with its publish date, and as a
    copy without one (the employer's own career page, a job board). The dated record is dropped as too old; without
    this the copy would be delivered as "date not stated". A posting with no date of its own, and a job-board copy
    whatever date the job board gives it, is too old when the employer's record of the same vacancy was."""

    def __init__(self) -> None:
        self._keys: dict[str, bool] = {}          # identifying key -> an employer's own record said so (not a job-board copy)
        self._ctl: dict[str, str] = {}            # company|title|location -> ATS key of the old record ("" when it has none)

    @staticmethod
    def keys(job) -> tuple[str, str, str]:
        return job_keys(job)                      # exactly what the duplicate removal compares: ATS key, address, company|title|location

    def add(self, job) -> None:
        akey, url, ctl = self.keys(job)
        own = not getattr(job, "aggregator", False)
        for key in (akey and "a:" + akey, url and "u:" + url):
            if key:
                self._keys[key] = self._keys.get(key, False) or own
        if own or ctl not in self._ctl:
            self._ctl[ctl] = akey
            self._keys["c:" + ctl] = self._keys.get("c:" + ctl, False) or own

    def __len__(self) -> int:
        return len(self._ctl)

    def update(self, other: "AgeWitness") -> None:
        for key, own in other._keys.items():
            self._keys[key] = self._keys.get(key, False) or own
        for ctl, akey in other._ctl.items():
            if ctl not in self._ctl or other._keys.get("c:" + ctl):
                self._ctl[ctl] = akey

    def says_old(self, job, now: datetime | None = None) -> bool:
        """Whether a record of the same vacancy was too old, for a posting that cannot speak for itself: one without
        a date, or a job-board copy (its date is the job board's, the employer's record wins)."""
        if not self._keys:
            return False
        dated = not undated(job, now)
        copy = bool(getattr(job, "aggregator", False))
        if dated and not copy:
            return False                          # an employer's own record with a date of its own
        akey, url, ctl = self.keys(job)
        hits = [self._keys.get("a:" + akey) if akey else None, self._keys.get("u:" + url) if url else None]
        other = self._ctl.get(ctl)
        if other is not None and not (akey and other and akey != other and akey.split(":")[0] == other.split(":")[0]):
            hits.append(self._keys.get("c:" + ctl))     # same employer, title and place; not two requisitions of one ATS
        return any(own or not dated for own in hits if own is not None)


# ---------------------------------------------------------------- discovery: asking search engines for recent results
def time_range(limit: int) -> str:
    """SearXNG's `time_range` that still covers a limit in days ("" = no restriction)."""
    if limit <= 0:
        return ""
    return "day" if limit <= 1 else "week" if limit <= 7 else "month" if limit <= 30 else "year"


def recency_phrase(limit: int) -> str:
    """How a prompt names the limit ("the last 7 days")."""
    return "" if limit <= 0 else "the last 24 hours" if limit == 1 else f"the last {limit} days"


def month_words(limit: int, now: datetime | None = None) -> list[str]:
    """Search words that name the months a posting within the limit can carry ("posted November 2026"): the current
    month, and the one before while the limit reaches back into it. Without a limit: the current month."""
    ref = today(now)
    days = limit if limit > 0 else 0
    months = [ref.replace(day=1)]
    start = ref - timedelta(days=days)
    if (start.year, start.month) != (ref.year, ref.month):
        months.append(start.replace(day=1))
    return ["posted " + m.strftime("%B %Y") for m in months]
