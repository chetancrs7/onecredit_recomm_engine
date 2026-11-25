"""API behaviour that needs no database: probes, validation, error shapes, request ids, metrics."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from fastapi.testclient import TestClient

from company_recs import api, db
from company_recs.api import PROBLEM_JSON, REQUEST_ID_HEADER


@pytest.fixture
def client() -> TestClient:
    return TestClient(api.app)


@contextmanager
def _database_down() -> Iterator[db.Connection]:
    raise psycopg.OperationalError("connection refused")
    yield


def _fail_pool() -> object:
    raise psycopg.OperationalError("connection refused")


def test_healthz_is_ok_without_a_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api.db, "get_pool", _fail_pool)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_is_503_when_the_database_is_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api.db, "get_pool", _fail_pool)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_every_response_has_a_fresh_request_id(client: TestClient) -> None:
    first = client.get("/healthz").headers[REQUEST_ID_HEADER]
    second = client.get("/healthz").headers[REQUEST_ID_HEADER]
    assert uuid.UUID(first) != uuid.UUID(second)


@pytest.mark.parametrize("limit", ["0", "51", "four"])
def test_out_of_range_limit_is_a_422_problem(client: TestClient, limit: str) -> None:
    response = client.get("/companies/SYN-000001/similar", params={"limit": limit})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["status"] == 422
    assert body["errors"][0]["loc"] == ["query", "limit"]
    assert body["request_id"] == response.headers[REQUEST_ID_HEADER]


def test_database_outage_is_a_503_problem(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api.db, "conn", _database_down)
    response = client.get("/companies/SYN-000001/similar")
    assert response.status_code == 503
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.headers["retry-after"] == "1"
    assert response.json()["title"] == "Service Unavailable"


def test_unknown_route_is_a_404_problem(client: TestClient) -> None:
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.json()["instance"] == "/nope"


def test_unexpected_error_is_a_500_problem_with_the_request_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def fake_conn() -> Iterator[None]:
        yield None

    def broken(*_: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(api.db, "conn", fake_conn)
    monkeypatch.setattr(api, "find_similar", broken)
    response = client.get("/companies/SYN-000001/similar")
    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]
    assert "boom" not in response.text


def test_metrics_count_requests_by_route_template(client: TestClient) -> None:
    client.get("/companies/SYN-000001/similar", params={"limit": "0"})
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'route="/companies/{company_id}/similar"' in response.text
    assert "SYN-000001" not in response.text
