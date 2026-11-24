set shell := ["bash", "-euo", "pipefail", "-c"]
set windows-shell := ["C:/Program Files/Git/bin/bash.exe", "-euo", "pipefail", "-c"]

default: setup

# Install the producer's dependencies.
setup:
    uv sync --locked
    if [ -f .pre-commit-config.yaml ]; then uv run --with pre-commit pre-commit install; fi

# Start Postgres (with pgvector) and RabbitMQ, and wait until both are healthy.
up:
    docker compose up -d --wait

# Stop everything and delete the volumes.
down:
    docker compose down -v

# Replay the registry feed onto the company-events queue.
publish:
    uv run python -m producer.publish

# Create or update the database schema (safe to re-run).
migrate:
    uv run python -m company_recs.db

# To be used later
consumer:
    @echo "not implemented"

api:
    @echo "not implemented"

test:
    @echo "not implemented"

lint:
    @echo "not implemented"

eval:
    @echo "not implemented"
