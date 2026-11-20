"""The shared list of applicant-tracking systems (app/ats_platforms.py). Other code reads it at run time, so these
tests pin what must not drift: which platforms exist, how they are named, and that a reader is only ever counted as
usable when the registry really has it."""
from app import ats_platforms
from app.ats_platforms import ACCESS, PLATFORMS, STARTUP_PLATFORMS, platform_of_host
from app.config import Settings
from app.portal.services import ats_health
from app.providers import registry

EXPECTED = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday", "icims": "iCIMS",
    "smartrecruiters": "SmartRecruiters", "workable": "Workable", "jobvite": "Jobvite", "oracle": "Oracle Recruiting Cloud",
    "successfactors": "SAP SuccessFactors", "bullhorn": "Bullhorn", "ceipal": "CEIPAL", "jobdiva": "JobDiva",
    "bamboohr": "BambooHR", "jazzhr": "JazzHR", "avature": "Avature", "pinpoint": "Pinpoint", "recruitee": "Recruitee",
    "rippling": "Rippling", "smartsearch": "SmartSearch", "adp": "ADP Workforce Now",
}
STARTUP = {"ashby", "greenhouse", "lever", "workable", "recruitee", "pinpoint", "rippling"}


def test_exactly_the_21_platforms_with_their_names():
    assert len(PLATFORMS) == 21
    assert {key: platform.label for key, platform in PLATFORMS.items()} == EXPECTED
    assert all(key == platform.id for key, platform in PLATFORMS.items())


def test_the_seven_startup_platforms():
    assert set(STARTUP_PLATFORMS) == STARTUP and len(STARTUP_PLATFORMS) == 7
    assert {p.id for p in PLATFORMS.values() if p.startup} == STARTUP


def test_every_access_class_is_known_and_explained():
    assert all(platform.access in ACCESS for platform in PLATFORMS.values())
    assert all(sentence.strip() for sentence in ACCESS.values())
    assert set(ACCESS) >= {"public_api", "public_pages", "credentials", "restricted"}


def test_a_reader_counts_as_usable_only_when_the_registry_has_it():
    """Holds while readers are still being added: a reader named here but missing from the registry is reported as
    not switched on, never as usable; one that is in the registry reads postings (it is not a discovery engine)."""
    settings = Settings(_env_file=None)
    usable = registry.enabled_specs(settings)
    for platform in PLATFORMS.values():
        if not platform.reader:
            continue
        assert platform.reader == platform.id                  # the reader's source name is the platform id
        spec = registry.SPECS.get(platform.reader)
        if spec is None:
            assert platform.reader not in usable
            assert ats_health.reader_switch(platform.reader, settings, set())           # a reason why it is off
            assert ats_health.platform_state(platform, settings, set(), None)[0] in ("unsupported", "public_discovery_only") or platform.access == "credentials"
        else:
            assert spec.kind != "discovery"
    for name in usable:                                        # and every usable reader of a platform is in the registry
        assert name in registry.SPECS
    working = [p for p in PLATFORMS.values() if p.reader and not ats_health.reader_switch(p.reader, settings, set())]
    assert all(p.reader in registry.SPECS for p in working)


def test_platform_of_host():
    assert platform_of_host("boards.greenhouse.io").id == "greenhouse"
    assert platform_of_host("www.jobs.lever.co").id == "lever"
    assert platform_of_host("JOBS.ASHBYHQ.COM").id == "ashby"
    assert platform_of_host("nvidia.wd5.myworkdayjobs.com").id == "workday"            # any subdomain of a ".suffix" host
    assert platform_of_host("greenhouse.io") is None                                    # an exact host is not a suffix
    assert platform_of_host("notmyworkdayjobs.com") is None
    assert platform_of_host("example.com") is None and platform_of_host("") is None
    for platform in PLATFORMS.values():                                                 # every listed host leads back to its platform
        for host in platform.hosts:
            assert platform_of_host("acme" + host if host.startswith(".") else host) is platform
    assert ats_platforms.platform_of_host is platform_of_host
