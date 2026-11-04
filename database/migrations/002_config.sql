-- Configuration tables (UI -> DB). Contract with backend: do not rename.

CREATE TABLE model_providers (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name       text UNIQUE NOT NULL,
  kind       text NOT NULL CHECK (kind IN ('ollama','llamacpp','openai_compatible','voice_service')),
  base_url   text NOT NULL,
  enabled    boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_model_providers_updated BEFORE UPDATE ON model_providers
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE provider_secrets (
  provider_id uuid PRIMARY KEY REFERENCES model_providers(id) ON DELETE CASCADE,
  ciphertext  bytea NOT NULL,
  key_version int NOT NULL DEFAULT 1,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_provider_secrets_updated BEFORE UPDATE ON provider_secrets
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE model_profiles (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name               text UNIQUE NOT NULL,
  role               text NOT NULL CHECK (role IN ('llm','embedding','stt','tts')),
  provider_id        uuid NOT NULL REFERENCES model_providers(id) ON DELETE RESTRICT,
  model_ref          text NOT NULL,
  params             jsonb NOT NULL DEFAULT '{}',
  est_memory_mb      int,
  measured_memory_mb int,
  is_active          boolean NOT NULL DEFAULT false,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_model_profiles_one_active_per_role ON model_profiles (role) WHERE is_active;
CREATE INDEX idx_model_profiles_provider ON model_profiles (provider_id);
CREATE TRIGGER trg_model_profiles_updated BEFORE UPDATE ON model_profiles
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE app_settings (
  key        text PRIMARY KEY,
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_app_settings_updated BEFORE UPDATE ON app_settings
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE config_audit_logs (
  id         bigserial PRIMARY KEY,
  entity     text NOT NULL,
  entity_id  text,
  action     text NOT NULL,
  old_value  jsonb,
  new_value  jsonb,
  changed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_config_audit_entity ON config_audit_logs (entity, changed_at DESC);

CREATE TABLE model_load_events (
  id          bigserial PRIMARY KEY,
  profile_id  uuid REFERENCES model_profiles(id) ON DELETE SET NULL,
  event       text CHECK (event IN ('load','unload','evict','error')),
  duration_ms int,
  memory_mb   int,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_model_load_events_profile ON model_load_events (profile_id, created_at DESC);
