"""Deterministic relevance, eligibility and scoring. AI ranking (when available) replaces the
component scores but is always clamped and checked against resume evidence here."""
import re
from functools import lru_cache

from .job_normalizer import ENRICH_VERSION, seniority_years
from .resume_parser import SKILL_PATTERNS, find_skills
from .schemas import Job, Profile, SearchRequest

WEIGHTS = {"skills": 35, "title": 25, "experience": 20, "arrangement": 10, "additional": 10}

_PHRASES = [
    (r"machine[- ]learning", "ml"), (r"artificial intelligence", "ai"), (r"generative ai|gen ?ai", "ai"),
    (r"large language models?|llms?", "llm"), (r"back[- ]end", "backend"), (r"front[- ]end", "frontend"),
    (r"full[- ]stack", "fullstack"), (r"site reliability( engineer)?|\bsre\b", "sre engineer"), (r"dev[- ]?ops", "devops"),
    (r"\bswe\b|\bsde\b", "software engineer"), (r"data science", "data scientist"), (r"\bsr\.?\b", "senior"),
]
_ROLE_FAMILY = {
    "engineer": "eng", "engineering": "eng", "developer": "eng", "programmer": "eng", "architect": "eng",
    "scientist": "sci", "researcher": "sci", "research": "sci", "analyst": "analyst", "manager": "mgr",
    "director": "mgr", "designer": "design", "administrator": "admin", "consultant": "consult",
    "recruiter": "recruit", "sales": "sales", "executive": "sales", "marketer": "mkt", "writer": "writer",
    "counsel": "legal", "attorney": "legal", "accountant": "fin", "technician": "tech",
}
_STOP = {"senior", "staff", "principal", "lead", "junior", "associate", "ii", "iii", "iv", "i", "the", "of", "and",
         "for", "in", "to", "a", "an", "at", "with", "new", "grad", "intern", "member", "technical", "team", "level",
         "1", "2", "3", "4", "5", "remote", "us", "usa", "hybrid"}
_AFFINITY = {("ai", "ml"): 0.7, ("ml", "ai"): 0.7, ("ai", "llm"): 0.7, ("llm", "ai"): 0.6, ("ml", "llm"): 0.5,
             ("llm", "ml"): 0.5, ("backend", "python"): 0.4, ("python", "backend"): 0.5, ("cloud", "infrastructure"): 0.6,
             ("cloud", "devops"): 0.5, ("cloud", "platform"): 0.4, ("software", "backend"): 0.5, ("software", "fullstack"): 0.5,
             ("software", "frontend"): 0.4, ("data", "analytics"): 0.5}
_OFF_TARGET = re.compile(r"\b(manager|director|head|vp|recruiter|sales|account|marketing|counsel|attorney|"
                         r"accountant|coordinator|assistant|support|success|writer|designer|intern|internship)\b")

RELATED_TITLES = {
    "ai": (["Machine Learning Engineer", "Applied AI Engineer", "AI Software Engineer", "Generative AI Engineer",
            "LLM Engineer", "ML Platform Engineer", "Applied Scientist", "AI Research Engineer"],
           {"Machine Learning", "Deep Learning", "LLM", "PyTorch", "TensorFlow", "NLP", "Generative AI", "scikit-learn"}),
    "ml": (["AI Engineer", "Applied AI Engineer", "ML Platform Engineer", "AI Software Engineer", "Applied Scientist",
            "MLOps Engineer", "Deep Learning Engineer"],
           {"Machine Learning", "Deep Learning", "PyTorch", "TensorFlow", "scikit-learn", "MLOps"}),
    "data scientist": (["Applied Scientist", "Machine Learning Scientist", "Decision Scientist", "Research Scientist, Machine Learning"],
                       {"Statistics", "Machine Learning", "Pandas", "A/B Testing", "scikit-learn"}),
    "python": (["Backend Engineer", "Software Engineer, Backend", "Python Engineer"], {"Python"}),
    "backend": (["Backend Developer", "Software Engineer, Backend", "API Engineer", "Platform Engineer"],
                {"Python", "Java", "Go", "REST APIs", "Microservices", "PostgreSQL", "Node.js", "Distributed Systems"}),
    "software": (["Software Developer", "Backend Engineer", "Full Stack Engineer"],
                 {"Python", "Java", "JavaScript", "TypeScript", "Go", "C++", "C#", "Rust"}),
    "cloud": (["DevOps Engineer", "Site Reliability Engineer", "Infrastructure Engineer", "Platform Engineer"],
              {"AWS", "GCP", "Azure", "Kubernetes", "Terraform", "Docker"}),
}


@lru_cache(maxsize=100_000)
def title_tokens(title: str) -> tuple[str, ...]:
    t = (title or "").lower()
    for rx, rep in _PHRASES:
        t = re.sub(rx, rep, t)
    return tuple(w for w in re.findall(r"[a-z0-9+#]+", t) if w not in _STOP)


@lru_cache(maxsize=200_000)
def title_relevance(job_title: str, target: str) -> float:
    """0..1 similarity between a posting title and a desired title."""
    tt, jt = title_tokens(target), title_tokens(job_title)
    if not tt or not jt:
        return 0.0
    jset = set(jt)
    t_roles = {_ROLE_FAMILY[w] for w in tt if w in _ROLE_FAMILY}
    j_roles = {_ROLE_FAMILY[w] for w in jt if w in _ROLE_FAMILY}
    total = got = 0.0
    for w in dict.fromkeys(tt):
        if w in _ROLE_FAMILY:
            total += 0.35
            got += 0.35 if _ROLE_FAMILY[w] in j_roles else 0.0
        else:
            total += 1.0
            if w in jset:
                got += 1.0
            else:
                got += max((v for (a, b), v in _AFFINITY.items() if a == w and b in jset), default=0.0)
    score = got / total if total else 0.0
    if t_roles:
        if j_roles and not (t_roles & j_roles):
            score *= 0.35
        elif not j_roles:
            score *= 0.6
    # Titles such as "Engineering Manager" or "Sales Engineer" are different jobs unless requested.
    off_job = set(_OFF_TARGET.findall((job_title or "").lower()))
    off_target = set(_OFF_TARGET.findall((target or "").lower()))
    if off_job - off_target:
        score *= 0.3
    return round(min(score, 1.0), 3)


def _index_safe(token: str) -> str:
    return token.replace("+", "plus").replace("#", "sharp")


def index_tokens(title: str) -> str:
    """Normalized title tokens stored in the full-text index (same normalization as title_relevance)."""
    return " ".join(_index_safe(t) for t in title_tokens(title))


def query_tokens(targets: list[str]) -> list[str]:
    """Index tokens that a posting title must share with at least one target to reach MIN relevance.
    A superset filter: the exact relevance score is still computed afterwards."""
    out: list[str] = []
    for target in targets:
        tokens = title_tokens(target)
        domain = [w for w in tokens if w not in _ROLE_FAMILY]
        if domain:
            for w in domain:
                out.append(w)
                out.extend(b for (a, b) in _AFFINITY if a == w)
        else:
            families = {_ROLE_FAMILY[w] for w in tokens}
            out.extend(w for w, family in _ROLE_FAMILY.items() if family in families)
    return [_index_safe(t) for t in dict.fromkeys(out)]


def static_related_titles(titles: list[str], resume_skills: list[str]) -> list[str]:
    """Related roles for the fallback path. Only added when the resume shows supporting skills."""
    have = set(resume_skills)
    out: list[str] = []
    for title in titles:
        toks = " ".join(title_tokens(title))
        for key, (related, evidence) in RELATED_TITLES.items():
            if re.search(rf"\b{re.escape(key)}\b", toks) and len(have & evidence) >= 1:
                out.extend(related)
    wanted = {t.lower() for t in titles}
    return [t for t in dict.fromkeys(out) if t.lower() not in wanted]


def best_title_relevance(job_title: str, titles: list[str], related: list[str]) -> float:
    primary = max((title_relevance(job_title, t) for t in titles), default=0.0)
    secondary = max((title_relevance(job_title, t) for t in related), default=0.0) * 0.85
    return max(primary, secondary)


# ---------------------------------------------------------------- eligibility
def evaluate_eligibility(job: Job, req: SearchRequest) -> tuple[bool, str]:
    """Apply authorization filters. Returns (reject, reason). Sets job.authorization_status / eligibility_note.
    Only explicit statements in the posting are used; silence is reported as 'Not specified'."""
    notes = []
    status = "Unknown"
    reject, reason = False, ""
    if job.citizenship_requirement == "Required":
        notes.append("Posting states US citizenship is required")
        if req.exclude_citizenship_required:
            reject, reason = True, "US citizenship required"
        status = "Eligible" if req.is_citizen else "Potential mismatch"
    elif job.citizenship_requirement == "Not required":
        notes.append("Posting accepts non-citizens (for example US persons / permanent residents)")
    if job.clearance_required:
        notes.append("Posting mentions a security clearance requirement")
        if not req.is_citizen:
            status = "Potential mismatch"
            if req.exclude_citizenship_required and not reject:
                reject, reason = True, "Security clearance required (clearances are limited to US citizens)"
    if job.sponsorship == "Not available":
        notes.append("Posting states visa sponsorship is not available")
        if req.needs_sponsorship:
            status = "Potential mismatch"
            if not reject:
                reject, reason = True, "No visa sponsorship"
    elif job.sponsorship == "Available":
        notes.append("Posting states visa sponsorship is available")
    if status == "Unknown":
        if req.is_citizen:
            status = "Eligible"
        elif req.needs_sponsorship:
            status = "Eligible" if job.sponsorship == "Available" else "Unknown"
        elif req.authorization in ("Green Card / US Permanent Resident",) and job.citizenship_requirement == "Not required":
            status = "Eligible"
        elif req.sponsorship_needed == "No" and job.sponsorship == "Not available" \
                and req.authorization != "Other / Prefer Not to Say" and job.citizenship_requirement != "Required":
            status = "Eligible"
    job.authorization_status = status
    job.eligibility_note = "; ".join(notes) if notes else "Not specified"
    return reject, reason


def work_mode_rejects(job: Job, req: SearchRequest) -> bool:
    return req.work_mode != "All" and job.work_mode not in ("Not listed", req.work_mode)


# ---------------------------------------------------------------- scoring
def job_skills(job: Job) -> list[str]:
    return job.skills if job.enriched >= ENRICH_VERSION else find_skills(f"{job.title}. {job.description}")


def experience_points(required: float | None, have: float) -> tuple[int, str]:
    if required is None:
        return 12, f"Required experience not listed; candidate has {have:g} years"
    gap = required - have
    if gap <= 0:
        pts = 20 if -gap <= 6 else 15
    elif gap <= 1:
        pts = 16
    elif gap <= 2:
        pts = 11
    elif gap <= 3:
        pts = 7
    else:
        pts = 3
    return pts, f"Posting asks for about {required:g}+ years; candidate has {have:g} years"


def heuristic_score(job: Job, profile: Profile, req: SearchRequest, related: list[str]) -> None:
    """Fill score fields from observable resume and posting evidence only."""
    resume_skills = profile.all_skills
    have = {s.lower() for s in resume_skills}
    wanted = job_skills(job)
    matching = [s for s in wanted if s.lower() in have]
    missing = [s for s in wanted if s.lower() not in have]
    if wanted:
        coverage = len(matching) / len(wanted)
        depth = min(len(matching), 8) / 8
        skills_pts = round(35 * (0.7 * coverage + 0.3 * depth))
    else:
        skills_pts = 14   # no description to compare against
    relevance = job.title_relevance or best_title_relevance(job.title, req.titles, related)
    title_pts = round(25 * relevance)
    required = job.required_years
    if required is None:
        required = seniority_years(job.title)
    exp_pts, exp_note = experience_points(required, req.years_experience)
    if req.work_mode == "All" or job.work_mode == req.work_mode:
        arr_pts = 10
    elif job.work_mode == "Not listed":
        arr_pts = 6
    else:
        arr_pts = 2
    past = max((title_relevance(job.title, t) for t in profile.job_titles), default=0.0)
    add_pts = round(10 * min(1.0, 0.6 * past + 0.4 * (min(len(matching), 6) / 6)))
    job.score_breakdown = {"skills": skills_pts, "title": title_pts, "experience": exp_pts,
                           "arrangement": arr_pts, "additional": add_pts}
    job.score = max(0, min(100, sum(job.score_breakdown.values())))
    job.score_source = "heuristic"
    job.matching_skills = matching[:12]
    job.missing_skills = missing[:8]
    job.experience_comparison = exp_note
    job.reason = (f"Title relevance {round(relevance * 100)}% to requested roles; "
                  f"{len(matching)} of {len(wanted)} skills named in the posting appear in the resume."
                  if wanted else f"Title relevance {round(relevance * 100)}% to requested roles; posting text unavailable for skill comparison.")


def apply_ai_ranking(job: Job, ranking: dict, profile: Profile, resume_text: str) -> None:
    """Accept an AI ranking after clamping scores and dropping skills the resume does not show."""
    try:
        parts = {k: max(0, min(WEIGHTS[k], int(round(float(ranking.get(k, 0)))))) for k in WEIGHTS}
    except (TypeError, ValueError):
        return
    resume_lower = resume_text.lower()
    have = {s.lower() for s in profile.all_skills}

    def supported(skill: str) -> bool:
        s = str(skill).strip()
        if not s or len(s) > 60:
            return False
        if s.lower() in have or s.lower() in resume_lower:
            return True
        rx = SKILL_PATTERNS.get(s)
        return bool(rx and rx.search(resume_text))

    matching = [str(s).strip() for s in ranking.get("matching_skills") or [] if supported(s)]
    missing = [str(s).strip()[:60] for s in ranking.get("missing_skills") or [] if str(s).strip() and not supported(s)]
    job.score_breakdown = parts
    job.score = sum(parts.values())
    job.score_source = "ai"
    job.matching_skills = list(dict.fromkeys(matching))[:12]
    job.missing_skills = list(dict.fromkeys(missing))[:8]
    if ranking.get("reason"):
        job.reason = str(ranking["reason"])[:500]
    if ranking.get("experience_comparison"):
        job.experience_comparison = str(ranking["experience_comparison"])[:300]


# ---------------------------------------------------------------- one 0-100 scale for every scorer
# Three scorers can put a number on a job: OpenAI / Claude judging the posting against the profile ("ai"),
# the company rerank model ("company_model") and the keyword scoring ("heuristic"). The 0-100 scale is the
# rubric scale (skills 35 + title 25 + experience 20 + arrangement 10 + additional 10). The reranker returns an
# unbounded-looking relevance between 0 and 1 that is not on that scale (measured: most values below 0.05, good
# matches near 1), so it is never used as a score directly. It is calibrated per search against jobs that carry a
# score on the rubric scale:
# - with OpenAI / Claude: the jobs they judged in the same search are the anchors. The fit is monotone (isotonic
#   regression): a higher relevance never maps to a lower score, and the result always lies between the lowest and
#   highest anchor score. A job therefore passes the minimum-score bar only if jobs of the same relevance were
#   scored above the bar by OpenAI / Claude;
# - without them: the keyword scores of the same jobs are handed out again in the order of the model's relevance
#   (redistribute_scores). The set of scores is unchanged, so exactly as many jobs pass the bar as with keyword
#   scoring alone; the reranker only decides which ones.
MIN_ANCHORS = 12
SCORER_ORDER = {"ai": 0, "company_model": 1}      # everything else (keyword scores) comes after


def fit_calibration(pairs: list[tuple[float, float]]):
    """Monotone map from raw relevance to the 0-100 scale, fitted on (raw, reference score) pairs.
    Returns a function, or None when there are too few anchors to fit anything honest."""
    points = sorted((float(r), float(s)) for r, s in pairs if r == r and s == s)
    if len(points) < MIN_ANCHORS:
        return None
    blocks: list[list[float]] = []                  # [sum_raw, sum_score, n]; pool adjacent violators
    for raw, score in points:
        blocks.append([raw, score, 1.0])
        while len(blocks) > 1 and blocks[-2][1] / blocks[-2][2] > blocks[-1][1] / blocks[-1][2]:
            last = blocks.pop()
            blocks[-1] = [blocks[-1][0] + last[0], blocks[-1][1] + last[1], blocks[-1][2] + last[2]]
    xs = [b[0] / b[2] for b in blocks]
    ys = [b[1] / b[2] for b in blocks]

    def to_scale(raw: float) -> int:
        if raw <= xs[0]:
            value = ys[0]
        elif raw >= xs[-1]:
            value = ys[-1]
        else:
            import bisect
            i = bisect.bisect_right(xs, raw)
            span = xs[i] - xs[i - 1]
            value = ys[i - 1] if span <= 0 else ys[i - 1] + (ys[i] - ys[i - 1]) * (raw - xs[i - 1]) / span
        return max(0, min(100, int(round(value))))

    return to_scale


def redistribute_scores(pairs: list[tuple[str, float, int]]) -> dict[str, int]:
    """(job id, raw relevance, keyword score) -> job id -> score. The same scores, in the order of relevance:
    the most relevant job gets the highest keyword score of the set, and so on (ties keep their keyword order)."""
    order = sorted(pairs, key=lambda p: (p[1], p[2]))
    scores = sorted(p[2] for p in pairs)
    return {p[0]: s for p, s in zip(order, scores)}


def apply_model_score(job: Job, raw: float, score: int, anchors: int, reference: str) -> None:
    """Give a job its calibrated company-model score and say so. The component points stay the keyword estimate."""
    keyword_total = sum(job.score_breakdown.values()) if job.score_breakdown else job.score
    job.score = max(0, min(100, int(score)))
    job.score_source = "company_model"
    job.reason = (f"Score {job.score} from the company rerank model (relevance {raw:.2f}), put on the 0-100 scale by comparing it with "
                  f"{anchors} jobs of this search that {reference}. Not judged by OpenAI or Claude. "
                  f"The component points are the keyword estimate (total {keyword_total}). " + (job.reason or ""))[:500]


def apply_explanation(job: Job, ranking: dict, profile: Profile, resume_text: str) -> None:
    """Take the written part of a company-chat-model judgement (skills, reason); the score is left alone."""
    scratch = job.model_copy()
    apply_ai_ranking(scratch, ranking, profile, resume_text)
    if scratch.score_source != "ai":                # the ranking was unusable
        return
    job.matching_skills, job.missing_skills = scratch.matching_skills, scratch.missing_skills
    if ranking.get("experience_comparison"):
        job.experience_comparison = scratch.experience_comparison
    if ranking.get("reason"):
        note = job.reason.split(" The component points")[0] if job.score_source == "company_model" else ""
        job.reason = (f"{str(ranking['reason'])[:260]} (Explanation written by the company model. {note})".replace(" )", ")"))[:500]


def best_matching_title(job_title: str, titles: list[str], related: list[str]) -> str:
    """Which requested title or variant a posting title matched best (the note kept on every result)."""
    best, score = "", 0.0
    for t in titles:
        r = title_relevance(job_title, t)
        if r > score:
            best, score = t, r
    for t in related:
        r = title_relevance(job_title, t) * 0.85
        if r > score:
            best, score = t, r
    return best
