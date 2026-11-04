-- 012: configurable tools registry (+ versions), audit extras, read-only role and whitelisted views. Idempotent.
CREATE TABLE IF NOT EXISTS agent_tools (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text UNIQUE NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{2,40}$'),
  kind          text NOT NULL CHECK (kind IN ('builtin','static_template','rag_search','sql_readonly','http_request','client_tool')),
  description   text NOT NULL,
  params_schema jsonb NOT NULL DEFAULT '{"type":"object","properties":{}}',
  config        jsonb NOT NULL DEFAULT '{}',
  intents       text[] NOT NULL DEFAULT '{}',
  enabled       boolean NOT NULL DEFAULT true,
  version       int NOT NULL DEFAULT 1,
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS agent_tool_versions (
  tool_id    uuid REFERENCES agent_tools(id) ON DELETE CASCADE,
  version    int,
  snapshot   jsonb NOT NULL,
  created_at timestamptz DEFAULT now(),
  PRIMARY KEY (tool_id, version)
);

ALTER TABLE tool_audit_logs
  ADD COLUMN IF NOT EXISTS tool_version int,
  ADD COLUMN IF NOT EXISTS duration_ms  int;
CREATE INDEX IF NOT EXISTS idx_tool_audit_name ON tool_audit_logs (tool_name, executed_at);

-- Whitelisted views for sql_readonly tools. No contact data. Views run with the owner's rights.
CREATE OR REPLACE VIEW v_tools_technicians_available AS
  SELECT t.name, t.specialties, t.zones, t.level, t.availability
  FROM technicians t
  WHERE t.active AND t.availability = 'disponible';

CREATE OR REPLACE VIEW v_tools_open_incidents_by_zone AS
  SELECT t.zone, t.category, count(*)::int AS count, max(t.created_at) AS latest
  FROM tickets t
  WHERE t.zone IS NOT NULL AND t.status IN ('open','assigned','in_progress')
  GROUP BY t.zone, t.category;

-- Read-only role: NOLOGIN, SELECT only on the whitelisted views; the app user does SET LOCAL ROLE to it.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trama_tools_ro') THEN
    CREATE ROLE trama_tools_ro NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
  EXECUTE format('GRANT trama_tools_ro TO %I', current_user);
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO trama_tools_ro', current_database());
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM trama_tools_ro;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM trama_tools_ro;
GRANT USAGE ON SCHEMA public TO trama_tools_ro;
GRANT SELECT ON v_tools_technicians_available, v_tools_open_incidents_by_zone TO trama_tools_ro;
ALTER ROLE trama_tools_ro SET statement_timeout = '2s';
ALTER ROLE trama_tools_ro SET default_transaction_read_only = on;
