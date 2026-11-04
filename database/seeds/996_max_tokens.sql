-- Answer length limits per turn type (LLM max_tokens), editable from Configuración → Parámetros.
-- Doubled vs. the first hard-coded values so explanatory answers are not cut (2026-11-04).
INSERT INTO app_settings (key, value) VALUES ('max_tokens_by_intent', '{
  "diagnostic": {"text": 1100, "voice": 600},
  "guide":      {"text": 1100, "voice": 600},
  "output":     {"text": 1100, "voice": 600},
  "consulta":   {"text": 1400, "voice": 600},
  "clarify":    {"text": 500,  "voice": 500},
  "ticket":     {"text": 400,  "voice": 400},
  "inventory":  {"text": 400,  "voice": 400},
  "smalltalk":  {"text": 240,  "voice": 240},
  "closing":    {"text": 160,  "voice": 160},
  "plantilla":  {"text": 440,  "voice": 440},
  "extraccion": {"text": 300,  "voice": 300}
}'::jsonb) ON CONFLICT (key) DO NOTHING;
