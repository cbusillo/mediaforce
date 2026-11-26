import sqlalchemy as sa
from alembic import op

revision = "20261125_0034"
down_revision = "20261125_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("animation_sorts"):
        op.create_table(
            "animation_sorts",
            sa.Column("title", sa.Text(), primary_key=True),
            sa.Column("item_id", sa.Integer(), sa.ForeignKey("library_items.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source", sa.Text(), nullable=False),
            sa.Column("kbps", sa.REAL()),
            sa.Column("choice", sa.Text()),
            sa.Column("moment_json", sa.Text()),
        )


def downgrade() -> None:
    op.drop_table("animation_sorts")
