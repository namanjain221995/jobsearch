"""Seed employers. Every key below answered its public source with live postings on 2026-11-17.
The list is a starting point only: boards found by AI web search are added at runtime and remembered
in SQLite. Dead boards are skipped and reported, never treated as results."""
from . import workday

SEED_BOARDS: dict[str, list[str]] = {
    "greenhouse": (
        "anthropic stripe databricks airbnb figma coinbase robinhood discord instacart lyft pinterest reddit dropbox "
        "cloudflare datadog mongodb twilio okta gitlab brex chime affirm gusto asana samsara scaleai waymo nuro "
        "duolingo squarespace elastic airtable flexport roblox doordashusa block xai togetherai coreweave gleanwork "
        "andurilindustries figureai snorkelai c3iot dataiku coursera udemy carta mercury verkada toast peloton nextdoor "
        "vercel cockroachlabs singlestore starburst fivetran planetscale launchdarkly pagerduty newrelic zscaler sofi "
        "upstart oscar recursionpharmaceuticals pathai 6sense abnormalsecurity adyen agilityrobotics akunacapital "
        "apolloio apptronik aqr archer assemblyai astranis attentive axios bandwidth betterment bigid bitwarden "
        "blackcanyonconsulting braze buildkite buzzfeed calendly calm carvana chainguard chargepoint circleci "
        "cloverhealth coast collectivehealth commercetools commvault cresta cribl customerio descope dialpad doximity "
        "dremio druva dynotherapeutics epicgames epirus expel faire fanduel fastly fireblocks five9 flatironhealth "
        "forward fourkites freenome gemini generatebiomedicines ginkgobioworks givedirectly grafanalabs honeycomb "
        "huntress imbue imc imply intercom janestreet jetbrains jumptrading justworks k2spacecorporation khanacademy "
        "kickstarter klaviyo knowbe4 kodiak komodohealth labelbox lightmatter lucidmotors make manifest mattermost "
        "maymobility medium mixpanel modernhealth motional motive mozilla natera nebius netlify novacredit odinlabs "
        "oklo omadahealth onemedical onetrust opentable pandadoc parsleyhealth payoneer planetlabs point72 prizepicks "
        "project44 purestorage qualtrics ramp redwoodmaterials relativity remote riotgames ripple rocketlab rubrik "
        "salesloft sambanovasystems scopely seatgeek sendbird sigmacomputing smartsheet socket speechmatics spire "
        "stabilityai stockx suki swordhealth tailscale talkspace tanium tenstorrent thoughtworks tripactions "
        "tripadvisor twitch typeface udio vast virtu vonage wikimedia wise workato worldquant yugabyte zocdoc zoominfo "
    ).split(),
    "ashby": (
        "openai ramp notion linear cursor perplexity cohere modal replit vanta harvey elevenlabs runway sierra decagon "
        "anyscale pinecone weaviate langchain baseten character writer sardine suno pika physicalintelligence cognition "
        "mercor supabase posthog airbyte prefect zapier plaid snowflake lambda crusoe reka poolside magic.dev 1x "
        "benchling quora docker 1password 3ive abridge aiuc alternativepayments ambiencehealthcare amplitude anterior "
        "artificialanalysis asimov astronomer bonfireanalytics category-labs cedar cerebras charge-robotics "
        "chariotclaims checkly circle clerk clickhouse clickup close colonist comity creditgenie deepgram deepl deeter- "
        "analytics drata duck-duck-go eightsleep equall etched expensify footprint formenergy goteleport growfig helion "
        "hex hightouch hiya hopper iceye incident influxdata insitro iterable jobber jupus kayak krakentech lancedb "
        "livekit luxor mastra materialize menlo middesk midjourney miro motherduck nabla neon norm-ai odyssey orca "
        "oscilar outschool pandadoc patreon paxos permitflow poshmark prior-labs productgenius proxima-fusion qumulo "
        "railway rebar redis reducto reframesystems render river runpod runway-ml saronic scholarly secureframe semgrep "
        "sentilink sentry sheerhealth skydio sleeper smarkets snyk socket solace somana.tech sonder speechmatics strava "
        "stream substack synthesia temporal thoughtworks thundercompute tolmo turquoise-health virtahealth voladynamics "
        "wayve webflow whoop workos worldlabs "
    ).split(),
    "lever": (
        "palantir spotify zoox wealthfront outreach ro matchgroup veeva quantco- weride achievers gopuff binance "
        "anchorage angellist apolloresearch auw brilliant curai dexterity findigs getduranta hermeus loftorbital "
        "lyrahealth neon olo osaro rai secureframe sila sonatype sysdig theathletic zilliz "
    ).split(),
}

# Workday career sites: "tenant|data centre|site" -> employer name shown on results.
# Technology-heavy employers come first: Workday is paced, so only the first ones are read while a search waits.
WORKDAY_SITES: dict[str, str] = {
    "nvidia|wd5|NVIDIAExternalCareerSite": "NVIDIA",
    "salesforce|wd12|External_Career_Site": "Salesforce",
    "adobe|wd5|external_experienced": "Adobe",
    "intel|wd1|External": "Intel",
    "cisco|wd5|Cisco_Careers": "Cisco",
    "micron|wd1|External": "Micron",
    "marvell|wd1|MarvellCareers": "Marvell",
    "broadcom|wd1|External_Career": "Broadcom",
    "amat|wd1|External": "Applied Materials",
    "kla|wd1|Search": "KLA",
    "analogdevices|wd1|External": "Analog Devices",
    "cadence|wd1|External_Careers": "Cadence",
    "globalfoundries|wd1|External": "GlobalFoundries",
    "hp|wd5|ExternalCareerSite": "HP",
    "hpe|wd5|Jobsathpe": "Hewlett Packard Enterprise",
    "crowdstrike|wd5|crowdstrikecareers": "CrowdStrike",
    "redhat|wd5|Jobs": "Red Hat",
    "workday|wd5|Workday": "Workday",
    "paypal|wd1|jobs": "PayPal",
    "mastercard|wd1|CorporateCareers": "Mastercard",
    "capitalone|wd12|Capital_One": "Capital One",
    "zoom|wd5|Zoom": "Zoom",
    "snapchat|wd1|snap": "Snap",
    "ebay|wd5|apply": "eBay",
    "zillow|wd5|Zillow_Group_External": "Zillow",
    "expedia|wd108|search": "Expedia",
    "equinix|wd1|External": "Equinix",
    "ffive|wd5|f5jobs": "F5",
    "ptc|wd1|PTC": "PTC",
    "trimble|wd1|TrimbleCareers": "Trimble",
    "logitech|wd5|Logitech": "Logitech",
    "sonyglobal|wd1|SonyGlobalCareers": "Sony",
    "motorolasolutions|wd5|Careers": "Motorola Solutions",
    "verizon|wd12|verizon-careers": "Verizon",
    "tmobile|wd1|External": "T-Mobile",
    "att|wd1|ATTGeneral": "AT&T",
    "boeing|wd1|EXTERNAL_CAREERS": "Boeing",
    "ngc|wd1|Northrop_Grumman_External_Site": "Northrop Grumman",
    "globalhr|wd5|REC_RTX_Ext_Gateway": "RTX",
    "leidos|wd5|External": "Leidos",
    "bah|wd1|BAH_Jobs": "Booz Allen Hamilton",
    "gdit|wd5|External_Career_Site": "GDIT (General Dynamics)",
    "blueorigin|wd5|BlueOrigin": "Blue Origin",
    "sierraspace|wd1|Sierra_Space_External_Career_Site": "Sierra Space",
    "gehc|wd5|GEHC_ExternalSite": "GE HealthCare",
    "geaerospace|wd5|GE_ExternalSite": "GE Aerospace",
    "gevernova|wd5|Vernova_ExternalSite": "GE Vernova",
    "generalmotors|wd5|Careers_GM": "General Motors",
    "thermofisher|wd5|ThermoFisherCareers": "Thermo Fisher Scientific",
    "jj|wd5|JJ": "Johnson & Johnson",
    "pfizer|wd1|PfizerCareers": "Pfizer",
    "msd|wd5|SearchJobs": "Merck",
    "amgen|wd1|Careers": "Amgen",
    "gilead|wd1|gileadcareers": "Gilead",
    "regeneron|wd1|Careers": "Regeneron",
    "illumina|wd1|illumina-careers": "Illumina",
    "modernatx|wd1|M_tx": "Moderna",
    "medtronic|wd1|MedtronicCareers": "Medtronic",
    "abbott|wd5|abbottcareers": "Abbott",
    "stryker|wd1|StrykerCareers": "Stryker",
    "danaher|wd1|DanaherJobs": "Danaher",
    "iqvia|wd1|IQVIA": "IQVIA",
    "roche|wd3|roche-ext": "Roche",
    "gsk|wd5|GSKCareers": "GSK",
    "citi|wd5|2": "Citi",
    "ghr|wd1|Lateral-US": "Bank of America",
    "usbank|wd1|US_Bank_Careers": "U.S. Bank",
    "pnc|wd5|External": "PNC",
    "pnc|wd5|HarrisWilliams": "Harris Williams (PNC)",
    "statestreet|wd1|Global": "State Street",
    "fmr|wd1|FidelityCareers": "Fidelity",
    "troweprice|wd5|TRowePrice": "T. Rowe Price",
    "blackrock|wd1|BlackRock_Professional": "BlackRock",
    "blackstone|wd1|Blackstone_Careers": "Blackstone",
    "vanguard|wd5|vanguard_external": "Vanguard",
    "nasdaq|wd1|Global_External_Site": "Nasdaq",
    "spgi|wd5|SPGI_Careers": "S&P Global",
    "factset|wd108|FactSetCareers": "FactSet",
    "transunion|wd5|TransUnion": "TransUnion",
    "equifax|wd5|External": "Equifax",
    "fico|wd1|External": "FICO",
    "fis|wd5|SearchJobs": "FIS",
    "fiserv|wd5|EXT": "Fiserv",
    "broadridge|wd5|Careers": "Broadridge",
    "3m|wd1|Search": "3M",
    "accenture|wd103|AccentureCareers": "Accenture",
    "accenture|wd103|AvanadeCareers": "Avanade",
    "agilent|wd5|Agilent_Careers": "Agilent",
    "allstate|wd5|allstate_careers": "Allstate",
    "ameriprise|wd5|Ameriprise": "Ameriprise",
    "aptiv|wd5|APTIV_CAREERS": "Aptiv",
    "athenahealth|wd1|External": "athenahealth",
    "bakerhughes|wd5|BakerHughes": "Baker Hughes",
    "baxter|wd1|baxter": "Baxter",
    "borgwarner|wd5|BorgWarner_Careers": "BorgWarner",
    "cardinalhealth|wd1|EXT": "Cardinal Health",
    "cardinalhealth|wd1|Navista": "Navista (Cardinal Health)",
    "carrier|wd5|jobs": "Carrier",
    "centene|wd5|Centene_External": "Centene",
    "chevron|wd5|jobs": "Chevron",
    "ciena|wd5|Careers": "Ciena",
    "cigna|wd5|cignacareers": "Cigna",
    "clorox|wd1|Clorox": "Clorox",
    "cox|wd1|Cox_External_Career_Site_1": "Cox",
    "cvshealth|wd1|CVS_Health_Careers": "CVS Health",
    "dexcom|wd1|Dexcom": "Dexcom",
    "disney|wd5|disneycareer": "Disney",
    "draper|wd5|Draper_Careers": "Draper",
    "dxctechnology|wd1|DXCJobs": "DXC Technology",
    "edwards|wd5|EdwardsCareers": "Edwards",
    "elevancehealth|wd1|ANT": "Elevance Health",
    "gartner|wd5|EXT": "Gartner",
    "geico|wd1|External": "GEICO",
    "google|wd501|GOCJobs": "Google Operations Center",
    "hitachi|wd1|hitachi": "Hitachi",
    "homedepot|wd5|CareerDepot": "The Home Depot",
    "humana|wd5|CenterWell_External_Career_Site": "CenterWell (Humana)",
    "insulet|wd5|insuletcareers": "Insulet",
    "intuitive|wd1|irtc_careers": "Intuitive",
    "invesco|wd1|IVZ": "Invesco",
    "kimberlyclark|wd1|GLOBAL": "Kimberly-Clark",
    "kyndryl|wd5|KyndrylProfessionalCareers": "Kyndryl",
    "labcorp|wd1|External": "Labcorp",
    "lowes|wd5|LWS_External_CS": "Lowe's",
    "lumentum|wd5|LITE": "Lumentum",
    "mars|wd3|External": "Mars",
    "mckesson|wd3|External_Careers": "McKesson",
    "nationwide|wd1|Nationwide_Career": "Nationwide",
    "ncr|wd1|ext_us": "NCR",
    "nike|wd1|nke": "Nike",
    "nordstrom|wd501|nordstrom_careers": "Nordstrom",
    "northwesternmutual|wd5|CORPORATE-CAREERS": "Northwestern Mutual",
    "pru|wd5|Careers": "Prudential",
    "pru|wd5|PGIM_Careers": "PGIM (Prudential)",
    "pwc|wd3|Global_Experienced_Careers": "PwC",
    "resmed|wd3|Brightree_External_Careers": "Brightree (ResMed)",
    "resmed|wd3|ResMed_External_Careers": "ResMed",
    "rockwellautomation|wd1|External_Rockwell_Automation": "Rockwell Automation",
    "ryder|wd5|RyderCareers": "Ryder",
    "sabre|wd1|SabreJobs": "Sabre",
    "sonos|wd1|Sonos": "Sonos",
    "swa|wd1|external": "Southwest Airlines",
    "synchronyfinancial|wd5|careers": "Synchrony",
    "target|wd5|targetcareers": "Target",
    "td|wd3|TD_Bank_Careers": "TD Bank",
    "thehartford|wd5|Careers_External": "The Hartford",
    "tiaa|wd1|Search": "TIAA",
    "tjx|wd1|TJX_EXTERNAL": "TJX",
    "travelers|wd5|External": "Travelers",
    "truist|wd1|Careers": "Truist",
    "unisys|wd5|External": "Unisys",
    "usaa|wd1|USAAJOBSWD": "USAA",
    "vfc|wd5|timberland_careers": "Timberland (VF Corporation)",
    "vfc|wd5|vans_careers": "Vans (VF Corporation)",
    "vrtx|wd501|Vertex_Careers": "Vertex Pharmaceuticals",
    "warnerbros|wd5|global": "Warner Bros. Discovery",
    "wexinc|wd5|WEXInc": "WEX",
    "zebra|wd501|Zebra_careers": "Zebra",
    "zoetis|wd5|zoetis": "Zoetis",
}
workday.DISPLAY_NAMES.update(WORKDAY_SITES)


def seed_pairs() -> list[tuple[str, str]]:
    """(source, key) pairs, interleaved so a small budget still covers every source."""
    lists = {**{k: list(v) for k, v in SEED_BOARDS.items()}, "workday": list(WORKDAY_SITES)}
    out: list[tuple[str, str]] = []
    while any(lists.values()):
        for name in ("greenhouse", "ashby", "workday", "lever"):
            if lists[name]:
                out.append((name, lists[name].pop(0)))
    return out


# ---------------------------------------------------------------- extra board sources (app/providers/ats_extra.py)
def extra_specs(include_disabled: bool = False) -> dict:
    """Spec objects for the additional board readers, ready to merge into registry.SPECS:
        SPECS.update(boards.extra_specs())
    Only sources switched on by their setting are returned (both are off by default; see docs/board-sources.md)."""
    from . import ats_extra
    from .registry import Spec
    out = {}
    if include_disabled or ats_extra.SMARTRECRUITERS_ENABLED:
        out["smartrecruiters"] = Spec("smartrecruiters", "SmartRecruiters", "board", ats_extra.fetch_smartrecruiters, keyed=True,
                                      detail=ats_extra.detail_smartrecruiters, min_ttl=6 * 3600,
                                      note="Employer boards, public SmartRecruiters posting API")
    if include_disabled or ats_extra.BAMBOOHR_ENABLED:
        out["bamboohr"] = Spec("bamboohr", "BambooHR", "board", ats_extra.fetch_bamboohr, keyed=True,
                               detail=ats_extra.detail_bamboohr, min_ttl=6 * 3600,
                               note="Employer boards, public BambooHR careers page feed")
    return out
