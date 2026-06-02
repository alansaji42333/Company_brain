# Company Brain — Full Project Context (for LLM handoff)

## What we are building

Company Brain is a production-grade, multi-tenant, retrieval-augmented generation (RAG) platform that ingests a company's internal documents and Slack communications, lets employees ask questions via an AI agent, and automatically learns the company's recurring operational procedures — producing structured "skill documents" (playbooks) for human review. The agent can perform actions (send Slack messages, create calendar events, log to Google Sheets), with explicit user confirmation gating every action.

The system is deployed and accessible via a public URL using Cloudflare Tunnel, backed by a Neon PostgreSQL database and Ollama Cloud for LLM inference.

## Current architecture

- **LLM**: Ollama Cloud (OpenAI-compatible API at `https://ollama.com/v1`) using `glm-5.2` model
- **Database**: Neon PostgreSQL (serverless Postgres) for conversation history
- **Vector store**: ChromaDB 1.5.x (persistent, local filesystem, per-user data isolation)
- **Embeddings**: ChromaDB built-in ONNX `all-MiniLM-L6-v2` (79MB, no torch dependency)
- **Auth**: JWT-based authentication with per-user data isolation
- **Deployment**: Cloudflare Tunnel exposing local uvicorn server (Docker-ready via Dockerfile + docker-compose)
- **Frontend**: Vanilla JS single-page apps for chat and skills admin

## Tech stack

- **Python 3.11+** (runs on 3.14 locally)
- **FastAPI + uvicorn** — HTTP API, web UI, JWT auth, rate limiting, CORS, Prometheus metrics
- **ChromaDB 1.5.x** — persistent vector store with multi-tenant `user_id` filtering
- **ONNX all-MiniLM-L6-v2** — lightweight embeddings (no torch/sentence-transformers)
- **Ollama Cloud (glm-5.2)** — LLM for chat, synthesis, and agent reasoning (via OpenAI SDK)
- **Neon PostgreSQL** — serverless Postgres for conversation/message persistence
- **SQLAlchemy 2.0 async + asyncpg** — ORM and async DB driver
- **Alembic** — database migrations (async env.py)
- **Google APIs** — Drive (ingestion), Calendar (event creation), Sheets (row append)
- **Slack SDK** — channel history ingestion and message posting
- **JWT (python-jose)** — authentication with HS256 signing
- **slowapi** — per-user rate limiting
- **structlog** — structured JSON logging
- **prometheus-fastapi-instrumentator** — metrics at `/metrics`
- **tenacity** — retry with exponential backoff for LLM calls
- **tiktoken** — token counting for chunking
- **Docker** — multi-stage build, non-root user, entrypoint with migrations

## Project structure

```
company-brain/
  .env                          # live secrets (gitignored)
  .env.example                  # template
  .dockerignore
  .gitignore
  Dockerfile                    # multi-stage, non-root, system-wide pip install
  docker-compose.yml            # backend-only (Neon for DB, Ollama Cloud for LLM)
  railway.json                  # Railway deployment config
  render.yaml                   # Render deployment config
  requirements.txt              # all dependencies
  pytest.ini                    # test config
  alembic.ini                   # Alembic config
  alembic/
    env.py                      # async Alembic env (uses create_async_engine)
    script.py.mako              # migration template
    versions/
      0001_initial.py           # creates conversations + messages tables with user_id
  .github/workflows/
    ci.yml                      # lint + syntax + tests on push/PR
  scripts/
    entrypoint.sh               # validates env, runs alembic, starts uvicorn (dynamic $PORT)
    init_db.sh                  # one-time: generate + apply initial migration
  app/
    __init__.py
    config.py                   # env loading, validate_config(), JWT/CORS/rate-limit settings
    llm.py                      # shared OpenAI client for Ollama Cloud (chat_completion)
    embeddings.py               # ChromaDB ONNX default embedding function (no torch)
    database.py                 # SQLAlchemy async engine, Conversation + Message models, get_db
    google_auth.py              # OAuth 2.0 — reads from file or GOOGLE_CREDENTIALS_JSON env var
    drive_ingest.py             # Google Drive ingestion (PDF, Docs, txt, md) with user_id
    slack_ingest.py             # Slack ingestion (history, threads, conversation grouping) with user_id
    chunking.py                 # tiktoken chunking (500 tokens, 50 overlap) with user_id propagation
    vectorstore.py              # ChromaDB: add_chunks, query, query_both, user_id where-filter, collision-safe IDs
    rag.py                      # single-turn RAG (retrieval + LLM prompt + answer)
    tools.py                    # 3 tools in OpenAI function-calling format (Slack, Calendar, Sheets)
    agent.py                    # multi-turn agent loop with tool calling, confirmation gating, DB persistence
    skill_store.py              # per-user skill CRUD (markdown + frontmatter in data/skills/{user_id}/)
    skill_synthesis.py          # LLM-based procedure extraction from chunks, creates skill docs
    cli.py                      # CLI: ingest, ingest-slack, synthesize, ask (all with --user-id)
    server.py                   # FastAPI: JWT auth, CORS, rate limit, health, metrics, all endpoints
  static/
    index.html                  # chat UI with JWT auth, confirm/decline buttons
    skills.html                 # skills admin UI with JWT auth
  tests/
    test_chunking.py            # 4 tests: token count, chunk basic, chunk empty, chunk documents
    test_config.py              # 2 tests: validate_config missing keys, validate_config passes
    test_auth.py                # 6 tests: 401 for missing/empty/no-Bearer tokens
  data/
    chroma/                     # ChromaDB persistent storage (gitignored, volume-mounted)
    skills/                     # skill docs per user (gitignored, volume-mounted)
    .last_synthesis_at          # incremental synthesis timestamp
```

## Phase 1 — Core RAG Pipeline (complete)

Built the ingestion-to-answer pipeline: Google Drive OAuth → file listing → text extraction (PDF, Docs, txt, md) → tiktoken chunking (500 tokens, 50 overlap) → embedding → ChromaDB storage → semantic retrieval → LLM prompt with labeled sources → answer with citations.

## Phase 2 — Slack Ingestion + Skill Synthesis (complete)

Added Slack channel ingestion (history pagination, thread resolution, conversation grouping with 15-min windows, user name resolution) and an automated skill synthesis pipeline that batches chunks, asks the LLM to extract operational procedures as structured JSON, and creates markdown skill documents with YAML frontmatter. Admin UI for reviewing, editing, approving, and rejecting skills. Approved skills are embedded into a separate ChromaDB collection and preferred over raw sources in answers.

## Phase 3 — Agent Actions with Confirmation (complete)

Built three tools (send Slack message, create Google Calendar event, append Google Sheets row) and a multi-turn agent loop where every tool call requires explicit user confirmation. The agent proposes an action, the user confirms or declines, the result is fed back to the LLM, and the loop continues (max 5 iterations). The frontend shows confirm/decline buttons for proposed actions.

## Phase 4 — Persistent Conversation Storage (complete)

Replaced in-memory conversation dict with PostgreSQL-backed storage:
- SQLAlchemy 2.0 async models: `Conversation` (id, user_id, created_at, updated_at, pending_action) and `Message` (id, conversation_id, role, content JSON, created_at)
- Content stored as JSON blocks matching the LLM's content format (text + tool_use blocks)
- `pending_action` stored as JSON column on Conversation
- Alembic async migrations
- Transaction-per-request with rollback on error
- Fixed agent loop bug: assistant messages with tool_use blocks are now persisted BEFORE setting pending_action, ensuring conversation history remains valid for the LLM API when the subsequent tool_result is appended

## Phase 5 — Docker Infrastructure (complete)

Multi-stage Dockerfile (python:3.11-slim builder + final image, non-root `appuser`, system-wide pip install). docker-compose.yml with backend service (Neon for DB, Ollama Cloud for LLM, volume mounts for ChromaDB + skills + Google credentials). Entrypoint script validates required env vars, runs Alembic migrations, starts uvicorn with graceful shutdown. `.dockerignore` excludes secrets, venvs, git, cache.

## Phase 6 — Multi-Tenant Data Isolation (complete)

Every chunk in ChromaDB has a `user_id` metadata field. All queries filter by `user_id` using ChromaDB's `where` clause. Chunk IDs include `user_id` to prevent cross-user collisions. Skills are stored per-user in `data/skills/{user_id}/`. Conversation ownership is enforced — users can only access their own conversations. The `X-User-Id` header (now `Authorization: Bearer <JWT>`) is required on all endpoints.

## Phase 7 — Production Hardening (complete)

- **Security**: JWT authentication (HS256), secret validation at startup, rate limiting (slowapi, 30/min default), CORS configuration, non-root Docker user
- **Reliability**: Retry with exponential backoff (tenacity, 3 attempts), LLM request timeouts (120s), global exception handler (no stack trace leakage), transaction-per-request with rollback
- **Observability**: Structured JSON logging (structlog), `/health` endpoint (DB ping), Prometheus metrics at `/metrics`
- **Testing**: 12 unit tests (chunking, config validation, auth 401s), GitHub Actions CI pipeline (lint + syntax + test)
- **LLM migration**: Switched from Anthropic Claude to Ollama Cloud (OpenAI-compatible API, `glm-5.2` model)
- **Embedding optimization**: Replaced sentence-transformers/torch (2GB+) with ChromaDB's built-in ONNX embedder (79MB, same model)
- **Deployment configs**: Railway (`railway.json`), Render (`render.yaml`), Cloudflare Tunnel (currently active)

## Current state (Phase 7 — deployed and live)

The app is running and accessible via Cloudflare Tunnel at a public URL. Verified working:
- Health check returns `{"status": "healthy"}`
- RAG Q&A retrieves relevant chunks and answers with source citations
- Tool calling proposes actions (Slack messages) and returns confirmation_required
- Confirm/decline flow works end-to-end
- Google Drive ingestion works (OAuth, PDF extraction, chunking, embedding)
- Neon PostgreSQL stores conversations and messages
- JWT authentication enforces user identity
- 12/12 tests pass
- CI pipeline runs on every push

## What needs real credentials to test end-to-end

- `OLLAMA_API_KEY` — for LLM calls (configured, working)
- `credentials.json` or `GOOGLE_CREDENTIALS_JSON` env — for Google Drive/Calendar/Sheets (configured, working)
- `token.json` or `GOOGLE_TOKEN_JSON` env — for Google OAuth token (configured, working)
- `SLACK_BOT_TOKEN` — for Slack ingestion/messaging (configured, bot needs `channels:join` scope to auto-join channels)
- `DATABASE_URL` — Neon PostgreSQL (configured, working)

## Next phase ideas

- Real OAuth provider integration (Auth0/Cognito instead of JWT-only)
- Background job queue for ingestion (Celery/RQ — currently synchronous)
- WebSocket streaming for chat responses
- API versioning (`/api/v1/`)
- Gunicorn with multiple workers for production
- Scheduled automatic ingestion and synthesis (cron)
- Integration tests with mocked external services
- TypeScript frontend rewrite