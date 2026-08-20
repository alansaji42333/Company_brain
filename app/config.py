import os
from dotenv import load_dotenv
from slowapi import Limiter
from slowapi.util import get_remote_address

load_dotenv()

limiter = Limiter(key_func=get_remote_address)

GOOGLE_DRIVE_FOLDER_ID: str = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_IDS: str = os.getenv("SLACK_CHANNEL_IDS", "")
GOOGLE_SHEET_ID: str = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_TAB: str = os.getenv("GOOGLE_SHEET_TAB", "Sheet1")
DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql+asyncpg://company_brain:company_brain@db:5432/company_brain")

CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "500"))
CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "50"))
TOP_K_RETRIEVAL: int = int(os.getenv("TOP_K_RETRIEVAL", "5"))
TOP_K_SKILLS: int = 2
SYNTHESIS_CHUNKS_PER_BATCH: int = 15

EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
CHROMA_DIR: str = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "chroma")
COLLECTION_NAME: str = "company_docs"
COLLECTION_SKILLS: str = "skill_docs"

OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "https://ollama.com/v1")
OLLAMA_API_KEY: str = os.getenv("OLLAMA_API_KEY", "")
LLM_MODEL: str = os.getenv("LLM_MODEL", "glm-5.2")
LLM_TIMEOUT: float = float(os.getenv("LLM_TIMEOUT", "120"))
LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "2048"))
LLM_MAX_TOKENS_SYNTHESIS: int = int(os.getenv("LLM_MAX_TOKENS_SYNTHESIS", "4096"))

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

# Authentication -----------------------------------------------------------
# If JWT_SECRET is set, shared-secret (HS256) mode is used as a fallback / dev mode.
# If AUTH0_ENABLED is true, JWTs are verified against the Auth0 JWKS endpoint.
JWT_SECRET: str = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_HOURS: int = int(os.getenv("JWT_EXPIRE_HOURS", "24"))

AUTH0_ENABLED: bool = os.getenv("AUTH0_ENABLED", "false").lower() in ("1", "true", "yes")
AUTH0_DOMAIN: str = os.getenv("AUTH0_DOMAIN", "")
AUTH0_AUDIENCE: str = os.getenv("AUTH0_AUDIENCE", "")
AUTH0_JWKS_CACHE_TTL: int = int(os.getenv("AUTH0_JWKS_CACHE_TTL", "3600"))

CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "*")
RATE_LIMIT: str = os.getenv("RATE_LIMIT", "30/minute")

# Redis / background jobs --------------------------------------------------
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Scheduling --------------------------------------------------------------
SCHEDULE_ENABLED: bool = os.getenv("SCHEDULE_ENABLED", "false").lower() in ("1", "true", "yes")
SCHEDULE_INGEST_CRON: str = os.getenv("SCHEDULE_INGEST_CRON", "0 */6 * * *")
SCHEDULE_SYNTHESIS_CRON: str = os.getenv("SCHEDULE_SYNTHESIS_CRON", "30 */6 * * *")
SCHEDULE_USER_ID: str = os.getenv("SCHEDULE_USER_ID", "scheduled")

# Workers / server --------------------------------------------------------
WEB_CONCURRENCY: int = int(os.getenv("WEB_CONCURRENCY", "4"))
WORKER_TIMEOUT: int = int(os.getenv("WORKER_TIMEOUT", "120"))


def validate_config():
    missing = []
    if not DATABASE_URL:
        missing.append("DATABASE_URL")
    if not OLLAMA_BASE_URL:
        missing.append("OLLAMA_BASE_URL")
    if not OLLAMA_API_KEY:
        missing.append("OLLAMA_API_KEY")
    if not LLM_MODEL:
        missing.append("LLM_MODEL")
    if missing:
        raise RuntimeError(f"Missing required config: {', '.join(missing)}")
    if AUTH0_ENABLED and not AUTH0_DOMAIN:
        raise RuntimeError("AUTH0_ENABLED is true but AUTH0_DOMAIN is not set")