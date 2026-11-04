# TRAMA — Backend + DB

Local voice agent for network diagnosis (academic project, UniPutumayo). This repo holds the FastAPI backend, the PostgreSQL + pgvector schema, and the model runtimes (llama.cpp, Ollama, speaches). The UI lives in the separate `trama-frontend` repo.

## Run

```bash
cp .env.example .env            # optional: compose has defaults
docker compose --profile core up -d --build
```

| Service | Host port | Notes |
|---|---|---|
| backend | 8000 | FastAPI, `GET /health`, API under `/api` |
| db | 5433 | PostgreSQL 17 + pgvector (`pgvector/pgvector:0.8.0-pg17`) |

The stack creates the Docker network **`trama-net`**. The backend joins it with the alias `backend`, which is how the frontend's nginx reaches it. Start this stack before the frontend.

## Profiles

| Profile | Services | Notes |
|---|---|---|
| `core` | `db`, `migrate` (one-shot), `backend` | always needed |
| `llm` | `llm-llamacpp` (router mode), `llm-ollama` | start EMPTY; no host ports |
| `voice` | `voice` (speaches STT + TTS) | starts EMPTY; no host port |

Running everything at once crashes a 12 GB WSL2 laptop. The backend's model manager loads/unloads models on demand
(RAM budget in `app_settings.ram_budget_mb`). Start the runtimes you need; they hold nothing until asked:

```bash
docker compose --profile core --profile llm --profile voice up -d
# or one at a time while testing:
docker compose --profile llm up -d llm-llamacpp && docker compose stop llm-llamacpp
```

Runtime URLs, verified endpoints and measured latency/RAM: [`docs/runtime-endpoints.md`](docs/runtime-endpoints.md).
First use of each model downloads it (LFM3 1.1 GB, EmbeddingGemma-2 0.56 GB, whisper small ~0.5 GB, Piper/Kokoro ~0.1/0.4 GB)
into the volumes `models-gguf`, `models-voice`, `models-ollama`. llama.cpp models are declared in `runtime/llamacpp-models.ini`.

## Migrations

`migrate` is a one-shot service (`database/migrate.sh`, postgres image). On every `up` it applies, in order,
`database/migrations/*.sql` then `database/seeds/*.sql` not yet recorded in `schema_migrations`, each file in one
transaction (`ON_ERROR_STOP`); `backend` waits for it to complete successfully. A DB created before tracking existed
(has `model_providers`, empty `schema_migrations`) is baselined automatically for 001-005 + 900/910.
Add new files with a higher number; seeds added after 910 must be idempotent (`ON CONFLICT` / `UPDATE`).
Re-run manually: `docker compose --profile core up migrate`.

## Reset the DB

```bash
docker compose --profile core down -v
```

`migrate` rebuilds everything on the empty volume.

## Database layout

- `database/migrations/NNN_*.sql`: schema (extensions, config, operational, knowledge, conversation/audit).
- `database/seeds/9NN_*.sql`: config seed (providers, model profiles, app settings), synthetic lab data, and `920_fix_model_refs.sql` (verified model refs + runtime params).
  `930_rag_settings.sql` adds `rag_mode` (`auto` default: backend retrieves every turn and injects cited context; `tool`; `both`), `rag_min_score` (0.7) and `max_tokens: 400` for LFM3 (only if absent).
- `database/migrate.sh`: the migration runner (the only path; initdb is no longer used).

## Local development

```bash
uv sync
uv run pytest        # endpoint tests (in-memory fakes, no DB needed)
uv run ruff check
```

## Rules

- Model names and secrets are configured from the UI and stored in the DB, never in `.env`.
- Provider secrets are Fernet-encrypted; the master key is generated on first start in the `trama-secrets` volume.
