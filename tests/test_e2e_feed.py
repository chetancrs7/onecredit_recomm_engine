"""End to end: a synthetic feed through a real RabbitMQ and the consumer into Postgres, read back
through the API.

The feed (`support.synthetic_feed`) has 100 messages about 60 companies, with 20 duplicate
message_ids and 5 out-of-order pairs, so the expected outcome is known exactly.
"""

from collections import Counter

import httpx
import pytest
from support import (
    Database,
    FakeEmbedder,
    RecordingConsumer,
    publish,
    queue_depth,
    running,
    synthetic_feed,
)

from company_recs.config import Settings
from company_recs.consumer import Status

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_synthetic_feed_end_to_end(
    db: Database, queues: str, api_client: httpx.AsyncClient
) -> None:
    feed = synthetic_feed()
    publish(queues, feed.bodies)
    consumer = RecordingConsumer(
        Settings(database_url=db.url, rabbitmq_url=queues), embed=FakeEmbedder()
    )
    with running(consumer):
        consumer.wait_for(len(feed.bodies))

    assert queue_depth(queues) == 0
    assert Counter(consumer.statuses) == {
        Status.STORED: 75,
        Status.STALE: 5,
        Status.DUPLICATE: 20,
    }

    companies = db.companies()
    assert len(companies) == len(feed.newest) == 60
    assert companies == feed.newest

    processed = {row[0] for row in db.fetchall("SELECT message_id FROM processed_messages")}
    assert db.count("processed_messages") == len(feed.unique_message_ids) == 80
    assert processed == feed.unique_message_ids

    for company_id in feed.out_of_order_ids:
        response = await api_client.get(f"/companies/{company_id}/similar")
        assert response.status_code == 200
        assert response.json()["company"]["name"] == feed.newest[company_id][0]
