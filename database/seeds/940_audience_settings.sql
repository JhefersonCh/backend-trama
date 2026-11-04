-- Answer style settings. Idempotent; never overwrites values edited from the UI.
-- audience_level: auto (detailed + friendly, more technical once the user writes technically) | basico | tecnico
-- trato: usted (default, Colombian Spanish) | tu
INSERT INTO app_settings (key, value) VALUES
 ('audience_level', '"auto"'),
 ('trato', '"usted"')
ON CONFLICT (key) DO NOTHING;
