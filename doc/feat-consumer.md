# Branch `feat/consumer`

Adds the RabbitMQ consumer: reads `company-events`, turns each message into a `CompanyEvent`, and applies it to
Postgres with its embedding. It stays correct under duplicates, redeliveries, out-of-order `updatedAt`, malformed
messages and database outages.

## What changed

| File | What it does |
|---|---|
| `src/company_recs/models.py` | `CompanyEvent` (Pydantic v2) and `parse_event(body)`. Flattens the feed's envelope and cleans every field. |
| `src/company_recs/consumer.py` | `process_event()` (the database transaction), the `Consumer` class (pika, ack/nack, dead-lettering, reconnects, backoff), Prometheus metrics and `main()`. |
| `src/company_recs/logs.py` | structlog set up to write JSON lines. |
| `migrations/002_description_hash_idx.sql` | Index on `description_hash`, used to find a vector to reuse. |
| `migrations/003_pending_strike_offs.sql` | Holds a strike-off that arrives before the company's first upsert. |
| `src/company_recs/db.py` | The pool checks each connection before handing it out, and waits at most 10 s for one. |
| `src/company_recs/config.py`, `.env.example` | `CONSUMER_METRICS_PORT` (default 9101). |
| `Justfile` | `just consumer`. |
| `tests/` | `test_models.py` (unit), `test_consumer.py`, `test_chaos.py`, `test_consumer_rabbitmq.py` (integration), shared fixtures in `conftest.py` and test doubles in `support.py`. |
| `DESIGN.md`, `SETUP.md` | Feed quirks and ordering rules; how to run the consumer. |

## How one message is handled

1. **Parse.** `parse_event` raises `ValueError` for anything unusable. The raw bytes are published to
   `company-events-dlq` with header `{"error": "<type>: <message>"}`, then the message is acked.
2. **Message id.** AMQP `message_id`, else the event's `message_id`, else `sha256(body)`.
3. **One transaction:**
   - `INSERT INTO processed_messages … ON CONFLICT DO NOTHING`. If no row is inserted, it's a duplicate: ack and stop.
   - `SELECT … FROM companies WHERE id = %s FOR UPDATE`.
   - If the stored row is newer (by `updatedAt`, then message id), the event is `stale`. It is audited but not
     applied, and **not embedded**.
   - Otherwise merge the fields: a missing category, state or description keeps the stored value. Then build the
     embed text and hash it. The model only runs if the hash changed **and** no other company already has a vector
     for that text.
   - Upsert using the spec's `WHERE` clause: newer `updatedAt` wins, and `message_id` breaks ties.
     `embedding = COALESCE(EXCLUDED.embedding, companies.embedding)` keeps the old vector when the embedding was skipped.
   - `INSERT INTO company_events` (the raw JSON, for audit).
4. **Ack** after the commit. A crash between the commit and the ack causes a redelivery, which step 3 turns into a duplicate.

`events_consumed_total{status}` labels: `stored`, `struck_off`, `strike_off_pending`, `stale`, `duplicate`,
`dead_lettered`, `requeued`.

## Failures

| What fails | What happens |
|---|---|
| Message can't be parsed | Dead-letter, ack. `consumer_errors_total{type="parse"}`. |
| `psycopg.OperationalError` (Postgres down, connection dropped, pool timeout, serialization failure) | The transaction rolls back and the message is `basic_nack(requeue=True)`. Both connections are closed, then reconnected after a backoff of 1 s, 2 s, 4 s … up to 30 s. The backoff resets after the next successful ack. `{type="database"}`. |
| RabbitMQ connection or channel lost | Same reconnect loop. Unacked messages go back on the queue by themselves. `{type="broker"}`. |
| Any other exception (for example the model crashes on one text) | Rolled back, dead-lettered, acked. Requeueing would just fail again, forever. `{type="unexpected"}`. |
| SIGINT / SIGTERM (and SIGBREAK on Windows) | Finish the messages already being handled, then close. Prefetched messages that weren't started are requeued by RabbitMQ. |

## Where this differs from the spec

- **The feed isn't the flat shape the spec describes.** Real messages are envelopes:
  `{"type", "registrationNumber", "updatedAt", "data": {"companyName", "natureOfBusiness", "state", "industry"}}`.
  `CompanyEvent` keeps the spec's field names (`id`, `name`, `category`, `state`, `description`, `updatedAt`,
  `is_active`, `message_id`, `extra="allow"`). A `model_validator(mode="before")` maps the envelope onto them, so
  the flat form still validates too.
- **`category: str | None`**, not `str`. 120 messages send `industry: null` and 14 leave it out. DESIGN.md already
  decided that category is nullable.
- **`name: str | None`**, required only for upserts, because a `company-struck-off` event has no `data`.
- **The upsert uses `COALESCE` for category**, as DESIGN.md (Missing category) says, instead of
  `category = EXCLUDED.category`. Without it, a later message with no industry would clear a category we know.
  I applied the same rule to `state` and `description`. It also sets `is_active` and refreshes `ingested_at`.
- **`message_id` is read from `properties.message_id`.** `msg.method` is a `Basic.Deliver`, which has no
  `message_id`; it lives on `BasicProperties`.
- **The PRECONDITION_FAILED note is a docstring** on `declare_queues()`, not a `#` comment (no-comments rule).
- **Strike-offs** weren't in the spec. A strike-off sets `is_active = false` and keeps the row and vector. If it
  arrives before the company's first upsert, it is held in `pending_strike_offs`, so a delayed upsert can't
  resurrect the company.
- **Embedding skips more than the spec asked**: stale events are never embedded, and a vector is copied from
  another company with the same embed text. Over the whole feed the model ran 217 times out of 2,344 stores.
- **Only `OperationalError` is requeued.** Other errors are dead-lettered so one bad message can't loop forever.
- **The consumer runs migrations at start** (they are idempotent and take a lock). If the tables were missing,
  every message would fail with `UndefinedTable` and be dead-lettered. It also loads the model before consuming.
- **`process_data_events(time_limit=1)` in a loop instead of `start_consuming()`.** The signal handler only sets
  a flag, so stopping never calls into pika from inside a signal handler.
- **pika 1.5 deprecates `BlockingConnection`** in favour of a thread-safe `pika.Connection`. I kept
  `BlockingConnection` as specified, and filtered the warning in pytest. Switching is a small follow-up.

## Results on the real feed

`just publish` then `just consumer`, with the real model, on this machine:

| | |
|---|---|
| Messages | 2,518: 2,344 stored, 66 stale, 63 duplicates, 8 struck off, 37 dead-lettered |
| Companies | 2,000, all with an embedding; 8 inactive; 102 with no category; 16 distinct states |
| Embeddings | 217 computed, 1,872 copied from a company with the same text, 255 kept |
| Time | about 45 s wall clock; 34 s of message handling (13 ms per message on average) |
| Shutdown | Ctrl+Break, then `consumer stopped`, exit code 0 |

The nearest neighbours of `SYN-001550` (the bakery in the README mock) are Seri Sejahtera Food (Johor),
Everbright Pacific Food (Kuala Lumpur) and Gemilang Delima Food (Johor). Those are the first three cards in the mock.

## Tests

| File | What it checks | Needs |
|---|---|---|
| `test_models.py` (34) | Envelope mapping, id and state normalisation, every timestamp format meaning the same instant, and 14 kinds of unusable message raising `ValueError`. | nothing |
| `test_consumer.py` (26) | Storing, duplicates, the message-id fallbacks, stale and tie-break ordering, when embedding is skipped, missing fields, strike-offs (including one that arrives early), and the failure paths: dead-letter, requeue on outage then apply on redelivery, rollback on unexpected errors. | Docker (Postgres) |
| `test_chaos.py` (6) | The whole feed gives 2,000 companies and 37 dead letters. Replaying it again changes nothing. Reversed, or shuffled with 20% redeliveries, it gives the same result as in order. Also the two missing-category tests from the spec. | Docker (Postgres) |
| `test_consumer_rabbitmq.py` (1) | The first 400 feed messages through a real RabbitMQ, with one simulated database outage. Checks requeue, reconnect, redelivery, the dead letters and their headers, and a clean stop. | Docker (Postgres, RabbitMQ) |

The tests use a fake embedder (a vector seeded from the text), so they run fast and can count model calls. The
real model is covered by `test_embedder.py`.

```powershell
uv run pytest -q -m "not integration and not model"   # 49 unit tests, under a second
uv run pytest -q tests/test_consumer.py               # Docker running; about 10 s
uv run pytest -q                                      # everything: 93 tests, about 3 minutes
```

## Known limitations

- **The merge is per row, not per field.** If a newer event without a category arrives before an older one with a
  category, the older one is stale and the category stays `NULL`. The latest event always wins; only the gap-filling
  depends on order. The chaos tests compare which event won, not the merged fields. The fix is a timestamp per field.
- **One consumer at a time is assumed for `pending_strike_offs`.** Two consumers racing on a brand-new company could
  both miss the held strike-off. Row locks cover every other path.
- **`description_hash` hashes the text only, not the model.** After changing `EMBED_MODEL`, vectors must be rebuilt
  (or the model name added to the hash) before "copy a vector with the same hash" is safe again.
