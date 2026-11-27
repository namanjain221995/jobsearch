"""admin invitations: an invitation may carry the role ADMIN

The check constraint ck_invitations_role on `invitations` allowed TEAM_LEAD and MEMBER only. It is replaced by one
that also allows ADMIN. No column, no index and no row is changed.

On a live table: `invitations` holds one row per invitation ever sent (tens to hundreds of rows). The old constraint
is dropped and the new one added NOT VALID (a catalogue change, no table scan under the exclusive lock), then
validated in a second statement, which scans the table while only blocking other schema changes, not reads or writes.
Every existing row satisfies the wider rule, so the validation cannot fail.

The previous release can run against this schema: it only ever writes TEAM_LEAD or MEMBER.

Downgrade: the narrower rule cannot hold rows with the role ADMIN, so those invitation rows are deleted first (the
history of who invited which Admin is then only in the audit log), and Admin accounts that were invited but never
activated are deactivated, because the previous release has no way to send them a new link. Admin accounts that were
activated stay as they are.

Revision ID: c9e4b7a2d615
Revises: a8c5e3f1d2b7
Create Date: 2026-11-27 09:00:00
"""
from alembic import op

revision = 'c9e4b7a2d615'
down_revision = 'a8c5e3f1d2b7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE invitations DROP CONSTRAINT IF EXISTS ck_invitations_role")
    op.execute("ALTER TABLE invitations ADD CONSTRAINT ck_invitations_role "
               "CHECK (invited_role IN ('ADMIN', 'TEAM_LEAD', 'MEMBER')) NOT VALID")
    op.execute("ALTER TABLE invitations VALIDATE CONSTRAINT ck_invitations_role")


def downgrade() -> None:
    op.execute("UPDATE users SET is_active = false WHERE role = 'ADMIN' AND password_hash IS NULL")
    op.execute("DELETE FROM invitations WHERE invited_role = 'ADMIN'")
    op.execute("ALTER TABLE invitations DROP CONSTRAINT IF EXISTS ck_invitations_role")
    op.execute("ALTER TABLE invitations ADD CONSTRAINT ck_invitations_role "
               "CHECK (invited_role IN ('TEAM_LEAD', 'MEMBER')) NOT VALID")
    op.execute("ALTER TABLE invitations VALIDATE CONSTRAINT ck_invitations_role")
