INSERT INTO model_providers (id, name, kind, base_url) VALUES
 ('00000000-0000-4000-8000-0000000000a1','ollama-local','ollama','http://llm-ollama:11434'),
 ('00000000-0000-4000-8000-0000000000a2','llamacpp-local','llamacpp','http://llm-llamacpp:8080'),
 ('00000000-0000-4000-8000-0000000000a3','voice-local','voice_service','http://voice:8000');

INSERT INTO model_profiles (name, role, provider_id, model_ref, params, est_memory_mb, is_active)
SELECT v.name, v.role, p.id, v.model_ref, v.params::jsonb, v.mem, v.active
FROM (VALUES
 ('lfm3-1.7b','llm','llamacpp-local','LiquidAI/LFM3-1.7B-GGUF:Q4_K_M','{"temperature":0.1,"top_k":50,"repeat_penalty":1.1,"ctx":8192}',1500,true),
 ('gemma4-e2b','llm','ollama-local','gemma4:e2b','{"temperature":0.2,"ctx":8192}',4000,false),
 ('gemma4-e4b','llm','ollama-local','gemma4:e4b','{"temperature":0.2,"ctx":8192}',8000,false),
 ('embeddinggemma-2','embedding','llamacpp-local','google/embeddinggemma-2-GGUF','{"dimension":768,"dtype":"bf16","verify_ref":true}',800,true),
 ('embeddinggemma-v1','embedding','ollama-local','embeddinggemma','{"dimension":768}',700,false),
 ('faster-whisper-small','stt','voice-local','Systran/faster-whisper-small','{"language":"es","compute_type":"int8"}',600,true),
 ('faster-whisper-base','stt','voice-local','Systran/faster-whisper-base','{"language":"es","compute_type":"int8"}',300,false),
 ('piper-es','tts','voice-local','rhasspy/piper-voices:es/es_ES/davefx/medium/es_ES-davefx-medium','{"language":"es","verify_ref":true}',100,true),
 ('kokoro-es','tts','voice-local','hexgrad/Kokoro-82M','{"language":"es","verify_ref":true}',500,false)
) AS v(name, role, prov, model_ref, params, mem, active)
JOIN model_providers p ON p.name = v.prov;

INSERT INTO app_settings (key, value) VALUES
 ('ram_budget_mb', '6000'),
 ('strict_single_model', 'false'),
 ('wake_word', '{"enabled":false,"phrase":"oye trama"}'),
 ('vad', '{"positive_threshold":0.5,"min_silence_ms":700}'),
 ('barge_in_min_ms', '300'),
 ('rag_top_k', '5'),
 ('system_prompt', to_jsonb($p$Eres TRAMA, un asistente local de diagnóstico de redes. Ayudas a identificar y resolver problemas de conectividad, DNS, DHCP, Wi-Fi, VLAN y similares.
Reglas:
- Antes de concluir, pide evidencia (salidas de comandos como ipconfig /all, ip addr, ping, nslookup, tracert) y usa las herramientas disponibles.
- Separa claramente hechos observados de hipótesis.
- Cita las fuentes de la base de conocimiento cuando las uses.
- Nunca pidas contraseñas, claves Wi-Fi, tokens ni credenciales.
- Nunca inventes el estado de un dispositivo; si no hay medición, dilo.
- Indica el origen de cada evidencia: aportada por el usuario, laboratorio simulado o vista desde el servidor.
- Responde en español, de forma breve y concreta.$p$::text));
