"""Application settings. Every secret comes from the environment (spec §28)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    app_env: Literal["development", "staging", "production", "test"] = "development"
    app_name: str = "AdNet"
    secret_key: str = "dev-only-insecure-key"
    base_url: str = "http://localhost:8000"

    database_url: str = "postgresql+psycopg://adnet:adnet@localhost:5432/adnet"
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_statement_timeout_ms: int = 15_000

    redis_url: str = "redis://localhost:6379/0"

    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    telegram_webhook_secret: str = ""
    telegram_webhook_path: str = "/telegram/webhook"

    mtproto_enabled: bool = False
    mtproto_api_id: str = ""
    mtproto_api_hash: str = ""
    mtproto_session: str = ""

    default_currency: str = "BDT"

    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""
    admin_require_2fa: bool = True
    admin_session_max_age: int = 8 * 3600

    allowed_hosts: str = "*"
    cors_origins: str = ""
    rate_limit_enabled: bool = True

    @field_validator("secret_key")
    @classmethod
    def _reject_default_secret_in_prod(cls, v: str, info) -> str:
        if info.data.get("app_env") == "production" and v == "dev-only-insecure-key":
            raise ValueError("SECRET_KEY must be set in production")
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
