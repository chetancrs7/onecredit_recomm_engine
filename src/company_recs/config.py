"""Runtime configuration, read from environment variables (and a local `.env` file if present)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """All settings the consumer and API need. Env var names match the field names, upper-cased."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://postgres:postgres@127.0.0.1:5432/companies"
    rabbitmq_url: str = "amqp://guest:guest@127.0.0.1:5672/"
    embed_model: str = "BAAI/bge-small-en-v1.5"
    top_k: int = Field(default=4, ge=1, le=50)
    log_level: LogLevel = "INFO"
    consumer_metrics_port: int = Field(default=9101, ge=1, le=65535)
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once."""
    return Settings()
