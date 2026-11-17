"""Request dependencies: database session, authenticated principal, CSRF, role and scope checks.

Authorization is enforced here and in the service layer on every request. The frontend only hides
navigation; it is never the security boundary."""
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy import ColumnElement, false, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from . import security
from .models import JobAssignment, Session, User

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
    """Caller address. Behind the web proxy the real client is the first X-Forwarded-For entry."""
    settings: Settings = request.app.state.settings
    forwarded = request.headers.get("x-forwarded-for", "")
    if settings.trust_proxy_headers and forwarded:
        return forwarded.split(",")[0].strip()[:64] or "unknown"
    return request.client.host if request.client else "unknown"


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
    request.state.principal = Principal(user, session)
    request.state.actor = (str(user.id), user.role)      # plain values for the request log: usable after the session is closed
    return request.state.principal


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
    return JobAssignment.member_id == user.id


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
