"""Application settings. All secrets come from the environment / .env, never from code."""
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
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
    search_max_results: int = 500

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
    email_backend: str = ""                          # "" = smtp when SMTP_HOST is set, otherwise none; "console" prints mails to the server log
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""
    bootstrap_admin_name: str = "Administrator"

    # Portal: team searches.
    max_members_per_search: int = 10
    max_results_per_search_run: int = 500            # members x results-per-member allowed in one run
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
    max_ai_calls_per_search: int = 30
    max_ai_ranked_jobs: int = 120
    ai_rank_batch_size: int = 6              # small batches answer faster; they run side by side
    ai_rank_concurrency: int = 10
    max_audit_jobs: int = 40
    max_page_verifications: int = 40
    max_ai_page_extractions: int = 8         # pages per search sent to the AI for job extraction (one batched call)
    ai_timeout_seconds: float = 180.0

    # Headless browser crawler (Playwright). Used only for pages that need JavaScript.
    browser_enabled: bool = True
    browser_concurrency: int = 6             # pages rendered at the same time
    max_browser_pages_per_search: int = 24
    max_crawl_links_per_search: int = 16     # job links followed from careers/listing pages

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
        return 500 if v in ("", None) else v

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
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)


@lru_cache
def get_settings() -> Settings:
    return Settings()
