-- Vision profiles, INACTIVE by default (activate from the UI). Idempotent (by profile name).
-- gemma4-e2b-vision: separate llama.cpp router section (with the mmproj projector) so text turns stay light.
-- deepseek-vision-exp: only if a DeepSeek provider exists (matched by name); it is EXTERNAL (est_memory_mb = 0)
-- and the first upload asks the person for consent before sending the photo.
INSERT INTO model_profiles (name, role, provider_id, model_ref, params, est_memory_mb, is_active)
SELECT 'gemma4-e2b-vision', 'vision', p.id, 'unsloth/gemma-4-E2B-it-GGUF:Q4_K_M',
       '{"router_model":"gemma-4-E2B-it-vision","temperature":0.1,"max_tokens":400,"vision_max_px":896}'::jsonb, 4500, false
FROM model_providers p WHERE p.name = 'llamacpp-local'
ON CONFLICT (name) DO NOTHING;

INSERT INTO model_profiles (name, role, provider_id, model_ref, params, est_memory_mb, is_active)
SELECT 'deepseek-vision-exp', 'vision', p.id, 'deepseek-v4-flash-vision-exp',
       '{"temperature":0.1,"max_tokens":400,"vision_max_px":1024}'::jsonb, 0, false
FROM model_providers p WHERE lower(p.name) LIKE '%deepseek%'
ORDER BY p.created_at LIMIT 1
ON CONFLICT (name) DO NOTHING;
