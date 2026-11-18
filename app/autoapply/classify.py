"""What an application-form question asks for.

Rules first (they cover the standard fields and the legal / EEO / certification wording seen on Greenhouse,
Lever, Ashby and Workable forms). Labels the rules do not recognise go to a model in one batched call: the company
model techsara-35b first, OpenAI or Claude as fallback. The model only picks a class from a fixed list; it never
answers. A class it returns outside the list counts as unknown."""
import logging
import re

from .schema import FormField

log = logging.getLogger("jobfinder.autoapply")

STANDARD = ("first_name", "last_name", "full_name", "preferred_name", "email", "phone", "location", "city", "state", "country",
            "linkedin", "github", "portfolio", "current_company", "current_title", "resume", "cover_letter_file")
LEGAL = ("authorized_us", "sponsorship", "age_18", "relocate", "previously_employed", "restrictive_agreements", "start_date",
         "notice_period", "salary", "referral_source")
EEO = ("gender", "race", "hispanic_latino", "veteran", "disability")
CERTIFICATION = ("personal_completion", "attest", "arbitration", "data_processing", "marketing_opt_in")
OTHER = ("free_text", "education", "unknown")
CLASSES = tuple(f"standard:{c}" for c in STANDARD) + tuple(f"legal:{c}" for c in LEGAL) + tuple(f"eeo:{c}" for c in EEO) \
    + tuple(f"certification:{c}" for c in CERTIFICATION) + OTHER

_R = lambda p: re.compile(p, re.I)  # noqa: E731
# Order matters: the first rule that matches wins (certifications before everything, sponsorship before authorization).
RULES: list[tuple[re.Pattern, str]] = [
    (_R(r"personally (completed|filled|wrote|written)|completed (this|the) application (myself|personally)|not (been )?(completed|filled out) by (an? )?(ai|bot|automated|third)|"
        r"without (the )?(use|help) of (ai|automation|a bot)"), "certification:personal_completion"),
    (_R(r"arbitrat"), "certification:arbitration"),
    (_R(r"(true|accurate|correct|complete)\b.{0,40}\b(true|accurate|correct|complete|knowledge|belief)|i certify|certify that|attest|"
        r"falsif|misrepresent"), "certification:attest"),
    (_R(r"privacy (policy|notice|statement)|data (processing|protection|retention)|gdpr|process(ing)? (of )?my (personal )?(data|information)|"
        r"consent to .*(stor|process|retain)|acknowledge .*(privacy|notice|policy)|candidate privacy"), "certification:data_processing"),
    (_R(r"(sign up|subscribe|opt.?in|receive) .*(newsletter|job alerts|marketing|updates|text messages|sms)|future (job )?opportunit"), "certification:marketing_opt_in"),
    (_R(r"sponsor"), "legal:sponsorship"),
    (_R(r"(legally )?(authori[sz]ed|eligible|permitted|right) to work|work authori[sz]ation|employment eligibility"), "legal:authorized_us"),
    (_R(r"\b(18|eighteen)\b.*(years|age|older)|(age|years) of age|at least 18|over (the age of )?18"), "legal:age_18"),
    (_R(r"relocat"), "legal:relocate"),
    (_R(r"(previously|ever|before|formerly|currently) (been )?(employed|worked|work)|worked (for|at) .{0,60} (before|previously)|former employee|"
        r"(are|were) you (a )?(current|former) (employee|contractor)"), "legal:previously_employed"),
    (_R(r"non.?compete|non.?solicit|restrictive (covenant|agreement)|agreements? .*(prevent|restrict|limit)"), "legal:restrictive_agreements"),
    (_R(r"notice period|how much notice"), "legal:notice_period"),
    (_R(r"(earliest|available|availability|when can you|able to) .*(start|join|begin)|start date|date (you are )?available"), "legal:start_date"),
    (_R(r"salary|compensation|pay (expectation|range|requirement)|desired (pay|rate)|expected (pay|rate|ctc)|\bctc\b|hourly rate"), "legal:salary"),
    (_R(r"how did you (hear|learn|find)|where did you (hear|find|see)|referr(al|ed) source|source of (application|referral)"), "legal:referral_source"),
    (_R(r"hispanic|latin[oax]"), "eeo:hispanic_latino"),
    (_R(r"\bgender\b|\bsex\b|pronoun"), "eeo:gender"),
    (_R(r"\brace\b|ethnic"), "eeo:race"),
    (_R(r"veteran|military|armed forces"), "eeo:veteran"),
    (_R(r"disabilit|handicap"), "eeo:disability"),
    (_R(r"^\s*(resume|cv|resume/cv|resume ?\/ ?cv|upload (your )?(resume|cv))\b"), "standard:resume"),
    (_R(r"cover letter"), "free_text"),             # a cover-letter *file* is re-classified by type below
    (_R(r"^\s*(legal )?first name|^\s*given name|^\s*first$"), "standard:first_name"),
    (_R(r"^\s*(legal )?last name|^\s*(family name|surname)|^\s*last$"), "standard:last_name"),
    (_R(r"preferred (first )?name|nickname"), "standard:preferred_name"),
    (_R(r"^\s*(full )?name\b|^\s*your name|^\s*legal name"), "standard:full_name"),
    (_R(r"e-?mail"), "standard:email"),
    (_R(r"phone|mobile|telephone"), "standard:phone"),
    (_R(r"linkedin"), "standard:linkedin"),
    (_R(r"github"), "standard:github"),
    (_R(r"portfolio|personal (web)?site|^\s*website|other (website|link)|twitter|^\s*url\b"), "standard:portfolio"),
    (_R(r"current (company|employer)|^\s*company\b|most recent (company|employer)|^\s*employer\b"), "standard:current_company"),
    (_R(r"current (title|role|position|job title)|^\s*(job )?title\b|most recent (title|role)"), "standard:current_title"),
    (_R(r"^\s*country\b|country of residence"), "standard:country"),
    (_R(r"^\s*state\b|province|state of residence"), "standard:state"),
    (_R(r"^\s*city\b|city of residence"), "standard:city"),
    (_R(r"^\s*(current )?location\b|where are you (located|based)|^\s*address\b"), "standard:location"),
    (_R(r"school|university|college|degree|discipline|field of study|major\b|gpa|graduat|education"), "education"),
    (_R(r"why (do )?you want|why are you interested|why (us|this|our)|what (excites|interests|attracts) you|tell us about|describe|"
        r"explain|what (is|are|was|would)|how (do|did|would|have) you|how (many|much)|share (an|a|your)|additional information|anything else|"
        r"summary|about yourself|motivat|experience with|experience (in|using)|walk us through"), "free_text"),
]


def classify_rules(f: FormField) -> str:
    label = " ".join((f.label or "").split())
    for pattern, cls in RULES:
        if pattern.search(label):
            if cls == "free_text" and f.type == "file":
                return "standard:cover_letter_file" if re.search(r"cover", label, re.I) else "unknown"
            if cls == "standard:resume" and f.type != "file":
                return "unknown"
            if cls == "free_text" and f.type not in ("text", "textarea"):
                continue
            return cls
    if f.type == "file" and re.search(r"resume|cv\b", label, re.I):
        return "standard:resume"
    return "unknown"


_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["key", "class"],
        "properties": {"key": {"type": "string"}, "class": {"type": "string", "enum": list(CLASSES)}}}}},
}
_SYSTEM = ("You classify the questions of a job application form. For every question return one class from the given list. "
           "Do not answer the questions. legal:* = work authorization, visa sponsorship, age, relocation, previous employment "
           "with this employer, non-compete agreements, start date, notice period, salary, how the applicant heard of the job. "
           "eeo:* = voluntary self-identification (gender, race, Hispanic/Latino, veteran, disability). certification:* = boxes "
           "where the applicant certifies, agrees or consents (personal_completion = the applicant states they filled in the form "
           "themselves; attest = answers are true; arbitration; data_processing = privacy notice; marketing_opt_in). free_text = "
           "an open question answered in sentences. education = school, degree, GPA. standard:* = contact and profile fields. "
           "unknown = anything else. Reply with JSON only.")


async def classify_with_models(fields: list[FormField], models: list, usage: dict | None = None) -> dict[str, str]:
    """One call for all labels the rules left unknown, to the first model that answers. {} when none does."""
    if not fields or not models:
        return {}
    listing = "\n".join(f"- key={f.key!r} type={f.type} options={f.options[:12]!r} label={f.label[:300]!r}" for f in fields)
    user = "Classes: " + ", ".join(CLASSES) + "\n\nQuestions:\n" + listing
    for model in models:
        try:
            data = await model.json_call(_SYSTEM, user, _SCHEMA, "question_classes")
        except Exception as exc:                          # ProviderError or anything else: try the next model
            log.warning("auto-apply classification by %s failed: %s", getattr(model, "name", "model"), type(exc).__name__)
            continue
        if usage is not None:
            usage["classify_calls"] = usage.get("classify_calls", 0) + 1
            usage.setdefault("classify_model", getattr(model, "name", "model"))
        keys = {f.key for f in fields}
        out = {}
        for item in (data or {}).get("items") or []:
            if isinstance(item, dict) and item.get("key") in keys and item.get("class") in CLASSES:
                out[item["key"]] = item["class"]
        return out
    return {}
