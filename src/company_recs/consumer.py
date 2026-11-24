"""The `company-events` consumer: applies each registry event to Postgres, with its embedding.

Run with `python -m company_recs.consumer` (or `just consumer`).

Delivery is at-least-once, so every message is handled the same way whether it is new, a
redelivery or a duplicate:

1. Parse it. A message that is not a usable event is copied to `company-events-dlq` with an
   `error` header and acked, so a poison message never blocks the queue.
2. Pick its id: the AMQP `message_id`, else the event's own `message_id`, else the SHA-256 of
   the body.
3. In one transaction: record the id in `processed_messages` (a second delivery stops here),
   lock the company's row, drop the event if it is older than what is stored, embed only if the
   text to embed changed, upsert, and write the raw event to `company_events`.
4. Ack only after the commit. A crash before the ack means a redelivery, which step 3 turns into
   a no-op.

If Postgres or RabbitMQ is unavailable the message is nacked back onto the queue, both connections
are dropped and the consumer reconnects with exponential backoff (1 s doubling to 30 s).
"""

import hashlib
import json
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import FrameType
from typing import Any, Final, Protocol

import pika
import pika.exceptions
import psycopg
import structlog
from pgvector import Vector
from pika.adapters.blocking_connection import BlockingChannel, BlockingConnection
from pika.spec import Basic, BasicProperties
from prometheus_client import Counter, Histogram, start_http_server
from psycopg.types.json import Jsonb

from company_recs import db
from company_recs.config import Settings, get_settings
from company_recs.embedder import build_embed_text, description_hash, embed_one, get_model
from company_recs.logs import configure_logging
from company_recs.models import CompanyEvent, parse_event

SOURCE_QUEUE: Final = "company-events"
DEAD_LETTER_QUEUE: Final = "company-events-dlq"
PREFETCH_COUNT: Final = 32
BACKOFF_INITIAL_S: Final = 1.0
BACKOFF_MAX_S: Final = 30.0
POLL_INTERVAL_S: Final = 1.0
MAX_ERROR_HEADER_LENGTH: Final = 1000
STOP_SIGNALS: Final = ("SIGINT", "SIGTERM", "SIGBREAK")

EVENTS_CONSUMED: Final = Counter(
    "events_consumed_total", "Queue messages handled, by outcome.", ["status"]
)
CONSUMER_ERRORS: Final = Counter(
    "consumer_errors_total", "Errors while consuming, by kind.", ["type"]
)
EMBEDDINGS: Final = Counter(
    "embeddings_total", "Embeddings stored, by where the vector came from.", ["source"]
)
EVENT_SECONDS: Final = Histogram("event_processing_seconds", "Time to handle one queue message.")

type Embed = Callable[[str], list[float]]

log = structlog.get_logger()


class Status(StrEnum):
    """What happened to one queue message; the `status` label of `events_consumed_total`."""

    STORED = "stored"
    STRUCK_OFF = "struck_off"
    STALE = "stale"
    STRIKE_OFF_PENDING = "strike_off_pending"
    DUPLICATE = "duplicate"
    DEAD_LETTERED = "dead_lettered"
    REQUEUED = "requeued"


class ErrorType(StrEnum):
    """The `type` label of `consumer_errors_total`."""

    PARSE = "parse"
    DATABASE = "database"
    BROKER = "broker"
    UNEXPECTED = "unexpected"


class DatabaseUnavailableError(Exception):
    """Postgres could not be reached; the message was requeued and the consumer must reconnect."""


class DeliveryChannel(Protocol):
    """The part of a pika channel the message handler uses."""

    def basic_ack(self, delivery_tag: int = 0, multiple: bool = False) -> None: ...

    def basic_nack(
        self, delivery_tag: int = 0, multiple: bool = False, requeue: bool = True
    ) -> None: ...

    def basic_publish(
        self,
        exchange: str,
        routing_key: str,
        body: bytes,
        properties: BasicProperties | None = None,
        mandatory: bool = False,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _StoredCompany:
    category: str | None
    state: str | None
    description: str | None
    description_hash: str | None
    has_embedding: bool
    source_updated_at: datetime
    source_message_id: str


_RECORD_MESSAGE_SQL: Final = """
INSERT INTO processed_messages (message_id) VALUES (%s) ON CONFLICT DO NOTHING
"""

_LOCK_COMPANY_SQL: Final = """
SELECT category, state, description, description_hash, embedding IS NOT NULL,
       source_updated_at, source_message_id
FROM companies
WHERE id = %s
FOR UPDATE
"""

_FIND_EMBEDDING_SQL: Final = """
SELECT embedding FROM companies
WHERE description_hash = %s AND embedding IS NOT NULL
LIMIT 1
"""

_UPSERT_COMPANY_SQL: Final = """
INSERT INTO companies (
    id, name, category, state, description, description_hash, embedding,
    source_updated_at, source_message_id, is_active
)
VALUES (
    %(id)s, %(name)s, %(category)s, %(state)s, %(description)s, %(description_hash)s,
    %(embedding)s, %(source_updated_at)s, %(source_message_id)s, %(is_active)s
)
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    category = COALESCE(EXCLUDED.category, companies.category),
    state = COALESCE(EXCLUDED.state, companies.state),
    description = COALESCE(EXCLUDED.description, companies.description),
    description_hash = EXCLUDED.description_hash,
    embedding = COALESCE(EXCLUDED.embedding, companies.embedding),
    source_updated_at = EXCLUDED.source_updated_at,
    source_message_id = EXCLUDED.source_message_id,
    is_active = EXCLUDED.is_active,
    ingested_at = now()
WHERE EXCLUDED.source_updated_at > companies.source_updated_at
   OR (EXCLUDED.source_updated_at = companies.source_updated_at
       AND EXCLUDED.source_message_id > companies.source_message_id)
"""

_STRIKE_OFF_SQL: Final = """
UPDATE companies
SET is_active = false, source_updated_at = %s, source_message_id = %s, ingested_at = now()
WHERE id = %s
"""

_HOLD_STRIKE_OFF_SQL: Final = """
INSERT INTO pending_strike_offs (company_id, source_updated_at, source_message_id)
VALUES (%s, %s, %s)
ON CONFLICT (company_id) DO UPDATE SET
    source_updated_at = EXCLUDED.source_updated_at,
    source_message_id = EXCLUDED.source_message_id
WHERE (EXCLUDED.source_updated_at, EXCLUDED.source_message_id)
    > (pending_strike_offs.source_updated_at, pending_strike_offs.source_message_id)
"""

_TAKE_STRIKE_OFF_SQL: Final = """
DELETE FROM pending_strike_offs WHERE company_id = %s
RETURNING source_updated_at, source_message_id
"""

_AUDIT_SQL: Final = """
INSERT INTO company_events (message_id, company_id, raw) VALUES (%s, %s, %s)
"""


def resolve_message_id(amqp_message_id: str | None, event: CompanyEvent, body: bytes) -> str:
    """The id used for deduplication: AMQP `message_id`, else the event's, else the body hash."""
    return amqp_message_id or event.message_id or hashlib.sha256(body).hexdigest()


def _is_newer(event: CompanyEvent, message_id: str, stored: _StoredCompany) -> bool:
    """Last write wins on `updatedAt`; equal timestamps are ordered by message id."""
    return (event.updatedAt, message_id) > (stored.source_updated_at, stored.source_message_id)


def _lock_company(connection: db.Connection, company_id: str) -> _StoredCompany | None:
    row = connection.execute(_LOCK_COMPANY_SQL, (company_id,)).fetchone()
    if row is None:
        return None
    return _StoredCompany(*row)


def _embedding_for(
    connection: db.Connection,
    text: str,
    text_hash: str,
    stored: _StoredCompany | None,
    embed: Embed,
) -> Vector | None:
    """The vector to store, or `None` to keep the one already stored for this company.

    The model only runs for text no company has been embedded from yet: if another company has
    the same embed text, its vector is copied.
    """
    if stored is not None and stored.has_embedding and stored.description_hash == text_hash:
        EMBEDDINGS.labels("kept").inc()
        return None
    row = connection.execute(_FIND_EMBEDDING_SQL, (text_hash,)).fetchone()
    if row is not None:
        EMBEDDINGS.labels("copied").inc()
        return row[0]
    EMBEDDINGS.labels("computed").inc()
    return Vector(embed(text))


def _take_pending_strike_off(
    connection: db.Connection, event: CompanyEvent, message_id: str
) -> tuple[datetime, str] | None:
    """A strike-off that arrived before the company's first upsert, if it is newer than `event`.

    Without this, an upsert delayed behind the strike-off would bring a struck-off company back.
    """
    row = connection.execute(_TAKE_STRIKE_OFF_SQL, (event.id,)).fetchone()
    if row is None:
        return None
    struck_off_at: datetime = row[0]
    struck_off_message_id: str = row[1]
    if (struck_off_at, struck_off_message_id) > (event.updatedAt, message_id):
        return struck_off_at, struck_off_message_id
    return None


def _or_stored(value: str | None, stored_value: str | None) -> str | None:
    """A field the event leaves out keeps the stored value instead of clearing it."""
    return value if value is not None else stored_value


def _store_company(
    connection: db.Connection,
    event: CompanyEvent,
    message_id: str,
    stored: _StoredCompany | None,
    embed: Embed,
) -> Status:
    """Upsert the company; returns `STRUCK_OFF` if a held strike-off is newer than the event."""
    struck_off = (
        None if stored is not None else _take_pending_strike_off(connection, event, message_id)
    )
    name = event.name or ""
    category = _or_stored(event.category, stored.category if stored else None)
    state = _or_stored(event.state, stored.state if stored else None)
    description = _or_stored(event.description, stored.description if stored else None)
    text = build_embed_text(name, category, state, description)
    text_hash = description_hash(text)
    connection.execute(
        _UPSERT_COMPANY_SQL,
        {
            "id": event.id,
            "name": name,
            "category": category,
            "state": state,
            "description": description,
            "description_hash": text_hash,
            "embedding": _embedding_for(connection, text, text_hash, stored, embed),
            "source_updated_at": struck_off[0] if struck_off else event.updatedAt,
            "source_message_id": struck_off[1] if struck_off else message_id,
            "is_active": struck_off is None,
        },
    )
    return Status.STORED if struck_off is None else Status.STRUCK_OFF


def process_event(
    connection: db.Connection,
    event: CompanyEvent,
    message_id: str,
    raw: Any,
    embed: Embed = embed_one,
) -> Status:
    """Apply one parsed event in a single transaction and return what happened.

    A message id seen before is a duplicate and changes nothing. An event older than the stored
    row (by `updatedAt`, then message id) is audited but not applied. A field the event leaves out
    (`category`, `state`, `description`) keeps the stored value instead of clearing it. A strike-off
    for a company not stored yet is held in `pending_strike_offs` until its first upsert arrives.
    """
    with connection.transaction():
        recorded = connection.execute(_RECORD_MESSAGE_SQL, (message_id,))
        if recorded.rowcount == 0:
            return Status.DUPLICATE
        stored = _lock_company(connection, event.id)
        if stored is not None and not _is_newer(event, message_id, stored):
            status = Status.STALE
        elif not event.is_active:
            if stored is None:
                connection.execute(_HOLD_STRIKE_OFF_SQL, (event.id, event.updatedAt, message_id))
                status = Status.STRIKE_OFF_PENDING
            else:
                connection.execute(_STRIKE_OFF_SQL, (event.updatedAt, message_id, event.id))
                status = Status.STRUCK_OFF
        else:
            status = _store_company(connection, event, message_id, stored, embed)
        connection.execute(_AUDIT_SQL, (message_id, event.id, Jsonb(raw)))
    return status


def declare_queues(channel: BlockingChannel) -> None:
    """Declare the source queue exactly as the producer does, and our own dead-letter queue.

    The registry team owns `company-events` and declares it durable with no arguments. Declaring
    it here with any other arguments, such as `x-dead-letter-exchange`, makes RabbitMQ refuse with
    `PRECONDITION_FAILED` and close the channel. So dead-lettering is done by this consumer, which
    publishes the bad message to `company-events-dlq`, a durable queue it owns, then acks it.
    """
    channel.queue_declare(SOURCE_QUEUE, durable=True)
    channel.queue_declare(DEAD_LETTER_QUEUE, durable=True)


class Consumer:
    """Consumes `company-events` until asked to stop, reconnecting whenever a dependency fails."""

    def __init__(self, settings: Settings | None = None, embed: Embed = embed_one) -> None:
        self._settings = settings or get_settings()
        self._embed = embed
        self._stopping = threading.Event()
        self._connection: BlockingConnection | None = None
        self._backoff_s = BACKOFF_INITIAL_S

    def request_stop(self) -> None:
        """Stop once the messages in hand are handled. Safe to call from a signal handler."""
        self._stopping.set()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def run(self) -> None:
        """Consume until `request_stop()`, reconnecting with exponential backoff after failures."""
        while not self._stopping.is_set():
            try:
                self._consume()
            except DatabaseUnavailableError:
                pass
            except pika.exceptions.AMQPError as exc:
                CONSUMER_ERRORS.labels(ErrorType.BROKER).inc()
                log.warning("broker unavailable", error=repr(exc))
            finally:
                self._close()
            if self._stopping.is_set():
                break
            log.info("reconnecting", backoff_s=self._backoff_s)
            self._stopping.wait(self._backoff_s)
            self._backoff_s = min(self._backoff_s * 2, BACKOFF_MAX_S)
        log.info("consumer stopped")

    def _consume(self) -> None:
        self._connection = pika.BlockingConnection(pika.URLParameters(self._settings.rabbitmq_url))
        channel = self._connection.channel()
        declare_queues(channel)
        channel.confirm_delivery()
        channel.basic_qos(prefetch_count=PREFETCH_COUNT)
        channel.basic_consume(SOURCE_QUEUE, self._on_message, auto_ack=False)
        log.info("consuming", queue=SOURCE_QUEUE, prefetch_count=PREFETCH_COUNT)
        while not self._stopping.is_set():
            self._connection.process_data_events(time_limit=POLL_INTERVAL_S)

    def _close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None and connection.is_open:
            try:
                connection.close()
            except pika.exceptions.AMQPError:
                pass

    def _on_message(
        self,
        channel: BlockingChannel,
        method: Basic.Deliver,
        properties: BasicProperties,
        body: bytes,
    ) -> None:
        message_id = properties.message_id
        if isinstance(message_id, bytes):
            message_id = message_id.decode("utf-8", "replace")
        self.handle_delivery(channel, method.delivery_tag or 0, message_id, body)

    def handle_delivery(
        self,
        channel: DeliveryChannel,
        delivery_tag: int,
        amqp_message_id: str | None,
        body: bytes,
    ) -> Status:
        """Handle one message end to end, including the ack, nack or dead-letter."""
        started = time.perf_counter()
        try:
            status = self._handle(channel, delivery_tag, amqp_message_id, body)
        finally:
            EVENT_SECONDS.observe(time.perf_counter() - started)
        EVENTS_CONSUMED.labels(status).inc()
        return status

    def _handle(
        self,
        channel: DeliveryChannel,
        delivery_tag: int,
        amqp_message_id: str | None,
        body: bytes,
    ) -> Status:
        logger = log.bind(message_id=amqp_message_id, company_id=None)
        try:
            event = parse_event(body)
            raw: Any = json.loads(body)
        except ValueError as exc:
            CONSUMER_ERRORS.labels(ErrorType.PARSE).inc()
            return self._dead_letter(channel, delivery_tag, amqp_message_id, body, exc, logger)

        message_id = resolve_message_id(amqp_message_id, event, body)
        logger = logger.bind(message_id=message_id, company_id=event.id)
        try:
            with db.conn() as connection:
                status = process_event(connection, event, message_id, raw, self._embed)
        except psycopg.OperationalError as exc:
            CONSUMER_ERRORS.labels(ErrorType.DATABASE).inc()
            EVENTS_CONSUMED.labels(Status.REQUEUED).inc()
            logger.warning("database unavailable, requeueing", error=repr(exc))
            channel.basic_nack(delivery_tag=delivery_tag, requeue=True)
            raise DatabaseUnavailableError from exc
        except Exception as exc:
            CONSUMER_ERRORS.labels(ErrorType.UNEXPECTED).inc()
            logger.exception("could not apply event")
            return self._dead_letter(channel, delivery_tag, message_id, body, exc, logger)

        channel.basic_ack(delivery_tag=delivery_tag)
        self._backoff_s = BACKOFF_INITIAL_S
        logger.info("event handled", status=status.value)
        return status

    def _dead_letter(
        self,
        channel: DeliveryChannel,
        delivery_tag: int,
        message_id: str | None,
        body: bytes,
        error: Exception,
        logger: Any,
    ) -> Status:
        reason = f"{type(error).__name__}: {error}"[:MAX_ERROR_HEADER_LENGTH]
        channel.basic_publish(
            exchange="",
            routing_key=DEAD_LETTER_QUEUE,
            body=body,
            properties=BasicProperties(
                headers={"error": reason},
                message_id=message_id,
                delivery_mode=pika.DeliveryMode.Persistent,
            ),
        )
        channel.basic_ack(delivery_tag=delivery_tag)
        logger.warning("dead-lettered", error=reason)
        return Status.DEAD_LETTERED


def main() -> None:
    """Migrate, load the model, serve metrics, then consume until a stop signal arrives.

    SIGINT (Ctrl+C) and SIGTERM stop it everywhere; SIGBREAK (Ctrl+Break) also does on Windows.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    db.run_migrations()
    get_model()
    start_http_server(settings.consumer_metrics_port)
    consumer = Consumer(settings)

    def stop(signum: int, _frame: FrameType | None) -> None:
        log.info("stop requested", signal=signal.Signals(signum).name)
        consumer.request_stop()

    for name in STOP_SIGNALS:
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
    log.info("consumer starting", metrics_port=settings.consumer_metrics_port)
    try:
        consumer.run()
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
