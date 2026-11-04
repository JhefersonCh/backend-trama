-- 011: full extracted text of each knowledge document (for the document reader). Idempotent; NULL = rebuilt lazily from chunks.
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS full_text text;
