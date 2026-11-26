import sqlalchemy as sa
from alembic import op

revision = "20261126_0035"
down_revision = "20261125_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not any(column["name"] == "picture_look" for column in sa.inspect(op.get_bind()).get_columns("animation_sorts")):
        op.add_column("animation_sorts", sa.Column("picture_look", sa.Text()))
    op.execute("UPDATE library_item_evidence_state SET work_status = NULL, last_error = NULL, attempt_count = 0 "
               "WHERE evidence_kind = 'animation_bitrate' AND work_status = 'failed'")
    op.execute("UPDATE animation_sorts SET kbps = NULL, moment_json = NULL WHERE choice IS NULL AND picture_look IS NULL")


def downgrade() -> None:
    op.execute("UPDATE animation_sorts SET kbps = NULL, moment_json = NULL WHERE choice IS NULL")
    op.drop_column("animation_sorts", "picture_look")
