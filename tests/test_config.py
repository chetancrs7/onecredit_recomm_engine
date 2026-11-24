from pathlib import Path

import pytest
from pydantic import ValidationError

from company_recs.config import Settings


@pytest.fixture(autouse=True)
def no_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run each test from an empty directory, so a developer's .env can't affect it."""
    monkeypatch.chdir(tmp_path)


def test_defaults_match_local_docker_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DATABASE_URL", "RABBITMQ_URL", "EMBED_MODEL", "TOP_K", "LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.database_url == "postgresql://postgres:postgres@127.0.0.1:5432/companies"
    assert settings.top_k == 4
    assert settings.log_level == "INFO"


def test_env_vars_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOP_K", "10")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    settings = Settings()
    assert settings.top_k == 10
    assert settings.log_level == "DEBUG"


def test_invalid_top_k_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOP_K", "0")
    with pytest.raises(ValidationError):
        Settings()
