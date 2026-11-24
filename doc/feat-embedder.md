# Branch `feat/embedder`

Adds the embedding module: turns a company into the text to embed, and that text into a 384-dim vector, locally
on CPU with fastembed. No database or RabbitMQ code.

## What changed

| File | What it does |
|---|---|
| `src/company_recs/embedder.py` | `MODEL_NAME`, `MODEL_VERSION`, `EMBED_DIM`, `BATCH_SIZE`, `get_model()`, `build_embed_text()`, `embed_one()`, `embed_batch()`, `description_hash()`. |
| `tests/test_embedder.py` | Unit tests for the text and hash rules, and model tests (marked `model`) for vector size, length and similarity. |
| `pyproject.toml`, `uv.lock` | `numpy` added as a direct dependency (used to normalise vectors; it was only there through fastembed). New `model` pytest marker. |
| `DESIGN.md` | Embedding row of the missing-category table marked done. |

## The API

| Name | What it is |
|---|---|
| `MODEL_NAME` | `Settings.embed_model`, default `BAAI/bge-small-en-v1.5`. |
| `MODEL_VERSION` | Same as `MODEL_NAME` for v1. Stored alongside embeddings later, so vectors from different models are never mixed. |
| `get_model()` | The shared `fastembed.TextEmbedding`, created on first use (thread-safe), not on import. Importing the module stays fast. |
| `build_embed_text(name, category, state, description)` | The description if it has any non-whitespace text; otherwise `name \| category \| state`. |
| `embed_batch(texts)` | One call to the model's batch API. Returns 384-dim vectors of length 1. |
| `embed_one(text)` | `embed_batch([text])[0]`. |
| `description_hash(text)` | Hex SHA-256 of the UTF-8 text. Lets the consumer skip re-embedding text it has already embedded. |

## Decisions

- **The fallback leaves out missing parts instead of writing `None`.** The spec's literal format,
  `f"{name} | {category} | {state or ''}"`, would embed `"Kopi Co | None | Selangor"` when the category is missing,
  and `"Kopi Co | Food & Beverage | "` when the state is. DESIGN.md (Assumptions → Missing category) says never to embed
  `"None"`, so `build_embed_text` joins only the parts that are present: `name | category | state`,
  `name | state`, or just `name`. When all three are present the output is exactly the spec's format.
- **Vectors are normalised by us**, not left to the model. Each vector is checked to be 384-dim (matching
  `vector(384)` in the schema) and scaled to length 1, so cosine distance in pgvector behaves the same whichever model
  is configured. A wrong-sized vector raises an error instead of failing later at insert time.
- **Batch size 4 instead of fastembed's default 256.** On this machine (16 cores, Windows, onnxruntime CPU)
  throughput fell sharply as the batch grew:

  | Batch size | Texts per second |
  |---|---|
  | 1 | 128 |
  | 4 | 120 |
  | 8 | 56–67 |
  | 16 | 37 |
  | 32 | 13 |
  | 256 (default) | 5 |

  Embedding all 2,490 feed messages with usable data went from **177 s to 12 s** (about 205 per second). The numbers may
  differ on other machines, so this is worth re-measuring in the latency deep dive.
- **Only 220 distinct embed texts in the whole feed** (for about 2,000 companies). The consumer can use
  `description_hash` to embed each distinct text once.

## Things to note

- **The model is downloaded on first use** (about 70 MB) into fastembed's default cache, `%TEMP%\fastembed_cache`.
  A temp-folder cleanup can delete it, in which case it's downloaded again. On Windows without Developer Mode,
  huggingface_hub warns that it can't use symlinks; that's harmless.
- **`bge-small-en-v1.5` is English-only**, and part of the feed is in Malay. fastembed also offers
  `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, which is also 384-dim, so it can be swapped in via
  `EMBED_MODEL` with no schema change. Comparing the two belongs to the eval branch.

## How to check it

```powershell
uv run pytest -q tests/test_embedder.py                     # 17 tests; first run downloads the model
uv run pytest -q -m "not model and not integration"         # just the fast unit tests
```
