"""PostgreSQL schema. Applied idempotently at start-up (`haris init-db` runs the same code)."""

from __future__ import annotations

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
  id          TEXT PRIMARY KEY,
  doc_id      TEXT NOT NULL,
  collection  TEXT NOT NULL,
  title       TEXT NOT NULL,
  source      TEXT NOT NULL,
  sensitivity SMALLINT NOT NULL,
  content     TEXT NOT NULL,
  embedding   vector({dim}) NOT NULL,
  tsv         tsvector GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || content)) STORED,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_tsv_gin ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_acl ON chunks (collection, sensitivity);

CREATE TABLE IF NOT EXISTS usage_daily (
  team     TEXT NOT NULL,
  day      DATE NOT NULL,
  tokens   BIGINT NOT NULL DEFAULT 0,
  requests INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (team, day)
);

CREATE TABLE IF NOT EXISTS audit_log (
  seq         BIGSERIAL PRIMARY KEY,
  record_json TEXT NOT NULL,
  record      JSONB GENERATED ALWAYS AS (record_json::jsonb) STORED,
  prev_hash   TEXT NOT NULL,
  hash        TEXT NOT NULL UNIQUE
);

CREATE OR REPLACE FUNCTION haris_audit_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log;
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION haris_audit_append_only();
"""


def init_schema(pool, embed_dim: int) -> None:
    with pool.connection() as conn:
        conn.execute(SCHEMA.format(dim=int(embed_dim)))
