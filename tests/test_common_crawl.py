"""Common Crawl board discovery: recorded sample index lines, no network."""
import asyncio
import gzip
import json

import httpx

from app.portal import common_crawl as cc
from app.portal import import_boards

SAMPLE = [  # shape of real cdx lines (CC-MAIN-2026-43), trimmed
    'co,lever,jobs)/acme 20261006101010 {"url": "https://jobs.lever.co/acme", "status": "200"}',
    'co,lever,jobs)/acme/0b8c2a1e-1111-2222-3333-444455556666 20261006101011 {"url": "https://jobs.lever.co/acme/0b8c2a1e-1111-2222-3333-444455556666", "status": "200"}',
    'co,lever,jobs)/gone 20261006101012 {"url": "https://jobs.lever.co/gone", "status": "404"}',
    'co,lever,jobs)/robots.txt 20261006101013 {"url": "https://jobs.lever.co/robots.txt", "status": "200"}',
    'co,lever,jobs)/zeta?lever-source=x 20261006101014 {"url": "https://jobs.lever.co/Zeta?lever-source=x", "status": "301"}',
]
WD = 'com,myworkdayjobs,wd5,acme)/en-us/external/job/x 20261006 {"url": "https://acme.wd5.myworkdayjobs.com/en-US/External/job/x", "status": "200"}'


def test_board_key_per_ats():
    assert cc.board_key("greenhouse", "https://job-boards.greenhouse.io/Stripe/jobs/123") == "stripe"
    assert cc.board_key("greenhouse", "https://boards.greenhouse.io/embed/job_board?for=figma") == "figma"
    assert cc.board_key("greenhouse", "https://boards.greenhouse.io/robots.txt") is None
    assert cc.board_key("ashby", "https://jobs.ashbyhq.com/openai/abc") == "openai"
    assert cc.board_key("workable", "https://apply.workable.com/j/ABC123") is None
    assert cc.board_key("workable", "https://apply.workable.com/huggingface/") == "huggingface"
    assert cc.board_key("lever", "https://evil.example/acme") is None
    assert cc.board_key("workday", "https://acme.wd5.myworkdayjobs.com/en-US/External/job/x") == "acme|wd5|External"
    assert cc.board_key("workday", "https://wd5.myworkdayjobs.com/x") is None


def test_keys_from_lines_dedupes_and_skips_dead_and_reserved():
    n, keys = cc.keys_from_lines("lever", SAMPLE + ["garbage", WD], "co,lever,jobs)/")
    assert n == 5 and sorted(keys.values()) == ["acme", "zeta"]


def test_blocks_for_takes_the_block_before_the_prefix():
    idx = ["co,lemon)/a 1\tcdx-00001.gz\t0\t10\t1", "co,lever,jobs)/b 1\tcdx-00001.gz\t10\t10\t2",
           "co,lever,jobs)/z 1\tcdx-00001.gz\t20\t10\t3", "co,lexus)/ 1\tcdx-00001.gz\t30\t10\t4"]
    assert [b.seq for b in cc.blocks_for(idx, "co,lever,jobs)/")] == [1, 2, 3]


def _transport(calls, fail_first=0):
    block = gzip.compress(("\n".join(SAMPLE) + "\n").encode())
    cluster = ("co,lemon)/a 1\tcdx-00001.gz\t0\t%d\t1\nco,lexus)/ 1\tcdx-00001.gz\t%d\t5\t2\n" % (len(block), len(block))).encode()
    state = {"fail": fail_first}

    def handler(request: httpx.Request):
        calls.append(request)
        assert request.headers["user-agent"].startswith("jobsearch-board-discovery")
        if state["fail"] > 0:
            state["fail"] -= 1
            return httpx.Response(504)
        if request.url.path == "/collinfo.json":
            return httpx.Response(200, json=[{"id": "CC-MAIN-2026-43"}, {"id": "CC-MAIN-2026-39"}])
        data = cluster if request.url.path.endswith("cluster.idx") else block
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(data))})
        a, b = request.headers["range"].removeprefix("bytes=").split("-")
        return httpx.Response(206, content=data[int(a): int(b) + 1])
    return httpx.MockTransport(handler)


def test_run_reads_pages_retries_and_resumes(tmp_path):
    calls, sleeps = [], []

    async def sleep(s):
        sleeps.append(s)

    async def go():
        async with httpx.AsyncClient(transport=_transport(calls, fail_first=2)) as client:
            r = cc.CommonCrawl(client, tmp_path / "s.json", gap=0, sleep=sleep, out=None)
            return await r.run(1, ["lever"])
    st = asyncio.run(go())["lever"]
    assert st.urls == 5 and sorted(st.keys.values()) == ["acme", "zeta"] and st.failures == 0
    assert len([s for s in sleeps if s >= 15]) == 2                  # two backoffs (~20 s, ~40 s) before success
    saved = json.loads((tmp_path / "s.json").read_text())["done"]
    assert list(saved) == ["CC-MAIN-2026-43|jobs.lever.co/*|1"]

    calls.clear()

    async def again():
        async with httpx.AsyncClient(transport=_transport(calls)) as client:
            return await cc.CommonCrawl(client, tmp_path / "s.json", gap=0, sleep=sleep, out=None).run(1, ["lever"])
    st2 = asyncio.run(again())["lever"]
    assert st2.resumed == 1 and st2.urls == 5
    assert not any("cdx-00001.gz" in str(c.url) for c in calls)        # the block was not fetched again


def test_import_registers_with_commoncrawl_origin(tmp_path, monkeypatch):
    from tests.test_board_catalog import FakeIndex
    monkeypatch.setattr(cc, "DEFAULT_STATE", tmp_path / "s.json")
    index = FakeIndex([("lever", "acme", "seed")])
    seen = []
    orig = index.register_catalog

    async def reg(pairs, origin="catalog"):
        seen.append(origin)
        return await orig(pairs)
    index.register_catalog = reg

    async def go():
        async with httpx.AsyncClient(transport=_transport([])) as client:
            return await import_boards.main(["--common-crawl", "1", "--only", "lever", "--cc-state", str(tmp_path / "s.json")],
                                            index=index, client=client)
    rep = asyncio.run(go())["ats"]["lever"]
    assert rep["new"] == 1 and rep["registered"] == 1 and rep["common_crawl"]["distinct"] == 2 and seen == ["commoncrawl"]
