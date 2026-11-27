"""Job-title suggestions for the Find Jobs form (Team Leads and Admins). See services/job_titles.py."""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from ..deps import Principal, get_db, get_settings, managers
from ..schemas import CandidateRef, CandidateTitle, CandidateTitles, ResumeTitlesIn, ResumeTitlesOut, TitleSuggestion, TitleSuggestionsOut
from ..services import job_titles as service

router = APIRouter(prefix="/api/job-titles", tags=["job titles"])


@router.get("/suggestions", response_model=TitleSuggestionsOut)
async def suggest_titles(request: Request, principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db),
                         q: str = Query(default="", max_length=80), candidate_ids: str | None = Query(default=None, max_length=2000)):
    """Titles matching what was typed so far: the selected candidates' own titles first, then titles that postings
    in the job index carry, the most frequent first. At most 8. Limited per person per minute."""
    await service.throttle(request, f"jf:title-suggest:{principal.user.id}", service.SUGGESTIONS_PER_MINUTE)
    candidates = await service.candidates_in_scope(db, principal, service.parse_ids(candidate_ids))     # 404 outside the caller's scope
    typed = " ".join(q.split())
    cache = service.vocabulary_of(request.app)
    if len(typed) < 2:
        return TitleSuggestionsOut(items=[], vocabulary_ready=cache.loaded)
    known = await service.known_titles(db, candidates)
    own = [title for entry in known.values() for title in (*entry["saved"], *entry["resume"])]
    vocabulary = await cache.current()
    return TitleSuggestionsOut(items=[TitleSuggestion(title=title, source=source)
                                      for title, source in service.typed_suggestions(vocabulary, typed, own)],
                               vocabulary_ready=cache.loaded)


@router.post("/from-resumes", response_model=ResumeTitlesOut)
async def titles_from_resumes(data: ResumeTitlesIn, request: Request, principal: Principal = Depends(managers),
                              db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Titles to offer for the selected candidates: saved in the profile, found in the resume, and (when a model
    answers in time) related titles. Never resume text. A candidate outside the caller's scope refuses the request."""
    await service.throttle(request, f"jf:title-resume:{principal.user.id}", service.RESUME_REQUESTS_PER_MINUTE)
    candidates = await service.candidates_in_scope(db, principal, list(dict.fromkeys(data.candidate_ids)))
    known = await service.known_titles(db, candidates)
    related, model = await service.related_titles(request.app, db, settings, known)
    out = []
    for candidate in candidates:
        entry = known[candidate.id]
        titles = [CandidateTitle(title=t, source="saved") for t in entry["saved"]]
        titles += [CandidateTitle(title=t, source="resume") for t in entry["resume"]]
        seen = {service.title_key(t.title) for t in titles}
        for title in related.get(candidate.id, []):
            if service.title_key(title) not in seen:
                seen.add(service.title_key(title))
                titles.append(CandidateTitle(title=title, source="model"))
        out.append(CandidateTitles(candidate=CandidateRef(id=candidate.id, name=candidate.name), has_resume=entry["has_resume"],
                                   titles=titles[:service.PER_CANDIDATE * 2]))
    return ResumeTitlesOut(candidates=out, model=model)
