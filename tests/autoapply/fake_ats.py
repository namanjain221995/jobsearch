"""A local fake of the four application systems, served over plain http on 127.0.0.1.

The browser reaches it under the real host names (job-boards.greenhouse.io, jobs.lever.co, jobs.ashbyhq.com,
apply.workable.com): the guard proxy's resolver used in the tests maps exactly these names to this server and
refuses every other host, so nothing in these tests can reach the internet. Every request is recorded; `posts`
counts the requests that could submit something (any method other than GET/HEAD)."""
import json
import os
import ssl
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOSTS = ("job-boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com", "apply.workable.com")
LEVER_ID = "11111111-2222-3333-4444-555555555555"
ASHBY_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font:14px sans-serif;margin:20px}} label{{display:block;margin-top:8px}} .field{{margin:8px 0}}
.listbox{{border:1px solid #999;background:#fff}} .hidden-file{{position:absolute;width:1px;height:1px;opacity:0}}</style></head>
<body>{body}</body></html>"""

INVISIBLE_RECAPTCHA = ('<iframe title="reCAPTCHA" width="256" height="60" style="position:fixed;bottom:0;right:0" '
                       'src="https://www.google.com/recaptcha/enterprise/anchor?ar=1&k=x&size=invisible"></iframe>')
VISIBLE_CHALLENGE = ('<iframe title="recaptcha challenge expires in two minutes" width="400" height="580" '
                     'src="https://www.google.com/recaptcha/enterprise/bframe?hl=en&k=x"></iframe>')

COMBOBOX_JS = """
<script>
document.querySelectorAll('[role=combobox]').forEach(function (box) {
  var list = document.getElementById(box.getAttribute('aria-controls'));
  box.addEventListener('click', function () { list.style.display = 'block'; });
  box.addEventListener('keydown', function (e) { if (e.key === 'Escape') list.style.display = 'none'; });
  list.querySelectorAll('[role=option]').forEach(function (o) {
    o.addEventListener('click', function () { box.value = o.textContent; document.getElementById(box.dataset.target).value = o.textContent; list.style.display = 'none'; });
  });
});
</script>"""


def greenhouse_form(company: str, extra: str = "", action: str = "") -> str:
    return f"""<h1>Machine Learning Engineer</h1><p>{company.capitalize()} builds things.</p>
<form id="application-form" method="post" enctype="multipart/form-data" action="{action or f'/{company}/jobs/1001/applications'}">
 <div class="field"><label for="first_name">First Name<span>*</span></label><input id="first_name" name="first_name" required></div>
 <div class="field"><label for="last_name">Last Name<span>*</span></label><input id="last_name" name="last_name" required></div>
 <div class="field"><label for="email">Email<span>*</span></label><input id="email" name="email" type="email" required></div>
 <div class="field"><label for="phone">Phone</label><input id="phone" name="phone" type="tel"></div>
 <div class="field"><label id="country-label" for="country">Country*</label>
   <input id="country" role="combobox" aria-labelledby="country-label" aria-required="true" aria-controls="country-list" data-target="country_value" autocomplete="off">
   <input type="hidden" id="country_value" name="country">
   <div id="country-list" class="listbox" role="listbox" style="display:none"><div role="option">United States</div><div role="option">Canada</div></div></div>
 <div class="field"><label for="resume">Resume/CV*</label><input id="resume" name="resume" type="file" class="hidden-file" required></div>
 <div class="field"><label for="cover_letter">Cover Letter</label><input id="cover_letter" name="cover_letter" type="file" class="hidden-file"></div>
 <div class="field"><label for="question_1">LinkedIn Profile</label><input id="question_1" name="question_1"></div>
 <div class="field"><label for="question_2">Why do you want to work at {company.capitalize()}?*</label><textarea id="question_2" name="question_2" required></textarea></div>
 <div class="field"><label for="question_3">Are you legally authorized to work in the United States?*</label>
   <select id="question_3" name="question_3" required><option value="">Select...</option><option>Yes</option><option>No</option></select></div>
 <div class="field"><label for="question_4">Will you now or in the future require sponsorship for employment visa status?*</label>
   <select id="question_4" name="question_4" required><option value="">Select...</option><option>Yes</option><option>No</option></select></div>
 <div class="field"><label for="question_5">Have you previously worked for {company.capitalize()}?</label>
   <select id="question_5" name="question_5"><option value="">Select...</option><option>Yes</option><option>No</option></select></div>
 <div id="eeo-section"><h3>Voluntary Self-Identification</h3>
 <div class="field"><label for="gender">Gender</label><select id="gender" name="gender"><option value="">Select...</option><option>Male</option><option>Female</option><option>Decline To Self Identify</option></select></div>
 <div class="field"><label for="race">Race</label><select id="race" name="race"><option value="">Select...</option><option>Asian</option><option>White</option><option>Decline To Self Identify</option></select></div>
 <div class="field"><label for="veteran_status">Veteran Status*</label><select id="veteran_status" name="veteran_status" required><option value="">Select...</option><option>I am not a protected veteran</option><option>I identify as one or more of the classifications of a protected veteran</option><option>I don't wish to answer</option></select></div>
 </div>
 {extra}
 <input type="text" name="website_hp" style="display:none" tabindex="-1">
 <button type="submit">Submit application</button>
</form>{INVISIBLE_RECAPTCHA}{COMBOBOX_JS}"""


def lever_form(company: str, extra: str = "") -> str:
    return f"""<h2>Machine Learning Engineer</h2>
<form id="application-form" method="post" enctype="multipart/form-data" action="/{company}/{LEVER_ID}/apply">
 <div class="application-question"><div class="application-label">Resume/CV<span class="required">✱</span></div>
   <input type="file" name="resume" class="hidden-file" required></div>
 <div class="application-question"><label><div class="application-label">Full name<span class="required">✱</span></div><input name="name" required></label></div>
 <div class="application-question"><label><div class="application-label">Email<span class="required">✱</span></div><input name="email" type="email" required></label></div>
 <div class="application-question"><label><div class="application-label">Phone</div><input name="phone"></label></div>
 <div class="application-question"><label><div class="application-label">Current company</div><input name="org"></label></div>
 <div class="application-question"><label><div class="application-label">LinkedIn URL</div><input name="urls[LinkedIn]"></label></div>
 <div class="application-question custom-question"><fieldset><legend>Are you authorized to work in the US?<span class="required">✱</span></legend>
   <label><input type="radio" name="cards[abc][field0]" value="Yes" required> Yes</label>
   <label><input type="radio" name="cards[abc][field0]" value="No"> No</label></fieldset></div>
 <div class="application-question custom-question"><fieldset><legend>Do you now or will you in the future require visa sponsorship?<span class="required">✱</span></legend>
   <label><input type="radio" name="cards[abc][field1]" value="Yes" required> Yes</label>
   <label><input type="radio" name="cards[abc][field1]" value="No"> No</label></fieldset></div>
 <div class="application-question"><label><div class="application-label">Additional information</div><textarea name="comments"></textarea></label></div>
 <div class="eeo-section"><label><div class="application-label">Gender</div><select name="eeo[gender]"><option value="">Select ...</option><option>Male</option><option>Female</option><option>Decline to self-identify</option></select></label></div>
 {extra}
 <button type="submit" id="btn-submit">Submit application</button>
</form>"""


def ashby_form(company: str, extra: str = "", note: str = "") -> str:
    return f"""<h1>Machine Learning Engineer</h1>{note}
<div class="ashby-application-form-container"><form id="ashby-form" onsubmit="return false">
 <div class="field"><label for="_systemfield_name">Name *</label><input id="_systemfield_name" name="_systemfield_name" required></div>
 <div class="field"><label for="_systemfield_email">Email *</label><input id="_systemfield_email" name="_systemfield_email" type="email" required></div>
 <div class="field"><label for="_systemfield_resume">Resume *</label><input id="_systemfield_resume" type="file" class="hidden-file" required></div>
 <div class="field"><label for="phone">Phone Number</label><input id="phone" name="phone" type="tel"></div>
 <div class="field required"><label>Are you legally authorized to work in the United States? *</label>
   <div class="yesno"><button type="button" onclick="this.parentElement.dataset.v='Yes'">Yes</button><button type="button" onclick="this.parentElement.dataset.v='No'">No</button></div></div>
 {extra}
 <button type="submit" id="submit" onclick="send()">Submit Application</button>
</form></div>
<script>
fetch('/api/non-user-graphql?op=ApiJobPosting', {{method: 'POST', headers: {{'content-type': 'application/json'}},
  body: JSON.stringify({{operationName: 'ApiJobPosting', query: 'query ApiJobPosting {{ jobPosting {{ id }} }}'}})}});
function send() {{
  fetch('/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction', {{method: 'POST', headers: {{'content-type': 'application/json'}},
    body: JSON.stringify({{operationName: 'Submit', query: 'mutation Submit {{ submit {{ id }} }}'}})}})
   .then(function () {{ document.body.innerHTML = '<h2>Your application was successfully submitted.</h2><p>We will be in touch.</p>'; }});
}}
</script>"""


def workable_form(company: str, extra: str = "") -> str:
    return f"""<h1>ML Engineer</h1>
<form data-ui="application-form" method="post" action="/{company}/j/ABC123/apply/">
 <div class="field"><label for="firstname">First name*</label><input id="firstname" name="firstname" required></div>
 <div class="field"><label for="lastname">Last name*</label><input id="lastname" name="lastname" required></div>
 <div class="field"><label for="wemail">Email*</label><input id="wemail" name="email" type="email" required></div>
 <div class="field"><label for="wphone">Phone*</label><input id="wphone" name="phone" type="tel" required></div>
 <div class="field"><label for="wresume">Resume*</label><input id="wresume" name="resume" type="file" class="hidden-file" required></div>
 <div class="field"><label for="salary">What are your salary expectations?*</label><input id="salary" name="salary" required></div>
 <div class="field"><label for="notice">What is your notice period?*</label><input id="notice" name="notice" required></div>
 <fieldset><legend>Do you require visa sponsorship?*</legend>
   <label><input type="radio" name="sponsor" value="yes" required> Yes</label><label><input type="radio" name="sponsor" value="no"> No</label></fieldset>
 <fieldset><legend>Gender*</legend>
   <label><input type="radio" name="gender" value="m" required> Male</label><label><input type="radio" name="gender" value="f"> Female</label>
   <label><input type="radio" name="gender" value="x"> Prefer not to say</label></fieldset>
 <div class="field"><label><input type="checkbox" name="privacy" required> I have read and accept the candidate privacy notice*</label></div>
 {extra}
 <button type="submit">Submit application</button>
</form>"""


def pages() -> dict:
    """(host, path) -> html."""
    gh, lv, ab, wk = HOSTS
    return {
        (gh, "/acme/jobs/1001"): ("Job Application for Machine Learning Engineer at Acme", greenhouse_form("acme")),
        (gh, "/acme/jobs/1001/confirmation"): ("Thank you", "<h1>Thank you for applying to Acme!</h1><p>Application ID: GH-4711</p>"),
        (gh, "/captcha/jobs/2001"): ("Job Application at Captcha Co", greenhouse_form("captcha", VISIBLE_CHALLENGE, "/captcha/jobs/2001/applications")),
        (gh, "/signin/jobs/2002"): ("Sign in", "<h1>Sign in to apply</h1><form><label for=u>Email</label><input id=u type=email>"
                                    "<label for=p>Password</label><input id=p type=password><button>Sign in</button></form>"),
        (gh, "/nojs/jobs/2003"): ("Gone", "<h1>Sorry, this job is no longer accepting applications.</h1>"),
        (lv, f"/acme/{LEVER_ID}/apply"): ("Acme - Machine Learning Engineer", lever_form("acme")),
        (lv, f"/arb/{LEVER_ID}/apply"): ("Arb - ML Engineer", lever_form("arb", '<div class="application-question"><label><input type="checkbox" '
                                         'name="arbitration" required> I agree to the Mutual Arbitration Agreement<span class="required">✱</span></label></div>')),
        (lv, f"/acme/{LEVER_ID}/thanks"): ("Thanks", "<h3>Application submitted!</h3><p>Thank you for your application.</p>"),
        (ab, f"/acme/{ASHBY_ID}/application"): ("Machine Learning Engineer @ Acme", ashby_form("acme")),
        (ab, f"/puzzle/{ASHBY_ID}/application"): ("ML @ Puzzle", ashby_form("puzzle", note="<p>To prove you are not a bot auto-applying, "
                                                  "solve this puzzle: what is the third word of this sentence?</p>")),
        (ab, f"/certify/{ASHBY_ID}/application"): ("ML @ Certify", ashby_form("certify", '<div class="field"><label><input type="checkbox" id="pc" '
                                                   'required> I have personally completed this application without the use of automation *</label></div>')),
        (wk, "/acme/j/ABC123/apply/"): ("ML Engineer - Acme", workable_form("acme")),
        (wk, "/unknown/j/UNK999/apply/"): ("ML Engineer - Unknown", workable_form("unknown", '<div class="field"><label for="shirt">T-shirt size*</label>'
                                           '<select id="shirt" name="shirt" required><option value="">Select</option><option>S</option><option>M</option></select></div>')),
    }


REDIRECT_AFTER_POST = {
    ("job-boards.greenhouse.io", "/acme/jobs/1001/applications"): "/acme/jobs/1001/confirmation",
    ("jobs.lever.co", f"/acme/{LEVER_ID}/apply"): f"/acme/{LEVER_ID}/thanks",
}


class FakeATS:
    def __init__(self):
        self.requests: list[tuple[str, str, str]] = []          # (method, host, path)
        self.bodies: list[bytes] = []
        self.pages = pages()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _host(self):
                return (self.headers.get("Host") or "").split(":")[0]

            def do_GET(self):
                host, path = self._host(), self.path.split("?")[0]
                fake.requests.append(("GET", host, path))
                if path == "/robots.txt":
                    return self._send(200, "User-agent: *\nDisallow: /api/\n" if host != "apply.workable.com" else "User-agent: *\nDisallow: /blocked/\n", "text/plain")
                hit = fake.pages.get((host, path))
                if hit is None:
                    return self._send(404, PAGE.format(title="Not found", body="<h1>Page not found</h1>"))
                self._send(200, PAGE.format(title=hit[0], body=hit[1]))

            def _post(self):
                host, path = self._host(), self.path.split("?")[0]
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                fake.requests.append((self.command, host, path))
                fake.bodies.append(body)
                if path.startswith("/api/non-user-graphql"):
                    return self._send(200, json.dumps({"data": {}}), "application/json")
                target = REDIRECT_AFTER_POST.get((host, path))
                if target is None and host == "apply.workable.com":
                    return self._send(200, PAGE.format(title="Thanks", body="<h2>Thank you for applying!</h2><p>Your application has been received.</p>"))
                if target:
                    self.send_response(303)
                    self.send_header("Location", target)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._send(200, PAGE.format(title="Hmm", body="<p>Received.</p>"))

            do_POST = do_PUT = do_PATCH = do_DELETE = _post

            def _send(self, status, text, ctype="text/html; charset=utf-8"):
                data = text.encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        # Chromium upgrades HSTS-preloaded hosts (ashbyhq.com, workable.com) to https: a TLS twin with a self-signed
        # certificate answers on port 443 of the fake hosts (the test browser ignores certificate errors).
        self._dir = tempfile.mkdtemp(prefix="fake-ats-")
        cert, key = os.path.join(self._dir, "c.pem"), os.path.join(self._dir, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=fake-ats",
                        "-keyout", key, "-out", cert], check=True, capture_output=True)
        self.tls = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.tls.socket = context.wrap_socket(self.tls.socket, server_side=True)
        self.tls_port = self.tls.server_address[1]
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.server, self.tls)]

    def __enter__(self):
        for t in self.threads:
            t.start()
        return self

    def __exit__(self, *exc):
        for s in (self.server, self.tls):
            s.shutdown()
            s.server_close()

    @property
    def posts(self) -> list[tuple[str, str, str]]:
        return [r for r in self.requests if r[0] not in ("GET", "HEAD")]

    def submissions(self) -> list[tuple[str, str, str]]:
        """POSTs that are not a GraphQL query (Ashby loads its form with one)."""
        out = []
        for (method, host, path), body in zip([r for r in self.requests if r[0] not in ("GET", "HEAD")], self.bodies):
            if path.startswith("/api/non-user-graphql") and b'"query": "query' in body.replace(b'":"', b'": "'):
                continue
            out.append((method, host, path))
        return out

    async def resolver(self, host: str, port: int):
        """Guard-proxy resolver: the four fake hosts go to this server, everything else is refused."""
        if host in HOSTS:
            return ("127.0.0.1", self.tls_port if port == 443 else self.port)
        raise ValueError("not a test host")

    async def host_check(self, url: str):
        from urllib.parse import urlsplit
        if (urlsplit(url).hostname or "") not in HOSTS:
            raise ValueError("not a test host")
        return "127.0.0.1"

    def url(self, host_index: int, path: str) -> str:
        scheme = "https" if HOSTS[host_index] in ("jobs.ashbyhq.com", "apply.workable.com") else "http"
        return f"{scheme}://{HOSTS[host_index]}{path}"
