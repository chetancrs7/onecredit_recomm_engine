"""Shared fixtures: one pgvector Postgres and one RabbitMQ container for the whole test run.

Containers start only when a test asks for them, so unit tests need no Docker.
"""

from collections.abc import AsyncIterator, Iterator
from typing import Final

import httpx
import pika
import pytest
from support import Database, Deliverer, FakeChannel, FakeEmbedder
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.rabbitmq import RabbitMqContainer

from company_recs import api
from company_recs import db as database
from company_recs.config import get_settings
from company_recs.consumer import DEAD_LETTER_QUEUE, SOURCE_QUEUE, Consumer, declare_queues

PGVECTOR_IMAGE: Final = "pgvector/pgvector:0.8.7-pg16"
RABBITMQ_IMAGE: Final = "rabbitmq:3.13.7-management"
TRUNCATE_ALL: Final = "TRUNCATE companies, processed_messages, company_events, pending_strike_offs"


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    with PostgresContainer(PGVECTOR_IMAGE, driver=None) as postgres:
        yield postgres


@pytest.fixture(scope="session")
def mq_container() -> Iterator[RabbitMqContainer]:
    with RabbitMqContainer(RABBITMQ_IMAGE) as rabbitmq:
        yield rabbitmq


@pytest.fixture(scope="session")
def db_url(pg_container: PostgresContainer) -> str:
    return pg_container.get_connection_url().replace("localhost", "127.0.0.1")


@pytest.fixture(scope="session")
def amqp_url(mq_container: RabbitMqContainer) -> str:
    host = mq_container.get_container_host_ip().replace("localhost", "127.0.0.1")
    port = mq_container.get_exposed_port(mq_container.port)
    return f"amqp://guest:guest@{host}:{port}/"


@pytest.fixture(scope="session")
def _schema(db_url: str) -> str:
    database.run_migrations(db_url)
    return db_url


@pytest.fixture
def migrated(_schema: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The migrated database, with Settings and the pool pointed at it."""
    monkeypatch.setenv("DATABASE_URL", _schema)
    get_settings.cache_clear()
    yield _schema
    database.close_pool()
    get_settings.cache_clear()


@pytest.fixture
def db(migrated: str) -> Database:
    """The migrated database with every table emptied, and helpers to query it."""
    with database.conn() as connection:
        connection.execute(TRUNCATE_ALL)
    return Database(migrated)


@pytest.fixture
def clean_db(db: Database) -> str:
    """The URL of the emptied database, for tests that only need it to exist."""
    return db.url


@pytest.fixture
def queues(amqp_url: str) -> str:
    """`company-events` and its dead-letter queue, declared and empty."""
    connection = pika.BlockingConnection(pika.URLParameters(amqp_url))
    try:
        channel = connection.channel()
        declare_queues(channel)
        channel.queue_purge(SOURCE_QUEUE)
        channel.queue_purge(DEAD_LETTER_QUEUE)
    finally:
        connection.close()
    return amqp_url


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def api_client(db: Database) -> AsyncIterator[httpx.AsyncClient]:
    """The API in-process over ASGI, against the emptied database."""
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


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
