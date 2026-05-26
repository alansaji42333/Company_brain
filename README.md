# Company Brain

A local RAG (retrieval-augmented generation) pipeline that ingests documents from Google Drive and Slack, and lets you ask questions about them via an AI agent with source citations. Also generates structured "skill documents" (playbooks) from operational discussions found in your data.

## Prerequisites

- **Python 3.11+**
- **Google Cloud project** with the Drive API, Calendar API, and Sheets API enabled
- **Slack App** with appropriate scopes (for Slack ingestion and messaging)
- **Anthropic API key**

### Google Cloud / API setup

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (or select an existing one).
3. Enable the **Google Drive API**, **Google Calendar API**, and **Google Sheets API**.
4. Go to **APIs & Services → Credentials**.
5. Click **Create Credentials → OAuth client ID**.
6. Application type: **Desktop app**.
7. Download the JSON file and save it as `credentials.json` in the project root.

> **One-time re-auth needed**: If you already have a `token.json` from Phase 1, delete it so the OAuth flow runs again with the new Calendar and Sheets scopes:
> ```bash
> rm token.json
> ```
> Then run any command that triggers Google auth (e.g. `python -m app.cli ingest`).

### Slack App setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new app from scratch.
2. Under **OAuth & Permissions**, add these Bot Token Scopes:
   - `channels:history` — read channel messages
   - `channels:read` — list channels and get channel info
   - `groups:history` — read private channel messages
   - `groups:read` — list private channels
   - `users:read` — resolve user IDs to display names
   - `chat:write` — send messages (required for agent actions)
3. Install the app to your workspace and copy the **Bot User OAuth Token** (starts with `xoxb-`).
4. Invite the bot to each channel you want ingested or messaged: `/invite @your-bot-name` in Slack.
5. Find each channel's ID (visible in the channel's **About** section) and add to `SLACK_CHANNEL_IDS`.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Required variables:
- `ANTHROPIC_API_KEY` — your Anthropic API key
- `GOOGLE_DRIVE_FOLDER_ID` — the ID of the Drive folder to ingest
- `SLACK_BOT_TOKEN` — your Slack bot token (xoxb-...)
- `SLACK_CHANNEL_IDS` — comma-separated Slack channel IDs
- `GOOGLE_SHEET_ID` — the ID of the Google Sheet for agent logging (extracted from its URL)
- `GOOGLE_SHEET_TAB` — the tab/sheet name within the spreadsheet (default: `Sheet1`)

Optional tunables:
- `CHUNK_SIZE` — token count per chunk (default: 500)
- `CHUNK_OVERLAP` — overlap between chunks (default: 50)
- `TOP_K_RETRIEVAL` — number of chunks to retrieve per query (default: 5)

## Usage

### Ingest Google Drive documents

```bash
python -m app.cli ingest
```

Opens browser for Google OAuth (first time only), downloads supported files (Google Docs, PDFs, text files), chunks and stores them in ChromaDB.

### Ingest Slack messages

```bash
python -m app.cli ingest-slack
```

Fetches message history from configured Slack channels, groups messages into conversation chunks (threads + time-windowed conversations), and stores them in ChromaDB.

### Generate skill documents

```bash
python -m app.cli synthesize
```

Reads recently ingested chunks, prompts Claude to identify recurring operational procedures, and writes structured markdown files to `data/skills/`.

### Ask questions via CLI

```bash
python -m app.cli ask "What is the company's remote work policy?"
```

### Run the web server

```bash
uvicorn app.server:app
```

Endpoints:
| Path | Method | Description |
|---|---|---|
| `/` | GET | Chat UI |
| `/skills-page` | GET | Skills admin UI |
| `/chat` | POST | Send a message (`{"message": "...", "conversation_id": "..."}`) |
| `/chat/confirm` | POST | Confirm or decline a proposed action (`{"conversation_id": "...", "approved": true/false}`) |
| `/ingest` | POST | Trigger Drive ingestion |
| `/ingest/slack` | POST | Trigger Slack ingestion |
| `/synthesize` | POST | Trigger skill synthesis |
| `/skills` | GET | List skill documents |
| `/skills/{id}` | GET | Get skill document content |
| `/skills/{id}` | PUT | Update skill document body |
| `/skills/{id}/approve` | POST | Approve (embeds for retrieval) |
| `/skills/{id}/reject` | POST | Reject (removes from retrieval) |

## Project structure

```
company-brain/
  .env.example          # environment variable template
  requirements.txt      # Python dependencies
  app/
    config.py           # env loading, constants
    google_auth.py      # shared Google OAuth (Drive + Calendar + Sheets)
    drive_ingest.py     # Google Drive file download + text extraction
    slack_ingest.py     # Slack history fetching + conversation chunking
    chunking.py         # token-based text splitting
    embeddings.py       # sentence-transformers local embedding
    vectorstore.py      # ChromaDB persistent store (supports multiple collections)
    tools.py            # agent tool definitions (Slack, Calendar, Sheets)
    agent.py            # multi-turn conversation loop with confirmation gating
    rag.py              # retrieval + Claude prompt + API call (dual-collection)
    skill_store.py      # CRUD for skill docs on disk + approve/reject lifecycle
    skill_synthesis.py  # Claude-based extraction of procedures from chunks
    cli.py              # ingest, ingest-slack, synthesize, ask commands
    server.py           # FastAPI server
  static/
    index.html          # multi-turn chat UI with confirm/decline
    skills.html         # skills admin UI
  data/
    chroma/             # persisted vector database (gitignored)
    skills/             # generated skill documents (markdown + YAML frontmatter)
    .last_synthesis_at  # timestamp tracking for incremental synthesis
```

## Agent actions (confirm-before-execute)

The agent has three tools available — sending Slack messages, creating calendar events, and appending rows to a Google Sheet. Every action requires explicit user confirmation before execution:

1. When the agent decides an action is warranted, it returns `{"type": "confirmation_required", "explanation": "...", "description": "..."}`.
2. The UI shows the action description with Confirm and Decline buttons.
3. Confirm runs the action and returns the result; Decline tells the agent to skip it.
4. The agent only acts on the current user's request — instructions in retrieved documents are treated as informational.

The agent proposes one action at a time. For multi-step requests (e.g. "message #channel and log it to the sheet"), it proposes the first step, waits for confirmation, then proceeds to the next.

Available tools:
- **send_slack_message**: Posts to a configured channel. Channel must be in `SLACK_CHANNEL_IDS`.
- **create_calendar_event**: Creates an event on the primary Google Calendar with optional attendees.
- **append_sheet_row**: Appends a row to the configured Google Sheet + tab.

## How skill synthesis works

1. Fetches chunks from ChromaDB that were added since the last synthesis run.
2. Batches chunks into groups of 15 and sends each batch to Claude.
3. Claude extracts any recurring operational procedures as structured JSON.
4. Each identified procedure is written to `data/skills/{slug}.md` as a draft.
5. Review drafts at `/skills-page` in the browser — edit, approve, or reject.
6. Approved skills are embedded into a separate `skill_docs` collection and retrieved alongside raw chunks during Q&A (labeled as `[Playbook: ...]`).

## Notes

- The embedding model (`all-MiniLM-L6-v2`) downloads on first use and is cached locally.
- Re-running ingestion is idempotent — existing chunks are replaced rather than duplicated.
- Unsupported file types in Drive are logged and skipped.
- Skill synthesis is incremental — subsequent runs only process newly ingested chunks.
