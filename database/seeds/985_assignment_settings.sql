-- Assignment settings (never overwrites values edited from the UI). Idempotent.
INSERT INTO app_settings (key, value) VALUES
 ('assignment_strategy', '"menor_carga"'),
 ('sla_hours', '{"critical":4,"high":8,"medium":24,"low":72}'),
 ('assignment_questions', '["zona","modalidad","franja","contacto"]'),
 ('zones', '["mocoa","puerto_asis","sibundoy","orito","valle_del_guamuez","villagarzon"]')
ON CONFLICT (key) DO NOTHING;
