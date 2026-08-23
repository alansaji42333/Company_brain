# COMPANY BRAIN — FULL TECHNICAL CONTEXT FOR INTERVIEW PREP

> This document is a complete, accurate technical description of the Company
> Brain codebase as it exists on the master branch (commit 573f1dc). It is
> written for an AI to use when preparing interview answers. Every claim below
> is grounded in the actual source code. Code snippets are real, not invented.

===============================================================================
SECTION 0: ONE-PARAGRAPH ELEVATOR PITCH
===============================================================================

Company Brain is a production-grade, multi-tenant, retrieval-augmented
generation (RAG) platform. It ingests a company's internal documents (Google
Drive) and Slack communications, lets employees ask questions via an AI agent
that can take real actions (send Slack messages, create calendar events, log
to Google Sheets) with explicit user confirmation gating every action, and
automatically learns the company's recurring operational procedures by
synthesizing structured "skill documents" (playbooks) for human review. It is
deployed live, backed by Neon serverless PostgreSQL, Ollama Cloud for LLM
inference, ChromaDB for vector search, Redis+ARQ for background jobs, and a
React/TypeScript frontend. It has an enterprise audit trail logging every
sensitive action.

===============================================================================
SECTION 1: WHAT THE PROJECT DOES
===============================================================================

PROBLEM
-------
Companies accumulate institutional knowledge in two places: documents (Google
Drive) and conversations (Slack). This knowledge is scattered, unsearchable, and
walks out the door when employees leave. New hires spend weeks asking the same
questions; experienced staff answer the same operational questions repeatedly.

WHAT IT BUILDS
-------------
A platform that:

1. INGESTS knowledge automatically:
   - Google Drive folder → extracts text from Google Docs, PDFs, txt, markdown
   - Slack channels → ingests history with thread resolution and conversation
     grouping (messages within 15-minute windows grouped into one chunk,
     threads become one chunk)
   - Everything is chunked (500 tokens, 50 overlap), embedded, and stored in
     ChromaDB with per-user isolation

2. ANSWERS questions via an AI AGENT:
   - Employee asks a question in the chat UI
   - System retrieves the top-5 relevant chunks from raw docs + top-2 from
     approved "playbooks" (playbooks preferred as authoritative)
   - LLM (glm-5.2 via Ollama Cloud) answers with source citations
   - If the question implies an action, the agent PROPOSES a tool call
     (e.g. "send a Slack message") and PAUSES — the user must explicitly
     confirm or decline before anything executes (the "safety gate")

3. LEARNS procedures automatically (SKILL SYNTHESIS):
   - Batches newly-ingested chunks (15 per batch)
   - Asks the LLM to extract recurring operational procedures as structured JSON
     (title, summary, steps, source chunk IDs)
   - Creates markdown "skill documents" with YAML frontmatter
   - Admin reviews/edits/approves/rejects them in a dedicated UI
   - Approved skills are embedded into a separate ChromaDB collection and
     PREFERRED over raw sources in future answers

4. EXECUTES ACTIONS with confirmation gating:
   - Three tools: send_slack_message, create_calendar_event, append_sheet_row
   - Multi-turn agent loop: LLM proposes → user confirms/declines → result fed
     back → LLM continues (max 5 iterations)
   - Every tool call is recorded in an immutable audit log

5. PROVIDES enterprise guarantees:
   - Multi-tenant data isolation (every chunk, conversation, and skill is
     scoped to a user_id; queries filter by user_id)
   - JWT authentication (shared-secret HS256 dev mode OR Auth0 RS256/JWKS prod)
   - Per-user rate limiting (30/min)
   - Audit trail of every sensitive action
   - Background job queue (ARQ + Redis) so ingestion/synthesis don't block HTTP
   - Scheduled ingestion/synthesis via APScheduler cron
   - WebSocket streaming for real-time token-by-token chat

WHO IT'S FOR
------------
Internal teams at small-to-mid companies that live in Google Drive + Slack and
want a private ChatGPT-like assistant grounded in their own knowledge, with the
ability to take real actions on their behalf.

===============================================================================
SECTION 2: COMPLETE TECH STACK
===============================================================================

BACKEND (Python 3.11+, runs on 3.14 locally)
--------------------------------------------
- FastAPI 0.115.6 — HTTP API, WebSocket, JWT auth, rate limiting, CORS, metrics
- uvicorn 0.34.0 / gunicorn>=21.2.0 — ASGI server; gunicorn with UvicornWorker
  for production multi-worker (WEB_CONCURRENCY workers, default 4)
- SQLAlchemy 2.0 async (sqlalchemy[asyncio]>=2.0.0) — ORM with async support
- asyncpg>=0.29.0 — async PostgreSQL driver
- Alembic>=1.13.0 — database migrations (async env.py using create_async_engine)

LLM
---
- OpenAI SDK (openai>=1.12.0) — calls Ollama Cloud's OpenAI-compatible /v1
  endpoint (NOT the Anthropic SDK; the project migrated from Claude to Ollama)
- Ollama Cloud — hosted LLM inference at https://ollama.com/v1, model: glm-5.2
- tenacity>=8.2.0 — retry with exponential backoff (3 attempts, 1-10s wait)

DATABASE / VECTOR STORE
-----------------------
- Neon PostgreSQL — serverless Postgres for conversation/message persistence
- ChromaDB>=1.0.0 — persistent vector store (local filesystem, no server)
  - Two collections: "company_docs" (raw ingested), "skill_docs" (playbooks)
  - Built-in ONNX all-MiniLM-L6-v2 embedding function (79MB, NO torch)
  - 384-dimensional embeddings
  - Multi-tenant filtering via ChromaDB `where` clause on user_id metadata

CHUNKING / DOCUMENT PROCESSING
------------------------------
- tiktoken>=0.8.0 — cl100k_base encoding for token counting and text splitting
  - 500-token chunks, 50-token overlap
- pypdf==5.1.0 — PDF text extraction
- python-frontmatter>=1.1.0 — markdown with YAML frontmatter for skill docs
- python-dotenv==1.0.1 — .env file loading

GOOGLE APIS
-----------
- google-api-python-client==2.159.0 — Drive (ingestion), Calendar (events),
  Sheets (row append)
- google-auth-oauthlib==1.2.1 — OAuth 2.0 installed app flow
- Scopes: drive.readonly, calendar, spreadsheets
- Credentials loaded from credentials.json file OR GOOGLE_CREDENTIALS_JSON /
  GOOGLE_TOKEN_JSON env vars (for cloud deployment)

SLACK
-----
- slack_sdk>=3.0.0 — WebClient for channel history, thread replies, posting
- Scopes: channels:history, channels:read, groups:history, groups:read,
  chat:write, users:read, links:read, channels:join (auto-joins public
  channels before ingestion)

AUTHENTICATION & SECURITY
-------------------------
- python-jose[cryptography]>=3.3.0 — JWT encode/decode (HS256 shared-secret
  AND RS256 Auth0 JWKS verification)
- httpx>=0.27.0 — fetches Auth0 JWKS endpoint
- slowapi>=0.1.9 — per-user rate limiting (30/min default)
- structlog>=24.1.0 — structured JSON logging
- prometheus-fastapi-instrumentator>=7.0.0 — metrics at /metrics

BACKGROUND JOBS / SCHEDULING / STREAMING
---------------------------------------
- arq>=0.26.0 — async job queue (Redis-backed) for ingestion + synthesis
- redis>=5.0.0 — Redis client
- apscheduler>=3.10.0 — in-process cron scheduler (AsyncIOScheduler)
- websockets>=12.0 — WebSocket transport for streaming chat

FRONTEND
--------
- Vite 5.4 + React 18.3 + TypeScript 5.6 (in frontend/)
- Builds to static/ (served by FastAPI)
- Typed API client for all /api/v1 endpoints + WebSocket streaming helper
- Pages: ChatPage (streaming chat + confirm/decline), SkillsPage (admin)

INFRASTRUCTURE
--------------
- Docker multi-stage build (python:3.11-slim builder + final, non-root
  appuser UID 1000, system-wide pip install)
- docker-compose.yml: backend + worker + redis services
- Cloudflare Tunnel — public URL exposure for the live demo
- Railway config (railway.json), Render config (render.yaml)

TESTING
-------
- pytest + pytest-asyncio
- respx>=0.21.0 — mocks the Ollama Cloud OpenAI-compatible HTTP endpoint
- aiosqlite — sqlite for hermetic test DB
- 40 tests across 6 files (all passing):
  - test_chunking.py (4): token count, chunk basic, chunk empty, chunk docs
  - test_config.py (2): validate_config missing keys / passes
  - test_auth.py (6): 401 for missing/empty/no-Bearer tokens
  - test_integration.py (5): plain answer, propose+confirm, decline, Auth0
    rejection, conversation isolation
  - test_ws_chat.py (7): WS auth, token streaming, tool_proposal, tool_confirm
  - test_agent_integration.py (8): respx-mocked Ollama, multi-turn tool flow,
    audit log creation, e2e HTTP chat, skill audit, user-scoped audit,
    WS audit parity
  - test_ingest_jobs.py (8): async ingest enqueue + status, ownership isolation

===============================================================================
SECTION 3: FULL PROJECT STRUCTURE
===============================================================================

company-brain/
├── .env                          # live secrets (gitignored) — API keys, DB URL, JWT secret
├── .env.example                  # template with all 25+ env vars documented
├── .dockerignore                 # excludes secrets, venvs, git, cache from Docker context
├── .gitignore                    # excludes .venv, .env, data/, node_modules/, static/assets/
├── ruff.toml                     # line-length 140 (fixes pre-existing E501 CI failures)
├── Dockerfile                    # multi-stage: builder (gcc for C extensions) + final (non-root)
├── docker-compose.yml            # backend + worker + redis services, volumes, healthcheck
├── railway.json                  # Railway deployment (Dockerfile builder, /health check)
├── render.yaml                   # Render deployment (Docker runtime, /health, 1GB disk)
├── requirements.txt              # 27 dependencies (no torch/sentence-transformers)
├── pytest.ini                    # asyncio_mode = auto, testpaths = tests
├── alembic.ini                   # Alembic config (async env.py)
├── alembic/
│   ├── env.py                    # async migration env — create_async_engine, NullPool, asyncio.run
│   ├── script.py.mako            # migration file template
│   └── versions/
│       ├── 0001_initial.py       # creates conversations + messages tables (user_id columns)
│       └── 0002_audit_logs.py    # creates audit_logs table (indexes on user/conversation/action)
├── .github/workflows/
│   └── ci.yml                    # CI: install deps + ruff, install respx/aiosqlite, syntax, pytest
├── scripts/
│   ├── entrypoint.sh             # gunicorn with UvicornWorker (WEB_CONCURRENCY), alembic upgrade, $PORT
│   ├── worker-entrypoint.sh      # arq app.worker.WorkerSettings
│   └── init_db.sh                # one-time local setup: generate + apply initial migration
├── app/
│   ├── __init__.py
│   ├── config.py                 # all env vars, validate_config(), JWT/CORS/rate/LLM/Redis/schedule settings, shared limiter
│   ├── auth.py                   # verify_token(): Auth0 RS256/JWKS + HS256 shared-secret fallback; AuthError
│   ├── llm.py                    # OpenAI client for Ollama Cloud; chat_completion() + chat_completion_stream()
│   ├── embeddings.py             # ChromaDB ONNX default embedding function (all-MiniLM-L6-v2, 79MB, no torch)
│   ├── database.py               # async engine (pool 10/20), Conversation/Message/AuditLog models, get_db
│   ├── google_auth.py            # OAuth 2.0 installed app flow; file or env-var credentials; token refresh
│   ├── drive_ingest.py           # Google Drive ingestion: list folder, export Docs, download PDF/txt, extract text, user_id
│   ├── slack_ingest.py           # Slack ingestion: history pagination, thread resolution, 15-min conversation grouping, channels:join, user_id
│   ├── chunking.py               # tiktoken cl100k_base chunking (500 tokens, 50 overlap), user_id propagation
│   ├── vectorstore.py            # ChromaDB: add_chunks (collision-safe IDs), query (where user_id), query_both, delete_by_ids
│   ├── rag.py                    # single-turn RAG: retrieve + LLM prompt + answer (used by CLI)
│   ├── tools.py                  # 3 tools in OpenAI function-calling format + describe_tool_call + execute_tool
│   ├── agent.py                  # multi-turn agent loop: tool calling, confirmation gating, DB persistence, audit, streaming
│   ├── audit.py                  # record(): append immutable AuditLog row in-tx; list_for_user(); action type constants
│   ├── skill_store.py            # per-user skill CRUD: markdown + frontmatter in data/skills/{user_id}/; approve/reject embed/delete
│   ├── skill_synthesis.py        # LLM-based procedure extraction from chunk batches; creates skill docs; incremental timestamp
│   ├── cli.py                    # CLI: ingest, ingest-slack, synthesize, ask (all with --user-id)
│   ├── api_v1.py                 # APIRouter /api/v1 (chat, skills, synthesize, conversations, audit) + /api/ingest router
│   ├── jobs.py                   # ARQ enqueue/status helpers (pool passed in, per-user ownership in Redis)
│   ├── worker.py                 # ARQ WorkerSettings + 3 job functions: ingest_drive, ingest_slack, synthesize
│   ├── scheduler.py              # APScheduler AsyncIOScheduler cron for ingest + synthesis (enqueues via Redis pool)
│   └── server.py                 # FastAPI app: lifespan, v1+ingest routers, root compat routes, /ws/chat WebSocket, /health, static
├── frontend/
│   ├── package.json              # Vite + React 18 + TS deps
│   ├── tsconfig.json             # strict TS, jsx react-jsx, @/* path alias
│   ├── vite.config.ts            # builds to ../static/, dev proxy /api + /ws to :8000
│   ├── index.html                # SPA entry
│   └── src/
│       ├── main.tsx              # ReactDOM.createRoot
│       ├── App.tsx               # token prompt + nav + page routing (chat/skills)
│       ├── index.css             # global styles
│       ├── api/client.ts         # typed /api/v1 client + WebSocket streaming (openChatStream, sendChat, confirmTool)
│       └── pages/
│           ├── ChatPage.tsx      # streaming chat via WS, token accumulation, tool_proposal confirm/decline cards
│           └── SkillsPage.tsx    # skills list/detail/edit/approve/reject + run-synthesis button
├── static/
│   ├── index.html                # built SPA bundle (Vite output)
│   └── assets/                   # JS + CSS bundles (gitignored, rebuilt from frontend/)
├── tests/
│   ├── __init__.py
│   ├── conftest.py               # hermetic env overrides (sqlite, OLLAMA, JWT, Redis, Auth0 off)
│   ├── test_chunking.py          # 4 tests
│   ├── test_config.py            # 2 tests
│   ├── test_auth.py              # 6 tests
│   ├── test_integration.py       # 5 e2e tests (HTTP chat, confirm, decline, Auth0, isolation)
│   ├── test_ws_chat.py           # 7 WS tests (auth, streaming, tool_proposal, tool_confirm)
│   ├── test_agent_integration.py # 8 hermetic tests (respx-mocked Ollama, audit, multi-turn)
│   └── test_ingest_jobs.py       # 8 async ingest enqueue/status tests
└── data/
    ├── chroma/                   # ChromaDB persistent storage (gitignored, volume-mounted)
    ├── skills/                   # skill docs per user (gitignored, volume-mounted)
    └── .last_synthesis_at         # incremental synthesis timestamp

===============================================================================
SECTION 4: DATABASE SCHEMA
===============================================================================

Three tables, all in Neon PostgreSQL. Managed via Alembic async migrations.
All datetime columns are timezone-aware (DateTime(timezone=True)).

TABLE: conversations
--------------------
  Column          Type            Constraints
  id              String          PRIMARY KEY (UUID string generated in app)
  user_id         String          NOT NULL, INDEXED (multi-tenant partition key)
  created_at      DateTime(tz)    NOT NULL, default now(utc)
  updated_at      DateTime(tz)    NOT NULL, default now(utc), onupdate now(utc)
  pending_action  JSON            NULLABLE (holds a pending tool_use_block while
                                  awaiting user confirmation)

Relationship: conversations 1 — N messages (messages.conversation_id FK,
              cascade delete-orphan, lazy="selectin", ordered by created_at)

TABLE: messages
--------------
  Column           Type      Constraints
  id               Integer   PRIMARY KEY, autoincrement
  conversation_id  String    FK -> conversations.id ON DELETE CASCADE, INDEXED
  role             String(16) NOT NULL ("user" | "assistant")
  content          JSON      NOT NULL
  created_at       DateTime(tz) NOT NULL, default now(utc)

content JSON shape:
  - user text message: a plain string, e.g. "what is the deploy process?"
  - assistant text: [{"type":"text","text":"..."}]
  - assistant with tool call: [{"type":"text","text":"..."},
                                 {"type":"tool_use","id":"call_1",
                                  "name":"send_slack_message","input":{...}}]
  - tool result (stored as role="user"):
        [{"type":"tool_result","tool_use_id":"call_1","content":"Result: ...","is_error":false}]

TABLE: audit_logs
-----------------
  Column          Type        Constraints
  id              Integer     PRIMARY KEY, autoincrement
  user_id         String      NOT NULL, INDEXED
  conversation_id String      NULLABLE, INDEXED
  action_type     String(32)  NOT NULL, INDEXED
  tool_name       String(64)  NULLABLE
  payload         JSON        NULLABLE
  status          String(16)  NOT NULL, default "success"
  created_at      DateTime(tz) NOT NULL, default now(utc)

action_type values (convention, not an enum): tool_proposed, tool_confirmed,
tool_declined, skill_approved, skill_rejected. Rows are append-only (immutable).
payload carries e.g. {"tool_id","arguments","description"} for proposals, and
{"tool_id","arguments","result"} for confirmations.

===============================================================================
SECTION 5: ALL API ENDPOINTS
===============================================================================

VERSIONED API (prefix /api/v1) — the canonical interface
--------------------------------------------------------

POST /api/v1/chat
  Auth: Bearer JWT
  Request body: {"conversation_id": str|null, "message": str}
  Response 200: {"type":"message"|"confirmation_required",
                 "conversation_id": str,
                 "answer": str,             # if message
                 "sources": [{name,url,type}],
                 "description": str,         # if confirmation_required
                 "tool_name": str,           # if confirmation_required
                 "tool_input": dict,         # if confirmation_required
                 "explanation": str}         # if confirmation_required
  Runs the synchronous multi-turn agent. Persists user message, retrieves
  context, calls LLM, returns either a final answer or a proposed action.

POST /api/v1/chat/confirm
  Auth: Bearer JWT
  Request body: {"conversation_id": str, "approved": bool}
  Response 200: same shape as /chat (the agent continues after confirm/decline)
  Executes or declines the pending tool, records an audit row, feeds the
  result back to the LLM, and returns the agent's follow-up.

GET /api/v1/skills
  Auth: Bearer JWT
  Response: {"skills": [{id, title, status, created_at, updated_at}, ...]}

GET /api/v1/skills/{skill_id}
  Auth: Bearer JWT
  Response: {id, title, status, created_at, updated_at, source_chunk_ids, content}

PUT /api/v1/skills/{skill_id}
  Auth: Bearer JWT
  Request body: {"content": str}
  Response: {"status": "ok"}

POST /api/v1/skills/{skill_id}/approve
  Auth: Bearer JWT
  Response: {"status": "ok"}
  Embeds the skill into the skill_docs ChromaDB collection; records
  skill_approved audit row.

POST /api/v1/skills/{skill_id}/reject
  Auth: Bearer JWT
  Response: {"status": "ok"}
  Removes the skill from retrieval; records skill_rejected audit row.

POST /api/v1/synthesize
  Auth: Bearer JWT
  Response: {"status":"ok","batches_processed":N,"new_skills":N,"skipped_duplicates":N}

POST /api/v1/synthesize/async
  Auth: Bearer JWT
  Response: {"status":"queued","job_id": str}  (requires Redis)
  Enqueues a background synthesis job on the ARQ pool.

GET /api/v1/conversations
  Auth: Bearer JWT
  Response: {"conversations": [{id, created_at, updated_at}, ...]}

GET /api/v1/audit
  Auth: Bearer JWT
  Query: ?limit=100
  Response: {"audit": [{id, user_id, conversation_id, action_type, tool_name,
                        payload, status, created_at}, ...]}
  Returns only the authenticated user's audit entries (user-scoped).

ASYNC INGEST API (prefix /api/ingest) — ARQ-backed, returns job_id
-------------------------------------------------------------------

POST /api/ingest/drive
  Auth: Bearer JWT
  Query: ?folder_id=str|none (defaults to GOOGLE_DRIVE_FOLDER_ID)
  Response: {"job_id": str, "status": "queued"}  (requires Redis)
  Enqueues ingest_drive background job. 503 if Redis unavailable.

POST /api/ingest/slack
  Auth: Bearer JWT
  Response: {"job_id": str, "status": "queued"}  (requires Redis)
  Enqueues ingest_slack background job. 503 if Redis unavailable.

GET /api/ingest/status/{job_id}
  Auth: Bearer JWT
  Response: {"job_id": str, "status": "queued|in_progress|complete|deferred",
             "result": dict|null}
  Enforces per-user ownership (404 if not owned by caller). 503 if no Redis.

WEBSOCKET
---------

WS /ws/chat
  Auth: first message {"action":"auth","token":"<jwt>"} OR subprotocol
  Client sends: {"action":"chat","message":"...","conversation_id":"..."}
                {"action":"tool_confirm","conversation_id":"...","tool_id":"...","approved":bool}
  Server streams events via send_json:
    {"type":"sources","sources":[...],"conversation_id":"..."}
    {"type":"token","content":"..."}     (per LLM delta, real-time)
    {"type":"tool_proposal","tool_id":"...","tool_name":"...","arguments":{...},"description":"...","conversation_id":"..."}
    {"type":"tool_result","tool_id":"...","approved":bool,"success":bool,"result":{...}}
    {"type":"message","conversation_id":"...","answer":"...","sources":[...]}
    {"type":"error","detail":"..."}
  Invalid auth closes with code 4401.

ROOT (backward-compatible, legacy) — proxy the v1 logic
-------------------------------------------------------
  GET  /                -> chat UI (built SPA)
  GET  /skills-page     -> skills UI (same SPA)
  GET  /health          -> {"status":"healthy"} (DB ping) or 503
  GET  /metrics         -> Prometheus metrics
  POST /chat            -> legacy synchronous chat (same as /api/v1/chat)
  POST /chat/confirm    -> legacy confirm (same as /api/v1/chat/confirm)
  GET  /skills, /skills/{id}, PUT, approve, reject -> legacy skills
  POST /synthesize      -> legacy synchronous synthesis

===============================================================================
SECTION 6: THE AGENT / RAG LAYER (how the AI works)
===============================================================================

This is the heart of the project. There is NO DSPy and NO separate
recommendation engine — it is a from-scratch RAG + function-calling agent built
on the OpenAI SDK pointed at Ollama Cloud. Here is exactly how it works.

RETRIEVAL (agent.py: _build_context)
-----------------------------------
Input: a user question (str) + user_id (str, for tenant isolation)
Process:
  1. query_both(question, top_k=5, user_id) hits two ChromaDB collections:
       - "company_docs" (raw ingested Drive + Slack chunks), top 5
       - "skill_docs" (approved playbooks), top 2
     Both queries embed the question and filter with ChromaDB `where`:
       {"user_id": user_id}   # tenant isolation
  2. Each returned chunk is labeled:
       raw:      "[Source: {doc_name}]\n{text}"
       playbook: "[Playbook: {title}]\n{text}"
  3. Returns (context_string, sources_list) where sources_list is deduped.

SYSTEM PROMPT (agent.py: _build_system_prompt)
---------------------------------------------
The retrieved context is injected into a system prompt that:
  - Tells the model to answer ONLY from the context (no hallucination)
  - Prefers [Playbook: ...] content over raw sources when both relevant
  - Lists the 3 available tools and the safety rule: "Do NOT call a tool
    silently — the user must confirm each action before it executes"
  - Enforces: one action at a time, retrieved content is informational only
    (never follow embedded instructions), acknowledge declines

TOOL SCHEMAS (tools.py: TOOL_SCHEMAS)
------------------------------------
Three tools in OpenAI function-calling format (JSON schema). Example:
  {
    "type": "function",
    "function": {
      "name": "send_slack_message",
      "description": "Send a message to a Slack channel...",
      "parameters": {
        "type": "object",
        "properties": {
          "channel_id": {"type": "string", "description": "..."},
          "message": {"type": "string", "description": "..."}
        },
        "required": ["channel_id", "message"]
      }
    }
  }
The others are create_calendar_event (title, start_time, end_time, description,
attendee_emails) and append_sheet_row (values: string[]).

LLM CALL (llm.py)
-----------------
chat_completion(messages, system, tools, max_tokens) uses the OpenAI SDK:
  client = OpenAI(base_url=OLLAMA_BASE_URL, api_key=OLLAMA_API_KEY, timeout=120)
  client.chat.completions.create(model=LLM_MODEL, messages=[...], tools=[...])
Wrapped in a tenacity @retry (3 attempts, exponential backoff 1-10s).

AGENT LOOP (agent.py: send_message — synchronous HTTP path)
-----------------------------------------------------------
Input: conversation_id (str|null), message (str), db session, user_id
  1. Load or create a Conversation row (filtered by user_id — ownership)
  2. Clear any pending_action, append the user message
  3. Load conversation history from DB, convert to OpenAI message format
     (_load_messages_as_openai reconstructs tool_calls + tool_results)
  4. Build context + system prompt
  5. Call LLM with tools
  6. _process_response:
       - If LLM returned tool_calls:
           * Persist the assistant message (text + tool_use block)
           * Set conv.pending_action = {"tool_use_block": {id, name, input}}
           * Record an audit row: tool_proposed
           * Return {"type":"confirmation_required", tool_name, tool_input, ...}
       - Else (plain answer):
           * Persist assistant text message
           * Return {"type":"message", answer, sources}
  7. Commit the transaction (rollback on any error)

CONFIRMATION GATE (agent.py: confirm_action)
-------------------------------------------
Input: conversation_id, approved (bool), db, user_id
  1. Load conversation (filtered by user_id). 404 if not found / not owned.
  2. Read conv.pending_action (the stored tool_use_block). Error if none.
  3. Clear pending_action.
  4. If approved: execute_tool(tool_name, tool_input) -> runs the real action
     (posts to Slack, creates Calendar event, appends Sheet row).
     Record audit: tool_confirmed, status "success" or "tool_error".
  5. If declined: record audit: tool_declined, status "declined".
  6. Append the tool_result as a role="user" tool_result block.
  7. _continue(): re-call the LLM with the updated history (the LLM now sees
     the tool result), max 5 iterations.

STREAMING AGENT (agent.py: send_message_stream — WebSocket path)
----------------------------------------------------------------
Same logic, but yields events instead of returning a dict. The streaming LLM
call runs in a worker thread (threading.Thread) and forwards content deltas to
the async event loop via asyncio.Queue + run_coroutine_threadsafe:

  def _produce():
      gen = _call_llm_stream(...)
      for chunk in gen:
          delta = chunk.choices[0].delta
          if delta.content:
              asyncio.run_coroutine_threadsafe(queue.put(("token", delta.content)), loop)
          if delta.tool_calls:
              accumulate by index (id, name, arguments fragments)
      queue.put(("done", {content, tool_calls}))

Yields:
  {"type":"token","content":"..."}            # per LLM content delta, real-time
  {"type":"tool_proposal", tool_id, tool_name, arguments, description, conversation_id}
  {"type":"message", answer, sources}
  {"type":"error", detail}

This is the key code for the streaming agent (real snippet):

  async def _stream_llm_events(history, system_prompt, user_id):
      loop = asyncio.get_event_loop()
      queue = asyncio.Queue()
      tool_acc = {}
      def _produce():
          gen = _call_llm_stream(history, system=system_prompt, tools=TOOL_SCHEMAS, ...)
          for chunk in gen:
              delta = chunk.choices[0].delta
              if delta.content:
                  asyncio.run_coroutine_threadsafe(queue.put(("token", delta.content)), loop).result()
              if delta.tool_calls:
                  for tc in delta.tool_calls:
                      slot = tool_acc.setdefault(tc.index, {"id":None,"name":"","arguments":""})
                      if tc.id: slot["id"] = tc.id
                      if tc.function.name: slot["name"] = tc.function.name
                      if tc.function.arguments: slot["arguments"] += tc.function.arguments
          ...assemble tool_calls list...
          asyncio.run_coroutine_threadsafe(queue.put(("done", {...})), loop).result()
      threading.Thread(target=_produce, daemon=True).start()
      while True:
          kind, val = await queue.get()
          if kind == "token": yield {"type":"token","content":val}
          elif kind == "error": raise val
          elif kind == "done": yield {"type":"_final", ...}; return

KEY DESIGN POINT: the agent never executes a tool without user confirmation.
The LLM proposes, the user confirms, the result is fed back. This is enforced
both in the system prompt AND in the application code (pending_action stored
on the conversation row, confirm_action gate).

===============================================================================
SECTION 7: SKILL SYNTHESIS (how it "learns" procedures)
===============================================================================

This is the automatic procedure-extraction pipeline. It turns raw ingested
chunks into reviewed playbooks.

PIPELINE (skill_synthesis.py)
-----------------------------
1. Read the last-synthesis timestamp from data/.last_synthesis_at
2. Fetch NEW chunks from ChromaDB since that timestamp (incremental), filtered
   by user_id. Paginates 1000 at a time.
3. Batch into groups of 15 (SYNTHESIS_CHUNKS_PER_BATCH).
4. For each batch, _synthesize_batch:
   - Build a prompt: "[Excerpt 1 - Source: #engineering]\n<text>\n\n..."
   - System prompt asks the LLM to return JSON: an array of objects with
     {title, summary, steps[], source_chunk_ids[]}
   - Call chat_completion (max_tokens 4096)
   - Parse JSON (with regex fallback if the LLM wrapped in fences)
   - Map "Excerpt N" references back to real chunk IDs
5. For each procedure:
   - Slugify the title -> skill_id
   - Skip if a skill with that title already exists (dedup)
   - create_skill(): write a markdown file with YAML frontmatter to
     data/skills/{user_id}/{skill_id}.md
6. Update the last-synthesis timestamp.

SKILL DOCUMENT FORMAT (markdown + frontmatter)
----------------------------------------------
  ---
  id: deploy-the-backend
  title: Deploy the Backend
  status: draft
  source_chunk_ids: [user1_doc123_0, user1_doc456_2]
  created_at: 2026-08-20T12:00:00Z
  updated_at: 2026-08-20T12:00:00Z
  ---
  Summary line.

  1. Step one.
  2. Step two.

REVIEW + APPROVAL (skill_store.py)
----------------------------------
Admin UI lists skills. On approve:
  - Set frontmatter status="approved"
  - Build a chunk: "# {title}\n\n{content}"
  - add_chunks([chunk], collection="skill_docs")  # embeds into the separate
    collection that the agent prefers in retrieval
On reject:
  - Set status="rejected"
  - delete_by_ids from the skill_docs collection (removes from retrieval)
Both record audit rows (skill_approved / skill_rejected).

===============================================================================
SECTION 8: AUTHENTICATION SYSTEM
===============================================================================

Two modes, configured by env. Both resolve to a user_id (the JWT sub claim)
used for all multi-tenant isolation.

MODE A — SHARED-SECRET HS256 (dev / fallback)
---------------------------------------------
Env: JWT_SECRET set, AUTH0_ENABLED=false
verify_token(token):
  payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
  return payload["sub"]
Tokens minted by the team with the shared secret. 24h expiry.

MODE B — AUTH0 RS256 / JWKS (production)
----------------------------------------
Env: AUTH0_ENABLED=true, AUTH0_DOMAIN, AUTH0_AUDIENCE
verify_token(token):
  1. jwt.get_unverified_header(token) -> extract "kid"
  2. If no kid -> reject (401) — it's not an Auth0 token
  3. Fetch JWKS from https://{AUTH0_DOMAIN}/.well-known/jwks.json (cached
     AUTH0_JWKS_CACHE_TTL seconds, default 1h; network failure -> 503)
  4. Find the signing key by kid; if none -> 401
  5. jwt.decode(token, signing_key, algorithms=["RS256"],
                audience=AUTH0_AUDIENCE, issuer="https://{AUTH0_DOMAIN}/")
  6. Return payload["sub"]

WHERE IT'S ENFORCED
-------------------
- HTTP: the get_user_id dependency in api_v1.py extracts the Bearer token and
  calls verify_token; raises HTTPException(401) on AuthError.
- WebSocket: /ws/chat reads {"action":"auth","token":...} as the first
  message OR the first subprotocol; closes with code 4401 on failure.
- Every conversation/skill/audit query is then filtered by the resolved
  user_id, so users can only ever see their own data.

AUTH FLOW (the actual dependency, real snippet):
  async def get_user_id(authorization: str | None = Header(None)) -> str:
      if not authorization or not authorization.startswith("Bearer "):
          raise HTTPException(status_code=401, detail="Authorization Bearer token required")
      token = authorization.removeprefix("Bearer ").strip()
      try:
          return verify_token(token)
      except AuthError as e:
          raise HTTPException(status_code=e.status_code, detail=e.detail)

===============================================================================
SECTION 9: BACKGROUND JOBS, SCHEDULING, AND STREAMING TRANSPORTS
===============================================================================

BACKGROUND JOB QUEUE (ARQ + Redis)
----------------------------------
- app/worker.py: WorkerSettings registers ingest_drive, ingest_slack,
  synthesize as ARQ job functions (max_jobs=10, job_timeout=600, max_tries=3).
- app/jobs.py: enqueue helpers take the ARQ pool as an arg (decoupled from the
  app). Each enqueued job is tagged in Redis with the owning user_id
  (hset job:{job_id} user_id ...) so the status endpoint enforces ownership.
- The ARQ pool (ArqRedis) is created in server.py's lifespan and stored on
  app.state.redis. If Redis is down, the server still boots; only the async
  ingest endpoints return 503.
- Worker runs as a separate Docker service (scripts/worker-entrypoint.sh ->
  arq app.worker.WorkerSettings) sharing the same volumes (ChromaDB, skills,
  credentials).

SCHEDULING (APScheduler)
------------------------
- app/scheduler.py: AsyncIOScheduler with CronTrigger.
- Three jobs: drive ingest, slack ingest, synthesis. Cron from env
  (SCHEDULE_INGEST_CRON default "0 */6 * * *", SCHEDULE_SYNTHESIS_CRON
  default "30 */6 * * *"). Scheduled runs use SCHEDULE_USER_ID as the tenant.
- Scheduled jobs ENQUEUE onto the Redis pool (single execution path with the
  API-triggered jobs), not run inline.
- Started/stopped in the FastAPI lifespan (only when SCHEDULE_ENABLED=true).

WEBSOCKET STREAMING (transport parity)
--------------------------------------
The HTTP endpoints (/api/v1/chat, /chat/confirm) and /ws/chat share the SAME
conversation persistence (_add_user_message, _add_assistant_message,
_add_tool_result), the SAME permission checks (conversation.user_id filter),
and the SAME audit recording. The only difference is the streaming path
yields events instead of returning a final dict.

===============================================================================
SECTION 10: DEPLOYMENT SETUP
===============================================================================

LIVE DEPLOYMENT
---------------
- Server: uvicorn on localhost:8000 tunneled via Cloudflare Tunnel (public
  URL, changes on restart). Docker-ready via gunicorn + UvicornWorker.
- Database: Neon serverless PostgreSQL (pooler endpoint)
- LLM: Ollama Cloud (https://ollama.com/v1, model glm-5.2)
- Vector store: local ChromaDB (persistent at ./data/chroma, volume-mounted)
- Frontend: built React/TS SPA served from static/ by FastAPI

DOCKER (docker-compose.yml)
---------------------------
Three services:
  1. backend: builds from Dockerfile, port 8000, env_file .env, mounts
     data/chroma, data/skills, data/.last_synthesis_at, credentials.json,
     token.json; depends_on redis (healthy)
  2. worker: same image, entrypoint scripts/worker-entrypoint.sh (arq), same
     volumes; depends_on redis (healthy)
  3. redis: redis:7-alpine, appendonly persistence, redis-cli healthcheck,
     named volume redis-data

Dockerfile: multi-stage. Builder stage has gcc for C extensions (asyncpg,
onnxruntime). Final stage is python:3.11-slim, non-root user appuser UID 1000,
system-wide pip install (not --user since non-root), mkdir data dirs.

ENTRYPOINT (scripts/entrypoint.sh)
---------------------------------
Validates env (DATABASE_URL, OLLAMA_*), runs `alembic upgrade head`, then:
  gunicorn app.server:app --workers $WEB_CONCURRENCY \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:$PORT --timeout $WORKER_TIMEOUT --graceful-timeout 30
Falls back to plain uvicorn if gunicorn is unavailable.

CI/CD (GitHub Actions, .github/workflows/ci.yml)
-----------------------------------------------
On every push/PR to master/main:
  - Set up Python 3.11
  - pip install -r requirements.txt + pytest pytest-asyncio ruff aiosqlite httpx respx
  - ruff check app/ --select E,F
  - python syntax check (ast.parse all app/*.py)
  - pytest tests/ -v (env: OLLAMA_API_KEY=test, DATABASE_URL=sqlite)

ENVIRONMENT VARIABLES (all in .env.example)
--------------------------------------------
Required: DATABASE_URL, OLLAMA_BASE_URL, OLLAMA_API_KEY, LLM_MODEL
Auth: JWT_SECRET, JWT_ALGORITHM, JWT_EXPIRE_HOURS, AUTH0_ENABLED, AUTH0_DOMAIN,
      AUTH0_AUDIENCE, AUTH0_JWKS_CACHE_TTL
LLM: LLM_MODEL, LLM_TIMEOUT, LLM_MAX_TOKENS, LLM_MAX_TOKENS_SYNTHESIS
Chunking: CHUNK_SIZE (500), CHUNK_OVERLAP (50), TOP_K_RETRIEVAL (5)
Google: GOOGLE_DRIVE_FOLDER_ID, GOOGLE_SHEET_ID, GOOGLE_SHEET_TAB,
        GOOGLE_CREDENTIALS_JSON, GOOGLE_TOKEN_JSON
Slack: SLACK_BOT_TOKEN, SLACK_CHANNEL_IDS
Server: CORS_ORIGINS, RATE_LIMIT (30/minute), WEB_CONCURRENCY (4),
        WORKER_TIMEOUT (120)
Jobs: REDIS_URL
Scheduling: SCHEDULE_ENABLED, SCHEDULE_INGEST_CRON, SCHEDULE_SYNTHESIS_CRON,
            SCHEDULE_USER_ID

===============================================================================
SECTION 11: HARD BUGS HIT AND HOW THEY WERE FIXED
===============================================================================

1. CHROMADB TYPE HINT CRASH (TypeError in CI)
   Symptom: CI failed across multiple tests:
     TypeError: unsupported operand type(s) for |: 'function' and 'NoneType'
   at app/vectorstore.py:12  _client: chromadb.PersistentClient | None = None
   Cause: chromadb.PersistentClient is a factory FUNCTION, not a class, so
   Python's runtime evaluation of the `X | None` union crashed.
   Fix: added `from __future__ import annotations` at the top of vectorstore.py
   (PEP 563 defers all type-hint evaluation to strings, so the union is never
   evaluated at runtime). Committed as d269616.

2. VITE BUILD ASSETS 404 (blank UI)
   Symptom: the chat UI rendered blank in the browser.
   Cause: Vite's built index.html references /assets/*.js and /assets/*.css
   (absolute paths), but FastAPI only mounted static files at /static. The
   JS/CSS 404'd, so React never booted.
   Fix: in server.py, mount the assets directory at /assets in addition to
   /static (both point at the same on-disk directory). Committed as 573f1dc.

3. SPANISH-CHARACTER CHUNK BOUNDARIES (tiktoken)
   Early on, splitting on raw text boundaries corrupted multibyte characters.
   Fix: chunking operates on tiktoken TOKEN IDS (encode -> slice token ids ->
   decode), so boundaries are always on token boundaries, never mid-character.

4. AGENT LOOP PERSISTENCE ORDERING
   Symptom: after a tool confirmation, the LLM call failed because the
   conversation history was invalid (tool_result appeared before the
   assistant tool_use message it answered).
   Fix: persist the assistant message WITH the tool_use block BEFORE setting
   pending_action, so when the tool_result is appended next, the OpenAI
   message ordering (assistant tool_calls -> tool result) is valid.

5. OPENAI SDK + RESPX MOCKING (tests)
   Symptom: respx mocks didn't intercept the OpenAI SDK's HTTP calls in tests
   (APIConnectionError), so the hermetic integration suite hung.
   Cause: OpenAI SDK 3.3.0 uses an INTERNAL httpx transport, so respx's global
   httpx patch is bypassed.
   Fix: build an httpx.Client(transport=httpx.MockTransport(router.handler))
   and inject it as the http_client into the OpenAI client. The test fixture
   patches llm.get_client to construct the client this way inside the respx
   context.

6. ARQ ENQUEUE DOUBLE user_id
   Symptom: TypeError: enqueue_job() got multiple values for argument 'user_id'
   Cause: enqueue_job passed user_id both as a named arg AND inside kwargs.
   Fix: set kwargs["user_id"] = user_id inside enqueue_job and don't pass it
   separately from the caller.

7. PRE-EXISTING E501 LINT FAILURES SILENTLY BREAKING CI
   The repo had no ruff config and CI ran `ruff --select E,F`, so 118 pre-
   existing line-too-long errors failed CI but nobody noticed. Fix: added
   ruff.toml with line-length 140, and cleaned up the genuine F-category
   issues (unused imports, unused variables).

8. .env.example OLLAMA_BASE_URL MISMATCH
   .env.example had OLLAMA_BASE_URL=https://ollama.com/api but the live .env
   and docs use https://ollama.com/v1. Fixed .env.example to match.

===============================================================================
SECTION 12: WHAT YOU WOULD BUILD NEXT / KNOWN LIMITATIONS
===============================================================================

KNOWN LIMITATIONS
-----------------
- Single-tenant vector store per process: ChromaDB is embedded (local file),
  not a server, so it can't be shared across multiple backend replicas without
  a shared volume. A managed vector DB (Pinecone/Weaviate) would be needed to
  scale horizontally.
- No real end-user self-service: JWTs are minted out-of-band (no signup/login
  UI). Auth0 is wired for verification but there's no Auth0 Lock integration
  in the frontend.
- Ingestion is all-or-nothing per folder/channel: no incremental Drive change
  detection (no Drive pageToken / last-modified watermark).
- No RBAC: every authenticated user has equal capabilities; isolation is by
  user_id only, not roles (admin vs user).
- Synthesis is batch, not streaming.
- No vector store backup/restore story beyond volume snapshots.

WHAT TO BUILD NEXT
------------------
1. Real user management UI (Auth0 Lock / signup / login) — the JWKS
   verification is already implemented.
2. Incremental Drive ingestion using Drive's change tokens / last-modified
   timestamps to only pull new/changed files.
3. Migrate ChromaDB to a managed vector service (Pinecone/Weaviate) for
   horizontal scaling and multi-replica safety.
4. Role-based access control (admin vs user) layered on top of user_id
   isolation — admins could see all tenants, users only their own.
5. Audit log streaming/alerting (e.g. a webhook on tool_confirmed failures).
6. Streaming synthesis (currently batched; could stream skill drafts as the
   LLM generates them).
7. Conversation list + deletion in the frontend UI.
8. Multi-language document support (chunking is English-centric via tiktoken
   cl100k_base; cjk or other encoders would be needed for some languages).
9. A periodic re-embedding job when the embedding model is upgraded.

===============================================================================
APPENDIX: KEY INTERVIEW TALKING POINTS
===============================================================================

- Multi-tenant isolation is enforced at THREE layers: (1) JWT resolves
  user_id, (2) every ChromaDB query filters `where user_id`, (3) every SQL
  query filters `where user_id`. There is no code path that returns another
  tenant's data.
- The safety gate: the agent NEVER executes a tool autonomously. The LLM
  proposes, the user confirms, the result is fed back. This is enforced in
  both the system prompt AND the application code (pending_action on the
  conversation row). Worth emphasizing — it's a real security property.
- The streaming agent runs the SYNCHRONOUS OpenAI streaming call in a worker
  thread and bridges to the async event loop via asyncio.Queue +
  run_coroutine_threadsafe — this is a non-trivial async/sync bridge worth
  explaining.
- The audit trail is append-only and recorded within the same DB transaction
  as the action it records, so an audit failure rolls back the whole action
  (an audit record can never be lost while the action succeeds).
- The hermetic test suite mocks the Ollama HTTP endpoint with respx by
  injecting an httpx.MockTransport into the OpenAI SDK client — this exercises
  the real SDK serialization/deserialization, not a stubbed function.
- Migration from sentence-transformers/torch (2GB+) to ChromaDB's built-in ONNX
  embedder (79MB, same all-MiniLM-L6-v2 model) cut the Docker image drastically
  while keeping the same embeddings.
- Transport standardization: HTTP and WebSocket share the exact same
  persistence, permission, and audit code paths; only the response shape
  differs (final dict vs yielded events).