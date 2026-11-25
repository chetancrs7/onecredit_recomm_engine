# Evaluation results

Written by `just eval` (`python -m company_recs.eval`) from the companies in Postgres.

- **Ground truth:** a neighbour is relevant when its category equals the query's.
- **Queries:** 1855 active companies with a description longer than 10 characters. Left out: 98 with no category, 0 whose category has no other active member.
- **Candidates:** 1992 active companies with an embedding, the set the API recommends from.
- **Metrics:** k = 4. P@4 is the fraction of the 4 neighbours that are relevant; NDCG@4 uses binary relevance and a 1 / log2(rank + 1) discount; MRR is 1 / rank of the first relevant neighbour in the top 4, else 0. All are means over the queries.
- **Retrievers:** `random` (seed 20261125); `hashed-bow` (SHA-256 token hashing into 384 buckets, exact cosine); `bge-small` (BAAI/bge-small-en-v1.5, the stored embeddings through the API's SQL and HNSW index).

| Retriever | P@4 | NDCG@4 | MRR | N |
|---|---:|---:|---:|---:|
| random | 0.0880 | 0.0891 | 0.1740 | 1855 |
| hashed-bow | 0.9454 | 0.9397 | 0.9554 | 1855 |
| bge-small | 0.9523 | 0.9498 | 0.9712 | 1855 |

## Without exact-text twins

1651 of the 1855 queries have at least 4 other companies with exactly the same description, so the table above mostly measures whether a retriever finds those. Here each query's twins are removed from its candidates and from the ideal ranking for NDCG@4, which shows how well a retriever does beyond string matching.

| Retriever | P@4 | NDCG@4 | MRR | N |
|---|---:|---:|---:|---:|
| random | 0.0825 | 0.0820 | 0.1580 | 1855 |
| hashed-bow | 0.3344 | 0.3385 | 0.3695 | 1855 |
| bge-small | 0.5090 | 0.5127 | 0.5637 | 1855 |

## Sample queries

Top 4 from `bge-small`, as the API returns them.

### SYN-000010 · Murni Kirana Farm Trading

Agriculture · Perak · "Oil palm plantation and fresh fruit bunch supply"

| # | Name | Category | State | Score |
|---:|---|---|---|---:|
| 1 | Mega Mutiara Ladang Sdn. Bhd. | Agriculture | Labuan | 1.0000 |
| 2 | Sejahtera Golden Ladang Trading | Agriculture | Sabah | 1.0000 |
| 3 | Bestari Kirana Ladang Sdn. Bhd. | Agriculture | Selangor | 1.0000 |
| 4 | Murni Sejahtera Farm Trading | Agriculture | Johor | 1.0000 |

### SYN-000024 · Gemilang Prima Learning Sdn. Bhd.

Education · Selangor · "Pusat tuisyen untuk pelajar sekolah rendah dan menengah"

| # | Name | Category | State | Score |
|---:|---|---|---|---:|
| 1 | Murni Pacific Academy Sdn. Bhd. | — | — | 1.0000 |
| 2 | Everbright Sejahtera Pendidikan Sdn. Bhd. | Education | Melaka | 1.0000 |
| 3 | Golden Sinar Education Enterprise | Education | Kuala Lumpur | 1.0000 |
| 4 | Unity Mega Learning PLT | Education | Perak | 1.0000 |

### SYN-000025 · Sejahtera Mega Resort Sdn. Bhd.

Hospitality · Kuala Lumpur · "Boutique hotel in Kuantan"

| # | Name | Category | State | Score |
|---:|---|---|---|---:|
| 1 | Kirana Indah Hotel Sdn. Bhd. | Hospitality | Johor | 1.0000 |
| 2 | Jaya Unity Resort PLT | Hospitality | Sarawak | 1.0000 |
| 3 | Maju Seri Travel Sdn. Bhd. | Hospitality | Kuala Lumpur | 1.0000 |
| 4 | Unity Golden Homestay Sdn. Bhd. | Hospitality | — | 1.0000 |

### SYN-000012 · Mutiara Teguh Precision Trading

Manufacturing · Johor · "Pengeluaran sarung tangan getah untuk eksport"

| # | Name | Category | State | Score |
|---:|---|---|---|---:|
| 1 | Teguh Seri Precision Trading | Manufacturing | Kuala Lumpur | 1.0000 |
| 2 | Teguh Mutiara Packaging Trading | Manufacturing | Kuala Lumpur | 1.0000 |
| 3 | Mega Harmoni Packaging Trading | Manufacturing | Kuala Lumpur | 1.0000 |
| 4 | Harmoni Unity Manufacturing Sdn. Bhd. | Manufacturing | Selangor | 1.0000 |

### SYN-000017 · Maju Cahaya Care PLT

Healthcare · Selangor · "Community pharmacy and health supplements"

| # | Name | Category | State | Score |
|---:|---|---|---|---:|
| 1 | Indah Bestari Medical Sdn. Bhd. | Healthcare | Selangor | 1.0000 |
| 2 | Gemilang Kirana Klinik PLT | Healthcare | Pulau Pinang | 1.0000 |
| 3 | Indah Seri Health Enterprise | Healthcare | Terengganu | 1.0000 |
| 4 | Delima Maju Pharmacy Sdn. Bhd. | Healthcare | Selangor | 1.0000 |
