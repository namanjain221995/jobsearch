"""SQLite -> PostgreSQL migration safety, and the parsing helpers used when engine results become job postings."""
import sqlite3

import asyncpg
import pytest

from app.database import Database, pack
from app.job_normalizer import html_to_structured_text
from app.portal.migrate_sqlite import migrate
from app.portal.search_runner import post_filter
from app.portal.services.jobs import parse_location, parse_salary
from app.schemas import Job
from tests.portal.conftest import PLAIN_URL


def _legacy_file(path: str, postings: int = 40) -> None:
    db = Database(path)
    rows = [(f"greenhouse:{n}", "AI Engineer" if n % 2 else "Account Executive", "ai engineer" if n % 2 else "account executive",
             "sig", pack(Job(title="t", company="Acme", url=f"https://acme.example/{n}", source="Greenhouse", ats="greenhouse", ats_id=str(n))))
            for n in range(postings)]
    db.replace_unit("greenhouse", "acme", "", rows, ttl=3600)
    db.fail_unit("lever", "beta", "", "HTTP 500")
    db.record_board("ashby", "newco", True, 7, "ai")
    db.meta_set("wd_us_facet:x|wd1|S", "{}")
    db.save_search("legacy-1", "completed", {"job_titles": "AI Engineer"}, {"final_matches": 3})
    db.save_jobs([Job(id="j1", title="AI Engineer", company="Acme", url="https://acme.example/1", source="Greenhouse")])
    db.close()


async def _counts() -> dict:
    conn = await asyncpg.connect(PLAIN_URL)
    try:
        return {t: await conn.fetchval(f"SELECT count(*) FROM {t}") for t in ("index_boards", "index_meta", "index_units", "index_postings", "legacy_records")}
    finally:
        await conn.close()


async def _truncate() -> None:
    conn = await asyncpg.connect(PLAIN_URL)
    await conn.execute("TRUNCATE index_postings, index_units, index_boards, index_meta, legacy_records RESTART IDENTITY CASCADE")
    await conn.close()


async def test_migration_copies_everything_verifies_and_is_repeatable(database, tmp_path, capsys):
    await _truncate()
    source = str(tmp_path / "legacy.db")
    _legacy_file(source)
    before = open(source, "rb").read()
    assert await migrate(source, PLAIN_URL, replace=False) == 0
    assert await _counts() == {"index_boards": 1, "index_meta": 1, "index_units": 2, "index_postings": 40, "legacy_records": 2}
    assert "full-text query 20 = 20" in capsys.readouterr().out            # same answer from both databases
    assert open(source, "rb").read() == before                             # the SQLite file is never written to
    assert await migrate(source, PLAIN_URL, replace=False) == 0            # second run: nothing to do
    assert (await _counts())["index_postings"] == 40
    conn = await asyncpg.connect(PLAIN_URL)
    try:                                                                   # new rows continue after the copied ids
        new_id = await conn.fetchval("INSERT INTO index_units (src, key, scope) VALUES ('x', 'y', '') RETURNING id")
        assert new_id > await conn.fetchval("SELECT max(id) FROM index_units WHERE src <> 'x'")
        assert await conn.fetchval("SELECT data FROM legacy_records WHERE kind='search'") is not None
    finally:
        await conn.close()


async def test_migration_refuses_to_overwrite_and_rolls_back_on_mismatch(database, tmp_path, capsys):
    await _truncate()
    source = str(tmp_path / "legacy.db")
    _legacy_file(source, postings=10)
    assert await migrate(source, PLAIN_URL, replace=False) == 0
    bigger = str(tmp_path / "bigger.db")
    _legacy_file(bigger, postings=25)
    assert await migrate(bigger, PLAIN_URL, replace=False) == 2            # target not empty: refused without --replace
    assert (await _counts())["index_postings"] == 10
    assert await migrate(bigger, PLAIN_URL, replace=True) == 0
    assert (await _counts())["index_postings"] == 25
    # A source whose full-text table disagrees with its rows fails verification: nothing in PostgreSQL changes.
    broken = str(tmp_path / "broken.db")
    _legacy_file(broken, postings=6)
    raw = sqlite3.connect(broken)
    raw.execute("DELETE FROM postings_fts")
    raw.commit()
    raw.close()
    assert await migrate(broken, PLAIN_URL, replace=True) == 1
    assert "nothing was changed" in capsys.readouterr().err
    assert (await _counts())["index_postings"] == 25                        # rolled back
    assert await migrate(str(tmp_path / "missing.db"), PLAIN_URL, replace=False) == 2
    await _truncate()


def test_structured_text_keeps_shape_and_drops_markup():
    html = ("&lt;h2&gt;About us&lt;/h2&gt;&lt;p&gt;We build &lt;b&gt;tools&lt;/b&gt;.&lt;/p&gt;<h3>Responsibilities</h3><ul><li>Train models</li>"
            "<li>Ship <a href='javascript:alert(1)'>APIs</a></li></ul><script>steal()</script><p>Salary: $120,000</p>")
    text = html_to_structured_text(html)
    assert text.splitlines() == ["About us", "", "We build tools.", "", "Responsibilities", "", "• Train models", "• Ship APIs", "", "Salary: $120,000"]
    assert "<" not in text and "steal" not in text and "javascript" not in text
    assert html_to_structured_text("") == "" and html_to_structured_text("plain text") == "plain text"


@pytest.mark.parametrize("text,expected", [
    ("San Francisco, CA", ("CA", "San Francisco")), ("US, CA, Santa Clara", ("CA", "Santa Clara")),
    ("Seattle, Washington, USA", ("WA", "Seattle")), ("New York, NY; London, UK", ("NY", "New York")),
    ("Remote - US", ("", "")), ("United States", ("", "")), ("", ("", "")), ("Berlin", ("", "")),
    ("New York, New York, United States", ("NY", "New York")), ("Washington, DC", ("DC", "Washington")), ("Texas", ("TX", "")),
    ("Remote, California, United States", ("CA", "")), ("Toronto, ON, CA", ("", "")), ("London, UK", ("", "")),
])
def test_parse_location_never_guesses(text, expected):
    assert parse_location(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("$150,000 - $210,000", (150000, 210000, "USD")), ("$132K - $240K", (132000, 240000, "USD")),
    ("USD 135,000 - 135,000 / annual", (135000, 135000, "USD")), ("GBP 31,659 - 37,246", (31659, 37246, "GBP")),
    ("Competitive", (None, None, "")), ("Not listed", (None, None, "")), ("", (None, None, "")),
])
def test_parse_salary_only_from_published_amounts(text, expected):
    assert parse_salary(text) == expected


def test_post_filter_applies_member_preferences():
    def job(company, location, mode="On-site"):
        return Job(title="AI Engineer", company=company, location=location, work_mode=mode, url=f"https://x.example/{company}", source="Greenhouse")
    jobs = [job("Acme AI", "Austin, TX"), job("Globex Inc.", "New York, NY"), job("Initech", "Seattle, WA"), job("Hooli", "Anywhere", "Remote")]
    assert [j.company for j in post_filter(jobs, {"excluded_companies": ["globex"]})] == ["Acme AI", "Initech", "Hooli"]
    assert [j.company for j in post_filter(jobs, {"preferred_states": ["NY", "washington"]})] == ["Globex Inc.", "Initech", "Hooli"]   # remote always fits
    assert post_filter(jobs, {}) == jobs


async def test_forwarded_host_is_trusted_only_behind_the_proxy(world):
    client = await world.as_admin()
    proxied = {"origin": "http://app.internal.example:3010", "x-forwarded-host": "app.internal.example:3010"}
    assert (await client.post("/api/teams", json={"name": "Via Proxy"}, headers=proxied)).status_code == 201
    world.settings.trust_proxy_headers = False                  # API exposed directly: the header proves nothing
    assert (await client.post("/api/teams", json={"name": "Direct"}, headers=proxied)).status_code == 403
