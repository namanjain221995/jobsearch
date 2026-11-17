"""Request dependencies: database session, authenticated principal, CSRF, role and scope checks.

Authorization is enforced here and in the service layer on every request. The frontend only hides
navigation; it is never the security boundary."""
import ipaddress
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy import ColumnElement, false, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from . import security
from .models import Candidate, JobAssignment, Session, User
from .services.common import viewer

SESSION_COOKIE = "jf_session"
CSRF_COOKIE = "jf_csrf"
CSRF_HEADER = "x-csrf-token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass
class Principal:
    user: User
    session: Session

    @property
    def is_admin(self) -> bool:
        return self.user.role == "ADMIN"

    @property
    def is_team_lead(self) -> bool:
        return self.user.role == "TEAM_LEAD"

    @property
    def is_member(self) -> bool:
        return self.user.role == "MEMBER"

    @property
    def is_manager(self) -> bool:
        return self.user.role in ("ADMIN", "TEAM_LEAD")


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_db(request: Request):
    async with request.app.state.sessions() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def client_ip(request: Request) -> str:
    """Address of the caller, for the audit trail and (only when it can be believed) for throttling.

    The browser talks to the web app, and the web app passes an X-Forwarded-For header it received on to the
    API unchanged. So unless a reverse proxy in front of the web app writes that header itself, its content is
    whatever the caller typed, and the only address the API really knows is its socket peer (the web app).
    TRUST_FORWARDED_FOR=true states that such a proxy exists. Then the entry it added is read: counted from the
    right (`trusted_proxy_hops`), because everything to the left of it came from the caller."""
    settings: Settings = request.app.state.settings
    peer = request.client.host if request.client else "unknown"
    if settings.trust_forwarded_for:
        entries = [entry.strip() for entry in request.headers.get("x-forwarded-for", "").split(",") if entry.strip()]
        hops = max(1, settings.trusted_proxy_hops)
        if len(entries) >= hops:
            try:
                address = ipaddress.ip_address(entries[-hops].strip("[]").split("%", 1)[0])
            except ValueError:
                return peer
            return str(getattr(address, "ipv4_mapped", None) or address)
    return peer


def knows_client_address(request: Request) -> bool:
    """Whether client_ip() is the caller's own address. If not, every caller appears under the web app's address
    and limits per address would make them share one budget, so only limits per account are applied."""
    return bool(request.app.state.settings.trust_forwarded_for)


def _allowed_origins(settings: Settings) -> set[str]:
    origins = {settings.public_base_url.rstrip("/")}
    origins.update(o.strip().rstrip("/") for o in settings.allowed_origins.split(",") if o.strip())
    return origins


def check_origin(request: Request, settings: Settings) -> None:
    """Reject cross-site state-changing requests. Browsers send Origin on such requests; when present it
    must be this application's own origin (or the request's own host, for same-origin deployments)."""
    origin = request.headers.get("origin")
    if not origin:
        return
    host = request.headers.get("host", "")
    own = {f"http://{host}", f"https://{host}"} if host else set()
    forwarded = request.headers.get("x-forwarded-host")
    if forwarded and settings.trust_proxy_headers:           # set by the Next.js proxy: the host the browser really used
        own.update({f"http://{forwarded}", f"https://{forwarded}"})
    if origin.rstrip("/") not in _allowed_origins(settings) | own:
        raise HTTPException(403, "Cross-site request refused")


async def same_origin(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """For public state-changing endpoints (login, password reset, invitation acceptance)."""
    check_origin(request, settings)


async def authenticated(request: Request, db: AsyncSession = Depends(get_db),
                        settings: Settings = Depends(get_settings)) -> Principal:
    loaded = await security.load_session(db, request.cookies.get(SESSION_COOKIE, ""))
    if not loaded:
        raise HTTPException(401, "Not authenticated")
    session, user = loaded
    if request.method in UNSAFE_METHODS:
        check_origin(request, settings)
        header = request.headers.get(CSRF_HEADER, "")
        cookie = request.cookies.get(CSRF_COOKIE, "")
        if not header or not cookie or not security.tokens_equal(header, cookie) \
                or not security.tokens_equal(security.hash_token(header), session.csrf_hash):
            raise HTTPException(403, "CSRF token missing or invalid")
    if user.role == "TEAM_LEAD" and request.method in UNSAFE_METHODS and user.team is not None and not user.team.is_active \
            and not request.url.path.startswith(("/api/auth/", "/api/notifications")):
        raise HTTPException(409, "Your team is deactivated. An administrator has to reactivate it before you can make changes.")
    request.state.principal = Principal(user, session)
    request.state.actor = (str(user.id), user.role)      # plain values for the request log: usable after the session is closed
    viewer.set((user.role, user.id))                     # lets shared output builders hide what this role may not see
    return request.state.principal


def from_this_site(request: Request, settings: Settings) -> bool:
    """False when the browser reports that another site started this request (Sec-Fetch-Site, or a foreign Origin).
    Used by the two GET routes that record something: a link on someone else's page must not be able to make a
    member's browser "view" or "open" a job."""
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return False
    try:
        check_origin(request, settings)
    except HTTPException:
        return False
    return True


def require(*roles: str):
    async def checker(principal: Principal = Depends(authenticated)) -> Principal:
        if principal.user.role not in roles:
            raise HTTPException(403, "You do not have access to this resource")
        return principal
    return checker


admin_only = require("ADMIN")
managers = require("ADMIN", "TEAM_LEAD")
any_user = require("ADMIN", "TEAM_LEAD", "MEMBER")


# ---------------------------------------------------------------- scoping
def forbid() -> HTTPException:
    return HTTPException(403, "You do not have access to this resource")


def assignment_scope(principal: Principal) -> ColumnElement[bool]:
    """SQL filter restricting job assignments to what the caller may see."""
    user = principal.user
    if principal.is_admin:
        return true()
    if principal.is_team_lead:
        return JobAssignment.team_id == user.team_id if user.team_id else false()
    return JobAssignment.candidate_id.in_(own_candidates(user.id))       # the candidates this member is responsible for today


def own_candidates(member_id: uuid.UUID):
    """Subquery: ids of the candidates currently assigned to this member."""
    return select(Candidate.id).where(Candidate.assigned_member_id == member_id)


def candidate_scope(principal: Principal) -> ColumnElement[bool]:
    """SQL filter restricting candidate rows: Admin all, Team Lead own team, Member those assigned to them today."""
    user = principal.user
    if principal.is_admin:
        return true()
    if principal.is_team_lead:
        return Candidate.team_id == user.team_id if user.team_id else false()
    return Candidate.assigned_member_id == user.id


async def candidate_in_scope(db: AsyncSession, principal: Principal, candidate_id: uuid.UUID, *, write: bool = False) -> Candidate:
    """Load a candidate the caller may see. A candidate outside the caller's scope answers exactly like one that
    does not exist (404), for every role. `write=True` is for changes: only managers make them, so a member is
    refused (403) for a candidate they can see."""
    candidate = (await db.execute(select(Candidate).where(Candidate.id == candidate_id, candidate_scope(principal)))).scalar_one_or_none()
    if candidate is None:
        raise HTTPException(404, "Candidate not found")
    if write and not principal.is_manager:
        raise forbid()
    return candidate


def user_scope(principal: Principal) -> ColumnElement[bool]:
    """SQL filter restricting user rows: Admin all, Team Lead own team, Member self."""
    user = principal.user
    if principal.is_admin:
        return true()
    if principal.is_team_lead:
        return User.team_id == user.team_id if user.team_id else false()
    return User.id == user.id


def can_access_team(principal: Principal, team_id: uuid.UUID | None) -> bool:
    if principal.is_admin:
        return True
    return bool(team_id) and principal.is_team_lead and principal.user.team_id == team_id


def ensure_team(principal: Principal, team_id: uuid.UUID | None) -> None:
    if not can_access_team(principal, team_id):
        raise forbid()


async def member_in_scope(db: AsyncSession, principal: Principal, member_id: uuid.UUID, *, allow_self: bool = True) -> User:
    """Load a MEMBER the caller is allowed to act on, or raise 403/404. Never trusts client-supplied team ids."""
    target = (await db.execute(select(User).where(User.id == member_id))).scalar_one_or_none()
    if principal.is_member:
        if not allow_self or member_id != principal.user.id:
            raise forbid()
        return principal.user
    if target is None:
        raise HTTPException(404, "User not found") if principal.is_admin else forbid()
    if principal.is_team_lead and (target.role != "MEMBER" or target.team_id != principal.user.team_id or not principal.user.team_id):
        raise forbid()
    return target
