"""End to end through a real RabbitMQ: publish part of the feed, run the consumer, check Postgres.

The embedder fails once with a database error, so the run also covers the requeue, the
reconnect with backoff and the redelivery.
"""

import hashlib
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pika
import psycopg
import pytest
from prometheus_client import REGISTRY
from psycopg import sql
from support import FakeEmbedder
from testcontainers.community.rabbitmq import RabbitMqContainer

from company_recs import db
from company_recs.config import Settings
from company_recs.consumer import DEAD_LETTER_QUEUE, SOURCE_QUEUE, Consumer
from company_recs.models import parse_event

pytestmark = pytest.mark.integration

RABBITMQ_IMAGE = "rabbitmq:3.13.7-management"
FEED = Path(__file__).resolve().parents[1] / "data" / "feed.jsonl"
SAMPLE = FEED.read_bytes().splitlines()[:400]
TIMEOUT_S = 90


@pytest.fixture(scope="module")
def rabbitmq_url() -> Iterator[str]:
    with RabbitMqContainer(RABBITMQ_IMAGE) as rabbitmq:
        host = rabbitmq.get_container_host_ip().replace("localhost", "127.0.0.1")
        port = rabbitmq.get_exposed_port(rabbitmq.port)
        yield f"amqp://guest:guest@{host}:{port}/"


def _publish(url: str, bodies: list[bytes]) -> None:
    """Publish like the producer: durable queue, no arguments, message_id = sha256(body)."""
    connection = pika.BlockingConnection(pika.URLParameters(url))
    try:
        channel = connection.channel()
        channel.queue_declare(SOURCE_QUEUE, durable=True)
        for body in bodies:
            channel.basic_publish(
                exchange="",
                routing_key=SOURCE_QUEUE,
                body=body,
                properties=pika.BasicProperties(
                    message_id=hashlib.sha256(body).hexdigest(),
                    content_type="application/json",
                    delivery_mode=pika.DeliveryMode.Persistent,
                ),
            )
    finally:
        connection.close()


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
        message_ids.add(hashlib.sha256(body).hexdigest())
        if event.is_active:
            companies.add(event.id)
    return len(message_ids), len(companies), poison


def _count(table: str) -> int:
    with db.conn() as connection:
        row = connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        ).fetchone()
    assert row is not None
    return int(row[0])


def _dead_letters(url: str) -> list[tuple[pika.BasicProperties, bytes]]:
    connection = pika.BlockingConnection(pika.URLParameters(url))
    try:
        channel = connection.channel()
        messages: list[tuple[pika.BasicProperties, bytes]] = []
        while True:
            method, properties, body = channel.basic_get(DEAD_LETTER_QUEUE, auto_ack=True)
            if method is None or properties is None or body is None:
                return messages
            messages.append((properties, body))
    finally:
        connection.close()


def _database_errors() -> float:
    return REGISTRY.get_sample_value("consumer_errors_total", {"type": "database"}) or 0.0


def test_consumer_survives_an_outage_and_stores_the_feed(clean_db: str, rabbitmq_url: str) -> None:
    expected_messages, expected_companies, expected_poison = _expected(SAMPLE)
    fake = FakeEmbedder()
    failures = iter([psycopg.OperationalError("server closed the connection unexpectedly")])

    def embed_with_one_outage(text: str) -> list[float]:
        for failure in failures:
            raise failure
        return fake(text)

    _publish(rabbitmq_url, SAMPLE)
    errors_before = _database_errors()
    consumer = Consumer(
        Settings(database_url=clean_db, rabbitmq_url=rabbitmq_url), embed=embed_with_one_outage
    )
    thread = threading.Thread(target=consumer.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + TIMEOUT_S
    while _count("processed_messages") < expected_messages and time.monotonic() < deadline:
        time.sleep(0.5)
    consumer.request_stop()
    thread.join(timeout=10)

    assert not thread.is_alive(), "consumer did not stop"
    assert _count("processed_messages") == expected_messages
    assert _count("companies") == expected_companies
    assert _database_errors() - errors_before == 1
    dead_letters = _dead_letters(rabbitmq_url)
    assert len(dead_letters) == expected_poison
    assert all("error" in (properties.headers or {}) for properties, _ in dead_letters)
