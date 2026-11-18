"""Shapes shared by the adapters, the answer engine and the run service. Plain data, JSON-serialisable."""
import hashlib
import json
from dataclasses import asdict, dataclass, field

FIELD_TYPES = ("text", "email", "tel", "url", "textarea", "select", "radio", "checkbox", "file", "date", "number", "unknown")


@dataclass
class FormField:
    key: str                       # stable id within the form (input name or id)
    label: str
    type: str = "text"
    required: bool = False
    options: list[str] = field(default_factory=list)
    section: str = ""              # e.g. "eeo", "custom", "standard"
    selector: str = ""             # CSS selector of the input (radio/checkbox groups: of the group container)
    multiple: bool = False         # checkbox group with several boxes

    def public(self) -> dict:
        """What is stored and shown: no selector internals beyond what reproduces the form."""
        data = asdict(self)
        data.pop("selector", None)
        return data


@dataclass
class Blocker:
    code: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail[:300]}


@dataclass
class FormSchema:
    ats: str
    url: str
    fields: list[FormField] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    title: str = ""

    def snapshot(self) -> list[dict]:
        return [f.public() for f in self.fields]

    def schema_hash(self) -> str:
        return digest([{k: v for k, v in f.public().items() if k in ("key", "label", "type", "required", "options")} for f in self.fields])


@dataclass
class Answer:
    field_key: str
    label: str
    value: str = ""
    source: str = ""               # profile | candidate | application_answers | resume_file | resume_line | salesforce | ai_draft | eeo_decline | admin_edit
    evidence: list[str] = field(default_factory=list)
    classification: str = ""
    required: bool = False
    status: str = "ok"             # ok | missing | blocked
    note: str = ""
    type: str = "text"

    def as_dict(self) -> dict:
        return asdict(self)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def answers_hash(answers: list[dict], schema_hash: str) -> str:
    """What the Admin approves: the exact field/value list on the exact form."""
    return digest({"schema": schema_hash, "answers": [(a.get("field_key"), a.get("value"), a.get("source")) for a in answers]})
