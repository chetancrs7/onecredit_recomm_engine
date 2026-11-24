"""Integration tests for the consumer's message handling, against a real Postgres.

The embedder is a fake (see `support.FakeEmbedder`) so the tests are fast and count model calls.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from psycopg import sql
from support import Deliverer, FakeChannel, FakeEmbedder, strike_off, upsert

from company_recs import consumer, db
from company_recs.consumer import DEAD_LETTER_QUEUE, DatabaseUnavailableError, Status

pytestmark = pytest.mark.integration


def _company(company_id: str = "SYN-000001") -> dict[str, Any]:
    with db.conn() as connection:
        cursor = connection.execute(
            """
            SELECT id, name, category, state, description, description_hash,
                   embedding IS NOT NULL AS has_embedding, source_updated_at,
                   source_message_id, is_active
            FROM companies WHERE id = %s
            """,
            (company_id,),
        )
        row = cursor.fetchone()
        assert row is not None, f"{company_id} is not stored"
        names = [column.name for column in cursor.description or []]
    return dict(zip(names, row, strict=True))


def _count(table: str) -> int:
    with db.conn() as connection:
        row = connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        ).fetchone()
    assert row is not None
    return int(row[0])


class TestStoring:
    def test_new_company_is_stored_with_an_embedding(
        self, deliver: Deliverer, channel: FakeChannel
    ) -> None:
        assert deliver(upsert()) is Status.STORED
        company = _company()
        assert company["name"] == "Kopi Co Sdn. Bhd."
        assert company["category"] == "Food & Beverage"
        assert company["has_embedding"] is True
        assert company["is_active"] is True
        assert channel.acked == [1]
        assert (_count("processed_messages"), _count("company_events")) == (1, 1)

    def test_newer_event_replaces_the_company(self, deliver: Deliverer) -> None:
        deliver(upsert(updated_at="2026-08-14T09:00:00+08:00"))
        deliver(
            upsert(
                updated_at="2026-08-14T10:00:00+08:00",
                companyName="Kopi Co Holdings",
                natureOfBusiness="Roasting and wholesale of coffee beans",
            )
        )
        company = _company()
        assert company["name"] == "Kopi Co Holdings"
        assert company["description"] == "Roasting and wholesale of coffee beans"

    def test_audit_row_keeps_the_raw_event(self, deliver: Deliverer) -> None:
        deliver(upsert(company_id=" syn-000001 "))
        with db.conn() as connection:
            row = connection.execute("SELECT company_id, raw FROM company_events").fetchone()
        assert row is not None
        assert row[0] == "SYN-000001"
        assert row[1]["registrationNumber"] == " syn-000001 "


class TestAtLeastOnceDelivery:
    def test_redelivery_is_a_duplicate(self, deliver: Deliverer, channel: FakeChannel) -> None:
        assert deliver(upsert(), message_id="m-1") is Status.STORED
        assert deliver(upsert(), message_id="m-1") is Status.DUPLICATE
        assert channel.acked == [1, 2]
        assert (_count("processed_messages"), _count("company_events")) == (1, 1)

    def test_message_id_falls_back_to_the_body_hash(self, deliver: Deliverer) -> None:
        assert deliver(upsert(), message_id=None) is Status.STORED
        assert deliver(upsert(), message_id=None) is Status.DUPLICATE

    def test_event_message_id_is_used_when_amqp_has_none(self, deliver: Deliverer) -> None:
        message = {**upsert(), "message_id": "from-the-event"}
        deliver(message, message_id=None)
        assert _company()["source_message_id"] == "from-the-event"


class TestOrdering:
    def test_older_event_is_stale(self, deliver: Deliverer) -> None:
        deliver(upsert(updated_at="2026-08-14T10:00:00+08:00", companyName="Newer Name"))
        status = deliver(upsert(updated_at="2026-08-14T09:00:00+08:00", companyName="Older Name"))
        assert status is Status.STALE
        assert _company()["name"] == "Newer Name"
        assert _count("company_events") == 2

    def test_timestamps_in_different_formats_compare_as_instants(self, deliver: Deliverer) -> None:
        deliver(upsert(updated_at="2026-08-14T10:00:00+08:00", companyName="Newer Name"))
        status = deliver(upsert(updated_at="2026-08-14T01:30:00Z", companyName="Older Name"))
        assert status is Status.STALE
        assert _company()["name"] == "Newer Name"

    @pytest.mark.parametrize("order", [("m-a", "m-b"), ("m-b", "m-a")])
    def test_equal_timestamps_are_ordered_by_message_id(
        self, deliver: Deliverer, order: tuple[str, str]
    ) -> None:
        names = {"m-a": "From A", "m-b": "From B"}
        for message_id in order:
            deliver(upsert(companyName=names[message_id]), message_id=message_id)
        assert _company()["name"] == "From B"


class TestEmbeddingCost:
    def test_unchanged_text_is_not_re_embedded(
        self, deliver: Deliverer, embedder: FakeEmbedder
    ) -> None:
        deliver(upsert(updated_at="2026-08-14T09:00:00+08:00"))
        deliver(upsert(updated_at="2026-08-14T10:00:00+08:00", companyName="Renamed Co"))
        assert len(embedder.calls) == 1
        assert _company()["has_embedding"] is True

    def test_changed_text_is_re_embedded(self, deliver: Deliverer, embedder: FakeEmbedder) -> None:
        deliver(upsert(updated_at="2026-08-14T09:00:00+08:00"))
        deliver(upsert(updated_at="2026-08-14T10:00:00+08:00", natureOfBusiness="Bakery"))
        assert embedder.calls == ["Operating cafes and coffee shops", "Bakery"]

    def test_text_another_company_has_is_copied(
        self, deliver: Deliverer, embedder: FakeEmbedder
    ) -> None:
        deliver(upsert(company_id="SYN-000001"))
        deliver(upsert(company_id="SYN-000002", companyName="Another Kopi"))
        assert len(embedder.calls) == 1
        assert _company("SYN-000002")["has_embedding"] is True

    def test_stale_event_is_not_embedded(self, deliver: Deliverer, embedder: FakeEmbedder) -> None:
        deliver(upsert(updated_at="2026-08-14T10:00:00+08:00"))
        deliver(upsert(updated_at="2026-08-14T09:00:00+08:00", natureOfBusiness="Old text"))
        assert len(embedder.calls) == 1


class TestMissingFields:
    def test_empty_description_is_embedded_from_name_category_state(
        self, deliver: Deliverer, embedder: FakeEmbedder
    ) -> None:
        deliver(upsert(natureOfBusiness=""))
        assert embedder.calls == ["Kopi Co Sdn. Bhd. | Food & Beverage | Selangor"]
        company = _company()
        assert company["description"] is None
        assert company["has_embedding"] is True

    def test_missing_description_keeps_the_stored_one(
        self, deliver: Deliverer, embedder: FakeEmbedder
    ) -> None:
        deliver(upsert(updated_at="2026-08-14T09:00:00+08:00"))
        deliver(upsert(updated_at="2026-08-14T10:00:00+08:00", natureOfBusiness=None))
        assert _company()["description"] == "Operating cafes and coffee shops"
        assert len(embedder.calls) == 1

    def test_missing_state_keeps_the_stored_one(self, deliver: Deliverer) -> None:
        deliver(upsert(updated_at="2026-08-14T09:00:00+08:00"))
        deliver(upsert(updated_at="2026-08-14T10:00:00+08:00", state=None))
        assert _company()["state"] == "Selangor"


class TestStrikeOff:
    def test_strike_off_deactivates_the_company(self, deliver: Deliverer) -> None:
        deliver(upsert())
        assert deliver(strike_off()) is Status.STRUCK_OFF
        company = _company()
        assert company["is_active"] is False
        assert company["has_embedding"] is True

    def test_older_upsert_after_strike_off_is_stale(self, deliver: Deliverer) -> None:
        deliver(upsert(updated_at="2026-08-14T09:00:00+08:00"))
        deliver(strike_off(updated_at="2026-08-14T16:00:00+08:00"))
        assert deliver(upsert(updated_at="2026-08-14T12:00:00+08:00")) is Status.STALE
        assert _company()["is_active"] is False

    def test_newer_upsert_reactivates(self, deliver: Deliverer) -> None:
        deliver(upsert(updated_at="2026-08-14T09:00:00+08:00"))
        deliver(strike_off(updated_at="2026-08-14T16:00:00+08:00"))
        assert deliver(upsert(updated_at="2026-08-14T18:00:00+08:00")) is Status.STORED
        assert _company()["is_active"] is True

    def test_strike_off_before_first_upsert_is_held(self, deliver: Deliverer) -> None:
        assert deliver(strike_off(updated_at="2026-08-14T16:00:00+08:00")) is (
            Status.STRIKE_OFF_PENDING
        )
        assert _count("companies") == 0
        assert deliver(upsert(updated_at="2026-08-14T09:00:00+08:00")) is Status.STRUCK_OFF
        company = _company()
        assert company["is_active"] is False
        assert company["name"] == "Kopi Co Sdn. Bhd."
        assert company["has_embedding"] is True
        assert _count("pending_strike_offs") == 0

    def test_held_strike_off_older_than_first_upsert_is_dropped(self, deliver: Deliverer) -> None:
        deliver(strike_off(updated_at="2026-08-14T08:00:00+08:00"))
        assert deliver(upsert(updated_at="2026-08-14T09:00:00+08:00")) is Status.STORED
        assert _company()["is_active"] is True
        assert _count("pending_strike_offs") == 0


class TestFailures:
    def test_poison_message_is_dead_lettered_and_acked(
        self, deliver: Deliverer, channel: FakeChannel
    ) -> None:
        assert deliver(b"null", message_id="m-poison") is Status.DEAD_LETTERED
        assert channel.acked == [1]
        [published] = channel.published
        assert published.routing_key == DEAD_LETTER_QUEUE
        assert published.body == b"null"
        assert published.properties is not None
        assert published.properties.message_id == "m-poison"
        headers = published.properties.headers or {}
        assert "ValidationError" in str(headers["error"])
        assert _count("processed_messages") == 0

    def test_database_outage_requeues_and_asks_to_reconnect(
        self, deliver: Deliverer, channel: FakeChannel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        @contextmanager
        def unavailable() -> Iterator[db.Connection]:
            raise psycopg.OperationalError("connection refused")
            yield

        monkeypatch.setattr(consumer.db, "conn", unavailable)
        with pytest.raises(DatabaseUnavailableError):
            deliver(upsert())
        assert channel.nacked == [(1, True)]
        assert channel.acked == []
        assert channel.published == []

    def test_requeued_message_is_applied_on_redelivery(
        self, deliver: Deliverer, channel: FakeChannel, embedder: FakeEmbedder
    ) -> None:
        failures = iter([psycopg.OperationalError("server closed the connection")])

        def flaky(text: str) -> list[float]:
            for failure in failures:
                raise failure
            return embedder(text)

        deliver.consumer = consumer.Consumer(embed=flaky)
        with pytest.raises(DatabaseUnavailableError):
            deliver(upsert(), message_id="m-1")
        assert _count("processed_messages") == 0
        assert deliver(upsert(), message_id="m-1") is Status.STORED
        assert channel.nacked == [(1, True)]
        assert channel.acked == [2]

    def test_unexpected_error_is_dead_lettered_and_rolled_back(
        self, deliver: Deliverer, channel: FakeChannel
    ) -> None:
        def broken(text: str) -> list[float]:
            raise RuntimeError("model crashed")

        deliver.consumer = consumer.Consumer(embed=broken)
        assert deliver(upsert(), message_id="m-1") is Status.DEAD_LETTERED
        assert channel.acked == [1]
        [published] = channel.published
        assert published.properties is not None
        assert "RuntimeError: model crashed" in str((published.properties.headers or {})["error"])
        assert (_count("companies"), _count("processed_messages")) == (0, 0)
