-- 010: technicians (skills, zones, capacity, schedule), ticket assignment fields, encrypted ticket contacts. Idempotent.
ALTER TABLE technicians
  ADD COLUMN IF NOT EXISTS specialties      text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS zones            text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS level            text NOT NULL DEFAULT 'n1' CHECK (level IN ('n1','n2')),
  ADD COLUMN IF NOT EXISTS max_open_tickets int  NOT NULL DEFAULT 5 CHECK (max_open_tickets > 0),
  ADD COLUMN IF NOT EXISTS availability     text NOT NULL DEFAULT 'disponible'
                           CHECK (availability IN ('disponible','ocupado','fuera_de_turno','vacaciones')),
  ADD COLUMN IF NOT EXISTS schedule         jsonb NOT NULL DEFAULT '{"lun-vie":["08:00","17:00"]}',
  ADD COLUMN IF NOT EXISTS contact_channel  text,
  ADD COLUMN IF NOT EXISTS updated_at       timestamptz NOT NULL DEFAULT now();

ALTER TABLE tickets
  ADD COLUMN IF NOT EXISTS session_id      uuid REFERENCES conversation_sessions(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS category        text,
  ADD COLUMN IF NOT EXISTS zone            text,
  ADD COLUMN IF NOT EXISTS service_mode    text CHECK (service_mode IN ('remoto','visita')),
  ADD COLUMN IF NOT EXISTS preferred_slot  text CHECK (preferred_slot IN ('manana','tarde','noche','cualquiera')),
  ADD COLUMN IF NOT EXISTS sla_due_at      timestamptz,
  ADD COLUMN IF NOT EXISTS handoff_summary jsonb;
CREATE INDEX IF NOT EXISTS idx_tickets_session ON tickets (session_id);

CREATE TABLE IF NOT EXISTS ticket_contacts (
  ticket_id     uuid PRIMARY KEY REFERENCES tickets(id) ON DELETE CASCADE,
  ciphertext    bytea NOT NULL,
  consent_at    timestamptz NOT NULL DEFAULT now()
);

-- Backfill: tickets.session_id from incidents.reporter_ref when it is the id of an existing session.
UPDATE tickets t SET session_id = s.id
FROM incidents i JOIN conversation_sessions s ON s.id::text = lower(i.reporter_ref)
WHERE i.id = t.incident_id AND t.session_id IS NULL;
-- Backfill: tickets.category from the incident.
UPDATE tickets t SET category = i.category
FROM incidents i WHERE i.id = t.incident_id AND t.category IS NULL AND i.category IS NOT NULL;
