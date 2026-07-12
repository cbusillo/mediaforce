"""Track successful external metadata refreshes.

Revision ID: 20260712_0010
Revises: 20260712_0009
Create Date: 2026-07-12 19:45:00
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20260712_0010"
down_revision = "20260712_0009"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("metadata_sync_state"):
        op.create_table(
            "metadata_sync_state",
            sa.Column("provider", sa.Text(), primary_key=True),
            sa.Column("last_success_at", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.Text(), nullable=False),
        )


def downgrade() -> None:
    if _has_table("metadata_sync_state"):
        op.drop_table("metadata_sync_state")
