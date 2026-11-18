"""Salesforce client. STRICTLY READ-ONLY: Salesforce holds production data and this code must never change it.

How that is guaranteed, in layers:
- every REST call goes through `SalesforceClient._send`, which refuses any method other than GET and any path
  outside `/services/data/<API_VERSION>/`;
- the one POST that exists is the sign-in (`_sign_in`, a fixed function with a fixed URL and a fixed form);
- a hook on the HTTP client itself refuses every request that is neither a GET nor that one sign-in POST, so
  even code that bypassed `_send` could not write.

What is read: Person Accounts (the fields in ACCOUNT_FIELDS, nothing else), the candidate's resume records and the
file attached to the newest active one. Every query is built by `build_query` from an explicit field list that is
checked against READABLE_FIELDS; `FIELDS(ALL)` is never used. The Account object also holds identity documents,
bank details, passwords and protected personal characteristics: none of those fields is named anywhere in this
code, and a query naming a field outside the whitelist raises before anything is sent.

Every piece of text that enters a query is quoted by `soql_quote`.

Logs carry an operation name, a status code and Salesforce's error code. Never a query, a name, a token or a body."""
import asyncio
import base64
import binascii
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
import jwt

from ...config import Settings

log = logging.getLogger("jobfinder.salesforce")
# httpx logs every request URL at INFO. A query URL contains what the user typed (a person's name), so those lines
# must never be written, whatever the surrounding logging setup is.
logging.getLogger("httpx").setLevel(logging.WARNING)

API_VERSION = "v68.0"                                 # the one place the REST API version is named
API_ROOT = f"/services/data/{API_VERSION}/"
TOKEN_PATH = "/services/oauth2/token"
JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
ASSERTION_SECONDS = 170                               # Salesforce accepts an assertion for at most a few minutes
TOKEN_SECONDS = 30 * 60                               # the JWT flow does not say how long a token lives: renew well before any session timeout
TOKEN_RENEW_MARGIN = 60
STATUS_SECONDS = 60
MAX_CONCURRENCY = 4
MAX_JSON_BYTES = 2 * 1024 * 1024
SALESFORCE_HOSTS = (".salesforce.com", ".force.com", ".salesforce.mil", ".cloudforce.com")
RESUME_STATUSES = ("Uploaded", "Approved")            # what counts as a usable resume record
RESUME_EXTENSIONS = ("pdf", "docx")
PERSON_ACCOUNT = "Person Account"
ID_PATTERN = re.compile(r"[A-Za-z0-9]{18}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Everything this integration may ever read, per object. A query naming anything else is refused.
ACCOUNT_WHITELIST = frozenset({
    "Id", "Name", "FirstName", "LastName", "PersonEmail", "Phone", "Candidate_Status__c", "Technology__c", "Niche__c",
    "Niches__r.Name", "Preferred_Job_Positions__c", "Preferred_Job_Locations__c", "Preferred_Job_Type__c",
    "Total_Years_of_Experience__c", "Work_Authorization__c", "Current_Visa_Status__c", "Sponsorship_Required_For_VISA__c",
    "Authorized_To_Work_In_US__c", "Targeted_Experience_Level__c", "Salary_Expectations__c", "Open_to_relocate__c",
    "Relocation_Readiness__c", "Current_Address__StateCode__s", "Current_Address__City__s", "List_of_Certifications__c",
    "Assigned_Recruiter__r.Name", "Assigned_Recruiter__r.Email__c",
    "Assigned_Marketing_Team_Lead__r.Name", "Assigned_Marketing_Team_Lead__r.Email__c",
    "LastModifiedDate", "RecordType.Name"})
READABLE_FIELDS = {
    "Account": ACCOUNT_WHITELIST,
    "Resume__c": frozenset({"Id", "Candidate__c", "Is_Active__c", "Resume_Status__c", "CreatedDate"}),
    "ContentDocumentLink": frozenset({"LinkedEntityId", "ContentDocument.FileExtension", "ContentDocument.ContentSize",
                                      "ContentDocument.LatestPublishedVersionId", "ContentDocument.ContentModifiedDate"}),
}
# What a candidate search / import actually selects (a subset of the whitelist: only what the portal uses).
ACCOUNT_FIELDS = (
    "Id", "Name", "PersonEmail", "Phone", "Candidate_Status__c", "Technology__c", "Niche__c", "Niches__r.Name",
    "Preferred_Job_Positions__c", "Preferred_Job_Locations__c", "Preferred_Job_Type__c", "Total_Years_of_Experience__c",
    "Work_Authorization__c", "Current_Visa_Status__c", "Sponsorship_Required_For_VISA__c", "Targeted_Experience_Level__c",
    "Assigned_Recruiter__r.Name", "Assigned_Recruiter__r.Email__c",
    "Assigned_Marketing_Team_Lead__r.Name", "Assigned_Marketing_Team_Lead__r.Email__c",
    "LastModifiedDate")


# ---------------------------------------------------------------- errors
class SalesforceError(Exception):
    """Something the caller can be told about. `public` is the whole message: nothing from Salesforce is echoed."""
    status_code = 502
    public = "Salesforce answered with an error. Try again later."

    def __init__(self, public: str | None = None):
        super().__init__(public or self.public)
        if public:
            self.public = public


class NotConfigured(SalesforceError):
    status_code = 503
    public = "Salesforce is not configured on this server."


class Unavailable(SalesforceError):
    status_code = 503
    public = "Salesforce could not be reached. Try again in a moment."


class SignInFailed(SalesforceError):
    public = "Salesforce refused the sign-in of the integration user. An administrator has to check the Salesforce settings."


class FileTooLarge(SalesforceError):
    public = "The file in Salesforce is larger than the portal accepts."


class ReadOnlyViolation(RuntimeError):
    """A programming error: something tried to send Salesforce a request that is not a read."""


class FieldNotAllowed(RuntimeError):
    """A programming error: a query named a field outside the whitelist."""


# ---------------------------------------------------------------- queries
def soql_quote(value: str, *, like: bool = False) -> str:
    """`value` as a SOQL string literal, quotes included. Backslash and both quote characters are escaped and
    control characters are dropped, so the text can never end the literal. With `like=True` the LIKE wildcards
    `%` and `_` are escaped too: the text is matched as written."""
    if not isinstance(value, str):
        raise TypeError("only text is quoted")
    text = _CONTROL.sub("", value).replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
    if like:
        text = text.replace("%", "\\%").replace("_", "\\_")
    return f"'{text}'"


def soql_id(value: str) -> str:
    """A record id as a literal. Only 18 letters and digits pass."""
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError("not a Salesforce id")
    return f"'{value}'"


def _field(sobject: str, name: str) -> str:
    if name not in READABLE_FIELDS.get(sobject, ()):
        raise FieldNotAllowed(f"{sobject}.{name} is not on the list of fields this integration may read")
    return name


def where(sobject: str, field: str, operator: str, literal: str) -> str:
    """One condition. The field is checked against the whitelist; `literal` comes from soql_quote / soql_id."""
    if operator not in ("=", "LIKE", "IN"):
        raise ValueError("operator")
    return f"{_field(sobject, field)} {operator} {literal}"


def build_query(sobject: str, fields: tuple[str, ...], conditions: list[str], order_by: str = "", limit: int = 50) -> str:
    """The only place a SELECT is put together. Explicit, whitelisted fields; always a LIMIT."""
    select = ", ".join(_field(sobject, f) for f in fields)
    order = f" ORDER BY {_field(sobject, order_by.removesuffix(' DESC'))}{' DESC' if order_by.endswith(' DESC') else ''}" if order_by else ""
    return f"SELECT {select} FROM {sobject} WHERE {' AND '.join(conditions)}{order} LIMIT {max(1, min(int(limit), 200))}"


# ---------------------------------------------------------------- records
def _text(value, limit: int = 255) -> str:
    """Salesforce text as the portal stores it: control characters out (PostgreSQL cannot hold NUL), spaces tidy."""
    return " ".join(_CONTROL.sub(" ", value).split())[:limit] if isinstance(value, str) else ""


def _when(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class SfCandidate:
    """A Person Account, reduced to what the portal uses. Text exactly as written in Salesforce (tidied)."""
    id: str
    name: str = ""
    email: str = ""
    phone: str = ""
    candidate_status: str = ""
    technology: str = ""
    niche: str = ""
    preferred_positions: str = ""
    preferred_locations: str = ""
    preferred_job_type: str = ""
    years_experience: float | None = None
    work_authorization: str = ""
    visa_status: str = ""
    sponsorship_required: bool | None = None
    experience_level: str = ""
    assigned_recruiter: str = ""
    assigned_recruiter_email: str = ""                # lower case; matched against the portal's members
    marketing_team_lead: str = ""
    marketing_team_lead_email: str = ""
    last_modified: datetime | None = None


@dataclass(frozen=True)
class SfResume:
    """The file attached to a candidate's newest active resume record."""
    content_version_id: str
    extension: str                                    # lower case, without the dot
    size: int
    modified_at: datetime | None = None


def parse_account(record: dict) -> SfCandidate:
    def related(key: str, field: str, limit: int = 255) -> str:
        return _text((record.get(key) or {}).get(field), limit)

    years = record.get("Total_Years_of_Experience__c")
    sponsorship = record.get("Sponsorship_Required_For_VISA__c")
    return SfCandidate(
        id=str(record.get("Id") or ""), name=_text(record.get("Name"), 160), email=_text(record.get("PersonEmail"), 320),
        phone=_text(record.get("Phone"), 40), candidate_status=_text(record.get("Candidate_Status__c")),
        technology=_text(record.get("Technology__c")), niche=related("Niches__r", "Name") or _text(record.get("Niche__c")),
        preferred_positions=_text(record.get("Preferred_Job_Positions__c")),
        preferred_locations=_text(record.get("Preferred_Job_Locations__c")),
        preferred_job_type=_text(record.get("Preferred_Job_Type__c")),
        years_experience=float(years) if isinstance(years, (int, float)) and not isinstance(years, bool) else None,
        work_authorization=_text(record.get("Work_Authorization__c")), visa_status=_text(record.get("Current_Visa_Status__c")),
        sponsorship_required=sponsorship if isinstance(sponsorship, bool) else None,
        experience_level=_text(record.get("Targeted_Experience_Level__c")),
        assigned_recruiter=related("Assigned_Recruiter__r", "Name"),
        assigned_recruiter_email=related("Assigned_Recruiter__r", "Email__c", 320).lower(),
        marketing_team_lead=related("Assigned_Marketing_Team_Lead__r", "Name"),
        marketing_team_lead_email=related("Assigned_Marketing_Team_Lead__r", "Email__c").lower(),
        last_modified=_when(record.get("LastModifiedDate")))


def file_kind(data: bytes) -> str | None:
    """What the bytes really are, from their first bytes: "pdf", "docx" (a ZIP container) or None."""
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        return "docx"
    return None


# ---------------------------------------------------------------- client
class SalesforceClient:
    """One per process (app.state.salesforce). Holds the access token in memory only."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.login_url = settings.sf_login_url.strip().rstrip("/")
        self._token_url = self.login_url + TOKEN_PATH
        self._http = httpx.AsyncClient(transport=transport, follow_redirects=False, timeout=httpx.Timeout(20.0, connect=10.0),
                                       event_hooks={"request": [self._refuse_writes]})
        self._slots = asyncio.Semaphore(MAX_CONCURRENCY)
        self._sign_in_lock = asyncio.Lock()
        self._access_token = ""
        self._instance_url = ""
        self._token_until = 0.0
        self._status: tuple[float, dict] | None = None

    # ---- configuration
    @property
    def configured(self) -> bool:
        return self.settings.salesforce_configured

    @property
    def instance_url(self) -> str:
        """Known after the first sign-in ("" before)."""
        return self._instance_url

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- the read-only guard
    async def _refuse_writes(self, request: httpx.Request) -> None:
        """Runs inside the HTTP client for every request, whoever built it."""
        if request.method == "GET":
            return
        if request.method == "POST" and str(request.url) == self._token_url:
            return
        raise ReadOnlyViolation(f"Salesforce is read-only: a {request.method} request was refused")

    # ---- sign-in: the one POST
    def _assertion(self) -> str:
        try:
            key = base64.b64decode(self.settings.sf_private_key_b64, validate=True)
        except (binascii.Error, ValueError):
            raise SignInFailed("The Salesforce private key on this server is not valid base64.") from None
        claims = {"iss": self.settings.sf_client_id, "sub": self.settings.sf_username, "aud": self.login_url,
                  "exp": int(time.time()) + ASSERTION_SECONDS}
        try:
            return jwt.encode(claims, key, algorithm="RS256")
        except Exception:                               # a key that is not an RSA private key in PEM
            raise SignInFailed("The Salesforce private key on this server cannot be used to sign in.") from None

    async def _sign_in(self) -> None:
        """POST /services/oauth2/token with a signed assertion. Fixed URL, fixed form: nothing else is ever posted."""
        if not self.configured:
            raise NotConfigured()
        if urlsplit(self.login_url).scheme != "https":
            raise SignInFailed("The Salesforce login URL on this server must start with https://.")
        form = {"grant_type": JWT_GRANT, "assertion": self._assertion()}
        try:
            response = await self._http.post(self._token_url, data=form)
        except httpx.HTTPError as exc:
            log.warning("salesforce sign-in could not be sent", extra={"problem": type(exc).__name__})
            raise Unavailable() from None
        if response.status_code != 200:
            log.warning("salesforce sign-in refused", extra={"status": response.status_code, "code": _error_code(response.content, "error")})
            raise SignInFailed() if response.status_code < 500 else Unavailable()
        try:
            body = response.json()
            token, instance = str(body["access_token"]), str(body["instance_url"]).rstrip("/")
        except (ValueError, KeyError, TypeError):
            raise SignInFailed() from None
        host = urlsplit(instance)
        if host.scheme != "https" or not (host.hostname or "").endswith(SALESFORCE_HOSTS):
            log.warning("salesforce sign-in named an unexpected instance address")
            raise SignInFailed()                        # the token is only ever sent to a Salesforce host
        lifetime = body.get("expires_in")
        lifetime = float(lifetime) if isinstance(lifetime, (int, float)) and lifetime > 0 else TOKEN_SECONDS
        self._access_token, self._instance_url = token, instance
        self._token_until = time.monotonic() + max(30.0, lifetime - TOKEN_RENEW_MARGIN)

    async def _session(self, stale: str = "") -> tuple[str, str]:
        """(access token, instance URL), signing in when there is no token, it is about to expire, or Salesforce
        just refused `stale`."""
        async with self._sign_in_lock:
            if not self._access_token or time.monotonic() >= self._token_until or self._access_token == stale:
                self._access_token = ""
                await self._sign_in()
            return self._access_token, self._instance_url

    # ---- the single low-level call
    async def _send(self, method: str, path: str, params: dict | None = None, *, max_bytes: int = MAX_JSON_BYTES,
                    operation: str = "read") -> bytes:
        """Every REST call goes through here. GET only, REST data API only, at most `max_bytes` of answer.
        A 401 renews the token once and repeats the request."""
        if method != "GET":
            raise ReadOnlyViolation(f"Salesforce is read-only: {method} is never sent")
        if not path.startswith(API_ROOT) or ".." in path or "?" in path:
            raise ReadOnlyViolation("Only the Salesforce REST data API is read")
        if not self.configured:
            raise NotConfigured()
        started = time.perf_counter()
        async with self._slots:
            token = ""
            for attempt in (1, 2):
                token, instance = await self._session(stale=token)
                try:
                    async with self._http.stream("GET", instance + path, params=params,
                                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}) as response:
                        status = response.status_code
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                if status == 200:
                                    raise FileTooLarge()
                                break
                            chunks.append(chunk)
                except httpx.HTTPError as exc:
                    log.warning("salesforce request failed", extra={"operation": operation, "problem": type(exc).__name__})
                    raise Unavailable() from None
                if status == 401 and attempt == 1:
                    continue                            # the token expired or was revoked: sign in again, once
                body = b"".join(chunks)
                log.info("salesforce request", extra={"operation": operation, "status": status,
                                                      "duration_ms": round((time.perf_counter() - started) * 1000, 1)})
                if status == 200:
                    return body
                code = _error_code(body, "errorCode")
                log.warning("salesforce answered with an error", extra={"operation": operation, "status": status, "code": code})
                if status == 401:
                    raise SignInFailed()
                if status in (429, 503) or status >= 500 or code == "REQUEST_LIMIT_EXCEEDED":
                    raise Unavailable("Salesforce is busy or has reached its request limit. Try again in a moment.")
                raise SalesforceError()
        raise SalesforceError()                          # not reached

    async def _json(self, path: str, params: dict | None = None, operation: str = "read"):
        body = await self._send("GET", path, params, operation=operation)
        try:
            return json.loads(body)
        except ValueError:
            raise SalesforceError() from None

    async def _query(self, soql: str, operation: str) -> list[dict]:
        data = await self._json(API_ROOT + "query", {"q": soql}, operation)
        records = data.get("records") if isinstance(data, dict) else None
        return [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []

    # ---- what the portal asks
    async def status(self) -> dict:
        """{"configured", "connected", "message", "checked_at"}. A cheap call (the API's resource list), remembered
        for a minute. Never raises."""
        if not self.configured:
            return {"configured": False, "connected": None, "message": "Salesforce is not configured on this server.", "checked_at": None}
        if self._status and time.monotonic() < self._status[0]:
            return self._status[1]
        try:
            await self._send("GET", API_ROOT, operation="status")
            connected, message = True, "Connected"
        except SalesforceError as exc:
            connected, message = False, exc.public
        result = {"configured": True, "connected": connected, "message": message, "checked_at": datetime.now(timezone.utc)}
        self._status = (time.monotonic() + STATUS_SECONDS, result)
        return result

    def _person_accounts(self) -> list[str]:
        return [where("Account", "RecordType.Name", "=", soql_quote(PERSON_ACCOUNT))]

    async def search(self, text: str, *, active_only: bool = True, limit: int = 10, team_lead_email: str | None = None) -> list[SfCandidate]:
        """Person Accounts whose name, e-mail or phone contains `text`, by name. With `team_lead_email`, only those
        whose marketing team lead has that e-mail address."""
        pattern = soql_quote(text, like=True)
        pattern = f"'%{pattern[1:-1]}%'"
        conditions = self._person_accounts()
        conditions.append("(" + " OR ".join(where("Account", f, "LIKE", pattern) for f in ("Name", "PersonEmail", "Phone")) + ")")
        if active_only:
            conditions.append(where("Account", "Candidate_Status__c", "=", soql_quote("Active")))
        if team_lead_email is not None:
            conditions.append(where("Account", "Assigned_Marketing_Team_Lead__r.Email__c", "=", soql_quote(team_lead_email)))
        records = await self._query(build_query("Account", ACCOUNT_FIELDS, conditions, "Name", limit), "search")
        return [c for c in map(parse_account, records) if ID_PATTERN.fullmatch(c.id)]

    async def get_candidate(self, salesforce_id: str) -> SfCandidate | None:
        conditions = [where("Account", "Id", "=", soql_id(salesforce_id)), *self._person_accounts()]
        records = await self._query(build_query("Account", ACCOUNT_FIELDS, conditions, limit=1), "candidate")
        candidate = parse_account(records[0]) if records else None
        return candidate if candidate and ID_PATTERN.fullmatch(candidate.id) else None

    def _usable_resumes(self) -> list[str]:
        return [f"{_field('Resume__c', 'Is_Active__c')} = true",
                where("Resume__c", "Resume_Status__c", "IN", "(" + ", ".join(soql_quote(s) for s in RESUME_STATUSES) + ")")]

    async def with_resume(self, salesforce_ids: list[str]) -> set[str]:
        """Of these candidates, those that have an active resume record with a usable status."""
        ids = [i for i in dict.fromkeys(salesforce_ids) if ID_PATTERN.fullmatch(i)][:100]
        if not ids:
            return set()
        conditions = [where("Resume__c", "Candidate__c", "IN", "(" + ", ".join(soql_id(i) for i in ids) + ")"), *self._usable_resumes()]
        records = await self._query(build_query("Resume__c", ("Candidate__c",), conditions, limit=200), "resume_flags")
        return {str(r.get("Candidate__c")) for r in records}

    async def resume_of(self, salesforce_id: str) -> SfResume | None:
        """The file of the newest active resume record (status Uploaded or Approved) that has a file attached.
        When a record carries several files, the one modified last."""
        conditions = [where("Resume__c", "Candidate__c", "=", soql_id(salesforce_id)), *self._usable_resumes()]
        resumes = await self._query(build_query("Resume__c", ("Id", "CreatedDate"), conditions, "CreatedDate DESC", 5), "resume_records")
        ids = [str(r.get("Id")) for r in resumes if ID_PATTERN.fullmatch(str(r.get("Id") or ""))]
        if not ids:
            return None
        fields = ("LinkedEntityId", "ContentDocument.FileExtension", "ContentDocument.ContentSize",
                  "ContentDocument.LatestPublishedVersionId", "ContentDocument.ContentModifiedDate")
        links = await self._query(build_query("ContentDocumentLink", fields, [
            where("ContentDocumentLink", "LinkedEntityId", "IN", "(" + ", ".join(soql_id(i) for i in ids) + ")")], limit=50), "resume_files")
        for resume_id in ids:                           # newest resume record first
            files = []
            for link in links:
                document = link.get("ContentDocument") or {}
                version = str(document.get("LatestPublishedVersionId") or "")
                if link.get("LinkedEntityId") == resume_id and ID_PATTERN.fullmatch(version):
                    size = document.get("ContentSize")
                    files.append(SfResume(content_version_id=version, extension=_text(document.get("FileExtension"), 20).lower(),
                                          size=int(size) if isinstance(size, int) else 0, modified_at=_when(document.get("ContentModifiedDate"))))
            if files:
                return max(files, key=lambda f: f.modified_at or datetime.min.replace(tzinfo=timezone.utc))
        return None

    async def download(self, resume: SfResume, max_bytes: int) -> bytes:
        """The file's bytes. Raises FileTooLarge as soon as more than `max_bytes` arrive."""
        return await self._send("GET", f"{API_ROOT}sobjects/ContentVersion/{soql_id(resume.content_version_id)[1:-1]}/VersionData",
                                max_bytes=max_bytes, operation="resume_download")


def _error_code(body: bytes, key: str) -> str:
    """Salesforce's error code (for example INVALID_SESSION_ID) when the body has one. Codes only: the message next
    to it can quote the query, so it is never read."""
    try:
        data = json.loads(body[:4096])
    except ValueError:
        return ""
    item = data[0] if isinstance(data, list) and data else data
    code = item.get(key) if isinstance(item, dict) else None
    return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z_]{1,60}", code) else ""
