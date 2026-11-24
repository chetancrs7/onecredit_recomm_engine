# Branch `feat/schema-and-db`

Adds the database layer: the schema, a migration runner, and a connection pool. No consumer or API logic yet.

## What changed

| File | What it does |
|---|---|
| `migrations/001_init.sql` | The `vector` extension and the `companies`, `processed_messages` and `company_events` tables, plus the HNSW and `(category, state)` indexes. |
| `src/company_recs/db.py` | `get_pool()`, `close_pool()`, the `conn()` context manager, and `run_migrations()`. Runnable as `python -m company_recs.db`. |
| `tests/test_db.py` | Integration tests against a real pgvector Postgres started by testcontainers. |
| `DESIGN.md` | Started, with the first assumption: why `category` is nullable. |
| `Justfile` | `just migrate` now runs the migrations. |
| `pyproject.toml` | Registers the `integration` pytest marker. |
| `src/company_recs/config.py`, `.env.example`, `tests/test_config.py`, `SETUP.md` | Default hosts changed from `localhost` to `127.0.0.1` (see below). |

## Schema

| Table | Purpose |
|---|---|
| `companies` | One row per company (`id` = registration number): latest name, `category` (industry, nullable), state, description, `description_hash`, `embedding vector(384)`, `source_updated_at`, `source_message_id`, `ingested_at`, `is_active`. |
| `processed_messages` | `message_id`s already handled, so redelivered messages can be skipped. |
| `company_events` | Audit log: every message received, with its raw body as `jsonb`. |
| `schema_migrations` | Names of migration files already applied. Created by the runner, not by `001_init.sql`. |

Indexes: `companies_embedding_hnsw` (HNSW, `vector_cosine_ops`, `m = 16`, `ef_construction = 64`) and
`companies_category_state_idx` on `(category, state)`.

## How migrations work

- Every `migrations/*.sql` file not listed in `schema_migrations` runs in name order.
- Each file runs **in its own transaction together with its `schema_migrations` row**. If a file fails, neither
  its changes nor its row are kept, so the next run retries it cleanly.
- A **Postgres advisory lock** makes sure only one process migrates at a time (e.g. if the consumer and API both
  start up and migrate).
- The SQL also uses `IF NOT EXISTS`, so it's harmless even if run by hand.
- Re-running is a no-op: `just migrate` prints `schema is up to date`.

## Decisions

- **`category` is nullable.** 134 feed messages have no `industry`, and a placeholder like `Unknown` would make
  all of those companies look alike to each other. The consumer, API and evaluation rules for a missing category are
  in `DESIGN.md` under Assumptions. `001_init.sql` was edited in place rather than adding a `002` migration,
  because this branch isn't merged yet.
- **Migrations use their own connection, not the pool.** Each pooled connection registers the pgvector type on
  connect, and that type only exists after `CREATE EXTENSION vector`. So: migrate first, then use the pool.
- **Pool:** 1–10 connections, opened on first `get_pool()`. `conn()` commits when the block succeeds and rolls back
  if it raises. `close_pool()` exists for clean shutdown and tests.
- **10-second connect timeout** on the pool and the migration connection, so an unreachable database fails quickly
  instead of hanging.
- **`127.0.0.1` instead of `localhost`.** On Windows, `localhost` tries IPv6 (`::1`) first. Docker Compose only
  publishes the ports on `127.0.0.1`, and Windows silently drops the IPv6 attempt instead of refusing it, so every
  connection stalled before falling back to IPv4. Measured: `127.0.0.1` connects in 0.02s, `localhost` took 5s with a
  5s timeout, and without one a second `just migrate` took minutes.
- **Migration files are found at `<repo>/migrations`**, relative to the source tree. That's fine for running from the
  repo; a packaged deployment would need the files shipped with the package.
- **Integration tests use testcontainers** with the same `pgvector/pgvector:0.8.7-pg16` image as docker-compose, so
  they never touch the dev database. They need Docker; skip them with `pytest -m "not integration"`.

## Things to handle in later branches

- **`company_events.raw` is `jsonb`**, but 11 feed lines aren't valid JSON. The consumer must wrap those (e.g. as a JSON
  string) or they can't be stored in the audit log.
- **`vector(384)` fixes the embedding size.** `bge-small-en-v1.5` and the multilingual MiniLM / e5-small models are
  all 384, but a bigger model would need a migration.
- **Filtering with an HNSW index** (`WHERE is_active`) can return fewer than k results. pgvector 0.8's iterative
  index scans (`hnsw.iterative_scan`) fix that; to be set up in the API branch.
- **`processed_messages` only catches exact repeats.** The same event can arrive with a different `message_id`
  (e.g. an extra `source` field), so the consumer should also compare `updatedAt` with `source_updated_at`.

## How to check it

```powershell
just up
just migrate        # applied: 001_init.sql
just migrate        # schema is up to date
uv run pytest -q    # 9 passed (needs Docker)
```
