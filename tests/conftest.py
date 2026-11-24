"""Shared fixtures: one pgvector Postgres container for the whole test run."""

from collections.abc import Iterator

import pytest
from support import Deliverer, FakeChannel, FakeEmbedder
from testcontainers.community.postgres import PostgresContainer

from company_recs import db
from company_recs.config import get_settings
from company_recs.consumer import Consumer

PGVECTOR_IMAGE = "pgvector/pgvector:0.8.7-pg16"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer(PGVECTOR_IMAGE, driver=None) as postgres:
        yield postgres.get_connection_url()


@pytest.fixture
def migrated(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A migrated database, with Settings and the pool pointed at it."""
    db.run_migrations(database_url)
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    yield database_url
    db.close_pool()
    get_settings.cache_clear()


@pytest.fixture
def clean_db(migrated: str) -> str:
    """A migrated database with every table emptied."""
    with db.conn() as connection:
        connection.execute(
            "TRUNCATE companies, processed_messages, company_events, pending_strike_offs"
        )
    return migrated


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def deliver(clean_db: str, embedder: FakeEmbedder, channel: FakeChannel) -> Deliverer:
    """Hand messages to a Consumer backed by the clean test database and the fake embedder."""
    return Deliverer(Consumer(get_settings(), embed=embedder), channel)
