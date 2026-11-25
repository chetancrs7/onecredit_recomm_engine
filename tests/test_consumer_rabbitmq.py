"""End to end through a real RabbitMQ: part of the real feed, the consumer, then Postgres.

The embedder fails once with a database error, so the run also covers the requeue, the
reconnect with backoff and the redelivery.
"""

from pathlib import Path

import psycopg
import pytest
from prometheus_client import REGISTRY
from support import (
    Database,
    FakeEmbedder,
    RecordingConsumer,
    dead_letters,
    message_id_of,
    publish,
    running,
)

from company_recs.config import Settings
from company_recs.models import parse_event

pytestmark = pytest.mark.integration

FEED = Path(__file__).resolve().parents[1] / "data" / "feed.jsonl"
SAMPLE = FEED.read_bytes().splitlines()[:400]


def _expected(bodies: list[bytes]) -> tuple[int, int, int]:
    """How many distinct valid messages, companies and poison messages `bodies` contain."""
    message_ids: set[str] = set()
    companies: set[str] = set()
    poison = 0
    for body in bodies:
        try:
            event = parse_event(body)
        except ValueError:
            poison += 1
            continue
        message_ids.add(message_id_of(body))
        if event.is_active:
            companies.add(event.id)
    return len(message_ids), len(companies), poison


def _database_errors() -> float:
    return REGISTRY.get_sample_value("consumer_errors_total", {"type": "database"}) or 0.0


def test_consumer_survives_an_outage_and_stores_the_feed(db: Database, queues: str) -> None:
    expected_messages, expected_companies, expected_poison = _expected(SAMPLE)
    fake = FakeEmbedder()
    failures = iter([psycopg.OperationalError("server closed the connection unexpectedly")])

    def embed_with_one_outage(text: str) -> list[float]:
        for failure in failures:
            raise failure
        return fake(text)

    publish(queues, SAMPLE)
    errors_before = _database_errors()
    consumer = RecordingConsumer(
        Settings(database_url=db.url, rabbitmq_url=queues), embed=embed_with_one_outage
    )
    with running(consumer):
        consumer.wait_for(len(SAMPLE))

    assert consumer.requeued == 1
    assert db.count("processed_messages") == expected_messages
    assert db.count("companies") == expected_companies
    assert _database_errors() - errors_before == 1
    letters = dead_letters(queues)
    assert len(letters) == expected_poison
    assert all("error" in (properties.headers or {}) for properties, _ in letters)
