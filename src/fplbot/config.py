"""Runtime configuration, loaded from environment / .env."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    bot_token: str = ""
    owner_chat_id: int = 0

    # Webhook
    use_polling: bool = True
    webhook_base: str = ""
    webhook_secret: str = "dev-secret"
    web_host: str = "0.0.0.0"
    web_port: int = 8080

    # Storage
    database_url: str = "postgresql+asyncpg://fpl:fpl@localhost:5432/fpl"
    redis_url: str = "redis://localhost:6379/0"

    # Behaviour
    default_timezone: str = "Asia/Singapore"
    live_poll_seconds: int = 45
    fpl_max_concurrency: int = 5
    fpl_rate_per_sec: float = 4.0
    log_level: str = "INFO"

    @property
    def webhook_path(self) -> str:
        return f"/webhook/{self.webhook_secret}"

    @property
    def webhook_url(self) -> str:
        return f"{self.webhook_base.rstrip('/')}{self.webhook_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
