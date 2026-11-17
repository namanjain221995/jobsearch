"""A synthetic Salesforce org behind httpx.MockTransport, for the real client. No network, no real people.

It answers the sign-in POST, the REST resource list, SOQL queries (a tiny reader that understands exactly the
queries the client builds) and file downloads, and it records every request so tests can assert on what was sent."""
import base64
import json
import re
import urllib.parse
from dataclasses import dataclass, field

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.portal.services.salesforce import SalesforceClient
from tests.salesforce_samples import make_docx, make_pdf

LOGIN = "https://login.salesforce.com"
INSTANCE = "https://example.my.salesforce.com"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
PUBLIC_KEY = _KEY.public_key()


def sf_id(prefix: str, n: int) -> str:
    return f"{prefix}000000000{n:03d}AAA"


def account(n: int, name: str, **fields) -> dict:
    first = name.split()[0].lower()
    record = {"Id": sf_id("001", n), "Name": name, "PersonEmail": f"{first}.{n}@example.com", "Phone": f"(555) 010-{2000 + n}",
              "Candidate_Status__c": "Active", "Technology__c": "AI/ML Engineer", "Niche__c": "AI ML Engineer", "Niches__r": {"Name": "AI ML Engineer"},
              "Preferred_Job_Positions__c": None, "Preferred_Job_Locations__c": "Texas, California", "Preferred_Job_Type__c": "Remote",
              "Total_Years_of_Experience__c": 4.0, "Work_Authorization__c": "OPT", "Current_Visa_Status__c": "STEM OPT Extension",
              "Sponsorship_Required_For_VISA__c": False, "Targeted_Experience_Level__c": "Mid-Senior_level",
              "Assigned_Recruiter__r": {"Name": "Rita Recruiter"},
              "Assigned_Marketing_Team_Lead__r": {"Name": "Lead Alpha", "Email__c": "TL.Alpha@Example.com"},
              "LastModifiedDate": "2026-11-01T10:00:00.000+0000", "RecordType": "Person Account"}
    record.update(fields)
    return record


@dataclass
class Org:
    accounts: dict = field(default_factory=dict)          # id -> record
    resumes: list = field(default_factory=list)           # {"Id", "Candidate__c", "Is_Active__c", "Resume_Status__c", "CreatedDate"}
    files: dict = field(default_factory=dict)             # resume id -> [{"version", "ext", "data", "modified", "size"}]
    requests: list = field(default_factory=list)          # (method, path, soql or "")
    assertions: list = field(default_factory=list)        # the JWT of every sign-in
    tokens: int = 0
    revoke_next: int = 0                                  # answer this many data requests with 401
    fail_with: int = 0                                    # answer every data request with this status
    fail_downloads: bool = False
    refuse_sign_in: bool = False

    def add(self, n: int, name: str, **fields) -> str:
        record = account(n, name, **fields)
        self.accounts[record["Id"]] = record
        return record["Id"]

    def add_resume(self, account_id: str, n: int, *, ext: str = "pdf", data: bytes | None = None, status: str = "Uploaded", active: bool = True,
                   created: str = "2026-10-01T09:00:00.000+0000", modified: str = "2026-10-01T09:00:00.000+0000", size: int | None = None,
                   with_file: bool = True) -> str:
        """Returns the ContentVersion id of the attached file."""
        resume_id, version = sf_id("a0R", n), sf_id("068", n)
        name = self.accounts[account_id]["Name"]
        self.resumes.append({"Id": resume_id, "Candidate__c": account_id, "Is_Active__c": active, "Resume_Status__c": status, "CreatedDate": created})
        if with_file:
            if data is None:
                data = make_pdf(name) if ext == "pdf" else make_docx(name)
            self.files.setdefault(resume_id, []).append({"version": version, "ext": ext, "data": data, "modified": modified,
                                                         "size": len(data) if size is None else size})
        return version

    @property
    def queries(self) -> list[str]:
        return [q for _, _, q in self.requests if q]

    # ---- HTTP
    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if request.method == "POST" and str(url) == LOGIN + "/services/oauth2/token":
            form = urllib.parse.parse_qs(request.content.decode())
            self.requests.append(("POST", url.path, ""))
            self.assertions.append((form["grant_type"][0], form["assertion"][0]))
            if self.refuse_sign_in:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "user hasn't approved this consumer"})
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"token-{self.tokens}", "instance_url": INSTANCE, "token_type": "Bearer"})
        soql = url.params.get("q", "")
        self.requests.append((request.method, url.path, soql))
        if request.method != "GET" or url.host != "example.my.salesforce.com":
            return httpx.Response(405, json=[{"errorCode": "METHOD_NOT_ALLOWED", "message": "the test org is read-only"}])
        if request.headers.get("authorization") != f"Bearer token-{self.tokens}" or self.revoke_next:
            self.revoke_next = max(0, self.revoke_next - 1)
            return httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "Session expired or invalid"}])
        if self.fail_with:
            return httpx.Response(self.fail_with, json=[{"errorCode": "SERVER_UNAVAILABLE", "message": "secret detail: Zed Zebra zed@example.com"}])
        if url.path == "/services/data/v68.0/":
            return httpx.Response(200, json={"sobjects": "/services/data/v68.0/sobjects"})
        if url.path == "/services/data/v68.0/query":
            return httpx.Response(200, json={"totalSize": 0, "done": True, "records": self.query(soql)})
        match = re.fullmatch(r"/services/data/v68\.0/sobjects/ContentVersion/(\w{18})/VersionData", url.path)
        if match:
            if self.fail_downloads:
                return httpx.Response(500, json=[{"errorCode": "UNKNOWN_EXCEPTION", "message": "x"}])
            for files in self.files.values():
                for f in files:
                    if f["version"] == match.group(1):
                        return httpx.Response(200, content=f["data"], headers={"content-type": "application/octet-stream"})
        return httpx.Response(404, json=[{"errorCode": "NOT_FOUND", "message": "x"}])

    # ---- a reader for exactly the queries the client builds
    def query(self, soql: str) -> list[dict]:
        sobject = re.search(r" FROM (\w+) WHERE ", soql).group(1)
        limit = int(re.search(r" LIMIT (\d+)$", soql).group(1))
        ids = re.findall(r"'(\w{18})'", soql)
        if sobject == "Account":
            rows = [a for a in self.accounts.values() if a["RecordType"] == "Person Account"]
            if " Id = '" in soql:
                rows = [a for a in rows if a["Id"] in ids]
            like = re.search(r"Name LIKE '%(.*?)%' OR ", soql)
            if like:
                text = re.sub(r"\\(.)", r"\1", like.group(1)).lower()
                rows = [a for a in rows if any(text in (a[f] or "").lower() for f in ("Name", "PersonEmail", "Phone"))]
            if "Candidate_Status__c = 'Active'" in soql:
                rows = [a for a in rows if a["Candidate_Status__c"] == "Active"]
            lead = re.search(r"Assigned_Marketing_Team_Lead__r\.Email__c = '(.*?)'", soql)
            if lead:
                rows = [a for a in rows if ((a["Assigned_Marketing_Team_Lead__r"] or {}).get("Email__c") or "").lower() == lead.group(1).lower()]
            rows = sorted(rows, key=lambda a: a["Name"])[:limit]
            return [{k: v for k, v in a.items() if k != "RecordType"} for a in rows]
        if sobject == "Resume__c":
            rows = [r for r in self.resumes if r["Candidate__c"] in ids and r["Is_Active__c"] and r["Resume_Status__c"] in ("Uploaded", "Approved")]
            return sorted(rows, key=lambda r: r["CreatedDate"], reverse=True)[:limit]
        if sobject == "ContentDocumentLink":
            return [{"LinkedEntityId": rid, "ContentDocument": {"FileExtension": f["ext"], "ContentSize": f["size"],
                                                                 "LatestPublishedVersionId": f["version"], "ContentModifiedDate": f["modified"]}}
                    for rid in ids for f in self.files.get(rid, [])][:limit]
        raise AssertionError(f"unexpected object {sobject}")


def sf_settings(**overrides) -> Settings:
    values = {"sf_client_id": "consumer-key-123", "sf_username": "integration@example.com", "sf_login_url": LOGIN + "/",
              "sf_private_key_b64": base64.b64encode(PRIVATE_PEM).decode(), **overrides}
    return Settings(_env_file=None, **values)


def make_client(org: Org, **overrides) -> SalesforceClient:
    return SalesforceClient(sf_settings(**overrides), transport=httpx.MockTransport(org.handler))


def claims(assertion: str) -> dict:
    """The assertion verified with the public key, as Salesforce would."""
    return jwt.decode(assertion, PUBLIC_KEY, algorithms=["RS256"], audience=LOGIN)


def standard_org() -> Org:
    """Eight Person Accounts and one business account. Team leads: tl.alpha@ (written in mixed case) and tl.beta@."""
    org = Org()
    beta = {"Name": "Lead Beta", "Email__c": "tl.beta@example.com"}
    org.add(1, "Ana Example")
    org.add(2, "Anabel Sample", Assigned_Marketing_Team_Lead__r=beta, Preferred_Job_Type__c="All of the above")
    org.add(3, "Andre Docx", Technology__c=None, Niches__r={"Name": "Product Manager"})
    org.add(4, "Anna Noresume")
    org.add(5, "Anton Hold", Candidate_Status__c="Hold")
    org.add(6, "Anya Bigfile")
    org.add(7, "Anselm Wrongtype")
    org.add(8, "Annika Twofiles")
    org.add(9, "Ana Business Inc", RecordType="Business Account")
    org.add_resume(sf_id("001", 1), 1)
    org.add_resume(sf_id("001", 2), 2)
    org.add_resume(sf_id("001", 3), 3, ext="docx", status="Approved")
    org.add_resume(sf_id("001", 6), 6, size=11 * 1024 * 1024)
    org.add_resume(sf_id("001", 7), 7, ext="png", data=b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    org.add_resume(sf_id("001", 8), 81, created="2026-09-01T09:00:00.000+0000")                       # older record
    org.add_resume(sf_id("001", 8), 82, ext="docx", created="2026-10-05T09:00:00.000+0000")           # the newest active one
    org.add_resume(sf_id("001", 8), 83, created="2026-10-20T09:00:00.000+0000", status="Draft")       # newer, but not usable
    org.add_resume(sf_id("001", 8), 84, created="2026-10-25T09:00:00.000+0000", active=False)         # newer, but inactive
    return org


def install(world, org: Org) -> SalesforceClient:
    """Give the test application the real client, talking to this synthetic org."""
    client = make_client(org)
    world.app.state.salesforce = client
    world.app.state.salesforce_cache.clear()
    return client
