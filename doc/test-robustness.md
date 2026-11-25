# Branch `test/robustness`

Adds the testcontainers integration and chaos suite: shared Postgres and RabbitMQ containers, an end-to-end test on a
synthetic feed, RabbitMQ fault-injection tests, a `chaos` marker and `just test-chaos`.

## What changed

| File | What it does |
|---|---|
| `tests/conftest.py` | Session fixtures `pg_container`, `mq_container`, `db_url`, `amqp_url`; migrations once per run; `db` (empties the tables, returns query helpers); `queues` (declares and purges `company-events` and the DLQ); `api_client` (`httpx.AsyncClient` over ASGI). |
| `tests/support.py` | `Database` query helpers, `publish` / `queue_depth` / `dead_letters`, `RecordingConsumer` and `running()` to run the consumer in a thread, and `synthetic_feed()`. |
| `tests/test_e2e_feed.py` | The synthetic feed through RabbitMQ, the consumer, Postgres and the API. |
| `tests/test_chaos.py` | The five new RabbitMQ chaos tests, next to the existing real-feed replays and the two missing-category tests. The whole module is marked `chaos`. |
| `tests/test_consumer_rabbitmq.py` | Uses the shared containers and helpers instead of its own. |
| `tests/test_db.py` | `database_url` → `db_url`. |
| `pyproject.toml`, `Justfile` | The `chaos` marker; `just test` and `just test-chaos`. |
| `DESIGN.md`, `SETUP.md` | A Testing section; how to run each part. |

## The synthetic feed

`synthetic_feed()` always builds the same 100 messages about 60 companies:

- companies 0–54 get a v1, and companies 0–14 later get a newer v2, in order;
- companies 55–59 are the **5 out-of-order pairs**: v2 is published before the older v1;
- **20 of the 80 distinct messages are published again** at a random later position (seeded), with the same
  `message_id`.

Each version changes the name, category, state and description, so "the newest event won" can be checked field by
field. The end-to-end test asserts:

- 60 companies, each matching its newest `updatedAt` event;
- 80 `processed_messages` rows, exactly the distinct message ids;
- statuses of 75 stored, 5 stale and 20 duplicates;
- an empty source queue;
- the API returns the v2 name for each out-of-order company.

## Chaos tests

| Test | Fault | Checks |
|---|---|---|
| `test_duplicate_publish_is_idempotent` | The synthetic feed published twice | Row counts (60, 80, 80) and every company unchanged; the second 100 are all duplicates. |
| `test_out_of_order_updates_apply_newest` | v2 then v1 | Stored then stale; the row is v2. |
| `test_malformed_lands_in_dlq` | Truncated JSON | It is in `company-events-dlq` with an `error` header and its message id; the source queue is empty; the consumer thread is still running and stores the next valid message. |
| `test_missing_message_id_is_synthesized` | The same body twice, no AMQP `message_id` | Stored then duplicate; the one `processed_messages` row is `sha256(body)`. |
| `test_db_restart_recovery` | `get_pool()` raises `OperationalError` once, after 30 messages are published | One nack with requeue, `consumer_errors_total{type="database"}` up by 1, a reconnect, then all 30 stored once, and the queue empty. |

## Where this differs from the spec

- **Pinned images.** `pgvector/pgvector:0.8.7-pg16` and `rabbitmq:3.13.7-management`, the same as
  `docker-compose.yml`, instead of `pg16` and `3-management`. Floating tags can change under the tests, and the
  API relies on pgvector 0.8 (`hnsw.iterative_scan`).
- **`RabbitMqContainer`**, not `RabbitMQContainer`. That's the class name in testcontainers.
- **`httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app))`.** httpx 0.28 removed the `app=` argument. The
  async test runs on anyio's pytest plugin (already installed with Starlette), so no new dependency.
- **The `db` fixture returns a `Database` helper**, not a connection or a URL. A fixture called `db` would shadow
  `from company_recs import db` in any test that also used the module. A helper with `count`, `counts`,
  `companies` and `fetchall` reads better in the assertions anyway. `clean_db` stays as a thin alias for the
  existing tests.
- **It also empties `pending_strike_offs`**, added in `feat/consumer`; otherwise a held strike-off could leak into
  the next test.
- **There's a `queues` fixture.** The RabbitMQ container is shared by the whole run, so each test that uses it
  starts with both queues declared and purged.
- **"Until the queue is drained" means "until every published message has an outcome".** `RecordingConsumer`
  counts finished deliveries and a test waits for the expected number. Queue depth alone can read 0 while up to
  32 prefetched messages are still unacked. Queue depth is still asserted to be 0 afterwards.
- **`test_chaos.py` already existed** with the real-feed replays and the two missing-category tests you asked for
  earlier. The new tests sit beside them and the whole module is `chaos`, so `just test-chaos` runs both kinds.
- **`just test` is implemented too** (it was a stub): `uv run pytest -q`.

## Results

| Command | Result |
|---|---|
| `uv run pytest -q` | 123 passed in about 2 min 45 s |
| `just test-chaos` | 11 passed in about 2 min 25 s; the replays of the real 2,518-message feed take most of that |
| `uv run pytest -q -m "not integration and not model"` | 59 passed in under a second, no Docker |
| The 6 RabbitMQ tests, run 3 times | all passed, about 28 s each time |
