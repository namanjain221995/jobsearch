"""Salesforce, read-only: connection status and the candidate search that feeds the import.

Managers only. A Member is refused on every route here (403). The import and refresh themselves are candidate
routes (routers/candidates.py); the rules live in services/salesforce_sync.py."""
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from ..deps import Principal, get_db, get_settings, managers
from ..schemas import SalesforceSearchOut, SalesforceStatus
from ..services import salesforce as sf
from ..services import salesforce_sync as sync
from ..services import settings as settings_service

router = APIRouter(prefix="/api/salesforce", tags=["salesforce"])
SEARCH_CACHE_SECONDS = 10
SEARCH_CACHE_ENTRIES = 200
MIN_QUERY = 2


async def status_out(request: Request, db: AsyncSession, settings: Settings) -> SalesforceStatus:
    """Also used by GET /api/integrations."""
    state = await request.app.state.salesforce.status()
    portal = await settings_service.load(db, settings)
    return SalesforceStatus(configured=bool(state["configured"]), connected=state["connected"], message=state["message"],
                            checked_at=state["checked_at"], api_version=sf.API_VERSION, team_lead_scope=portal.salesforce_team_lead_scope)


@router.get("/status", response_model=SalesforceStatus)
async def salesforce_status(request: Request, principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db),
                            settings: Settings = Depends(get_settings)):
    """Whether Salesforce is set up and answering (checked at most once a minute). Never fails because of Salesforce."""
    return await status_out(request, db, settings)


async def throttle(request: Request, settings: Settings, principal: Principal) -> None:
    """Typing in the search box must not hammer Salesforce: a budget per signed-in user and minute."""
    limiter, key = request.app.state.limiter, f"jf:sf-search:{principal.user.id}"
    if await limiter.count(key, 60) >= settings.salesforce_searches_per_minute:
        raise HTTPException(429, "Too many Salesforce searches. Wait a moment and try again.", headers={"Retry-After": "20"})
    await limiter.hit(key, 60)


@router.get("/candidates", response_model=SalesforceSearchOut)
async def search_salesforce_candidates(request: Request, principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db),
                                       settings: Settings = Depends(get_settings), q: str | None = Query(default=None, max_length=80),
                                       status: str = Query(default="active", pattern="^(active|all)$"),
                                       limit: int = Query(default=10, ge=1, le=25)):
    """Person Accounts whose name, e-mail or phone contains `q`. Fewer than two characters: nothing, and Salesforce
    is not asked. Under the setting `salesforce_team_lead_scope = own` a Team Lead only finds candidates whose
    marketing team lead in Salesforce has the Team Lead's own sign-in e-mail address."""
    text = " ".join((q or "").split())
    if len(text) < MIN_QUERY:
        return SalesforceSearchOut()
    client = request.app.state.salesforce
    if not client.configured:
        raise sync.as_http(sf.NotConfigured())
    only_of = await sync.team_lead_filter(db, settings, principal)
    await throttle(request, settings, principal)
    cache: dict = request.app.state.salesforce_cache
    key, now = (text.lower(), status, limit, only_of), time.monotonic()
    hit = cache.get(key)
    if hit and hit[0] > now:
        found, with_resume = hit[1], hit[2]
    else:
        try:
            found = await client.search(text, active_only=status == "active", limit=limit + 1, team_lead_email=only_of)
            found = [c for c in found if sync.visible(c, only_of)]
            with_resume = await client.with_resume([c.id for c in found])
        except sf.SalesforceError as problem:
            raise sync.as_http(problem) from None
        for old in [k for k, v in cache.items() if v[0] <= now]:
            cache.pop(old, None)
        if len(cache) >= SEARCH_CACHE_ENTRIES:
            cache.clear()
        cache[key] = (now + SEARCH_CACHE_SECONDS, found, with_resume)
    return SalesforceSearchOut(items=await sync.search_items(db, principal, found[:limit], with_resume), limit_reached=len(found) > limit)
