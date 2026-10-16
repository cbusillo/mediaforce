"""Record a missing file apart from its workflow stage.

A file that went missing used to have its status overwritten with ``missing``,
which lost the stage it had reached. ``missing_since`` now records presence on
its own. Rows already marked missing keep that presence through
``missing_since``; their earlier stage is gone, so they return to
``discovered``, which is the status a later scan gave them when they came back.
"""

import sqlalchemy as sa
# noinspection PyPackageRequirements
from alembic import op

revision = "20261015_0025"
down_revision = "20261012_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("library_items")}
    if "missing_since" not in columns:
        with op.batch_alter_table("library_items") as batch:
            batch.add_column(sa.Column("missing_since", sa.Text(), nullable=True))
    # Marking a file missing stamped updated_at, so it is when the file went missing.
    op.execute(
        sa.text(
            "UPDATE library_items SET missing_since = COALESCE(missing_since, updated_at), status = 'discovered' "
            "WHERE status = 'missing'"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("UPDATE library_items SET status = 'missing' WHERE missing_since IS NOT NULL"))
    with op.batch_alter_table("library_items") as batch:
        batch.drop_column("missing_since")
