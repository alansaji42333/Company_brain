"""Add drive_oauth_pendings: server-side pending Drive OAuth transactions

Revision ID: 0005_drive_oauth_pendings
Revises: 0004_connector_configs
Create Date: 2026-11-09

One row per in-progress Drive OAuth flow. The row (not the opaque state
nonce) is the callback's authority: it binds the flow to (org_id, user_id,
folder_id), to the initiating browser via a hashed browser secret (delivered
as an HttpOnly callback-scoped cookie), and carries the PKCE code_verifier
ENCRYPTED at rest. Timestamps are epoch-integer seconds; `consumed_at` stays
NULL until the single atomic consume (replay protection). Expired rows are
lazily deleted. Fully reversible: downgrade drops the table. No secrets are
stored in plaintext.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_drive_oauth_pendings"
down_revision: str | None = "0004_connector_configs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "drive_oauth_pendings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("nonce_hash", sa.String(64), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("folder_id", sa.String(), nullable=False),
        sa.Column("browser_secret_hash", sa.String(64), nullable=False),
        sa.Column("code_verifier_encrypted", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("consumed_at", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_drive_oauth_pendings_nonce_hash", "drive_oauth_pendings", ["nonce_hash"])
    op.create_index("ix_drive_oauth_pendings_org_id", "drive_oauth_pendings", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_drive_oauth_pendings_org_id", table_name="drive_oauth_pendings")
    op.drop_index("ix_drive_oauth_pendings_nonce_hash", table_name="drive_oauth_pendings")
    op.drop_table("drive_oauth_pendings")
