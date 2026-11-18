"""Free-text answers: one drafting call per run (OpenAI, then Claude), then a deterministic verification.

The writer gets the resume text, the job-search profile and the posting, and must tie every claim to an excerpt
of the resume or profile. The verification rejects a draft when
1. an excerpt given as evidence is not found word for word (ignoring case and white space) in resume or profile;
2. it contains a number (years, counts, percentages, dates) that is not in the resume or profile;
3. it names a skill, tool, degree, certification or other proper noun that is not in the resume or profile (the
   employer's name and the job title may be used; a term only found in the question or the posting may not);
4. it is empty, longer than the field allows, or the writer said it cannot answer from the sources.
A question about a skill that the sources do not mention is not sent to the writer at all: it needs a human.
On failure the draft is regenerated once with the reasons; a second failure leaves the field for a human."""
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("jobfinder.autoapply")

MAX_RESUME_CHARS = 14000
MAX_ANSWER_CHARS = 3000
# Terms that are claims when they appear in an answer (lower case). Matched as whole words / phrases.
VOCABULARY = {
    "python", "java", "javascript", "typescript", "golang", "rust", "c++", "c#", "scala", "kotlin", "ruby", "php",
    "sql", "nosql", "postgresql", "postgres", "mysql", "mongodb", "redis", "cassandra", "dynamodb", "snowflake", "bigquery",
    "redshift", "spark", "pyspark", "hadoop", "kafka", "airflow", "dbt", "flink", "databricks", "tableau", "looker", "power bi",
    "aws", "azure", "gcp", "google cloud", "kubernetes", "k8s", "docker", "terraform", "ansible", "jenkins", "ci/cd",
    "linux", "react", "angular", "vue", "node.js", "django", "flask", "fastapi", "spring boot", "graphql",
    "pytorch", "tensorflow", "keras", "jax", "scikit-learn", "sklearn", "xgboost", "pandas", "numpy", "hugging face",
    "transformers", "llm", "llms", "rag", "nlp", "computer vision", "cuda", "mlops", "langchain", "openai", "salesforce",
    "sap", "oracle", "jira", "figma", "agile", "scrum", "microservices", "machine learning", "deep learning", "data science",
    "bachelor", "bachelors", "bachelor's", "master", "masters", "master's", "phd", "ph.d", "doctorate", "mba", "b.s", "b.s.",
    "m.s", "m.s.", "bsc", "msc", "b.tech", "m.tech", "associate degree", "certified", "certification", "certificate", "pmp",
    "cpa", "cissp", "ccna", "security clearance", "clearance", "top secret", "ts/sci",
}
ALLOWED_CAPS = {"i", "i'm", "i've", "i'd", "i'll", "us", "u.s", "u.s.", "usa", "english", "monday", "tuesday", "wednesday",
                "thursday", "friday", "january", "february", "march", "april", "may", "june", "july", "august", "september",
                "october", "november", "december", "dear", "hiring", "manager", "team", "thank", "thanks", "sincerely", "regards"}


@dataclass
class Draft:
    ok: bool
    text: str = ""
    evidence: list[str] = field(default_factory=list)
    reason: str = ""


def _flat(text: str) -> str:
    return " ".join((text or "").lower().split())


def _has_term(haystack: str, term: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", haystack) is not None


def sources_text(facts) -> str:
    profile = facts.profile
    parts = [facts.resume_text[:MAX_RESUME_CHARS]]
    if profile is not None:
        parts.append("Skills: " + ", ".join(profile.skills or []))
        parts.append("Desired titles: " + ", ".join(profile.desired_titles or []))
        if profile.years_experience is not None:
            years = float(profile.years_experience)
            parts.append(f"Years of experience: {int(years) if years.is_integer() else years}")
    return "\n".join(parts)


def unsupported_terms(label: str, sources: str) -> list[str]:
    """Skills / degrees named in a question that the sources do not mention."""
    flat_label, flat_src = _flat(label), _flat(sources)
    return sorted(t for t in VOCABULARY if len(t) > 1 and _has_term(flat_label, t) and not _has_term(flat_src, t))


def check(text: str, evidence: list[str], facts, sources: str, max_len: int = MAX_ANSWER_CHARS) -> list[str]:
    """Problems with a draft; [] means it passed."""
    problems: list[str] = []
    flat_src = _flat(sources)
    allowed = flat_src + " " + _flat(facts.company) + " " + _flat(facts.job_title)
    if not text.strip():
        return ["empty answer"]
    if len(text) > max_len:
        problems.append(f"longer than {max_len} characters")
    for excerpt in evidence:
        if _flat(excerpt) and _flat(excerpt) not in flat_src:
            problems.append(f"evidence not found in the resume or profile: {excerpt[:80]!r}")
    if not evidence:
        problems.append("no evidence given")
    for number in set(re.findall(r"\d+(?:[.,]\d+)?", text)):
        if not _has_term(flat_src, number.replace(",", "")) and not _has_term(flat_src, number):
            problems.append(f"number not in the sources: {number}")
    flat_text = _flat(text)
    for term in VOCABULARY:
        if len(term) > 1 and _has_term(flat_text, term) and not _has_term(allowed, term):
            problems.append(f"term not in the sources: {term}")
    # Proper nouns (capitalised words that do not start a sentence): employers, schools, products, places.
    for sentence in re.split(r"(?<=[.!?:;])\s+|\n+", text):
        words = re.findall(r"[A-Za-z][A-Za-z0-9.+#'&-]*", sentence)
        for word in words[1:]:
            clean = word.strip(".'-").lower()
            if word[0].isupper() and clean and clean not in ALLOWED_CAPS and not _has_term(allowed, clean):
                problems.append(f"name not in the sources: {word.strip('.')}")
    return sorted(set(problems))


_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["answers"],
    "properties": {"answers": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["key", "text", "cannot_answer", "claims"],
        "properties": {
            "key": {"type": "string"}, "text": {"type": "string"}, "cannot_answer": {"type": "boolean"},
            "claims": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["claim", "evidence"],
                                                  "properties": {"claim": {"type": "string"}, "evidence": {"type": "string"}}}}}}}},
}
_SYSTEM = (
    "You write short answers to open questions of a job application, in the first person, for the candidate. Use ONLY facts "
    "stated in the candidate's resume and profile below. Never add a number, an employer, a school, a degree, a skill, a tool, "
    "a certification or an achievement that is not written there. Do not exaggerate. You may mention the employer and the job "
    "title. For every claim give the exact excerpt (copied word for word) of the resume or profile it rests on. If a question "
    "cannot be answered truthfully from these sources, set cannot_answer to true and leave text empty. Plain text, no "
    "markdown, at most 120 words per answer (cover letters at most 220 words). Reply with JSON only.")


async def draft_free_text(items: list, facts, writers: list, max_calls: int, usage: dict) -> dict[str, Draft]:
    """items: [(FormField, Answer)]. Returns {field_key: Draft}."""
    sources = sources_text(facts)
    out: dict[str, Draft] = {}
    pending = []
    for f, _ in items:
        missing = unsupported_terms(f.label, sources)
        if missing:
            out[f.key] = Draft(False, reason="The resume and profile do not mention: " + ", ".join(missing))
        else:
            pending.append(f)
    if not pending:
        return out
    if not writers:
        for f in pending:
            out[f.key] = Draft(False, reason="No writer model is configured (OpenAI or Claude)")
        return out
    feedback: dict[str, list[str]] = {}
    calls = 0
    while pending and calls < max(1, max_calls):
        calls += 1
        user = ("CANDIDATE RESUME AND PROFILE (the only allowed facts):\n" + sources + "\n\nEMPLOYER: " + facts.company
                + "\nJOB TITLE: " + facts.job_title + "\nJOB DESCRIPTION (context only, not facts about the candidate):\n"
                + (facts.job_description or "")[:3000] + "\n\nQUESTIONS:\n"
                + "\n".join(f"- key={f.key!r}: {f.label[:500]}" + (f"\n  A previous draft was rejected: {'; '.join(feedback[f.key])[:600]}"
                                                                  if f.key in feedback else "") for f in pending))
        data = None
        for writer in writers:
            try:
                data = await writer.json_call(_SYSTEM, user, _SCHEMA, "application_answers")
                usage["draft_calls"] = usage.get("draft_calls", 0) + 1
                usage.setdefault("draft_model", getattr(writer, "name", "model"))
                break
            except Exception as exc:
                log.warning("auto-apply drafting by %s failed: %s", getattr(writer, "name", "model"), type(exc).__name__)
        if data is None:
            break
        by_key = {a.get("key"): a for a in (data.get("answers") or []) if isinstance(a, dict)}
        still = []
        for f in pending:
            got = by_key.get(f.key)
            if not got:
                feedback[f.key] = ["no answer returned"]
                still.append(f)
                continue
            if got.get("cannot_answer"):
                out[f.key] = Draft(False, reason="The writer found no support in the resume or profile")
                continue
            text = str(got.get("text") or "").strip()
            evidence = [str(c.get("evidence") or "") for c in (got.get("claims") or []) if isinstance(c, dict)]
            problems = check(text, evidence, facts, sources)
            if problems:
                feedback[f.key] = problems
                still.append(f)
            else:
                out[f.key] = Draft(True, text=text, evidence=[e[:300] for e in evidence if e][:10])
        pending = still
    for f in pending:
        out[f.key] = Draft(False, reason="Draft failed verification: " + "; ".join(feedback.get(f.key, ["no draft"]))[:400])
    return out
