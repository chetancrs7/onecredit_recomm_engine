"""Runtime configuration, read from environment variables (and a local `.env` file if present)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """All settings the consumer and API need. Env var names match the field names, upper-cased."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Defaults match docker-compose.yml so local development works without a .env file.
    database_url: str = "postgresql://postgres:postgres@localhost:5432/companies"
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    embed_model: str = "BAAI/bge-small-en-v1.5"
    top_k: int = Field(default=4, ge=1, le=50)
    log_level: LogLevel = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once."""
    return Settings()
