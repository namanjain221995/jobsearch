"""auto apply: candidate consents, recorded application answers, auto-apply runs and their screenshots

- candidate_consents: the candidate's permission for the portal to apply on their behalf (method, document
  reference, who recorded it, revocation). Auto-apply needs an active row.
- candidate_profiles.application_answers (JSONB, default '{}') and application_answers_updated_by: the answers the
  candidate gave for legal / EEO / link questions. Every value is "unknown" until recorded.
- auto_apply_runs: one per candidate + assignment attempt (form snapshot, proposed answers with sources, blockers,
  outcome). Two partial unique indexes: one open run per assignment; one submission per candidate and posting ever.
- auto_apply_screenshots: size-capped JPEGs, deleted after the retention period by the worker.

Existing data: nothing is changed; existing profiles get an empty answers object. The downgrade drops exactly what
the upgrade added.

Revision ID: d3f6a1b8c925
Revises: b5e8d2c47a19
Create Date: 2026-11-18 14:00:00
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'd3f6a1b8c925'
down_revision = 'b5e8d2c47a19'
branch_labels = None
depends_on = None

OPEN = "status IN ('queued', 'preparing', 'needs_input', 'ready_for_review', 'approved', 'submitting')"
STATUSES = ("queued", "preparing", "needs_input", "ready_for_review", "approved", "submitting", "submitted", "failed", "blocked",
            "cancelled")


def _in(column: str, values) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _uuid(name: str, **kw):
    return sa.Column(name, postgresql.UUID(as_uuid=True), **kw)


def _ts(name: str, nullable: bool = True, default: bool = False):
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text('now()') if default else None, nullable=nullable)


def _jsonb(name: str, empty: str):
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text(f"'{empty}'::jsonb"), nullable=False)


def upgrade() -> None:
    op.add_column('candidate_profiles', _jsonb('application_answers', '{}'))
    op.add_column('candidate_profiles', _uuid('application_answers_updated_by', nullable=True))
    op.create_foreign_key('fk_candidate_profiles_answers_updated_by', 'candidate_profiles', 'users',
                          ['application_answers_updated_by'], ['id'], ondelete='SET NULL')

    op.create_table(
        'candidate_consents',
        _uuid('id', primary_key=True),
        _uuid('candidate_id', nullable=False),
        sa.Column('scope', sa.String(30), server_default='auto_apply', nullable=False),
        sa.Column('granted', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('method', sa.String(20), nullable=False),
        sa.Column('note', sa.Text(), server_default='', nullable=False),
        sa.Column('document_reference', sa.String(300), server_default='', nullable=False),
        sa.Column('may_attest', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('may_accept_data_processing', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        _uuid('recorded_by', nullable=True),
        _ts('recorded_at', nullable=False, default=True),
        _ts('revoked_at'),
        _uuid('revoked_by', nullable=True),
        sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['recorded_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['revoked_by'], ['users.id'], ondelete='SET NULL'),
        sa.CheckConstraint("scope IN ('auto_apply')", name='ck_candidate_consents_scope'),
        sa.CheckConstraint(_in('method', ('written', 'email', 'e_signature', 'verbal_recorded')), name='ck_candidate_consents_method'),
    )
    op.create_index('ix_candidate_consents_candidate', 'candidate_consents', ['candidate_id', 'recorded_at'])

    op.create_table(
        'auto_apply_runs',
        _uuid('id', primary_key=True),
        _uuid('candidate_id', nullable=False),
        _uuid('assignment_id', nullable=False),
        _uuid('job_id', nullable=False),
        _uuid('consent_id', nullable=True),
        sa.Column('status', sa.String(20), server_default='queued', nullable=False),
        sa.Column('ats', sa.String(20), server_default='unsupported', nullable=False),
        sa.Column('job_url', sa.Text(), nullable=False),
        sa.Column('employer_key', sa.String(200), server_default='', nullable=False),
        _jsonb('form_schema', '[]'),
        sa.Column('schema_hash', sa.String(64), server_default='', nullable=False),
        _jsonb('answers', '[]'),
        sa.Column('answers_hash', sa.String(64), server_default='', nullable=False),
        _jsonb('admin_edits', '{}'),
        _jsonb('blocked_reasons', '[]'),
        _jsonb('needs_input', '[]'),
        _jsonb('fill_report', '[]'),
        _jsonb('guard_report', '{}'),
        _jsonb('confirmation', '{}'),
        _jsonb('ai_usage', '{}'),
        sa.Column('outcome_unknown', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('error', sa.String(500), server_default='', nullable=False),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
        _uuid('created_by', nullable=True),
        _uuid('approved_by', nullable=True),
        _ts('approved_at'), _ts('started_at'), _ts('submitted_at'), _ts('finished_at'),
        _ts('created_at', nullable=False, default=True), _ts('updated_at', nullable=False, default=True),
        sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['assignment_id'], ['job_assignments.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['job_id'], ['job_postings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['consent_id'], ['candidate_consents.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['approved_by'], ['users.id'], ondelete='SET NULL'),
        sa.CheckConstraint(_in('status', STATUSES), name='ck_auto_apply_runs_status'),
        sa.CheckConstraint(_in('ats', ('greenhouse', 'lever', 'ashby', 'workable', 'unsupported')), name='ck_auto_apply_runs_ats'),
    )
    op.create_index('uq_auto_apply_runs_open', 'auto_apply_runs', ['assignment_id'], unique=True, postgresql_where=sa.text(OPEN))
    op.create_index('uq_auto_apply_runs_applied', 'auto_apply_runs', ['candidate_id', 'job_id'], unique=True,
                    postgresql_where=sa.text("status IN ('submitting', 'submitted') OR outcome_unknown"))
    op.create_index('ix_auto_apply_runs_candidate', 'auto_apply_runs', ['candidate_id', 'created_at'])
    op.create_index('ix_auto_apply_runs_status', 'auto_apply_runs', ['status'])
    op.create_index('ix_auto_apply_runs_employer_submitted', 'auto_apply_runs', ['employer_key', 'submitted_at'])

    op.create_table(
        'auto_apply_screenshots',
        _uuid('id', primary_key=True),
        _uuid('run_id', nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('content_type', sa.String(40), server_default='image/jpeg', nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        _ts('created_at', nullable=False, default=True),
        sa.ForeignKeyConstraint(['run_id'], ['auto_apply_runs.id'], ondelete='CASCADE'),
        sa.CheckConstraint("kind IN ('dry_run', 'before_submit', 'confirmation')", name='ck_auto_apply_screenshots_kind'),
        sa.CheckConstraint('size_bytes <= 2000000', name='ck_auto_apply_screenshots_size'),
    )
    op.create_index('ix_auto_apply_screenshots_run', 'auto_apply_screenshots', ['run_id'])
    op.create_index('ix_auto_apply_screenshots_created', 'auto_apply_screenshots', ['created_at'])


def downgrade() -> None:
    op.drop_table('auto_apply_screenshots')
    op.drop_table('auto_apply_runs')
    op.drop_table('candidate_consents')
    op.drop_constraint('fk_candidate_profiles_answers_updated_by', 'candidate_profiles', type_='foreignkey')
    op.drop_column('candidate_profiles', 'application_answers_updated_by')
    op.drop_column('candidate_profiles', 'application_answers')
