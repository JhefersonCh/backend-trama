-- Knowledge base (RAG). Kept logically separate from operational tables.

CREATE TABLE knowledge_documents (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title       text NOT NULL,
  source_uri  text,
  publisher   text,
  version     text,
  license     text,
  language    text NOT NULL DEFAULT 'es',
  checksum    text,
  category    text,
  is_synthetic boolean NOT NULL DEFAULT false,
  data_origin text NOT NULL DEFAULT 'real' CHECK (data_origin IN ('synthetic','lab','real','public_doc')),
  ingested_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (checksum)
);

CREATE TABLE knowledge_chunks (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
  chunk_index int NOT NULL,
  content     text NOT NULL,
  metadata    jsonb NOT NULL DEFAULT '{}',
  UNIQUE (document_id, chunk_index)
);
CREATE INDEX idx_knowledge_chunks_fts ON knowledge_chunks USING gin (to_tsvector('spanish', content));

-- Embedding dimension is intentionally NOT fixed in the column type, so several
-- embedding profiles (different dims) can coexist.
-- HNSW indexes are created per active profile by the backend at reindex time, as a
-- partial expression index, e.g.:
--   CREATE INDEX ... ON chunk_embeddings USING hnsw ((embedding::vector(768)) vector_cosine_ops)
--   WHERE embedding_profile_id = '<profile uuid>';
-- Queries must use the same cast expression to hit the index.
CREATE TABLE chunk_embeddings (
  chunk_id             uuid NOT NULL REFERENCES knowledge_chunks(id) ON DELETE CASCADE,
  embedding_profile_id uuid NOT NULL REFERENCES model_profiles(id) ON DELETE CASCADE,
  dimension            int NOT NULL,
  embedding            vector NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (chunk_id, embedding_profile_id)
);
CREATE INDEX idx_chunk_embeddings_profile ON chunk_embeddings (embedding_profile_id);

CREATE TABLE reindex_jobs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  from_profile_id uuid REFERENCES model_profiles(id) ON DELETE SET NULL,
  to_profile_id   uuid NOT NULL REFERENCES model_profiles(id) ON DELETE CASCADE,
  status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','cancelled')),
  progress        numeric(5,2) NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  error           text,
  started_at      timestamptz,
  finished_at     timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_reindex_jobs_status ON reindex_jobs (status);
