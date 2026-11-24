"""Integration tests for the database layer, against a real pgvector Postgres in Docker."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from testcontainers.community.postgres import PostgresContainer

from company_recs import db
from company_recs.config import get_settings

pytestmark = pytest.mark.integration

PGVECTOR_IMAGE = "pgvector/pgvector:0.8.7-pg16"


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    with PostgresContainer(PGVECTOR_IMAGE, driver=None) as postgres:
        yield postgres.get_connection_url()


@pytest.fixture
def migrated(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A migrated database, with Settings and the pool pointed at it."""
    db.run_migrations(database_url)
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    yield database_url
    db.close_pool()
    get_settings.cache_clear()


def test_migrations_are_idempotent(database_url: str) -> None:
    db.run_migrations(database_url)
    assert db.run_migrations(database_url) == []


def test_migrations_create_tables_and_indexes(migrated: str) -> None:
    with db.conn() as c:
        tables = {
            row[0]
            for row in c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
        indexes = {
            row[0]
            for row in c.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'companies'"
            ).fetchall()
        }
        applied = c.execute("SELECT name FROM schema_migrations").fetchall()
    assert {"companies", "processed_messages", "company_events", "schema_migrations"} <= tables
    assert {"companies_embedding_hnsw", "companies_category_state_idx"} <= indexes
    assert ("001_init.sql",) in applied


def test_new_migration_files_are_applied_once(database_url: str, tmp_path: Path) -> None:
    for source in db.MIGRATIONS_DIR.glob("*.sql"):
        (tmp_path / source.name).write_bytes(source.read_bytes())
    (tmp_path / "999_test.sql").write_text("CREATE TABLE IF NOT EXISTS migration_probe (x int);")
    assert db.run_migrations(database_url, tmp_path) == ["999_test.sql"]
    assert db.run_migrations(database_url, tmp_path) == []


def test_failed_migration_is_rolled_back(database_url: str, tmp_path: Path) -> None:
    (tmp_path / "998_broken.sql").write_text(
        "CREATE TABLE half_applied (x int); SELECT * FROM table_that_does_not_exist;"
    )
    with pytest.raises(Exception, match="table_that_does_not_exist"):
        db.run_migrations(database_url, tmp_path)
    with psycopg.connect(database_url) as c:
        assert c.execute("SELECT to_regclass('half_applied')").fetchone() == (None,)
        recorded = c.execute(
            "SELECT 1 FROM schema_migrations WHERE name = '998_broken.sql'"
        ).fetchall()
    assert recorded == []


def test_pool_round_trips_vectors(migrated: str) -> None:
    near = [1.0] + [0.0] * 383
    far = [0.0, 1.0] + [0.0] * 382
    with db.conn() as c:
        for company_id, vector in (("SYN-T00001", near), ("SYN-T00002", far)):
            c.execute(
                """
                INSERT INTO companies (id, name, category, embedding, source_updated_at,
                                       source_message_id)
                VALUES (%s, 'Test Co', 'Testing', %s::vector, now(), 'm-' || %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (company_id, str(vector), company_id),
            )
        nearest = c.execute(
            "SELECT id FROM companies WHERE embedding IS NOT NULL "
            "ORDER BY embedding <=> %s::vector LIMIT 1",
            (str(near),),
        ).fetchone()
        stored = c.execute("SELECT embedding FROM companies WHERE id = 'SYN-T00001'").fetchone()
    assert nearest == ("SYN-T00001",)
    assert stored is not None
    assert stored[0].to_list() == near


def test_category_is_nullable(migrated: str) -> None:
    """134 feed messages have no industry; a company without one must still be stored."""
    with db.conn() as c:
        c.execute(
            """
            INSERT INTO companies (id, name, category, source_updated_at, source_message_id)
            VALUES ('SYN-T00003', 'No Category Co', NULL, now(), 'm-SYN-T00003')
            ON CONFLICT (id) DO NOTHING
            """
        )
        row = c.execute("SELECT category FROM companies WHERE id = 'SYN-T00003'").fetchone()
    assert row == (None,)
