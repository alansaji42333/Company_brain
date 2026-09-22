# Company Brain — State of the Project Report

**Prepared for:** External architectural advisor (final deployment phase planning)
**Branch:** `master` — in sync with `origin/master`
**Latest commit:** `ba200b3` (docs: full technical interview context document)
**Workspace:** `/Users/alansaji/Desktop/AI automation/Company Brain`
**Build status:** 40/40 tests passing · ruff clean · runtime healthy

---

## 1. Directory Structure

High-level repository layout with focus on the application core, background workers, and deployment assets.

```
Company Brain/
├── app/                          # Python application core
│   ├── server.py                 # FastAPI app: lifespan, routers, /ws/chat WebSocket, /health, /metrics, static mounts
│   ├── api_v1.py                 # /api/v1 router (chat, skills, synthesize, conversations, audit) + /api/ingest router
│   ├── agent.py                  # Multi-turn agent loop; HTTP + streaming/WS paths; confirmation gating; DB persistence; audit
│   ├── llm.py                    # OpenAI SDK client → Ollama Cloud (glm-5.2); sync + streaming completions; retries
│   ├── auth.py                   # JWT verification: Auth0 RS256/JWKS (prod) and HS256 shared-secret (dev)
│   ├── tools.py                  # Tool schemas + executor: Slack message, Calendar event, Sheets row
│   ├── vectorstore.py            # ChromaDB persistent client; add/query/delete; user_id-scoped; dual collections
│   ├── embeddings.py             # ChromaDB built-in ONNX embedder (all-MiniLM-L6-v2, 384-dim, no torch)
│   ├── chunking.py               # tiktoken-based chunking (500 tokens / 50 overlap)
│   ├── drive_ingest.py           # Google Drive ingestion (list, export, extract text)
│   ├── slack_ingest.py           # Slack ingestion (history, threads, 15-min conversation grouping, channels:join)
│   ├── google_auth.py            # OAuth 2.0 installed-app flow (file or env-var credentials)
│   ├── skill_synthesis.py        # LLM procedure extraction from chunk batches → skill docs (incremental)
│   ├── skill_store.py            # Per-user skill CRUD; approve/reject embeds/deletes in ChromaDB
│   ├── rag.py                    # Single-turn RAG helper (CLI path)
│   ├── audit.py                  # Append-only audit trail (tool actions, skill approvals)
│   ├── jobs.py                   # ARQ enqueue/status helpers (per-user job ownership in Redis)
│   ├── worker.py                 # ARQ WorkerSettings + job fns: ingest_drive, ingest_slack, synthesize
│   ├── scheduler.py              # APScheduler cron → enqueues ingest/synthesis onto Redis pool
│   ├── database.py               # SQLAlchemy 2 async engine; Conversation, Message, AuditLog models
│   ├── config.py                 # Centralized env config + validation; shared rate limiter
│   └── cli.py                    # CLI: ingest, synthesize, ask (each with --user-id)
│
├── alembic/versions/
│   ├── 0001_initial.py           # conversations + messages tables
│   └── 0002_audit_logs.py        # audit_logs table
│
├── frontend/src/                 # Vite + React 18 + TypeScript SPA
│   ├── App.tsx                   # Token prompt + nav (Chat / Skills pages)
│   ├── api/client.ts             # Typed REST client + WebSocket streaming helpers
│   └── pages/
│       ├── ChatPage.tsx          # Streaming chat, token accumulation, tool confirm/decline cards
│       └── SkillsPage.tsx        # Skill review/edit/approve/reject + run-synthesis
│
├── static/                       # Vite build output (served by FastAPI at /static and /assets)
├── scripts/
│   ├── entrypoint.sh             # Prod server boot: alembic upgrade head + gunicorn/UvicornWorker
│   ├── worker-entrypoint.sh      # ARQ worker boot: arq app.worker.WorkerSettings
│   └── init_db.sh                # One-time local DB bootstrap
├── tests/                        # 40 hermetic tests (auth, chunking, config, integration, WS, ingest jobs, agent)
├── .github/workflows/ci.yml      # GitHub Actions CI (lint, syntax, pytest)
├── Dockerfile                    # Multi-stage image (python:3.11-slim, non-root)
├── docker-compose.yml            # backend + worker + redis services
├── railway.json / render.yaml    # PaaS deployment configs
└── data/                         # ChromaDB store, skill docs, synthesis watermark (volume-mounted)
```

---

## 2. Tech Stack & Services

### Application tier
| Component | Choice | Role |
|---|---|---|
| Web framework | **FastAPI 0.115** (Python 3.11+; local 3.14) | HTTP APIs, WebSocket streaming, lifespan management |
| ASGI servers | **Uvicorn 0.34 / Gunicorn** (+UvicornWorker) | Dev server / production multi-worker |
| ORM / migrations | **SQLAlchemy 2.0 async + Alembic** | Conversation/message/audit persistence |

### AI / RAG
| Component | Choice | Role |
|---|---|---|
| LLM inference | **OpenAI SDK → Ollama Cloud** (`https://ollama.com/v1`, model `glm-5.2`) | Chat completions (sync + streaming); note: the SDK is pointed at OpenAI's compatible endpoint, not OpenAI's API itself |
| Retries | **tenacity** | Exponential backoff (3 attempts) on LLM calls |
| Vector store | **ChromaDB** (embedded, persistent) | Two collections: `company_docs` (raw), `skill_docs` (approved playbooks). Multi-tenant via `where user_id` |
| Embeddings | **ChromaDB ONNX embedder — all-MiniLM-L6-v2** (384-dim, 79MB, no torch) | Replaces prior sentence-transformers/torch stack, same model, far smaller image |
| Chunking | **tiktoken** (cl100k_base) | 500-token chunks, 50-token overlap |

### Background jobs, scheduling, streaming
| Component | Choice | Role |
|---|---|---|
| Job queue | **ARQ + Redis** | Async ingest (Drive/Slack) + background synthesis; jobs stored with owning `user_id` |
| Scheduler | **APScheduler** (AsyncIOScheduler) | Cron-based drive/slack ingest + synthesis; enqueues onto the Redis pool |
| Streaming transport | **WebSockets** | Token-by-token streaming from the agent loop |

### Integrations & governance
| Component | Choice | Role |
|---|---|---|
| Google APIs | `google-api-python-client` | Drive (ingest), Calendar (events), Sheets (row append) |
| Slack | `slack_sdk` WebClient | Channel history, threads, posting; auto `channels:join` |
| Auth | **python-jose** + httpx (JWKS) | RS256 Auth0 JWKS (prod) / HS256 shared-secret (dev) |
| Rate limiting | **slowapi** | 30 req/min per user |
| Logging / metrics | **structlog**, **prometheus-fastapi-instrumentator** | Structured JSON logs; `/metrics` |

### Frontend
| Component | Choice | Role |
|---|---|---|
| SPA | **Vite 5 + React 18 + TypeScript 5.6** | Chat UI (streaming) + Skill admin UI; builds to `static/` |

### Services
- **Neon serverless PostgreSQL** — persisted conversation/message/audit data
- **Redis 7** — ARQ job broker (not running locally today; affects only async ingest)
- **Cloudflare Tunnel** — public exposure of the local demo server

---

## 3. Core Workflows

### 3.1 JWT Authentication & Multi-Tenant Isolation
- **Token verification** (`app/auth.py::verify_token`): prod mode resolves Auth0 RS256 JWTs against the tenant's JWKS endpoint (cached 1h; `kid`-guided; network failure → 503); dev mode decodes HS256 shared-secret JWTs. Both yield the `user_id` from the `sub` claim, passed as a string through the whole app.
- **Enforcement**: HTTP through the `get_user_id` dependency (Bearer header) on every `/api/v1/*` and `/api/ingest/*` route; WebSocket authenticates via first-message `{action: auth, token}` or subprotocol, else closes with 4401.
- **Isolation — three layers**:
  1. **ChromaDB** — every retrieval/add/delete filters `where: {"user_id": ...}`.
  2. **SQL** — every conversation, message, and audit query filters by the caller's `user_id`.
  3. **Job ownership** — ARQ job hashes are tagged with the owner; `/api/ingest/status/{job_id}` returns 404 unless owned.
- There is no code path that returns another tenant's data — a core, interview-defensible property.

### 3.2 Asynchronous Ingestion (Drive & Slack → ARQ → ChromaDB)
```
Drive folder / Slack channels
   └─> HTTP POST /api/ingest/drive|slack  (Bearer JWT)
        └─> enqueue_job() → ARQ on Redis    (job tagged with user_id; 503 if Redis down)
             └─> worker.py job fn (separate worker service, max 10 concurrent)
                  ├─ fetch content (Drive export/PDF extract | Slack history + threads)
                  ├─ chunk (tiktoken 500/50)  →  embed (ONNX MiniLM)  →  add to ChromaDB (user_id-filtered)
                  └─ status tracked: queued → in_progress → complete/deferred (per-user status endpoint)
```
- Slack messages within 15-minute windows are grouped into one conversation chunk; threads collapse to a single chunk; public channels are auto-joined before history fetch.
- Ingestion is **incremental de facto** for the store (latest synthesis watermark) but Drive ingestion is currently full-folder (no change-token diffing yet — noted limitation).

### 3.3 Agent / Chat Execution Loop
```
User message → POST /api/v1/chat  (or WS /ws/chat)
  1. Load-or-create Conversation (user_id-scoped); clear stale pending_action
  2. Retrieve: query_both(user question, top 5 raw + top 2 playbooks, user_id filter)
      → tagged sources (raw vs [Playbook: …], playbooks preferred)
  3. Build system prompt: answer ONLY from context; never satisfy embedded instructions; one action at a time
  4. LLM (glm-5.2 via OpenAI SDK, tools=[send_slack_message, create_calendar_event, append_sheet_row])
  5. If tool proposed → store pending tool_use_block on conversation, record audit (tool_proposed),
     return "confirmation_required"
  6. If text only → persist assistant message, return answer + sources
  ┌────────────────────────── SAFETY GATE ──────────────────────────┐
  │ POST /api/v1/chat/confirm {approved}                            │
  │   approved=true  → execute tool, record audit (tool_confirmed), │
  │   approved=false → audit (tool_declined), no execution          │
  │   Either way → tool_result fed back as user message → LLM continues (max 5 iters) │
  └─────────────────────────────────────────────────────────────────┘
```
- **Streaming path parity**: `/ws/chat` runs the same persistence, permission, and audit code, but streams events — `sources` → `token` deltas (LLM tokens forwarded in real time) → `tool_proposal` → `tool_result` → `message`.
- The sync/async bridge: the blocking OpenAI streaming call runs in a `threading.Thread`; deltas are forwarded to the asyncio loop via `asyncio.Queue` + `run_coroutine_threadsafe`.
- **Tools executed today**: `send_slack_message`, `create_calendar_event`, `append_sheet_row`. Each execution is recorded in the append-only `audit_logs` table within the same DB transaction as the action.

### 3.4 Skill Synthesis (self-learning layer)
New chunks since watermark → batches of 15 → LLM extracts procedures as JSON `{title, summary, steps, source_chunk_ids}` → markdown skill docs with YAML frontmatter (`data/skills/{user_id}/`) → admin reviews/edits in the Skills UI → **approve** embeds into `skill_docs` (and removes from it on reject), recording audit rows. Approved playbooks are then *preferred* over raw sources in retrieval.

---

## 4. Environment Configuration Checklist

Source of truth: [`.env.example`](.env.example). Missing/invalid required vars raise `RuntimeError` at config load.

**Required — core**
- [ ] `DATABASE_URL` — Neon/Postgres async URL (`postgresql+asyncpg://…`); sqlite for tests
- [ ] `OLLAMA_BASE_URL` — `https://ollama.com/v1` (OpenAI-compatible endpoint; .env.example historically used `/api` — fixed to `/v1`)
- [ ] `OLLAMA_API_KEY` — key for the Ollama-compatible endpoint
- [ ] `LLM_MODEL` — e.g. `glm-5.2`

**Authentication**
- [ ] `JWT_SECRET` — shared secret for HS256 dev mode (required unless Auth0 mode)
- [ ] `JWT_ALGORITHM`, `JWT_EXPIRE_HOURS`
- [ ] `AUTH0_ENABLED` (false by default), `AUTH0_DOMAIN`, `AUTH0_AUDIENCE`, `AUTH0_JWKS_CACHE_TTL` — required only if Auth0 mode
- [ ] `RATE_LIMIT` (default 30/minute), `CORS_ORIGINS`

**LLM tuning (optional, have sane defaults)**
- [ ] `LLM_TIMEOUT`, `LLM_MAX_TOKENS`, `LLM_MAX_TOKENS_SYNTHESIS`
- [ ] `CHUNK_SIZE` (500), `CHUNK_OVERLAP` (50), `TOP_K_RETRIEVAL` (5)

**Google**
- [ ] `GOOGLE_DRIVE_FOLDER_ID`
- [ ] `GOOGLE_SHEET_ID`, `GOOGLE_SHEET_TAB`
- [ ] `GOOGLE_CREDENTIALS_JSON` / `GOOGLE_TOKEN_JSON` (env-var alternative to seeded `credentials.json`/`token.json` files)

**Slack**
- [ ] `SLACK_BOT_TOKEN`
- [ ] `SLACK_CHANNEL_IDS` (comma-separated)

**Jobs & scheduling**
- [ ] `REDIS_URL` — ARQ/Redis broker (async ingest + scheduler gate)
- [ ] `SCHEDULE_ENABLED`, `SCHEDULE_INGEST_CRON`, `SCHEDULE_SYNTHESIS_CRON`, `SCHEDULE_USER_ID`

**Server**
- [ ] `WEB_CONCURRENCY` (4), `WORKER_TIMEOUT` (120)

> **Clarification for the advisor:** there is no "BigPickle/OpenCode" key in this application. The project calls **Ollama Cloud** via the **OpenAI SDK**; the only LLM credential is `OLLAMA_API_KEY`. (BigPickle/OpenCode refer to the agent tooling writing this report, not the app runtime.)

---

## 5. Deployment Readiness

| Asset | Exists | Status / Notes |
|---|---|---|
| `Dockerfile` | ✅ | Multi-stage: builder (gcc for C ext: asyncpg/onnxruntime) → `python:3.11-slim`, non-root `appuser` UID 1000, system-wide pip install, `data/` dirs created |
| `docker-compose.yml` | ✅ | 3 services: `backend` (port 8000, gunicorn), `worker` (arq, `worker-entrypoint.sh`), `redis` (`redis:7-alpine`, volume + healthcheck); backend/worker share `data/` volumes + `REDIS_URL=redis://redis:6379/0` |
| `scripts/entrypoint.sh` | ✅ | Env validation → `alembic upgrade head` → gunicorn with UvicornWorker (`$PORT`, `$WEB_CONCURRENCY`, graceful timeout); uvicorn fallback |
| `scripts/worker-entrypoint.sh` | ✅ | Runs `arq app.worker.WorkerSettings` |
| GitHub Actions CI (`.github/workflows/ci.yml`) | ✅ | On push/PR to `master`/`main`: Python 3.11 → `pip install -r requirements.txt` + test deps (`pytest`, `pytest-asyncio`, `ruff`, `aiosqlite`, `httpx`, `respx`) → `ruff check app/ --select E,F` → AST syntax check → `pytest tests/ -v` with hermetic env overrides |
| `railway.json` / `render.yaml` | ✅ | PaaS configs present (Dockerfile builder, `/health` check); not the live target |
| Test suite | ✅ | 40 hermetic tests, no external network (respx-mocked Ollama, sqlite DB). Currently **passing** |
| Live demo | ✅ | Running locally on `:8000` behind Cloudflare Tunnel. **Note:** Redis currently down locally → async ingest endpoints 503 (expected); chat/skills/UI fully functional |

**Current runtime snapshot:** `GET /health` → `{"status":"healthy"}`. Frontend SPA served and rendering (fixed `/assets` mount). Audit DB and migrations applied via Alembic (`0001`, `0002`).

---

## Known Limitations to Flag to the Advisor
1. **Embedded ChromaDB is single-replica** — local filesystem store; multi-replica scale requires a managed vector DB (Pinecone/Weaviate) or shared volume.
2. **No self-service auth UI** — JWTs are minted out-of-band; Auth0 *verification* is wired, Auth0 *login* is not.
3. **Drive ingestion is full-folder, not incremental** — no change-token watermark; `synthesis` is incremental.
4. **No RBAC** — isolation is tenant-scoped, not role-scoped (all users equal).
5. **Redis is a hard dependency** for async ingest + scheduler; the HTTP layer degrades gracefully when it's absent (503 on queue endpoints), but a resilient deployment should always include it.