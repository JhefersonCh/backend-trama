-- Agent settings (spec Fase 0 §1.3). Idempotent.
INSERT INTO app_settings (key, value) VALUES
  ('cf_thresholds', '{"accept":0.8,"explore_min":0.4,"reject":-0.5}'),
  ('autonomy_policy', '{"ticket_create_on_request":"con_aviso","ticket_create_own_initiative":"con_confirmacion","assign_technician":"con_aviso","renew_ip":"con_confirmacion","share_contact":"con_confirmacion","close_case":"con_confirmacion"}'),
  ('followup_hours', '{"after_ticket":24,"after_step":0.25}')
ON CONFLICT (key) DO NOTHING;
