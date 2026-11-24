"""The real feed under at-least-once delivery: duplicates, redeliveries and reordering.

Every test replays `data/feed.jsonl` (2,518 messages, 2,000 companies) through the consumer's
message handler, against a real Postgres, with the fake embedder.
"""

import random
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from support import Deliverer, FakeChannel, upsert

from company_recs import db
from company_recs.consumer import DEAD_LETTER_QUEUE, Status

pytestmark = pytest.mark.integration

FEED = Path(__file__).resolve().parents[1] / "data" / "feed.jsonl"
FEED_LINES = FEED.read_bytes().splitlines()
FEED_COMPANIES = 2_000
FEED_POISON_MESSAGES = 37


def _replay(deliver: Deliverer, lines: list[bytes]) -> Counter[Status]:
    return Counter(deliver(line) for line in lines)


def _outcome() -> list[tuple[Any, ...]]:
    """What a reader of `companies` sees, minus fields that depend on arrival order."""
    with db.conn() as connection:
        return connection.execute(
            """
            SELECT id, name, is_active, source_updated_at, source_message_id,
                   embedding IS NOT NULL
            FROM companies ORDER BY id
            """
        ).fetchall()


def _reset() -> None:
    with db.conn() as connection:
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
    with db.conn() as connection:
        row = connection.execute(
            "SELECT category, embedding IS NOT NULL FROM companies WHERE id = 'SYN-000001'"
        ).fetchone()
    assert row == (None, True)


def test_missing_category_existing_company_keeps_previous(deliver: Deliverer) -> None:
    deliver(upsert(updated_at="2026-08-14T09:00:00+08:00", industry="Food & Beverage"))
    deliver(upsert(updated_at="2026-08-14T10:00:00+08:00", industry=None, companyName="New Name"))
    with db.conn() as connection:
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
