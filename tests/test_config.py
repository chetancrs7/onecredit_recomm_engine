import pytest
from pydantic import ValidationError

from company_recs.config import Settings


def test_defaults_match_local_docker_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DATABASE_URL", "RABBITMQ_URL", "EMBED_MODEL", "TOP_K", "LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.database_url == "postgresql://postgres:postgres@localhost:5432/companies"
    assert settings.top_k == 4
    assert settings.log_level == "INFO"


def test_env_vars_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOP_K", "10")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.top_k == 10
    assert settings.log_level == "DEBUG"


def test_invalid_top_k_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOP_K", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
