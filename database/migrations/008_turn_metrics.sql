-- Per-turn metrics (text + voice) for the Métricas / Diagnóstico views. Idempotent.
CREATE TABLE IF NOT EXISTS turn_metrics (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id           uuid NOT NULL REFERENCES conversation_sessions(id) ON DELETE CASCADE,
  turn_id              text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  input_mode           text,
  intent               text,
  audience             text,
  model_profile_id     uuid,
  stt_ms               int,
  rag_ms               int,
  llm_first_token_ms   int,
  tts_first_audio_ms   int,
  total_ms             int,
  prompt_tokens        int,
  cached_tokens        int,
  completion_tokens    int,
  finish_reason        text,
  sources_count        int,
  top_source_score     real,
  tool_calls           int,
  tool_errors          int,
  interrupted          boolean,
  voseo_hits           int,
  citations_removed    int,
  final_text_rewritten boolean,
  case_state           jsonb
);
CREATE INDEX IF NOT EXISTS idx_turn_metrics_created ON turn_metrics (created_at);
CREATE INDEX IF NOT EXISTS idx_turn_metrics_session ON turn_metrics (session_id);
