"""Passwords (Argon2id), opaque tokens, server-side sessions, CSRF tokens and login throttling."""
import hashlib
import hmac
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from .models import Session, User

_hasher = PasswordHasher()          # Argon2id with the library's current recommended parameters
# Verified for unknown e-mails so a missing account costs the same time as a wrong password.
_DUMMY_HASH = _hasher.hash("timing-equalisation-only")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and bool(password_hash)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def password_problem(password: str, settings: Settings, email: str = "", name: str = "") -> str | None:
    """Return a user-facing reason when the password is not acceptable, else None."""
    if len(password) < settings.password_min_length:
        return f"Password must be at least {settings.password_min_length} characters long."
    if len(password) > 128:
        return "Password must be at most 128 characters long."
    classes = sum(bool(test(password)) for test in (
        lambda p: any(c.islower() for c in p), lambda p: any(c.isupper() for c in p),
        lambda p: any(c.isdigit() for c in p), lambda p: any(not c.isalnum() for c in p)))
    if classes < 3:
        return "Use at least three of: lower-case letters, upper-case letters, digits, symbols."
    lowered = password.lower()
    local = email.split("@")[0].lower() if email else ""
    if (local and len(local) >= 4 and local in lowered) or lowered in ("password123!", "changeme123!"):
        return "Password is too easy to guess."
    return None


def new_token() -> tuple[str, str]:
    """(token to hand out once, SHA-256 hex digest to store)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ---------------------------------------------------------------- sessions
async def create_session(db: AsyncSession, user: User, settings: Settings, remember: bool, ip: str, user_agent: str) -> tuple[str, str, Session]:
    """Create a server-side session. Returns (session token, csrf token, row). Only hashes are stored."""
    token, token_hash = new_token()
    csrf, csrf_hash = new_token()
    lifetime = timedelta(days=settings.remember_days) if remember else timedelta(hours=settings.session_hours)
    row = Session(user_id=user.id, token_hash=token_hash, csrf_hash=csrf_hash, remember=remember, ip=ip[:64],
                  user_agent=user_agent[:300], expires_at=utcnow() + lifetime)
    db.add(row)
    return token, csrf, row


async def load_session(db: AsyncSession, token: str) -> tuple[Session, User] | None:
    if not token or len(token) > 200:
        return None
    result = await db.execute(
        select(Session, User).join(User, User.id == Session.user_id)
        .where(Session.token_hash == hash_token(token), Session.revoked_at.is_(None), Session.expires_at > utcnow()))
    row = result.first()
    if not row or not row[1].is_active or not row[1].password_hash:
        return None
    return row[0], row[1]


async def revoke_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    await db.execute(update(Session).where(Session.id == session_id, Session.revoked_at.is_(None)).values(revoked_at=utcnow()))


async def revoke_user_sessions(db: AsyncSession, user_id: uuid.UUID, keep: uuid.UUID | None = None) -> None:
    stmt = update(Session).where(Session.user_id == user_id, Session.revoked_at.is_(None)).values(revoked_at=utcnow())
    if keep:
        stmt = stmt.where(Session.id != keep)
    await db.execute(stmt)


# ---------------------------------------------------------------- throttling
class RateLimiter:
    """Sliding-window counters. Uses Redis when available so limits hold across processes; falls back to
    process memory (single-process development and tests)."""

    def __init__(self, redis=None):
        self.redis = redis
        self._local: dict[str, deque] = defaultdict(deque)

    async def count(self, key: str, window_seconds: int) -> int:
        if self.redis is not None:
            try:
                now = time.time()
                await self.redis.zremrangebyscore(key, 0, now - window_seconds)
                return int(await self.redis.zcard(key))
            except Exception:
                pass
        q = self._local[key]
        cutoff = time.time() - window_seconds
        while q and q[0] < cutoff:
            q.popleft()
        return len(q)

    async def hit(self, key: str, window_seconds: int) -> None:
        if self.redis is not None:
            try:
                now = time.time()
                await self.redis.zadd(key, {f"{now}:{secrets.token_hex(4)}": now})
                await self.redis.expire(key, window_seconds)
                return
            except Exception:
                pass
        self._local[key].append(time.time())

    async def clear(self, key: str) -> None:
        if self.redis is not None:
            try:
                await self.redis.delete(key)
                return
            except Exception:
                pass
        self._local.pop(key, None)
