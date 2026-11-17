"""Global job postings: one row per distinct vacancy, whichever way it entered the system
(search engine result, manual entry, URL import, CSV import)."""
import csv
import hashlib
import io
import re
import uuid
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...deduplicator import norm_location, norm_title
from ...job_normalizer import US_STATES, canonical_url, html_to_structured_text, norm_company
from ...schemas import Candidate
from ...schemas import Job as EngineJob
from ..deps import Principal
from ..models import JobAssignment, JobPosting, SearchResult, SearchRun, Team, TeamJobQueue, User
from ..schemas import (CsvImportResult, JobAssignee, JobCreate, JobDetail, JobImportDraft, JobListItem, JobManagerView,
                       JobSummary, JobUpdate)
from ..security import utcnow
from .audit import audit
from .common import team_ref, teams_by_id, user_ref, users_by_id

_STATE_BY_NAME = {name: abbr for abbr, name in US_STATES.items()}
_NOT_A_CITY = {"us", "usa", "united states", "united states of america", "remote", "hybrid", "on-site", "onsite", "u.s.", "u.s.a."}
_TAG = re.compile(r"<[a-zA-Z/][^>]*>")


def ctl_key(company: str, title: str, location: str) -> str:
    return f"{norm_company(company)}|{norm_title(title)}|{norm_location(location)}"[:400]


def url_identity(url: str) -> str:
    return "url:" + hashlib.sha1(canonical_url(url).encode()).hexdigest()


def parse_location(text: str) -> tuple[str, str]:
    """Best-effort (state, city) from the first listed location. Unknown parts stay empty; nothing is guessed."""
    first = re.split(r"[;|/]", text or "")[0]
    parts = [p.strip(" ()") for p in first.split(",") if p.strip(" ()")]
    codes = [p for p in parts if re.fullmatch(r"[A-Z]{2}", p)]
    if any(code not in US_STATES and code != "US" for code in codes):
        return "", ""                                   # "Toronto, ON, CA", "London, UK": not a US address
    index = next((i for i, part in enumerate(parts) if part in US_STATES), None)
    if index is None:                                   # full state name; the last one wins ("New York, New York")
        index = next((i for i in range(len(parts) - 1, -1, -1) if parts[i].lower() in _STATE_BY_NAME), None)
    if index is None:
        return "", ""
    state = parts[index] if parts[index] in US_STATES else _STATE_BY_NAME[parts[index].lower()]
    city = ""
    for i, part in enumerate(parts):
        if i != index and part.lower() not in _NOT_A_CITY and part not in US_STATES and not part.isdigit():
            city = re.sub(r"^(US|USA)\s*-\s*", "", part)[:120]
            break
    return state, city


def parse_salary(text: str) -> tuple[int | None, int | None, str]:
    """(min, max, currency) only when the published text clearly contains amounts."""
    if not text or text == "Not listed":
        return None, None, ""
    amounts = []
    for raw in re.findall(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?\s?[kK]\b|\d{4,7}", text):
        value = float(raw.lower().replace("k", "").replace(",", "").strip())
        if raw.strip().lower().endswith("k"):
            value *= 1000
        if 1_000 <= value <= 5_000_000:
            amounts.append(int(value))
    if not amounts:
        return None, None, ""
    currency = "USD" if ("$" in text or "USD" in text.upper()) else next((c for c in ("GBP", "EUR", "CAD", "AUD") if c in text.upper()), "")
    low, high = amounts[0], (amounts[1] if len(amounts) > 1 else amounts[0])
    return min(low, high), max(low, high), currency


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if value else None
    except ValueError:
        return None


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def clean_text(value: str) -> str:
    """Stored descriptions are plain text. Anything that looks like markup is converted, never kept as HTML:
    not a tag written with entities ("&lt;img ...&gt;" would otherwise come out as a real tag after decoding),
    and not a tag that never closes."""
    value = (value or "").replace("\x00", "")
    if _TAG.search(value) or "&" in value or "<" in value:
        value = html_to_structured_text(value)
    return value.strip()


def web_link(value: str) -> str:
    """The value when it is an http(s) URL with a host, else ''."""
    value = (value or "").strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    return value if parts.scheme in ("http", "https") and parts.hostname and len(value) <= 2000 else ""


def safe_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not canonical_url(value):
        raise HTTPException(422, "Links must be valid http(s) URLs")
    return value


# ---------------------------------------------------------------- from the search engine
async def upsert_engine_job(db: AsyncSession, job: EngineJob, created_by: uuid.UUID | None = None) -> JobPosting:
    """Store (or refresh) the global record of a posting the search engine verified."""
    if job.ats and job.ats_id:
        identity = f"ats:{job.ats}:{job.ats_id}"[:200]
    else:
        identity = url_identity(job.url)
    ctl = ctl_key(job.company, job.title, job.location)
    posting = (await db.execute(select(JobPosting).where(JobPosting.identity_key == identity))).scalar_one_or_none()
    if posting is None:
        # The same vacancy may have been stored earlier from another source (job-board copy vs employer posting).
        twin = (await db.execute(select(JobPosting).where(JobPosting.ctl_key == ctl).order_by(JobPosting.created_at).limit(1))).scalar_one_or_none()
        if twin is not None and not (twin.external_source == job.ats and twin.external_job_id and job.ats_id
                                     and twin.external_job_id != job.ats_id):
            posting = twin
    # Postings come from employers' boards and web pages: only http(s) links are ever stored.
    link, apply_link = web_link(job.url), web_link(job.apply_url)
    if not link:
        raise ValueError("Posting without a valid http(s) link")
    description = clean_text(job.description_rich or job.description or "")
    state, city = parse_location(job.location)
    salary_text = "" if job.salary == "Not listed" else job.salary
    smin, smax, currency = parse_salary(salary_text)
    fields = dict(
        company=job.company[:200], title=job.title[:300], country="United States", state=state, city=city,
        location_text=("" if job.location == "Not listed" else job.location)[:400], work_mode=job.work_mode,
        salary_text=salary_text[:160], salary_min=smin, salary_max=smax, salary_currency=currency,
        experience_text=("" if job.experience_required == "Not listed" else job.experience_required)[:80],
        experience_min=job.required_years, requirements=list(job.skills or [])[:60],
        citizenship_requirement=job.citizenship_requirement, sponsorship_status=job.sponsorship,
        clearance_required=job.clearance_required, posted_at=_parse_date(job.posted_at),
        last_verified_at=_parse_dt(job.verified_at) or utcnow(), verification_status="verified",
        verification_note=job.verification[:300], source_label=job.source[:80])
    if posting is None:
        posting = JobPosting(identity_key=identity, ctl_key=ctl, external_source=job.ats[:40], external_job_id=job.ats_id[:160],
                             origin="search", description=description, canonical_url=link, apply_url=apply_link,
                             sources=list(job.sources or [job.source]), created_by=created_by, **fields)
        db.add(posting)
        await db.flush()
        return posting
    for name, value in fields.items():
        if value not in (None, "", []) or name in ("last_verified_at", "verification_status"):
            setattr(posting, name, value)
    if len(description) > len(posting.description or ""):
        posting.description = description
    if apply_link and not posting.apply_url:
        posting.apply_url = apply_link
    if not posting.canonical_url:
        posting.canonical_url = link
    posting.sources = list(dict.fromkeys([*(posting.sources or []), *(job.sources or [job.source])]))
    return posting


# ---------------------------------------------------------------- manual entry / import
async def create_manual(db: AsyncSession, actor: User, data: JobCreate, origin: str = "manual") -> JobPosting:
    posting_url, apply_url, company_url = safe_url(data.canonical_url), safe_url(data.apply_url), safe_url(data.company_url)
    identity = url_identity(posting_url or apply_url) if (posting_url or apply_url) else f"manual:{uuid.uuid4()}"
    existing = (await db.execute(select(JobPosting).where(JobPosting.identity_key == identity))).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"This posting already exists (job {existing.id})")
    location = ", ".join(p for p in (data.city.strip(), data.state.strip()) if p)
    payload = data.model_dump(exclude={"canonical_url", "apply_url", "company_url", "description", "requirements"})
    if not payload.get("salary_text") and (data.salary_min or data.salary_max):
        low, high = data.salary_min or data.salary_max, data.salary_max or data.salary_min
        payload["salary_text"] = f"{data.salary_currency or 'USD'} {low:,} - {high:,}"
    posting = JobPosting(
        identity_key=identity, ctl_key=ctl_key(data.company, data.title, location), origin=origin, source_label="Manual entry",
        sources=["Manual entry"], location_text=location, description=clean_text(data.description),
        requirements=[str(r).strip()[:80] for r in data.requirements if str(r).strip()][:60],
        canonical_url=posting_url, apply_url=apply_url, company_url=company_url, created_by=actor.id,
        verification_status="unverified", verification_note="Entered manually; not yet verified against the posting.", **payload)
    db.add(posting)
    await db.flush()
    audit(db, actor, "job.created", "job", posting.id, None, {"origin": origin, "company": posting.company, "title": posting.title})
    return posting


async def update_job(db: AsyncSession, actor: User, posting: JobPosting, data: JobUpdate) -> None:
    changes = data.model_dump(exclude_unset=True)
    for name, value in changes.items():
        if name in ("canonical_url", "apply_url"):
            value = safe_url(value or "")
        elif name == "description":
            value = clean_text(value or "")
        elif value is None and name not in ("salary_min", "salary_max", "posted_at"):
            continue
        setattr(posting, name, value)
    if {"company", "title", "state", "city"} & set(changes):
        posting.location_text = ", ".join(p for p in (posting.city, posting.state) if p) or posting.location_text
        posting.ctl_key = ctl_key(posting.company, posting.title, posting.location_text)
    if {"canonical_url", "apply_url", "title", "company"} & set(changes) and posting.origin != "search":
        posting.verification_status = "unverified"            # edited by hand: the earlier check no longer covers it
        posting.verification_note = "Edited manually after the last verification."
    audit(db, actor, "job.updated", "job", posting.id, None, {"fields": sorted(changes)})


def draft_from_engine(job: EngineJob) -> JobCreate:
    state, city = parse_location(job.location)
    salary_text = "" if job.salary == "Not listed" else job.salary
    smin, smax, currency = parse_salary(salary_text)
    return JobCreate(
        company=job.company[:200], title=job.title[:300], state=state, city=city,
        work_mode=job.work_mode if job.work_mode in ("Remote", "Hybrid", "On-site") else "Not listed",
        salary_text=salary_text[:160], salary_min=smin, salary_max=smax, salary_currency=currency,
        experience_min=job.required_years, experience_text=("" if job.experience_required == "Not listed" else job.experience_required)[:80],
        description=(job.description_rich or job.description)[:60000], requirements=list(job.skills or [])[:60],
        canonical_url=job.url, apply_url=job.apply_url, citizenship_requirement=job.citizenship_requirement,
        sponsorship_status=job.sponsorship, clearance_required=job.clearance_required, posted_at=_parse_date(job.posted_at))


async def import_from_url(verifier, url: str) -> JobImportDraft:
    """Read a posting page (SSRF-guarded, robots.txt respected) and return what it states, for review."""
    if not canonical_url(url):
        raise HTTPException(422, "Enter a valid http(s) URL")
    from ...job_normalizer import enrich_job
    verdict = await verifier.verify(Candidate(url=url, provider="Import", cited=True), None)
    if verdict.job is None:
        return JobImportDraft(verified=False, note=verdict.reason or "The page could not be read as a job posting.", job=None)
    enrich_job(verdict.job)
    return JobImportDraft(verified=True, note=verdict.job.verification, job=draft_from_engine(verdict.job))


async def verify_posting(db: AsyncSession, verifier, actor: User, posting: JobPosting) -> None:
    """Check a posting's page now and record the outcome. A manual job becomes 'verified' only through this."""
    url = posting.canonical_url or posting.apply_url
    if not url:
        raise HTTPException(409, "This job has no posting URL to verify")
    verdict = await verifier.verify(Candidate(url=url, title=posting.title, company=posting.company, provider="Import", cited=True), None)
    if verdict.job is not None:
        posting.verification_status, posting.verification_note = "verified", verdict.job.verification[:300]
    else:
        closed = any(marker in (verdict.reason or "") for marker in ("closed", "not found", "expired"))
        posting.verification_status = "closed" if closed else "failed"
        posting.verification_note = (verdict.reason or "Verification failed")[:300]
    posting.last_verified_at = utcnow()
    audit(db, actor, "job.verified", "job", posting.id, None, {"status": posting.verification_status})


CSV_COLUMNS = ("company", "title", "state", "city", "work_mode", "employment_type", "salary_text", "experience_text",
               "description", "requirements", "canonical_url", "apply_url", "citizenship_requirement", "sponsorship_status", "posted_at")


async def import_csv(db: AsyncSession, actor: User, raw: bytes) -> CsvImportResult:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "The CSV file must be UTF-8 encoded") from None
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not {"company", "title"} <= {f.strip().lower() for f in reader.fieldnames}:
        raise HTTPException(400, "The CSV needs at least the columns: company, title")
    created = skipped = 0
    errors: list[str] = []
    for number, row in enumerate(reader, start=2):
        if number > 502:
            errors.append("Only the first 500 rows were read")
            break
        clean = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        data = {k: clean[k] for k in CSV_COLUMNS if clean.get(k)}
        if "requirements" in data:
            data["requirements"] = [s.strip() for s in re.split(r"[;|]", data["requirements"]) if s.strip()]
        try:
            job = JobCreate(**data)
            async with db.begin_nested():
                await create_manual(db, actor, job, origin="import")
            created += 1
        except HTTPException as exc:
            skipped += 1
            if len(errors) < 20:
                errors.append(f"Row {number}: {exc.detail}")
        except Exception as exc:
            skipped += 1
            if len(errors) < 20:
                errors.append(f"Row {number}: {str(exc).splitlines()[0][:160]}")
    return CsvImportResult(created=created, updated=0, skipped=skipped, errors=errors)


# ---------------------------------------------------------------- visibility + serialization
def tl_job_filter(team_id: uuid.UUID, user_id: uuid.UUID):
    """Jobs a Team Lead may see: assigned inside the team, delegated to the team, or found by the team's searches.
    Who started a search does not matter: a lead who moved to another team leaves the old team's results behind."""
    return or_(
        exists().where(JobAssignment.job_id == JobPosting.id, JobAssignment.team_id == team_id),
        exists().where(TeamJobQueue.job_id == JobPosting.id, TeamJobQueue.team_id == team_id),
        exists().where(SearchResult.job_id == JobPosting.id, SearchResult.search_run_id == SearchRun.id, SearchRun.team_id == team_id),
    )


async def visible_jobs(db: AsyncSession, principal: Principal, job_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    if not job_ids:
        return set()
    stmt = select(JobPosting.id).where(JobPosting.id.in_(job_ids))
    if principal.is_team_lead:
        if not principal.user.team_id:
            return set()
        stmt = stmt.where(tl_job_filter(principal.user.team_id, principal.user.id))
    elif not principal.is_admin:
        return set()
    return set((await db.execute(stmt)).scalars())


def job_summary(p: JobPosting) -> dict:
    return dict(
        id=p.id, company=p.company, title=p.title, country=p.country, state=p.state, city=p.city, location_text=p.location_text,
        work_mode=p.work_mode, employment_type=p.employment_type, seniority=p.seniority, salary_text=p.salary_text,
        salary_min=p.salary_min, salary_max=p.salary_max, salary_currency=p.salary_currency, experience_text=p.experience_text,
        requirements=p.requirements or [], citizenship_requirement=p.citizenship_requirement,
        sponsorship_status=p.sponsorship_status, clearance_required=p.clearance_required, posted_at=p.posted_at,
        last_verified_at=p.last_verified_at, verification_status=p.verification_status, source_label=p.source_label,
        origin=p.origin, has_apply_link=bool(p.apply_url or p.canonical_url), created_at=p.created_at)


def summary_out(p: JobPosting) -> JobSummary:
    return JobSummary(**job_summary(p))


_LINK = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"')\]]+")


def without_links(text: str) -> str:
    """The member's copy of a posting's text: web addresses written in it are taken out, so the only way to the
    posting is Open Job, which records that the link was opened."""
    return _LINK.sub("[link removed: use Open Job]", text or "")


def detail_out(p: JobPosting, hide_links: bool = False) -> JobDetail:
    description, note = p.description or "", p.verification_note
    if hide_links:
        description, note = without_links(description), without_links(note)
    return JobDetail(**job_summary(p), description=description, verification_note=note,
                     sources=[str(s) for s in (p.sources or [])])


async def list_items(db: AsyncSession, postings: list[JobPosting], scope_where: list) -> list[JobListItem]:
    ids = [p.id for p in postings]
    counts = {}
    if ids:
        rows = (await db.execute(
            select(JobAssignment.job_id, func.count(), func.count().filter(JobAssignment.applied_at.is_not(None)))
            .where(JobAssignment.job_id.in_(ids), JobAssignment.is_active, *scope_where).group_by(JobAssignment.job_id))).all()
        counts = {r[0]: (r[1], r[2]) for r in rows}
    return [JobListItem(**job_summary(p), canonical_url=p.canonical_url, assignment_count=counts.get(p.id, (0, 0))[0],
                        applied_count=counts.get(p.id, (0, 0))[1]) for p in postings]


async def manager_view(db: AsyncSession, principal: Principal, p: JobPosting) -> JobManagerView:
    where = [JobAssignment.job_id == p.id, JobAssignment.is_active]
    if principal.is_team_lead:
        where.append(JobAssignment.team_id == principal.user.team_id)
    assignments = list((await db.execute(select(JobAssignment).where(*where).order_by(JobAssignment.assigned_at.desc()).limit(200))).scalars())
    users = await users_by_id(db, {a.member_id for a in assignments})
    queue_where = [TeamJobQueue.job_id == p.id, TeamJobQueue.status == "pending"]
    if principal.is_team_lead:
        queue_where.append(TeamJobQueue.team_id == principal.user.team_id)
    queue_team_ids = set((await db.execute(select(TeamJobQueue.team_id).where(*queue_where))).scalars())
    teams = await teams_by_id(db, {a.team_id for a in assignments} | queue_team_ids)
    assignees = [JobAssignee(assignment_id=a.id, member=user_ref(users[a.member_id]), team=team_ref(teams.get(a.team_id)),
                             status=a.status, assigned_at=a.assigned_at, applied_at=a.applied_at)
                 for a in assignments if a.member_id in users]
    return JobManagerView(**job_summary(p), description=p.description or "", verification_note=p.verification_note,
                          sources=[str(s) for s in (p.sources or [])], canonical_url=p.canonical_url, apply_url=p.apply_url,
                          company_url=p.company_url, external_source=p.external_source, external_job_id=p.external_job_id,
                          assignment_count=len(assignees), queue_teams=[team_ref(teams[t]) for t in queue_team_ids if t in teams],
                          assignees=assignees)
