from functools import lru_cache
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine.url import make_url

# libpq/psycopg accept these; ORM helpers like pgbouncer=true must be stripped
_ALLOWED_QUERY_KEYS = {
    "sslmode",
    "sslrootcert",
    "sslcert",
    "sslkey",
    "sslcrl",
    "gssencmode",
    "channel_binding",
    "connect_timeout",
    "application_name",
    "options",
    "target_session_attrs",
}


def normalize_database_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        return value

    url = value.strip().strip('"').strip("'")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    elif not url.startswith("postgresql+psycopg://") and "://" not in url:
        # already a driver URL or sqlite — leave non-postgres alone below
        pass

    if not url.startswith("postgresql+psycopg://"):
        return url

    parsed = make_url(url)
    query = {k: v for k, v in parsed.query.items() if k in _ALLOWED_QUERY_KEYS}

    host = (parsed.host or "").lower()
    if "supabase.com" in host or "supabase.co" in host:
        query.setdefault("sslmode", "require")
        query.setdefault("gssencmode", "disable")

    return parsed.set(query=query).render_as_string(hide_password=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./bi.db"
    mercos_adaptor_url: str = ""
    mercos_adaptor_api_key: str = ""
    bi_api_key: str = "change-me"
    jwt_secret: str = "change-me-in-production"
    auth_admin_username: str = "admin@xnamai.com"
    auth_admin_password: str = "123456"
    auth_viewer_username: str = "viewer"
    auth_viewer_password: str = ""
    auth_access_minutes: int = 15
    auth_refresh_days: int = 7
    auth_cookie_secure: bool = True
    auth_cookie_samesite: str = "none"
    cors_origins: str = "http://localhost:5173,https://xnamai-bi-frontend.vercel.app"
    sync_orders_minutes: int = 10
    sync_catalog_hours: int = 6
    log_level: str = "INFO"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    redis_url: str = ""
    retail_concurrency: int = 5
    retail_cache_ttl_seconds: int = 120

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url_field(cls, value: str) -> str:
        return normalize_database_url(value)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def normalize_cors_origins(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            return "http://localhost:5173,https://xnamai-bi-frontend.vercel.app"
        return value.strip().strip('"').strip("'")

    @field_validator("auth_cookie_samesite")
    @classmethod
    def validate_cookie_samesite(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in {"lax", "strict", "none"}:
            raise ValueError("AUTH_COOKIE_SAMESITE deve ser lax, strict ou none")
        return normalized

    @property
    def origins(self):
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]


@lru_cache
def settings():
    return Settings()
