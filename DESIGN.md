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
| Consumer | A message without a category never clears one we already have: `category = COALESCE(EXCLUDED.category, companies.category)`. A new company with no category is stored with `category = NULL`. | Planned (`feat/consumer`) |
| Embedding | With no description, embed `name \| category \| state`; with no category either, `name \| state`. Never embed the text `"None"`. | Done (`feat/embedder`, `build_embed_text`) |
| API | Return `"category": null` and let the portal decide how to show it. | Planned (`feat/api`) |
| Evaluation | Companies with no category are left out of the category-match ground truth, and the eval table reports `N` so the exclusion is visible. | Planned (`feat/eval`) |
