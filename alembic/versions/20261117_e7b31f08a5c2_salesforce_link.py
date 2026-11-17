"""salesforce link: where a candidate and a resume came from

Candidates can be brought in from Salesforce (read-only). The candidate keeps the Person Account id it was made
from, when it was last read, and the values last written from Salesforce (so a later refresh does not overwrite
what a manager changed in the portal). A resume says whether it was uploaded in the portal or copied from
Salesforce, and from which file version.

Existing rows: every candidate stays a manual one (salesforce_id NULL), every resume an upload.
The downgrade drops the four candidate/resume links; candidates and resumes themselves are kept.

Revision ID: e7b31f08a5c2
Revises: c4d2a9e71f36
Create Date: 2026-11-17 23:30:00
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'e7b31f08a5c2'
down_revision = 'c4d2a9e71f36'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('candidates', sa.Column('salesforce_id', sa.String(length=18), nullable=True))
    op.add_column('candidates', sa.Column('salesforce_synced_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('candidates', sa.Column('salesforce_snapshot', postgresql.JSONB(astext_type=sa.Text()),
                                          server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.create_index('uq_candidates_salesforce_id', 'candidates', ['salesforce_id'], unique=True,
                    postgresql_where=sa.text('salesforce_id IS NOT NULL'))
    op.add_column('resumes', sa.Column('source', sa.String(length=20), server_default='upload', nullable=False))
    op.add_column('resumes', sa.Column('salesforce_content_version_id', sa.String(length=18), nullable=True))
    op.create_check_constraint('ck_resumes_source', 'resumes', "source IN ('upload', 'salesforce')")


def downgrade() -> None:
    op.drop_constraint('ck_resumes_source', 'resumes', type_='check')
    op.drop_column('resumes', 'salesforce_content_version_id')
    op.drop_column('resumes', 'source')
    op.drop_index('uq_candidates_salesforce_id', table_name='candidates')
    op.drop_column('candidates', 'salesforce_snapshot')
    op.drop_column('candidates', 'salesforce_synced_at')
    op.drop_column('candidates', 'salesforce_id')
