"""catalogue read history: three columns on index_boards for the order in which imported boards are read

- tried_at  (double precision, NULL): when a catalogue read of the board was last started. Until now the attempt
  overwrote checked_at, which the ATS health view reads as "the board answered at"; attempts are now kept apart.
- us_count  (integer, NULL): US postings at the last read that answered. NULL = never measured. job_count counts
  postings of every country, so "this board has nothing for the United States" was not recorded anywhere.
- misses    (integer, NOT NULL, default 0): reads in a row that brought no US posting (failed, empty, or only
  postings elsewhere). index_units keeps only the last read and is pruned after three days, so a growing retry
  interval needs its own counter.

Existing data: nothing is changed. Boards read before this migration have us_count NULL and misses 0 and are treated
as before (a board with postings is live, an empty one is re-checked after BOARD_CATALOG_RECHECK_DAYS).

The previous release can run against this schema: it names its columns in every INSERT and SELECT on index_boards and
never reads the new ones. The downgrade drops exactly these three columns.

Revision ID: f2a9c4d7e1b3
Revises: d3f6a1b8c925
Create Date: 2026-11-21 03:00:00
"""
from alembic import op

revision = 'f2a9c4d7e1b3'
down_revision = 'd3f6a1b8c925'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE index_boards ADD COLUMN IF NOT EXISTS tried_at double precision")
    op.execute("ALTER TABLE index_boards ADD COLUMN IF NOT EXISTS us_count integer")
    op.execute("ALTER TABLE index_boards ADD COLUMN IF NOT EXISTS misses integer NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE index_boards DROP COLUMN IF EXISTS misses")
    op.execute("ALTER TABLE index_boards DROP COLUMN IF EXISTS us_count")
    op.execute("ALTER TABLE index_boards DROP COLUMN IF EXISTS tried_at")
