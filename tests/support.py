"""Test doubles for the consumer: a deterministic embedder and an in-memory channel."""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from pika.spec import BasicProperties

from company_recs.consumer import Consumer, Status
from company_recs.embedder import EMBED_DIM


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


class Deliverer:
    """Feeds messages to a Consumer the way RabbitMQ would, with the producer's message ids."""

    def __init__(self, consumer: Consumer, channel: FakeChannel) -> None:
        self.consumer = consumer
        self.channel = channel
        self._next_tag = 0

    def __call__(self, message: dict[str, Any] | bytes, message_id: str | None = "auto") -> Status:
        body = body_of(message)
        if message_id == "auto":
            message_id = hashlib.sha256(body).hexdigest()
        self._next_tag += 1
        return self.consumer.handle_delivery(self.channel, self._next_tag, message_id, body)
