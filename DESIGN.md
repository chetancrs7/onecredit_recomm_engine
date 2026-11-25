# Design

Work in progress. Sections are filled in as each branch lands; per-branch notes are in [`doc/`](doc/).

## Assumptions

### Missing category

`category` is nullable because 134 feed messages omit `industry`; we do not invent a category.

A placeholder such as `Unknown` would make every company without an industry look alike, and they would end up
recommended to each other. How each layer handles a missing category:

| Layer | Rule | Status |
|---|---|---|
| Schema | `companies.category` is `text`, nullable. | Done (`feat/schema-and-db`) |
| Consumer | A message without a category never clears one we already have: `category = COALESCE(EXCLUDED.category, companies.category)`. A new company with no category is stored with `category = NULL`. | Done (`feat/consumer`) |
| Embedding | With no description, embed `name \| category \| state`; with no category either, `name \| state`. Never embed the text `"None"`. | Done (`feat/embedder`, `build_embed_text`) |
| API | Return `"category": null` and let the portal decide how to show it. | Done (`feat/api`) |
| Evaluation | Companies with no category are left out of the category-match ground truth, and the eval table reports `N` so the exclusion is visible. | Planned (`feat/eval`) |

### The feed, as it really arrives

The registry team doesn't validate what upstream sends, and `data/feed.jsonl` shows it. Of the 2,518 messages,
2,481 are usable and describe all 2,000 companies; the other 37 go to `company-events-dlq` with an `error` header.

| What the feed does | How many | What we do |
|---|---|---|
| Not valid JSON / the body is `null` | 5 / 6 | Dead-letter. |
| No `registrationNumber`, or a number instead of a string (name `"X"`) | 7 / 3 | Dead-letter: no reliable company to attach it to. |
| No `updatedAt` (these are also the `"Unknown"` placeholder updates) | 7 | Dead-letter: without a time we can't order it, and applying it could overwrite good data. |
| `data` missing, or a string like `"companyName=Foo; state=Perak"` | 5 / 4 | Dead-letter. |
| Registration number with spaces or lower case (`" syn-000001 "`) | 49 | Normalise: strip all whitespace, upper-case. |
| `updatedAt` as `Z`, as epoch milliseconds, or with no offset | 28 / 30 / 14 | Parse all of them; a timestamp with no offset is read as Malaysia time (+08:00), which lines up with its neighbours in the feed. |
| State not in title case (`"KUALA LUMPUR"`, `"selangor"`) | 388 | Normalise to `Kuala Lumpur`, `Selangor`. |
| `natureOfBusiness` empty or `null`; `industry` `null`; `state` `null` | 15 + 29 / 120 / 60 | Store `NULL`; embed from `name \| category \| state` (see above). |
| Extra envelope fields (`source`, `ingestedBy`) | 75 | Ignored for the company; kept in the `company_events` audit row. |
| Exact duplicates (same `message_id`) | 63 | `processed_messages` turns the second one into a no-op. |
| An older update arriving after a newer one | 66 | Dropped (last write wins on `updatedAt`). |
| `company-struck-off` | 8 | `is_active = false`; the row and its vector stay for history. |

### Ordering and merging

- **Last write wins on `updatedAt`**, with `message_id` as the tie-break, so any arrival order ends with the same
  event applied. `test_chaos.py` checks this by replaying the feed in order, reversed, and shuffled with 20%
  redeliveries, and comparing the results.
- **A field an event leaves out keeps the stored value**: category (as above), and also state and description.
  We read a missing field as "this source didn't say", not "it was removed".
- **Limitation:** that merge is per row, not per field. If a newer event without a category arrives *before* an
  older event with one, the older event is dropped as stale and the category stays `NULL`. The latest event still
  wins; only the gap-filling depends on order. Fixing it needs a timestamp per field. In the feed this can only
  affect companies whose sole `industry` is in an out-of-order message.
- **A strike-off that arrives before the company's first upsert** is held in `pending_strike_offs`. When the upsert
  arrives, the company is stored as struck off if the strike-off is newer, so a delayed upsert can't bring it back.

## The API

`GET /companies/{company_id}/similar?limit=4` returns what the portal needs for the panel:

```json
{
  "company": {"id": "SYN-001550", "name": "Sinar Teguh Bakery Sdn. Bhd.", "category": "Food & Beverage", "state": "Selangor"},
  "similar": [{"id": "SYN-000539", "name": "Seri Sejahtera Food Sdn. Bhd.", "category": "Food & Beverage", "state": "Johor", "score": 1.0}],
  "meta": {"model": "BAAI/bge-small-en-v1.5", "took_ms": 12.5, "count": 4}
}
```

- **One nearest-neighbour query in Postgres.** `ORDER BY embedding <=> (SELECT embedding FROM q)` uses the HNSW
  index (`vector_cosine_ops`), the queried company is excluded with `id <>`, and struck-off companies with
  `is_active`. `score` is cosine similarity (`1 - distance`), rounded to 4 places.
- **Iterative index scans are on** (`hnsw.iterative_scan = strict_order`, per transaction). A filtered HNSW scan
  only looks at `hnsw.ef_search` (40) candidates, so `limit=50`, or many struck-off neighbours, would come back
  short. With the setting it keeps scanning until the limit is filled, still in exact distance order.
  `test_api_similar.py` shows both: the forced index scan returns all 50 with the setting and fewer without it.
- **`limit`** is 1 to 50, default `TOP_K` (4, the number of cards in the mock).
- **Ids are matched like the feed**: `syn-001550` and ` SYN-001550 ` find `SYN-001550`.
- **Struck-off and unknown companies are both 404.** The portal shouldn't show a panel for a company that no longer
  trades, and recommending away from it would be odd.
- **A company with no embedding** (not possible through the consumer, but possible in the table) gets `200` with
  an empty `similar`, rather than an error.

### What the portal gets when something goes wrong

Every error is an RFC 7807 problem, `application/problem+json`, with the same `request_id` as the
`X-Request-ID` header and the log line:

| Status | When |
|---|---|
| 404 | No active company with that id, or an unknown path. |
| 422 | `limit` outside 1 to 50, or not a number. The body adds FastAPI's `errors` list. |
| 503 | Postgres is down or the pool is exhausted. `Retry-After: 1`. |
| 500 | Anything else. The body never includes the exception text; the log line does. |

### Probes and metrics

- `/healthz` is liveness and never touches the database, so a database outage doesn't get the pods restarted.
- `/readyz` is readiness: `SELECT 1` with a 2 s pool timeout, 503 otherwise, so traffic moves away during an outage.
- `/metrics`: `api_requests_total{method,route,status}` and the `api_request_seconds{route}` histogram. The label
  is the route template, never the raw path, so there is one series per endpoint rather than per company.

On this laptop, against the 2,000-company table: the query itself runs in under 1 ms, `took_ms` is about 8 ms
(including borrowing a pooled connection), and 198 of 200 requests finished end to end in under 10 ms.
