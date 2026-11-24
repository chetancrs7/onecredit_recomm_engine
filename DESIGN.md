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
| API | Return `"category": null` and let the portal decide how to show it. | Planned (`feat/api`) |
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
