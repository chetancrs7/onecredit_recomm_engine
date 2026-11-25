# Branch `feat/api`

Adds the FastAPI service the portal calls for the "Similar companies" panel, plus health, readiness and metrics
endpoints.

## What changed

| File | What it does |
|---|---|
| `src/company_recs/api.py` | The app: `GET /companies/{id}/similar`, `/healthz`, `/readyz`, `/metrics`, the request-id/logging middleware, RFC 7807 error handlers, and `main()` (uvicorn). |
| `src/company_recs/models.py` | `normalise_company_id()` pulled out of the validator so the API matches ids the same way as the consumer. |
| `src/company_recs/config.py`, `.env.example` | `API_HOST` (default `127.0.0.1`) and `API_PORT` (default `8000`). |
| `Justfile` | `just api`. |
| `pyproject.toml` | Filters Starlette's warning about `httpx` in `TestClient`. |
| `tests/test_api.py` | Unit tests, no database: probes, 422/404/500/503 problem bodies, request ids, metrics labels. |
| `tests/test_api_similar.py` | Integration tests against pgvector with hand-made vectors whose scores are known exactly. |
| `DESIGN.md`, `SETUP.md` | The API section (shape, errors, probes, metrics, latency) and how to run it. |

## How a request is handled

1. The middleware gives the request a UUID, binds it into the structlog context, and starts a timer.
2. The id is normalised (`syn-001550` → `SYN-001550`).
3. One pooled connection, one transaction:
   - Look up the company (`WHERE id = %(id)s AND is_active`). Missing or struck off → `CompanyNotFoundError` → 404.
   - No embedding → `similar: []`.
   - `set_config('hnsw.iterative_scan', 'strict_order', true)`, then the spec's nearest-neighbour query.
4. `meta.took_ms` is the time for step 3 measured with `time.perf_counter()`. `meta.model` is
   `embedder.MODEL_NAME`.
5. The middleware sets `X-Request-ID`, records the metrics and logs `method`, `path`, `status`, `took_ms`.

## Where this differs from the spec

- **`AND embedding IS NOT NULL`** is added to the query. Without it, a company with no vector would sort last with
  a `NULL` score whenever the planner chooses a sequential scan. The index never contains those rows anyway.
- **A separate company lookup** runs before the spec's query, in the same transaction. It is needed for the
  `company` block, and it lets a missing company be told apart from one with no neighbours.
- **`hnsw.iterative_scan = strict_order`** is set for the transaction. Without it, a filtered HNSW scan only sees
  `ef_search` = 40 candidates, so `limit=50` (or a company with many struck-off neighbours) came back short. The
  test `test_without_iterative_scan_the_index_returns_too_few` shows this.
- **The default `limit` is `TOP_K`**, which defaults to 4, not a hard-coded 4. The setting already existed for this.
- **422 is also `application/problem+json`.** FastAPI still does the validation, and its error list is kept as an
  `errors` member; only the envelope changes, so the portal sees one error shape.
- **More error cases than the spec:** a database outage is a 503 problem with `Retry-After: 1`, an unknown path is
  a 404 problem, and any other exception is a 500 problem that never includes the exception text.
- **Every problem body carries `request_id`**, the same as the header.
- **Two API metrics** are added so `/metrics` has something about the API: `api_requests_total{method,route,status}`
  and `api_request_seconds{route}`. The label is the route template, so there's one series per endpoint.
- **`score` is rounded to 4 decimal places.**
- **Uvicorn's access log is off**, because the middleware already logs every request as JSON with more fields.
- **The API doesn't run migrations.** The consumer or `just migrate` does that. `/readyz` reports 503 until the
  database answers.

## Results on the real data

With the 2,000 companies from `feat/consumer` and `just api`:

| | |
|---|---|
| `SYN-001550` | Seri Sejahtera Food (1.0), Gemilang Delima Food (0.8336), Everbright Pacific Food (0.8336), Pacific Everbright Logistik (0.8301) |
| Struck-off `SYN-001748` | 404 `application/problem+json` |
| `limit=51` | 422 problem with FastAPI's `errors` |
| `limit=50` | 50 results |
| Query plan | `Index Scan using companies_embedding_hnsw`, 0.8 ms execution |
| 200 requests | `took_ms` about 8 ms; 198 finished end to end in under 10 ms |

## Tests

```powershell
uv run pytest -q tests/test_api.py           # 10 tests, no Docker
uv run pytest -q tests/test_api_similar.py   # 14 tests, Docker running
uv run pytest -q                             # everything: 117 tests, about 2 minutes
```

## Known limitations

- **Ties aren't ordered.** Gemilang Delima and Everbright Pacific have the same score for `SYN-001550`, and the
  mock shows them the other way round. Adding `, id` to the `ORDER BY` would stop the HNSW index from providing the
  order by itself, so I left it.
- **Similarity is on the embedding only.** Category and state play no part in ranking, so a Logistics company is
  fourth for a bakery. `feat/eval` will measure how often the category differs.
- **No caching.** Every request hits Postgres. At about 8 ms that is fine for now.
