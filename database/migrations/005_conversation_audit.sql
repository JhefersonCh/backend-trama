CREATE TABLE conversation_sessions (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_ref   text,  -- opaque reference, no PII
  channel    text NOT NULL DEFAULT 'web' CHECK (channel IN ('web','voice','api')),
  status     text NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed')),
  started_at timestamptz NOT NULL DEFAULT now(),
  ended_at   timestamptz
);

CREATE TABLE conversation_messages (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id       uuid NOT NULL REFERENCES conversation_sessions(id) ON DELETE CASCADE,
  role             text NOT NULL CHECK (role IN ('system','user','assistant','tool')),
  content          text NOT NULL,
  input_mode       text NOT NULL DEFAULT 'text' CHECK (input_mode IN ('text','voice')),
  model_profile_id uuid REFERENCES model_profiles(id) ON DELETE SET NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_conv_messages_session ON conversation_messages (session_id, created_at);

CREATE TABLE voice_turns (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id          uuid NOT NULL REFERENCES conversation_sessions(id) ON DELETE CASCADE,
  transcript          text,
  interrupted         boolean NOT NULL DEFAULT false,
  stt_ms              int,
  rag_ms              int,
  llm_first_token_ms  int,
  tts_first_audio_ms  int,
  created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_voice_turns_session ON voice_turns (session_id, created_at);

CREATE TABLE tool_audit_logs (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id        uuid REFERENCES conversation_sessions(id) ON DELETE SET NULL,
  tool_name         text NOT NULL,
  arguments_summary text,
  result_summary    text,
  authorized_by     text,
  status            text NOT NULL CHECK (status IN ('ok','error','denied','timeout')),
  executed_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_tool_audit_session ON tool_audit_logs (session_id, executed_at);
