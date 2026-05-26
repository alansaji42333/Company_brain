import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_DRIVE_FOLDER_ID: str = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_IDS: str = os.getenv("SLACK_CHANNEL_IDS", "")
GOOGLE_SHEET_ID: str = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_TAB: str = os.getenv("GOOGLE_SHEET_TAB", "Sheet1")

CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "500"))
CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "50"))
TOP_K_RETRIEVAL: int = int(os.getenv("TOP_K_RETRIEVAL", "5"))
TOP_K_SKILLS: int = 2
SYNTHESIS_CHUNKS_PER_BATCH: int = 15

EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
CHROMA_DIR: str = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "chroma")
COLLECTION_NAME: str = "company_docs"
COLLECTION_SKILLS: str = "skill_docs"

CLAUDE_MODEL: str = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS: int = 1024
CLAUDE_MAX_TOKENS_SYNTHESIS: int = 4096
CLAUDE_MAX_TOKENS_AGENT: int = 2048

CREDENTIALS_FILE: str = "credentials.json"
TOKEN_FILE: str = "token.json"
SCOPES: list[str] = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
]

SKILLS_DIR: str = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "skills")
LAST_SYNTHESIS_FILE: str = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", ".last_synthesis_at")

SLACK_CONVERSATION_WINDOW_MINUTES: int = 15

AGENT_MAX_ITERATIONS: int = 5
