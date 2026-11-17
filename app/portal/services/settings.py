"""Admin-editable application settings stored in PostgreSQL (app_settings), with defaults from the environment."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from ..models import AppSetting, User
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


def ui(settings_out: SettingsOut, settings: Settings | None = None) -> UiSettings:
    providers = [name for name, key in (("OpenAI", settings.openai_api_key), ("Anthropic", settings.anthropic_api_key)) if key] if settings else []
    return UiSettings(watermark_enabled=settings_out.watermark_enabled, default_target_count=settings_out.default_target_count,
                      max_members_per_search=settings_out.max_members_per_search,
                      max_results_per_search_run=settings_out.max_results_per_search_run,
                      default_min_match_score=settings_out.default_min_match_score,
                      deep_search_default=settings_out.deep_search_default, password_min_length=settings_out.password_min_length,
                      profile_extra_fields=settings_out.profile_extra_fields, ai_providers=providers)
