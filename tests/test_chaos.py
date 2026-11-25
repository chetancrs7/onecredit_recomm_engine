"""Chaos: the consumer under at-least-once delivery and failures.

Two kinds of test, all against a real Postgres with the fake embedder:

- The real feed, `data/feed.jsonl` (2,518 messages, 2,000 companies), replayed through the
  message handler in order, reversed, shuffled and with redeliveries.
- Faults through a real RabbitMQ with the consumer running in a background thread: the same feed
  published twice, updates out of order, a malformed message, no AMQP message_id, and the
  database failing while messages are waiting.
"""

import random
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg
import pytest
from prometheus_client import REGISTRY
from support import (
    Database,
    Deliverer,
    FakeChannel,
    FakeEmbedder,
    RecordingConsumer,
    company_version,
    dead_letters,
    message_id_of,
    publish,
    queue_depth,
    running,
    synthetic_feed,
    upsert,
)

from company_recs import db as database
from company_recs.config import Settings
from company_recs.consumer import DEAD_LETTER_QUEUE, Status

pytestmark = [pytest.mark.integration, pytest.mark.chaos]

FEED = Path(__file__).resolve().parents[1] / "data" / "feed.jsonl"
FEED_LINES = FEED.read_bytes().splitlines()
FEED_COMPANIES = 2_000
FEED_POISON_MESSAGES = 37


def _replay(deliver: Deliverer, lines: list[bytes]) -> Counter[Status]:
    return Counter(deliver(line) for line in lines)


def _outcome() -> list[tuple[Any, ...]]:
    """What a reader of `companies` sees, minus fields that depend on arrival order."""
    with database.conn() as connection:
        return connection.execute(
            """
            SELECT id, name, is_active, source_updated_at, source_message_id,
                   embedding IS NOT NULL
            FROM companies ORDER BY id
            """
        ).fetchall()


def _reset() -> None:
    with database.conn() as connection:
        connection.execute(
            "TRUNCATE companies, processed_messages, company_events, pending_strike_offs"
        )


_in_order_cache: list[list[tuple[Any, ...]]] = []


def _in_order_outcome(deliver: Deliverer) -> list[tuple[Any, ...]]:
    """The outcome of the feed replayed once, in order; computed once per test run."""
    if not _in_order_cache:
        _replay(deliver, FEED_LINES)
        _in_order_cache.append(_outcome())
        _reset()
    return _in_order_cache[0]


def test_missing_category_new_company_inserts_null(deliver: Deliverer) -> None:
    deliver(upsert(industry=None))
    with database.conn() as connection:
        row = connection.execute(
            "SELECT category, embedding IS NOT NULL FROM companies WHERE id = 'SYN-000001'"
        ).fetchone()
    assert row == (None, True)


def test_missing_category_existing_company_keeps_previous(deliver: Deliverer) -> None:
    deliver(upsert(updated_at="2026-08-14T09:00:00+08:00", industry="Food & Beverage"))
    deliver(upsert(updated_at="2026-08-14T10:00:00+08:00", industry=None, companyName="New Name"))
    with database.conn() as connection:
        row = connection.execute(
            "SELECT name, category FROM companies WHERE id = 'SYN-000001'"
        ).fetchone()
    assert row == ("New Name", "Food & Beverage")


def test_full_feed_stores_every_company(deliver: Deliverer, channel: FakeChannel) -> None:
    statuses = _replay(deliver, FEED_LINES)
    outcome = _outcome()
    _in_order_cache[:] = [outcome]

    assert len(outcome) == FEED_COMPANIES
    assert all(has_embedding for *_, has_embedding in outcome)
    assert statuses[Status.DEAD_LETTERED] == FEED_POISON_MESSAGES
    assert len(channel.published) == FEED_POISON_MESSAGES
    assert {message.routing_key for message in channel.published} == {DEAD_LETTER_QUEUE}
    assert len(channel.acked) == len(FEED_LINES)
    assert channel.nacked == []
    assert sum(1 for row in outcome if row[2] is False) == statuses[Status.STRUCK_OFF] == 8
    assert statuses[Status.STALE] > 0


def test_redelivering_the_whole_feed_changes_nothing(deliver: Deliverer) -> None:
    _replay(deliver, FEED_LINES)
    before = _outcome()
    again = _replay(deliver, FEED_LINES)
    assert set(again) == {Status.DUPLICATE, Status.DEAD_LETTERED}
    assert _outcome() == before


def test_reversed_feed_matches_in_order(deliver: Deliverer) -> None:
    in_order = _in_order_outcome(deliver)
    _replay(deliver, FEED_LINES[::-1])
    assert _outcome() == in_order


def test_shuffled_feed_with_redeliveries_matches_in_order(deliver: Deliverer) -> None:
    in_order = _in_order_outcome(deliver)
    rng = random.Random(7)
    lines = list(FEED_LINES) + rng.sample(FEED_LINES, k=len(FEED_LINES) // 5)
    rng.shuffle(lines)
    _replay(deliver, lines)

    assert _outcome() == in_order


def _consumer(db: Database, url: str) -> RecordingConsumer:
    return RecordingConsumer(Settings(database_url=db.url, rabbitmq_url=url), embed=FakeEmbedder())


def _database_errors() -> float:
    return REGISTRY.get_sample_value("consumer_errors_total", {"type": "database"}) or 0.0


def test_duplicate_publish_is_idempotent(db: Database, queues: str) -> None:
    feed = synthetic_feed()
    consumer = _consumer(db, queues)
    with running(consumer):
        publish(queues, feed.bodies)
        consumer.wait_for(len(feed.bodies))
        counts, companies = db.counts(), db.companies()
        publish(queues, feed.bodies)
        consumer.wait_for(2 * len(feed.bodies))

    assert counts == (60, 80, 80)
    assert db.counts() == counts
    assert db.companies() == companies
    assert set(consumer.statuses[len(feed.bodies) :]) == {Status.DUPLICATE}


def test_out_of_order_updates_apply_newest(db: Database, queues: str) -> None:
    v2, expected_v2 = company_version(7, 2)
    v1, _ = company_version(7, 1)
    consumer = _consumer(db, queues)
    with running(consumer):
        publish(queues, [v2, v1])
        consumer.wait_for(2)

    assert consumer.statuses == [Status.STORED, Status.STALE]
    assert db.companies() == {"SYN-000007": expected_v2}


def test_malformed_lands_in_dlq(db: Database, queues: str) -> None:
    malformed = b'{"type": "company-upserted", "registrationNumber": '
    valid, _ = company_version(1, 1)
    consumer = _consumer(db, queues)
    with running(consumer) as thread:
        publish(queues, [malformed])
        consumer.wait_for(1)
        assert queue_depth(queues) == 0
        assert thread.is_alive() and not consumer.stopping
        publish(queues, [valid])
        consumer.wait_for(2)

    assert consumer.statuses == [Status.DEAD_LETTERED, Status.STORED]
    [(properties, body)] = dead_letters(queues)
    assert body == malformed
    assert properties.message_id == message_id_of(malformed)
    assert "error" in (properties.headers or {})
    assert db.count("companies") == 1


def test_missing_message_id_is_synthesized(db: Database, queues: str) -> None:
    body, _ = company_version(3, 1)
    consumer = _consumer(db, queues)
    with running(consumer):
        publish(queues, [body, body], with_message_id=False)
        consumer.wait_for(2)

    assert consumer.statuses == [Status.STORED, Status.DUPLICATE]
    processed = db.fetchall("SELECT message_id FROM processed_messages")
    assert processed == [(message_id_of(body),)]
    assert db.count("companies") == 1


def test_db_restart_recovery(db: Database, queues: str, monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = [company_version(number, 1)[0] for number in range(30)]
    publish(queues, bodies)

    real_get_pool = database.get_pool
    restarted: list[bool] = []

    def restarting_database() -> Any:
        if not restarted:
            restarted.append(True)
            raise psycopg.OperationalError("the database system is starting up")
        return real_get_pool()

    monkeypatch.setattr(database, "get_pool", restarting_database)
    errors_before = _database_errors()
    consumer = _consumer(db, queues)
    with running(consumer):
        consumer.wait_for(len(bodies))

    assert consumer.requeued == 1
    assert _database_errors() - errors_before == 1
    assert consumer.statuses == [Status.STORED] * len(bodies)
    assert db.counts() == (30, 30, 30)
    assert queue_depth(queues) == 0
