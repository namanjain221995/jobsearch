"""Sign-in, sign-out, current user, password reset."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from .. import security
from ..deps import (CSRF_COOKIE, SESSION_COOKIE, Principal, authenticated, client_ip, get_db, get_settings, knows_client_address,
                    same_origin)
from ..models import Notification, User
from ..schemas import ChangePasswordIn, ForgotPasswordIn, LoginIn, MeOut, Message, PasswordPolicy, ResetPasswordIn
from ..security import utcnow
from ..services import invitations as invitation_service
from ..services import settings as settings_service
from ..services.audit import audit
from ..services.common import team_ref

router = APIRouter(prefix="/api/auth", tags=["auth"])
HOME = {"ADMIN": "/admin/dashboard", "TEAM_LEAD": "/tl/dashboard", "MEMBER": "/member/dashboard"}
# The answer never depends on whether the address has an account, and it never claims a mail that cannot go out.
RESET_ANSWERS = {
    "smtp": "If an account exists for this address, a password reset link is being e-mailed to it.",
    "console": "E-mail is in development mode: if an account exists for this address, its reset link was written to the server log.",
    "none": "This server cannot send e-mail, so no reset link was sent. Ask your Team Lead or an administrator for a reset link.",
}


async def me_out(db: AsyncSession, settings: Settings, user: User) -> MeOut:
    unread = (await db.execute(select(func.count()).select_from(Notification).where(
        Notification.user_id == user.id, Notification.read_at.is_(None)))).scalar_one()
    portal = await settings_service.load(db, settings)
    return MeOut(id=user.id, name=user.name, email=user.email, role=user.role, team=team_ref(user.team), home=HOME[user.role],
                 unread_notifications=unread, settings=settings_service.ui(portal, settings))


def set_cookies(response: Response, settings: Settings, token: str, csrf: str, remember: bool) -> None:
    max_age = settings.remember_days * 86400 if remember else None       # no max-age = ends with the browser session
    common = dict(secure=settings.secure_cookies, samesite="lax", path="/", max_age=max_age)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **common)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **common)     # readable by the page so it can echo it in a header


def clear_cookies(response: Response, settings: Settings) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/", secure=settings.secure_cookies, samesite="lax")


@router.post("/login", response_model=MeOut, dependencies=[Depends(same_origin)])
async def login(data: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db),
                settings: Settings = Depends(get_settings)):
    limiter = request.app.state.limiter
    email, ip = data.email.strip().lower(), client_ip(request)
    window = settings.login_window_minutes * 60
    # Counters of failed attempts: this account from any address (always), and, when the caller's address is
    # known, this account from this address and this address for any account.
    key_pair, key_ip, key_account = f"jf:login:{email}|{ip}", f"jf:login-ip:{ip}", f"jf:login-account:{email}"
    by_address = knows_client_address(request)
    # Address unknown (no trusted proxy in front): the account itself is locked after the usual number of failures.
    account_limit = settings.login_account_max_failures if by_address else settings.login_max_failures
    if (by_address and (await limiter.count(key_pair, window) >= settings.login_max_failures
                        or await limiter.count(key_ip, window) >= settings.login_max_failures * 6)) \
            or await limiter.count(key_account, window) >= account_limit:
        audit(db, None, "auth.login_throttled", "user", None, None, {"email": email}, ip)
        await db.commit()
        raise HTTPException(429, "Too many failed sign-in attempts. Wait a few minutes and try again.")
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    valid = await security.verify_password_async(user.password_hash if user else None, data.password)
    if not valid or user is None or not user.is_active:
        if by_address:
            await limiter.hit(key_pair, window)
            await limiter.hit(key_ip, window)
        await limiter.hit(key_account, window)
        audit(db, user, "auth.login_failed", "user", user.id if user else None, user.team_id if user else None, {"email": email}, ip)
        await db.commit()
        raise HTTPException(401, "Invalid e-mail or password.")       # same answer whether or not the account exists
    await limiter.clear(key_pair)
    await limiter.clear(key_account)
    if security.needs_rehash(user.password_hash):
        user.password_hash = await security.hash_password_async(data.password)
    token, csrf, _ = await security.create_session(db, user, settings, data.remember, ip, request.headers.get("user-agent", ""))
    user.last_login_at = utcnow()
    audit(db, user, "auth.login", "user", user.id, user.team_id, {"remember": data.remember}, ip)
    out = await me_out(db, settings, user)
    await db.commit()
    set_cookies(response, settings, token, csrf, data.remember)
    return out


@router.post("/logout", response_model=Message)
async def logout(request: Request, response: Response, principal: Principal = Depends(authenticated),
                 db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    await security.revoke_session(db, principal.session.id)
    audit(db, principal.user, "auth.logout", "user", principal.user.id, principal.user.team_id, {}, client_ip(request))
    await db.commit()
    clear_cookies(response, settings)
    return Message(message="Signed out")


@router.get("/me", response_model=MeOut)
async def me(principal: Principal = Depends(authenticated), db: AsyncSession = Depends(get_db),
             settings: Settings = Depends(get_settings)):
    return await me_out(db, settings, principal.user)


@router.get("/password-policy", response_model=PasswordPolicy)
async def password_policy(settings: Settings = Depends(get_settings)):
    """The rules a new password has to meet, for the screens that ask for one before anyone is signed in."""
    return PasswordPolicy(min_length=settings.password_min_length)


@router.post("/forgot-password", response_model=Message, dependencies=[Depends(same_origin)])
async def forgot_password(data: ForgotPasswordIn, request: Request, db: AsyncSession = Depends(get_db),
                          settings: Settings = Depends(get_settings)):
    limiter = request.app.state.limiter
    email, ip = data.email.strip().lower(), client_ip(request)
    emailer = request.app.state.emailer
    answer = Message(message=RESET_ANSWERS[emailer.backend])
    if emailer.backend == "none":
        return answer                                                 # nobody could receive a link: none is created
    by_address = knows_client_address(request)
    if (by_address and await limiter.count(f"jf:forgot-ip:{ip}", 3600) >= 10) or await limiter.count(f"jf:forgot:{email}", 3600) >= 3:
        return answer                                                 # throttled silently: no signal about the account
    if by_address:
        await limiter.hit(f"jf:forgot-ip:{ip}", 3600)
    await limiter.hit(f"jf:forgot:{email}", 3600)
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is not None and user.is_active and user.password_hash:
        _, _, (subject, body) = await invitation_service.prepare_reset(db, settings, user, None)
        audit(db, user, "auth.password_reset_requested", "user", user.id, user.team_id, {}, ip)
        await db.commit()
        # Delivered after the answer: waiting for the mail server here would tell an existing address from an unknown one.
        request.app.state.spawn(emailer.send(user.email, subject, body))
    return answer


@router.post("/reset-password", response_model=Message, dependencies=[Depends(same_origin)])
async def reset_password(data: ResetPasswordIn, request: Request, db: AsyncSession = Depends(get_db),
                         settings: Settings = Depends(get_settings)):
    await invitation_service.reset_password(db, settings, data.token, data.password, client_ip(request))
    await db.commit()
    return Message(message="Password updated. Sign in with your new password.")


@router.post("/change-password", response_model=Message)
async def change_password(data: ChangePasswordIn, request: Request, principal: Principal = Depends(authenticated),
                          db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    user = principal.user
    if not await security.verify_password_async(user.password_hash, data.current_password):
        raise HTTPException(403, "Current password is incorrect.")
    problem = security.password_problem(data.new_password, settings, user.email, user.name)
    if problem:
        raise HTTPException(422, problem)
    user.password_hash = await security.hash_password_async(data.new_password)
    await invitation_service.retire_reset_links(db, user.id)                         # links issued earlier die with the old password
    await security.revoke_user_sessions(db, user.id, keep=principal.session.id)      # other devices are signed out
    audit(db, user, "auth.password_changed", "user", user.id, user.team_id, {}, client_ip(request))
    await db.commit()
    return Message(message="Password changed.")
