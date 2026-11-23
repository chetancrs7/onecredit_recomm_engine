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
just publish                    # put the registry feed on the queue
```

- Postgres: `postgresql://postgres:postgres@localhost:5432/companies`
- RabbitMQ UI: <http://localhost:15672> (`guest` / `guest`)

To start again from empty: `just down`, then `just up`.

## Checks

```powershell
uv run ruff check .
uv run pyright
uv run pytest -q
```

## Settings

Read from environment variables or `.env` (see `src/company_recs/config.py`):

| Variable | Default |
|---|---|
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/companies` |
| `RABBITMQ_URL` | `amqp://guest:guest@localhost:5672/` |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` |
| `TOP_K` | `4` |
| `LOG_LEVEL` | `INFO` |

If a port is taken, set `POSTGRES_PORT`, `RABBITMQ_PORT` or `RABBITMQ_UI_PORT` before `just up`, e.g.
`$env:POSTGRES_PORT = "5433"`, and update `DATABASE_URL` / `RABBITMQ_URL` to match.

## Windows gotchas

- **`uv: command not found` from `just`**: a bare `bash` on Windows is WSL's, which can't see Windows programs. The
  Justfile's `windows-shell` points at Git Bash (`C:/Program Files/Git/bin/bash.exe`). If Git is installed
  somewhere else, change that path.
- **`python` opens the Microsoft Store**: Python isn't on `PATH`. Use `uv run python ...` instead.
- **`just` not found right after installing**: open a new terminal.
