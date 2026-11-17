"""Admin-editable application settings stored in PostgreSQL (app_settings), with defaults from the environment."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from ..models import AppSetting, User
from ..schemas import SettingsOut, SettingsUpdate, UiSettings

KEY = "portal"


def _defaults(settings: Settings) -> dict:
    return SettingsOut(
        default_min_match_score=settings.default_min_match_score,
        max_members_per_search=settings.max_members_per_search,
        max_results_per_search_run=settings.max_results_per_search_run,
        invitation_hours=settings.invitation_hours,
        disabled_sources=[s.strip().lower() for s in settings.disabled_sources.split(",") if s.strip()],
    ).model_dump()


async def load(db: AsyncSession, settings: Settings) -> SettingsOut:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == KEY))).scalar_one_or_none()
    data = _defaults(settings)
    if row:
        data.update({k: v for k, v in row.value.items() if k in data})
    return SettingsOut(**data)


async def save(db: AsyncSession, settings: Settings, actor: User, patch: SettingsUpdate) -> SettingsOut:
    current = (await load(db, settings)).model_dump()
    current.update(patch.model_dump(exclude_none=True))
    current["disabled_sources"] = sorted({str(s).strip().lower() for s in current["disabled_sources"] if str(s).strip()})
    row = (await db.execute(select(AppSetting).where(AppSetting.key == KEY))).scalar_one_or_none()
    if row:
        row.value, row.updated_by = current, actor.id
    else:
        db.add(AppSetting(key=KEY, value=current, updated_by=actor.id))
    return SettingsOut(**current)


def ui(settings_out: SettingsOut) -> UiSettings:
    return UiSettings(watermark_enabled=settings_out.watermark_enabled, default_target_count=settings_out.default_target_count,
                      max_members_per_search=settings_out.max_members_per_search,
                      max_results_per_search_run=settings_out.max_results_per_search_run,
                      default_min_match_score=settings_out.default_min_match_score)
