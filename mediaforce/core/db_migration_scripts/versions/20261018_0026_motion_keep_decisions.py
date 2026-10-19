"""Remember the owner's choice to keep a file held for its motion pattern original."""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20261018_0026"
down_revision = "20261015_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("motion_keep_decisions"):
        op.create_table(
            "motion_keep_decisions",
            sa.Column(
                "library_item_id",
                sa.Integer(),
                sa.ForeignKey("library_items.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("source_fingerprint", sa.Text(), nullable=False),
            sa.Column("reason_code", sa.Text(), nullable=False),
            sa.Column("prefix", sa.Text(), nullable=False),
            sa.Column("kept_at", sa.Text(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("motion_keep_decisions")
