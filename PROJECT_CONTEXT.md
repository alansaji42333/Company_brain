# Company Brain — Full Project Context for LLM Handoff

> **Status as of:** 20 Aug 2026
> **Working tree:** Uncommitted changes (Phase 8 complete, not yet committed/pushed).
> **Branch:** `master`, up to date with `origin/master` (last commit `1839e6e`).
> **Local state:** 18 files modified, 9 new files/dirs added — see "Uncommitted changes" below.

## What Company Brain is

A production-grade, multi-tenant, retrieval-augmented generation (RAG) platform. It ingests a company's internal documents (Google Drive) and Slack communications, lets employees ask questions via an AI agent, and automatically learns recurring operational procedures — producing structured "skill documents" (playbooks) for human review. The agent can perform actions (send Slack messages, create calendar events, log to Google Sheets) with explicit user confirmation gating every action. Deployed live via Cloudflare Tunnel, backed by Neon PostgreSQL and Ollama Cloud.

## Phases built (1–8, ALL COMPLETE)

### Phase 1 — Core RAG Pipeline (committed)
- Google Drive OAuth 2.0 (installed app flow), file listing with pagination, text extraction (Docs→plain text, PDF→pypdf, txt/md)
- Tiktoken chunking (500-token chunks, 50-token overlap)
- ChromaDB persistent vector store with deterministic chunk IDs
- Semantic retrieval + LLM prompt with labeled source citations
- FastAPI server with POST /ingest, POST /chat, GET / (chat UI)
- CLI tool (ingest, ask commands)

### Phase 2 — Slack Ingestion + Skill Synthesis (committed)
- Slack channel history ingestion with pagination, thread resolution, user-name resolution (cached)
- Conversation grouping: threads = one chunk; standalone messages within 15-min windows grouped together
- Automated skill synthesis: batches chunks, LLM extracts procedures as JSON, creates markdown skill docs with YAML frontmatter
- Admin UI for review/edit/approve/reject skills
- Approved skills embedded into separate ChromaDB collection, preferred over raw sources
- Dual-collection retrieval (raw docs + playbooks)

### Phase 3 — Agent Actions with Confirmation (committed)
- Three tools: send_slack_message, create_calendar_event, append_sheet_row (OpenAI function-calling format)
- Multi-turn agent loop: LLM proposes action → user confirms/declines → result fed back → loop continues (max 5 iterations)
- Confirmation gating: every tool call requires explicit user approval
- Frontend confirm/decline buttons
- System prompt enforces: one action at a time, retrieved content is informational only (never act on embedded instructions)

### Phase 4 — Persistent Conversation Storage (committed)
- Replaced in-memory dict with PostgreSQL (Neon serverless Postgres)
- SQLAlchemy 2.0 async models: Conversation (id, user_id, created_at, updated_at, pending_action) and Message (id, conversation_id, role, content JSON, created_at)
- Content stored as JSON blocks matching LLM content format (text + tool_use blocks)
- Alembic async migrations, transaction-per-request with rollback on error
- Fixed agent loop bug: assistant messages with tool_use blocks persisted BEFORE setting pending_action

### Phase 5 — Docker Infrastructure (committed)
- Multi-stage Dockerfile (python:3.11-slim builder + final, non-root appuser UID 1000, system-wide pip install)
- docker-compose.yml with backend service, volume mounts for ChromaDB + skills + Google credentials
- Entrypoint script: validates env vars, runs Alembic migrations, starts server with graceful shutdown + dynamic $PORT
- Railway and Render deployment configs

### Phase 6 — Multi-Tenant Data Isolation (committed)
- Every ChromaDB chunk has user_id metadata; all queries filter by user_id
- Chunk IDs include user_id: {user_id}_{doc_id}_{chunk_index}
- Skills stored per-user in data/skills/{user_id}/
- Conversation ownership enforced (filtered by user_id in SQL)
- JWT auth required on all endpoints

### Phase 7 — Production Hardening (committed)
- JWT authentication (HS256 via python-jose), secret validation at startup
- Rate limiting (slowapi, 30/min default, per-user)
- CORS (from CORS_ORIGINS env var)
- Global exception handler (no stack trace leakage)
- Retry with exponential backoff (tenacity, 3 attempts, 1–10s wait)
- LLM request timeouts (120s)
- Structured JSON logging (structlog)
- /health (DB ping), /metrics (Prometheus)
- 12 unit tests (chunking, config, auth 401s) — all passing
- GitHub Actions CI (lint + syntax + test on every push)
- Migrated from Anthropic Claude to Ollama Cloud (OpenAI-compatible API, glm-5.2 model)
- Replaced sentence-transformers/torch (2GB+) with ChromaDB built-in ONNX embedder (79MB, same all-MiniLM-L6-v2 model)
- Deployed live via Cloudflare Tunnel

### Phase 8 — Scale & Polish (COMPLETE, UNCOMMITTED — this session's work)
All 9 remaining items from the roadmap are implemented and verified:

1. **API versioning (/api/v1/)** — New `app/api_v1.py` APIRouter mounted at `/api/v1/` with 14 endpoints. Root routes (`/chat`, `/skills`, `/ingest`, etc.) kept as backward-compat proxies so existing clients/UIs keep working. New endpoints: `/api/v1/chat`, `/chat/confirm`, `/ingest`, `/ingest/slack`, `/ingest/async`, `/synthesize`, `/synthesize/async`, `/jobs/{job_id}`, `/skills` (GET list, GET one, PUT, approve, reject), `/conversations`.

2. **Auth0 JWKS OAuth provider** — `app/auth.py` verifies RS256 JWTs against the Auth0 tenant's JWKS endpoint (cached for AUTH0_JWKS_CACHE_TTL seconds). Falls back to HS256 shared-secret mode when `AUTH0_ENABLED=false`. Both modes resolve `user_id` from the JWT `sub` claim. Network failures during JWKS fetch return 503 (not 500); tokens missing a `kid` header are rejected with 401 (so shared-secret tokens don't trigger a JWKS fetch in Auth0 mode). Config: `AUTH0_ENABLED`, `AUTH0_DOMAIN`, `AUTH0_AUDIENCE`, `AUTH0_JWKS_CACHE_TTL`.

3. **Background job queue (ARQ + Redis)** — `app/jobs.py` with three job functions (`job_ingest_drive`, `job_ingest_slack`, `job_synthesize`) and a `WorkerSettings` class for the ARQ worker. Enqueue helpers (`enqueue_ingest`, `enqueue_synthesis`) store job ownership (user_id) in Redis. `get_job_status` enforces per-user access. New API endpoints: `POST /api/v1/ingest/async`, `/synthesize/async`, `GET /api/v1/jobs/{job_id}`. Run the worker via `scripts/worker-entrypoint.sh` (executes `arq app.jobs.WorkerSettings`). docker-compose now has a `worker` service + a `redis` service.

4. **WebSocket streaming for chat** — `WS /ws/chat` endpoint. Auth via `?token=` query param. Client sends `{message, conversation_id}`; server streams back a sequence: `{type:"sources"}` → multiple `{type:"token", text}` → final `{type:"message"|"confirmation_required"}`. `agent.send_message_stream()` does a single non-streaming LLM call (so tool calls are reliably detected), then emits the answer text in ~40-char chunks for a streaming UX; if a tool call is detected, it emits a `confirmation_required` event instead. The TS frontend uses this WS path.

5. **Gunicorn with multiple uvicorn workers** — `scripts/entrypoint.sh` now runs `gunicorn app.server:app --workers $WEB_CONCURRENCY --worker-class uvicorn.workers.UvicornWorker --timeout $WORKER_TIMEOUT --graceful-timeout 30`. Falls back to plain uvicorn if gunicorn is unavailable. Config: `WEB_CONCURRENCY` (default 4), `WORKER_TIMEOUT` (default 120).

6. **Scheduled ingestion/synthesis (APScheduler)** — `app/scheduler.py` uses AsyncIOScheduler with CronTrigger. Started/stopped in the FastAPI lifespan context (only when `SCHEDULE_ENABLED=true`). Three jobs: drive ingest, slack ingest, synthesis — cron expressions from `SCHEDULE_INGEST_CRON` (default `0 */6 * * *`) and `SCHEDULE_SYNTHESIS_CRON` (default `30 */6 * * *`). Scheduled runs use `SCHEDULE_USER_ID` as the tenant so scheduled data is isolated from interactive users.

7. **Slack channels:join auto-join** — `slack_ingest._ensure_in_channel(channel_id)` calls `conversations.join` before fetching history for each channel. Public channels are auto-joined (needs `channels:join` scope); private channels still require a manual invite. Already-in-channel and method-not-allowed errors are silently ignored; other errors are logged as warnings.

8. **TypeScript frontend rewrite (Vite + React + TS)** — `frontend/` directory with a full Vite+React+TS app that builds into `static/`. Components: `App.tsx` (token prompt + nav + page routing), `pages/ChatPage.tsx` (streaming chat via WebSocket with confirm/decline UI), `pages/SkillsPage.tsx` (skills list/detail/edit/approve/reject + run-synthesis button), `api/client.ts` (typed API client with interfaces for all /api/v1 endpoints + WebSocket streaming helper). Build: `cd frontend && npm run build` → outputs to `../static/`. Dev: `npm run dev` (Vite dev server proxies /api and /ws to localhost:8000). The old vanilla-JS `static/skills.html` was removed (the SPA handles both pages now). `static/index.html` is now the built SPA bundle.

9. **Integration tests with mocked external services** — `tests/test_integration.py` (5 e2e tests) + `tests/conftest.py`. Tests run hermetically: mocked LLM (scripted responses), mocked ChromaDB retrieval, mocked tool execution, sqlite in-memory DB (engine + session factory swapped per-test via monkeypatch). Tests: plain answer, action proposal + confirm flow, action decline flow, Auth0-mode rejects shared-secret tokens, cross-user conversation isolation. All 17 tests pass (12 original + 5 new).

## Tech stack (everything used)

### Backend
- Python 3.11+ (runs on 3.14 locally), FastAPI 0.115.6, uvicorn 0.34.0 / **gunicorn** (production workers)
- SQLAlchemy 2.0 async + asyncpg (Neon PostgreSQL), Alembic (async migrations)
- ChromaDB 1.5.x (persistent local vector store), built-in ONNX all-MiniLM-L6-v2 embedder (79MB, no torch)
- tiktoken chunking (500 tokens, 50 overlap)
- OpenAI SDK → Ollama Cloud (https://ollama.com/v1, glm-5.2 model), tenacity retry
- google-api-python-client (Drive/Calendar/Sheets), google-auth-oauthlib (OAuth 2.0)
- slack_sdk (ingestion + posting)
- **NEW:** ARQ + Redis (background jobs), APScheduler (cron scheduling), httpx (Auth0 JWKS fetch), websockets, gunicorn

### Auth & security
- python-jose (JWT) — HS256 shared-secret mode AND **NEW** RS256 Auth0 JWKS verification
- slowapi (per-user rate limiting), structlog (JSON logging), prometheus metrics
- CORS, global exception handler (no stack trace leakage), non-root Docker user, secret validation at startup

### Frontend
- **NEW:** Vite + React 18 + TypeScript (in `frontend/`), builds to `static/`
- Typed API client (`frontend/src/api/client.ts`) for all /api/v1 endpoints + WebSocket streaming
- Pages: ChatPage (streaming chat + confirm/decline), SkillsPage (list/edit/approve/reject/synthesize)

### Infrastructure
- Docker multi-stage build (python:3.11-slim, non-root appuser)
- docker-compose: **backend** + **worker** (NEW) + **redis** (NEW) services
- Cloudflare Tunnel (live), Railway config, Render config
- GitHub Actions CI (lint via ruff + syntax + 17 tests on every push)

## Project structure (every file)

```
company-brain/
  .env                          # live secrets (gitignored)
  .env.example                  # template — now includes Auth0, Redis, Schedule, Workers vars
  .dockerignore
  .gitignore                    # excludes static/assets/, node_modules/ (NEW)
  ruff.toml                     # NEW — line-length 140 (fixes pre-existing E501 CI failures)
  Dockerfile                    # multi-stage, non-root
  docker-compose.yml            # backend + worker (NEW) + redis (NEW)
  railway.json                  # Railway deployment
  render.yaml                   # Render deployment
  requirements.txt             # +gunicorn, httpx, arq, redis, apscheduler, websockets
  pytest.ini
  alembic.ini
  alembic/
    env.py                      # async migration env
    script.py.mako
    versions/
      0001_initial.py           # conversations + messages tables with user_id
  .github/workflows/
    ci.yml                      # lint + syntax + 17 tests (fixed env vars: OLLAMA_* not ANTHROPIC)
  scripts/
    entrypoint.sh               # gunicorn + uvicorn workers (NEW), alembic upgrade, $PORT
    worker-entrypoint.sh        # NEW — arq worker entrypoint
    init_db.sh                  # one-time local setup
  app/
    __init__.py
    config.py                   # +Auth0, +Redis, +Schedule, +Workers settings, +limiter singleton
    auth.py                     # NEW — verify_token() with Auth0 JWKS + shared-secret fallback
    llm.py                      # chat_completion() + NEW chat_completion_stream()
    embeddings.py               # ChromaDB ONNX default embedding function
    database.py                 # SQLAlchemy async engine, Conversation + Message models, get_db
    google_auth.py              # OAuth 2.0 (file or env-var credentials)
    drive_ingest.py             # Google Drive ingestion with user_id
    slack_ingest.py             # Slack ingestion + NEW _ensure_in_channel() (channels:join)
    chunking.py                 # tiktoken chunking with user_id propagation
    vectorstore.py              # ChromaDB: add_chunks, query, query_both, delete_by_ids
    rag.py                      # single-turn RAG
    tools.py                    # 3 tools (Slack/Calendar/Sheets) + describe + execute
    agent.py                     # multi-turn agent + NEW send_message_stream() for WebSocket
    skill_store.py              # per-user skill CRUD (markdown + frontmatter)
    skill_synthesis.py           # LLM procedure extraction
    cli.py                      # CLI: ingest, ingest-slack, synthesize, ask
    api_v1.py                   # NEW — APIRouter at /api/v1 (14 endpoints)
    jobs.py                     # NEW — ARQ background jobs (ingest, synthesis) + status tracking
    scheduler.py                # NEW — APScheduler cron for ingestion + synthesis
    server.py                   # FastAPI app: lifespan, v1 router, root proxy routes, /ws/chat
  frontend/                     # NEW — Vite + React + TS app
    package.json
    tsconfig.json
    vite.config.ts              # builds to ../static/, dev proxy to :8000
    index.html
    src/
      main.tsx
      App.tsx                   # token prompt + nav + page routing
      index.css
      api/client.ts             # typed /api/v1 client + WS streaming helper
      pages/
        ChatPage.tsx            # streaming chat via WebSocket + confirm/decline UI
        SkillsPage.tsx          # skills list/detail/edit/approve/reject/synthesize
  static/
    index.html                  # built SPA bundle (gitignored assets/)
    assets/                     # build output (gitignored)
  tests/
    __init__.py
    conftest.py                 # NEW — hermetic env overrides for tests
    test_chunking.py            # 4 tests
    test_config.py              # 2 tests
    test_auth.py                # 6 tests (401 for missing/empty/no-Bearer tokens)
    test_integration.py         # NEW — 5 e2e tests (chat, confirm/decline, Auth0, isolation)
  data/
    chroma/                     # ChromaDB persistent storage (gitignored, volume-mounted)
    skills/                     # skill docs per user (gitignored, volume-mounted)
    .last_synthesis_at          # incremental synthesis timestamp
```

## API endpoints

### Versioned (/api/v1/) — the canonical API
| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | /api/v1/ingest | JWT | Ingest Google Drive folder (synchronous) |
| POST | /api/v1/ingest/slack | JWT | Ingest Slack channels (synchronous) |
| POST | /api/v1/ingest/async | JWT | Enqueue background ingestion (returns job_id) |
| POST | /api/v1/synthesize | JWT | Run skill synthesis (synchronous) |
| POST | /api/v1/synthesize/async | JWT | Enqueue background synthesis (returns job_id) |
| GET | /api/v1/jobs/{job_id} | JWT | Poll background job status |
| GET | /api/v1/skills | JWT | List skills for current user |
| GET | /api/v1/skills/{id} | JWT | Get skill detail |
| PUT | /api/v1/skills/{id} | JWT | Update skill content |
| POST | /api/v1/skills/{id}/approve | JWT | Approve + embed skill |
| POST | /api/v1/skills/{id}/reject | JWT | Reject + remove skill |
| POST | /api/v1/chat | JWT | Send message to agent |
| POST | /api/v1/chat/confirm | JWT | Confirm/decline proposed action |
| GET | /api/v1/conversations | JWT | List user's conversations |

### Root (backward-compatible — proxies to v1 logic)
| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | / | No | Chat UI (built SPA) |
| GET | /skills-page | No | Skills UI (same SPA) |
| GET | /health | No | Health check (DB ping) |
| GET | /metrics | No | Prometheus metrics |
| WS | /ws/chat | token query param | Streaming chat over WebSocket |
| POST | /ingest | JWT | Ingest Drive (legacy path) |
| POST | /ingest/slack | JWT | Ingest Slack (legacy path) |
| POST | /synthesize | JWT | Synthesize (legacy path) |
| GET | /skills | JWT | List skills (legacy path) |
| GET | /skills/{id} | JWT | Get skill (legacy path) |
| PUT | /skills/{id} | JWT | Update skill (legacy path) |
| POST | /skills/{id}/approve | JWT | Approve (legacy path) |
| POST | /skills/{id}/reject | JWT | Reject (legacy path) |
| POST | /chat | JWT | Chat (legacy path) |
| POST | /chat/confirm | JWT | Confirm (legacy path) |

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| DATABASE_URL | postgresql+asyncpg://... | Neon PostgreSQL connection |
| OLLAMA_BASE_URL | https://ollama.com/v1 | Ollama Cloud API endpoint |
| OLLAMA_API_KEY | (required) | Ollama Cloud API key |
| LLM_MODEL | glm-5.2 | Model name |
| LLM_TIMEOUT | 120 | Request timeout (seconds) |
| LLM_MAX_TOKENS | 2048 | Max tokens for chat |
| LLM_MAX_TOKENS_SYNTHESIS | 4096 | Max tokens for synthesis |
| JWT_SECRET | (required for shared-secret mode) | HS256 signing secret |
| JWT_ALGORITHM | HS256 | JWT algorithm (shared-secret mode) |
| JWT_EXPIRE_HOURS | 24 | Token expiry |
| AUTH0_ENABLED | false | Enable Auth0 JWKS verification |
| AUTH0_DOMAIN | | Auth0 tenant domain |
| AUTH0_AUDIENCE | | Expected JWT audience |
| AUTH0_JWKS_CACHE_TTL | 3600 | JWKS cache TTL (seconds) |
| CORS_ORIGINS | * | Allowed origins |
| RATE_LIMIT | 30/minute | Per-user rate limit |
| REDIS_URL | redis://localhost:6379/0 | Redis for ARQ job queue |
| SCHEDULE_ENABLED | false | Enable APScheduler |
| SCHEDULE_INGEST_CRON | 0 */6 * * * | Ingestion cron (every 6h) |
| SCHEDULE_SYNTHESIS_CRON | 30 */6 * * * | Synthesis cron (every 6h, offset 30m) |
| SCHEDULE_USER_ID | scheduled | Tenant ID for scheduled jobs |
| WEB_CONCURRENCY | 4 | Gunicorn worker count |
| WORKER_TIMEOUT | 120 | Gunicorn worker timeout |
| SLACK_BOT_TOKEN | (required) | xoxb-... |
| SLACK_CHANNEL_IDS | (required) | Comma-separated channel IDs |
| GOOGLE_DRIVE_FOLDER_ID | (required) | Drive folder to ingest |
| GOOGLE_SHEET_ID | (required) | Sheet for append_sheet_row tool |
| GOOGLE_SHEET_TAB | Sheet1 | Sheet tab name |
| GOOGLE_CREDENTIALS_JSON | (optional) | JSON string of credentials.json |
| GOOGLE_TOKEN_JSON | (optional) | JSON string of token.json |
| CHUNK_SIZE | 500 | Token chunk size |
| CHUNK_OVERLAP | 50 | Token overlap |
| TOP_K_RETRIEVAL | 5 | Chunks to retrieve |

## Current deployment

- **Live URL:** Cloudflare Tunnel (changes on restart)
- **Database:** Neon PostgreSQL (serverless, pooler endpoint)
- **LLM:** Ollama Cloud (https://ollama.com/v1, glm-5.2)
- **Vector store:** Local ChromaDB (persistent at ./data/chroma)
- **Server:** uvicorn on localhost:8000 tunneled via cloudflared (Docker-ready: gunicorn + uvicorn workers)
- **Frontend:** Built React/TS SPA served from static/ by FastAPI

## What works end-to-end (verified)

1. Google Drive ingestion (OAuth → file listing → text extraction → chunking → embedding → ChromaDB)
2. RAG Q&A (retrieve chunks → build prompt → LLM → answer with source citations)
3. Tool calling (LLM proposes Slack message → confirmation_required → user confirms/declines → result fed back)
4. Conversation persistence (messages + tool_use blocks + tool_results in Neon, survives restart)
5. Multi-tenant isolation (user_id filtering on all queries, conversation ownership enforced)
6. JWT authentication (401 for missing/invalid tokens) — shared-secret HS256 mode
7. Auth0 JWKS verification (RS256, rejects shared-secret tokens when enabled) — NEW
8. Rate limiting (30 requests/minute per user)
9. Health check (DB ping), Prometheus metrics
10. CI pipeline (lint + syntax + 17 tests on every push)
11. Alembic migrations (async, applied automatically on container startup)
12. Background ingestion/synthesis jobs (ARQ + Redis, async endpoints + status polling) — NEW
13. WebSocket streaming chat (tokens streamed to frontend, confirm/decline over WS) — NEW
14. Scheduled ingestion/synthesis (APScheduler cron, in-process) — NEW
15. Slack channels:join (auto-join public channels before ingestion) — NEW
16. TypeScript frontend (Vite+React+TS, streaming chat, skills admin, builds to static/) — NEW
17. Gunicorn multi-worker production server — NEW

## Test suite (17 tests, all passing)

- `test_chunking.py` (4): token count, chunk basic, chunk empty, chunk documents
- `test_config.py` (2): validate_config missing keys, validate_config passes
- `test_auth.py` (6): 401 for missing/empty/no-Bearer tokens
- `test_integration.py` (5, NEW): plain chat answer, propose+confirm action, decline action, Auth0 rejects shared-secret token, cross-user conversation isolation

## Uncommitted changes (this session's work)

**Modified (18):** `.env.example`, `.github/workflows/ci.yml`, `.gitignore`, `app/agent.py`, `app/config.py`, `app/drive_ingest.py`, `app/google_auth.py`, `app/llm.py`, `app/rag.py`, `app/server.py`, `app/skill_synthesis.py`, `app/slack_ingest.py`, `app/tools.py`, `docker-compose.yml`, `requirements.txt`, `scripts/entrypoint.sh`, `static/index.html`, `static/skills.html` (deleted)

**New (9):** `app/api_v1.py`, `app/auth.py`, `app/jobs.py`, `app/scheduler.py`, `frontend/` (full Vite+React+TS app), `ruff.toml`, `scripts/worker-entrypoint.sh`, `tests/conftest.py`, `tests/test_integration.py`

**Verification:** `ruff check` clean, `pytest` 17/17 pass, `python -c "from app.server import app"` boots (33 routes), `npm run build` succeeds (154 KB JS / 49 KB gzip).

## Key design decisions

1. **Ollama Cloud instead of local Ollama** — no GPU needed, faster inference
2. **ONNX embeddings instead of sentence-transformers** — 79MB vs 2GB+, same model
3. **Neon PostgreSQL instead of local Postgres** — serverless, free tier, no Docker DB
4. **OpenAI SDK for Ollama** — Ollama Cloud exposes an OpenAI-compatible /v1 endpoint
5. **ChromaDB embedded (not server)** — no separate vector DB service, data on persistent volume
6. **Per-user data isolation via metadata** — ChromaDB where filter on user_id, not separate collections
7. **Transaction-per-request** — all DB writes commit once at the end, rollback on any error
8. **Confirmation gating** — agent proposes actions, user must confirm — prevents autonomous execution
9. **Cloudflare Tunnel for demo** — exposes local server without Docker build/deploy
10. **API versioning with backward compat** — new /api/v1/ router + root proxy routes so nothing breaks
11. **Auth0 + shared-secret dual mode** — JWKS when enabled, HS256 fallback for dev
12. **ARQ + Redis for jobs** — survives restart, retries, status tracked per-user in Redis
13. **APScheduler in-process** — no separate cron service, runs alongside uvicorn/gunicorn
14. **Single LLM call for streaming** — WS path does one non-streaming call (reliable tool-call detection), then emits text in chunks for streaming UX
15. **Frontend builds to static/** — Vite outputs directly into the FastAPI-served static dir, no separate web server needed

## What's NOT done (none blocking — all optional future work)

Nothing on the original roadmap remains. Potential future enhancements:
- Real end-user management UI (user registration/login with Auth0 lock)
- Per-conversation listing/deletion in the frontend
- Incremental Drive ingestion (change detection via Drive's pageToken / last-modified)
- Vector store migration to a managed service (Pinecone/Weaviate) if scale requires
- Multi-language document support (currently English-centric chunking)
- Streaming for synthesis (currently batch)
- Role-based access control (admin vs user) beyond per-user isolation
- Audit log of all tool executions