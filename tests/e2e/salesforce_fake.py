"""An in-process stand-in for Salesforce, for the demo / browser-test stack. No network, nobody real.

It replaces `app.state.salesforce` and answers the same questions as the real client (services/salesforce.py):
status, search, one candidate, resume flags, the resume file. The real client is covered by tests/portal with
httpx.MockTransport; this fake exists so the UI can be exercised without the real org.

The twelve candidates (all on example.com):
  Team lead "Lena Alpha" (lead.alpha@example.com):
    Amara Nwosu      Active       PDF resume   imported into Team Alpha when the demo data is seeded
    Bruno Castell    Active       DOCX resume
    Chloe Bennett    Active       PDF resume   all work arrangements, several states
    Dmitri Volkov    Active       no resume
    Farah Siddiqui   Hold         PDF resume   (found only with status=all)
    Gideon Park      Active       a .png file as "resume": refused, imported without a resume
    Chloe Benson     Placed       DOCX resume  (found only with status=all)
  Team lead "Ben Beta" (lead.beta@example.com):
    Elif Demir       Active       PDF resume   imported into Team Beta when the demo data is seeded
    Hugo Lindqvist   Active       DOCX resume
    Ines Carvalho    Active       PDF resume
    Jamal Rahimi     In Progress  no resume    (found only with status=all)
  No team lead in Salesforce:
    Keiko Tanabe     Active       PDF resume"""
from datetime import datetime, timezone

from app.portal.services.salesforce import ID_PATTERN, SfCandidate, SfResume
from tests.salesforce_samples import make_docx, make_pdf

INSTANCE_URL = "https://salesforce.example.com"
ALPHA, BETA = ("Lena Alpha", "lead.alpha@example.com"), ("Ben Beta", "lead.beta@example.com")
IMPORTED_ALPHA, IMPORTED_BETA = "001E2E000000001AAA", "001E2E000000005AAA"
# number, name, status, technology, niche, visa status, work authorization, years, job type, locations, level, lead, resume kind
_PEOPLE = [
    (1, "Amara Nwosu", "Active", "AI/ML Engineer", "AI ML Engineer", "STEM OPT Extension", "OPT", 4.0, "Remote", "Texas, California", "Mid-Senior_level", ALPHA, "pdf"),
    (2, "Bruno Castell", "Active", "Java Developer", "Java Full Stack Developer", "H1B", "H1B", 7.0, "Hybrid", "New York, New Jersey", "Mid-Senior_level", ALPHA, "docx"),
    (3, "Chloe Bennett", "Active", "Product Manager", "Product Manager", "", "USC", None, "All of the above", "Texas, NJ, St. Louis (Open to relocate)", "Associate", ALPHA, "pdf"),
    (4, "Dmitri Volkov", "Active", "Python Developer", "Python Developer", "OPT", "OPT", 2.0, "Remote", "Anywhere", "Entry_Level", ALPHA, None),
    (5, "Elif Demir", "Active", "Data Engineer", "Data Engineer/Data Analyst", "Green Card EAD", "Green Card", 5.5, "Remote, Hybrid", "Washington, Texas, Florida", "Mid-Senior_level", BETA, "pdf"),
    (6, "Farah Siddiqui", "Hold", "Financial Analyst", "Financial Analyst", "", "H4", None, "", "", "Associate", ALPHA, "pdf"),
    (7, "Gideon Park", "Active", "QA Engineer", "QA Engineer", "CPT", "CPT", 1.0, "On-site", "Chicago", "Entry_Level", ALPHA, "png"),
    (8, "Hugo Lindqvist", "Active", "DevOps Engineer", "DevOps Engineer", "US Citizen", "USC", 9.0, "Hybrid", "NJ/NY", "Mid-Senior_level", BETA, "docx"),
    (9, "Ines Carvalho", "Active", "AI/ML Engineer", "AI ML Engineer", "OPT", "OPT", 3.0, "", "California", "", BETA, "pdf"),
    (10, "Jamal Rahimi", "In Progress", "", "Other", "", "", None, "", "", "", BETA, None),
    (11, "Keiko Tanabe", "Active", "Full Stack Developer", "Full Stack Developer", "Green Card / Permanent Resident", "Green Card", 6.0, "Remote", "All over USA", "Associate", None, "pdf"),
    (12, "Chloe Benson", "Placed", "Business Analyst", "Business Analyst", "", "OPT", 2.5, "Hybrid", "Ohio or nearby states", "Entry Level", ALPHA, "docx"),
]


class FakeSalesforce:
    configured = True
    instance_url = INSTANCE_URL

    def __init__(self):
        self.people: dict[str, SfCandidate] = {}
        self.files: dict[str, tuple[SfResume, bytes]] = {}
        modified = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
        for n, name, status, technology, niche, visa, authorization, years, job_type, locations, level, lead, kind in _PEOPLE:
            sid = f"001E2E000000{n:03d}AAA"
            first, last = name.lower().split()
            self.people[sid] = SfCandidate(
                id=sid, name=name, email=f"{first}.{last}@example.com", phone=f"(555) 01{n:02d}-0{n:03d}", candidate_status=status,
                technology=technology, niche=niche, preferred_locations=locations, preferred_job_type=job_type, years_experience=years,
                work_authorization=authorization, visa_status=visa, sponsorship_required=n == 2, experience_level=level,
                assigned_recruiter="Rita Recruiter" if n % 2 else "Sam Sourcer", marketing_team_lead=lead[0] if lead else "",
                marketing_team_lead_email=lead[1] if lead else "", last_modified=modified)
            if kind:
                data = make_pdf(name) if kind == "pdf" else make_docx(name) if kind == "docx" else b"\x89PNG\r\n\x1a\n" + b"0" * 256
                self.files[sid] = (SfResume(content_version_id=f"068E2E000000{n:03d}AAA", extension=kind, size=len(data), modified_at=modified), data)

    async def aclose(self) -> None:
        pass

    async def status(self) -> dict:
        return {"configured": True, "connected": True, "message": "Connected (demo data, not the real Salesforce)",
                "checked_at": datetime.now(timezone.utc)}

    async def search(self, text: str, *, active_only: bool = True, limit: int = 10, team_lead_email: str | None = None) -> list[SfCandidate]:
        needle = text.lower()
        found = [c for c in self.people.values()
                 if (needle in c.name.lower() or needle in c.email or needle in c.phone)
                 and (not active_only or c.candidate_status == "Active")
                 and (team_lead_email is None or c.marketing_team_lead_email == team_lead_email.lower())]
        return sorted(found, key=lambda c: c.name)[:limit]

    async def get_candidate(self, salesforce_id: str) -> SfCandidate | None:
        return self.people.get(salesforce_id) if ID_PATTERN.fullmatch(salesforce_id) else None

    async def with_resume(self, salesforce_ids: list[str]) -> set[str]:
        return {i for i in salesforce_ids if i in self.files}

    async def resume_of(self, salesforce_id: str) -> SfResume | None:
        return self.files[salesforce_id][0] if salesforce_id in self.files else None

    async def download(self, resume: SfResume, max_bytes: int) -> bytes:
        return next(data for info, data in self.files.values() if info.content_version_id == resume.content_version_id)
