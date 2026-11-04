-- Agent core (spec Fase 0 §1.1). Idempotent.
-- Caso = una conversación con un objetivo. 1:1 con conversation_sessions.
CREATE TABLE IF NOT EXISTS cases (
  session_id   uuid PRIMARY KEY REFERENCES conversation_sessions(id) ON DELETE CASCADE,
  state        text NOT NULL DEFAULT 'nuevo' CHECK (state IN
               ('nuevo','entendiendo','diagnosticando','intentando_solucion','verificando',
                'resuelto','escalado','seguimiento','cerrado')),
  goal         text NOT NULL DEFAULT 'restablecer_conectividad',
  subgoals     jsonb NOT NULL DEFAULT '[]',          -- [{key, label, done}]
  ticket_id    uuid REFERENCES tickets(id) ON DELETE SET NULL,
  verified     boolean NOT NULL DEFAULT false,      -- resolución confirmada con evidencia
  opened_at    timestamptz NOT NULL DEFAULT now(),
  resolved_at  timestamptz,
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Base de hechos (memoria de trabajo). Un hecho nuevo con la misma key "reemplaza" al anterior (superseded_at).
CREATE TABLE IF NOT EXISTS case_facts (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id    uuid NOT NULL REFERENCES conversation_sessions(id) ON DELETE CASCADE,
  key           text NOT NULL,                       -- p. ej. 'luz_modem', 'otros_equipos_navegan'
  value         jsonb NOT NULL,                      -- true/false/'roja'/'169.254.10.2'…
  source        text NOT NULL CHECK (source IN
                ('user_reported','browser_measured','local_agent','router','public_api','photo','technician','system')),
  certainty     real NOT NULL DEFAULT 1.0 CHECK (certainty BETWEEN 0 AND 1),  -- confianza en el hecho (STT ruidoso < 1)
  evidence_ref  uuid,                                -- tool_audit_logs.id o diagnostic_observations.id
  turn_id       text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  superseded_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_case_facts_active ON case_facts (session_id, key) WHERE superseded_at IS NULL;

-- Hipótesis con factor de certeza (−1..1), recalculadas por el motor.
CREATE TABLE IF NOT EXISTS case_hypotheses (
  session_id  uuid NOT NULL REFERENCES conversation_sessions(id) ON DELETE CASCADE,
  key         text NOT NULL,                         -- 'H1_caida_proveedor'…
  cf          real NOT NULL DEFAULT 0 CHECK (cf BETWEEN -1 AND 1),
  status      text NOT NULL DEFAULT 'viva' CHECK (status IN ('viva','aceptada','descartada')),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, key)
);

-- Traza de decisiones (módulo de explicación + auditoría del agente).
CREATE TABLE IF NOT EXISTS decision_trace (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id    uuid NOT NULL REFERENCES conversation_sessions(id) ON DELETE CASCADE,
  turn_id       text,
  phase         text NOT NULL CHECK (phase IN ('percibir','creer','decidir','actuar','verificar','proactivo')),
  facts_used    jsonb NOT NULL DEFAULT '[]',         -- [{key, value, source}]
  rules_fired   text[] NOT NULL DEFAULT '{}',        -- ['R3','R12']
  hypotheses    jsonb NOT NULL DEFAULT '[]',         -- snapshot [{key, cf, status}]
  action        text,                                -- 'ask:otros_equipos', 'tool:browser_connectivity_check', 'ticket:create'
  autonomy      text CHECK (autonomy IN ('autonomo','con_aviso','con_confirmacion')),
  reason        text,                                -- frase corta legible ("discrimina H1 vs H2")
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_decision_trace_session ON decision_trace (session_id, created_at);

-- Reglas versionadas (editables desde la UI; el YAML del repo es el seed inicial).
CREATE TABLE IF NOT EXISTS rule_sets (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name         text NOT NULL DEFAULT 'diagnostico_red',
  version      int  NOT NULL,
  content_yaml text NOT NULL,
  is_active    boolean NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (name, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rule_sets_active ON rule_sets (name) WHERE is_active;

-- Proactividad: trabajos programados.
CREATE TABLE IF NOT EXISTS scheduled_jobs (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind        text NOT NULL CHECK (kind IN ('followup','sla_watch','outage_watch','remeasure','step_timer')),
  session_id  uuid REFERENCES conversation_sessions(id) ON DELETE CASCADE,
  ticket_id   uuid REFERENCES tickets(id) ON DELETE CASCADE,
  run_at      timestamptz NOT NULL,
  payload     jsonb NOT NULL DEFAULT '{}',
  status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','failed','cancelled')),
  attempts    int NOT NULL DEFAULT 0,
  last_error  text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON scheduled_jobs (run_at) WHERE status = 'pending';

-- Mensajes proactivos que esperan a que la persona vuelva (o se envían por WS si está conectada).
CREATE TABLE IF NOT EXISTS pending_notifications (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id  uuid NOT NULL REFERENCES conversation_sessions(id) ON DELETE CASCADE,
  text        text NOT NULL,
  kind        text NOT NULL,                         -- 'followup','ticket_update','outage_update'
  delivered_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);
