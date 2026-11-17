"""AWS web search for managers: POST /api/web-search/jobs (Admin and Team Lead; members 403).

Returns verified jobs only, nothing is saved: a manager adds a result to a candidate through the existing job and
assignment endpoints."""
import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from ..deps import Principal, candidate_in_scope, client_ip, get_db, get_settings, managers
from ..models import CandidateProfile, Resume
from ..schemas import WebSearchJobsIn, WebSearchJobsOut
from ..services import web_search as service
from ..services.audit import audit

router = APIRouter(prefix="/api", tags=["web-search"])


async def check_user_limits(request: Request, settings: Settings, user_id) -> None:
    """Per-user limits (Redis sliding windows when available, so they hold across API processes). The request is
    counted first and then compared, so concurrent requests cannot all slip under the limit."""
    limiter = request.app.state.limiter
    day = time.strftime("%Y-%m-%d", time.gmtime())
    for key, window, limit, text in (
            (f"aws-ws:min:{user_id}", 60, settings.aws_web_search_user_per_minute, "per minute"),
            (f"aws-ws:day:{user_id}:{day}", 86400, settings.aws_web_search_user_per_day, "per day")):
        await limiter.hit(key, window)
        if await limiter.count(key, window) > max(0, limit):
            raise HTTPException(429, f"Web search limit reached ({limit} {text}). Try again later.",
                                headers={"Retry-After": "60" if window == 60 else "3600"})


@router.post("/web-search/jobs", response_model=WebSearchJobsOut)
async def web_search_jobs(data: WebSearchJobsIn, request: Request, principal: Principal = Depends(managers),
                          db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    aws = getattr(request.app.state, "aws_search", None)
    if not settings.aws_web_search_enabled or aws is None or not aws.enabled:
        raise HTTPException(503, "AWS web search is switched off.")
    resume_text, fields = "", {}
    if data.candidate_id is not None:
        candidate = await candidate_in_scope(db, principal, data.candidate_id)       # other team's candidate: 404
        profile = await db.get(CandidateProfile, candidate.id)
        if profile is not None:
            fields = {"years_experience": float(profile.years_experience or 0), "work_authorization": profile.work_authorization,
                      "sponsorship_needed": profile.sponsorship_needed, "skills": list(profile.skills or []),
                      "exclude_citizenship_required": profile.exclude_citizenship_required}
        resume_text = (await db.execute(select(Resume.text).where(Resume.candidate_id == candidate.id, Resume.is_current))).scalar() or ""
    await check_user_limits(request, settings, principal.user.id)
    try:
        out = await asyncio.wait_for(
            service.run(settings, aws, request.app.state.http, request.app.state.verifier, data, resume_text, fields),
            timeout=settings.aws_web_search_endpoint_timeout_seconds)
    except asyncio.TimeoutError:
        raise HTTPException(504, "The web search took too long. Try fewer locations.")
    out.candidate_id = data.candidate_id
    audit(db, principal.user, "web_search.run", "candidate" if data.candidate_id else "", data.candidate_id, principal.user.team_id,
          {"queries": out.stats.queries_used, "returned": out.stats.returned, "locations": len(data.locations)}, client_ip(request))
    await db.commit()
    return out
