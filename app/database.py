import logging
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

from app.config import DATABASE_URL

logger = logging.getLogger(__name__)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=3600,
)
async_session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Organization(Base):
    """A workspace (tenant). Users belong to organizations via memberships."""
    __tablename__ = "organizations"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    slug = Column(String, nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    memberships = relationship("Membership", backref="organization", lazy="selectin",
                               cascade="all, delete-orphan")


class Membership(Base):
    """Role of a user within an organization."""
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_memberships_org_user"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    role = Column(String(16), nullable=False, server_default="member")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(String, primary_key=True)
    org_id = Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)
    pending_action = Column(JSON, nullable=True)

    messages = relationship("Message", backref="conversation", lazy="selectin",
                            order_by="Message.created_at", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(16), nullable=False)
    content = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class AuditLog(Base):
    """Immutable record of a sensitive action for enterprise compliance.

    One row per: tool_proposed, tool_confirmed, tool_declined,
    skill_approved, skill_rejected. Never updated or deleted by app code.
    """
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    conversation_id = Column(String, nullable=True, index=True)
    action_type = Column(String(32), nullable=False, index=True)
    tool_name = Column(String(64), nullable=True)
    payload = Column(JSON, nullable=True)
    status = Column(String(16), nullable=False, default="success")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class OrgConnectorConfig(Base):
    """Per-org connector credentials (Slack bot token, Google Drive OAuth),
    stored ENCRYPTED at rest (Fernet ciphertext of the JSON config in
    config_encrypted). Secrets are never returned by API reads (see
    app/connectors.get_connector_public) and never logged or audited.

    One row per (org_id, source). An org without a row for a source falls
    back to the process-level env/file configuration, preserving the
    single-tenant deployment behavior.
    """
    __tablename__ = "org_connector_configs"
    __table_args__ = (UniqueConstraint("org_id", "source", name="uq_org_connector_org_source"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    source = Column(String(16), nullable=False)  # "slack" | "drive"
    config_encrypted = Column(Text, nullable=False)
    updated_by = Column(String, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)


class DriveOAuthPending(Base):
    """One in-progress Drive OAuth transaction, created by the start endpoint.

    This row — not the opaque `state` string — is the authority for the
    callback: it binds the flow to (org, initiating user, folder), to the
    INITIATING BROWSER (hash of a high-entropy browser secret delivered as an
    HttpOnly callback-scoped cookie), and carries the PKCE code_verifier
    ENCRYPTED at rest (same Fernet facilities as connector secrets).

    - nonce_hash: sha256 of the random `state` nonce Google echoes back. The
      raw nonce is never stored.
    - browser_secret_hash: sha256 of the browser secret; compared
      constant-time at callback time BEFORE consuming or exchanging.
    - code_verifier_encrypted: Fernet ciphertext (secrets machinery in
      app.connectors), decrypted only to perform the token exchange.
    - created_at / expires_at / consumed_at: epoch-integer seconds. `expires_at`
      is ~10 minutes after creation; `consumed_at` stays NULL until the single
      atomic consume UPDATE flips it (replay protection). Rows are lazily
      deleted once expired.
    """
    __tablename__ = "drive_oauth_pendings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    nonce_hash = Column(String(64), nullable=False, index=True)
    org_id = Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String, nullable=False)
    folder_id = Column(String, nullable=False)
    browser_secret_hash = Column(String(64), nullable=False)
    code_verifier_encrypted = Column(Text, nullable=False)
    created_at = Column(BigInteger, nullable=False)
    expires_at = Column(BigInteger, nullable=False)
    consumed_at = Column(BigInteger, nullable=True)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()