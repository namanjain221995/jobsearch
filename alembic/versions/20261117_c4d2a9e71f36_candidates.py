"""candidates: the person who needs a job becomes a record of its own; members are recruiters

Until now a MEMBER account was the job seeker and owned a profile, a resume, search targets, search results and
job assignments. From here on those belong to a candidate (a record, never an account) and a member is the
recruiter responsible for candidates.

Existing data survives: every user that owns job-seeker data becomes a candidate WITH THE SAME UUID, so every
stored foreign-key value stays valid when the columns are renamed and re-pointed from users to candidates.
"Job-seeker data" is a resume, a search target, a search result, a job assignment, or a profile in which at least
one field was filled in. The old API gave every invited member an empty profile row: such a row alone does not
make an employee a candidate.

Nothing is deleted. The two kinds of profile row that have no place in the new model (the untouched, all-default
row of a member who owns nothing else, and the row of a user who is not a member) are parked in legacy_records
and put back by the downgrade.

Revision ID: c4d2a9e71f36
Revises: a1c7e52b9d04
Create Date: 2026-11-17 21:00:00
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'c4d2a9e71f36'
down_revision = 'a1c7e52b9d04'
branch_labels = None
depends_on = None

PLACEHOLDER = 'candidate-%@candidate.invalid'       # accounts the downgrade creates for candidates that have none
# A profile row exactly as the old schema created it: every column still at its default, nothing filled in.
DEFAULT_PROFILE = ("p.desired_titles = '[]'::jsonb AND p.years_experience IS NULL AND p.skills = '[]'::jsonb "
                   "AND p.preferred_states = '[]'::jsonb AND p.preferred_cities = '[]'::jsonb AND p.work_mode = 'All' "
                   "AND p.work_authorization = 'Other / Prefer Not to Say' AND p.sponsorship_needed = 'Unknown' "
                   "AND NOT p.exclude_citizenship_required AND p.salary_min IS NULL AND p.seniority = '' "
                   "AND p.excluded_companies = '[]'::jsonb AND p.required_keywords = '[]'::jsonb AND p.extra = '{}'::jsonb")
PARKED_KINDS = "('member_profile_without_member', 'member_profile_all_default')"
OWNERS = ("SELECT user_id AS id FROM resumes UNION SELECT member_id FROM search_targets "
          "UNION SELECT member_id FROM search_results UNION SELECT member_id FROM job_assignments")


def upgrade() -> None:
    op.create_table(
        'candidates',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('team_id', sa.UUID(), nullable=True),
        sa.Column('name', sa.String(length=160), nullable=False),
        sa.Column('email', sa.String(length=320), server_default='', nullable=False),
        sa.Column('phone', sa.String(length=40), server_default='', nullable=False),
        sa.Column('notes', sa.Text(), server_default='', nullable=False),
        sa.Column('assigned_member_id', sa.UUID(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['assigned_member_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_candidates_team_id', 'candidates', ['team_id'], unique=False)
    op.create_index('ix_candidates_assigned_member_id', 'candidates', ['assigned_member_id'], unique=False)

    # Profile rows that belong to no candidate are parked, not lost. Two kinds, both of users who own nothing else:
    # - the row of a user who is not a member (an Admin opening a Team Lead's profile created an empty one);
    # - the all-default row every member got at invitation and never filled in: an employee, not a job seeker.
    # A member restored by a re-upgrade after a downgrade (legacy kind 'candidate') keeps the profile.
    stray = (f"p.user_id NOT IN ({OWNERS}) AND (u.role <> 'MEMBER' OR ({DEFAULT_PROFILE})) "
             "AND p.user_id::text NOT IN (SELECT legacy_id FROM legacy_records WHERE kind = 'candidate')")
    op.execute(f"""
        INSERT INTO legacy_records (kind, legacy_id, data)
        SELECT CASE WHEN u.role <> 'MEMBER' THEN 'member_profile_without_member' ELSE 'member_profile_all_default' END,
               p.user_id::text, to_jsonb(p)
        FROM member_profiles p JOIN users u ON u.id = p.user_id
        WHERE {stray}
        ON CONFLICT (kind, legacy_id) DO UPDATE SET data = EXCLUDED.data""")
    op.execute(f"DELETE FROM member_profiles p USING users u WHERE u.id = p.user_id AND {stray}")

    # One candidate per user that owns job-seeker data, reusing the user's UUID. A member is responsible for the
    # candidate made from their own account. The candidate is active exactly when the account was: a deactivated
    # job seeker could not be searched or given jobs before, and cannot now.
    op.execute(f"""
        INSERT INTO candidates (id, team_id, name, email, assigned_member_id, is_active, created_at, updated_at)
        SELECT u.id, u.team_id, u.name, lower(u.email), CASE WHEN u.role = 'MEMBER' THEN u.id END, u.is_active, u.created_at, now()
        FROM users u
        WHERE u.id IN (SELECT user_id FROM member_profiles UNION {OWNERS})
           OR u.id::text IN (SELECT legacy_id FROM legacy_records WHERE kind = 'candidate')""")

    # Only after a downgrade of this revision: what the old schema could not hold was copied to legacy_records.
    # It is put back, so downgrade followed by upgrade returns the same candidates (except the resume files).
    op.execute(f"""
        UPDATE candidates c SET
            email = coalesce(l.data->>'email', ''), phone = coalesce(l.data->>'phone', ''), notes = coalesce(l.data->>'notes', ''),
            created_by = (SELECT u.id FROM users u WHERE u.id = (l.data->>'created_by')::uuid),
            assigned_member_id = (SELECT u.id FROM users u WHERE u.id = (l.data->>'assigned_member_id')::uuid AND u.email NOT LIKE '{PLACEHOLDER}'),
            is_active = CASE WHEN p.id IS NULL THEN c.is_active ELSE coalesce((l.data->>'is_active')::boolean, true) END
        FROM legacy_records l LEFT JOIN users p ON p.id::text = l.legacy_id AND p.email LIKE '{PLACEHOLDER}' AND p.password_hash IS NULL
        WHERE l.kind = 'candidate' AND l.legacy_id = c.id::text""")
    op.execute("DELETE FROM legacy_records WHERE kind = 'candidate'")

    op.rename_table('member_profiles', 'candidate_profiles')
    op.alter_column('candidate_profiles', 'user_id', new_column_name='candidate_id')
    op.execute('ALTER TABLE candidate_profiles RENAME CONSTRAINT member_profiles_pkey TO candidate_profiles_pkey')
    op.drop_constraint('member_profiles_user_id_fkey', 'candidate_profiles', type_='foreignkey')
    op.create_foreign_key('candidate_profiles_candidate_id_fkey', 'candidate_profiles', 'candidates', ['candidate_id'], ['id'], ondelete='CASCADE')

    op.alter_column('resumes', 'user_id', new_column_name='candidate_id')
    op.drop_constraint('resumes_user_id_fkey', 'resumes', type_='foreignkey')
    op.create_foreign_key('resumes_candidate_id_fkey', 'resumes', 'candidates', ['candidate_id'], ['id'], ondelete='CASCADE')
    op.execute('ALTER INDEX ix_resumes_user_id RENAME TO ix_resumes_candidate_id')
    # The uploaded file, kept from now on. NULL for every resume uploaded earlier: those files were never stored.
    op.add_column('resumes', sa.Column('file_data', sa.LargeBinary(), nullable=True))

    op.alter_column('search_targets', 'member_id', new_column_name='candidate_id')
    op.alter_column('search_targets', 'member_specific_filters', new_column_name='candidate_specific_filters')
    op.drop_constraint('search_targets_member_id_fkey', 'search_targets', type_='foreignkey')
    op.create_foreign_key('search_targets_candidate_id_fkey', 'search_targets', 'candidates', ['candidate_id'], ['id'], ondelete='CASCADE')
    op.execute('ALTER INDEX ix_search_targets_member_id RENAME TO ix_search_targets_candidate_id')
    # Which resume version a search ran with is recorded from now on; for earlier searches it is not known.
    op.add_column('search_targets', sa.Column('resume_id', sa.UUID(), nullable=True))
    op.create_foreign_key('search_targets_resume_id_fkey', 'search_targets', 'resumes', ['resume_id'], ['id'], ondelete='SET NULL')

    op.alter_column('search_results', 'member_id', new_column_name='candidate_id')
    op.drop_constraint('search_results_member_id_fkey', 'search_results', type_='foreignkey')
    op.create_foreign_key('search_results_candidate_id_fkey', 'search_results', 'candidates', ['candidate_id'], ['id'], ondelete='CASCADE')
    op.execute('ALTER TABLE search_results RENAME CONSTRAINT uq_search_results_run_member_job TO uq_search_results_run_candidate_job')

    op.alter_column('job_assignments', 'member_id', new_column_name='candidate_id')
    op.drop_constraint('job_assignments_member_id_fkey', 'job_assignments', type_='foreignkey')
    op.create_foreign_key('job_assignments_candidate_id_fkey', 'job_assignments', 'candidates', ['candidate_id'], ['id'], ondelete='RESTRICT')
    op.execute('ALTER TABLE job_assignments RENAME CONSTRAINT uq_job_assignments_job_member TO uq_job_assignments_job_candidate')
    op.execute('ALTER INDEX ix_job_assignments_member_id RENAME TO ix_job_assignments_candidate_id')

    # Placeholder accounts a downgrade made for candidates without an account are not needed any more.
    op.execute(f"DELETE FROM users WHERE email LIKE '{PLACEHOLDER}' AND password_hash IS NULL AND id IN (SELECT id FROM candidates)")


def downgrade() -> None:
    """Back to "the member is the job seeker". Every profile, resume, search and assignment row is kept.

    A candidate created after the upgrade has no account, and the old schema can only hang its rows on a user:
    such a candidate gets a placeholder MEMBER account with the candidate's UUID that nobody can sign in to (no
    password, deactivated, an address under the reserved domain `candidate.invalid`). What the old schema cannot
    hold at all (phone, notes, who was responsible, the candidate's own e-mail) is copied to legacy_records.
    The stored resume files and the resume version of each search are dropped with their columns."""
    op.execute("""
        INSERT INTO legacy_records (kind, legacy_id, data)
        SELECT 'candidate', c.id::text, to_jsonb(c) FROM candidates c
        ON CONFLICT (kind, legacy_id) DO UPDATE SET data = EXCLUDED.data""")
    op.execute("""
        INSERT INTO users (id, name, email, password_hash, role, team_id, is_active, created_by, created_at, updated_at)
        SELECT c.id, c.name, 'candidate-' || c.id::text || '@candidate.invalid', NULL, 'MEMBER', c.team_id, false, c.created_by, c.created_at, now()
        FROM candidates c WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = c.id)""")

    op.execute('ALTER INDEX ix_job_assignments_candidate_id RENAME TO ix_job_assignments_member_id')
    op.execute('ALTER TABLE job_assignments RENAME CONSTRAINT uq_job_assignments_job_candidate TO uq_job_assignments_job_member')
    op.drop_constraint('job_assignments_candidate_id_fkey', 'job_assignments', type_='foreignkey')
    op.alter_column('job_assignments', 'candidate_id', new_column_name='member_id')
    op.create_foreign_key('job_assignments_member_id_fkey', 'job_assignments', 'users', ['member_id'], ['id'], ondelete='RESTRICT')

    op.execute('ALTER TABLE search_results RENAME CONSTRAINT uq_search_results_run_candidate_job TO uq_search_results_run_member_job')
    op.drop_constraint('search_results_candidate_id_fkey', 'search_results', type_='foreignkey')
    op.alter_column('search_results', 'candidate_id', new_column_name='member_id')
    op.create_foreign_key('search_results_member_id_fkey', 'search_results', 'users', ['member_id'], ['id'], ondelete='CASCADE')

    op.drop_constraint('search_targets_resume_id_fkey', 'search_targets', type_='foreignkey')
    op.drop_column('search_targets', 'resume_id')
    op.execute('ALTER INDEX ix_search_targets_candidate_id RENAME TO ix_search_targets_member_id')
    op.drop_constraint('search_targets_candidate_id_fkey', 'search_targets', type_='foreignkey')
    op.alter_column('search_targets', 'candidate_specific_filters', new_column_name='member_specific_filters')
    op.alter_column('search_targets', 'candidate_id', new_column_name='member_id')
    op.create_foreign_key('search_targets_member_id_fkey', 'search_targets', 'users', ['member_id'], ['id'], ondelete='CASCADE')

    op.drop_column('resumes', 'file_data')
    op.execute('ALTER INDEX ix_resumes_candidate_id RENAME TO ix_resumes_user_id')
    op.drop_constraint('resumes_candidate_id_fkey', 'resumes', type_='foreignkey')
    op.alter_column('resumes', 'candidate_id', new_column_name='user_id')
    op.create_foreign_key('resumes_user_id_fkey', 'resumes', 'users', ['user_id'], ['id'], ondelete='CASCADE')

    op.drop_constraint('candidate_profiles_candidate_id_fkey', 'candidate_profiles', type_='foreignkey')
    op.execute('ALTER TABLE candidate_profiles RENAME CONSTRAINT candidate_profiles_pkey TO member_profiles_pkey')
    op.alter_column('candidate_profiles', 'candidate_id', new_column_name='user_id')
    op.rename_table('candidate_profiles', 'member_profiles')
    op.create_foreign_key('member_profiles_user_id_fkey', 'member_profiles', 'users', ['user_id'], ['id'], ondelete='CASCADE')

    # Profiles the upgrade parked (not a member, or a member's untouched empty row) go back where they were.
    op.execute(f"""
        INSERT INTO member_profiles
        SELECT (jsonb_populate_record(NULL::member_profiles, l.data)).*
        FROM legacy_records l JOIN users u ON u.id::text = l.legacy_id
        WHERE l.kind IN {PARKED_KINDS}
        ON CONFLICT (user_id) DO NOTHING""")
    op.execute(f"DELETE FROM legacy_records WHERE kind IN {PARKED_KINDS}")

    op.drop_index('ix_candidates_assigned_member_id', table_name='candidates')
    op.drop_index('ix_candidates_team_id', table_name='candidates')
    op.drop_table('candidates')
