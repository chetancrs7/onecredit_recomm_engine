"""`GET /companies/{id}/similar` against a real pgvector Postgres, with hand-made vectors.

Each neighbour's vector is at a chosen angle to the queried company's, so its expected cosine
similarity, and therefore its `score` and rank, is known exactly.
"""

import math
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pgvector import Vector

from company_recs import api, db, embedder
from company_recs.api import PROBLEM_JSON, REQUEST_ID_HEADER, SIMILAR_SQL, find_similar
from company_recs.embedder import EMBED_DIM

pytestmark = pytest.mark.integration

QUERY_ID = "SYN-000001"


def _vector(similarity: float) -> Vector:
    """A unit vector with cosine similarity `similarity` to the query vector [1, 0, 0, ...]."""
    angle = math.acos(similarity)
    values = [0.0] * EMBED_DIM
    values[0], values[1] = math.cos(angle), math.sin(angle)
    return Vector(values)


def _insert(
    company_id: str,
    similarity: float | None,
    *,
    category: str | None = "Food & Beverage",
    state: str | None = "Selangor",
    active: bool = True,
) -> None:
    with db.conn() as connection:
        connection.execute(
            """
            INSERT INTO companies (id, name, category, state, embedding,
                                   source_updated_at, source_message_id, is_active)
            VALUES (%s, %s, %s, %s, %s, now(), 'test', %s)
            """,
            (
                company_id,
                f"{company_id} Sdn. Bhd.",
                category,
                state,
                None if similarity is None else _vector(similarity),
                active,
            ),
        )


@pytest.fixture
def client(clean_db: str) -> Iterator[TestClient]:
    with TestClient(api.app) as client:
        yield client


@pytest.fixture
def neighbours(clean_db: str) -> None:
    _insert(QUERY_ID, 1.0)
    for number, similarity in enumerate([0.9, 0.8, 0.7, 0.6, 0.5], start=2):
        _insert(f"SYN-00000{number}", similarity)


def _similar(client: TestClient, company_id: str = QUERY_ID, **params: int) -> dict[str, Any]:
    response = client.get(f"/companies/{company_id}/similar", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _ids(body: dict[str, Any]) -> list[str]:
    return [company["id"] for company in body["similar"]]


@pytest.mark.usefixtures("neighbours")
class TestSimilar:
    def test_most_similar_first_without_the_company_itself(self, client: TestClient) -> None:
        body = _similar(client)
        assert body["company"] == {
            "id": QUERY_ID,
            "name": "SYN-000001 Sdn. Bhd.",
            "category": "Food & Beverage",
            "state": "Selangor",
        }
        assert _ids(body) == ["SYN-000002", "SYN-000003", "SYN-000004", "SYN-000005"]
        similar = body["similar"]
        assert [company["score"] for company in similar] == pytest.approx([0.9, 0.8, 0.7, 0.6])
        assert set(similar[0]) == {"id", "name", "category", "state", "score"}

    def test_meta(self, client: TestClient) -> None:
        meta = _similar(client)["meta"]
        assert meta["model"] == embedder.MODEL_NAME
        assert meta["count"] == 4
        assert meta["took_ms"] >= 0

    def test_limit(self, client: TestClient) -> None:
        assert _ids(_similar(client, limit=2)) == ["SYN-000002", "SYN-000003"]
        assert len(_ids(_similar(client, limit=50))) == 5

    def test_id_is_matched_like_the_feed(self, client: TestClient) -> None:
        assert _similar(client, " syn-000001")["company"]["id"] == QUERY_ID

    def test_struck_off_neighbours_are_left_out(self, client: TestClient) -> None:
        with db.conn() as connection:
            connection.execute("UPDATE companies SET is_active = false WHERE id = 'SYN-000002'")
        assert _ids(_similar(client))[0] == "SYN-000003"

    def test_neighbours_without_an_embedding_are_left_out(self, client: TestClient) -> None:
        _insert("SYN-000099", None)
        assert "SYN-000099" not in _ids(_similar(client, limit=50))

    def test_missing_category_is_null(self, client: TestClient) -> None:
        _insert("SYN-000010", 0.95, category=None)
        similar = _similar(client)["similar"]
        assert similar[0] == {
            "id": "SYN-000010",
            "name": "SYN-000010 Sdn. Bhd.",
            "category": None,
            "state": "Selangor",
            "score": pytest.approx(0.95),
        }


class TestNotFound:
    def test_unknown_company(self, client: TestClient) -> None:
        response = client.get("/companies/SYN-404404/similar")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith(PROBLEM_JSON)
        assert response.json() == {
            "type": "about:blank",
            "title": "Not Found",
            "status": 404,
            "detail": "No active company with registration number SYN-404404.",
            "instance": "/companies/SYN-404404/similar",
            "request_id": response.headers[REQUEST_ID_HEADER],
        }

    def test_struck_off_company(self, client: TestClient) -> None:
        _insert(QUERY_ID, 1.0, active=False)
        _insert("SYN-000002", 0.9)
        assert client.get(f"/companies/{QUERY_ID}/similar").status_code == 404


def test_company_without_an_embedding_has_no_neighbours(client: TestClient) -> None:
    _insert(QUERY_ID, None)
    _insert("SYN-000002", 0.9)
    body = _similar(client)
    assert body["similar"] == []
    assert body["meta"]["count"] == 0


def test_readyz_is_ok_when_the_database_answers(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


class TestHnswIndex:
    """With 150 companies and 21 of the nearest struck off, force the index and ask for 50."""

    @pytest.fixture(autouse=True)
    def crowd(self, clean_db: str) -> None:
        _insert(QUERY_ID, 1.0)
        for rank in range(150):
            _insert(f"SYN-{rank + 100:06d}", 0.99 - rank * 0.005, active=rank % 3 != 0 or rank > 60)

    def _expected(self) -> list[str]:
        ranks = [rank for rank in range(150) if rank % 3 != 0 or rank > 60]
        return [f"SYN-{rank + 100:06d}" for rank in ranks[:50]]

    def test_the_query_uses_the_hnsw_index(self, clean_db: str) -> None:
        with db.conn() as connection:
            connection.execute("SET LOCAL enable_seqscan = off")
            plan = connection.execute(
                "EXPLAIN " + SIMILAR_SQL, {"id": QUERY_ID, "limit": 50}
            ).fetchall()
        assert "companies_embedding_hnsw" in "\n".join(str(row[0]) for row in plan)

    def test_filtered_index_scan_still_fills_the_limit(self, clean_db: str) -> None:
        with db.conn() as connection:
            connection.execute("SET LOCAL enable_seqscan = off")
            _, similar = find_similar(connection, QUERY_ID, 50)
        assert [company.id for company in similar] == self._expected()

    def test_without_iterative_scan_the_index_returns_too_few(self, clean_db: str) -> None:
        with db.conn() as connection:
            connection.execute("SET LOCAL enable_seqscan = off")
            rows = connection.execute(SIMILAR_SQL, {"id": QUERY_ID, "limit": 50}).fetchall()
        assert len(rows) < 50
