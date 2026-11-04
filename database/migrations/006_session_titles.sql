-- Multiple chats (ChatGPT-like): title, last activity, archive flag. Idempotent.
ALTER TABLE conversation_sessions
  ADD COLUMN IF NOT EXISTS title      text,
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS archived   boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_conv_sessions_updated ON conversation_sessions (updated_at DESC);

-- Backfill sessions created before this migration: last activity + title from the first user message.
UPDATE conversation_sessions s
SET updated_at = COALESCE((SELECT max(m.created_at) FROM conversation_messages m WHERE m.session_id = s.id), s.started_at),
    title = COALESCE(s.title, (
      SELECT left(regexp_replace(m.content, '\s+', ' ', 'g'), 60)
      FROM conversation_messages m
      WHERE m.session_id = s.id AND m.role = 'user'
      ORDER BY m.created_at LIMIT 1))
WHERE s.title IS NULL;
