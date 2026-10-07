"""Add organizations and memberships; scope conversations/audit_logs by org

Revision ID: 0003_organizations
Revises: 0002_audit_logs
Create Date: 2026-09-30

Backfills a personal organization per distinct user_id and assigns all of that
user's existing rows (conversations, audit_logs) to it. Reversible: downgrade
drops the new tables and columns back to the pre-org schema.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0003_organizations"
down_revision: Union[str, None] = "0002_audit_logs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _personal_slug(user_id: str) -> str:
    import hashlib
    return f"personal-{hashlib.md5(user_id.encode()).hexdigest()[:12]}"


def _digest(user_id: str) -> str:
    import hashlib
    return hashlib.md5(user_id.encode()).hexdigest()[:12]


def upgrade() -> None:
    # --- organizations + memberships -------------------------------------
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_organizations_slug", "organizations", ["slug"], unique=True)

    op.create_table(
        "memberships",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "user_id", name="uq_memberships_org_user"),
    )
    op.create_index("ix_memberships_org_id", "memberships", ["org_id"])
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    _add_membership_fk_org()

    # --- org_id on conversations + audit_logs (nullable initially) -------
    op.add_column("conversations", sa.Column("org_id", sa.String(), nullable=True))
    op.add_column("audit_logs", sa.Column("org_id", sa.String(), nullable=True))

    # --- backfill: personal org per distinct user -------------------------
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute("""
            INSERT INTO organizations (id, name, slug, created_at)
            SELECT 'org_' || substr(md5(u.user_id), 1, 12),
                   u.user_id || '''s workspace',
                   'personal-' || substr(md5(u.user_id), 1, 12),
                   now()
            FROM (
                SELECT user_id FROM conversations
                WHERE user_id IS NOT NULL AND user_id <> ''
                UNION
                SELECT user_id FROM audit_logs
                WHERE user_id IS NOT NULL AND user_id <> ''
            ) u
            ON CONFLICT (slug) DO NOTHING
        """)
        op.execute("""
            INSERT INTO memberships (org_id, user_id, role, created_at)
            SELECT 'org_' || substr(md5(u.user_id), 1, 12), u.user_id, 'admin', now()
            FROM (
                SELECT user_id FROM conversations
                WHERE user_id IS NOT NULL AND user_id <> ''
                UNION
                SELECT user_id FROM audit_logs
                WHERE user_id IS NOT NULL AND user_id <> ''
            ) u
            ON CONFLICT (org_id, user_id) DO NOTHING
        """)
        op.execute("""
            UPDATE conversations c
            SET org_id = 'org_' || substr(md5(c.user_id), 1, 12)
            WHERE c.user_id IS NOT NULL AND c.user_id <> ''
        """)
        op.execute("""
            UPDATE audit_logs a
            SET org_id = 'org_' || substr(md5(a.user_id), 1, 12)
            WHERE a.user_id IS NOT NULL AND a.user_id <> ''
        """)
    else:
        # Portable fallback (sqlite): per-distinct-user inserts via Python.
        from datetime import datetime, timezone
        conn = op.get_bind()
        from sqlalchemy import text
        users = [
            row[0]
            for row in conn.execute(text(
                "SELECT user_id FROM conversations "
                "UNION SELECT user_id FROM audit_logs"
            ))
        ]
        now = datetime.now(timezone.utc).isoformat()
        for user_id in users:
            if not user_id:
                continue
            org_id = f"org_{_digest(user_id)}"
            conn.execute(text(
                "INSERT OR IGNORE INTO organizations (id, name, slug, created_at) "
                "VALUES (:id, :name, :slug, :created)"
            ).bindparams(
                id=org_id,
                name=f"{user_id}'s workspace",
                slug=_personal_slug(user_id),
                created=now,
            ))
            conn.execute(text(
                "INSERT OR IGNORE INTO memberships (org_id, user_id, role, created_at) "
                "VALUES (:org_id, :user_id, 'admin', :created)"
            ).bindparams(
                org_id=org_id, user_id=user_id, created=now,
            ))
            conn.execute(text("UPDATE conversations SET org_id = :org_id WHERE user_id = :user_id")
                         .bindparams(org_id=org_id, user_id=user_id))
            conn.execute(text("UPDATE audit_logs SET org_id = :org_id WHERE user_id = :user_id")
                         .bindparams(org_id=org_id, user_id=user_id))

    # --- enforce org_id + FKs after backfill -----------------------------
    op.alter_column("conversations", "org_id", existing_type=sa.String(), nullable=False)
    op.alter_column("audit_logs", "org_id", existing_type=sa.String(), nullable=False)
    op.create_index("ix_conversations_org_id", "conversations", ["org_id"])
    op.create_index("ix_audit_logs_org_id", "audit_logs", ["org_id"])
    _add_conversation_fk_org()
    _add_audit_log_fk_org()


def _add_membership_fk_org():
    op.create_foreign_key(
        "fk_memberships_org_id", "memberships", "organizations",
        ["org_id"], ["id"], ondelete="CASCADE",
    )


def _add_conversation_fk_org():
    op.create_foreign_key(
        "fk_conversations_org_id", "conversations", "organizations",
        ["org_id"], ["id"], ondelete="CASCADE",
    )


def _add_audit_log_fk_org():
    op.create_foreign_key(
        "fk_audit_logs_org_id", "audit_logs", "organizations",
        ["org_id"], ["id"], ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_audit_logs_org_id", "audit_logs", type_="foreignkey")
    op.drop_constraint("fk_conversations_org_id", "conversations", type_="foreignkey")
    op.drop_index("ix_audit_logs_org_id", table_name="audit_logs")
    op.drop_index("ix_conversations_org_id", table_name="conversations")
    op.drop_column("conversations", "org_id")
    op.drop_column("audit_logs", "org_id")
    op.drop_constraint("fk_memberships_org_id", "memberships", type_="foreignkey")
    op.drop_index("ix_memberships_user_id", table_name="memberships")
    op.drop_index("ix_memberships_org_id", table_name="memberships")
    op.drop_table("memberships")
    op.drop_index("ix_organizations_slug", table_name="organizations")
    op.drop_table("organizations")