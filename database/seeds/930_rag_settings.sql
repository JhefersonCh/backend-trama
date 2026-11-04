-- Deterministic RAG settings + latency defaults. Idempotent; never overwrites values edited from the UI.

-- rag_mode: auto = backend always retrieves and injects context (search_knowledge not offered to the LLM);
--           tool = the LLM decides via search_knowledge (legacy); both = injected context + tool.
-- rag_min_score: cosine similarity floor for injected hits. Calibrated on the seed corpus with EmbeddingGemma-2:
--   chit-chat / ticket questions 0.60-0.66 (noise floor), elliptical follow-ups ~0.69, relevant hits 0.71-0.80.
INSERT INTO app_settings (key, value) VALUES
 ('rag_mode', '"auto"'),
 ('rag_min_score', '0.7')
ON CONFLICT (key) DO NOTHING;

-- Bound generation on CPU (voice-friendly answers): only when the profile has no max_tokens yet.
UPDATE model_profiles
SET params = params || jsonb_build_object('max_tokens', 400)
WHERE name = 'lfm3-1.7b' AND NOT (params ? 'max_tokens');

-- Shorter base prompt (the non-negotiable rules live in code, RULES_ADDENDUM). Only replaces the ORIGINAL
-- seeded text, so a prompt edited from the UI is kept.
UPDATE app_settings
SET value = to_jsonb($p$Eres TRAMA, asistente local de diagnóstico de redes (conectividad, DNS, DHCP, Wi-Fi, VLAN).$p$::text)
WHERE key = 'system_prompt'
  AND value = to_jsonb($p$Eres TRAMA, un asistente local de diagnóstico de redes. Ayudas a identificar y resolver problemas de conectividad, DNS, DHCP, Wi-Fi, VLAN y similares.
Reglas:
- Antes de concluir, pide evidencia (salidas de comandos como ipconfig /all, ip addr, ping, nslookup, tracert) y usa las herramientas disponibles.
- Separa claramente hechos observados de hipótesis.
- Cita las fuentes de la base de conocimiento cuando las uses.
- Nunca pidas contraseñas, claves Wi-Fi, tokens ni credenciales.
- Nunca inventes el estado de un dispositivo; si no hay medición, dilo.
- Indica el origen de cada evidencia: aportada por el usuario, laboratorio simulado o vista desde el servidor.
- Responde en español, de forma breve y concreta.$p$::text);
