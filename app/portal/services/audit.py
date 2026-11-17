"""Audit trail and in-app notifications (written inside the caller's transaction)."""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditLog, Notification, User


def audit(db: AsyncSession, actor: User | None, action: str, target_type: str = "", target_id: uuid.UUID | str | None = None,
          team_id: uuid.UUID | None = None, meta: dict | None = None, ip: str = "") -> None:
    db.add(AuditLog(actor_id=actor.id if actor else None, action=action, target_type=target_type,
                    target_id=str(target_id or ""), team_id=team_id, meta=meta or {}, ip=ip[:64]))


def notify(db: AsyncSession, user_id: uuid.UUID, type_: str, payload: dict) -> None:
    db.add(Notification(user_id=user_id, type=type_, payload=payload))
