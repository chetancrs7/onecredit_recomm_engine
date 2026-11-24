CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS companies (
    id                text PRIMARY KEY,
    name              text NOT NULL,
    category          text,
    state             text,
    description       text,
    description_hash  text,
    embedding         vector(384),
    source_updated_at timestamptz NOT NULL,
    source_message_id text NOT NULL,
    ingested_at       timestamptz NOT NULL DEFAULT now(),
    is_active         boolean NOT NULL DEFAULT true
);

CREATE INDEX IF NOT EXISTS companies_embedding_hnsw
    ON companies USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS companies_category_state_idx
    ON companies (category, state);

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id   text PRIMARY KEY,
    processed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS company_events (
    id          bigserial PRIMARY KEY,
    message_id  text,
    company_id  text,
    received_at timestamptz DEFAULT now(),
    raw         jsonb
);
