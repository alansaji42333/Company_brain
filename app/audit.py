"""Audit trail recording for enterprise compliance.

Every sensitive event (tool proposed/confirmed/declined, skill approved/
rejected) creates an immutable AuditLog row. Recording is best-effort: a
failure to write an audit row logs the error but does not abort the user's
action (the audit row is appended within the caller's existing transaction
where possible, so a DB failure rolls the whole request back).
"""
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import AuditLog

logger = logging.getLogger(__name__)

# Canonical action_type values (validated by convention, not an enum, so the
# column stays a plain indexed string for portability).
ACTION_TOOL_PROPOSED = "tool_proposed"
ACTION_TOOL_CONFIRMED = "tool_confirmed"
ACTION_TOOL_DECLINED = "tool_declined"
ACTION_SKILL_APPROVED = "skill_approved"
ACTION_SKILL_REJECTED = "skill_rejected"


async def record(
    db: AsyncSession,
    user_id: str,
    action_type: str,
    conversation_id: str | None = None,
    tool_name: str | None = None,
    payload: dict | None = None,
    status: str = "success",
) -> None:
    """Append an immutable audit row within the current transaction.

    The caller is responsible for committing. If a DB error occurs it is
    logged and re-raised so the surrounding transaction rolls back (an audit
    failure must not silently lose the record of a sensitive action).
    """
    try:
        db.add(AuditLog(
            user_id=user_id,
            conversation_id=conversation_id,
            action_type=action_type,
            tool_name=tool_name,
            payload=payload,
            status=status,
        ))
        await db.flush()
    except Exception as e:
        logger.error("Failed to record audit %s for user %s: %s", action_type, user_id, e)
        raise


async def list_for_user(db: AsyncSession, user_id: str, limit: int = 100) -> list[dict]:
    """Return the most recent audit rows for a user (for admin inspection)."""
    from sqlalchemy import select
    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.user_id == user_id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "user_id": r.user_id,
            "conversation_id": r.conversation_id,
            "action_type": r.action_type,
            "tool_name": r.tool_name,
            "payload": r.payload,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]