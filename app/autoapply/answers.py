"""The answer engine: a proposed value for every field of an application form, with its source and evidence.

Rules (docs/auto-apply-api.md, "Answer rules"):
- standard fields come from the candidate record, the job-search profile, the recorded application answers and the
  stored resume file; nothing is read off the resume text except as evidence for free text;
- legal and EEO questions are answered only from recorded answers (plus the profile's work authorization,
  sponsorship and salary, which the candidate gave for the job search). Unknown -> needs input, naming the field;
- EEO without a recorded value: the form's decline option (source eeo_decline);
- certifications are never ticked for the candidate unless the consent covers them; "I personally completed this
  application" and arbitration agreements always need a human;
- free text is drafted once per run by OpenAI / Claude from resume and profile facts and verified deterministically
  (verify.py); one regeneration, then needs input."""
import re
from dataclasses import dataclass, field

from . import records
from .classify import classify_rules, classify_with_models
from .schema import Answer, Blocker, FormField
from .verify import draft_free_text

DECLINE = re.compile(r"decline|prefer not|don.?t wish|do not wish|not wish to|choose not|not to (answer|disclose|say|self)|"
                     r"rather not|i don.?t want|no answer|not specified|undisclosed", re.I)
YES = re.compile(r"^\s*(yes|y|true|i am|i do|i have|i will|i would)\b", re.I)
NO = re.compile(r"^\s*(no|n|false|i am not|i do not|i don.?t|i have not|i haven.?t|i will not|i won.?t)\b", re.I)


@dataclass
class Facts:
    """Everything the engine may use. `resume_text` is evidence for free text only."""
    candidate_name: str
    email: str
    phone: str
    answers_raw: dict
    profile: object | None
    candidate: object | None
    resume_filename: str | None          # None = no stored resume file
    resume_text: str
    company: str
    job_title: str
    job_description: str = ""
    job_requirements: list[str] = field(default_factory=list)
    from_salesforce: bool = False
    may_attest: bool = False
    may_accept_data_processing: bool = False


@dataclass
class Prepared:
    answers: list[Answer]
    needs_input: list[dict]
    blockers: list[Blocker]
    usage: dict


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def match_option(value: str, options: list[str]) -> str | None:
    """The option that says the same as `value` (exact, then prefix, then word match). None when ambiguous or absent."""
    if not options:
        return value
    v = _norm(value)
    if not v:
        return None
    exact = [o for o in options if _norm(o) == v]
    if exact:
        return exact[0]
    if v in ("yes", "no"):
        rx = YES if v == "yes" else NO
        hits = [o for o in options if rx.search(o)]
        return hits[0] if len(hits) == 1 else None
    prefix = [o for o in options if _norm(o).startswith(v + " ") or v.startswith(_norm(o) + " ")]
    if len(prefix) == 1:
        return prefix[0]
    words = [o for o in options if re.search(rf"\b{re.escape(v)}\b", _norm(o))]
    return words[0] if len(words) == 1 else None


def decline_option(options: list[str]) -> str | None:
    hits = [o for o in options if DECLINE.search(o)]
    return hits[0] if hits else None


class Engine:
    def __init__(self, classifiers: list | None = None, writers: list | None = None, max_draft_calls: int = 2):
        self.classifiers = classifiers or []
        self.writers = writers or []
        self.max_draft_calls = max_draft_calls

    async def prepare(self, fields: list[FormField], facts: Facts, admin_edits: dict | None = None) -> Prepared:
        usage: dict = {}
        admin_edits = admin_edits or {}
        classes = {f.key: classify_rules(f) for f in fields}
        unknown = [f for f in fields if classes[f.key] == "unknown"]
        if unknown and self.classifiers:
            classes.update(await classify_with_models(unknown, self.classifiers, usage))
        eff = records.effective(facts.answers_raw, profile=facts.profile, candidate=facts.candidate)
        answers: list[Answer] = []
        needs: list[dict] = []
        blockers: list[Blocker] = []
        free: list[tuple[FormField, Answer]] = []

        def need(f: FormField, reason: str, fill_in: str, a: Answer, note: str = "") -> None:
            a.status, a.note = "missing", note or a.note
            if f.required:
                needs.append({"field_key": f.key, "label": f.label[:300], "reason": reason, "fill_in": fill_in})
            else:
                a.status = "ok"                       # optional and unknown: left empty
                a.value = ""

        if facts.resume_filename is None:
            blockers.append(Blocker("no_resume_file", "The candidate has no stored resume file (upload the resume again)"))

        for f in fields:
            cls = classes[f.key]
            a = Answer(field_key=f.key, label=f.label[:300], classification=cls, required=f.required, type=f.type)
            answers.append(a)
            if f.key in admin_edits and not cls.startswith("certification:"):
                a.value, a.source, a.evidence = str(admin_edits[f.key]), "admin_edit", []
                if f.options and a.value and match_option(a.value, f.options) is None:
                    need(f, "no_matching_option", "run_answer", a, "The edited value is not one of the form's options")
                continue
            kind, _, name = cls.partition(":")
            if kind == "standard":
                self._standard(f, name, a, facts, eff, need)
            elif kind == "legal":
                self._legal(f, name, a, facts, eff, need)
            elif kind == "eeo":
                self._eeo(f, name, a, eff, need)
            elif kind == "certification":
                self._certification(f, name, a, facts, blockers)
            elif cls == "free_text":
                free.append((f, a))
            elif cls == "education":
                need(f, "not_recorded", "run_answer", a, "Education details are not recorded for applications")
            else:
                need(f, "required_unknown_field", "run_answer", a, "Question not understood")

        if free:
            drafted = await draft_free_text([(f, a) for f, a in free], facts, self.writers, self.max_draft_calls, usage)
            for f, a in free:
                result = drafted.get(f.key)
                if result and result.ok:
                    a.value, a.source, a.evidence = result.text, "ai_draft", result.evidence
                else:
                    need(f, "free_text_unverified", "run_answer", a, (result.reason if result else "No writer model available"))
        return Prepared(answers=answers, needs_input=needs, blockers=blockers, usage=usage)

    # ---------------------------------------------------------------- per class
    @staticmethod
    def _set(f: FormField, a: Answer, value: str, source: str, evidence: list[str], need, fill_in: str) -> None:
        if f.options:
            chosen = match_option(value, f.options)
            if chosen is None:
                need(f, "no_matching_option", fill_in, a, f"Recorded value {value!r} is not one of the options")
                return
            value = chosen
        a.value, a.source, a.evidence = value, source, evidence

    def _standard(self, f, name, a, facts: Facts, eff, need) -> None:
        cand_src = "salesforce" if facts.from_salesforce else "candidate"

        def from_eff(key: str, label: str) -> None:
            e = eff[key]
            if e.source == "unknown" or not e.value:
                need(f, "not_recorded", f"application_answers.{key}", a, f"{label} is not recorded")
                return
            src = {"application_answers": "application_answers", "profile": "profile"}.get(e.source, cand_src)
            self._set(f, a, str(e.value), src, [f"{key}"], need, f"application_answers.{key}")

        if name == "resume":
            if facts.resume_filename is None:
                a.status, a.note = "blocked", "No stored resume file"
            else:
                a.value, a.source, a.evidence = facts.resume_filename, "resume_file", [facts.resume_filename]
        elif name == "cover_letter_file":
            need(f, "file_required", "run_answer", a, "A cover-letter file is not stored for the candidate")
        elif name == "email":
            if facts.email:
                a.value, a.source, a.evidence = facts.email, cand_src, ["candidate.email"]
            else:
                need(f, "not_recorded", "candidate.email", a, "The candidate has no e-mail address")
        elif name == "full_name":
            a.value, a.source, a.evidence = facts.candidate_name, cand_src, ["candidate.name"]
        elif name in ("first_name", "last_name", "phone"):
            from_eff(name, name.replace("_", " ").capitalize())
        elif name == "preferred_name":
            e = eff["preferred_name"]
            if e.source == "application_answers":
                a.value, a.source = str(e.value), "application_answers"
            else:
                need(f, "not_recorded", "application_answers.preferred_name", a)
        elif name in ("linkedin", "github", "portfolio"):
            from_eff(f"{name}_url", name.capitalize() + " address")
        elif name in ("current_company", "current_title", "city", "state", "country"):
            from_eff(name, name.replace("_", " ").capitalize())
        elif name == "location":
            parts = [str(eff[k].value) for k in ("city", "state", "country") if eff[k].source == "application_answers" and eff[k].value]
            if not parts:
                need(f, "not_recorded", "application_answers.city", a, "City / state are not recorded")
            else:
                self._set(f, a, ", ".join(parts), "application_answers", ["city", "state", "country"], need, "application_answers.city")
        else:
            need(f, "required_unknown_field", "run_answer", a)

    def _legal(self, f, name, a, facts: Facts, eff, need) -> None:
        simple = {"authorized_us": "authorized_us", "sponsorship": "sponsorship_now_or_future", "age_18": "age_18_or_over",
                  "relocate": "willing_to_relocate", "restrictive_agreements": "restrictive_agreements"}
        if name in simple:
            key = simple[name]
            e = eff[key]
            if e.value not in ("yes", "no"):
                need(f, "unknown", f"application_answers.{key}", a, f"{key} is unknown")
                return
            value = e.value if f.options else e.value.capitalize()
            self._set(f, a, value, "profile" if e.source == "profile" else "application_answers", [key], need, f"application_answers.{key}")
        elif name == "previously_employed":
            employers = [_norm(x) for x in eff["previously_employed_by"].value or []]
            company = _norm(facts.company)
            if company and any(e and (e == company or e in company or company in e) for e in employers):
                self._set(f, a, "yes" if f.options else "Yes", "application_answers", ["previously_employed_by"], need,
                          "application_answers.previously_employed_by")
            elif eff["previous_employers_complete"].value is True:
                self._set(f, a, "no" if f.options else "No", "application_answers", ["previously_employed_by", "previous_employers_complete"],
                          need, "application_answers.previously_employed_by")
            else:
                need(f, "unknown", "application_answers.previously_employed_by", a,
                     "Previous employers are not recorded as complete")
        elif name in ("start_date", "notice_period", "salary", "referral_source"):
            key = {"start_date": "earliest_start", "notice_period": "notice_period", "salary": "expected_salary",
                   "referral_source": "referral_source"}[name]
            e = eff[key]
            if name == "start_date" and e.source == "unknown" and eff["notice_period"].source != "unknown" and f.type in ("text", "textarea"):
                e = eff["notice_period"]
                key = "notice_period"
            if e.source == "unknown" or not e.value:
                need(f, "not_recorded", f"application_answers.{key}", a, f"{key} is not recorded")
                return
            value = str(e.value)
            if f.type == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                need(f, "not_recorded", f"application_answers.{key}", a, "A date (YYYY-MM-DD) is needed")
                return
            if f.type == "number":
                digits = re.sub(r"[^\d]", "", value)
                if not digits:
                    need(f, "not_recorded", f"application_answers.{key}", a, "A number is needed")
                    return
                value = digits
            self._set(f, a, value, "profile" if e.source == "profile" else "application_answers", [key], need, f"application_answers.{key}")
        else:
            need(f, "unknown", "run_answer", a)

    def _eeo(self, f, name, a, eff, need) -> None:
        key = f"eeo_{name}"
        value = str(eff[key].value or "decline")
        if value.lower() == "decline":
            option = decline_option(f.options) if f.options else None
            if option:
                a.value, a.source, a.evidence = option, "eeo_decline", [key]
            elif f.type in ("text", "textarea") and f.required:
                need(f, "not_recorded", f"application_answers.{key}", a, "No decline option; a recorded answer is needed")
            else:
                need(f, "no_matching_option", f"application_answers.{key}", a, "The form offers no decline option")
            return
        self._set(f, a, value, "application_answers", [key], need, f"application_answers.{key}")

    @staticmethod
    def _certification(f, name, a, facts: Facts, blockers: list[Blocker]) -> None:
        a.value = "unchecked"
        a.source = "application_answers"
        if name == "personal_completion":
            a.status, a.note = "blocked", "The candidate must complete this application personally"
            blockers.append(Blocker("personal_completion_certification", f.label[:200]))
        elif name == "arbitration":
            if f.required:
                a.status, a.note = "blocked", "An arbitration agreement needs the candidate's own decision"
                blockers.append(Blocker("arbitration_agreement", f.label[:200]))
        elif name == "attest":
            if facts.may_attest:
                a.value, a.source, a.evidence, a.note = "checked", "application_answers", ["consent.may_attest"], "Covered by the consent"
            elif f.required:
                a.status, a.note = "blocked", "The consent does not allow certifying answers for the candidate"
                blockers.append(Blocker("attestation_not_permitted", f.label[:200]))
        elif name == "data_processing":
            if facts.may_accept_data_processing:
                a.value, a.evidence, a.note = "checked", ["consent.may_accept_data_processing"], "Covered by the consent"
            elif f.required:
                a.status, a.note = "blocked", "The consent does not allow accepting the employer's privacy notice"
                blockers.append(Blocker("data_processing_consent_not_permitted", f.label[:200]))
        # marketing_opt_in: always left unchecked
        if f.type not in ("checkbox",) and a.value in ("checked", "unchecked"):
            # A certification asked as a select / radio ("I agree" / "I do not agree"): only pick "agree" when allowed.
            if a.value == "checked":
                agree = [o for o in f.options if re.search(r"\b(yes|agree|accept|acknowledge|confirm)", o, re.I)
                         and not re.search(r"\b(not|don.?t|decline|disagree)\b", o, re.I)]
                a.value = agree[0] if agree else ""
                if not agree:
                    a.status = "blocked"
            else:
                a.value = ""
