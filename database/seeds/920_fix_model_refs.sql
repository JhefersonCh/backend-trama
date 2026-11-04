-- Verified model refs (3 Nov 2026) against the pinned runtimes. Idempotent (UPDATE by profile name).
-- llama.cpp router id == model_ref ("<hf repo>:<quant>"); backend may override via params.router_model.

UPDATE model_profiles SET
  model_ref = 'LiquidAI/LFM3-1.7B-GGUF:Q4_K_M',
  params = (params - 'verify_ref') || '{"router_model":"LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"}'::jsonb
WHERE name = 'lfm3-1.7b';

-- EmbeddingGemma 2 GGUF exists: ggml-org/embeddinggemma-2-GGUF (BF16 558 MB / Q8_0 310 MB; mmproj files ignored, text only). 768 dims verified.
UPDATE model_profiles SET
  model_ref = 'ggml-org/embeddinggemma-2-GGUF:BF16',
  params = (params - 'verify_ref') || '{"dimension":768,"dtype":"bf16","router_model":"ggml-org/embeddinggemma-2-GGUF:BF16"}'::jsonb
WHERE name = 'embeddinggemma-2';

UPDATE model_profiles SET
  model_ref = 'Systran/faster-whisper-small',
  params = (params - 'verify_ref') || '{"language":"es","compute_type":"int8"}'::jsonb
WHERE name = 'faster-whisper-small';

UPDATE model_profiles SET
  model_ref = 'Systran/faster-whisper-base',
  params = (params - 'verify_ref') || '{"language":"es","compute_type":"int8"}'::jsonb
WHERE name = 'faster-whisper-base';

UPDATE model_profiles SET
  model_ref = 'speaches-ai/piper-es_ES-davefx-medium',
  params = (params - 'verify_ref') || '{"language":"es","voice":"davefx","sample_rate":22050}'::jsonb
WHERE name = 'piper-es';

UPDATE model_profiles SET
  model_ref = 'speaches-ai/Kokoro-82M-v1.0-ONNX',
  params = (params - 'verify_ref') || '{"language":"es","voice":"ef_dora","sample_rate":24000}'::jsonb
WHERE name = 'kokoro-es';
