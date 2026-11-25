"""Test doubles and helpers: a deterministic embedder, an in-memory channel, a consumer that
records what it did, RabbitMQ publishing, database queries and a synthetic feed."""

import hashlib
import json
import random
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final, LiteralString

import numpy as np
import pika
from pika.spec import BasicProperties
from psycopg import sql

from company_recs import db as database
from company_recs.consumer import (
    DEAD_LETTER_QUEUE,
    SOURCE_QUEUE,
    Consumer,
    DatabaseUnavailableError,
    DeliveryChannel,
    Status,
)
from company_recs.embedder import EMBED_DIM

DRAIN_TIMEOUT_S: Final = 60.0
STOP_TIMEOUT_S: Final = 10.0


class FakeEmbedder:
    """Returns a unit vector seeded from the text, so equal texts get equal vectors."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, text: str) -> list[float]:
        self.calls.append(text)
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8])
        vector = np.random.default_rng(seed).standard_normal(EMBED_DIM)
        return [float(x) for x in vector / np.linalg.norm(vector)]


@dataclass
class Published:
    routing_key: str
    body: bytes
    properties: BasicProperties | None


@dataclass
class FakeChannel:
    """Records what the consumer acks, nacks and publishes."""

    acked: list[int] = field(default_factory=list[int])
    nacked: list[tuple[int, bool]] = field(default_factory=list[tuple[int, bool]])
    published: list[Published] = field(default_factory=list[Published])

    def basic_ack(self, delivery_tag: int = 0, multiple: bool = False) -> None:
        self.acked.append(delivery_tag)

    def basic_nack(
        self, delivery_tag: int = 0, multiple: bool = False, requeue: bool = True
    ) -> None:
        self.nacked.append((delivery_tag, requeue))

    def basic_publish(
        self,
        exchange: str,
        routing_key: str,
        body: bytes,
        properties: BasicProperties | None = None,
        mandatory: bool = False,
    ) -> None:
        self.published.append(Published(routing_key, body, properties))


def upsert(
    company_id: str = "SYN-000001",
    updated_at: str = "2026-08-14T09:00:00+08:00",
    **data: Any,
) -> dict[str, Any]:
    """A `company-upserted` feed message; `data` overrides the default company fields."""
    fields: dict[str, Any] = {
        "companyName": "Kopi Co Sdn. Bhd.",
        "natureOfBusiness": "Operating cafes and coffee shops",
        "state": "Selangor",
        "industry": "Food & Beverage",
    }
    fields.update(data)
    return {
        "type": "company-upserted",
        "registrationNumber": company_id,
        "updatedAt": updated_at,
        "data": fields,
    }


def strike_off(
    company_id: str = "SYN-000001", updated_at: str = "2026-08-14T16:00:00+08:00"
) -> dict[str, Any]:
    return {"type": "company-struck-off", "registrationNumber": company_id, "updatedAt": updated_at}


def body_of(message: dict[str, Any] | bytes) -> bytes:
    return message if isinstance(message, bytes) else json.dumps(message).encode()


def message_id_of(body: bytes) -> str:
    """The producer's message id: the SHA-256 of the body."""
    return hashlib.sha256(body).hexdigest()


class Deliverer:
    """Feeds messages to a Consumer the way RabbitMQ would, with the producer's message ids."""

    def __init__(self, consumer: Consumer, channel: FakeChannel) -> None:
        self.consumer = consumer
        self.channel = channel
        self._next_tag = 0

    def __call__(self, message: dict[str, Any] | bytes, message_id: str | None = "auto") -> Status:
        body = body_of(message)
        if message_id == "auto":
            message_id = message_id_of(body)
        self._next_tag += 1
        return self.consumer.handle_delivery(self.channel, self._next_tag, message_id, body)


class Database:
    """Read-side helpers for assertions against the test database."""

    def __init__(self, url: str) -> None:
        self.url = url

    def count(self, table: str) -> int:
        with database.conn() as connection:
            row = connection.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()
        assert row is not None
        return int(row[0])

    def fetchall(self, query: LiteralString, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with database.conn() as connection:
            return connection.execute(query, params).fetchall()

    def counts(self) -> tuple[int, int, int]:
        """Rows in companies, processed_messages and company_events."""
        return (
            self.count("companies"),
            self.count("processed_messages"),
            self.count("company_events"),
        )

    def companies(self) -> dict[str, tuple[Any, ...]]:
        """Each company's (name, category, state, description, source_updated_at), by id."""
        rows = self.fetchall(
            "SELECT id, name, category, state, description, source_updated_at FROM companies"
        )
        return {row[0]: tuple(row[1:]) for row in rows}


def publish(url: str, bodies: list[bytes], *, with_message_id: bool = True) -> None:
    """Publish like the producer: persistent JSON, message_id = sha256(body) unless turned off."""
    connection = pika.BlockingConnection(pika.URLParameters(url))
    try:
        channel = connection.channel()
        for body in bodies:
            channel.basic_publish(
                exchange="",
                routing_key=SOURCE_QUEUE,
                body=body,
                properties=pika.BasicProperties(
                    message_id=message_id_of(body) if with_message_id else None,
                    content_type="application/json",
                    delivery_mode=pika.DeliveryMode.Persistent,
                ),
            )
    finally:
        connection.close()


def queue_depth(url: str, queue: str = SOURCE_QUEUE) -> int:
    """Messages ready on `queue`, not counting ones delivered and not yet acked."""
    connection = pika.BlockingConnection(pika.URLParameters(url))
    try:
        method = connection.channel().queue_declare(queue, passive=True).method
        return int(method.message_count or 0)
    finally:
        connection.close()


def dead_letters(url: str) -> list[tuple[BasicProperties, bytes]]:
    """Take every message off the dead-letter queue."""
    connection = pika.BlockingConnection(pika.URLParameters(url))
    try:
        channel = connection.channel()
        messages: list[tuple[BasicProperties, bytes]] = []
        while True:
            method, properties, body = channel.basic_get(DEAD_LETTER_QUEUE, auto_ack=True)
            if method is None or properties is None or body is None:
                return messages
            messages.append((properties, body))
    finally:
        connection.close()


class RecordingConsumer(Consumer):
    """A Consumer that records the outcome of every delivery, so a test can wait for them."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.statuses: list[Status] = []
        self.requeued = 0
        self._changed = threading.Condition()

    def handle_delivery(
        self,
        channel: DeliveryChannel,
        delivery_tag: int,
        amqp_message_id: str | None,
        body: bytes,
    ) -> Status:
        try:
            status = super().handle_delivery(channel, delivery_tag, amqp_message_id, body)
        except DatabaseUnavailableError:
            with self._changed:
                self.requeued += 1
            raise
        with self._changed:
            self.statuses.append(status)
            self._changed.notify_all()
        return status

    def wait_for(self, handled: int, timeout_s: float = DRAIN_TIMEOUT_S) -> None:
        """Block until `handled` deliveries have finished (acked or dead-lettered)."""
        with self._changed:
            done = self._changed.wait_for(lambda: len(self.statuses) >= handled, timeout_s)
        assert done, f"only {len(self.statuses)} of {handled} messages handled in {timeout_s} s"


@contextmanager
def running(consumer: Consumer) -> Iterator[threading.Thread]:
    """Run `consumer` in a background thread; on exit, stop it and check that it stopped."""
    thread = threading.Thread(target=consumer.run, name="consumer", daemon=True)
    thread.start()
    try:
        yield thread
    finally:
        consumer.request_stop()
        thread.join(STOP_TIMEOUT_S)
    assert not thread.is_alive(), "consumer did not stop"


FEED_START: Final = datetime(2026, 8, 14, 9, tzinfo=timezone(timedelta(hours=8)))
_STATES: Final = ("Selangor", "Johor", "Penang", "Sabah", "Perak", "Kuala Lumpur")
_CATEGORIES: Final = ("Food & Beverage", "Logistics", "Retail", "Construction", "Technology")
_ACTIVITIES: Final = (
    "Operating cafes and coffee shops",
    "Freight forwarding and warehousing",
    "Retail of household goods",
    "Building and civil engineering works",
    "Software development and IT consulting",
    "Bakery and confectionery",
    "Courier and last-mile delivery",
)


@dataclass(frozen=True)
class SyntheticFeed:
    """A deterministic feed with known duplicates and reordering, and what it should produce."""

    bodies: list[bytes]
    unique_message_ids: set[str]
    newest: dict[str, tuple[Any, ...]]
    out_of_order_ids: list[str]


def company_version(number: int, version: int) -> tuple[bytes, tuple[Any, ...]]:
    """Version `version` of company `number`: the message body and the row it should produce.

    A higher version has a later `updatedAt` and different name, category, state and description.
    """
    updated_at = FEED_START + timedelta(hours=version, minutes=number)
    expected = (
        f"Company {number:02d} v{version} Sdn. Bhd.",
        _CATEGORIES[(number + version) % len(_CATEGORIES)],
        _STATES[(number + version) % len(_STATES)],
        f"{_ACTIVITIES[(number + version) % len(_ACTIVITIES)]} (company {number}, v{version})",
        updated_at,
    )
    message = upsert(
        f"SYN-{number:06d}",
        updated_at.isoformat(),
        companyName=expected[0],
        industry=expected[1],
        state=expected[2],
        natureOfBusiness=expected[3],
    )
    return json.dumps(message).encode(), expected


def synthetic_feed(seed: int = 20261125) -> SyntheticFeed:
    """100 messages about 60 companies.

    - Companies 0-54 get a v1; companies 0-14 later get a newer v2, in order.
    - Companies 55-59 are the 5 out-of-order pairs: v2 is published before the older v1.
    - 20 of the 80 distinct messages are published a second time, at a random later position,
      with the same message_id.
    """
    ordered: list[bytes] = []
    newest: dict[str, tuple[Any, ...]] = {}

    def add(number: int, version: int) -> None:
        body, expected = company_version(number, version)
        ordered.append(body)
        company_id = f"SYN-{number:06d}"
        if company_id not in newest or expected[-1] > newest[company_id][-1]:
            newest[company_id] = expected

    for number in range(55):
        add(number, 1)
    for number in range(55, 60):
        add(number, 2)
        add(number, 1)
    for number in range(15):
        add(number, 2)

    rng = random.Random(seed)
    bodies = list(ordered)
    for original in rng.sample(ordered, k=20):
        first = bodies.index(original)
        bodies.insert(rng.randint(first + 1, len(bodies)), original)

    assert len(bodies) == 100 and len(set(bodies)) == 80 and len(newest) == 60
    return SyntheticFeed(
        bodies=bodies,
        unique_message_ids={message_id_of(body) for body in ordered},
        newest=newest,
        out_of_order_ids=[f"SYN-{number:06d}" for number in range(55, 60)],
    )
