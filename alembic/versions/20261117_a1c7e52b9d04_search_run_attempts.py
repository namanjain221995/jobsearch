"""search runs: attempt counter (one automatic resume after a worker stop)

Revision ID: a1c7e52b9d04
Revises: 60a3fc9f26ba
Create Date: 2026-11-17 12:55:00
"""
import sqlalchemy as sa
from alembic import op

revision = 'a1c7e52b9d04'
down_revision = '60a3fc9f26ba'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A constant default makes this a metadata-only change in PostgreSQL: no table rewrite, existing rows read 0.
    op.add_column('search_runs', sa.Column('attempts', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('search_runs', 'attempts')
