"""The HTTP service behind the portal's "Similar companies" panel.

`GET /companies/{company_id}/similar` returns the queried company and its nearest active neighbours
by cosine similarity of their description embeddings, found with the HNSW index in Postgres.
Every error is an RFC 7807 problem (`application/problem+json`), and every response carries an
`X-Request-ID` header that matches the request's log line.

Run with `python -m company_recs.api` (or `just api`).
"""

import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Annotated, Any, Final

import psycopg
import structlog
import uvicorn
from fastapi import FastAPI, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from company_recs import db, embedder
from company_recs.config import get_settings
from company_recs.logs import configure_logging
from company_recs.models import normalise_company_id

PROBLEM_JSON: Final = "application/problem+json"
REQUEST_ID_HEADER: Final = "X-Request-ID"
MAX_LIMIT: Final = 50
READINESS_TIMEOUT_S: Final = 2.0
RETRY_AFTER_S: Final = 1
UNMATCHED_ROUTE: Final = "unmatched"

_COMPANY_SQL: Final = """
SELECT id, name, category, state, embedding IS NOT NULL
FROM companies
WHERE id = %(id)s AND is_active
"""

_ITERATIVE_SCAN_SQL: Final = "SELECT set_config('hnsw.iterative_scan', 'strict_order', true)"

SIMILAR_SQL: Final = """
WITH q AS (SELECT embedding FROM companies WHERE id = %(id)s AND is_active)
SELECT id, name, category, state, 1 - (embedding <=> (SELECT embedding FROM q)) AS score
FROM companies
WHERE id <> %(id)s AND is_active AND embedding IS NOT NULL
ORDER BY embedding <=> (SELECT embedding FROM q)
LIMIT %(limit)s
"""

REQUESTS: Final = Counter(
    "api_requests_total", "HTTP requests handled", ["method", "route", "status"]
)
REQUEST_SECONDS: Final = Histogram(
    "api_request_seconds",
    "Time to handle an HTTP request",
    ["route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

log = structlog.get_logger()


class Company(BaseModel):
    """What the portal needs to draw one company card."""

    id: str
    name: str
    category: str | None
    state: str | None


class SimilarCompany(Company):
    """A recommended company. `score` is cosine similarity: 1 is identical, higher is closer."""

    score: float


class Meta(BaseModel):
    """How the answer was produced."""

    model: str
    took_ms: float
    count: int


class SimilarResponse(BaseModel):
    """The queried company and its most similar active companies, most similar first."""

    company: Company
    similar: list[SimilarCompany]
    meta: Meta


class CompanyNotFoundError(Exception):
    """No active company has this registration number."""

    def __init__(self, company_id: str) -> None:
        super().__init__(company_id)
        self.company_id = company_id


def find_similar(
    connection: db.Connection, company_id: str, limit: int
) -> tuple[Company, list[SimilarCompany]]:
    """Return the active company `company_id` and its `limit` nearest active neighbours.

    Raises `CompanyNotFoundError` if the company is missing or struck off. A company without an
    embedding has no neighbours. `hnsw.iterative_scan` is turned on for this transaction so the
    index keeps scanning when the `is_active` and self-exclusion filters remove candidates;
    without it a filtered HNSW scan can return fewer than `limit` rows.
    """
    row = connection.execute(_COMPANY_SQL, {"id": company_id}).fetchone()
    if row is None:
        raise CompanyNotFoundError(company_id)
    company = Company(id=row[0], name=row[1], category=row[2], state=row[3])
    if not row[4]:
        return company, []
    connection.execute(_ITERATIVE_SCAN_SQL)
    rows = connection.execute(SIMILAR_SQL, {"id": company_id, "limit": limit}).fetchall()
    similar = [
        SimilarCompany(id=r[0], name=r[1], category=r[2], state=r[3], score=round(float(r[4]), 4))
        for r in rows
    ]
    return company, similar


def _problem(
    request: Request,
    status: int,
    detail: str,
    headers: Mapping[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "request_id": getattr(request.state, "request_id", None),
        **extra,
    }
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    configure_logging(get_settings().log_level)
    db.get_pool()
    log.info("api started", model=embedder.MODEL_NAME)
    try:
        yield
    finally:
        db.close_pool()
        log.info("api stopped")


app = FastAPI(
    title="Similar companies",
    summary="Recommends companies similar to a given one, for the OneCredit portal.",
    version="0.1.0",
    lifespan=_lifespan,
)


@app.middleware("http")
async def _request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()
    with structlog.contextvars.bound_contextvars(request_id=request_id):
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error", method=request.method, path=request.url.path)
            response = _problem(request, 500, "Something went wrong on our side.")
        took_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        route = getattr(request.scope.get("route"), "path", UNMATCHED_ROUTE)
        REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        REQUEST_SECONDS.labels(route).observe(took_ms / 1000)
        level = log.error if response.status_code >= 500 else log.info
        level(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            took_ms=round(took_ms, 2),
        )
    return response


@app.exception_handler(CompanyNotFoundError)
async def _company_not_found(request: Request, exc: CompanyNotFoundError) -> JSONResponse:
    return _problem(request, 404, f"No active company with registration number {exc.company_id}.")


@app.exception_handler(psycopg.OperationalError)
async def _database_unavailable(request: Request, exc: psycopg.OperationalError) -> JSONResponse:
    log.warning("database unavailable", error=str(exc))
    return _problem(
        request,
        503,
        "The database is unavailable. Try again shortly.",
        headers={"Retry-After": str(RETRY_AFTER_S)},
    )


@app.exception_handler(RequestValidationError)
async def _invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _problem(
        request, 422, "The request is not valid.", errors=jsonable_encoder(exc.errors())
    )


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return _problem(request, exc.status_code, str(exc.detail), headers=exc.headers)


@app.get(
    "/companies/{company_id}/similar",
    response_model=SimilarResponse,
    responses={
        404: {"description": "No active company with that registration number"},
        503: {"description": "The database is unavailable"},
    },
)
def similar_companies(
    company_id: str,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = get_settings().top_k,
) -> SimilarResponse:
    """Companies most similar to `company_id`, most similar first. Struck-off companies are
    never returned, and `company_id` is matched case- and whitespace-insensitively."""
    started = time.perf_counter()
    with db.conn() as connection:
        company, similar = find_similar(connection, normalise_company_id(company_id), limit)
    took_ms = (time.perf_counter() - started) * 1000
    return SimilarResponse(
        company=company,
        similar=similar,
        meta=Meta(model=embedder.MODEL_NAME, took_ms=round(took_ms, 2), count=len(similar)),
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up. Never touches the database."""
    return {"status": "ok"}


@app.get("/readyz", responses={503: {"description": "The database is unavailable"}})
def readyz() -> JSONResponse:
    """Readiness: 200 only if the database answers `SELECT 1`."""
    try:
        with db.get_pool().connection(timeout=READINESS_TIMEOUT_S) as connection:
            connection.execute("SELECT 1")
    except psycopg.Error as exc:
        log.warning("not ready", error=str(exc))
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus metrics for this process."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def main() -> None:
    """Serve the API on API_HOST:API_PORT until Ctrl+C."""
    settings = get_settings()
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, access_log=False)


if __name__ == "__main__":
    main()
