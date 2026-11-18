"""Admin-editable application settings stored in PostgreSQL (app_settings), with defaults from the environment."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from ..models import AppSetting, User
from fastapi import HTTPException
from sqlalchemy import delete, func, text

from ..models import Candidate, SearchRun, Session, Team
from ..schemas import ExtraField, SettingsOut, SettingsUpdate, UiSettings
from .audit import audit

KEY = "portal"


def _defaults(settings: Settings) -> dict:
    return SettingsOut(
        default_min_match_score=settings.default_min_match_score,
        max_members_per_search=settings.max_members_per_search,
        max_results_per_search_run=settings.max_results_per_search_run,
        invitation_hours=settings.invitation_hours,
        disabled_sources=[s.strip().lower() for s in settings.disabled_sources.split(",") if s.strip()],
        password_min_length=settings.password_min_length,
    ).model_dump()


async def load(db: AsyncSession, settings: Settings) -> SettingsOut:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == KEY))).scalar_one_or_none()
    data = _defaults(settings)
    if row:
        data.update({k: v for k, v in row.value.items() if k in data and k != "password_min_length"})
    data["profile_extra_fields"] = valid_extra_fields(data.get("profile_extra_fields"))
    return SettingsOut(**data)


def valid_extra_fields(stored) -> list[dict]:
    """Field definitions as stored, minus any that no longer fit the schema (an older release kept free-form dicts)."""
    out, seen = [], set()
    for item in stored or []:
        try:
            field = ExtraField.model_validate(item)
        except ValueError:
            continue
        if field.key not in seen:
            seen.add(field.key)
            out.append(field.model_dump())
    return out


async def save(db: AsyncSession, settings: Settings, actor: User, patch: SettingsUpdate) -> SettingsOut:
    current = (await load(db, settings)).model_dump()
    current.update(patch.model_dump(exclude_none=True))
    current.pop("password_min_length", None)             # environment setting, never stored
    current["disabled_sources"] = sorted({str(s).strip().lower() for s in current["disabled_sources"] if str(s).strip()})
    row = (await db.execute(select(AppSetting).where(AppSetting.key == KEY))).scalar_one_or_none()
    if row:
        row.value, row.updated_by = current, actor.id
    else:
        db.add(AppSetting(key=KEY, value=current, updated_by=actor.id))
    if patch.profile_extra_fields is not None:
        from . import people                               # imported here: people does not need this module, and stays that way
        dropped = await people.drop_unfitting_extra(db, patch.profile_extra_fields)
        if dropped:
            audit(db, actor, "settings.extra_answers_deleted", "settings", None, None, {"answers": dropped})
    return SettingsOut(**{**current, "password_min_length": settings.password_min_length})


# Everything people made while trying the portal out. Not in the list, so kept: the Admin accounts, app_settings,
# the job board index (index_*), the monthly AWS web search count and alembic_version.
TEST_DATA_TABLES = ("application_events", "assignment_notes", "audit_logs", "auto_apply_screenshots", "auto_apply_runs",
                    "candidate_consents", "candidate_profiles", "resumes", "search_results", "search_targets", "search_presets",
                    "team_job_queue", "job_assignments", "search_runs", "job_postings", "legacy_records", "notifications",
                    "password_resets", "invitations", "team_lead_assignments", "candidates")


async def set_test_mode(db: AsyncSession, settings: Settings, on: bool, actor: User | None = None) -> None:
    current = (await load(db, settings)).model_dump()
    current.pop("password_min_length", None)
    current["test_mode"] = on
    row = (await db.execute(select(AppSetting).where(AppSetting.key == KEY))).scalar_one_or_none()
    if row:
        row.value, row.updated_by = current, actor.id if actor else row.updated_by
    else:
        db.add(AppSetting(key=KEY, value=current, updated_by=actor.id if actor else None))


async def go_live(db: AsyncSession, settings: Settings, actor: User, keep_session, ip: str) -> dict[str, int]:
    """End test mode: delete every team, Team Lead, member, candidate, search, job and log entry, keep the Admin
    accounts, the settings and the job board index, and switch test mode off. Works once: without test mode it is
    refused, so the portal's real data can never be deleted from the screen. The caller commits."""
    if not (await load(db, settings)).test_mode:
        raise HTTPException(409, "The portal is not in test mode. There is no test data to remove.")
    active = (await db.execute(select(func.count()).select_from(SearchRun).where(SearchRun.status.in_(("queued", "running"))))).scalar_one()
    if active:
        raise HTTPException(409, "A search is queued or running. Wait until it has finished or cancel it, then try again.")
    removed = {"teams": (await db.execute(select(func.count()).select_from(Team))).scalar_one(),
               "users": (await db.execute(select(func.count()).select_from(User).where(User.role != "ADMIN"))).scalar_one(),
               "candidates": (await db.execute(select(func.count()).select_from(Candidate))).scalar_one(),
               "searches": (await db.execute(select(func.count()).select_from(SearchRun))).scalar_one()}
    await db.execute(text("TRUNCATE " + ", ".join(TEST_DATA_TABLES)))
    await db.execute(delete(Session).where(Session.id != keep_session))       # the Admin who asked stays signed in
    await db.execute(delete(User).where(User.role != "ADMIN"))
    await db.execute(delete(Team))
    await set_test_mode(db, settings, False, actor)
    audit(db, actor, "data.test_data_removed", "settings", None, None, removed, ip)
    return removed


def ui(settings_out: SettingsOut, settings: Settings | None = None) -> UiSettings:
    providers = [name for name, key in (("OpenAI", settings.openai_api_key), ("Anthropic", settings.anthropic_api_key),
                                        ("TechSara (company model)", settings.techsara_api_key)) if key] if settings else []
    return UiSettings(watermark_enabled=settings_out.watermark_enabled, default_target_count=settings_out.default_target_count,
                      max_members_per_search=settings_out.max_members_per_search,
                      max_results_per_search_run=settings_out.max_results_per_search_run,
                      max_target_count=settings.result_cap if settings else 1000,
                      default_min_match_score=settings_out.default_min_match_score,
                      deep_search_default=settings_out.deep_search_default, password_min_length=settings_out.password_min_length,
                      profile_extra_fields=settings_out.profile_extra_fields, ai_providers=providers)
