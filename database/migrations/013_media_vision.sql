-- 013: media (images + audio) uploaded in a conversation, vision model role, retention setting. Idempotent.
CREATE TABLE IF NOT EXISTS media (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id  uuid NOT NULL REFERENCES conversation_sessions(id) ON DELETE CASCADE,
  kind        text NOT NULL CHECK (kind IN ('image','audio')),
  mime        text NOT NULL,
  path        text NOT NULL,                 -- relative to MEDIA_DIR (volume media-data -> /data/media)
  size_bytes  int,
  width       int,
  height      int,
  analysis    jsonb,                         -- vision result (image only); NULL = not analyzed
  transcript  text,                          -- audio only
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_media_session ON media (session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_media_created ON media (created_at);

-- model_profiles.role now accepts 'vision' (the inline CHECK from 002 is named model_profiles_role_check).
ALTER TABLE model_profiles DROP CONSTRAINT IF EXISTS model_profiles_role_check;
ALTER TABLE model_profiles ADD CONSTRAINT model_profiles_role_check
  CHECK (role IN ('llm','embedding','stt','tts','vision'));

-- Days to keep uploaded files (0 = keep forever).
INSERT INTO app_settings (key, value) VALUES ('media_retention_days', '30') ON CONFLICT (key) DO NOTHING;
