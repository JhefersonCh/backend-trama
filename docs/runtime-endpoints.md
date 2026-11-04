# Model runtime endpoints (verified 3 Nov 2026, CPU, WSL2)

All runtimes sit on the default compose network (not `trama-net`), start EMPTY, and have no host ports.
Pinned images: `ghcr.io/ggml-org/llama.cpp:server-b12063`, `ollama/ollama:0.40.14`, `ghcr.io/speaches-ai/speaches:0.9.0-rc.3-cpu`.

## llama.cpp (`http://llm-llamacpp:8080`, router mode, `--models-max 2`)

Router model id == `model_ref` as seeded (`<hf repo>:<quant>`); verified, so `params.router_model` is only an override
(it is set anyway in seed 920).

| Purpose | Call | Notes |
|---|---|---|
| Health | `GET /health` | 200 while router is up (even with no model loaded) |
| List models + state | `GET /models` -> `{"data":[{"id","status":{"value": "unloaded"\|"loading"\|"loaded"\|"sleeping"\|"downloading"\|"incomplete"}}]}` | CONFIRMED. Only models declared in `runtime/llamacpp-models.ini` (or cached/`--models-dir`) are listed |
| Load | `POST /models/load` `{"model": id}` -> `{"success":true}` | CONFIRMED. **Async**: returns immediately, status goes `loading` -> `loaded` (LFM3 ~5 s warm, EmbeddingGemma BF16 ~25 s first time). Poll `GET /models` |
| Unload | `POST /models/unload` `{"model": id}` -> `{"success":true}` | CONFIRMED. Instant; container RAM 1.18 GiB -> 17 MiB |
| Download new model (no restart) | `POST /models` + `GET /models/sse` | documented, not exercised |
| Chat (+tools) | `POST /v1/chat/completions` `{model,messages,tools?}` | `tool_calls` returned in OpenAI format |
| Embeddings | `POST /v1/embeddings` `{"model": id, "input": ["..."]}` | CONFIRMED standard OpenAI payload; 768 dims, L2-normalised, no NaN |

- **Autoload**: a request to `/v1/*` for an unloaded model loads it (and evicts the LRU one beyond `--models-max`) - measured ~20 s for embedding -> LFM3 swap, request blocks meanwhile.
- A model must be declared in `runtime/llamacpp-models.ini` with `hf-repo = <repo>:<quant>`, otherwise loading fails ("failed to load model ''"). New models: add a section and restart `llm-llamacpp`, or `POST /models` to download.
- `reasoning = off` is set for LFM3 in the ini. LFM3 still sometimes emits `answer</think>answer`: strip everything up to and including `</think>` if present, and any `reasoning_content` field.
- First load of a model downloads from Hugging Face (LFM3 Q4_K_M 1.1 GB, EmbeddingGemma-2 BF16 0.56 GB) into volume `models-gguf` (`LLAMA_CACHE=/models/hf-cache`).
- Embedding instance uses `embeddings = true`, `no-mmproj = true`, ctx 2048.
- `--models-max 2` (was 1): every conversation turn now embeds the user message (deterministic RAG), and with 1 the
  router evicted LFM3 on each turn (reload + lost prompt cache). LFM3 (~1.2 GB) + EmbeddingGemma-2 (~0.6 GB) fit;
  the backend ModelManager still enforces `ram_budget_mb` / `strict_single_model`.
- LFM3 `threads = 10` (ini). Measured, 1085-token prompt: threads 4 -> 70 tok/s prompt, 6 (default) -> 78, 8 -> 89,
  10 -> 98; generation 11-13 tok/s regardless. Split `threads`/`threads-batch` was not better (noisy, worse).
- Prompt cache: the backend sends `cache_prompt: true`. LFM3 is a hybrid model, so llama.cpp reuses only up to a
  context checkpoint saved ~516 tokens (n_ubatch + 4) before the end of the previous prompt; `--cache-reuse` is
  rejected ("not supported by this context"). Smaller `ubatch-size` / `checkpoint-min-step` made reuse 0, keep
  defaults. Consequence: keep the per-turn tail (RAG context + question) short and the prefix (system + tools +
  history) stable.

## speaches (`http://voice:8000`)

| Purpose | Call | Notes |
|---|---|---|
| Health | `GET /health` | 200 |
| List loaded | `GET /api/ps` -> `{"models":["Systran/faster-whisper-small", ...]}` | CONFIRMED on 0.9.0-rc.3 (lists STT, TTS and `silero_vad_v5`). On 0.8.3 it listed whisper only |
| Load | `POST /api/ps/{model_id}` -> 201 `{"message":..}`; 409 if already loaded | CONFIRMED (whisper small ~11 s, piper ~1 s, kokoro ~22 s). Model must be downloaded first |
| Unload | `DELETE /api/ps/{model_id}` -> 200 `{"message":..}` | CONFIRMED on rc.3 (~0.2 s). **On 0.8.3 it DEADLOCKED** (hung forever, froze whisper) - that is why rc.3 is pinned |
| Download model | `POST /v1/models/{model_id}` -> 200 (blocking, 1-3 min) | `GET /v1/registry?task=text-to-speech` / `automatic-speech-recognition` lists valid ids |
| STT | `POST /v1/audio/transcriptions` multipart `file, model, language=es, response_format=json` -> `{"text": ...}` | CONFIRMED |
| TTS | `POST /v1/audio/speech` JSON `{model, input, voice, response_format}` -> audio bytes | CONFIRMED (`wav`) |
| Voices of a TTS model | `GET /v1/audio/models` (has `voices[]` per model) | |

- Idle TTL: env `STT_MODEL_TTL`, `TTS_MODEL_TTL`, `VAD_MODEL_TTL` (seconds, default 300; -1 never, 0 immediately). Set to 300 in compose.
- Refs: STT `Systran/faster-whisper-small` / `-base`; TTS Piper `speaches-ai/piper-es_ES-davefx-medium` voice `davefx` (22050 Hz); Kokoro `speaches-ai/Kokoro-82M-v1.0-ONNX` voice `ef_dora` (also `em_alex`, `em_santa`; 24000 Hz). Kokoro "voice" must be given explicitly.
- Persistence caveat: after unloading everything the Python process still holds ~0.8 GiB (faster-whisper does not return all memory).
- Models not on disk are NOT auto-downloaded on first use of `/api/ps` or the audio endpoints: download first via `POST /v1/models/{id}`.

## Ollama (`http://llm-ollama:11434`)

`GET /api/tags` -> `{"models":[]}` (verified, starts empty). Env: `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_KEEP_ALIVE=5m`.
Unload with `POST /api/generate {"model": m, "keep_alive": 0}`. The image has no `curl`; healthcheck is `ollama list`.

## Measured on this laptop (shared with other workloads, so treat as worst-case)

| Test | Result |
|---|---|
| LFM3-1.7B Q4_K_M chat, ES | 12-13 tok/s generation, ~65 tok/s prompt; 3-9 s per short answer; RAM 1.18 GiB loaded |
| LFM3 tool call `get_ticket_status` | proper `tool_calls` YES, args `{"ticket_number":"TRM-000001"}`, 24 tokens, 3.6 s; follow-up turn with tool result answered correctly |
| EmbeddingGemma-2 BF16 | 768 dims, no NaN, norm 1.0; 0.1-0.27 s for 2 short texts |
| Piper `davefx` | 1.0 s warm (2.4 s cold) for a sentence with IPs |
| Kokoro `ef_dora` | 6.7 s warm (16 s cold) |
| Whisper small int8 | 5-8 s for ~8 s of audio (round trip text identical to input, IPs preserved) |
| voice RAM | idle 240 MiB; all STT+Piper+Kokoro+VAD loaded 2.48 GiB |
