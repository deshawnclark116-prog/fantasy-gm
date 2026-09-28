"""Runtime settings (environment variables prefixed ``FANTASY_GM_``)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FANTASY_GM_", env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "sqlite:///./fantasy_gm.sqlite3"
    log_level: str = "INFO"
    # Global kill switch for provider writes; defaults to OFF.
    execution_enabled: bool = False
