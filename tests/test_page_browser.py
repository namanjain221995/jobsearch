"""The crawler's browser (app/browser_pool.py) in a real Chromium against a local site. Nothing leaves this machine:
the guard proxy's resolver only knows the fake hosts below and sends them to a server on 127.0.0.1.

What is proven: the page is taken when it has shown its content (not after a fixed pause), late JobPosting data is
waited for, a page that stays empty is not waited for longer than its network is busy, the text returned is what a
reader sees, and the browser cannot be used to submit anything or to reach a host that is never fetched."""
import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.browser_pool import BrowserPool, _reads_only
from app.job_verifier import host_blocked

pytest.importorskip("playwright.async_api")

SITE, OTHER, FORBIDDEN = "careers.fake-employer.example", "tracker.elsewhere.example", "www.linkedin.com"
JSONLD = ('{"@context":"https://schema.org","@type":"JobPosting","title":"Data Engineer","datePosted":"2026-11-20",'
          '"hiringOrganization":{"@type":"Organization","name":"Fake Employer"},'
          '"jobLocation":{"@type":"Place","address":{"addressLocality":"Austin","addressRegion":"TX","addressCountry":"US"}}}')
FILLER = "Build pipelines with Python and SQL. " * 60          # a page full of text
PAGES = {
    "/static": f"<html><head><title>Data Engineer - Fake Employer</title></head><body><h1>Data Engineer</h1><p>{FILLER}</p><button>Apply now</button></body></html>",
    # a script shell: the posting arrives 2.5 s after the document, its structured data 1.5 s later still
    "/shell": ("<html><head><title>Careers</title></head><body><div id='app'>Loading</div><script>"
               "setTimeout(() => { document.title = 'Data Engineer - Fake Employer';"
               f" document.getElementById('app').innerHTML = '<h1>Data Engineer</h1><p>{FILLER}</p><button>Apply now</button>'; }}, 2500);"
               "setTimeout(() => { const s = document.createElement('script'); s.type = 'application/ld+json';"
               f" s.textContent = '{JSONLD}'; document.head.appendChild(s); }}, 4000);"
               "</script></body></html>"),
    # a shell that only ever shows one short line, and loads nothing more
    "/gone": ("<html><head><title>Careers</title></head><body><div id='app'></div><script>"
              "setTimeout(() => { document.getElementById('app').textContent = 'This job is no longer available.'; }, 800);</script></body></html>"),
    # a shell that shows one short line late, while it is still loading things
    "/slow-gone": ("<html><head><title>Careers</title></head><body><div id='app'>Cookies</div><script>"
                   "let n = 0; const t = setInterval(() => { fetch('/ping?' + (n++)); if (n > 9) clearInterval(t); }, 600);"
                   "setTimeout(() => { document.getElementById('app').textContent = 'We are no longer accepting applications for this position.'; }, 5500);"
                   "</script></body></html>"),
    "/hidden": ("<html><head><title>Data Engineer</title><link rel='stylesheet' href='/site.css'></head><body><h1>Data Engineer</h1>"
                f"<p>{FILLER}</p><div class='tpl'>This job is no longer available.</div><div style='display:none'>Session expired</div></body></html>"),
    "/site.css": ".tpl { display: none }",
    # a page that tries to use the visitor's browser: a beacon and a text POST to another site, and requests to a
    # host this app never fetches
    "/hostile": (f"<html><head><title>Data Engineer</title></head><body><h1>Data Engineer</h1><p>{FILLER}</p>"
                 f"<img src='http://{FORBIDDEN}/px.gif'><script src='http://{FORBIDDEN}/widget.js'></script><script>"
                 f"fetch('http://{OTHER}/track', {{method: 'POST', body: 'a=1'}}).catch(() => 0);"
                 f"fetch('http://{FORBIDDEN}/li/track', {{method: 'POST', body: 'x'}}).catch(() => 0);"
                 f"navigator.sendBeacon('http://{OTHER}/beacon', 'b');"
                 "fetch('/api/read', {method: 'POST', headers: {'content-type': 'application/json'}, body: '{\"query\":\"{job{title}}\"}'}).catch(() => 0);"
                 "fetch('/api/form', {method: 'POST', body: new URLSearchParams({a: '1'})}).catch(() => 0);</script></body></html>"),
    # ... and one that submits a form by itself, to another site or to its own
    "/autosubmit": (f"<html><head><title>Data Engineer</title></head><body><h1>Data Engineer</h1><p>{FILLER}</p>"
                    f"<form id='f' method='post' action='http://{OTHER}/collect'><input name='comment' value='spam'></form>"
                    "<script>setTimeout(() => document.getElementById('f').submit(), 300);</script></body></html>"),
    "/autosubmit-home": (f"<html><head><title>Data Engineer</title></head><body><h1>Data Engineer</h1><p>{FILLER}</p>"
                         "<form id='f' method='post' action='/apply'><input name='name' value='x'></form>"
                         "<script>setTimeout(() => document.getElementById('f').submit(), 300);</script></body></html>"),
}
# Job lists built by script, as on the career sites read on 2026-11-21: three jobs at a time, the next part behind a
# button without an address. Around the list stand the controls a crawler must never press: a carousel's "Next
# similar jobs", an "Apply" button, and a form whose submit button says "Next".
_LIST_HEAD = ("<html><head><title>Jobs at Fake Employer</title></head><body><h1>Open positions</h1><p>" + "We build things. " * 120 + "</p>"
              "<ul id='jobs'></ul><script>const ids = [1001,1002,1003,1004,1005,1006,1007,1008,1009]; let shown = 0;"
              "const add = (id) => { const li = document.createElement('li'); li.innerHTML = '<a href=\"/jobs/' + id + '\">Engineer ' + id + '</a>';"
              " document.getElementById('jobs').appendChild(li); };"
              "const trap = (name) => { const a = document.createElement('a'); a.href = '/trap/' + name; a.textContent = name; document.body.appendChild(a); };")
_DECOYS = ("<button aria-label='Previous jobs' disabled>&lt;</button><button aria-label='Next similar jobs' onclick=\"trap('carousel')\">&gt;</button>"
           "<button onclick=\"trap('apply')\">Apply</button>"
           "<form method='post' action='/apply-next'><input name='email' value='a@example.com'><button type='submit'>Next</button></form>")
PAGES.update({
    # "Next jobs" (an icon button: the words are its aria-label) replaces the three jobs shown with the next three
    "/list": (_LIST_HEAD + "const show = () => { document.getElementById('jobs').innerHTML = ''; ids.slice(shown, shown + 3).forEach(add); };"
              "show(); function next() { fetch('/api/jobs?start=' + (shown + 3)).then(() => { shown += 3; show();"
              " if (shown + 3 >= ids.length) document.getElementById('n').setAttribute('aria-disabled', 'true'); }); }</script>"
              + _DECOYS + "<button id='n' aria-label='Next jobs' onclick='next()'><svg width='10' height='10'></svg>&gt;</button></body></html>"),
    # "Load more jobs" adds three jobs to the list and goes away when there are no more
    "/list-more": (_LIST_HEAD + "const more = () => { ids.slice(shown, shown + 3).forEach(add); shown += 3;"
                   " if (shown >= ids.length) document.getElementById('m').remove(); }; more();</script>"
                   + _DECOYS + "<button id='m' onclick='setTimeout(more, 200)'>Load more jobs</button></body></html>"),
    # the only "Next" belongs to a form: it is never pressed
    "/list-form": (_LIST_HEAD + "ids.slice(0, 3).forEach(add);</script>" + _DECOYS + "</body></html>"),
    # a "Next jobs" button that tries to send a form when it is pressed
    "/list-post": (_LIST_HEAD + "ids.slice(0, 3).forEach(add);</script><form id='f' method='post' action='/collect'><input name='q' value='x'></form>"
                   "<button aria-label='Next jobs' onclick=\"document.getElementById('f').submit()\">&gt;</button></body></html>"),
})


class Site:
    def __init__(self):
        self.seen: list[tuple[str, str, str]] = []          # (method, host, path)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                host = (self.headers.get("host") or "").split(":")[0]
                outer.seen.append((self.command, host, self.path))
                length = int(self.headers.get("content-length") or 0)
                if length:
                    self.rfile.read(length)
                body = PAGES.get(self.path.split("?")[0], "ok") if host == SITE else "ok"
                self.send_response(200)
                self.send_header("content-type", "text/css" if self.path.endswith(".css") else "text/html; charset=utf-8")
                self.send_header("access-control-allow-origin", "*")
                self.send_header("content-length", str(len(body.encode())))
                self.end_headers()
                self.wfile.write(body.encode())
            do_GET = do_POST = do_OPTIONS = _serve

            def log_message(self, *args):
                return
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    async def resolver(self, host: str, port: int):
        if host in (SITE, OTHER, FORBIDDEN):
            return "127.0.0.1", self.port
        raise ValueError("unknown host")

    def close(self):
        self.server.shutdown()
        self.server.server_close()


async def guard(url: str) -> None:
    if host_blocked(url):                       # the same rule the search engine's guard applies (search_engine._guard)
        raise ValueError("never fetched")


@pytest.fixture(scope="module")
def site():
    s = Site()
    yield s
    s.close()


def render(site, path, **kw):
    async def go():
        pool = BrowserPool(concurrency=2, guard=guard, resolver=site.resolver, **kw)
        try:
            started = time.time()
            result = await pool.render(f"http://{SITE}{path}")
            return result, time.time() - started, pool
        finally:
            await pool.aclose()
    result, seconds, pool = asyncio.run(go())
    if result.error and "Chromium could not start" in result.error:
        pytest.skip(result.error)
    return result, seconds, pool


def test_a_page_that_is_already_complete_is_not_waited_for(site):
    result, seconds, _ = render(site, "/static")
    assert not result.error and "Apply now" in result.text and "<h1>Data Engineer</h1>" in result.html
    assert seconds < 8                                   # start of the browser included; the wait itself is about `settle` seconds


def test_a_script_shell_is_given_time_and_late_posting_data_is_waited_for(site):
    result, seconds, _ = render(site, "/shell")
    assert not result.error and "Build pipelines" in result.text and "Loading" not in result.text
    assert '"@type":"JobPosting"' in result.html           # added 1.5 s after the text: the page was not taken before it
    assert seconds < 12


def test_a_page_that_stays_nearly_empty_is_taken_once_nothing_loads_any_more(site):
    result, seconds, _ = render(site, "/gone")
    assert result.text.strip() == "This job is no longer available." and seconds < 10      # not the full max_wait


def test_a_slow_shell_is_not_given_up_while_it_is_still_loading(site):
    result, _, _ = render(site, "/slow-gone")
    assert "no longer accepting applications" in result.text


def test_the_text_is_what_a_reader_sees(site):
    result, _, _ = render(site, "/hidden")
    assert "Build pipelines" in result.text
    assert "no longer available" not in result.text and "Session expired" not in result.text      # hidden by the page's own rules
    assert "no longer available" in result.html             # still in the markup: only the visible text leaves it out
    assert ("GET", SITE, "/site.css") in site.seen          # stylesheets are loaded, or the first line could not hold


def test_a_page_cannot_make_the_browser_write_elsewhere_or_reach_a_forbidden_host(site):
    site.seen.clear()
    result, _, pool = render(site, "/hostile")
    assert not result.error and "Build pipelines" in result.text
    posts = {(host, path.split("?")[0]) for method, host, path in site.seen if method == "POST"}
    assert posts == {(SITE, "/api/read"), (SITE, "/api/form")}          # the page's own host only: its beacon and tracker never left
    assert not any(host == FORBIDDEN for _, host, _ in site.seen)       # not even a GET: the host is never contacted
    assert not any(host == OTHER for method, host, _ in site.seen if method != "OPTIONS")
    assert pool.blocked_writes >= 2


@pytest.mark.parametrize("path", ["/autosubmit", "/autosubmit-home"])
def test_a_form_that_submits_itself_is_never_sent(site, path):
    site.seen.clear()
    result, _, pool = render(site, path)
    assert not any(method == "POST" for method, _, _ in site.seen) and pool.blocked_writes >= 1
    assert result.error and not result.html            # the page left itself for an error page: it is not read as a posting


def render_list(site, path, steps, **kw):
    async def go():
        pool = BrowserPool(concurrency=2, guard=guard, resolver=site.resolver, **kw)
        try:
            started = time.time()
            result = await pool.render_list(f"http://{SITE}{path}", steps, pause=1.0)
            return result, time.time() - started, pool
        finally:
            await pool.aclose()
    site.seen.clear()
    result, seconds, pool = asyncio.run(go())
    if result.error and "Chromium could not start" in result.error:
        pytest.skip(result.error)
    return result, seconds, pool


def job_links(links) -> list[str]:
    return sorted({link.rsplit("/", 1)[-1] for link in links if "/jobs/" in link})


def test_a_script_built_list_is_followed_through_its_own_next_button(site):
    result, seconds, pool = render_list(site, "/list", 5)
    assert not result.error and all(f'href="/jobs/{n}"' in result.html for n in (1001, 1002, 1003))      # the first view, as always
    assert result.steps == 2 and pool.paging_steps == 2                 # nine jobs, three at a time: after two presses the button is disabled
    assert job_links(result.links) == ["1004", "1005", "1006", "1007", "1008", "1009"]
    assert seconds >= 2.0                                               # a pause of at least a second before every press
    assert not any("/trap/" in link for link in result.links)           # the carousel and "Apply" were not pressed
    assert not any(method != "GET" for method, _, _ in site.seen)       # ... and neither was the form's "Next"
    assert [path for _, _, path in site.seen if path.startswith("/api/jobs")] == ["/api/jobs?start=3", "/api/jobs?start=6"]


def test_a_load_more_button_is_pressed_until_it_is_gone_and_never_more_often_than_asked(site):
    result, _, _ = render_list(site, "/list-more", 5)
    assert result.steps == 2 and job_links(result.links) == [str(n) for n in range(1001, 1010)]
    result, _, _ = render_list(site, "/list-more", 1)
    assert result.steps == 1 and job_links(result.links) == [str(n) for n in range(1001, 1007)]
    assert not any(method != "GET" for method, _, _ in site.seen) and not any("/trap/" in link for link in result.links)


def test_nothing_is_pressed_unless_paging_is_asked_for_and_never_a_forms_button(site):
    result, _, pool = render_list(site, "/list", 0)                     # the off switch: exactly render()
    assert not result.error and result.steps == 0 and result.links == () and pool.paging_steps == 0
    assert not any(path.startswith("/api/jobs") for _, _, path in site.seen)
    result, _, pool = render_list(site, "/list-form", 5)                # "Next" is there, but it submits a form
    assert not result.error and result.steps == 0 and pool.paging_steps == 0
    assert not any(method != "GET" for method, _, _ in site.seen) and not any(path.startswith("/apply-next") for _, _, path in site.seen)


def test_a_paging_button_cannot_be_used_to_send_a_form(site):
    result, _, pool = render_list(site, "/list-post", 3)
    assert result.steps == 1 and pool.blocked_writes >= 1               # pressed once; what it tried to send was refused
    assert not any(method == "POST" for method, _, _ in site.seen) and result.links == ()
    assert all(f'href="/jobs/{n}"' in result.html for n in (1001, 1002, 1003))      # the first view is still what was read


def test_which_labels_are_a_paging_control():
    import re
    from app.browser_pool import MAX_PAGING_STEPS, PAGING_LABEL
    rx = re.compile(PAGING_LABEL, re.I)
    for label in ("Next", "next page", "Next jobs", "Load more", "Load more jobs", "Show more", "Show more results", "View more jobs", "More jobs",
                  "Next ›", "› Next"):
        assert rx.match(label), label
    for label in ("Next similar jobs", "Next step", "Apply", "Apply now", "Submit", "Search", "Sign in", "Continue", "Next: your details",
                  "Load more filters", "More", "Accept all", "Send application", "Upload resume", "Save and next", "Next month"):
        assert not rx.match(label), label
    assert 0 < MAX_PAGING_STEPS <= 50


def test_which_requests_may_leave():
    assert _reads_only("GET", "document", "a.example", "b.example", "")
    assert _reads_only("POST", "fetch", "api.acme.example", "acme.example", "text/plain")                # the page's own site
    assert _reads_only("POST", "xhr", "search.vendor.example", "acme.example", "application/json")        # JSON: preflighted by the other site
    assert _reads_only("POST", "fetch", "x.example", "acme.example", "application/vnd.oracle.adf.resourceitem+json")
    assert not _reads_only("POST", "document", "acme.example", "acme.example", "application/x-www-form-urlencoded")   # a form submit, even at home
    assert not _reads_only("POST", "fetch", "other.example", "acme.example", "text/plain;charset=UTF-8")
    assert not _reads_only("POST", "fetch", "other.example", "acme.example", "application/x-www-form-urlencoded")
    assert not _reads_only("POST", "ping", "acme.example", "acme.example", "")
    assert not _reads_only("PUT", "fetch", "jobs.acme.example", "careers.acme.example", "multipart/form-data")       # sibling hosts are not "the same site"
    assert not _reads_only("DELETE", "other", "acme.example", "acme.example", "application/json")


def test_lookups_are_remembered_and_failures_only_briefly():
    calls = []

    async def resolver(host, port):
        calls.append(host)
        if host == "down.example":
            raise ValueError("no answer")
        return "203.0.113.7"

    async def go():
        pool = BrowserPool(resolver=resolver)
        assert await pool._resolve("Careers.Example", 443) == "203.0.113.7" and await pool._resolve("careers.example", 443) == "203.0.113.7"
        for _ in range(2):
            with pytest.raises(ValueError):
                await pool._resolve("down.example", 443)
        return pool
    pool = asyncio.run(go())
    assert calls == ["Careers.Example", "down.example"]            # one lookup each; the refusal is kept for seconds, not minutes
    assert pool._dns[("down.example", 443)][0] - time.monotonic() < 16 < pool._dns[("careers.example", 443)][0] - time.monotonic()
