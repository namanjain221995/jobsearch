"""aws web search usage: the monthly query counter

One row per calendar month (UTC, 'YYYY-MM') with the number of AWS Bedrock AgentCore Web Search queries reserved
so far. Every process reserves a query with one conditional UPDATE (used < ceiling) before the call, so the API
and the workers together can never pass the monthly limit. The table holds no query text and no result.

Existing data: nothing is changed. The downgrade drops only this table.

Revision ID: b5e8d2c47a19
Revises: e7b31f08a5c2
Create Date: 2026-11-18 10:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = 'b5e8d2c47a19'
down_revision = 'e7b31f08a5c2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'aws_web_search_usage',
        sa.Column('month', sa.String(length=7), primary_key=True),
        sa.Column('used', sa.Integer(), server_default='0', nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('used >= 0', name='ck_aws_web_search_usage_used'),
        sa.CheckConstraint("month ~ '^[0-9]{4}-[0-9]{2}$'", name='ck_aws_web_search_usage_month'),
    )


def downgrade() -> None:
    op.drop_table('aws_web_search_usage')
