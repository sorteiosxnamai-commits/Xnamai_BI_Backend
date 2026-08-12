from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./bi.db"
    mercos_adaptor_url: str = ""
    mercos_adaptor_api_key: str = ""
    bi_api_key: str = "change-me"
    cors_origins: str = "http://localhost:3000"
    sync_orders_minutes: int = 10
    sync_catalog_hours: int = 6
    log_level: str = "INFO"
    @property
    def origins(self): return [x.strip() for x in self.cors_origins.split(",") if x.strip()]

@lru_cache
def settings(): return Settings()

