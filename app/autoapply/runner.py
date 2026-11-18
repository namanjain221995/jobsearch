"""Worker jobs of auto-apply: prepare (inspect + answers + dry-run fill) and submit (after an Admin approval).

prepare: consent and resume checked, the form opened in a browser whose submissions are blocked, read, answered,
filled, checked for blockers, photographed. Ends in ready_for_review, needs_input or blocked. The guard's count of
submission-like requests that went out must be zero, otherwise the run fails loudly.

submit: consent, assignment and cap checked again; the form opened fresh and compared with the approved snapshot;
filled with the approved answers; blockers re-checked; the submit button clicked once. A confirmation page ->
submitted (assignment APPLIED through the status service, an admin-only application event, audit, notifications).
No submission request went out -> failed, safe to retry with a new run. A request went out but no confirmation was
seen, or anything broke after the click -> failed with outcome_unknown: never retried."""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import delete, select, update
from sqlalchemy.orm import undefer

from ..config import Settings
from .adapters import adapter_for
from .answers import Engine, Facts
from .browser import ApplyBrowser, screenshot
from .detect import detect
from .schema import Blocker, FormField, FormSchema, answers_hash, digest

log = logging.getLogger("jobfinder.autoapply")
MIME = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "txt": "text/plain"}


@dataclass
class RunnerDeps:
    sessions: object
    settings: Settings
    browser: ApplyBrowser
    engine_factory: object                     # () -> Engine
    robots_allowed: object = None              # async (url) -> bool; None = allowed
    last_open: dict = field(default_factory=dict)


def build_engine(settings: Settings) -> Engine:
    """Classifier: techsara-35b, then OpenAI, then Claude. Writer: OpenAI / Claude in AUTO_APPLY_DRAFT_PROVIDER order."""
    classifiers, writers = [], []
    openai = claude = None
    if settings.openai_api_key:
        from ..providers.openai_provider import OpenAIProvider
        openai = OpenAIProvider(settings.openai_api_key, settings.openai_model, settings.ai_timeout_seconds, settings.openai_reasoning_effort)
    if settings.anthropic_api_key:
        from ..providers.claude_provider import ClaudeProvider
        claude = ClaudeProvider(settings.anthropic_api_key, settings.anthropic_model, settings.ai_timeout_seconds)
    if settings.auto_apply_ai_classify:
        if settings.techsara_configured:
            from ..providers.techsara_provider import TechsaraProvider
            classifiers.append(TechsaraProvider(settings.techsara_api_key, settings.techsara_base_url, settings.techsara_model,
                                                settings.techsara_rerank_model, settings.techsara_timeout_seconds, 1, 1))
        classifiers += [p for p in (openai, claude) if p]
    for name in [n.strip().lower() for n in settings.auto_apply_draft_provider.split(",")]:
        p = {"openai": openai, "claude": claude}.get(name)
        if p and p not in writers:
            writers.append(p)
    return Engine(classifiers=classifiers, writers=writers, max_draft_calls=2)


def _now():
    from ..portal.security import utcnow
    return utcnow()


async def _finish(deps: RunnerDeps, run_id: uuid.UUID, **values) -> None:
    from ..portal.models import AutoApplyRun
    from ..portal.services.auto_apply import notify_outcome
    async with deps.sessions() as db:
        run = await db.get(AutoApplyRun, run_id, with_for_update=True)
        if run is None:
            return
        if run.status == "cancelled" and values.get("status") not in ("failed",):
            await db.commit()                      # cancelled meanwhile (consent revoked, Admin): keep it cancelled
            return
        for k, v in values.items():
            setattr(run, k, v)
        if run.status in ("submitted", "failed", "blocked", "cancelled"):
            run.finished_at = _now()
        await notify_outcome(db, run)
        await db.commit()


async def _pace(deps: RunnerDeps, url: str) -> None:
    host = urlsplit(url).hostname or ""
    wait = deps.last_open.get(host, 0) + deps.settings.auto_apply_host_interval_seconds - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)
    deps.last_open[host] = time.monotonic()


async def _facts(db, run) -> tuple[Facts | None, tuple | None, str]:
    from ..portal.models import Candidate, CandidateProfile, JobPosting, Resume
    from ..portal.services.auto_apply import active_consent
    from ..portal.services.people import resume_extension
    consent = await active_consent(db, run.candidate_id)
    if consent is None:
        return None, None, "no_consent"
    candidate = await db.get(Candidate, run.candidate_id)
    profile = await db.get(CandidateProfile, run.candidate_id)
    posting = await db.get(JobPosting, run.job_id)
    resume = (await db.execute(select(Resume).where(Resume.candidate_id == run.candidate_id, Resume.is_current)
                               .options(undefer(Resume.file_data)))).scalar_one_or_none()
    file = None
    if resume is not None and resume.file_data is not None:
        ext = resume_extension(resume.filename) or "txt"
        file = (resume.filename, MIME.get(ext, "application/octet-stream"), bytes(resume.file_data))
    facts = Facts(candidate_name=candidate.name, email=candidate.email, phone=candidate.phone, answers_raw=dict(profile.application_answers or {}) if profile else {},
                  profile=profile, candidate=candidate, resume_filename=file[0] if file else None, resume_text=resume.text if resume else "",
                  company=posting.company, job_title=posting.title, job_description=posting.description or "",
                  job_requirements=list(posting.requirements or []), from_salesforce=bool(candidate.salesforce_id),
                  may_attest=consent.may_attest, may_accept_data_processing=consent.may_accept_data_processing)
    return facts, file, ""


def _schema_from(run) -> FormSchema:
    fields = [FormField(**{k: v for k, v in f.items() if k in FormField.__dataclass_fields__}) for f in run.form_schema or []]
    return FormSchema(ats=run.ats, url=run.job_url, fields=fields)


async def _store_shot(deps: RunnerDeps, run_id, kind: str, data: bytes | None) -> None:
    if not data:
        return
    from ..portal.models import AutoApplyScreenshot
    async with deps.sessions() as db:
        db.add(AutoApplyScreenshot(run_id=run_id, kind=kind, size_bytes=len(data), data=data))
        await db.commit()


async def _claim(deps: RunnerDeps, run_id: uuid.UUID, frm: tuple[str, ...], to: str):
    from ..portal.models import AutoApplyRun
    async with deps.sessions() as db:
        row = (await db.execute(update(AutoApplyRun).where(AutoApplyRun.id == run_id, AutoApplyRun.status.in_(frm))
                                .values(status=to, started_at=_now(), attempts=AutoApplyRun.attempts + 1)
                                .returning(AutoApplyRun.id))).scalar_one_or_none()
        await db.commit()
        if row is None:
            return None
        run = await db.get(AutoApplyRun, run_id)
        db.expunge(run)
        return run


# ---------------------------------------------------------------- prepare
async def prepare(run_id: uuid.UUID, deps: RunnerDeps) -> None:
    run = await _claim(deps, run_id, ("queued", "preparing"), "preparing")
    if run is None:
        return
    try:
        await asyncio.wait_for(_prepare(run, deps), timeout=deps.settings.auto_apply_run_timeout_seconds)
    except asyncio.TimeoutError:
        await _finish(deps, run.id, status="failed", error="timeout while preparing")
    except Exception as exc:
        log.warning("auto-apply prepare failed: %s", type(exc).__name__, exc_info=True)
        await _finish(deps, run.id, status="failed", error=f"error while preparing ({type(exc).__name__})")


async def _prepare(run, deps: RunnerDeps) -> None:
    async with deps.sessions() as db:
        facts, file, problem = await _facts(db, run)
    if problem:
        await _finish(deps, run.id, status="blocked", blocked_reasons=[{"code": problem, "detail": "No active consent"}])
        return
    target = detect(run.job_url)
    if target.ats != run.ats or target.ats == "unsupported":
        await _finish(deps, run.id, status="blocked", blocked_reasons=[{"code": target.code or "unsupported_site", "detail": target.reason}])
        return
    if deps.settings.auto_apply_respect_robots and deps.robots_allowed is not None and not await deps.robots_allowed(target.url):
        await _finish(deps, run.id, status="blocked", blocked_reasons=[{"code": "robots_disallowed", "detail": "robots.txt disallows the form page"}])
        return
    adapter = adapter_for(run.ats)
    await _pace(deps, target.url)
    async with deps.browser.session(dry_run=True) as (page, guard, proxy):
        status = await adapter.open(page, target.url)
        guard.armed = True
        schema = await adapter.inspect(page, target.url)
        if status >= 400 and not schema.fields:
            schema.blockers.append(Blocker("posting_closed", f"HTTP {status}"))
        prepared = await deps.engine_factory().prepare(schema.fields, facts, run.admin_edits or {}) if schema.fields else None
        answers = [a.as_dict() for a in prepared.answers] if prepared else []
        blockers = [b.as_dict() for b in schema.blockers] + ([b.as_dict() for b in prepared.blockers] if prepared else [])
        fill = []
        if prepared and not schema.blockers:
            fill = await adapter.fill(page, schema, answers, {a["field_key"]: file for a in answers if a["source"] == "resume_file" and file})
            blockers += [b.as_dict() for b in await adapter.detect_blockers(page) if b.code not in {x["code"] for x in blockers}]
        shot = await screenshot(page, deps.settings.auto_apply_screenshot_max_bytes)
        report = guard.report(proxy)
    if report["allowed_submissions"] != 0:                   # cannot happen with the guard in place: fail loudly if it ever does
        await _finish(deps, run.id, status="failed", error="dry-run guard let a submission through", guard_report=report)
        return
    await _store_shot(deps, run.id, "dry_run", shot)
    needs = prepared.needs_input if prepared else []
    for entry in fill:
        if not entry["filled"]:
            needs.append({"field_key": entry["field_key"], "label": entry["label"], "reason": "no_matching_option", "fill_in": "run_answer"})
    if blockers:
        status = "blocked"
    elif needs:
        status = "needs_input"
    else:
        status = "ready_for_review"
    schema_hash = schema.schema_hash()
    await _finish(deps, run.id, status=status, form_schema=schema.snapshot(), schema_hash=schema_hash, answers=answers,
                  answers_hash=answers_hash(answers, schema_hash) if status == "ready_for_review" else "", blocked_reasons=blockers[:30],
                  needs_input=needs[:100], fill_report=fill, guard_report=report, ai_usage=prepared.usage if prepared else {}, error="")


# ---------------------------------------------------------------- submit
async def submit(run_id: uuid.UUID, deps: RunnerDeps) -> None:
    from sqlalchemy.exc import IntegrityError
    try:
        run = await _claim(deps, run_id, ("approved",), "submitting")
    except IntegrityError:
        await _finish(deps, run_id, status="failed", error="already_submitted: this posting was already applied to for the candidate")
        return
    if run is None:
        return
    state = {"clicked": False}
    try:
        await asyncio.wait_for(_submit(run, deps, state), timeout=deps.settings.auto_apply_run_timeout_seconds)
    except asyncio.TimeoutError:
        await _finish(deps, run.id, status="failed", outcome_unknown=state["clicked"],
                      error="timeout " + ("after the submit click: check the employer's site" if state["clicked"] else "before submitting"))
    except Exception as exc:
        log.warning("auto-apply submit failed: %s", type(exc).__name__, exc_info=True)
        await _finish(deps, run.id, status="failed", outcome_unknown=state["clicked"],
                      error=f"error {'after' if state['clicked'] else 'before'} the submit click ({type(exc).__name__})")


async def _submit(run, deps: RunnerDeps, state: dict) -> None:
    from ..portal.models import AutoApplyRun
    from ..portal.services.auto_apply import Refused, check_approvable
    async with deps.sessions() as db:
        facts, file, problem = await _facts(db, run)
        refusal = None
        if not problem:
            probe = await db.get(AutoApplyRun, run.id)
            saved = (probe.status, probe.answers_hash)
            probe.status = "ready_for_review"              # re-use the approval checks on the approved state
            try:
                settings = deps.settings
                await check_approvable(db, settings, probe, saved[1])
            except Refused as exc:
                refusal = exc
            await db.rollback()
    if problem or refusal:
        code = problem or refusal.reason
        await _finish(deps, run.id, status="blocked" if code in ("no_consent", "employer_daily_cap") else "failed",
                      blocked_reasons=[{"code": code, "detail": refusal.detail if refusal else "No active consent"}],
                      error=code)
        return
    adapter = adapter_for(run.ats)
    approved = _schema_from(run)
    await _pace(deps, run.job_url)
    async with deps.browser.session(dry_run=False) as (page, guard, proxy):
        await adapter.open(page, run.job_url)
        guard.armed = True
        schema = await adapter.inspect(page, run.job_url)
        if schema.blockers:
            await _finish(deps, run.id, status="blocked", blocked_reasons=[b.as_dict() for b in schema.blockers])
            return
        if schema.schema_hash() != run.schema_hash:
            await _finish(deps, run.id, status="blocked", blocked_reasons=[{"code": "form_changed", "detail": "The form differs from the approved preview"}])
            return
        answers = run.answers or []
        fill = await adapter.fill(page, schema, answers, {a["field_key"]: file for a in answers if a.get("source") == "resume_file" and file})
        failed = [e for e in fill if not e["filled"]]
        blockers = await adapter.detect_blockers(page)
        if failed or blockers:
            await _finish(deps, run.id, status="blocked" if blockers else "failed", fill_report=fill,
                          blocked_reasons=[b.as_dict() for b in blockers], error="" if blockers else "a field could not be filled; nothing was submitted")
            return
        await _store_shot(deps, run.id, "before_submit", await screenshot(page, deps.settings.auto_apply_screenshot_max_bytes))
        async with deps.sessions() as db:                   # last consent check right before the click
            from ..portal.services.auto_apply import active_consent
            if await active_consent(db, run.candidate_id) is None:
                await _finish(deps, run.id, status="cancelled", error="consent_revoked")
                return
        state["clicked"] = True
        result = await adapter.submit(page, guard)
        if not result.get("clicked"):
            state["clicked"] = False
            await _finish(deps, run.id, status="failed", error=result.get("error", "submit button not found"))
            return
        evidence = await adapter.confirm(page)
        after = await adapter.detect_blockers(page)
        shot = await screenshot(page, deps.settings.auto_apply_screenshot_max_bytes)
        report = guard.report(proxy)
    await _store_shot(deps, run.id, "confirmation", shot)
    confirmation = {k: evidence[k] for k in ("detected", "text_sha256", "reference", "url")}
    if evidence["detected"]:
        await _mark_submitted(deps, run, confirmation, report)
        return
    if result.get("requests", 0) == 0:
        codes = [b.as_dict() for b in after]
        await _finish(deps, run.id, status="blocked" if codes else "failed", blocked_reasons=codes, confirmation=confirmation,
                      guard_report=report, error="" if codes else "the form was not sent (validation errors on the page)")
        return
    await _finish(deps, run.id, status="failed", outcome_unknown=True, confirmation=confirmation, guard_report=report,
                  error="outcome_unknown: the form was sent but no confirmation was shown; check the employer's site or the candidate's mailbox")


async def _mark_submitted(deps: RunnerDeps, run, confirmation: dict, report: dict) -> None:
    from ..portal.deps import Principal
    from ..portal.models import ApplicationEvent, AutoApplyRun, JobAssignment, User
    from ..portal.schemas import StatusUpdate
    from ..portal.services import assignments as assignments_service
    from ..portal.services.audit import audit
    from ..portal.services.auto_apply import notify_outcome
    async with deps.sessions() as db:
        row = await db.get(AutoApplyRun, run.id, with_for_update=True)
        assignment = await db.get(JobAssignment, run.assignment_id, with_for_update=True)
        actor = await db.get(User, row.approved_by) if row.approved_by else None
        now = _now()
        row.status, row.submitted_at, row.finished_at, row.confirmation, row.guard_report = "submitted", now, now, confirmation, report
        if assignment is not None and assignment.is_active and assignment.applied_at is None and actor is not None:
            await assignments_service.set_status(db, Principal(user=actor, session=None), assignment,
                                                 StatusUpdate(status="APPLIED", reference=(confirmation.get("reference") or None)))
        elif assignment is not None and assignment.applied_at is None:
            assignment.status, assignment.applied_at = "APPLIED", now
            if confirmation.get("reference"):
                assignment.application_reference = confirmation["reference"][:200]
        db.add(ApplicationEvent(assignment_id=run.assignment_id, actor_id=row.approved_by, event_type="auto_apply_submitted",
                                old_status=None, new_status="APPLIED",
                                meta={"source": "auto_apply", "admin_only": True, "run_id": str(run.id), "answers_hash": row.answers_hash,
                                      "schema_hash": row.schema_hash, "confirmation_sha256": confirmation.get("text_sha256", ""),
                                      "reference_recorded": bool(confirmation.get("reference"))}))
        audit(db, actor, "auto_apply.submitted", "auto_apply_run", run.id, assignment.team_id if assignment else None,
              {"assignment_id": str(run.assignment_id), "answers_hash": row.answers_hash, "confirmation_sha256": confirmation.get("text_sha256", "")})
        await notify_outcome(db, row)
        await db.commit()


# ---------------------------------------------------------------- housekeeping
async def cleanup(deps: RunnerDeps) -> int:
    from ..portal.models import AutoApplyScreenshot
    async with deps.sessions() as db:
        cut = _now() - timedelta(days=max(0, deps.settings.auto_apply_screenshot_retention_days))
        ids = (await db.execute(delete(AutoApplyScreenshot).where(AutoApplyScreenshot.created_at < cut).returning(AutoApplyScreenshot.id))).scalars().all()
        await db.commit()
        return len(ids)


async def recover(deps: RunnerDeps) -> None:
    """After a worker restart: a run stopped while preparing is queued again; one stopped while submitting may have
    gone through, so it is failed with outcome_unknown and never retried."""
    from ..portal.models import AutoApplyRun
    async with deps.sessions() as db:
        await db.execute(update(AutoApplyRun).where(AutoApplyRun.status == "preparing").values(status="queued"))
        await db.execute(update(AutoApplyRun).where(AutoApplyRun.status == "submitting").values(
            status="failed", outcome_unknown=True, finished_at=_now(),
            error="outcome_unknown: the worker stopped while submitting; check the employer's site"))
        await db.commit()


def run_digest(value) -> str:
    return digest(value)
