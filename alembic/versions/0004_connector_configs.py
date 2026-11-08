"""Add org_connector_configs: per-org encrypted connector credentials

Revision ID: 0004_connector_configs
Revises: 0003_organizations
Create Date: 2026-11-08

One row per (org_id, source) storing the Fernet-encrypted JSON connector
config (slack: bot_token + channel_ids; drive: folder_id + credentials_json
+ token_json). Secrets live in the encrypted column only — never in logs or
audit payloads. Fully reversible: downgrade drops the table.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_connector_configs"
down_revision: str | None = "0003_organizations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "org_connector_configs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("config_encrypted", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "source", name="uq_org_connector_org_source"),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"],
            name="fk_org_connector_configs_org_id", ondelete="CASCADE",
        ),
    )
    op.create_index("ix_org_connector_configs_org_id", "org_connector_configs", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_org_connector_configs_org_id", table_name="org_connector_configs")
    op.drop_table("org_connector_configs")
