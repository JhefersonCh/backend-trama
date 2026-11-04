-- Operational data (doc 9.1). data_origin separates synthetic / lab / real (doc 15.10).

CREATE TABLE sites (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text UNIQUE NOT NULL,
  address_label text,
  description   text,
  data_origin   text NOT NULL DEFAULT 'real' CHECK (data_origin IN ('synthetic','lab','real')),
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE technicians (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name              text NOT NULL,
  email_or_username text UNIQUE,
  specialty         text,
  active            boolean NOT NULL DEFAULT true,
  data_origin       text NOT NULL DEFAULT 'real' CHECK (data_origin IN ('synthetic','lab','real')),
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE network_devices (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id       uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  name          text NOT NULL,
  device_type   text NOT NULL CHECK (device_type IN ('router','switch','access_point','firewall','server','workstation','printer','other')),
  vendor        text,
  model         text,
  management_ip inet,
  serial_number text,
  status        text NOT NULL DEFAULT 'unknown' CHECK (status IN ('active','inactive','maintenance','unknown')),
  data_origin   text NOT NULL DEFAULT 'real' CHECK (data_origin IN ('synthetic','lab','real')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (site_id, name)
);
CREATE INDEX idx_network_devices_site ON network_devices (site_id);
CREATE TRIGGER trg_network_devices_updated BEFORE UPDATE ON network_devices
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE networks (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id     uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  name        text NOT NULL,
  cidr        cidr NOT NULL,
  gateway     inet,
  dns_servers inet[] NOT NULL DEFAULT '{}',
  vlan_id     int CHECK (vlan_id BETWEEN 1 AND 4094),
  data_origin text NOT NULL DEFAULT 'real' CHECK (data_origin IN ('synthetic','lab','real')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (site_id, name)
);
CREATE INDEX idx_networks_site ON networks (site_id);

CREATE TABLE interfaces (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  device_id    uuid NOT NULL REFERENCES network_devices(id) ON DELETE CASCADE,
  name         text NOT NULL,
  mac_address  macaddr,
  ip_address   inet,
  vlan_id      int CHECK (vlan_id BETWEEN 1 AND 4094),
  admin_status text NOT NULL DEFAULT 'up' CHECK (admin_status IN ('up','down')),
  oper_status  text NOT NULL DEFAULT 'unknown' CHECK (oper_status IN ('up','down','unknown')),
  UNIQUE (device_id, name)
);
CREATE INDEX idx_interfaces_device ON interfaces (device_id);

CREATE TABLE incidents (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id     uuid REFERENCES sites(id) ON DELETE SET NULL,
  reporter_ref text,  -- opaque reference, no PII
  title       text NOT NULL,
  description text,
  category    text,
  priority    text NOT NULL DEFAULT 'medium' CHECK (priority IN ('low','medium','high','critical')),
  status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open','investigating','resolved','closed')),
  source_type text NOT NULL DEFAULT 'conversation' CHECK (source_type IN ('conversation','manual','monitoring','lab_scenario')),
  is_synthetic boolean NOT NULL DEFAULT false,
  data_origin text NOT NULL DEFAULT 'real' CHECK (data_origin IN ('synthetic','lab','real')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_incidents_site ON incidents (site_id);
CREATE INDEX idx_incidents_status ON incidents (status);
CREATE TRIGGER trg_incidents_updated BEFORE UPDATE ON incidents
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE SEQUENCE ticket_number_seq;

CREATE TABLE tickets (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_id            uuid NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  ticket_number          text UNIQUE NOT NULL DEFAULT ('TRM-' || lpad(nextval('ticket_number_seq')::text, 6, '0')),
  assigned_technician_id uuid REFERENCES technicians(id) ON DELETE SET NULL,
  status                 text NOT NULL DEFAULT 'open' CHECK (status IN ('open','assigned','in_progress','resolved','closed')),
  priority               text NOT NULL DEFAULT 'medium' CHECK (priority IN ('low','medium','high','critical')),
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  closed_at              timestamptz
);
ALTER SEQUENCE ticket_number_seq OWNED BY tickets.ticket_number;
CREATE INDEX idx_tickets_incident ON tickets (incident_id);
CREATE INDEX idx_tickets_technician ON tickets (assigned_technician_id);
CREATE TRIGGER trg_tickets_updated BEFORE UPDATE ON tickets
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE ticket_events (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id  uuid NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  event_type text NOT NULL,
  old_value  jsonb,
  new_value  jsonb,
  details    text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_ticket_events_ticket ON ticket_events (ticket_id, created_at);

CREATE TABLE diagnostic_runs (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_id uuid REFERENCES incidents(id) ON DELETE CASCADE,
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  status      text NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed','cancelled')),
  summary     text,
  confidence  numeric(3,2) CHECK (confidence BETWEEN 0 AND 1)
);
CREATE INDEX idx_diagnostic_runs_incident ON diagnostic_runs (incident_id);

CREATE TABLE diagnostic_observations (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  diagnostic_run_id uuid NOT NULL REFERENCES diagnostic_runs(id) ON DELETE CASCADE,
  observation_type  text NOT NULL,
  target            text,
  result            jsonb NOT NULL DEFAULT '{}',
  unit              text,
  collected_at      timestamptz NOT NULL DEFAULT now(),
  source            text NOT NULL CHECK (source IN ('user_provided','lab_simulated','server_view'))
);
CREATE INDEX idx_diag_obs_run ON diagnostic_observations (diagnostic_run_id);
