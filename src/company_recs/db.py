"""Postgres access: a shared connection pool and the schema migration runner.

Run migrations with `python -m company_recs.db` (or `just migrate`) before opening the pool:
each pooled connection registers the pgvector type, which only exists once the `vector` extension
is installed.
"""

import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from company_recs.config import get_settings

type Connection = psycopg.Connection[TupleRow]

MIGRATIONS_DIR: Final = Path(__file__).resolve().parents[2] / "migrations"

_MIGRATION_LOCK_ID: Final = 7_284_001

_CONNECT_TIMEOUT_S: Final = 10

_pool: ConnectionPool[Connection] | None = None
_pool_lock = threading.Lock()


def _configure(connection: Connection) -> None:
    register_vector(connection)


def get_pool() -> ConnectionPool[Connection]:
    """Return the process-wide pool, creating it on first use."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(
                get_settings().database_url,
                min_size=1,
                max_size=10,
                kwargs={"connect_timeout": _CONNECT_TIMEOUT_S},
                configure=_configure,
                check=ConnectionPool.check_connection,
                timeout=_CONNECT_TIMEOUT_S,
                open=True,
            )
        return _pool


def close_pool() -> None:
    """Close the pool, if one is open. The next get_pool() creates a new one."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


@contextmanager
def conn() -> Generator[Connection]:
    """Borrow a pooled connection. Commits on success, rolls back if the block raises."""
    with get_pool().connection() as connection:
        yield connection


def run_migrations(
    database_url: str | None = None, migrations_dir: Path = MIGRATIONS_DIR
) -> list[str]:
    """Apply every `*.sql` file in `migrations_dir` not yet recorded in schema_migrations.

    Files run in name order, each in its own transaction together with its schema_migrations row,
    so a failed migration leaves nothing half-applied. An advisory lock stops two processes
    migrating at once. Safe to run any number of times. Returns the names applied by this call.
    """
    url = database_url or get_settings().database_url
    applied: list[str] = []
    with psycopg.connect(url, autocommit=True, connect_timeout=_CONNECT_TIMEOUT_S) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_ID,))
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY)"
            )
            done = {row[0] for row in connection.execute("SELECT name FROM schema_migrations")}
            for path in sorted(migrations_dir.glob("*.sql")):
                if path.name in done:
                    continue
                with connection.transaction():
                    connection.execute(path.read_bytes())
                    connection.execute(
                        "INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,)
                    )
                applied.append(path.name)
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_ID,))
    return applied


if __name__ == "__main__":
    names = run_migrations()
    print(f"applied: {', '.join(names)}" if names else "schema is up to date")
