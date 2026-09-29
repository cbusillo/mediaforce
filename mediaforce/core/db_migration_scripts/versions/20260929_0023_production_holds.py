"""Remember files a production run held back so they can join it once their evidence clears."""

import sqlalchemy as sa
from alembic import op

revision = "20260929_0023"
down_revision = "20260908_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("production_holds"):
        op.create_table(
            "production_holds",
            sa.Column(
                "library_item_id",
                sa.Integer(),
                sa.ForeignKey("library_items.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("prefix", sa.Text(), nullable=False),
            sa.Column("mode", sa.Text(), nullable=False),
            sa.Column("approval_identity", sa.Text(), nullable=False),
            sa.Column("reason_code", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("held_at", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
        )
    if "idx_production_holds_status" not in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("production_holds")
    }:
        op.create_index("idx_production_holds_status", "production_holds", ["status"])


def downgrade() -> None:
    op.drop_index("idx_production_holds_status", table_name="production_holds")
    op.drop_table("production_holds")
