"""Application settings. All secrets come from the environment / .env, never from code."""
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_OPENAI_MODEL = "gpt-6-astra"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), extra="ignore")

    openai_api_key: str = ""
    anthropic_api_key: str = ""
    openai_model: str = DEFAULT_OPENAI_MODEL
    openai_reasoning_effort: str = "low"     # "" to send no reasoning setting
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL
    # Primary database (PostgreSQL). A sqlite:/// value is still accepted and selects the legacy single-user mode.
    database_url: str = "postgresql+asyncpg://jobfinder:jobfinder@127.0.0.1:55432/jobfinder"
    legacy_sqlite_path: str = "./jobfinder.db"       # the pre-PostgreSQL index file (source for migration, legacy app)
    redis_url: str = "redis://127.0.0.1:56379/0"
    search_max_results: int = 1000                   # most results one candidate search may return (hard ceiling of the engine)

    # Portal: sessions, invitations, e-mail.
    app_env: str = "development"                     # "production" enforces secure cookies
    public_base_url: str = "http://127.0.0.1:3010"   # where users reach the web app; used in invitation / reset links
    allowed_origins: str = ""                        # extra origins allowed to send state-changing requests (comma separated)
    cookie_secure: bool = False
    trust_proxy_headers: bool = True                 # the API sits behind the Next.js proxy; set false if it is exposed directly
    trust_forwarded_for: bool = False                # true only when a reverse proxy in front of the web app sets X-Forwarded-For itself
    trusted_proxy_hops: int = 1                      # how many such proxies append to X-Forwarded-For (the entry read counts from the right)
    login_account_max_failures: int = 20             # failed sign-ins per account and window, from any address
    api_docs: bool | None = None                     # /api/docs and /api/openapi.json; default: off in production
    max_json_body_bytes: int = 512 * 1024            # any request that is not a file upload
    max_csv_bytes: int = 2 * 1024 * 1024
    max_active_searches_per_user: int = 3            # queued + running searches one person may have
    max_active_searches: int = 12                    # queued + running searches in the whole installation
    invitations_per_address_per_hour: int = 5
    invitations_per_sender_per_hour: int = 60
    session_hours: int = 12
    remember_days: int = 30
    invitation_hours: int = 48
    reset_minutes: int = 60
    password_min_length: int = 10
    login_max_failures: int = 5
    login_window_minutes: int = 15
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    smtp_implicit_tls: bool | None = None            # TLS from the first byte instead of STARTTLS; None = on exactly when SMTP_PORT is 465
    email_backend: str = ""                          # "" = smtp when SMTP_HOST is set, otherwise none; "console" prints mails to the server log
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""
    bootstrap_admin_name: str = "Administrator"

    # Salesforce (strictly read-only). JWT bearer flow: a connected app's consumer key, the integration user, the
    # login URL (also the token audience) and the RSA private key (PEM) in base64. All four empty = switched off.
    sf_client_id: str = ""
    sf_username: str = ""
    sf_login_url: str = ""
    sf_private_key_b64: str = Field(default="", validation_alias=AliasChoices("sf_private_key_b64", "private_key_b64"))
    salesforce_searches_per_minute: int = 60         # Salesforce candidate searches one user may run per minute

    # Portal: team searches.
    max_members_per_search: int = 10
    max_results_per_search_run: int = 500            # candidates x results-per-candidate allowed in one run; an Admin can raise it
                                                     # in Settings (up to 5000). A 1,000-job request for one candidate needs 1000 here.
    max_target_count: int = 1000                     # most results per candidate one search may ask for (never above SEARCH_MAX_RESULTS)
    member_search_concurrency: int = 2               # members of one run searched at the same time
    worker_max_searches: int = 2                     # search runs a worker processes at the same time
    analytics_cache_seconds: int = 60                # Redis cache for team / organisation analytics; 0 turns it off
    log_format: str = "json"                         # json | text
    log_level: str = "INFO"
    default_min_match_score: int = 50                # jobs scoring lower are never auto-assigned

    # Optional job-search API (not required).
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""

    # Local job index (SQLite). A source is re-read when its copy is older than the TTL.
    index_ttl_seconds: int = 3600            # how long a fetched board / query counts as verified-fresh
    index_stale_ok_seconds: int = 86400      # older copies may still be shown as *preliminary* results while refreshing
    prewarm_on_start: bool = True            # read employer boards in the background when the server starts
    fetch_deadline_seconds: float = 45.0     # how long one search waits for sources before using what it has
    disabled_sources: str = ""               # comma-separated source names to switch off, e.g. "amazon,jobicy"
    # Startup ATS board readers (providers/ats_startup.py has the robots.txt / terms findings behind each default).
    recruitee_enabled: bool = True
    pinpoint_enabled: bool = False           # OFF: most employer tenants answer robots.txt with `Disallow: /`
    rippling_enabled: bool = False           # OFF until Rippling's terms are reviewed: the endpoints are undocumented and the terms could not be read
    workable_search_enabled: bool = True     # Workable's cross-company job search (undocumented endpoint); false keeps only the documented account reader
    rippling_pages: int = 20                 # list pages (100 rows each) read per Rippling board

    # Search budgets / stop conditions.
    max_boards_per_search: int = 1500
    search_queries_per_search: int = 2       # how many title queries go to query-based sources (Workday, Workable, ...)
    workday_pages: int = 1                   # result pages (20 each) read per Workday employer
    max_detail_fetches: int = 150            # postings whose description is fetched on demand per search
    http_timeout_seconds: float = 45.0
    openai_search_calls: int = 2
    claude_search_calls: int = 2
    ai_web_searches_per_call: int = 5
    ai_discovery_deadline_seconds: float = 120.0
    # AI web search (discovery) of one search: what each provider may do. The calls per search are OPENAI_SEARCH_CALLS /
    # CLAUDE_SEARCH_CALLS above (scaled by the request size, see search_plan.py).
    ai_discovery_call_timeout_seconds: float = 100.0  # one web-search call; a call still running then is dropped, the finished ones keep their leads
    ai_discovery_breaker_failures: int = 2            # failed web-search calls in a row that switch a provider off for the rest of that search (the other one takes over its searches)
    openai_search_concurrency: int = 4                # web-search calls of one search running at the same time, per provider
    claude_search_concurrency: int = 4
    openai_search_model: str = ""                     # model for web-search discovery only; empty = OPENAI_MODEL
    anthropic_search_model: str = ""                  # model for web-search discovery only; empty = ANTHROPIC_MODEL
    ai_open_web_share: float = 0.5                    # share of the OpenAI / Claude web-search calls of a search that look at employers' OWN career sites (any domain) instead of the ATS hosts; 0 = none, 1 = all (always none under an ATS platform filter)
    ai_open_web_split: str = "auto"                   # who makes those calls. auto: first the provider whose search tool holds a `site:` query to its site (Claude), the other one the ATS hosts; even: each provider alternates
    max_ai_calls_per_search: int = 30
    max_ai_ranked_jobs: int = 120
    ai_rank_batch_size: int = 6              # small batches answer faster; they run side by side
    ai_rank_concurrency: int = 10
    max_audit_jobs: int = 40
    # Page leads (postings on employers' own career sites and on career platforms without a reader) are verified from
    # the page. Measured on 2026-11-21 (tests/fixtures/pages/eval_set.json): the plain fetch decides 11 pages a second
    # at 32 at once (replayed with the recorded response times), the company model's page check answers about 1 page a
    # second with 4 calls at once, and a browser page takes 3 to 13 s (mean 8 s; BROWSER_CONCURRENCY at once).
    max_page_verifications: int = 160        # page leads read per search of 50 results (grows with the request, see MAX_PAGE_VERIFICATIONS_MAX)
    page_verification_concurrency: int = 32  # page leads checked at the same time (never more than two requests at a time per host)
    max_ai_page_extractions: int = 8         # pages sent to the AI page check in one call
    ai_page_extraction_calls: int = 10       # such calls per round when the company model answers them (no bill)
    ai_page_extraction_paid_calls: int = 1   # ... of which a model with a bill may answer this many (all of them when there is no company model)
    ai_page_extraction_concurrency: int = 4  # page-check calls running at the same time (the company model's queue is shared with the chat app)
    ai_timeout_seconds: float = 180.0

    # ---- Wider searches ("find more"): title expansion, search-engine discovery, larger AI budgets, rounds.
    # Company model service (OpenAI-compatible, no per-call bill). Empty key = not used; everything falls back
    # to OpenAI / Claude and the keyword scoring.
    techsara_api_key: str = ""
    techsara_base_url: str = "https://ai.techsarasolutions.com/v1"
    techsara_model: str = "techsara-35b"             # chat: title expansion, page extraction, explanations beyond the top slice
    techsara_rerank_model: str = "techsara-rerank"   # /v1/rerank: scores every remaining candidate against the profile
    techsara_web_search_enabled: bool = True         # the model's web_search tool as one more search engine for employer job boards
    techsara_web_searches_per_search: int = 40       # most web searches one job search may ask the model for (hard ceiling)
    techsara_web_searches_base: int = 12             # ... for a request of 50 or fewer; larger requests scale up to the ceiling above
    techsara_web_search_concurrency: int = 2         # web searches sent to the model at the same time (its queue is shared with the chat app)
    techsara_web_search_timeout_seconds: float = 60.0
    techsara_timeout_seconds: float = 90.0
    techsara_retries: int = 3                        # attempts after a 503 / 429 / timeout (Retry-After is honoured, capped at 20 s)
    techsara_concurrency: int = 4                    # requests to the company model at the same time (its queue is shared with the chat app)
    techsara_rerank_batch: int = 32                  # documents per /v1/rerank request
    techsara_rerank_max_jobs: int = 3000             # most candidates of one search sent to the reranker
    techsara_explain_max_jobs: int = 400             # jobs beyond the OpenAI/Claude slice that get a written explanation from the chat model
    techsara_explain_batch: int = 5
    techsara_explain_seconds: float = 180.0          # time one search may spend on those explanations; the rest keep the rerank note
    techsara_rerank_seconds: float = 90.0            # time the reranker may work for one search in the background, starting when the first postings are verified
    techsara_rerank_wait_seconds: float = 20.0       # time the ranking stage then waits for it at most; jobs it did not reach (the weakest keyword matches) keep their keyword score
    ai_rank_seconds: float = 150.0                   # time one search may spend on OpenAI / Claude judging the shortlist; unjudged jobs keep their other score
    career_page_memory: bool = True                  # postings verified on employers' own career sites are kept in the index and checked again later (source "career_page")
    # Career-site crawler (app/career_sites.py): an employer's own career site, once a search verified a posting on it
    # (or once it was imported from a file), is read in the background for all its US postings. Never during a search.
    career_site_crawl: bool = True                   # false: sites are still remembered, none is crawled
    career_site_crawl_days: float = 7.0              # one site is crawled at most once in this many days
    career_site_pages_per_crawl: int = 150           # new posting pages read on one site in one crawl
    career_site_pages_per_day: int = 2000            # new posting pages read on all sites together in 24 hours
    career_site_request_gap_seconds: float = 1.0     # pause between two requests to one host (never less than 1 s; a longer Crawl-delay in robots.txt wins)
    career_site_sitemaps_per_crawl: int = 12         # sitemap files read on one site in one crawl
    career_site_browser_pages: int = 4               # pages rendered in the headless browser on one site in one crawl (listing pages; 0 = never)
    career_site_concurrency: int = 2                 # sites crawled at the same time (each at its own one-request pace)
    career_site_crawl_seconds: float = 1200.0        # one crawl stops reading new pages after this long
    career_site_max_backoff_days: float = 90.0       # a site that fails, is not readable or has no US postings waits longer each time, never longer than this
    rank_cache_days: float = 7.0                     # a model's judgement of the same candidate and the same posting text is reused this long; 0 = never

    title_expansion: bool = True                     # one AI call per search: real-world variants of the requested titles
    title_expansion_max: int = 12                    # most variants kept after validation
    title_expansion_cache_hours: float = 168.0       # the same titles are not expanded again for a week

    searxng_url: str = ""                            # e.g. http://searxng:8080 ; empty = search-engine discovery off
    searxng_timeout_seconds: float = 12.0
    searxng_min_interval_seconds: float = 2.0        # gap between two requests to the instance (it forwards to public engines)
    searxng_cache_hours: float = 12.0                # an identical query is answered from the index for this long
    searxng_queries_per_search: int = 12             # queries for a request of 50 or fewer
    searxng_queries_per_search_max: int = 90         # hard ceiling however many results are requested
    searxng_pages_per_query: int = 2                 # result pages read per query (page 2 only when page 1 was full of new addresses)
    searxng_new_boards_per_search: int = 40          # for a request of 50 or fewer
    searxng_new_boards_per_search_max: int = 400     # hard ceiling: employer boards one search may add to the index
    searxng_page_leads_per_search: int = 40          # direct posting addresses (non-board hosts) passed to page verification
    searxng_sites: str = ("boards.greenhouse.io,job-boards.greenhouse.io,jobs.lever.co,jobs.ashbyhq.com,apply.workable.com,"
                          "myworkdayjobs.com,jobs.smartrecruiters.com,recruitee.com,applytojob.com")   # add pinpointhq.com with PINPOINT_ENABLED, ats.rippling.com with RIPPLING_ENABLED

    # ---- Paid sources. Spent only after the free ones (index, SearXNG, company model) left a search short.
    # SerpApi, Google Jobs. The plan's monthly allowance is the binding limit: every call is counted per calendar month.
    serpapi_api_key: str = Field(default="", validation_alias=AliasChoices("serpapi_KEY", "serpapi_key", "SERPAPI_API_KEY", "serpapi_api_key"))
    serpapi_max_calls_per_search: int = 8            # result pages (10 jobs each) one candidate search may request
    serpapi_monthly_limit: int = 250                 # the plan's searches per month, used when the account endpoint cannot be read
    serpapi_monthly_reserve: int = 25                # calls kept back so the last searches of the month still get some
    serpapi_pages_per_query: int = 3                 # a query is paged deeper only while a page brings new jobs
    serpapi_cache_hours: float = 6.0                 # an identical query page is answered from the index for this long (no call)
    serpapi_account_check_seconds: float = 300.0     # how often the remaining allowance is read from the account endpoint (free)
    serpapi_include_listed_only: bool = True         # return jobs that only have job-board links, marked "unverified: listed on Google Jobs"
    # Job boards and aggregators: their pages are never fetched, whoever names them.
    aggregator_hosts: str = ("linkedin.com,indeed.com,glassdoor.com,ziprecruiter.com,simplyhired.com,dice.com,talent.com,jobleads.com,"
                             "tealhq.com,theladders.com,bebee.com,recruit.net,monster.com,careerbuilder.com,jooble.org,adzuna.com,"
                             "lensa.com,snagajob.com,jobrapido.com,whatjobs.com,learn4good.com,salary.com,jobget.com,builtin.com,"
                             "wellfound.com,flexjobs.com,remoterocketship.com,jobgether.com,google.com")
    # TinyFish: TinySearch as a second discovery engine beside SearXNG, TinyFetch for pages our own browser could not render.
    # Its agent, research, browser and vault endpoints are never used.
    tinyfish_api_key: str = Field(default="", validation_alias=AliasChoices("tinyfish_api_key", "TINYFISH_API_KEY"))
    tinyfish_search_queries_per_search: int = 20     # TinySearch queries one candidate search may send
    tinyfish_fetches_per_search: int = 10            # TinyFetch page reads one candidate search may request
    tinyfish_search_usd: float = 0.005               # price per query (wallet endpoint, 2026-11-18)
    tinyfish_fetch_usd: float = 0.001                # price per fetched address
    tinyfish_daily_usd: float = 1.0                  # most that may be spent per UTC day, computed from the two prices above
    tinyfish_wallet_reserve_usd: float = 2.0         # stop using TinyFish when the wallet balance is below this
    tinyfish_wallet_check_seconds: float = 300.0
    tinyfish_timeout_seconds: float = 30.0

    # AWS Bedrock AgentCore Web Search (boto3, instance-profile credentials). Off until the operator switches it on.
    # A discovery source in every search (round 1 and later rounds while the target is not met) and the managers'
    # POST /api/web-search/jobs. Result text is never stored (see providers/aws_web_search.py and the README).
    aws_web_search_enabled: bool = False
    aws_web_search_region: str = "us-east-1"
    aws_web_search_id: str = "web-search.v1"
    aws_web_search_monthly_limit: int = 4000         # queries per calendar month (UTC), counted in PostgreSQL before each call
    aws_web_search_monthly_reserve: int = 300        # kept for the interactive endpoint: background searches stop at limit - reserve
    aws_web_search_max_calls_per_search: int = 4     # per candidate search for a request of 50 or fewer (paced down when the month runs low)
    aws_web_search_max_calls_per_search_max: int = 10  # hard ceiling for large requests
    aws_web_search_expected_searches_per_day: int = 45   # pacing assumption: candidate searches per working day (15 runs x up to 3)
    aws_web_search_max_results: int = 25             # results per call (the API allows 1-25)
    aws_web_search_published_days: int = 0           # publishedAfter filter in days; 0 = off. With the filter on, job-board queries returned nothing (2026-11-20)
    aws_web_search_connect_timeout_seconds: float = 5.0
    aws_web_search_read_timeout_seconds: float = 20.0
    aws_web_search_max_attempts: int = 3             # attempts per query on throttling / 5xx / timeout (each attempt counts)
    aws_web_search_user_per_minute: int = 3          # endpoint requests per user and minute
    aws_web_search_user_per_day: int = 40            # endpoint requests per user and UTC day
    aws_web_search_endpoint_max_calls: int = 6       # queries one endpoint request may use
    aws_web_search_endpoint_timeout_seconds: float = 75.0

    # AI web search scales with the requested number. The base values above (OPENAI_SEARCH_CALLS, CLAUDE_SEARCH_CALLS,
    # AI_WEB_SEARCHES_PER_CALL, MAX_AI_CALLS_PER_SEARCH, MAX_AI_RANKED_JOBS) apply to a request of 50 or fewer;
    # these are the hard ceilings for large requests.
    ai_budget_scaling: bool = True
    openai_search_calls_max: int = 16
    claude_search_calls_max: int = 16
    ai_web_searches_per_call_max: int = 10
    max_ai_calls_per_search_max: int = 260
    max_ai_ranked_jobs_max: int = 400                # ceiling of the OpenAI/Claude-judged slice (40 % of the request, at least MAX_AI_RANKED_JOBS)
    max_page_verifications_max: int = 1200           # page leads read by the largest searches (replayed 2026-11-21: 576 pages in 40 s, 16 ms of CPU a page; AI and browser checks keep their own limits)
    max_detail_fetches_max: int = 1000
    max_audit_jobs_max: int = 200
    max_boards_per_search_max: int = 6000            # boards and source queries one search may ask to be fresh
    max_index_candidates: int = 6000                 # postings of one search loaded from the index for screening (best title matches first)

    # Rounds: while fewer verified matches than requested exist, discovery goes on with further variants / locations.
    # A search stops only when the target is reached, SEARCH_STALE_ROUNDS rounds in a row added no new verified job,
    # every source's budget is used up, the wall-clock ceiling is reached, or someone cancels it.
    search_max_rounds: int = 12                      # safety ceiling on rounds; 1 = single pass, as before
    search_stale_rounds: int = 2                     # consecutive rounds without a new verified job that end a search
    search_max_seconds: float = 10800.0              # wall-clock ceiling of one candidate search: no new round starts after it
    search_budget_rounds: int = 4                    # the search-wide discovery budgets are handed out over this many rounds
    worker_job_timeout_seconds: int = 14400          # the worker's limit for one whole run (all its candidates)
    round_fetch_deadline_seconds: float = 60.0       # how long a round waits for newly found boards to be read

    # Headless browser crawler (Playwright). Used only for pages that need JavaScript.
    browser_enabled: bool = True
    browser_concurrency: int = 6             # pages rendered at the same time
    max_browser_pages_per_search: int = 24
    max_crawl_links_per_search: int = 48     # job links followed from careers/listing pages (each is one page verification)

    # AI auto-apply (Admin only, docs/auto-apply-api.md). Off until switched on. The worker opens employer-hosted
    # application forms in the honest browser; nothing is submitted without an Admin approval of the exact preview.
    auto_apply_enabled: bool = False
    auto_apply_browser_concurrency: int = 1          # application browsers at the same time (one on the 4 GB server)
    auto_apply_employer_daily_cap: int = 5           # submissions per employer per 24 h, across all candidates
    auto_apply_run_timeout_seconds: float = 240.0    # one inspect + fill (or one submission) of one run
    auto_apply_host_interval_seconds: float = 20.0   # gap between two form openings on the same employer (pacing)
    auto_apply_screenshot_retention_days: int = 14   # screenshots are deleted after this; the run row stays
    auto_apply_screenshot_max_bytes: int = 600_000   # JPEG cap per screenshot (quality is lowered, then the page is cropped)
    auto_apply_max_runs_per_request: int = 20
    auto_apply_respect_robots: bool = True           # robots.txt of the form's host is read like the crawler does
    auto_apply_ai_classify: bool = True              # unknown question labels are classified by techsara-35b, then OpenAI/Claude
    auto_apply_draft_provider: str = "openai,claude"  # free-text drafting order (one call per run, one regeneration)

    # Abuse limits.
    max_concurrent_searches: int = 2
    searches_per_10_minutes: int = 10
    requests_per_minute: int = 240
    max_upload_bytes: int = 10 * 1024 * 1024
    resume_ttl_seconds: int = 3600

    @field_validator("openai_model", mode="before")
    @classmethod
    def _openai_model_default(cls, v):
        return v or DEFAULT_OPENAI_MODEL

    @field_validator("anthropic_model", mode="before")
    @classmethod
    def _anthropic_model_default(cls, v):
        return v or DEFAULT_ANTHROPIC_MODEL

    @field_validator("database_url", mode="before")
    @classmethod
    def _db_default(cls, v):
        return v or "postgresql+asyncpg://jobfinder:jobfinder@127.0.0.1:55432/jobfinder"

    @field_validator("search_max_results", mode="before")
    @classmethod
    def _cap_default(cls, v):
        return 1000 if v in ("", None) else v

    @property
    def sqlite_path(self) -> str:
        """SQLite file of the legacy single-user app (and the source of the one-off migration)."""
        url = self.database_url
        path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else self.legacy_sqlite_path
        if path == ":memory:":
            return path
        p = Path(path)
        return str(p if p.is_absolute() else BASE_DIR / p)

    @property
    def postgres_dsn(self) -> str:
        """Plain libpq-style DSN (for asyncpg) derived from DATABASE_URL."""
        if not self.database_url.startswith("postgresql"):
            raise ValueError("DATABASE_URL must be a postgresql+asyncpg:// URL for the portal")
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def secure_cookies(self) -> bool:
        return self.cookie_secure or self.is_production

    @property
    def salesforce_configured(self) -> bool:
        return bool(self.sf_client_id and self.sf_username and self.sf_login_url and self.sf_private_key_b64)

    @property
    def techsara_configured(self) -> bool:
        return bool(self.techsara_api_key and self.techsara_base_url)

    @property
    def result_cap(self) -> int:
        """Most results per candidate: the smaller of the engine ceiling and the per-candidate limit."""
        return max(1, min(self.search_max_results, self.max_target_count))

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)


@lru_cache
def get_settings() -> Settings:
    return Settings()
