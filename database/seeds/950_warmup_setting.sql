-- warmup_on_start: load the active llm + embedding profiles in the background after backend startup. Idempotent.
INSERT INTO app_settings (key, value) VALUES ('warmup_on_start', 'true') ON CONFLICT (key) DO NOTHING;
