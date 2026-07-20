# Company Brain — Complete Project Context for Claude

## What we are building

Company Brain is a production-grade, multi-tenant, retrieval-augmented generation (RAG) platform. It ingests a company's internal documents (Google Drive) and Slack communications, lets employees ask questions via an AI agent, and automatically learns the company's recurring operational procedures — producing structured "skill documents" (playbooks) for human review. The agent can perform actions (send Slack messages, create calendar events, log to Google Sheets), with explicit user confirmation gating every action.

The system is fully built, deployed, and live via Cloudflare Tunnel, backed by Neon PostgreSQL and Ollama Cloud.

## What has been built (all 7 phases, complete)

### Phase 1 — Core RAG Pipeline
- Google Drive OAuth 2.0 authentication (installed app flow)
- File listing with pagination, text extraction from Google Docs (export as plain text), PDFs (pypdf), text/markdown
- Tiktoken-based chunking (500-token chunks, 50-token overlap)
- ChromaDB persistent vector store with deterministic chunk IDs
- Semantic retrieval + LLM prompt with labeled source citations
- FastAPI server with POST /ingest, POST /chat, GET / (chat UI)
- CLI tool (ingest, ask commands)

### Phase 2 — Slack Ingestion + Skill Synthesis
- Slack channel history ingestion with pagination
- Thread resolution (parent + replies)
- User ID to display name resolution with caching
- Conversation grouping: threads become one chunk, standalone messages within 15-minute windows grouped together
- Automated skill synthesis pipeline: batches chunks, asks LLM to extract operational procedures as structured JSON, creates markdown skill documents with YAML frontmatter
- Admin UI for reviewing, editing, approving, rejecting skills
- Approved skills embedded into separate ChromaDB collection, preferred over raw sources in answers
- Dual-collection retrieval (raw docs + playbooks)

### Phase 3 — Agent Actions with Confirmation
- Three tools: send_slack_message, create_calendar_event, append_sheet_row
- Multi-turn agent loop: LLM proposes action → user confirms/declines → result fed back → loop continues (max 5 iterations)
- Confirmation gating: every tool call requires explicit user approval
- Tool schemas in OpenAI function-calling format
- Frontend shows confirm/decline buttons for proposed actions
- System prompt enforces: one action at a time, retrieved content is informational only (never act on embedded instructions), must propose + wait for confirmation

### Phase 4 — Persistent Conversation Storage
- Replaced in-memory dict with PostgreSQL (Neon serverless Postgres)
- SQLAlchemy 2.0 async models: Conversation (id, user_id, created_at, updated_at, pending_action) and Message (id, conversation_id, role, content JSON, created_at)
- Content stored as JSON blocks matching LLM content format (text + tool_use blocks)
- pending_action stored as JSON column on Conversation row
- Alembic async migrations
- Transaction-per-request with rollback on error
- Fixed agent loop bug: assistant messages with tool_use blocks persisted BEFORE setting pending_action, ensuring conversation history remains valid for LLM API

### Phase 5 — Docker Infrastructure
- Multi-stage Dockerfile (python:3.11-slim builder + final image)
- Non-root user (appuser, UID 1000)
- System-wide pip install (not --user, since running as non-root)
- docker-compose.yml with backend service (Neon for DB, Ollama Cloud for LLM, volume mounts for ChromaDB + skills + Google credentials)
- Entrypoint script: validates env vars, runs Alembic migrations, starts uvicorn with graceful shutdown and dynamic $PORT
- .dockerignore excludes secrets, venvs, git, cache
- Railway and Render deployment configs

### Phase 6 — Multi-Tenant Data Isolation
- Every chunk in ChromaDB has user_id metadata field
- All queries filter by user_id using ChromaDB where clause
- Chunk IDs include user_id to prevent cross-user collisions: {user_id}_{doc_id}_{chunk_index}
- Skills stored per-user in data/skills/{user_id}/
- Conversation ownership enforced — users can only access their own conversations (filtered by user_id in SQL query)
- JWT authentication required on all endpoints

### Phase 7 — Production Hardening
- JWT authentication (HS256 via python-jose)
- Secret validation at startup (validate_config() fails fast)
- Rate limiting (slowapi, 30/min default, per-user)
- CORS configuration (from CORS_ORIGINS env var)
- Global exception handler (no stack trace leakage, returns generic 500)
- Retry with exponential backoff (tenacity, 3 attempts, 1-10s wait)
- LLM request timeouts (120s)
- Structured JSON logging (structlog)
- Health check endpoint at /health (DB ping)
- Prometheus metrics at /metrics
- 12 unit tests (chunking, config validation, auth 401s) — all passing
- GitHub Actions CI pipeline (lint + syntax + test on every push)
- Migrated from Anthropic Claude to Ollama Cloud (OpenAI-compatible API, glm-5.2 model)
- Replaced sentence-transformers/torch (2GB+) with ChromaDB built-in ONNX embedder (79MB, same all-MiniLM-L6-v2 model)
- Deployed live via Cloudflare Tunnel

## Tech stack — everything used

### Backend
- Python 3.11+ (runs on 3.14 locally)
- FastAPI 0.115.6 — HTTP API, web UI, JWT auth, rate limiting, CORS, Prometheus metrics
- uvicorn 0.34.0 — ASGI server with graceful shutdown

### LLM
- Ollama Cloud — OpenAI-compatible API at https://ollama.com/v1
- Model: glm-5.2 (via Ollama Cloud)
- OpenAI SDK (openai>=1.12.0) — used to call Ollama Cloud's /v1/chat/completions endpoint
- tenacity — retry with exponential backoff (3 attempts, 1-10s wait)

### Database
- Neon PostgreSQL — serverless Postgres for conversation/message persistence
- SQLAlchemy 2.0 async (sqlalchemy[asyncio]>=2.0.0) — ORM with async support
- asyncpg>=0.29.0 — async PostgreSQL driver
- Alembic>=1.13.0 — database migrations (async env.py using create_async_engine)

### Vector Store & Embeddings
- ChromaDB 1.5.x — persistent vector store (local filesystem, no external server)
- ChromaDB built-in ONNX all-MiniLM-L6-v2 — 79MB embedding model (no torch dependency)
- 384-dimensional embeddings
- Multi-tenant filtering via ChromaDB where clause (user_id)
- Two collections: company_docs (raw ingested), skill_docs (approved playbooks)

### Chunking
- tiktoken (cl100k_base encoding) — token counting and text splitting
- 500-token chunks with 50-token overlap

### Google APIs
- google-api-python-client 2.159.0 — Drive, Calendar, Sheets API client
- google-auth-oauthlib 1.2.1 — OAuth 2.0 installed app flow
- google_auth — token refresh and management
- Scopes: drive.readonly, calendar, spreadsheets
- Credentials loaded from file or GOOGLE_CREDENTIALS_JSON / GOOGLE_TOKEN_JSON env vars (for cloud deployment)

### Slack
- slack_sdk>=3.0.0 — WebClient for channel history, thread replies, message posting
- Scopes: channels:history, channels:read, groups:history, groups:read, chat:write, users:read, links:read

### Authentication & Security
- python-jose[cryptography]>=3.3.0 — JWT encode/decode (HS256)
- slowapi>=0.1.9 — per-user rate limiting
- structlog>=24.1.0 — structured JSON logging
- prometheus-fastapi-instrumentator>=7.0.0 — metrics at /metrics

### Document Processing
- pypdf 5.1.0 — PDF text extraction
- python-frontmatter>=1.1.0 — markdown with YAML frontmatter for skill docs
- python-dotenv 1.0.1 — .env file loading

### Frontend
- Vanilla HTML/CSS/JS (no framework) — single-page apps
- static/index.html — chat UI with JWT auth, confirm/decline buttons, conversation history
- static/skills.html — skills admin UI with JWT auth, list/edit/approve/reject

### Infrastructure
- Docker — multi-stage build, non-root user, python:3.11-slim base
- docker-compose.yml — backend service with volume mounts
- Cloudflare Tunnel — public URL exposure (currently live)
- Railway config (railway.json) — alternative deployment
- Render config (render.yaml) — alternative deployment
- GitHub Actions CI — .github/workflows/ci.yml (lint + syntax + test)

### Testing
- pytest + pytest-asyncio
- 12 unit tests across 3 files:
  - test_chunking.py (4 tests): token count, chunk basic, chunk empty, chunk documents
  - test_config.py (2 tests): validate_config missing keys, validate_config passes
  - test_auth.py (6 tests): 401 for missing/empty/no-Bearer tokens

## Project structure (every file)

```
company-brain/
  .env                          # live secrets (gitignored) — all API keys, DB URL, JWT secret
  .env.example                  # template with all env vars
  .dockerignore                 # excludes secrets, venvs, git, cache from Docker context
  .gitignore                    # excludes .venv, .env, data/, token.json, credentials.json
  Dockerfile                    # multi-stage: builder (gcc for C extensions) + final (non-root appuser)
  docker-compose.yml            # backend-only service (Neon for DB, Ollama Cloud for LLM, volume mounts)
  railway.json                  # Railway deployment config (Dockerfile builder, /health check)
  render.yaml                   # Render deployment config (Docker runtime, /health check, 1GB disk)
  requirements.txt              # 21 dependencies (no torch/sentence-transformers)
  pytest.ini                    # async test config
  alembic.ini                   # Alembic config (loggers, script_location)
  alembic/
    env.py                      # async migration env — uses create_async_engine with NullPool, asyncio.run()
    script.py.mako              # migration file template
    versions/
      0001_initial.py           # creates conversations + messages tables with user_id column
  .github/workflows/
    ci.yml                      # CI: install deps, ruff lint, syntax check, pytest
  scripts/
    entrypoint.sh               # Docker entrypoint: validate env, alembic upgrade, uvicorn with $PORT
    init_db.sh                  # one-time local setup: generate + apply initial migration
  app/
    __init__.py
    config.py                   # all env vars, validate_config(), JWT/CORS/rate-limit/LLM settings
    llm.py                      # shared OpenAI client for Ollama Cloud — chat_completion() function
    embeddings.py               # ChromaDB ONNX default embedding function (no torch)
    database.py                 # SQLAlchemy async engine (pool_size=10), Conversation + Message models, get_db
    google_auth.py              # OAuth 2.0 — reads from file or env var, token refresh, env-only mode for cloud
    drive_ingest.py             # Google Drive ingestion: list, export Docs, download PDFs/txt, extract text, user_id
    slack_ingest.py             # Slack ingestion: history pagination, threads, conversation grouping, user_id
    chunking.py                 # tiktoken chunking (500 tokens, 50 overlap), user_id propagation
    vectorstore.py              # ChromaDB: add_chunks (collision-safe IDs), query (where filter), query_both, delete_by_ids
    rag.py                      # single-turn RAG: retrieval + LLM prompt + answer with sources
    tools.py                    # 3 tools in OpenAI function format + describe_tool_call + execute_tool
    agent.py                    # multi-turn agent: tool calling, confirmation gating, DB persistence, retry, transaction-per-request
    skill_store.py              # per-user skill CRUD: markdown + frontmatter in data/skills/{user_id}/
    skill_synthesis.py          # LLM-based procedure extraction from chunks, creates skill docs per user
    cli.py                      # CLI: ingest, ingest-slack, synthesize, ask (all with --user-id)
    server.py                   # FastAPI: JWT auth, CORS, rate limit, health, metrics, exception handler, all endpoints
  static/
    index.html                  # chat UI with JWT auth (Authorization: Bearer), confirm/decline buttons
    skills.html                 # skills admin UI with JWT auth, list/edit/approve/reject
  tests/
    __init__.py
    test_chunking.py            # 4 tests
    test_config.py              # 2 tests
    test_auth.py                 # 6 tests
  data/
    chroma/                     # ChromaDB persistent storage (gitignored, volume-mounted)
    skills/                     # skill docs per user (gitignored, volume-mounted)
    .last_synthesis_at           # incremental synthesis timestamp
```

## API endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | / | No | Chat UI (index.html) |
| GET | /skills-page | No | Skills admin UI (skills.html) |
| GET | /health | No | Health check (DB ping) |
| GET | /metrics | No | Prometheus metrics |
| POST | /ingest | JWT | Ingest Google Drive folder |
| POST | /ingest/slack | JWT | Ingest Slack channels |
| POST | /synthesize | JWT | Run skill synthesis |
| GET | /skills | JWT | List skills for current user |
| GET | /skills/{id} | JWT | Get skill detail |
| PUT | /skills/{id} | JWT | Update skill content |
| POST | /skills/{id}/approve | JWT | Approve + embed skill |
| POST | /skills/{id}/reject | JWT | Reject + remove skill |
| POST | /chat | JWT | Send message to agent |
| POST | /chat/confirm | JWT | Confirm/decline proposed action |

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| DATABASE_URL | postgresql+asyncpg://...@db:5432/company_brain | Neon PostgreSQL connection |
| OLLAMA_BASE_URL | https://ollama.com/v1 | Ollama Cloud API endpoint |
| OLLAMA_API_KEY | (required) | Ollama Cloud API key |
| LLM_MODEL | glm-5.2 | Model name |
| LLM_TIMEOUT | 120 | Request timeout (seconds) |
| LLM_MAX_TOKENS | 2048 | Max tokens for chat/synthesis |
| JWT_SECRET | (required) | HS256 signing secret |
| JWT_ALGORITHM | HS256 | JWT algorithm |
| JWT_EXPIRE_HOURS | 24 | Token expiry |
| CORS_ORIGINS | * | Allowed origins |
| RATE_LIMIT | 30/minute | Per-user rate limit |
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

## Key design decisions

1. **Ollama Cloud instead of local Ollama** — no GPU needed, faster inference, no local model management
2. **ONNX embeddings instead of sentence-transformers** — 79MB vs 2GB+ (torch), same model, fits in 512MB containers
3. **Neon PostgreSQL instead of local Postgres** — serverless, free tier, no Docker DB container needed
4. **OpenAI SDK for Ollama** — Ollama Cloud exposes an OpenAI-compatible /v1 endpoint, so we use the openai Python package
5. **ChromaDB embedded (not server)** — no separate vector DB service, data on persistent volume
6. **Per-user data isolation via metadata** — ChromaDB where filter on user_id, not separate collections per user
7. **Transaction-per-request** — all DB writes in send_message/confirm_action commit once at the end, rollback on any error
8. **Confirmation gating** — agent proposes actions, user must confirm, result fed back to LLM — prevents autonomous tool execution
9. **Cloudflare Tunnel for demo** — exposes local server to public internet without Docker build/deploy overhead

## Current deployment

- **Live URL**: Cloudflare Tunnel (changes on restart, currently https://formation-mozilla-packet-sofa.trycloudflare.com)
- **Database**: Neon PostgreSQL (ep-snowy-mud-aoj0s425-pooler.c-2.ap-southeast-1.aws.neon.tech)
- **LLM**: Ollama Cloud (https://ollama.com/v1, model glm-5.2)
- **Vector store**: Local ChromaDB on Mac (persistent at ./data/chroma)
- **Server**: uvicorn on localhost:8000, tunneled via cloudflared

## What works end-to-end (verified)

1. Google Drive ingestion (OAuth → file listing → text extraction → chunking → embedding → ChromaDB)
2. RAG Q&A (retrieve chunks → build prompt → LLM → answer with source citations)
3. Tool calling (LLM proposes Slack message → confirmation_required → user confirms/declines → result fed back)
4. Conversation persistence (messages + tool_use blocks + tool_results stored in Neon, survives restart)
5. Multi-tenant isolation (user_id filtering on all queries, conversation ownership enforced)
6. JWT authentication (401 for missing/invalid tokens)
7. Rate limiting (30 requests/minute per user)
8. Health check (DB ping)
9. Metrics (Prometheus)
10. CI pipeline (lint + syntax + 12 tests on every push)
11. Alembic migrations (async, applied automatically on container startup)

## What's not yet done

- Real OAuth provider (Auth0/Cognito) — currently JWT-only with shared secret
- Background job queue for ingestion — currently synchronous (blocks HTTP request)
- WebSocket streaming for chat — currently request/response
- API versioning (/api/v1/)
- Gunicorn with multiple workers
- Scheduled automatic ingestion/synthesis (cron)
- Integration tests with mocked external services
- TypeScript frontend rewrite
- Slack bot needs channels:join scope to auto-join channels (currently must be manually invited)