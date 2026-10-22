"""Keep a published file's source measurements apart from the measurements of its output."""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20261022_0027"
down_revision = "20261018_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("library_item_source_evidence"):
        op.create_table(
            "library_item_source_evidence",
            sa.Column(
                "library_item_id",
                sa.Integer(),
                sa.ForeignKey("library_items.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("evidence_kind", sa.Text(), primary_key=True),
            sa.Column("summary_json", sa.Text(), nullable=False),
            sa.Column("source_fingerprint", sa.Text()),
            sa.Column("published_fingerprint", sa.Text()),
            sa.Column("origin", sa.Text(), nullable=False),
            sa.Column("recorded_at", sa.Text(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("library_item_source_evidence")
