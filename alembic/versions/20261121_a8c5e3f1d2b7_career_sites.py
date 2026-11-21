"""career sites: employers' own career sites the crawler reads, and the history of its crawls

Two new tables in the job index, used only by app/career_sites.py:

- index_career_sites  one row per site (key: host name without "www."). Who the employer is, where the site was found
  (a posting a search verified, or a file), when it was crawled last and is due next (`next_at`), how the last crawl
  ended (`state`, `method`, counts, `error`), how many crawls in a row stored no US posting (`misses`, the growing
  wait), and the hashes of the pages already read (`seen`), so a later crawl reads new pages. No personal data.
- index_career_crawls one row per crawl: start and end, pages read, postings verified, US postings stored, requests,
  why it stopped. Rows older than 180 days are removed by the crawler.

Existing data: nothing is changed, no existing table is touched. The previous release can run against this schema: it
never names these tables. The downgrade drops exactly these two tables (the postings the crawler stored are ordinary
`career_page` units and stay).

Revision ID: a8c5e3f1d2b7
Revises: f2a9c4d7e1b3
Create Date: 2026-11-21 18:00:00
"""
from alembic import op

revision = 'a8c5e3f1d2b7'
down_revision = 'f2a9c4d7e1b3'
branch_labels = None
depends_on = None

DDL = """
CREATE TABLE IF NOT EXISTS index_career_sites (
    host text PRIMARY KEY, base_url text NOT NULL, employer text NOT NULL DEFAULT '', origin text NOT NULL DEFAULT 'search',
    found_url text NOT NULL DEFAULT '', listing_url text NOT NULL DEFAULT '', created_at double precision NOT NULL,
    state text NOT NULL DEFAULT 'new', method text NOT NULL DEFAULT '', tried_at double precision, crawled_at double precision,
    next_at double precision NOT NULL DEFAULT 0, misses integer NOT NULL DEFAULT 0, candidates integer NOT NULL DEFAULT 0,
    pages_seen integer NOT NULL DEFAULT 0, postings integer NOT NULL DEFAULT 0, us_postings integer NOT NULL DEFAULT 0,
    error text NOT NULL DEFAULT '', seen bigint[] NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ix_index_career_sites_next ON index_career_sites (next_at);
CREATE TABLE IF NOT EXISTS index_career_crawls (
    id bigserial PRIMARY KEY, host text NOT NULL REFERENCES index_career_sites (host) ON DELETE CASCADE,
    started_at double precision NOT NULL, finished_at double precision NOT NULL, method text NOT NULL DEFAULT '',
    state text NOT NULL DEFAULT '', candidates integer NOT NULL DEFAULT 0, pages_seen integer NOT NULL DEFAULT 0,
    postings integer NOT NULL DEFAULT 0, us_postings integer NOT NULL DEFAULT 0, requests integer NOT NULL DEFAULT 0,
    browser_pages integer NOT NULL DEFAULT 0, error text NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_index_career_crawls_started ON index_career_crawls (started_at);
CREATE INDEX IF NOT EXISTS ix_index_career_crawls_host ON index_career_crawls (host, started_at)
"""


def upgrade() -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS index_career_crawls")
    op.execute("DROP TABLE IF EXISTS index_career_sites")
