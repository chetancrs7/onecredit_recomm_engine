# Local setup (Windows, PowerShell)

How to get this project running on my machine. The assessment brief is in [`README.md`](README.md).

## Tools

| Tool | Install | Check |
|---|---|---|
| Git for Windows (provides Git Bash) | <https://git-scm.com/download/win> | `git --version` |
| Docker Desktop | <https://www.docker.com/products/docker-desktop/> | `docker --version` |
| uv (installs Python 3.14 itself) | `powershell -c "irm https://astral.sh/uv/install.ps1 \| iex"` | `uv --version` |
| just | `winget install --id Casey.Just --exact` | `just --version` |

After installing a tool, open a new PowerShell window so it picks up the new `PATH`.

## First run

```powershell
just setup                      # install dependencies from uv.lock
Copy-Item .env.example .env     # optional: local settings (the defaults already match docker-compose)
just up                         # start Postgres + RabbitMQ (Docker Desktop must be running)
just migrate                    # create the schema (safe to re-run)
just publish                    # put the registry feed on the queue
just consumer                   # consume it into Postgres; Ctrl+C to stop (also migrates)
just api                        # in a second window: serve the API on http://127.0.0.1:8000
```

The consumer logs one JSON line per message. The whole feed takes about 45 seconds; it is done when the
`company-events` queue in the RabbitMQ UI shows 0 messages. Then:

- `companies` has 2,000 rows, every one with an embedding.
- `company-events-dlq` holds the 37 messages that could not be used, each with an `error` header
  (RabbitMQ UI → Queues → `company-events-dlq` → Get messages).
- Prometheus metrics are at <http://127.0.0.1:9101/metrics> while it runs (`events_consumed_total`,
  `consumer_errors_total`, `embeddings_total`, `event_processing_seconds`).

- Postgres: `postgresql://postgres:postgres@127.0.0.1:5432/companies`
- RabbitMQ UI: <http://localhost:15672> (`guest` / `guest`)

With the consumer finished (or still running), try the API:

```powershell
curl.exe -s http://127.0.0.1:8000/companies/SYN-001550/similar
curl.exe -s "http://127.0.0.1:8000/companies/SYN-001550/similar?limit=10"
curl.exe -si http://127.0.0.1:8000/companies/SYN-999999/similar   # 404, application/problem+json
```

- Interactive docs: <http://127.0.0.1:8000/docs>.
- Probes: `/healthz` (process is up), `/readyz` (Postgres answers; 503 otherwise).
- Metrics: <http://127.0.0.1:8000/metrics> (`api_requests_total`, `api_request_seconds`).
- Each request logs one JSON line with `request_id`, `method`, `path`, `status` and `took_ms`; the same
  `request_id` comes back in the `X-Request-ID` header and in every error body.

To start again from empty: `just down`, then `just up`.

## Checks

```powershell
uv run ruff check .
uv run pyright
just test                       # everything; Docker must be running (integration tests start their own Postgres and RabbitMQ, about 3 minutes)
just test-chaos                 # only the chaos tests (about 2.5 minutes)
uv run pytest -q -m "not integration and not model"   # unit tests only, no Docker or model download
```

The test containers are pinned to the same images as `docker-compose.yml` and are separate from the stack
`just up` starts, so the tests never touch your local data.

## Settings

Read from environment variables or `.env` (see `src/company_recs/config.py`):

| Variable | Default |
|---|---|
| `DATABASE_URL` | `postgresql://postgres:postgres@127.0.0.1:5432/companies` |
| `RABBITMQ_URL` | `amqp://guest:guest@127.0.0.1:5672/` |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` |
| `TOP_K` | `4` (the API's default `limit`) |
| `LOG_LEVEL` | `INFO` |
| `CONSUMER_METRICS_PORT` | `9101` |
| `API_HOST` | `127.0.0.1` |
| `API_PORT` | `8000` |

If a port is taken, set `POSTGRES_PORT`, `RABBITMQ_PORT` or `RABBITMQ_UI_PORT` before `just up`, e.g.
`$env:POSTGRES_PORT = "5433"`, and update `DATABASE_URL` / `RABBITMQ_URL` to match.

## Windows gotchas

- **`uv: command not found` from `just`**: a bare `bash` on Windows is WSL's, which can't see Windows programs. The
  Justfile's `windows-shell` points at Git Bash (`C:/Program Files/Git/bin/bash.exe`). If Git is installed
  somewhere else, change that path.
- **`python` opens the Microsoft Store**: Python isn't on `PATH`. Use `uv run python ...` instead.
- **`just` not found right after installing**: open a new terminal.
- **Database connections take ages with `localhost`**: `localhost` tries IPv6 (`::1`) first, Docker only listens on
  `127.0.0.1`, and Windows silently drops the IPv6 attempt. Use `127.0.0.1` in `DATABASE_URL` / `RABBITMQ_URL`
  (the defaults already do).
- **Stopping a consumer that runs in the background**: on Windows only Ctrl+C in its own window, or Ctrl+Break
  (`SIGBREAK`), stops it cleanly; the consumer listens for both. Killing it is also safe: unacked messages go
  back on the queue and are deduplicated when redelivered.
- **`failed to connect to the docker API`**: Docker Desktop isn't running yet. Start it and wait until it says
  the engine is running.
