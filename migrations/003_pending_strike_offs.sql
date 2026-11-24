CREATE TABLE IF NOT EXISTS pending_strike_offs (
    company_id text PRIMARY KEY,
    source_updated_at timestamptz NOT NULL,
    source_message_id text NOT NULL
);
