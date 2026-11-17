"""Bringing Salesforce candidates into the portal: who may find which, the mapping, import and refresh.

Salesforce is only read (see services/salesforce.py). Everything written here is written to the portal's database.

The rule that protects local work, used by import-again and refresh alike:
- a synced field is written only when the local value is empty (or the built-in default) or still equal to the
  value last written from Salesforce (kept in candidates.salesforce_snapshot). A field a manager changed stays.
  An empty Salesforce value never erases anything;
- a resume uploaded in the portal is replaced only by a Salesforce file that was modified after that upload, and a
  Salesforce file this candidate already received is never fetched twice."""
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from ...job_normalizer import US_STATES
from ..deps import Principal, forbid
from ..models import Candidate, Resume, Team
from ..schemas import SalesforceCandidate, SalesforceSync
from ..security import utcnow
from . import candidates as candidates_service
from . import people
from . import salesforce as sf
from .audit import audit

OTHER_AUTHORIZATION = "Other / Prefer Not to Say"
# Salesforce's visa / authorization values on the portal's options. Anything not listed becomes OTHER_AUTHORIZATION:
# CPT, H4 and B2 do not by themselves say that the person holds a work visa or an EAD, so they are not guessed.
AUTHORIZATION = {
    "us citizen": "US Citizen", "usc": "US Citizen",
    "green card / permanent resident": "Green Card / US Permanent Resident", "green card": "Green Card / US Permanent Resident",
    "green card ead": "Employment Authorization Document (EAD)", "opt": "Employment Authorization Document (EAD)",
    "stem opt extension": "Employment Authorization Document (EAD)",
    "h1b": "Current Work Visa",
}
SENIORITY = {"entry level": "Entry Level", "associate": "Associate", "mid senior level": "Mid-Senior level",
             "director": "Director", "executive": "Executive"}
WORK_MODES = {"remote": "Remote", "hybrid": "Hybrid", "on-site": "On-site", "onsite": "On-site", "on site": "On-site"}
PLACEHOLDERS = {"", "na", "n/a", "n.a.", "none", "nil", "null", "-", "--", "tbd", "other"}
NOTES_HEADING = "Preferred job locations (from Salesforce): "
NOTES_MAX = 10000
_STATE_BY_NAME = {name: code for code, name in US_STATES.items()}
_STATE_BY_NAME.update({"washington dc": "DC", "washington d.c.": "DC", "washington d.c": "DC", "d.c.": "DC"})
_PHONE_JUNK = re.compile(r"[^0-9+()\-./# xXeEtT]")
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}")
# The fields a sync may write, with the value that counts as "nothing entered".
CANDIDATE_FIELDS = {"name": "", "email": "", "phone": ""}
PROFILE_FIELDS = {"desired_titles": [], "years_experience": None, "work_mode": "All", "work_authorization": OTHER_AUTHORIZATION,
                  "sponsorship_needed": "Unknown", "seniority": "", "preferred_states": []}


def as_http(problem: sf.SalesforceError) -> HTTPException:
    """A Salesforce failure as an API answer: 502 or 503 with a plain sentence. Never Salesforce's own text."""
    return HTTPException(problem.status_code, problem.public)


# ---------------------------------------------------------------- mapping
def _real(text: str) -> str:
    return "" if text.strip().lower() in PLACEHOLDERS else text.strip()


def map_titles(candidate: sf.SfCandidate) -> list[str]:
    """Preferred positions (a free-text list), else the technology, else the niche. At most 8."""
    parts = [_real(p) for p in re.split(r"[,;\n|]+", candidate.preferred_positions)]
    titles = list(dict.fromkeys(p[:120] for p in parts if len(p) >= 2))
    if not titles:
        single = _real(candidate.technology) or _real(candidate.niche)
        titles = [single[:120]] if single else []
    return titles[:8]


def map_years(candidate: sf.SfCandidate) -> float | None:
    years = candidate.years_experience
    return round(years, 1) if years is not None and 0 <= years <= 40 else None


def map_work_mode(candidate: sf.SfCandidate) -> str | None:
    """One arrangement named: that one. Several, or "All of the above": All. Nothing recognisable: None."""
    text = candidate.preferred_job_type.strip().lower()
    if not text:
        return None
    if text.startswith("all"):
        return "All"
    modes = {WORK_MODES[p.strip()] for p in re.split(r"[,;/]+", text) if p.strip() in WORK_MODES}
    return next(iter(modes)) if len(modes) == 1 else "All" if modes else None


def map_authorization(candidate: sf.SfCandidate) -> str | None:
    """The current visa status, else the work authorization. A value the portal has no option for is "Other"."""
    text = candidate.visa_status.strip() or candidate.work_authorization.strip()
    return AUTHORIZATION.get(text.lower(), OTHER_AUTHORIZATION) if text else None


def map_sponsorship(candidate: sf.SfCandidate) -> str | None:
    """Only a ticked box says something. The box is unticked on nearly every record, so unticked is not "No"."""
    return "Yes" if candidate.sponsorship_required is True else None


def map_seniority(candidate: sf.SfCandidate) -> str | None:
    labels = []
    for part in re.split(r"[,;]+", candidate.experience_level):
        label = SENIORITY.get(" ".join(re.sub(r"[_\-]+", " ", part).split()).lower())
        if label and label not in labels:
            labels.append(label)
    while labels and len(", ".join(labels)) > 40:
        labels.pop()
    return ", ".join(labels) or None


def map_states(text: str) -> list[str]:
    """US states named in a free-text list of locations, as two-letter codes in the order written. A state is a
    full state name (any case) or an upper-case two-letter code standing alone; cities, regions, "anywhere" and
    misspellings are ignored, never guessed."""
    states = []
    for part in re.split(r"[,;/|\n]+|\s+(?:or|and|&)\s+", re.sub(r"\([^)]*\)", " ", text), flags=re.IGNORECASE):
        token = " ".join(part.strip(" .").split())
        code = token if len(token) == 2 and token.isupper() and token in US_STATES else _STATE_BY_NAME.get(token.lower())
        if code and code not in states:
            states.append(code)
    return states[:30]


def map_profile(candidate: sf.SfCandidate) -> dict:
    """Portal profile values from a Salesforce candidate. None / [] = Salesforce has nothing for that field."""
    return {"desired_titles": map_titles(candidate), "years_experience": map_years(candidate), "work_mode": map_work_mode(candidate),
            "work_authorization": map_authorization(candidate), "sponsorship_needed": map_sponsorship(candidate),
            "seniority": map_seniority(candidate), "preferred_states": map_states(candidate.preferred_locations)}


def map_details(candidate: sf.SfCandidate) -> dict:
    email = candidate.email.strip().lower()
    phone = " ".join(_PHONE_JUNK.sub(" ", candidate.phone).split())[:40]
    return {"name": candidate.name[:160], "email": email if len(email) <= 320 and _EMAIL.fullmatch(email) else "", "phone": phone}


def notes_line(candidate: sf.SfCandidate) -> str:
    """The original location text, kept in the notes so nothing the mapping could not read is lost."""
    text = _real(candidate.preferred_locations)
    return f"{NOTES_HEADING}{text}" if text else ""


# ---------------------------------------------------------------- who may find what
async def team_lead_filter(db: AsyncSession, settings: Settings, principal: Principal) -> str | None:
    """None when the caller may find every Salesforce candidate (an Admin always; a Team Lead under scope "all").
    Otherwise the e-mail address a candidate's marketing team lead must have: the Team Lead's own sign-in address."""
    if principal.is_admin:
        return None
    if not principal.is_team_lead:
        raise forbid()
    from . import settings as settings_service
    portal = await settings_service.load(db, settings)
    return None if portal.salesforce_team_lead_scope == "all" else principal.user.email.strip().lower()


def visible(candidate: sf.SfCandidate, team_lead_email: str | None) -> bool:
    """Checked in the portal for every candidate that comes back, whatever the query asked for."""
    return team_lead_email is None or (bool(team_lead_email) and candidate.marketing_team_lead_email.lower() == team_lead_email)


def can_see_local(principal: Principal, team_id: uuid.UUID | None) -> bool:
    return principal.is_admin or bool(principal.is_team_lead and principal.user.team_id and team_id == principal.user.team_id)


async def search_items(db: AsyncSession, principal: Principal, found: list[sf.SfCandidate], with_resume: set[str]) -> list[SalesforceCandidate]:
    """Search results with whether each is already in the portal. A candidate imported by a team the caller cannot
    see is marked `imported_elsewhere` and nothing else about it is told."""
    rows = (await db.execute(select(Candidate.salesforce_id, Candidate.id, Candidate.team_id)
                             .where(Candidate.salesforce_id.in_([c.id for c in found])))).all() if found else []
    local = {row[0]: (row[1], row[2]) for row in rows}
    items = []
    for c in found:
        here = local.get(c.id)
        mine = here is not None and can_see_local(principal, here[1])
        items.append(SalesforceCandidate(
            salesforce_id=c.id, name=c.name, email=c.email, phone=c.phone, candidate_status=c.candidate_status, technology=c.technology,
            niche=c.niche, work_authorization=c.work_authorization, visa_status=c.visa_status, years_experience=c.years_experience,
            preferred_job_type=c.preferred_job_type, preferred_locations=c.preferred_locations, experience_level=c.experience_level,
            assigned_recruiter=c.assigned_recruiter, marketing_team_lead=c.marketing_team_lead, has_resume=c.id in with_resume,
            imported_candidate_id=here[0] if mine else None, imported_elsewhere=here is not None and not mine, importable=here is None))
    return items


# ---------------------------------------------------------------- reading Salesforce (no database work)
@dataclass
class Fetched:
    """Everything one import or refresh needs from Salesforce, read before the database is touched."""
    candidate: sf.SfCandidate
    resume: sf.SfResume | None = None
    data: bytes | None = None
    resume_problem: str = ""                          # refused_type | refused_size | unavailable
    instance_url: str = ""


async def fetch_candidate(client, settings: Settings, salesforce_id: str, team_lead_email: str | None) -> sf.SfCandidate:
    try:
        candidate = await client.get_candidate(salesforce_id)
    except sf.SalesforceError as problem:
        raise as_http(problem) from None
    if candidate is None or not visible(candidate, team_lead_email):
        raise HTTPException(404, "Salesforce candidate not found")
    return candidate


async def fetch_resume(client, settings: Settings, fetched: Fetched, skip_versions: set[str]) -> None:
    """Find the candidate's newest active resume file and, unless the portal already has that version, download
    it. Only PDF and DOCX, only up to the upload limit, and only when the bytes are what the extension says.
    A problem here never fails the import: it is recorded and the candidate comes without a resume."""
    try:
        fetched.resume = await client.resume_of(fetched.candidate.id)
        resume = fetched.resume
        if resume is None or resume.content_version_id in skip_versions:
            return
        if resume.extension not in sf.RESUME_EXTENSIONS:
            fetched.resume_problem = "refused_type"
        elif resume.size > settings.max_upload_bytes:
            fetched.resume_problem = "refused_size"
        else:
            data = await client.download(resume, settings.max_upload_bytes)
            if sf.file_kind(data) != resume.extension:
                fetched.resume_problem = "refused_type"       # not the kind of file its name claims
            else:
                fetched.data = data
    except sf.FileTooLarge:
        fetched.resume_problem = "refused_size"
    except sf.SalesforceError:
        fetched.resume_problem = "unavailable"


# ---------------------------------------------------------------- writing the portal
@dataclass
class Outcome:
    updated: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


def _plain(value):
    return float(value) if isinstance(value, Decimal) else value


def _write(target, name: str, new, empty, previous: dict, outcome: Outcome) -> None:
    """The field rule. `previous` holds what the last sync wrote."""
    current = _plain(getattr(target, name))
    if new is None or new == empty or new == current:
        return
    if current == empty or (name in previous and current == previous[name]):
        setattr(target, name, new)
        outcome.updated.append(name)
    else:
        outcome.kept.append(name)


def _sync_notes(candidate: Candidate, line: str, previous_line: str, outcome: Outcome) -> None:
    notes = candidate.notes or ""
    if not line or line in notes:
        return
    if previous_line and previous_line in notes:
        updated = notes.replace(previous_line, line, 1)           # the line is still as the portal wrote it
    elif previous_line and line == previous_line:
        return                                                    # a manager removed or rewrote it: leave the notes alone
    else:
        updated = f"{notes.rstrip()}\n\n{line}" if notes.strip() else line
    if len(updated) <= NOTES_MAX:
        candidate.notes = updated
        outcome.updated.append("notes")


async def apply(db: AsyncSession, settings: Settings, principal: Principal, candidate: Candidate, fetched: Fetched, ip: str,
                *, created: bool) -> SalesforceSync:
    """Write what Salesforce has into a portal candidate, under the rule at the top of this module."""
    snapshot = dict(candidate.salesforce_snapshot or {})
    previous = dict(snapshot.get("fields") or {})
    outcome = Outcome()
    details, mapped = map_details(fetched.candidate), map_profile(fetched.candidate)
    for name, empty in CANDIDATE_FIELDS.items():
        if not created or name != "name":
            _write(candidate, name, details[name], empty, previous, outcome)
    profile = await people.get_profile(db, candidate.id)
    for name, empty in PROFILE_FIELDS.items():
        _write(profile, name, mapped[name], empty, previous, outcome)
    line = notes_line(fetched.candidate)
    _sync_notes(candidate, line, str(snapshot.get("notes_line") or ""), outcome)
    for name, value in {**details, **mapped}.items():
        if value not in (None, "", []):
            previous[name] = value
    candidate.salesforce_snapshot = {"fields": previous, "notes_line": line or snapshot.get("notes_line") or "",
                                     "instance_url": fetched.instance_url or snapshot.get("instance_url") or ""}
    candidate.salesforce_synced_at = utcnow()
    resume = await _sync_resume(db, settings, principal, candidate, fetched, ip)
    sync = SalesforceSync(created=created, updated_fields=[] if created else outcome.updated, kept_fields=outcome.kept, resume=resume)
    audit(db, principal.user, "candidate.imported_salesforce" if created else "candidate.salesforce_refreshed", "candidate", candidate.id,
          candidate.team_id, {"salesforce_id": candidate.salesforce_id, "updated": outcome.updated, "kept": outcome.kept, "resume": resume}, ip)
    return sync


async def known_versions(db: AsyncSession, candidate_id: uuid.UUID) -> set[str]:
    """Salesforce file versions this candidate already received (current or replaced)."""
    return set((await db.execute(select(Resume.salesforce_content_version_id).where(
        Resume.candidate_id == candidate_id, Resume.salesforce_content_version_id.is_not(None)))).scalars())


async def _sync_resume(db: AsyncSession, settings: Settings, principal: Principal, candidate: Candidate, fetched: Fetched, ip: str) -> str:
    if fetched.resume is None:
        return "none"
    if fetched.resume_problem:
        return fetched.resume_problem
    if fetched.data is None:
        return "unchanged"                                # the portal already received this version
    current = await people.current_resume(db, candidate.id)
    if current is not None and current.source != "salesforce":
        modified = fetched.resume.modified_at
        if modified is None or current.created_at >= modified:
            return "kept_local"                           # uploaded in the portal after the Salesforce file was last changed
    filename = f"{candidate.name} - Resume.{fetched.resume.extension}"
    try:
        await people.save_resume(db, settings, candidate, principal.user, filename, fetched.data,
                                 people.DOWNLOAD_TYPES[fetched.resume.extension], ip,
                                 salesforce_content_version_id=fetched.resume.content_version_id)
    except HTTPException as problem:                      # raised before anything was changed
        return "refused_size" if problem.status_code == 413 else "unreadable"
    return "imported"


async def new_candidate(db: AsyncSession, principal: Principal, fetched: Fetched, team_id: uuid.UUID | None,
                        assigned_member_id: uuid.UUID | None, ip: str) -> Candidate:
    """The local record for a Salesforce candidate that is not in the portal yet. Same team rules as a manual one."""
    if principal.is_team_lead:
        if not principal.user.team_id or (team_id is not None and team_id != principal.user.team_id):
            raise forbid()
        team_id = principal.user.team_id
    if team_id is None:
        raise HTTPException(422, "Choose the team this candidate belongs to")
    team = await db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found")
    if not team.is_active:
        raise HTTPException(409, "This team is deactivated")
    details = map_details(fetched.candidate)
    candidate = Candidate(team_id=team.id, name=details["name"] or "Salesforce candidate", is_active=True, created_by=principal.user.id,
                          salesforce_id=fetched.candidate.id, salesforce_snapshot={})
    db.add(candidate)
    try:
        await db.flush()
    except IntegrityError:                                # someone imported the same candidate a moment ago
        await db.rollback()
        raise HTTPException(409, "This candidate is already in the portal.") from None
    await people.get_profile(db, candidate.id)
    if assigned_member_id is not None:
        candidates_service.hand_over(db, principal.user, candidate,
                                     await candidates_service.member_for(db, principal, candidate, assigned_member_id), ip)
    return candidate


async def existing(db: AsyncSession, salesforce_id: str) -> Candidate | None:
    return (await db.execute(select(Candidate).where(Candidate.salesforce_id == salesforce_id))).scalar_one_or_none()
