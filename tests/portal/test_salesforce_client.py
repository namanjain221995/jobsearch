"""The Salesforce client itself, against a synthetic org behind httpx.MockTransport: sign-in, the read-only guard,
quoting, the field whitelist, errors, logs and the mapping into the portal profile. No network, no database."""
import inspect
import logging
import re
import time

import httpx
import pytest

from app.config import Settings
from app.portal.services import salesforce as sf
from app.portal.services import salesforce_sync as sync
from tests.portal.sf_org import INSTANCE, LOGIN, Org, claims, make_client, sf_id, standard_org

# The complete list of Account fields this integration may read. Adding one here is a decision, not a refactoring.
ALLOWED_ACCOUNT_FIELDS = {
    "Id", "Name", "FirstName", "LastName", "PersonEmail", "Phone", "Candidate_Status__c", "Technology__c", "Niche__c", "Niches__r.Name",
    "Preferred_Job_Positions__c", "Preferred_Job_Locations__c", "Preferred_Job_Type__c", "Total_Years_of_Experience__c",
    "Work_Authorization__c", "Current_Visa_Status__c", "Sponsorship_Required_For_VISA__c", "Authorized_To_Work_In_US__c",
    "Targeted_Experience_Level__c", "Salary_Expectations__c", "Open_to_relocate__c", "Relocation_Readiness__c",
    "Current_Address__StateCode__s", "Current_Address__City__s", "List_of_Certifications__c", "Assigned_Recruiter__r.Name",
    "Assigned_Marketing_Team_Lead__r.Name", "Assigned_Marketing_Team_Lead__r.Email__c", "LastModifiedDate", "RecordType.Name"}
ALLOWED = {"Account": ALLOWED_ACCOUNT_FIELDS,
           "Resume__c": {"Id", "Candidate__c", "Is_Active__c", "Resume_Status__c", "CreatedDate"},
           "ContentDocumentLink": {"LinkedEntityId", "ContentDocument.FileExtension", "ContentDocument.ContentSize",
                                   "ContentDocument.LatestPublishedVersionId", "ContentDocument.ContentModifiedDate"}}
SOQL_WORDS = {"SELECT", "FROM", "WHERE", "AND", "OR", "LIKE", "IN", "ORDER", "BY", "DESC", "LIMIT", "true"}
# Kinds of data the Account object holds and this integration must never ask for.
NEVER = re.compile(r"ssn|social|passport|bank|routing|password|birth|dob|disab|race|ethnic|military|veteran|criminal|convict|gender", re.IGNORECASE)
HOSTILE = ["O'Brien", "x' OR Name LIKE '%", "\\", "\\' OR 1=1 --", "100%_sure", "a\nb\r\tc\x00d", "' AND Id != '", "\"quoted\"", "%", "_", "\\\\'",
           "x') OR (Name LIKE '", "zoë ' ünï"]


def literals(soql: str) -> list[str]:
    """The string literals of a query, unescaped, read the way Salesforce reads them. Fails on a stray quote."""
    out, i = [], 0
    while i < len(soql):
        if soql[i] != "'":
            i += 1
            continue
        i, text = i + 1, []
        while soql[i] != "'":
            if soql[i] == "\\":
                i += 1
            text.append(soql[i])
            i += 1
        out.append("".join(text))
        i += 1
    return out


def without_literals(soql: str) -> str:
    return re.sub(r"'(?:\\.|[^'\\])*'", "''", soql)


async def exercise(client: sf.SalesforceClient) -> None:
    """Every kind of read the client can make."""
    await client.status()
    for term in ("an", *HOSTILE):
        for active in (True, False):
            for lead in (None, "tl.alpha@example.com", "x' OR Name != '"):
                await client.search(term, active_only=active, limit=5, team_lead_email=lead)
    for n in range(1, 10):
        await client.get_candidate(sf_id("001", n))
        resume = await client.resume_of(sf_id("001", n))
        if resume and resume.size < 1024 * 1024:
            await client.download(resume, 1024 * 1024)
    await client.with_resume([sf_id("001", n) for n in range(1, 10)])


# ---------------------------------------------------------------- sign-in
async def test_sign_in_sends_a_signed_assertion_and_the_token_is_cached_and_renewed():
    org = standard_org()
    client = make_client(org)
    assert client.configured and client.instance_url == ""
    assert [c.name for c in await client.search("ana")] == ["Ana Example", "Anabel Sample"]
    assert org.requests[0] == ("POST", "/services/oauth2/token", "") and client.instance_url == INSTANCE
    grant, assertion = org.assertions[0]
    assert grant == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    sent = claims(assertion)                                           # verified with the public key, audience = the login URL
    assert sent["iss"] == "consumer-key-123" and sent["sub"] == "integration@example.com" and sent["aud"] == LOGIN
    assert 0 < sent["exp"] - time.time() <= 180
    assert __import__("jwt").get_unverified_header(assertion)["alg"] == "RS256"
    # Cached: further calls do not sign in again.
    await client.get_candidate(sf_id("001", 1))
    await client.status()
    assert org.tokens == 1
    # Salesforce refuses the token (expired, revoked): one new sign-in, the request is repeated, the caller notices nothing.
    org.revoke_next = 1
    assert (await client.get_candidate(sf_id("001", 1))).name == "Ana Example"
    assert org.tokens == 2
    # Shortly before the token's lifetime ends it is renewed without waiting for a 401.
    client._token_until = time.monotonic() - 1
    await client.get_candidate(sf_id("001", 1))
    assert org.tokens == 3
    # Still refused after a fresh sign-in: a clear error, not a loop.
    org.revoke_next = 2
    with pytest.raises(sf.SignInFailed):
        await client.get_candidate(sf_id("001", 1))
    assert org.tokens == 4
    await client.aclose()


async def test_sign_in_problems_are_plain_errors_and_never_echo_salesforce():
    org = standard_org()
    org.refuse_sign_in = True
    client = make_client(org)
    with pytest.raises(sf.SignInFailed) as refused:
        await client.search("ana")
    assert "approved" not in refused.value.public and refused.value.status_code == 502
    state = await client.status()
    assert state["configured"] is True and state["connected"] is False and "approved" not in state["message"]
    # Not configured: nothing is sent at all.
    empty = Org()
    off = sf.SalesforceClient(Settings(_env_file=None), transport=httpx.MockTransport(empty.handler))
    assert not off.configured and (await off.status()) == {"configured": False, "connected": None, "checked_at": None,
                                                           "message": "Salesforce is not configured on this server."}
    with pytest.raises(sf.NotConfigured) as missing:
        await off.search("ana")
    assert missing.value.status_code == 503 and empty.requests == []
    # A key that is not a key, a login URL without TLS, and an instance address that is not Salesforce.
    for overrides in ({"sf_private_key_b64": "not base64 !!"}, {"sf_private_key_b64": "aGVsbG8="}, {"sf_login_url": "http://login.salesforce.com"}):
        org2 = standard_org()
        with pytest.raises(sf.SignInFailed):
            await make_client(org2, **overrides).search("ana")
        assert org2.requests == []

    def elsewhere(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "t", "instance_url": "https://salesforce.example.org"})
        raise AssertionError("the token must not be sent to a host that is not Salesforce")

    from tests.portal.sf_org import sf_settings
    with pytest.raises(sf.SignInFailed):
        await sf.SalesforceClient(sf_settings(), transport=httpx.MockTransport(elsewhere)).search("ana")


# ---------------------------------------------------------------- read-only
async def test_only_get_requests_can_ever_reach_salesforce():
    org = standard_org()
    client = make_client(org)
    await client.search("ana")                                         # signed in, instance known
    before = len(org.requests)
    path = "/services/data/v68.0/sobjects/Account/" + sf_id("001", 1)
    for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "get"):
        with pytest.raises(sf.ReadOnlyViolation):
            await client._send(method, path)
    for outside in ("/services/oauth2/revoke", "/services/data/v1.0/query", "/services/apexrest/x", "/services/data/v68.0/../../oauth2/revoke",
                    "https://example.org/services/data/v68.0/query"):
        with pytest.raises(sf.ReadOnlyViolation):
            await client._send("GET", outside)
    # Even code that went around _send and used the HTTP client directly cannot write: the client itself refuses.
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(sf.ReadOnlyViolation):
            await client._http.request(method, INSTANCE + path, json={"Name": "changed"})
    with pytest.raises(sf.ReadOnlyViolation):
        await client._http.post(LOGIN + "/services/oauth2/revoke", data={"token": "x"})          # a POST, but not the sign-in
    with pytest.raises(sf.ReadOnlyViolation):
        await client._http.post(INSTANCE + "/services/oauth2/token", data={})
    assert len(org.requests) == before                                 # none of them left the process
    # Everything the client does in normal use: GET requests, and the sign-in POST.
    await exercise(client)
    org.revoke_next = 1
    await client.search("renew")
    sent = {(method, path) for method, path, _ in org.requests}
    assert {m for m, _ in sent} == {"GET", "POST"}
    assert {p for m, p in sent if m == "POST"} == {"/services/oauth2/token"}
    assert all(p.startswith("/services/data/v68.0/") for m, p in sent if m == "GET")
    # And in the source: one place posts (the sign-in), nothing names another write method.
    source = inspect.getsource(sf)
    assert source.count(".post(") == 1 and "self._http.post(self._token_url" in source
    assert not re.search(r"\.(put|patch|delete)\(|\.request\(|\"(PUT|PATCH|DELETE)\"", source)
    assert sf.API_VERSION == "v68.0" and source.count("v68.0") == 1    # the version is named once
    await client.aclose()


# ---------------------------------------------------------------- quoting
@pytest.mark.parametrize("value", HOSTILE)
def test_quoting_keeps_hostile_text_inside_one_literal(value):
    clean = re.sub(r"[\x00-\x1f\x7f]", "", value)
    quoted = sf.soql_quote(value)
    assert literals(f"SELECT Id FROM Account WHERE Name = {quoted} LIMIT 1") == [clean]     # one literal, exactly the text
    like = sf.soql_quote(value, like=True)
    assert literals(f"x LIKE {like}") == [clean.replace("%", "%").replace("_", "_")]
    # In a LIKE pattern the wildcards of the text are escaped: the only bare % are the two the client adds itself.
    assert not re.search(r"(?<!\\)(?:\\\\)*[%_]", like[1:-1])
    assert without_literals(f"A = {quoted} AND B LIKE {like}") == "A = '' AND B LIKE ''"


def test_quoting_refuses_what_is_not_text_and_ids_that_are_not_ids():
    for bad in (None, 5, b"x", ["x"]):
        with pytest.raises(TypeError):
            sf.soql_quote(bad)
    assert sf.soql_id(sf_id("001", 1)) == f"'{sf_id('001', 1)}'"
    for bad in ("", "001", "001000000000001AA'", "001000000000001AAA' OR Id != '", "001 00000000001AAA", None, "0010000000000001AAAA"):
        with pytest.raises(ValueError):
            sf.soql_id(bad)


async def test_hostile_search_text_reaches_salesforce_as_data():
    org = standard_org()
    org.add(20, "Miles O'Brien")
    org.add(21, "Percy 100%_sure")
    client = make_client(org)
    assert [c.name for c in await client.search("o'br")] == ["Miles O'Brien"]
    assert [c.name for c in await client.search("100%_s")] == ["Percy 100%_sure"]
    # % and _ are matched as written, not as wildcards: they find the one name that contains them, not everybody.
    assert [c.name for c in await client.search("%")] == ["Percy 100%_sure"] and [c.name for c in await client.search("_")] == ["Percy 100%_sure"]
    assert await client.search("a_a") == [] and await client.search("A%e") == []
    for value in HOSTILE:
        org.requests.clear()
        found = await client.search(value, team_lead_email=value)
        soql = org.queries[0]
        clean = re.sub(r"[\x00-\x1f\x7f]", "", value)
        assert literals(soql) == ["Person Account", f"%{clean}%", f"%{clean}%", f"%{clean}%", "Active", clean], value
        assert without_literals(soql).count("''") == 6 and " OR 1=1" not in without_literals(soql)
        assert all(value.lower() in c.name.lower() for c in found)
    await client.aclose()


# ---------------------------------------------------------------- the field whitelist
def test_the_whitelist_is_exactly_the_agreed_list():
    assert sf.READABLE_FIELDS == {k: frozenset(v) for k, v in ALLOWED.items()}
    assert set(sf.ACCOUNT_FIELDS) <= ALLOWED_ACCOUNT_FIELDS and len(set(sf.ACCOUNT_FIELDS)) == len(sf.ACCOUNT_FIELDS)
    assert not [f for fields in sf.READABLE_FIELDS.values() for f in fields if NEVER.search(f)]


async def test_no_query_names_a_field_outside_the_whitelist():
    org = standard_org()
    client = make_client(org)
    await exercise(client)
    assert len(org.queries) > 100 and {re.search(r" FROM (\w+) ", q).group(1) for q in org.queries} == set(ALLOWED)
    for soql in org.queries:
        bare = without_literals(soql)
        assert "FIELDS(" not in bare.upper() and "*" not in bare and not NEVER.search(bare), bare
        sobject = re.search(r" FROM (\w+) ", bare).group(1)
        named = set(re.findall(r"[A-Za-z_][\w.]*", bare)) - SOQL_WORDS - {sobject}
        assert named <= ALLOWED[sobject], (named - ALLOWED[sobject], bare)
        assert re.search(r" LIMIT \d+$", bare)
    # The same holds for every path that is read: the query endpoint, the resource list and file contents. No record
    # is ever fetched whole (GET /sobjects/Account/<id> would return every field).
    paths = {re.sub(r"\w{18}", "<id>", p) for m, p, _ in org.requests if m == "GET"}
    assert paths == {"/services/data/v68.0/", "/services/data/v68.0/query", "/services/data/v68.0/sobjects/ContentVersion/<id>/VersionData"}
    await client.aclose()


def test_a_query_with_a_field_outside_the_whitelist_cannot_be_built():
    for sobject, name in (("Account", "SSN__c"), ("Account", "Date_of_Birth__c"), ("Account", "FIELDS(ALL)"), ("Account", "Id, Passport__c"),
                          ("Contact", "Id"), ("Resume__c", "Lead__c"), ("ContentDocumentLink", "ContentDocument.Title")):
        with pytest.raises(sf.FieldNotAllowed):
            sf.build_query(sobject, (name,), ["Id = ''"])
        with pytest.raises(sf.FieldNotAllowed):
            sf.where(sobject, name, "=", "''")
    with pytest.raises(sf.FieldNotAllowed):
        sf.build_query("Account", ("Id",), ["Id = ''"], order_by="Bank_Account__c")
    assert sf.build_query("Account", ("Id", "Name"), ["Id = ''"], "Name", 5000) == "SELECT Id, Name FROM Account WHERE Id = '' ORDER BY Name LIMIT 200"


# ---------------------------------------------------------------- reading
async def test_candidates_resumes_and_files_are_read_as_documented():
    org = standard_org()
    client = make_client(org)
    ana = await client.get_candidate(sf_id("001", 1))
    assert (ana.name, ana.email, ana.phone, ana.candidate_status) == ("Ana Example", "ana.1@example.com", "(555) 010-2001", "Active")
    assert (ana.technology, ana.niche, ana.years_experience, ana.sponsorship_required) == ("AI/ML Engineer", "AI ML Engineer", 4.0, False)
    assert (ana.assigned_recruiter, ana.marketing_team_lead, ana.marketing_team_lead_email) == ("Rita Recruiter", "Lead Alpha", "tl.alpha@example.com")
    assert ana.last_modified.isoformat() == "2026-11-01T10:00:00+00:00"
    assert await client.get_candidate(sf_id("001", 9)) is None            # a business account is not a candidate
    assert await client.get_candidate(sf_id("001", 99)) is None
    # Search: Person Accounts only, active only unless asked, by name; e-mail and phone match too; the limit holds.
    assert [c.name for c in await client.search("an")] == ["Ana Example", "Anabel Sample", "Andre Docx", "Anna Noresume", "Annika Twofiles",
                                                           "Anselm Wrongtype", "Anya Bigfile"]
    assert "Anton Hold" in [c.name for c in await client.search("an", active_only=False)]
    assert [c.name for c in await client.search("ANDRE.3@EXAMPLE")] == ["Andre Docx"] and [c.name for c in await client.search("010-2004")] == ["Anna Noresume"]
    assert len(await client.search("an", limit=3)) == 3
    assert [c.name for c in await client.search("an", team_lead_email="tl.beta@example.com")] == ["Anabel Sample"]
    # Resume flags and the file of the newest active resume with a usable status.
    assert await client.with_resume([sf_id("001", n) for n in range(1, 9)]) == {sf_id("001", n) for n in (1, 2, 3, 6, 7, 8)}
    assert await client.with_resume([]) == set() and await client.resume_of(sf_id("001", 4)) is None
    pdf = await client.resume_of(sf_id("001", 1))
    assert (pdf.content_version_id, pdf.extension) == (sf_id("068", 1), "pdf") and pdf.modified_at.year == 2026
    data = await client.download(pdf, 1024 * 1024)
    assert len(data) == pdf.size and sf.file_kind(data) == "pdf"
    newest = await client.resume_of(sf_id("001", 8))                       # not the draft, not the inactive one, not the older one
    assert (newest.content_version_id, newest.extension) == (sf_id("068", 82), "docx")
    assert sf.file_kind(await client.download(newest, 1024 * 1024)) == "docx" and sf.file_kind(b"\x89PNG....") is None
    with pytest.raises(sf.FileTooLarge):                                   # cut off while reading, whatever the declared size said
        await client.download(pdf, 100)
    await client.aclose()


async def test_errors_are_mapped_and_nothing_personal_is_logged():
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(" ".join(f"{k}={v}" for k, v in record.__dict__.items()) + " " + record.getMessage())

    handler, logger = Keep(), logging.getLogger("jobfinder.salesforce")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        org = standard_org()
        client = make_client(org)
        await exercise(client)
        state = await client.status()
        assert state["connected"] is True and state["message"] == "Connected"
        calls = len(org.requests)
        assert await client.status() == state and len(org.requests) == calls             # remembered for a minute
        for status, kind, code in ((500, sf.Unavailable, 503), (503, sf.Unavailable, 503), (429, sf.Unavailable, 503), (400, sf.SalesforceError, 502),
                                   (403, sf.SalesforceError, 502), (404, sf.SalesforceError, 502)):
            org.fail_with = status
            with pytest.raises(kind) as problem:
                await client.search("ana")
            assert problem.value.status_code == code and "Zed" not in problem.value.public and "zed@" not in str(problem.value)
        org.fail_with = 0

        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("no route")

        from tests.portal.sf_org import sf_settings
        unreachable = sf.SalesforceClient(sf_settings(), transport=httpx.MockTransport(down))
        with pytest.raises(sf.Unavailable):
            await unreachable.search("ana")
        assert (await unreachable.status())["connected"] is False
        await client.aclose()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    text = "\n".join(records)
    assert len(records) > 50 and "operation=search" in text and "status=200" in text and "code=SERVER_UNAVAILABLE" in text
    for secret in ("token-1", "Bearer", "eyJ", "Ana", "ana.1@", "example.com", "(555)", "SELECT", "O'Brien", "Zed", "consumer-key", "PRIVATE KEY"):
        assert secret not in text, secret
    assert logging.getLogger("httpx").level >= logging.WARNING             # httpx would log every query URL at INFO


# ---------------------------------------------------------------- mapping
def person(**fields) -> sf.SfCandidate:
    return sf.SfCandidate(id=sf_id("001", 1), name="Ana Example", **fields)


@pytest.mark.parametrize("fields, expected", [
    # desired titles: positions, else technology, else niche; placeholders are not titles
    ({"preferred_positions": "Data Engineer, ML Engineer; Data Engineer", "technology": "Java Developer"}, {"desired_titles": ["Data Engineer", "ML Engineer"]}),
    ({"preferred_positions": "NA", "technology": "AI/ML Engineer", "niche": "AI ML Engineer"}, {"desired_titles": ["AI/ML Engineer"]}),
    ({"preferred_positions": "", "technology": "", "niche": "Product Manager"}, {"desired_titles": ["Product Manager"]}),
    ({"technology": "", "niche": "Other"}, {"desired_titles": []}),
    ({}, {"desired_titles": [], "years_experience": None, "work_mode": None, "work_authorization": None, "sponsorship_needed": None,
          "seniority": None, "preferred_states": []}),
    # years
    ({"years_experience": 4.26}, {"years_experience": 4.3}), ({"years_experience": 0.0}, {"years_experience": 0.0}),
    ({"years_experience": -1.0}, {"years_experience": None}), ({"years_experience": 75.0}, {"years_experience": None}),
    # work mode
    ({"preferred_job_type": "Remote"}, {"work_mode": "Remote"}), ({"preferred_job_type": "Hybrid"}, {"work_mode": "Hybrid"}),
    ({"preferred_job_type": "On-site"}, {"work_mode": "On-site"}), ({"preferred_job_type": "All of the above"}, {"work_mode": "All"}),
    ({"preferred_job_type": "Remote, Hybrid"}, {"work_mode": "All"}), ({"preferred_job_type": "Remote, Hybrid, On-site"}, {"work_mode": "All"}),
    ({"preferred_job_type": "Weekends"}, {"work_mode": None}),
    # work authorization: the visa status first, else the authorization; what has no portal option is "Other", never a guess
    ({"visa_status": "US Citizen", "work_authorization": "OPT"}, {"work_authorization": "US Citizen"}),
    ({"visa_status": "Green Card / Permanent Resident"}, {"work_authorization": "Green Card / US Permanent Resident"}),
    ({"visa_status": "Green Card EAD"}, {"work_authorization": "Employment Authorization Document (EAD)"}),
    ({"visa_status": "OPT"}, {"work_authorization": "Employment Authorization Document (EAD)"}),
    ({"visa_status": "STEM OPT Extension"}, {"work_authorization": "Employment Authorization Document (EAD)"}),
    ({"visa_status": "H1B"}, {"work_authorization": "Current Work Visa"}),
    ({"visa_status": "CPT"}, {"work_authorization": "Other / Prefer Not to Say"}), ({"visa_status": "B2"}, {"work_authorization": "Other / Prefer Not to Say"}),
    ({"visa_status": "B2", "work_authorization": "USC"}, {"work_authorization": "Other / Prefer Not to Say"}),
    ({"work_authorization": "USC"}, {"work_authorization": "US Citizen"}), ({"work_authorization": "Green Card"}, {"work_authorization": "Green Card / US Permanent Resident"}),
    ({"work_authorization": "H4"}, {"work_authorization": "Other / Prefer Not to Say"}), ({"work_authorization": "Other"}, {"work_authorization": "Other / Prefer Not to Say"}),
    ({"work_authorization": "Something new"}, {"work_authorization": "Other / Prefer Not to Say"}),
    # sponsorship: only a ticked box says something
    ({"sponsorship_required": True}, {"sponsorship_needed": "Yes"}), ({"sponsorship_required": False}, {"sponsorship_needed": None}),
    # seniority, in the spellings found in the org
    ({"experience_level": "Entry_Level"}, {"seniority": "Entry Level"}), ({"experience_level": "Entry Level"}, {"seniority": "Entry Level"}),
    ({"experience_level": "Mid-Senior_level"}, {"seniority": "Mid-Senior level"}), ({"experience_level": "Associate"}, {"seniority": "Associate"}),
    ({"experience_level": "Entry level, Associate, Mid-Senior level"}, {"seniority": "Entry Level, Associate, Mid-Senior level"}),
    ({"experience_level": "Guru"}, {"seniority": None}),
    # preferred states
    ({"preferred_locations": "Texas, California"}, {"preferred_states": ["TX", "CA"]}),
    ({"preferred_locations": "california"}, {"preferred_states": ["CA"]}), ({"preferred_locations": "NJ/NY"}, {"preferred_states": ["NJ", "NY"]}),
    ({"preferred_locations": "Dallas, TX"}, {"preferred_states": ["TX"]}),
    ({"preferred_locations": "NEW YORK, VIRGINIA, CALIFORNIA, CHICAGO."}, {"preferred_states": ["NY", "VA", "CA"]}),
    ({"preferred_locations": "Ohio or nearby states"}, {"preferred_states": ["OH"]}),
    ({"preferred_locations": "Texas, NJ, St. Louis (Open to relocate)"}, {"preferred_states": ["TX", "NJ"]}),
    ({"preferred_locations": "Texas,Atlanta,New Jesey"}, {"preferred_states": ["TX"]}),                # a misspelling is not guessed
    ({"preferred_locations": "New york, New Jersey and Washington DC"}, {"preferred_states": ["NY", "NJ", "DC"]}),
    ({"preferred_locations": "California, Texas, Ohio, etc"}, {"preferred_states": ["CA", "TX", "OH"]}),
    ({"preferred_locations": "All over USA"}, {"preferred_states": []}), ({"preferred_locations": "Anywhere"}, {"preferred_states": []}),
    ({"preferred_locations": "NA"}, {"preferred_states": []}), ({"preferred_locations": "in or near me"}, {"preferred_states": []}),
    ({"preferred_locations": "SF, cali or anywhere in usa"}, {"preferred_states": []}),
])
def test_mapping_into_the_portal_profile(fields, expected):
    mapped = sync.map_profile(person(**fields))
    for key, value in expected.items():
        assert mapped[key] == value, (key, mapped[key])


def test_mapped_values_are_values_the_portal_accepts():
    from typing import get_args

    from app.schemas import Authorization, WorkMode
    assert set(sync.AUTHORIZATION.values()) | {sync.OTHER_AUTHORIZATION} <= set(get_args(Authorization))
    assert set(sync.WORK_MODES.values()) | {"All"} <= set(get_args(WorkMode))
    assert all(len(v) <= 40 for v in sync.SENIORITY.values())
    details = sync.map_details(person(email="  Ana.Example@EXAMPLE.com ", phone="+1 (555) 010-2000 <ext> 7"))
    assert details == {"name": "Ana Example", "email": "ana.example@example.com", "phone": "+1 (555) 010-2000 ext 7"}
    assert sync.map_details(person(email="not an address", phone="call me ☎"))["email"] == ""
    assert sync.notes_line(person(preferred_locations="Texas or anywhere")) == "Preferred job locations (from Salesforce): Texas or anywhere"
    assert sync.notes_line(person(preferred_locations="N/A")) == "" and sync.notes_line(person()) == ""
    # Text from Salesforce is tidied before the portal sees it: no control characters, bounded length.
    parsed = sf.parse_account({"Id": sf_id("001", 1), "Name": "Ana\x00 \n Example", "PersonEmail": None, "Total_Years_of_Experience__c": "7",
                               "Sponsorship_Required_For_VISA__c": "yes", "Niches__r": None, "Niche__c": "x" * 400})
    assert parsed.name == "Ana Example" and parsed.email == "" and parsed.years_experience is None and parsed.sponsorship_required is None
    assert len(parsed.niche) == 255
